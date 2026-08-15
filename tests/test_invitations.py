"""A-02 self-service invitation lifecycle and secrecy tests."""

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from httpx2 import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import AppEnvironment, Settings
from app.contracts.actor import ActorContext, ActorKind, ParticipantRole
from app.contracts.primitives import JobId, ParticipantId, RequestId
from app.main import create_app
from app.modules.access.invitations import (
    ActorAccessLinkNotFoundError,
    InvitationConflictError,
    InvitationNotFoundError,
    accept_invitation,
    create_invitation,
    decline_invitation,
    get_actor_self,
    reissue_invitation,
)
from app.modules.access.models import (
    InvitationStatus,
    ParticipantAccessToken,
    ParticipantInvitation,
)
from app.modules.access.schemas import InvitationCreate
from app.modules.access.service import InvalidAccessTokenError
from app.modules.completion.models import AuditEvent, AuditEventType
from app.platform.db import Base, create_session_factory


@pytest.fixture
async def invitation_api(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession]]]:
    database_path = (tmp_path / "invitations.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    factory = create_session_factory(engine)
    application = create_app(Settings(environment=AppEnvironment.TEST))
    application.state.database_session_factory = factory
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        yield client, factory
    await engine.dispose()


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


async def _onboard_customer(client: AsyncClient, title: str = "초대 테스트") -> dict[str, Any]:
    response = await client.post(
        "/api/v1/move-jobs/onboarding",
        json={
            "title": title,
            "customer_display_name": "고객",
            "locations": [
                {
                    "kind": "origin",
                    "label": "출발지",
                    "room_zones": [{"name": "거실", "sort_order": 0}],
                }
            ],
        },
    )
    assert response.status_code == 201
    return cast(dict[str, Any], response.json())


async def _invite(
    client: AsyncClient,
    *,
    job_id: str,
    secret: str,
    role: str,
    display_name: str,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/move-jobs/{job_id}/invitations",
        json={"role": role, "display_name": display_name},
        headers=_bearer(secret),
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    return cast(dict[str, Any], response.json())


@pytest.mark.anyio
async def test_customer_invites_manager_then_manager_invites_worker(
    invitation_api: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, factory = invitation_api
    created = await _onboard_customer(client)
    job_id = created["job"]["id"]
    customer_secret = created["customer_access_link"]["secret"]

    customer_self = await client.get("/api/v1/me", headers=_bearer(customer_secret))
    assert customer_self.status_code == 200
    assert customer_self.headers["cache-control"] == "no-store"
    assert customer_self.json()["invitation"] is None
    assert "invitation:company_manager:manage" in customer_self.json()["permissions"]
    empty = await client.get(
        f"/api/v1/move-jobs/{job_id}/invitations",
        headers=_bearer(customer_secret),
    )
    assert empty.status_code == 200
    assert empty.json() == {"invitations": []}

    manager_issued = await _invite(
        client,
        job_id=job_id,
        secret=customer_secret,
        role="company_manager",
        display_name="업체 담당자",
    )
    manager_invitation = manager_issued["invitation"]
    manager_secret = manager_issued["access_link"]["secret"]
    assert manager_invitation["status"] == "pending"
    assert manager_issued["access_link"]["role"] == "company_manager"
    assert datetime.fromisoformat(manager_issued["access_link"]["expires_at"]) <= (
        datetime.fromisoformat(created["customer_access_link"]["expires_at"])
    )

    pending_job = await client.get(
        f"/api/v1/move-jobs/{job_id}",
        headers=_bearer(manager_secret),
    )
    assert pending_job.status_code == 401
    pending_self = await client.get("/api/v1/me", headers=_bearer(manager_secret))
    assert pending_self.status_code == 200
    assert pending_self.json()["permissions"] == [
        "invitation:read",
        "invitation:accept",
        "invitation:decline",
    ]
    assert pending_self.json()["invitation"]["id"] == manager_invitation["id"]
    pending_worker_invite = await client.post(
        f"/api/v1/move-jobs/{job_id}/invitations",
        json={"role": "field_worker", "display_name": "현장기사"},
        headers=_bearer(manager_secret),
    )
    assert pending_worker_invite.status_code == 401

    forbidden_chain = await client.post(
        f"/api/v1/move-jobs/{job_id}/invitations",
        json={"role": "field_worker", "display_name": "현장기사"},
        headers=_bearer(customer_secret),
    )
    assert forbidden_chain.status_code == 403
    assert forbidden_chain.json()["detail"] == "insufficient role"

    accept_url = f"/api/v1/move-jobs/{job_id}/invitations/{manager_invitation['id']}/accept"
    accepted = await client.post(accept_url, headers=_bearer(manager_secret))
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    repeated_accept = await client.post(accept_url, headers=_bearer(manager_secret))
    assert repeated_accept.status_code == 200
    assert repeated_accept.json()["resolved_at"] == accepted.json()["resolved_at"]
    assert (
        await client.get(
            f"/api/v1/move-jobs/{job_id}",
            headers=_bearer(manager_secret),
        )
    ).status_code == 200
    manager_self = await client.get("/api/v1/me", headers=_bearer(manager_secret))
    assert manager_self.status_code == 200
    assert "invitation:field_worker:manage" in manager_self.json()["permissions"]

    worker_issued = await _invite(
        client,
        job_id=job_id,
        secret=manager_secret,
        role="field_worker",
        display_name="현장기사",
    )
    worker_invitation = worker_issued["invitation"]
    worker_secret = worker_issued["access_link"]["secret"]
    assert datetime.fromisoformat(worker_issued["access_link"]["expires_at"]) <= (
        datetime.fromisoformat(manager_issued["access_link"]["expires_at"])
    )
    worker_accept = await client.post(
        f"/api/v1/move-jobs/{job_id}/invitations/{worker_invitation['id']}/accept",
        headers=_bearer(worker_secret),
    )
    assert worker_accept.status_code == 200
    assert worker_accept.json()["status"] == "accepted"
    worker_self = await client.get("/api/v1/me", headers=_bearer(worker_secret))
    assert worker_self.status_code == 200
    assert "field:check_in" in worker_self.json()["permissions"]

    manager_invitations = await client.get(
        f"/api/v1/move-jobs/{job_id}/invitations",
        headers=_bearer(manager_secret),
    )
    assert manager_invitations.status_code == 200
    assert [item["role"] for item in manager_invitations.json()["invitations"]] == [
        "company_manager",
        "field_worker",
    ]
    worker_list = await client.get(
        f"/api/v1/move-jobs/{job_id}/invitations",
        headers=_bearer(worker_secret),
    )
    assert worker_list.status_code == 403

    manager_reissued = await client.post(
        f"/api/v1/move-jobs/{job_id}/invitations/{manager_invitation['id']}/reissue",
        headers=_bearer(customer_secret),
    )
    assert manager_reissued.status_code == 200
    assert (await client.get("/api/v1/me", headers=_bearer(worker_secret))).status_code == 401
    async with factory() as session:
        worker_invitation_row = await session.get(
            ParticipantInvitation,
            UUID(worker_invitation["id"]),
        )
    assert worker_invitation_row is not None
    assert worker_invitation_row.status is InvitationStatus.REVOKED


@pytest.mark.anyio
async def test_invitation_revoke_reissue_decline_and_conflicts(
    invitation_api: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, _ = invitation_api
    created = await _onboard_customer(client)
    job_id = created["job"]["id"]
    customer_secret = created["customer_access_link"]["secret"]
    issued = await _invite(
        client,
        job_id=job_id,
        secret=customer_secret,
        role="company_manager",
        display_name="업체",
    )
    invitation_id = issued["invitation"]["id"]
    original_link_id = issued["access_link"]["id"]
    original_secret = issued["access_link"]["secret"]
    accept_url = f"/api/v1/move-jobs/{job_id}/invitations/{invitation_id}/accept"
    assert (await client.post(accept_url, headers=_bearer(original_secret))).status_code == 200

    duplicate = await client.post(
        f"/api/v1/move-jobs/{job_id}/invitations",
        json={"role": "company_manager", "display_name": "다른 업체"},
        headers=_bearer(customer_secret),
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "role already provisioned"

    own_reissue = await client.post(
        f"/api/v1/move-jobs/{job_id}/invitations/{invitation_id}/reissue",
        headers=_bearer(original_secret),
    )
    assert own_reissue.status_code == 404

    reissue_url = f"/api/v1/move-jobs/{job_id}/invitations/{invitation_id}/reissue"
    reissued = await client.post(reissue_url, headers=_bearer(customer_secret))
    assert reissued.status_code == 200
    assert reissued.headers["cache-control"] == "no-store"
    assert reissued.json()["invitation"]["status"] == "pending"
    assert reissued.json()["access_link"]["id"] != original_link_id
    reissued_secret = reissued.json()["access_link"]["secret"]
    assert (await client.get("/api/v1/me", headers=_bearer(original_secret))).status_code == 401

    revoke_url = f"/api/v1/move-jobs/{job_id}/invitations/{invitation_id}/revoke"
    revoked = await client.post(revoke_url, headers=_bearer(customer_secret))
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    repeated_revoke = await client.post(revoke_url, headers=_bearer(customer_secret))
    assert repeated_revoke.status_code == 200
    assert repeated_revoke.json()["resolved_at"] == revoked.json()["resolved_at"]
    assert (await client.get("/api/v1/me", headers=_bearer(reissued_secret))).status_code == 401

    declined_issue = await client.post(reissue_url, headers=_bearer(customer_secret))
    assert declined_issue.status_code == 200
    declined_secret = declined_issue.json()["access_link"]["secret"]
    decline_url = f"/api/v1/move-jobs/{job_id}/invitations/{invitation_id}/decline"
    declined = await client.post(decline_url, headers=_bearer(declined_secret))
    assert declined.status_code == 200
    assert declined.json()["status"] == "declined"
    assert (await client.get("/api/v1/me", headers=_bearer(declined_secret))).status_code == 401

    accepted_issue = await client.post(reissue_url, headers=_bearer(customer_secret))
    accepted_secret = accepted_issue.json()["access_link"]["secret"]
    assert (await client.post(accept_url, headers=_bearer(accepted_secret))).status_code == 200
    decline_after_accept = await client.post(
        decline_url,
        headers=_bearer(accepted_secret),
    )
    assert decline_after_accept.status_code == 409
    assert decline_after_accept.json()["detail"] == "invitation is not pending"

    other = await _onboard_customer(client, "다른 작업")
    cross_job = await client.post(
        f"/api/v1/move-jobs/{other['job']['id']}/invitations/{invitation_id}/accept",
        headers=_bearer(accepted_secret),
    )
    assert cross_job.status_code == 404
    missing = await client.post(
        f"/api/v1/move-jobs/{job_id}/invitations/{uuid4()}/accept",
        headers=_bearer(accepted_secret),
    )
    assert missing.status_code == 404


@pytest.mark.anyio
async def test_invitation_expiry_secrecy_and_connection_audit(
    invitation_api: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, factory = invitation_api
    created = await _onboard_customer(client)
    job_id = created["job"]["id"]
    customer_secret = created["customer_access_link"]["secret"]
    issued = await _invite(
        client,
        job_id=job_id,
        secret=customer_secret,
        role="company_manager",
        display_name="업체",
    )
    invitation_id = UUID(issued["invitation"]["id"])
    link_id = UUID(issued["access_link"]["id"])
    manager_secret = issued["access_link"]["secret"]

    assert (await client.get("/api/v1/me", headers=_bearer(manager_secret))).status_code == 200
    async with factory() as session:
        link = await session.get(ParticipantAccessToken, link_id)
        invitation = await session.get(ParticipantInvitation, invitation_id)
        connected = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.job_id == UUID(job_id),
                    AuditEvent.event_type == AuditEventType.PARTICIPANT_CONNECTED,
                    AuditEvent.actor_participant_id
                    == UUID(issued["access_link"]["participant_id"]),
                )
            )
        ).all()
    assert link is not None
    assert invitation is not None
    assert hashlib.sha256(manager_secret.encode()).hexdigest() == link.token_hash
    assert manager_secret not in repr(invitation)
    assert connected == []

    accept_url = f"/api/v1/move-jobs/{job_id}/invitations/{invitation_id}/accept"
    assert (await client.post(accept_url, headers=_bearer(manager_secret))).status_code == 200
    async with factory() as session:
        connected = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.job_id == UUID(job_id),
                    AuditEvent.event_type == AuditEventType.PARTICIPANT_CONNECTED,
                    AuditEvent.actor_participant_id
                    == UUID(issued["access_link"]["participant_id"]),
                )
            )
        ).all()
        issued_audit = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.job_id == UUID(job_id),
                    AuditEvent.event_type == AuditEventType.ACCESS_LINK_ISSUED,
                    AuditEvent.actor_participant_id
                    == UUID(created["customer_access_link"]["participant_id"]),
                )
            )
        ).all()
    assert len(connected) == 1
    assert connected[0].payload["operation"] == "invitation_accepted"
    assert len(issued_audit) == 1
    assert issued_audit[0].payload["operation"] == "invitation_issued"
    assert manager_secret not in repr(issued_audit[0].payload)

    reissue_url = f"/api/v1/move-jobs/{job_id}/invitations/{invitation_id}/reissue"
    reissued = await client.post(reissue_url, headers=_bearer(customer_secret))
    expiring_link_id = UUID(reissued.json()["access_link"]["id"])
    expiring_secret = reissued.json()["access_link"]["secret"]
    now = datetime.now(UTC)
    async with factory.begin() as session:
        await session.execute(
            update(ParticipantAccessToken)
            .where(ParticipantAccessToken.id == expiring_link_id)
            .values(
                created_at=now - timedelta(days=2),
                expires_at=now - timedelta(days=1),
            )
        )
        await session.execute(
            update(ParticipantInvitation)
            .where(ParticipantInvitation.id == invitation_id)
            .values(
                issued_at=now - timedelta(days=2),
                expires_at=now - timedelta(days=1),
            )
        )
    listed = await client.get(
        f"/api/v1/move-jobs/{job_id}/invitations",
        headers=_bearer(customer_secret),
    )
    assert listed.status_code == 200
    assert listed.json()["invitations"][0]["status"] == "expired"
    assert (await client.get("/api/v1/me", headers=_bearer(expiring_secret))).status_code == 401
    renewed = await client.post(reissue_url, headers=_bearer(customer_secret))
    assert renewed.status_code == 200
    assert renewed.json()["invitation"]["status"] == "pending"


