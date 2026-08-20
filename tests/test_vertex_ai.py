"""B-04 Vertex AI (google-genai) adapter tests without network access."""

import json
import logging
import time
from copy import deepcopy
from uuid import uuid4

import pytest
from google.genai import _transformers, errors, types

from app.contracts.ai import (
    AnalysisFailureDetail,
    AnalysisFailureStage,
    AnalysisRequest,
    AnalysisResult,
    AnalysisSourceContext,
)
from app.contracts.ports import AIProviderPort, ProviderError, ProviderErrorKind
from app.contracts.primitives import AnalysisRunId, CaptureSessionId, IdempotencyKey, MediaAssetId
from app.platform.ai.vertex import (
    MAX_ANALYSIS_ITEMS,
    MAX_LOCATION_CONDITIONS,
    MAX_PERSISTED_QUANTITY,
    VertexAIProvider,
)

PROMPTS = {"inventory-1": "이삿짐을 방별로 나열하라"}
KEY = IdempotencyKey("analysis:1")


class StubResponse:
    def __init__(self, text: str | None) -> None:
        self.text = text


class StubModels:
    def __init__(
        self,
        *,
        text: str | None = None,
        error: Exception | None = None,
        sleep_seconds: float = 0.0,
    ) -> None:
        self._text = text
        self._error = error
        self._sleep_seconds = sleep_seconds
        self.calls: list[dict[str, object]] = []

    def generate_content(
        self,
        *,
        model: str,
        contents: list[object],
        config: object,
    ) -> StubResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self._sleep_seconds:
            time.sleep(self._sleep_seconds)
        if self._error is not None:
            raise self._error
        return StubResponse(self._text)


class StubClient:
    def __init__(self, models: StubModels) -> None:
        self._models = models

    @property
    def models(self) -> StubModels:
        return self._models


class FakeAPIError(errors.APIError):
    def __init__(self, code: int) -> None:
        self.code = code
        Exception.__init__(self, "stub api error")


def _provider(models: StubModels) -> VertexAIProvider:
    return VertexAIProvider(
        project="seqret-dev",
        location="us-central1",
        bucket_name="seqret-media",
        prompt_library=PROMPTS,
        client_factory=lambda: StubClient(models),
    )


def _request(
    *,
    source_count: int = 2,
    prompt_version: str = "inventory-1",
) -> AnalysisRequest:
    sources = tuple(MediaAssetId(uuid4()) for _ in range(source_count))
    keys = tuple(f"jobs/1/room{index}" for index in range(source_count))
    return AnalysisRequest(
        analysis_run_id=AnalysisRunId(uuid4()),
        capture_session_id=CaptureSessionId(uuid4()),
        source_media_asset_ids=sources,
        object_keys=keys,
        content_types=tuple("video/mp4" for _ in sources),
        model_name="gemini-2.5-flash",
        model_version="2025-08",
        prompt_version=prompt_version,
    )


