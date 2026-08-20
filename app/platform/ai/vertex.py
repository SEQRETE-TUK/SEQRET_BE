"""Vertex AI (google-genai) adapter returning only the versioned draft contract.

The adapter turns an :class:`AnalysisRequest` into a Gemini call against
A-approved media in private object storage and validates the model output
fail-closed into an :class:`AnalysisResult`. It never touches A-owned ORM models
or ``scope_version``; the result is an editable draft. Raw media, prompts, GCS
URIs, and provider error bodies are kept out of logs and return values.

Idempotency is intentionally enforced at the analysis-run persistence layer
(B-03/B-06), so this adapter does not cache by idempotency key.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import NoReturn, Protocol, cast

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.contracts.ai import (
    AnalysisCarryDistanceCondition,
    AnalysisElevatorAvailability,
    AnalysisFloorCondition,
    AnalysisKnowledgeStatus,
    AnalysisLocationConditionField,
    AnalysisParkingAccess,
    AnalysisRequest,
    AnalysisResidenceType,
    AnalysisResult,
    AnalysisStairUsage,
    DraftItem,
    DraftLocationCondition,
)
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.contracts.primitives import IdempotencyKey, MediaAssetId


class AnalysisFailureStage(StrEnum):
    """Stage that a Vertex analysis failure is attributed to, for diagnosis."""

    PROMPT = "prompt"
    PROVIDER_CALL = "provider_call"
    PARSE = "parse"
    SOURCE_MAP = "source_map"


class _StrictProviderModel(BaseModel):
    """Provider JSON model that rejects fields outside the versioned schema."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _RawDraftItem(_StrictProviderModel):
    """Strict shape the model must return for one draft item."""

    item_key: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0.0, le=1.0)
    source_indices: tuple[int, ...]
    review_required: bool = False


class _RawAnalysisOutput(_StrictProviderModel):
    """Strict envelope validated fail-closed before mapping to the contract."""

    items: tuple[_RawDraftItem, ...] = Field(min_length=1)


class _RawDraftItemV2(_StrictProviderModel):
    """Structured item output used only by result schema v2."""

    item_key: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2000)
    name: str = Field(min_length=1, max_length=200)
    quantity: int | None = Field(default=None, ge=1)
    unit: str | None = Field(default=None, min_length=1, max_length=20)
    work_note: str | None = Field(default=None, min_length=1, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)
    source_indices: tuple[int, ...] = Field(min_length=1)
    review_required: bool = False


class _RawFloorCondition(_StrictProviderModel):
    status: AnalysisKnowledgeStatus = "unknown"
    value: int | None = Field(default=None, ge=-10, le=200)


class _RawCarryDistanceCondition(_StrictProviderModel):
    status: AnalysisKnowledgeStatus = "unknown"
    value_m: int | None = Field(default=None, ge=0, le=100_000)


class _RawLocationConditionV2(_StrictProviderModel):
    """Location suggestion keyed only by input indices, never provider IDs."""

    source_indices: tuple[int, ...] = Field(min_length=1)
    residence_type: AnalysisResidenceType = "unknown"
    floor: _RawFloorCondition = Field(default_factory=_RawFloorCondition)
    elevator: AnalysisElevatorAvailability = "unknown"
    stairs: AnalysisStairUsage = "unknown"
    parking_access: AnalysisParkingAccess = "unknown"
    carry_distance: _RawCarryDistanceCondition = Field(default_factory=_RawCarryDistanceCondition)
    access_note: str | None = Field(default=None, min_length=1, max_length=1000)
    confidence: float = Field(ge=0.0, le=1.0)
    review_required_fields: tuple[AnalysisLocationConditionField, ...] = ()


class _RawAnalysisOutputV2(_StrictProviderModel):
    """Strict v2 envelope for inventory and quote-impacting conditions."""

    items: tuple[_RawDraftItemV2, ...] = Field(min_length=1)
    location_conditions: tuple[_RawLocationConditionV2, ...] = ()


class GenerateContentResponse(Protocol):
    text: str | None


