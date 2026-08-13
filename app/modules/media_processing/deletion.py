"""Media-deletion handler bridging the storage adapter and A's commands.

This B-owned handler never touches A-owned tables directly. It leases the
attempt through ``start_media_deletion``, deletes the private object through the
provider-neutral :class:`StoragePort`, and reports the outcome back through
``complete_media_deletion``. The object key and generation stay inside the
provider call and this function, never reaching logs or return values.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.maintenance import (
    MediaDeletionOutcome,
    MediaDeletionResultV1,
    MediaDeletionTaskV1,
)
from app.contracts.ports import ProviderError, StoragePort
from app.contracts.primitives import IdempotencyKey
from app.modules.background_job.service import complete_media_deletion, start_media_deletion
from app.platform.db import transactional_session

DEFAULT_DELETE_TIMEOUT_SECONDS = 30.0


async def handle_media_deletion(
    factory: async_sessionmaker[AsyncSession],
    storage: StoragePort,
    task: MediaDeletionTaskV1,
    *,
    now: datetime,
    delete_timeout_seconds: float = DEFAULT_DELETE_TIMEOUT_SECONDS,
) -> MediaDeletionResultV1 | None:
    """Lease the attempt, delete the object, and record the outcome.

    Returns ``None`` when the job is already terminal, so a duplicate task has
    no additional effect. The deletion idempotency key is attempt-independent so
    two attempts converge on the same provider effect.
    """

    async with transactional_session(factory) as session:
        work = await start_media_deletion(session, task, now=now)
    if work is None:
        return None

    idempotency_key = IdempotencyKey(f"media-delete:{work.background_job_id}")
    try:
        await storage.delete_object(
            object_key=work.object_key,
            generation=work.generation,
            idempotency_key=idempotency_key,
            timeout_seconds=delete_timeout_seconds,
        )
    except ProviderError as error:
        result = MediaDeletionResultV1(
            background_job_id=work.background_job_id,
            attempt_count=work.attempt_count,
            outcome=MediaDeletionOutcome.FAILED,
            error_kind=error.kind,
        )
    else:
        result = MediaDeletionResultV1(
            background_job_id=work.background_job_id,
            attempt_count=work.attempt_count,
            outcome=MediaDeletionOutcome.SUCCEEDED,
        )

    async with transactional_session(factory) as session:
        await complete_media_deletion(session, result, completed_at=now)
    return result
