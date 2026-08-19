"""Lease-based external notification delivery with bounded retries."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.events import DomainEventType
from app.contracts.notification import ExternalNotificationChannel, OutboundNotification
from app.contracts.ports import NotificationProviderPort, ProviderError
from app.contracts.primitives import IdempotencyKey, utc_now
from app.modules.notification.models import (
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
)
from app.platform.db import transactional_session

MAX_DELIVERY_ATTEMPTS = 5
MAX_RETRY_DELAY_SECONDS = 300
DELIVERY_CONCURRENCY = 10

EVENT_MESSAGE = {
    DomainEventType.CAPTURE_SUBMITTED_V1: "촬영 자료가 제출되었습니다.",
    DomainEventType.ANALYSIS_COMPLETED_V1: "촬영 분석이 완료되었습니다.",
    DomainEventType.ANALYSIS_FAILED_V1: "촬영 분석을 완료하지 못해 확인이 필요합니다.",
    DomainEventType.SCOPE_LOCKED_V1: "고객과 업체가 작업범위를 함께 확인했습니다.",
    DomainEventType.CHANGE_REQUESTED_V1: "현장 변경 요청이 등록되었습니다.",
    DomainEventType.DISPATCH_CONFIRMED_V1: "기사와 차량 배차가 확정되었습니다.",
    DomainEventType.COMPLETION_MEDIA_SUBMITTED_V1: "완료 확인 자료가 등록되었습니다.",
    DomainEventType.COMPLETION_SUBMITTED_V1: "현장 작업 완료 내용이 제출되었습니다.",
    DomainEventType.COMPLETION_REQUESTED_V1: "작업 완료 확인 요청이 도착했습니다.",
    DomainEventType.COMPLETION_DECIDED_V1: "작업 완료 확인 결과가 등록되었습니다.",
    DomainEventType.MEDIA_DELETED_V1: "보존기간이 지난 미디어가 삭제되었습니다.",
}


@dataclass(frozen=True, slots=True)
class ClaimedNotification:
    notification_id: UUID
    lock_token: UUID
    message: OutboundNotification


@dataclass(frozen=True, slots=True)
class ExternalDeliveryResult:
    claimed: int
    sent: int
    retry_scheduled: int
    failed: int


def _external_channel(channel: NotificationChannel) -> ExternalNotificationChannel:
    return {
        NotificationChannel.EMAIL: ExternalNotificationChannel.EMAIL,
        NotificationChannel.SMS: ExternalNotificationChannel.SMS,
        NotificationChannel.KAKAO: ExternalNotificationChannel.KAKAO,
    }[channel]


def _message(row: NotificationDelivery, frontend_origin: str) -> OutboundNotification:
    assert row.destination is not None
    body = EVENT_MESSAGE[row.event_type]
    return OutboundNotification(
        notification_id=row.id,
        event_id=row.event_id,
        job_id=row.job_id,
        channel=_external_channel(row.channel),
        destination=row.destination,
        subject="[SEQRET] 작업 상태 알림",
        body=body,
        deep_link=f"{frontend_origin}/?job={row.job_id}",
    )


async def claim_external_notifications(
    session: AsyncSession,
    *,
    frontend_origin: str,
    now: datetime | None = None,
    limit: int = 100,
    lease_seconds: int = 60,
) -> tuple[ClaimedNotification, ...]:
    """Lease due external deliveries while leaving in-app history untouched."""

    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    claimed_at = now or utc_now()
    rows = (
        await session.scalars(
            select(NotificationDelivery)
            .where(
                NotificationDelivery.channel != NotificationChannel.IN_APP,
                NotificationDelivery.status == NotificationStatus.PENDING,
                NotificationDelivery.next_attempt_at <= claimed_at,
                or_(
                    NotificationDelivery.locked_until.is_(None),
                    NotificationDelivery.locked_until <= claimed_at,
                ),
            )
            .order_by(NotificationDelivery.next_attempt_at, NotificationDelivery.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    claims: list[ClaimedNotification] = []
    for row in rows:
        lock_token = uuid4()
        row.lock_token = lock_token
        row.locked_until = claimed_at + timedelta(seconds=lease_seconds)
        row.attempt_count += 1
        row.last_attempt_at = claimed_at
        claims.append(
            ClaimedNotification(
                notification_id=row.id,
                lock_token=lock_token,
                message=_message(row, frontend_origin),
            )
        )
    await session.flush()
    return tuple(claims)


async def _mark_sent(
    session: AsyncSession,
    claim: ClaimedNotification,
    provider_message_id: str,
    *,
    now: datetime,
) -> bool:
    row = await session.scalar(
        select(NotificationDelivery)
        .where(
            NotificationDelivery.id == claim.notification_id,
            NotificationDelivery.lock_token == claim.lock_token,
            NotificationDelivery.status == NotificationStatus.PENDING,
        )
        .with_for_update()
    )
    if row is None:
        return False
    row.status = NotificationStatus.SENT
    row.sent_at = now
    row.last_error_code = None
    row.next_attempt_at = None
    row.lock_token = None
    row.locked_until = None
    row.provider_message_id = provider_message_id
    await session.flush()
    return True


async def _mark_failure(
    session: AsyncSession,
    claim: ClaimedNotification,
    error_code: str,
    *,
    retryable: bool,
    now: datetime,
) -> str:
    row = await session.scalar(
        select(NotificationDelivery)
        .where(
            NotificationDelivery.id == claim.notification_id,
            NotificationDelivery.lock_token == claim.lock_token,
            NotificationDelivery.status == NotificationStatus.PENDING,
        )
        .with_for_update()
    )
    if row is None:
        return "stale"
    row.lock_token = None
    row.locked_until = None
    if retryable and row.attempt_count < MAX_DELIVERY_ATTEMPTS:
        row.last_error_code = None
        delay = min(2 ** min(max(row.attempt_count - 1, 0), 8), MAX_RETRY_DELAY_SECONDS)
        row.next_attempt_at = now + timedelta(seconds=delay)
        outcome = "retry"
    else:
        row.status = NotificationStatus.FAILED
        row.last_error_code = error_code
        row.next_attempt_at = None
        outcome = "failed"
    await session.flush()
    return outcome


async def deliver_external_notifications_once(
    session_factory: async_sessionmaker[AsyncSession],
    provider: NotificationProviderPort,
    *,
    frontend_origin: str,
    batch_size: int = 100,
    lease_seconds: int = 60,
    timeout_seconds: float = 10.0,
    now: datetime | None = None,
) -> ExternalDeliveryResult:
    """Deliver one bounded batch, claiming each row immediately before its provider call."""

    started_at = now or utc_now()
    if not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if lease_seconds <= timeout_seconds + 1:
        raise ValueError("lease_seconds must exceed timeout_seconds by more than one second")

    claimed = sent = retry_scheduled = failed = 0
    claim_gate = asyncio.Lock()
    exhausted = False

    async def deliver_next() -> None:
        nonlocal claimed, exhausted, failed, retry_scheduled, sent
        while True:
            async with claim_gate:
                if exhausted or claimed >= batch_size:
                    return
                async with transactional_session(session_factory) as session:
                    claims = await claim_external_notifications(
                        session,
                        frontend_origin=frontend_origin,
                        now=started_at,
                        limit=1,
                        lease_seconds=lease_seconds,
                    )
                if not claims:
                    exhausted = True
                    return
                claim = claims[0]
                claimed += 1
            try:
                result = await provider.send(
                    message=claim.message,
                    idempotency_key=IdempotencyKey(f"notification:{claim.notification_id}"),
                    timeout_seconds=timeout_seconds,
                )
            except ProviderError as error:
                async with transactional_session(session_factory) as session:
                    outcome = await _mark_failure(
                        session,
                        claim,
                        error.kind.value,
                        retryable=error.retryable,
                        now=started_at,
                    )
                retry_scheduled += int(outcome == "retry")
                failed += int(outcome == "failed")
            except Exception:
                async with transactional_session(session_factory) as session:
                    outcome = await _mark_failure(
                        session,
                        claim,
                        "unavailable",
                        retryable=True,
                        now=started_at,
                    )
                retry_scheduled += int(outcome == "retry")
                failed += int(outcome == "failed")
            else:
                async with transactional_session(session_factory) as session:
                    finalized = await _mark_sent(
                        session,
                        claim,
                        result.provider_message_id,
                        now=started_at,
                    )
                sent += int(finalized)

    await asyncio.gather(*(deliver_next() for _ in range(min(batch_size, DELIVERY_CONCURRENCY))))
    return ExternalDeliveryResult(
        claimed=claimed,
        sent=sent,
        retry_scheduled=retry_scheduled,
        failed=failed,
    )