class GenerativeModels(Protocol):
    def generate_content(
        self,
        *,
        model: str,
        contents: list[object],
        config: object,
    ) -> GenerateContentResponse: ...


class GenAIClient(Protocol):
    @property
    def models(self) -> GenerativeModels: ...


def _default_client(project: str, location: str) -> GenAIClient:  # pragma: no cover
    """Create the real Vertex client; exercised only with live credentials."""

    return cast(GenAIClient, genai.Client(vertexai=True, project=project, location=location))


def _classify_provider_error(error: Exception) -> tuple[ProviderErrorKind, bool, int]:
    """Map a provider exception to (kind, retryable, http_status) without its body."""

    if isinstance(error, errors.APIError):
        code = error.code or 0
        if code == 400:
            kind, retryable = ProviderErrorKind.INVALID_INPUT, False
        elif code in (401, 403):
            kind, retryable = ProviderErrorKind.PERMISSION_DENIED, False
        elif code == 404:
            kind, retryable = ProviderErrorKind.NOT_FOUND, False
        elif code == 409:
            kind, retryable = ProviderErrorKind.CONFLICT, False
        elif code == 504:
            kind, retryable = ProviderErrorKind.DEADLINE_EXCEEDED, True
        else:
            kind, retryable = ProviderErrorKind.UNAVAILABLE, True
        return kind, retryable, code
    return ProviderErrorKind.UNAVAILABLE, True, 0


