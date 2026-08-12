"""Authenticated participant notification history API."""

from typing import cast
from uuid import UUID

from fastapi import APIRouter

from app.modules.access.auth import CurrentActor, authorize_job_actor
from app.modules.notification.schemas import NotificationResponse
from app.modules.notification.service import list_notifications
from app.platform.db.dependencies import Session

router = APIRouter(prefix="/move-jobs", tags=["notification"])


@router.get(
    "/{job_id}/notifications",
    response_model=tuple[NotificationResponse, ...],
    summary="내 작업 알림 이력 조회",
)
async def list_notifications_endpoint(
    job_id: UUID,
    actor: CurrentActor,
    session: Session,
) -> tuple[NotificationResponse, ...]:
    authorize_job_actor(actor, job_id)
    return await list_notifications(session, job_id, cast(UUID, actor.participant_id))