@pytest.mark.anyio
async def test_access_link_revocation_cascades_delegated_capabilities(
    invitation_api: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, factory = invitation_api

    async def accepted_chain(title: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        customer = await _onboard_customer(client, title)
        job_id = customer["job"]["id"]
        manager = await _invite(
            client,
            job_id=job_id,
            secret=customer["customer_access_link"]["secret"],
            role="company_manager",
            display_name="업체",
        )
        assert (
            await client.post(
                f"/api/v1/move-jobs/{job_id}/invitations/{manager['invitation']['id']}/accept",
                headers=_bearer(manager["access_link"]["secret"]),
            )
        ).status_code == 200
        worker = await _invite(
            client,
            job_id=job_id,
            secret=manager["access_link"]["secret"],
            role="field_worker",
            display_name="현장기사",
        )
        assert (
            await client.post(
                f"/api/v1/move-jobs/{job_id}/invitations/{worker['invitation']['id']}/accept",
                headers=_bearer(worker["access_link"]["secret"]),
            )
        ).status_code == 200
        return customer, manager, worker

    customer, manager, worker = await accepted_chain("업체 직접 철회")
    job_id = customer["job"]["id"]
    manager_secret = manager["access_link"]["secret"]
    worker_secret = worker["access_link"]["secret"]
    revoked_manager = await client.post(
        f"/api/v1/move-jobs/{job_id}/access-links/{manager['access_link']['id']}/revoke",
        headers=_bearer(manager_secret),
    )
    assert revoked_manager.status_code == 204
    assert (await client.get("/api/v1/me", headers=_bearer(manager_secret))).status_code == 401
    assert (await client.get("/api/v1/me", headers=_bearer(worker_secret))).status_code == 401
    assert (
        await client.get(
            "/api/v1/me",
            headers=_bearer(customer["customer_access_link"]["secret"]),
        )
    ).status_code == 200

    customer, manager, worker = await accepted_chain("소비자 직접 철회")
    job_id = customer["job"]["id"]
    customer_secret = customer["customer_access_link"]["secret"]
    revoked_customer = await client.post(
        f"/api/v1/move-jobs/{job_id}/access-links/{customer['customer_access_link']['id']}/revoke",
        headers=_bearer(customer_secret),
    )
    assert revoked_customer.status_code == 204
    for secret in (
        customer_secret,
        manager["access_link"]["secret"],
        worker["access_link"]["secret"],
    ):
        assert (await client.get("/api/v1/me", headers=_bearer(secret))).status_code == 401

    async with factory() as session:
        statuses = tuple(
            (
                await session.scalars(
                    select(ParticipantInvitation.status)
                    .where(ParticipantInvitation.job_id == UUID(job_id))
                    .order_by(ParticipantInvitation.role)
                )
            ).all()
        )
    assert statuses == (InvitationStatus.REVOKED, InvitationStatus.REVOKED)


def test_invitation_command_rejects_customer_and_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="customer cannot be invited"):
        InvitationCreate.model_validate({"role": "customer", "display_name": "고객"})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        InvitationCreate.model_validate(
            {
                "role": "company_manager",
                "display_name": "업체",
                "secret": "must-not-be-accepted",
            }
        )


@pytest.mark.anyio
async def test_invitation_services_and_routes_map_disappeared_state(
    invitation_api: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory = invitation_api
    created = await _onboard_customer(client)
    job_id = UUID(created["job"]["id"])
    customer_id = UUID(created["customer_access_link"]["participant_id"])
    customer_secret = created["customer_access_link"]["secret"]
    customer_actor = ActorContext(
        actor_kind=ActorKind.PARTICIPANT,
        participant_id=ParticipantId(customer_id),
        participant_role=ParticipantRole.CUSTOMER,
        job_id=JobId(job_id),
        request_id=RequestId(uuid4()),
        trace_id="a" * 32,
    )
    command = InvitationCreate(
        role=ParticipantRole.COMPANY_MANAGER,
        display_name="업체",
    )
    async with factory.begin() as session:
        with pytest.raises(InvitationNotFoundError):
            await create_invitation(session, uuid4(), customer_actor, command)

    issued = await _invite(
        client,
        job_id=str(job_id),
        secret=customer_secret,
        role="company_manager",
        display_name="업체",
    )
    invitation_id = UUID(issued["invitation"]["id"])
    manager_id = UUID(issued["access_link"]["participant_id"])
    manager_secret = issued["access_link"]["secret"]
    manager_actor = ActorContext(
        actor_kind=ActorKind.PARTICIPANT,
        participant_id=ParticipantId(manager_id),
        participant_role=ParticipantRole.COMPANY_MANAGER,
        job_id=JobId(job_id),
        request_id=RequestId(uuid4()),
        trace_id="b" * 32,
    )
    async with factory.begin() as session:
        invitation = await session.get(ParticipantInvitation, invitation_id)
        assert invitation is not None
        invitation.status = InvitationStatus.EXPIRED
        with pytest.raises(InvitationConflictError):
            await accept_invitation(
                session,
                job_id,
                invitation_id,
                manager_actor,
                secret=manager_secret,
            )

    async with factory.begin() as session:
        with pytest.raises(InvalidAccessTokenError):
            await accept_invitation(
                session,
                job_id,
                invitation_id,
                manager_actor,
                secret="x" * 43,
            )
        invitation = await session.get(ParticipantInvitation, invitation_id)
        assert invitation is not None
        invitation.status = InvitationStatus.PENDING
        with pytest.raises(InvalidAccessTokenError):
            await decline_invitation(
                session,
                job_id,
                invitation_id,
                manager_actor,
                secret="y" * 43,
            )

    missing_actor = customer_actor.model_copy(update={"participant_id": ParticipantId(uuid4())})
    async with factory() as session:
        with pytest.raises(ActorAccessLinkNotFoundError):
            await get_actor_self(session, missing_actor)

    async def disappear_self(*_args: object) -> None:
        raise ActorAccessLinkNotFoundError(customer_id)

    async def disappear_job(*_args: object) -> None:
        raise InvitationNotFoundError(job_id)

    async def conflict_reissue(*_args: object) -> None:
        raise InvitationConflictError("issuer access link is unavailable")

    async def stale_invitation_secret(*_args: object, **_kwargs: object) -> None:
        raise InvalidAccessTokenError

    with monkeypatch.context() as context:
        context.setattr("app.modules.access.router.get_actor_self", disappear_self)
        disappeared_self = await client.get(
            "/api/v1/me",
            headers=_bearer(customer_secret),
        )
    assert disappeared_self.status_code == 401
    assert disappeared_self.headers["www-authenticate"] == "Bearer"

    with monkeypatch.context() as context:
        context.setattr("app.modules.access.router.create_invitation", disappear_job)
        disappeared_job = await client.post(
            f"/api/v1/move-jobs/{job_id}/invitations",
            json={"role": "company_manager", "display_name": "업체"},
            headers=_bearer(customer_secret),
        )
    assert disappeared_job.status_code == 404

    with monkeypatch.context() as context:
        context.setattr("app.modules.access.router.reissue_invitation", conflict_reissue)
        conflicted_reissue = await client.post(
            f"/api/v1/move-jobs/{job_id}/invitations/{invitation_id}/reissue",
            headers=_bearer(customer_secret),
        )
    assert conflicted_reissue.status_code == 409
    assert conflicted_reissue.json()["detail"] == "issuer access link is unavailable"

    with monkeypatch.context() as context:
        context.setattr("app.modules.access.router.accept_invitation", stale_invitation_secret)
        stale_accept = await client.post(
            f"/api/v1/move-jobs/{job_id}/invitations/{invitation_id}/accept",
            headers=_bearer(manager_secret),
        )
    assert stale_accept.status_code == 401
    assert stale_accept.headers["www-authenticate"] == "Bearer"

    async with factory.begin() as session:
        await session.execute(
            update(ParticipantAccessToken)
            .where(ParticipantAccessToken.participant_id == customer_id)
            .values(revoked_at=datetime.now(UTC))
        )
        with pytest.raises(InvitationConflictError, match="issuer access link"):
            await reissue_invitation(
                session,
                job_id,
                invitation_id,
                customer_actor,
            )

    unavailable_created = await _onboard_customer(client, "발급자 링크 소실")
    unavailable_job_id = UUID(unavailable_created["job"]["id"])
    unavailable_customer_id = UUID(unavailable_created["customer_access_link"]["participant_id"])
    unavailable_actor = customer_actor.model_copy(
        update={
            "participant_id": ParticipantId(unavailable_customer_id),
            "job_id": JobId(unavailable_job_id),
        }
    )
    async with factory.begin() as session:
        await session.execute(
            update(ParticipantAccessToken)
            .where(ParticipantAccessToken.participant_id == unavailable_customer_id)
            .values(revoked_at=datetime.now(UTC))
        )
        with pytest.raises(InvitationConflictError, match="issuer access link"):
            await create_invitation(
                session,
                unavailable_job_id,
                unavailable_actor,
                command,
            )