class VertexAIProvider:
    """AIProviderPort adapter keeping Vertex types out of the application layer."""

    def __init__(
        self,
        *,
        project: str,
        location: str,
        bucket_name: str,
        prompt_library: Mapping[str, str],
        client_factory: Callable[[], GenAIClient] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        factory = client_factory or (lambda: _default_client(project, location))
        self._client = factory()
        self._bucket_name = bucket_name
        self._prompts = dict(prompt_library)
        self._logger = logger

    def _log_failure(
        self,
        stage: AnalysisFailureStage,
        kind: ProviderErrorKind,
        retryable: bool,
        *,
        status_code: int = 0,
    ) -> None:
        """Emit one structured failure event; never logs media, prompts, or URIs."""

        if self._logger is None:
            return
        self._logger.warning(
            "analysis provider failure",
            extra={
                "event": "analysis_provider_failure",
                "analysis_stage": stage.value,
                "provider_status": status_code,
                "error_kind": kind.value,
                "retryable": retryable,
            },
        )

    def _reject_output(
        self,
        message: str,
        *,
        stage: AnalysisFailureStage = AnalysisFailureStage.PARSE,
    ) -> NoReturn:
        self._log_failure(stage, ProviderErrorKind.INVALID_INPUT, False)
        raise ProviderError(
            ProviderErrorKind.INVALID_INPUT,
            message,
            retryable=False,
        )

    @staticmethod
    def _v2_prompt(request: AnalysisRequest, base_prompt: str) -> str:
        """Append non-address source topology and the v2 review contract."""

        location_ordinals: dict[object, int] = {}
        room_ordinals: dict[object, int] = {}
        source_lines: list[str] = []
        for index, context in enumerate(request.source_contexts):
            location_group = location_ordinals.setdefault(
                context.location_id,
                len(location_ordinals),
            )
            room_group = room_ordinals.setdefault(context.room_zone_id, len(room_ordinals))
            source_lines.append(
                f"source_index={index}, location={context.location_kind}, "
                f"location_group={location_group}, room_group={room_group}"
            )
        instructions = (
            "SEQRET result schema v2: identify each item's name, quantity, unit, "
            "optional work_note, confidence, and every supporting source_index. "
            "If quantity or unit is uncertain, set both to null and "
            "review_required=true. Also suggest quote-impacting location conditions "
            "for origin/destination using only supporting source_indices. Use explicit "
            "unknown states and list every uncertain field in review_required_fields. "
            "Never infer or return an address, person, UUID, or final price."
        )
        return "\n\n".join((base_prompt, instructions, "\n".join(source_lines)))

    async def analyze(
        self,
        *,
        request: AnalysisRequest,
        idempotency_key: IdempotencyKey,
        timeout_seconds: float,
    ) -> AnalysisResult:
        del idempotency_key
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        try:
            prompt = self._prompts[request.prompt_version]
        except KeyError as error:
            self._log_failure(AnalysisFailureStage.PROMPT, ProviderErrorKind.INVALID_INPUT, False)
            raise ProviderError(
                ProviderErrorKind.INVALID_INPUT,
                "analysis prompt version is not configured",
                retryable=False,
            ) from error

        rendered_prompt = (
            prompt
            if request.requested_result_schema_version == 1
            else self._v2_prompt(request, prompt)
        )
        contents: list[object] = [rendered_prompt]
        contents.extend(
            types.Part.from_uri(
                file_uri=f"gs://{self._bucket_name}/{object_key}",
                mime_type=content_type,
            )
            for object_key, content_type in zip(
                request.object_keys,
                request.content_types,
                strict=True,
            )
        )
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=(
                _RawAnalysisOutput
                if request.requested_result_schema_version == 1
                else _RawAnalysisOutputV2
            ),
        )

        def call() -> str | None:
            return self._client.models.generate_content(
                model=request.model_name,
                contents=contents,
                config=config,
            ).text

        try:
            text = await asyncio.wait_for(asyncio.to_thread(call), timeout=timeout_seconds)
        except TimeoutError as error:
            self._log_failure(
                AnalysisFailureStage.PROVIDER_CALL, ProviderErrorKind.DEADLINE_EXCEEDED, True
            )
            raise ProviderError(
                ProviderErrorKind.DEADLINE_EXCEEDED,
                "analysis provider call failed",
                retryable=True,
            ) from error
        except Exception as error:
            kind, retryable, status_code = _classify_provider_error(error)
            self._log_failure(
                AnalysisFailureStage.PROVIDER_CALL, kind, retryable, status_code=status_code
            )
            raise ProviderError(
                kind, "analysis provider call failed", retryable=retryable
            ) from error

        return self._to_result(request, text)

    def _to_result(self, request: AnalysisRequest, text: str | None) -> AnalysisResult:
        if text is None:
            self._log_failure(AnalysisFailureStage.PARSE, ProviderErrorKind.UNAVAILABLE, True)
            raise ProviderError(
                ProviderErrorKind.UNAVAILABLE,
                "analysis provider returned no content",
                retryable=True,
            )
        try:
            if request.requested_result_schema_version == 1:
                return self._to_result_v1(
                    request,
                    _RawAnalysisOutput.model_validate_json(text),
                )
            return self._to_result_v2(
                request,
                _RawAnalysisOutputV2.model_validate_json(text),
            )
        except ValidationError as error:
            self._log_failure(AnalysisFailureStage.PARSE, ProviderErrorKind.INVALID_INPUT, False)
            raise ProviderError(
                ProviderErrorKind.INVALID_INPUT,
                "analysis provider returned malformed output",
                retryable=False,
            ) from error

    def _source_assets(
        self,
        request: AnalysisRequest,
        source_indices: tuple[int, ...],
        *,
        allow_single_source_fallback: bool,
    ) -> tuple[MediaAssetId, ...]:
        source_count = len(request.source_media_asset_ids)
        normalized = (0,) if allow_single_source_fallback and source_count == 1 else source_indices
        if not normalized or len(normalized) != len(set(normalized)):
            self._reject_output(
                "analysis output contained invalid media references",
                stage=AnalysisFailureStage.SOURCE_MAP,
            )
        sources = []
        for index in normalized:
            if index < 0 or index >= source_count:
                self._reject_output(
                    "analysis output referenced unknown media",
                    stage=AnalysisFailureStage.SOURCE_MAP,
                )
            sources.append(request.source_media_asset_ids[index])
        return tuple(sources)

    def _to_result_v1(
        self,
        request: AnalysisRequest,
        raw: _RawAnalysisOutput,
    ) -> AnalysisResult:

        draft_items: list[DraftItem] = []
        review_required_items: list[DraftItem] = []
        item_keys = [item.item_key for item in raw.items]
        if len(item_keys) != len(set(item_keys)):
            self._reject_output(
                "analysis provider returned duplicate item keys",
            )
        for item in raw.items:
            sources = self._source_assets(
                request,
                item.source_indices,
                allow_single_source_fallback=True,
            )
            draft = DraftItem(
                item_key=item.item_key,
                description=item.description,
                confidence=item.confidence,
                source_media_asset_ids=sources,
            )
            (review_required_items if item.review_required else draft_items).append(draft)

        return AnalysisResult(
            analysis_run_id=request.analysis_run_id,
            capture_session_id=request.capture_session_id,
            model_name=request.model_name,
            model_version=request.model_version,
            prompt_version=request.prompt_version,
            draft_items=tuple(draft_items),
            review_required_items=tuple(review_required_items),
        )

    def _to_result_v2(
        self,
        request: AnalysisRequest,
        raw: _RawAnalysisOutputV2,
    ) -> AnalysisResult:
        draft_items: list[DraftItem] = []
        review_required_items: list[DraftItem] = []
        item_keys = [item.item_key for item in raw.items]
        if len(item_keys) != len(set(item_keys)):
            self._reject_output("analysis provider returned duplicate item keys")

        for item in raw.items:
            sources = self._source_assets(
                request,
                item.source_indices,
                allow_single_source_fallback=True,
            )
            draft = DraftItem(
                item_key=item.item_key,
                description=item.description,
                name=item.name,
                quantity=item.quantity,
                unit=item.unit,
                work_note=item.work_note,
                confidence=item.confidence,
                source_media_asset_ids=sources,
            )
            (review_required_items if item.review_required else draft_items).append(draft)

        location_suggestions: list[DraftLocationCondition] = []
        location_ids: set[object] = set()
        location_kinds: set[str] = set()
        for condition in raw.location_conditions:
            source_indices = (
                (0,) if len(request.source_media_asset_ids) == 1 else condition.source_indices
            )
            sources = self._source_assets(
                request,
                source_indices,
                allow_single_source_fallback=True,
            )
            contexts = [request.source_contexts[index] for index in source_indices]
            context_location_ids = {context.location_id for context in contexts}
            context_location_kinds = {context.location_kind for context in contexts}
            if len(context_location_ids) != 1 or len(context_location_kinds) != 1:
                self._reject_output(
                    "analysis location output mixed source locations",
                    stage=AnalysisFailureStage.SOURCE_MAP,
                )
            location_id = contexts[0].location_id
            location_kind = contexts[0].location_kind
            if location_id in location_ids or location_kind in location_kinds:
                self._reject_output("analysis provider returned duplicate location suggestions")
            location_ids.add(location_id)
            location_kinds.add(location_kind)
            location_suggestions.append(
                DraftLocationCondition(
                    location_id=location_id,
                    location_kind=location_kind,
                    residence_type=condition.residence_type,
                    floor=AnalysisFloorCondition(
                        status=condition.floor.status,
                        value=condition.floor.value,
                    ),
                    elevator=condition.elevator,
                    stairs=condition.stairs,
                    parking_access=condition.parking_access,
                    carry_distance=AnalysisCarryDistanceCondition(
                        status=condition.carry_distance.status,
                        value_m=condition.carry_distance.value_m,
                    ),
                    access_note=condition.access_note,
                    confidence=condition.confidence,
                    review_required_fields=condition.review_required_fields,
                    source_media_asset_ids=sources,
                )
            )

        return AnalysisResult(
            analysis_run_id=request.analysis_run_id,
            capture_session_id=request.capture_session_id,
            model_name=request.model_name,
            model_version=request.model_version,
            prompt_version=request.prompt_version,
            result_schema_version=2,
            draft_items=tuple(draft_items),
            review_required_items=tuple(review_required_items),
            location_condition_suggestions=tuple(location_suggestions),
        )
