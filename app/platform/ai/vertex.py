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
from typing import Protocol, cast

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field, ValidationError

from app.contracts.ai import AnalysisRequest, AnalysisResult, DraftItem
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.contracts.primitives import IdempotencyKey


class AnalysisFailureStage(StrEnum):
    """Stage that a Vertex analysis failure is attributed to, for diagnosis."""

    PROMPT = "prompt"
    PROVIDER_CALL = "provider_call"
    PARSE = "parse"
    SOURCE_MAP = "source_map"


class _RawDraftItem(BaseModel):
    """Strict shape the model must return for one draft item."""

    item_key: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0.0, le=1.0)
    source_indices: tuple[int, ...]
    review_required: bool = False


class _RawAnalysisOutput(BaseModel):
    """Strict envelope validated fail-closed before mapping to the contract."""

    items: tuple[_RawDraftItem, ...] = Field(min_length=1)


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

        contents: list[object] = [prompt]
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
            response_schema=_RawAnalysisOutput,
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
            raw = _RawAnalysisOutput.model_validate_json(text)
        except ValidationError as error:
            self._log_failure(AnalysisFailureStage.PARSE, ProviderErrorKind.INVALID_INPUT, False)
            raise ProviderError(
                ProviderErrorKind.INVALID_INPUT,
                "analysis provider returned malformed output",
                retryable=False,
            ) from error

        source_count = len(request.source_media_asset_ids)
        draft_items: list[DraftItem] = []
        review_required_items: list[DraftItem] = []
        item_keys = [item.item_key for item in raw.items]
        if len(item_keys) != len(set(item_keys)):
            self._log_failure(AnalysisFailureStage.PARSE, ProviderErrorKind.INVALID_INPUT, False)
            raise ProviderError(
                ProviderErrorKind.INVALID_INPUT,
                "analysis provider returned duplicate item keys",
                retryable=False,
            )
        for item in raw.items:
            if not item.source_indices or len(item.source_indices) != len(set(item.source_indices)):
                self._log_failure(
                    AnalysisFailureStage.SOURCE_MAP, ProviderErrorKind.INVALID_INPUT, False
                )
                raise ProviderError(
                    ProviderErrorKind.INVALID_INPUT,
                    "analysis output contained invalid media references",
                    retryable=False,
                )
            sources = []
            for index in item.source_indices:
                if index < 0 or index >= source_count:
                    self._log_failure(
                        AnalysisFailureStage.SOURCE_MAP, ProviderErrorKind.INVALID_INPUT, False
                    )
                    raise ProviderError(
                        ProviderErrorKind.INVALID_INPUT,
                        "analysis output referenced unknown media",
                        retryable=False,
                    )
                sources.append(request.source_media_asset_ids[index])
            draft = DraftItem(
                item_key=item.item_key,
                description=item.description,
                confidence=item.confidence,
                source_media_asset_ids=tuple(sources),
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