def _request_v2() -> AnalysisRequest:
    sources = (MediaAssetId(uuid4()), MediaAssetId(uuid4()))
    origin_id = uuid4()
    destination_id = uuid4()
    return AnalysisRequest(
        analysis_run_id=AnalysisRunId(uuid4()),
        capture_session_id=CaptureSessionId(uuid4()),
        source_media_asset_ids=sources,
        object_keys=("jobs/1/origin.mp4", "jobs/1/destination.mp4"),
        content_types=("video/mp4", "video/mp4"),
        model_name="gemini-2.5-flash",
        model_version="2025-08",
        prompt_version="inventory-1",
        requested_result_schema_version=2,
        source_contexts=(
            AnalysisSourceContext(
                media_asset_id=sources[0],
                location_id=origin_id,
                location_kind="origin",
                room_zone_id=uuid4(),
            ),
            AnalysisSourceContext(
                media_asset_id=sources[1],
                location_id=destination_id,
                location_kind="destination",
                room_zone_id=uuid4(),
            ),
        ),
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_analyze_maps_structured_output_to_draft() -> None:
    output = (
        '{"items": ['
        '{"item_key": "bed", "description": "퀸 침대", "confidence": 0.9, "source_indices": [0]},'
        '{"item_key": "box", "description": "확인 필요 박스", "confidence": 0.3,'
        ' "source_indices": [1], "review_required": true}'
        "]}"
    )
    models = StubModels(text=output)
    provider = _provider(models)
    request = _request()

    assert isinstance(provider, AIProviderPort)
    result = await provider.analyze(request=request, idempotency_key=KEY, timeout_seconds=30)

    assert result.model_name == "gemini-2.5-flash"
    assert result.model_version == "2025-08"
    assert result.prompt_version == "inventory-1"
    assert result.analysis_run_id == request.analysis_run_id
    assert [item.item_key for item in result.draft_items] == ["bed"]
    assert result.draft_items[0].source_media_asset_ids == (request.source_media_asset_ids[0],)
    assert [item.item_key for item in result.review_required_items] == ["box"]
    assert result.review_required_items[0].source_media_asset_ids == (
        request.source_media_asset_ids[1],
    )
    call = models.calls[0]
    assert call["model"] == "gemini-2.5-flash"
    assert isinstance(call["contents"], list)
    assert len(call["contents"]) == 3  # prompt + two media parts
    media_parts = call["contents"][1:]
    assert [part.file_data.mime_type for part in media_parts] == ["video/mp4", "video/mp4"]
    assert [part.file_data.file_uri for part in media_parts] == [
        "gs://seqret-media/jobs/1/room0",
        "gs://seqret-media/jobs/1/room1",
    ]
    config = call["config"]
    assert isinstance(config, types.GenerateContentConfig)
    assert config.temperature == 0


@pytest.mark.anyio
async def test_analyze_v2_maps_items_and_location_context_without_exposing_ids() -> None:
    output = (
        '{"items": ['
        '{"item_key": "fridge", "description": "냉장고 1대", "name": "냉장고", '
        '"quantity": 1, "unit": "대", "work_note": "문 분리 확인", '
        '"confidence": 0.91, "source_indices": [0]},'
        '{"item_key": "boxes", "description": "박스 수량 확인", "name": "박스", '
        '"quantity": null, "unit": null, "confidence": 0.45, '
        '"source_indices": [1], "review_required": true}], '
        '"location_conditions": ['
        '{"source_indices": [0], "residence_type": "apartment", '
        '"floor": {"status": "known", "value": 12}, "elevator": "available", '
        '"stairs": "not_required", "parking_access": "restricted", '
        '"carry_distance": {"status": "known", "value_m": 40}, '
        '"access_note": "진입 확인 필요", "confidence": 0.83, '
        '"review_required_fields": ["parking_access", "access_note"]},'
        '{"source_indices": [1], "confidence": 0.3, '
        '"review_required_fields": ["floor", "elevator", "carry_distance"]}]}'
    )
    models = StubModels(text=output)
    provider = _provider(models)
    request = _request_v2()

    result = await provider.analyze(request=request, idempotency_key=KEY, timeout_seconds=30)

    assert result.result_schema_version == 2
    assert result.draft_items[0].model_dump() == {
        "item_key": "fridge",
        "description": "냉장고 1대",
        "name": "냉장고",
        "quantity": 1,
        "unit": "대",
        "work_note": "문 분리 확인",
        "confidence": 0.91,
        "source_media_asset_ids": (request.source_media_asset_ids[0],),
    }
    assert result.review_required_items[0].quantity is None
    assert [suggestion.location_kind for suggestion in result.location_condition_suggestions] == [
        "origin",
        "destination",
    ]
    assert result.location_condition_suggestions[0].location_id == (
        request.source_contexts[0].location_id
    )
    assert result.location_condition_suggestions[1].floor.status == "unknown"
    contents = models.calls[0]["contents"]
    assert isinstance(contents, list)
    rendered_prompt = contents[0]
    assert isinstance(rendered_prompt, str)
    assert "result schema v2" in rendered_prompt
    assert "source_index=0, location=origin" in rendered_prompt
    assert str(request.source_contexts[0].location_id) not in rendered_prompt
    assert AnalysisResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.anyio
async def test_analyze_v2_uses_simple_wire_schema_and_strict_local_contract() -> None:
    output = (
        '{"items": [{"item_key": "bed", "description": "침대", "name": "침대", '
        '"quantity": 1, "unit": "개", "confidence": 0.9, "source_indices": [0]}]}'
    )
    models = StubModels(text=output)

    await _provider(models).analyze(
        request=_request_v2(),
        idempotency_key=KEY,
        timeout_seconds=30,
    )

    config = models.calls[0]["config"]
    assert isinstance(config, types.GenerateContentConfig)
    json_schema = config.response_schema
    assert isinstance(json_schema, dict)
    items_schema = json_schema["properties"]["items"]
    assert items_schema["items"] == {"$ref": "#/$defs/_RawDraftItemV2"}
    assert items_schema["type"] == "array"
    assert "minItems" not in items_schema
    assert "maxItems" not in items_schema
    location_schema = json_schema["properties"]["location_conditions"]
    assert "maxItems" not in location_schema
    item_properties = json_schema["$defs"]["_RawDraftItemV2"]["properties"]
    assert "maximum" not in item_properties["quantity"]["anyOf"][0]
    assert "minimum" not in item_properties["source_indices"]["items"]
    assert json_schema["required"] == ["items"]

    wire_schema = _transformers.t_schema(None, deepcopy(json_schema))
    assert wire_schema is not None
    assert wire_schema.properties is not None
    wire_items = wire_schema.properties["items"]
    assert wire_items.min_items is None
    assert wire_items.max_items is None
    assert wire_items.items is not None
    assert wire_items.items.properties is not None
    wire_source_indices = wire_items.items.properties["source_indices"]
    assert wire_source_indices.items is not None
    assert wire_source_indices.items.minimum is None


@pytest.mark.anyio
async def test_analyze_v2_normalizes_blank_optional_text_to_reviewable_unknowns() -> None:
    output = (
        '{"items": [{"item_key": "boxes", "description": "박스", "name": "박스", '
        '"quantity": 3, "unit": "   ", "work_note": "  ", "confidence": 0.6, '
        '"source_indices": [0]}], "location_conditions": [{"source_indices": [0], '
        '"access_note": "  ", "confidence": 0.4}]}'
    )

    result = await _provider(StubModels(text=output)).analyze(
        request=_request_v2(),
        idempotency_key=KEY,
        timeout_seconds=30,
    )

    assert result.draft_items == ()
    assert len(result.review_required_items) == 1
    item = result.review_required_items[0]
    assert item.quantity is None
    assert item.unit is None
    assert item.work_note is None
    assert result.location_condition_suggestions[0].access_note is None


@pytest.mark.anyio
async def test_analyze_v2_rejects_values_outside_persistence_contract() -> None:
    item = {
        "item_key": "bed",
        "description": "침대",
        "name": "침대",
        "quantity": 1,
        "unit": "개",
        "confidence": 0.9,
        "source_indices": [0],
    }
    cases = (
        {"items": [{**item, "quantity": MAX_PERSISTED_QUANTITY + 1}]},
        {"items": [{**item, "source_indices": [-1]}]},
        {
            "items": [
                {**item, "item_key": f"item-{index}"} for index in range(MAX_ANALYSIS_ITEMS + 1)
            ],
        },
        {
            "items": [item],
            "location_conditions": [
                {"source_indices": [0], "confidence": 0.5}
                for _ in range(MAX_LOCATION_CONDITIONS + 1)
            ],
        },
    )

    for payload in cases:
        with pytest.raises(ProviderError, match="malformed") as error_info:
            await _provider(StubModels(text=json.dumps(payload))).analyze(
                request=_request_v2(),
                idempotency_key=KEY,
                timeout_seconds=30,
            )

        assert error_info.value.kind is ProviderErrorKind.UNAVAILABLE
        assert error_info.value.retryable is True
        assert error_info.value.failure_stage is AnalysisFailureStage.PARSE
        assert error_info.value.failure_detail is AnalysisFailureDetail.SCHEMA_VALIDATION


@pytest.mark.anyio
async def test_analyze_v2_promotes_uncertain_model_combinations_to_review() -> None:
    output = (
        '{"items": ['
        '{"item_key": "boxes", "description": "박스 수량 미상", "name": "박스", '
        '"quantity": null, "unit": null, "confidence": 0.5, "source_indices": [0]},'
        '{"item_key": "chairs", "description": "의자 단위 미상", "name": "의자", '
        '"quantity": 2, "unit": null, "confidence": 0.6, "source_indices": [0]}], '
        '"location_conditions": [{"source_indices": [0], '
        '"floor": {"status": "known", "value": null}, '
        '"carry_distance": {"status": "unknown", "value_m": 10}, '
        '"confidence": 0.4, "review_required_fields": ["floor", "floor"]},'
        '{"source_indices": [1], '
        '"floor": {"status": "unknown", "value": 8}, '
        '"carry_distance": {"status": "known", "value_m": null}, '
        '"confidence": 0.5, '
        '"review_required_fields": ["carry_distance", "carry_distance"]}]} '
    )
    request = _request_v2()

    result = await _provider(StubModels(text=output)).analyze(
        request=request,
        idempotency_key=KEY,
        timeout_seconds=30,
    )

    assert result.draft_items == ()
    assert [item.item_key for item in result.review_required_items] == ["boxes", "chairs"]
    assert all(item.quantity is None and item.unit is None for item in result.review_required_items)
    origin, destination = result.location_condition_suggestions
    assert origin.floor.status == "unknown"
    assert origin.floor.value is None
    assert origin.carry_distance.status == "unknown"
    assert origin.carry_distance.value_m is None
    assert origin.review_required_fields == ("floor", "carry_distance")
    assert destination.floor.status == "unknown"
    assert destination.floor.value is None
    assert destination.carry_distance.status == "unknown"
    assert destination.carry_distance.value_m is None
    assert destination.review_required_fields == ("carry_distance", "floor")


@pytest.mark.anyio
async def test_analyze_v2_maps_any_source_indices_to_only_input() -> None:
    output = (
        '{"items": [{"item_key": "bed", "description": "침대", "name": "침대", '
        '"quantity": 1, "unit": "개", "confidence": 0.9, "source_indices": [99]}], '
        '"location_conditions": [{"source_indices": [99], "confidence": 0.5}]}'
    )
    request = _request_v2()
    request = request.model_copy(
        update={
            "source_media_asset_ids": request.source_media_asset_ids[:1],
            "object_keys": request.object_keys[:1],
            "content_types": request.content_types[:1],
            "source_contexts": request.source_contexts[:1],
        }
    )

    result = await _provider(StubModels(text=output)).analyze(
        request=request,
        idempotency_key=KEY,
        timeout_seconds=30,
    )

    assert result.draft_items[0].source_media_asset_ids == request.source_media_asset_ids
    assert result.location_condition_suggestions[0].source_media_asset_ids == (
        request.source_media_asset_ids
    )


@pytest.mark.anyio
async def test_analyze_v2_rejects_untrusted_or_incomplete_output() -> None:
    cases = [
        (
            '{"items": [{"item_key": "bed", "description": "침대", "name": "침대", '
            '"quantity": 1, "unit": "개", "confidence": 0.9, "source_indices": [9]}]}',
            "unknown media",
        ),
        (
            '{"items": [{"item_key": "bed", "description": "침대", "name": "침대", '
            '"quantity": 1, "unit": "개", "confidence": 0.9, "source_indices": [0]}], '
            '"location_conditions": [{"source_indices": [0, 1], "confidence": 0.5}]}',
            "mixed source locations",
        ),
        (
            '{"items": [{"item_key": "bed", "description": "침대", "name": "침대", '
            '"quantity": 1, "unit": "개", "confidence": 0.9, "source_indices": [0]}, '
            '{"item_key": "bed", "description": "중복 침대", "name": "침대", '
            '"quantity": 1, "unit": "개", "confidence": 0.8, "source_indices": [0]}]}',
            "duplicate item keys",
        ),
        (
            '{"items": [{"item_key": "bed", "description": "침대", "name": "침대", '
            '"quantity": 1, "unit": "개", "confidence": 0.9, "source_indices": [0]}], '
            '"location_conditions": [{"source_indices": [0], "confidence": 0.5}, '
            '{"source_indices": [0], "confidence": 0.4}]}',
            "duplicate location suggestions",
        ),
    ]

    for output, message in cases:
        provider = _provider(StubModels(text=output))

        with pytest.raises(ProviderError, match=message) as error_info:
            await provider.analyze(
                request=_request_v2(),
                idempotency_key=KEY,
                timeout_seconds=30,
            )

        assert error_info.value.kind is ProviderErrorKind.UNAVAILABLE
        assert error_info.value.retryable is True


@pytest.mark.anyio
async def test_analyze_rejects_empty_response() -> None:
    provider = _provider(StubModels(text=None))

    with pytest.raises(ProviderError) as error_info:
        await provider.analyze(request=_request(), idempotency_key=KEY, timeout_seconds=30)

    assert error_info.value.kind is ProviderErrorKind.UNAVAILABLE
    assert error_info.value.retryable is True
    assert error_info.value.failure_stage is AnalysisFailureStage.PARSE
    assert error_info.value.failure_detail is AnalysisFailureDetail.EMPTY_RESPONSE


@pytest.mark.anyio
async def test_analyze_rejects_malformed_output() -> None:
    cases = [
        "not json at all",
        '{"items": [{"item_key": "bed", "description": "d", "confidence": 2.0}]}',
        "{}",
        '{"items": []}',
        '{"items": [{"item_key": "bed", "description": "침대", "confidence": 0.9}]}',
    ]

    for text in cases:
        provider = _provider(StubModels(text=text))

        with pytest.raises(ProviderError, match="malformed") as error_info:
            await provider.analyze(request=_request(), idempotency_key=KEY, timeout_seconds=30)

        assert error_info.value.kind is ProviderErrorKind.UNAVAILABLE
        assert error_info.value.retryable is True
        assert error_info.value.failure_stage is AnalysisFailureStage.PARSE
        assert error_info.value.failure_detail is AnalysisFailureDetail.SCHEMA_VALIDATION


@pytest.mark.anyio
async def test_analyze_rejects_out_of_range_source_index() -> None:
    output = (
        '{"items": [{"item_key": "bed", "description": "침대", "confidence": 0.9,'
        ' "source_indices": [5]}]}'
    )
    provider = _provider(StubModels(text=output))

    with pytest.raises(ProviderError, match="unknown media") as error_info:
        await provider.analyze(
            request=_request(source_count=2),
            idempotency_key=KEY,
            timeout_seconds=30,
        )

    assert error_info.value.kind is ProviderErrorKind.UNAVAILABLE
    assert error_info.value.retryable is True
    assert error_info.value.failure_stage is AnalysisFailureStage.SOURCE_MAP
    assert error_info.value.failure_detail is AnalysisFailureDetail.INVALID_SOURCE_REFERENCE


@pytest.mark.anyio
async def test_analyze_maps_any_source_indices_to_only_input() -> None:
    for source_indices in ([], [1], [0, 0], [99]):
        output = (
            '{"items": [{"item_key": "bed", "description": "침대", "confidence": 0.9,'
            f' "source_indices": {source_indices}'
            "}]}"
        )
        provider = _provider(StubModels(text=output))
        request = _request(source_count=1)

        result = await provider.analyze(
            request=request,
            idempotency_key=KEY,
            timeout_seconds=30,
        )

        assert result.draft_items[0].source_media_asset_ids == request.source_media_asset_ids


@pytest.mark.anyio
async def test_analyze_rejects_ambiguous_or_duplicate_source_indices() -> None:
    for source_indices in ([], [0, 0]):
        output = (
            '{"items": [{"item_key": "bed", "description": "침대", "confidence": 0.9,'
            f' "source_indices": {source_indices}'
            "}]}"
        )
        provider = _provider(StubModels(text=output))

        with pytest.raises(ProviderError, match="invalid media references") as error_info:
            await provider.analyze(
                request=_request(source_count=2),
                idempotency_key=KEY,
                timeout_seconds=30,
            )

        assert error_info.value.kind is ProviderErrorKind.UNAVAILABLE
        assert error_info.value.retryable is True


@pytest.mark.anyio
async def test_analyze_rejects_duplicate_item_keys() -> None:
    item = '{"item_key": "bed", "description": "침대", "confidence": 0.9, "source_indices": [0]}'
    provider = _provider(StubModels(text=f'{{"items": [{item}, {item}]}}'))

    with pytest.raises(ProviderError, match="duplicate item keys") as error_info:
        await provider.analyze(
            request=_request(source_count=1),
            idempotency_key=KEY,
            timeout_seconds=30,
        )

    assert error_info.value.kind is ProviderErrorKind.UNAVAILABLE
    assert error_info.value.retryable is True


@pytest.mark.anyio
async def test_analyze_rejects_unknown_prompt_version() -> None:
    provider = _provider(StubModels(text="{}"))

    with pytest.raises(ProviderError, match="prompt version") as error_info:
        await provider.analyze(
            request=_request(prompt_version="missing"),
            idempotency_key=KEY,
            timeout_seconds=30,
        )

    assert error_info.value.kind is ProviderErrorKind.INVALID_INPUT


@pytest.mark.anyio
async def test_analyze_times_out() -> None:
    provider = _provider(StubModels(text="{}", sleep_seconds=0.2))

    with pytest.raises(ProviderError) as error_info:
        await provider.analyze(request=_request(), idempotency_key=KEY, timeout_seconds=0.01)

    assert error_info.value.kind is ProviderErrorKind.DEADLINE_EXCEEDED


@pytest.mark.anyio
async def test_analyze_maps_provider_errors() -> None:
    cases = [
        (
            FakeAPIError(400),
            ProviderErrorKind.INVALID_INPUT,
            False,
            AnalysisFailureDetail.PROVIDER_REJECTED,
        ),
        (
            FakeAPIError(403),
            ProviderErrorKind.PERMISSION_DENIED,
            False,
            AnalysisFailureDetail.PROVIDER_REJECTED,
        ),
        (
            FakeAPIError(404),
            ProviderErrorKind.NOT_FOUND,
            False,
            AnalysisFailureDetail.PROVIDER_REJECTED,
        ),
        (
            FakeAPIError(409),
            ProviderErrorKind.CONFLICT,
            False,
            AnalysisFailureDetail.PROVIDER_REJECTED,
        ),
        (
            FakeAPIError(504),
            ProviderErrorKind.DEADLINE_EXCEEDED,
            True,
            AnalysisFailureDetail.PROVIDER_TIMEOUT,
        ),
        (
            FakeAPIError(500),
            ProviderErrorKind.UNAVAILABLE,
            True,
            AnalysisFailureDetail.PROVIDER_UNAVAILABLE,
        ),
        (
            RuntimeError("offline"),
            ProviderErrorKind.UNAVAILABLE,
            True,
            AnalysisFailureDetail.PROVIDER_UNAVAILABLE,
        ),
    ]

    for error, kind, retryable, failure_detail in cases:
        provider = _provider(StubModels(error=error))

        with pytest.raises(ProviderError, match="provider call failed") as error_info:
            await provider.analyze(request=_request(), idempotency_key=KEY, timeout_seconds=30)

        assert error_info.value.kind is kind
        assert error_info.value.retryable is retryable
        assert error_info.value.failure_stage is AnalysisFailureStage.PROVIDER_CALL
        assert error_info.value.provider_status == (
            error.code if isinstance(error, errors.APIError) else None
        )
        assert error_info.value.failure_detail is failure_detail


@pytest.mark.anyio
async def test_analyze_rejects_nonpositive_timeout() -> None:
    provider = _provider(StubModels(text="{}"))

    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        await provider.analyze(request=_request(), idempotency_key=KEY, timeout_seconds=0)


def _logging_provider(models: StubModels) -> tuple[VertexAIProvider, list[logging.LogRecord]]:
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(f"test.vertex.{uuid4().hex}")
    logger.handlers = [_Capture()]
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    provider = VertexAIProvider(
        project="seqret-dev",
        location="us-central1",
        bucket_name="seqret-media",
        prompt_library=PROMPTS,
        client_factory=lambda: StubClient(models),
        logger=logger,
    )
    return provider, records


@pytest.mark.anyio
async def test_provider_call_failure_records_stage_and_status() -> None:
    provider, records = _logging_provider(StubModels(error=FakeAPIError(503)))

    with pytest.raises(ProviderError):
        await provider.analyze(request=_request(), idempotency_key=KEY, timeout_seconds=30)

    assert len(records) == 1
    fields = records[0].__dict__
    assert fields["event"] == "analysis_provider_failure"
    assert fields["analysis_stage"] == "provider_call"
    assert fields["provider_status"] == 503
    assert fields["error_kind"] == "unavailable"
    assert fields["retryable"] is True


@pytest.mark.anyio
async def test_malformed_and_source_index_failures_are_distinguished() -> None:
    malformed_provider, malformed_records = _logging_provider(StubModels(text="not json"))
    with pytest.raises(ProviderError, match="malformed"):
        await malformed_provider.analyze(
            request=_request(), idempotency_key=KEY, timeout_seconds=30
        )

    index_output = (
        '{"items": [{"item_key": "bed", "description": "침대", "confidence": 0.9,'
        ' "source_indices": [9]}]}'
    )
    index_provider, index_records = _logging_provider(StubModels(text=index_output))
    with pytest.raises(ProviderError, match="unknown media"):
        await index_provider.analyze(
            request=_request(source_count=2), idempotency_key=KEY, timeout_seconds=30
        )

    assert malformed_records[0].__dict__["analysis_stage"] == "parse"
    assert malformed_records[0].__dict__["retryable"] is True
    assert index_records[0].__dict__["analysis_stage"] == "source_map"
    assert index_records[0].__dict__["provider_status"] == 0
    assert index_records[0].__dict__["retryable"] is True

    empty_index_output = (
        '{"items": [{"item_key": "bed", "description": "침대", "confidence": 0.9,'
        ' "source_indices": []}]}'
    )
    empty_index_provider, empty_index_records = _logging_provider(
        StubModels(text=empty_index_output)
    )
    with pytest.raises(ProviderError, match="invalid media references"):
        await empty_index_provider.analyze(
            request=_request(source_count=2), idempotency_key=KEY, timeout_seconds=30
        )

    assert empty_index_records[0].__dict__["analysis_stage"] == "source_map"
    assert empty_index_records[0].__dict__["retryable"] is True
