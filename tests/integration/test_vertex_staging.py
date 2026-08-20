"""Opt-in Vertex contract canary against an explicitly selected staging video."""

import os
from uuid import uuid4

import google.auth
import pytest

from app.contracts.ai import AnalysisRequest, AnalysisSourceContext
from app.contracts.primitives import AnalysisRunId, CaptureSessionId, IdempotencyKey, MediaAssetId
from app.entrypoints.worker import (
    ANALYSIS_MODEL_NAME,
    ANALYSIS_PROMPT_LIBRARY,
    ANALYSIS_PROMPT_VERSION,
)
from app.platform.ai.vertex import VertexAIProvider

VERTEX_GCS_URI_ENV = "SEQRET_TEST_VERTEX_GCS_URI"
VERTEX_PROJECT_ENV = "SEQRET_TEST_GCP_PROJECT_ID"
VERTEX_LOCATION_ENV = "SEQRET_TEST_VERTEX_LOCATION"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _staging_video() -> tuple[str, str]:
    uri = os.getenv(VERTEX_GCS_URI_ENV)
    if uri is None:
        pytest.skip(f"{VERTEX_GCS_URI_ENV} is not configured")
    if not uri.startswith("gs://") or "/" not in uri.removeprefix("gs://"):
        pytest.fail(f"{VERTEX_GCS_URI_ENV} must be a gs:// bucket/object URI")
    bucket_name, object_key = uri.removeprefix("gs://").split("/", 1)
    if not bucket_name or not object_key:
        pytest.fail(f"{VERTEX_GCS_URI_ENV} must include a bucket and object key")
    return bucket_name, object_key


@pytest.mark.anyio
async def test_vertex_v2_accepts_staging_video_and_returns_reviewable_items() -> None:
    """Exercise the production prompt and strict result mapper with real Vertex output."""

    bucket_name, object_key = _staging_video()
    _, detected_project = google.auth.default()
    project = os.getenv(VERTEX_PROJECT_ENV) or detected_project
    if not project:
        pytest.skip(f"{VERTEX_PROJECT_ENV} and the ADC project are unavailable")
    location = os.getenv(VERTEX_LOCATION_ENV, "asia-northeast3")
    media_asset_id = MediaAssetId(uuid4())
    request = AnalysisRequest(
        analysis_run_id=AnalysisRunId(uuid4()),
        capture_session_id=CaptureSessionId(uuid4()),
        source_media_asset_ids=(media_asset_id,),
        object_keys=(object_key,),
        content_types=("video/mp4",),
        model_name=ANALYSIS_MODEL_NAME,
        model_version="staging-canary",
        prompt_version=ANALYSIS_PROMPT_VERSION,
        requested_result_schema_version=2,
        source_contexts=(
            AnalysisSourceContext(
                media_asset_id=media_asset_id,
                location_id=uuid4(),
                location_kind="origin",
                room_zone_id=uuid4(),
            ),
        ),
    )
    provider = VertexAIProvider(
        project=project,
        location=location,
        bucket_name=bucket_name,
        prompt_library=ANALYSIS_PROMPT_LIBRARY,
    )

    result = await provider.analyze(
        request=request,
        idempotency_key=IdempotencyKey("analysis:vertex-staging-canary"),
        timeout_seconds=180,
    )

    items = result.draft_items + result.review_required_items
    assert items
    assert all(item.source_media_asset_ids == (media_asset_id,) for item in items)
    assert all(item.quantity is not None and item.unit is not None for item in result.draft_items)
    assert all(
        (item.quantity is None) == (item.unit is None) for item in result.review_required_items
    )
