"""Media-validation handler bridging storage and A-owned state commands."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.maintenance import (
    MediaValidationOutcome,
    MediaValidationResultV1,
    MediaValidationTaskV1,
    MediaValidationWorkV1,
)
from app.contracts.ports import ProviderError, ProviderErrorKind, StoragePort
from app.modules.background_job.service import complete_media_validation, start_media_validation
from app.platform.db import transactional_session

DEFAULT_METADATA_TIMEOUT_SECONDS = 30.0
DEFAULT_HASH_TIMEOUT_SECONDS = 5 * 60.0


async def handle_media_validation(
    factory: async_sessionmaker[AsyncSession],
    storage: StoragePort,
    task: MediaValidationTaskV1,
    *,
    now: datetime,
    metadata_timeout_seconds: float = DEFAULT_METADATA_TIMEOUT_SECONDS,
    hash_timeout_seconds: float = DEFAULT_HASH_TIMEOUT_SECONDS,
) -> MediaValidationResultV1 | None:
    """Lease, validate, hash, and atomically record one immutable attempt."""

    if metadata_timeout_seconds <= 0 or hash_timeout_seconds <= 0:
        raise ValueError("storage timeouts must be positive")
    async with transactional_session(factory) as session:
        work = await start_media_validation(session, task, now=now)
    if work is None:
        return None

    try:
        metadata = await storage.get_metadata(
            object_key=work.object_key,
            timeout_seconds=metadata_timeout_seconds,
        )
        if (
            metadata.object_key != work.object_key
            or metadata.generation != work.source_generation
            or metadata.content_type != work.expected_content_type
            or metadata.size_bytes != work.expected_size_bytes
        ):
            result = _failed_result(work, ProviderErrorKind.INVALID_INPUT)
        else:
            sha256_hex = await storage.calculate_sha256(
                object_key=work.object_key,
                generation=work.source_generation,
                timeout_seconds=hash_timeout_seconds,
            )
            result = MediaValidationResultV1(
                background_job_id=work.background_job_id,
                attempt_count=work.attempt_count,
                source_generation=work.source_generation,
                outcome=MediaValidationOutcome.SUCCEEDED,
                observed_content_type=metadata.content_type,
                observed_size_bytes=metadata.size_bytes,
                sha256_hex=sha256_hex,
            )
    except ProviderError as error:
        result = _failed_result(work, error.kind)

    async with transactional_session(factory) as session:
        await complete_media_validation(session, result, completed_at=now)
    return result


def _failed_result(
    work: MediaValidationWorkV1,
    error_kind: ProviderErrorKind,
) -> MediaValidationResultV1:
    return MediaValidationResultV1(
        background_job_id=work.background_job_id,
        attempt_count=work.attempt_count,
        source_generation=work.source_generation,
        outcome=MediaValidationOutcome.FAILED,
        error_kind=error_kind,
    )
