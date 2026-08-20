"""Durable workspace, multi-job list, contact consent, and cookie auth tests."""

from base64 import urlsafe_b64encode
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import AppEnvironment, Settings
from app.contracts.actor import ParticipantRole
from app.main import create_app
from app.modules.access.auth import get_bearer_secret
from app.modules.access.models import InvitationStatus, NotificationContactChannel
from app.modules.access.router import _workspace_cookie_security
from app.modules.access.workspace import (
    InvalidWorkspaceSessionError,
    WorkspaceConflictError,
    WorkspacePrincipal,
    _invitation_response,
    _load_session,
    _normalized_destination,
    authenticate_workspace_actor,
    create_or_extend_workspace_session,
    delete_contact_point,
)
from app.modules.scope.service import ScopeVersionConflictError
from app.platform.db import Base, create_session_factory


@pytest.fixture
async def workspace_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    database_path = (tmp_path / "workspace.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    application = create_app(Settings(environment=AppEnvironment.TEST))
    application.state.database_session_factory = create_session_factory(engine)
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    await engine.dispose()


def _job_payload(title: str, scheduled_at: str) -> dict[str, object]:
    return {
        "title": title,
        "scheduled_at": scheduled_at,
        "participants": [
            {"role": "customer", "display_name": f"{title} 고객"},
            {"role": "company_manager", "display_name": "한결이사"},
            {"role": "field_worker", "display_name": "홍기사"},
        ],
        "locations": [
            {
                "kind": "origin",
                "label": f"{title} 출발지",
                "detail_address": f"{title} 상세주소",
                "room_zones": [{"name": "거실", "sort_order": 0}],
            },
            {
                "kind": "destination",
                "label": f"{title} 도착지",
                "room_zones": [{"name": "거실", "sort_order": 0}],
            },
        ],
    }


def _secret(created: dict[str, Any], role: str) -> str:
    return cast(
        str,
        next(link["secret"] for link in created["access_links"] if link["role"] == role),
    )


@pytest.mark.anyio
async def test_one_move_connection_code_opens_each_selected_role(
    workspace_client: AsyncClient,
) -> None:
    created_response = await workspace_client.post(
        "/api/v1/move-jobs/onboarding",
        json={
            "title": "공용 코드 이사",
            "scheduled_at": "2026-08-20T09:00:00+09:00",
            "customer_display_name": "김고객",
            "locations": [
                {
                    "kind": "origin",
                    "label": "출발지",
                    "room_zones": [{"name": "거실", "sort_order": 0}],
                }
            ],
        },
    )
    assert created_response.status_code == 201
    created = created_response.json()
    code = created["connection_code"]

    invited = await workspace_client.post(
        f"/api/v1/move-jobs/{created['job']['id']}/invitations",
        json={"role": "company_manager", "display_name": "공용 코드 업체"},
        headers={"Authorization": f"Bearer {created['customer_access_link']['secret']}"},
    )
    assert invited.status_code == 201

    preview = await workspace_client.post(
        "/api/v1/connections/preview",
        json={"connection_code": code.lower(), "role": "customer"},
    )
    assert preview.status_code == 200
    assert preview.headers["cache-control"] == "no-store"
    assert preview.json() == {"role": "customer", "display_name": "김고객"}
    assert "set-cookie" not in preview.headers
    unassigned_preview = await workspace_client.post(
        "/api/v1/connections/preview",
        json={"connection_code": code, "role": "field_worker"},
    )
    assert unassigned_preview.status_code == 200
    assert unassigned_preview.json() == {
        "role": "field_worker",
        "display_name": "현장기사",
    }

    connected_customer = await workspace_client.post(
        "/api/v1/connections",
        json={"connection_code": code.lower(), "role": "customer"},
    )
    assert connected_customer.status_code == 201
    reconnected_customer = await workspace_client.post(
        "/api/v1/connections",
        json={"connection_code": code, "role": "customer"},
    )
    assert reconnected_customer.status_code == 201
    assert "set-cookie" not in reconnected_customer.headers
    role_conflict = await workspace_client.post(
        "/api/v1/connections",
        json={"connection_code": code, "role": "company_manager"},
    )
    assert role_conflict.status_code == 409

    for role in ("company_manager", "field_worker"):
        workspace_client.cookies.clear()
        connected = await workspace_client.post(
            "/api/v1/connections",
            json={"connection_code": code.lower(), "role": role},
        )
        assert connected.status_code == 201
        assert connected.headers["cache-control"] == "no-store"
        assert connected.json()["role"] == role
        assert len(connected.json()["members"]) == 1
        member = connected.json()["members"][0]
        assert member["job_id"] == created["job"]["id"]
        assert member["role"] == role
        if role == "company_manager":
            assert member["invitation"]["status"] == "accepted"
        else:
            assert member["invitation"] is None

    workspace_client.cookies.clear()
    rejected = await workspace_client.post(
        "/api/v1/connections",
        json={"connection_code": "MOVE-00000000", "role": "customer"},
    )
    assert rejected.status_code == 401
    rejected_preview = await workspace_client.post(
        "/api/v1/connections/preview",
        json={"connection_code": "MOVE-00000000", "role": "customer"},
    )
    assert rejected_preview.status_code == 401
    malformed_prefix = await workspace_client.post(
        "/api/v1/connections",
        json={"connection_code": "NOPE-00000000", "role": "customer"},
    )
    assert malformed_prefix.status_code == 422
    malformed_length = await workspace_client.post(
        "/api/v1/connections",
        json={"connection_code": " MOVE-123456", "role": "customer"},
    )
    assert malformed_length.status_code == 422
    malformed_hex = await workspace_client.post(
        "/api/v1/connections",
        json={"connection_code": "MOVE-ZZZZZZZZ", "role": "customer"},
    )
    assert malformed_hex.status_code == 422

    canceled = await workspace_client.delete(
        f"/api/v1/move-jobs/{created['job']['id']}",
        headers={"Authorization": f"Bearer {created['customer_access_link']['secret']}"},
    )
    assert canceled.status_code == 204
    workspace_client.cookies.clear()
    rejected_canceled = await workspace_client.post(
        "/api/v1/connections",
        json={"connection_code": code, "role": "customer"},
    )
    assert rejected_canceled.status_code == 401


@pytest.mark.anyio
async def test_workspace_restores_and_searches_multiple_jobs(
    workspace_client: AsyncClient,
) -> None:
    first_response = await workspace_client.post(
        "/api/v1/move-jobs",
        json=_job_payload("첫째 이사", "2026-08-20T09:00:00+09:00"),
    )
    second_response = await workspace_client.post(
        "/api/v1/move-jobs",
        json=_job_payload("둘째 이사", "2026-08-25T09:00:00+09:00"),
    )
    assert first_response.status_code == second_response.status_code == 201
    first = first_response.json()
    second = second_response.json()

    first_session = await workspace_client.post(
        "/api/v1/sessions",
        headers={"Authorization": f"Bearer {_secret(first, 'company_manager')}"},
    )
    assert first_session.status_code == 201
    assert "HttpOnly" in first_session.headers["set-cookie"]
    assert "Path=/api/v1" in first_session.headers["set-cookie"]
    assert "SameSite=lax" in first_session.headers["set-cookie"]
    assert "seqret_workspace_session" not in first_session.json()

    restored = await workspace_client.get("/api/v1/session")
    assert restored.status_code == 200
    assert [member["job_id"] for member in restored.json()["members"]] == [first["job"]["id"]]

    attached = await workspace_client.post(
        "/api/v1/sessions",
        headers={"Authorization": f"Bearer {_secret(second, 'company_manager')}"},
    )
    assert attached.status_code == 201
    assert "set-cookie" not in attached.headers
    assert {member["job_id"] for member in attached.json()["members"]} == {
        first["job"]["id"],
        second["job"]["id"],
    }
    same_membership = await workspace_client.post(
        "/api/v1/sessions",
        headers={"Authorization": f"Bearer {_secret(first, 'company_manager')}"},
    )
    assert same_membership.status_code == 201
    assert "set-cookie" not in same_membership.headers
    contact = await workspace_client.put(
        "/api/v1/session/contact-points/email",
        json={"destination": "workspace@example.com", "delivery_consent": True},
        headers={"X-SEQRET-CSRF": first_session.json()["csrf_token"]},
    )
    assert contact.status_code == 200
    role_conflict = await workspace_client.post(
        "/api/v1/sessions",
        headers={"Authorization": f"Bearer {_secret(first, 'customer')}"},
    )
    assert role_conflict.status_code == 409

    listed = await workspace_client.get("/api/v1/move-jobs")
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert {move["job"]["id"] for move in listed.json()["moves"]} == {
        first["job"]["id"],
        second["job"]["id"],
    }
    assert all(move["version_label"] == "초안" for move in listed.json()["moves"])
    assert all(
        move["company_participation_status"] == "company_joined" for move in listed.json()["moves"]
    )
    assert all(
        next(location for location in move["job"]["locations"] if location["kind"] == "origin")[
            "detail_address"
        ]
        is not None
        for move in listed.json()["moves"]
    )
    assert listed.json()["next_cursor"] is None

    first_page = await workspace_client.get("/api/v1/move-jobs", params={"limit": 1})
    assert first_page.status_code == 200
    assert len(first_page.json()["moves"]) == 1
    assert first_page.json()["next_cursor"]
    second_page = await workspace_client.get(
        "/api/v1/move-jobs",
        params={"limit": 1, "cursor": first_page.json()["next_cursor"]},
    )
    assert second_page.status_code == 200
    assert len(second_page.json()["moves"]) == 1
    assert second_page.json()["next_cursor"] is None
    assert {
        first_page.json()["moves"][0]["job"]["id"],
        second_page.json()["moves"][0]["job"]["id"],
    } == {first["job"]["id"], second["job"]["id"]}

    searched = await workspace_client.get("/api/v1/move-jobs", params={"q": "둘째"})
    assert searched.status_code == 200
    assert [move["job"]["id"] for move in searched.json()["moves"]] == [second["job"]["id"]]
    detail_searched = await workspace_client.get(
        "/api/v1/move-jobs",
        params={"q": "둘째 이사 상세주소"},
    )
    assert detail_searched.status_code == 200
    assert [move["job"]["id"] for move in detail_searched.json()["moves"]] == [second["job"]["id"]]
    no_matches = await workspace_client.get("/api/v1/move-jobs", params={"q": "없는 작업"})
    assert no_matches.status_code == 200
    assert no_matches.json() == {"moves": [], "next_cursor": None}
    invalid_cursor = await workspace_client.get(
        "/api/v1/move-jobs",
        params={"cursor": "not-a-valid-cursor"},
    )
    assert invalid_cursor.status_code == 422
    wrong_version_cursor = urlsafe_b64encode(b"v2:0").decode().rstrip("=")
    wrong_version = await workspace_client.get(
        "/api/v1/move-jobs",
        params={"cursor": wrong_version_cursor},
    )
    assert wrong_version.status_code == 422
    oversized_offset_cursor = urlsafe_b64encode(f"v1:{2**63}".encode()).decode().rstrip("=")
    oversized_cursor = await workspace_client.get(
        "/api/v1/move-jobs",
        params={"cursor": oversized_offset_cursor},
    )
    assert oversized_cursor.status_code == 422
    scheduled = await workspace_client.get(
        "/api/v1/move-jobs",
        params={
            "status": "draft",
            "scheduled_from": "2026-08-23T00:00:00+09:00",
            "scheduled_to": "2026-08-30T00:00:00+09:00",
        },
    )
    assert scheduled.status_code == 200
    assert [move["job"]["id"] for move in scheduled.json()["moves"]] == [second["job"]["id"]]
    assert (await workspace_client.get("/api/v1/move-jobs", params={"q": "   "})).status_code == 422
    assert (
        await workspace_client.get(
            "/api/v1/move-jobs",
            params={
                "scheduled_from": "2026-08-30T00:00:00+09:00",
                "scheduled_to": "2026-08-20T00:00:00+09:00",
            },
        )
    ).status_code == 422

    bearer_only = await workspace_client.get(
        "/api/v1/move-jobs",
        headers={"Authorization": f"Bearer {_secret(first, 'company_manager')}"},
    )
    assert bearer_only.status_code == 200
    assert [move["job"]["id"] for move in bearer_only.json()["moves"]] == [first["job"]["id"]]

    workspace_client.cookies.clear()
    recovered = await workspace_client.post(
        "/api/v1/sessions",
        headers={
            "Authorization": f"Bearer {_secret(first, 'company_manager')}",
            "Cookie": "seqret_workspace_session=not-a-valid-cookie",
        },
    )
    assert recovered.status_code == 201
    assert "set-cookie" in recovered.headers
    assert [member["job_id"] for member in recovered.json()["members"]] == [first["job"]["id"]]
    recovered_list = await workspace_client.get("/api/v1/move-jobs")
    assert [move["job"]["id"] for move in recovered_list.json()["moves"]] == [first["job"]["id"]]
    assert (await workspace_client.get("/api/v1/session/contact-points")).json() == {"contacts": []}

    third_response = await workspace_client.post(
        "/api/v1/move-jobs",
        json=_job_payload("셋째 이사", "2026-09-01T09:00:00+09:00"),
    )
    assert third_response.status_code == 201
    third = third_response.json()
    workspace_client.cookies.clear()
    third_session = await workspace_client.post(
        "/api/v1/sessions",
        headers={"Authorization": f"Bearer {_secret(third, 'company_manager')}"},
    )
    assert third_session.status_code == 201
    other_account_conflict = await workspace_client.post(
        "/api/v1/sessions",
        headers={"Authorization": f"Bearer {_secret(first, 'company_manager')}"},
    )
    assert other_account_conflict.status_code == 409


@pytest.mark.anyio
async def test_cookie_csrf_updates_job_and_manages_masked_contacts(
    workspace_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_response = await workspace_client.post(
        "/api/v1/move-jobs",
        json=_job_payload("수정 이사", "2026-08-20T09:00:00+09:00"),
    )
    assert created_response.status_code == 201
    created = created_response.json()
    job_id = created["job"]["id"]
    customer_secret = _secret(created, "customer")
    company_secret = _secret(created, "company_manager")

    session_response = await workspace_client.post(
        "/api/v1/sessions",
        headers={"Authorization": f"Bearer {customer_secret}"},
    )
    assert session_response.status_code == 201
    csrf_token = session_response.json()["csrf_token"]
    zone_id = created["job"]["locations"][0]["room_zones"][0]["id"]
    initial_scope = await workspace_client.post(
        f"/api/v1/move-jobs/{job_id}/scope-versions",
        headers={"Authorization": f"Bearer {customer_secret}"},
        json={
            "content": {
                "schema_version": 2,
                "items": [
                    {
                        "item_key": "sofa",
                        "room_zone_id": zone_id,
                        "name": "소파",
                        "quantity": 1,
                        "unit": "개",
                        "review_status": "confirmed",
                        "source": "customer",
                    }
                ],
            }
        },
    )
    assert initial_scope.status_code == 201
    origin_conditions = {
        "residence_type": "apartment",
        "floor": {"status": "known", "value": 7},
        "elevator": "available",
        "stairs": "not_required",
        "ladder": "required",
        "parking_access": "restricted",
        "carry_distance": {"status": "known", "value_m": 20},
        "access_note": "지하주차장 진입 확인",
    }
    patch = {
        "title": "수정된 이사",
        "scheduled_at": "2026-08-22T13:30:00+09:00",
        "locations": [
            {
                "kind": "origin",
                "label": "수정된 출발지",
                "detail_address": "101동 1203호",
                "conditions": origin_conditions,
            }
        ],
    }
    denied = await workspace_client.patch(f"/api/v1/move-jobs/{job_id}", json=patch)
    assert denied.status_code == 401
    wrong_csrf = await workspace_client.patch(
        f"/api/v1/move-jobs/{job_id}",
        json=patch,
        headers={"X-SEQRET-CSRF": "x" * 43},
    )
    assert wrong_csrf.status_code == 401
    updated = await workspace_client.patch(
        f"/api/v1/move-jobs/{job_id}",
        json=patch,
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "수정된 이사"
    origin = next(item for item in updated.json()["locations"] if item["kind"] == "origin")
    assert origin["label"] == "수정된 출발지"
    assert origin["detail_address"] == "101동 1203호"
    assert origin["conditions"]["floor"] == {"status": "known", "value": 7}
    assert origin["conditions"]["ladder"] == "required"
    scopes_after_first_patch = await workspace_client.get(
        f"/api/v1/move-jobs/{job_id}/scope-versions",
        headers={"Authorization": f"Bearer {customer_secret}"},
    )
    assert scopes_after_first_patch.status_code == 200
    assert len(scopes_after_first_patch.json()) == 2
    origin_snapshot = next(
        item
        for item in scopes_after_first_patch.json()[-1]["content"]["location_conditions"]
        if item["kind"] == "origin"
    )
    assert origin_snapshot["conditions"]["floor"] == {"status": "known", "value": 7}
    assert origin_snapshot["conditions"]["ladder"] == "required"

    company_view = await workspace_client.get(
        f"/api/v1/move-jobs/{job_id}",
        headers={"Authorization": f"Bearer {company_secret}"},
    )
    assert company_view.status_code == 200
    assert company_view.json()["title"] == "수정된 이사"

    conditions_only = await workspace_client.patch(
        f"/api/v1/move-jobs/{job_id}",
        json={
            "locations": [
                {
                    "kind": "destination",
                    "conditions": {
                        "residence_type": "villa",
                        "floor": {"status": "unknown", "value": None},
                        "elevator": "unknown",
                        "stairs": "unknown",
                        "ladder": "not_required",
                        "parking_access": "unknown",
                        "carry_distance": {"status": "unknown", "value_m": None},
                    },
                }
            ]
        },
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert conditions_only.status_code == 200
    scopes_after_second_patch = await workspace_client.get(
        f"/api/v1/move-jobs/{job_id}/scope-versions",
        headers={"Authorization": f"Bearer {customer_secret}"},
    )
    assert scopes_after_second_patch.status_code == 200
    assert len(scopes_after_second_patch.json()) == 3
    destination_snapshot = next(
        item
        for item in scopes_after_second_patch.json()[-1]["content"]["location_conditions"]
        if item["kind"] == "destination"
    )
    assert destination_snapshot["conditions"]["residence_type"] == "villa"
    assert destination_snapshot["conditions"]["ladder"] == "not_required"

    async def raise_scope_conflict(*_args: object, **_kwargs: object) -> None:
        raise ScopeVersionConflictError(job_id)

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            "app.modules.move_job.service.create_scope_version",
            raise_scope_conflict,
        )
        raced_patch = await workspace_client.patch(
            f"/api/v1/move-jobs/{job_id}",
            json={
                "locations": [
                    {
                        "kind": "origin",
                        "conditions": origin_conditions,
                    }
                ]
            },
            headers={"X-SEQRET-CSRF": csrf_token},
        )
    assert raced_patch.status_code == 409

    latest_scope_id = scopes_after_second_patch.json()[-1]["id"]
    approval_url = f"/api/v1/move-jobs/{job_id}/scope-versions/{latest_scope_id}/approvals"
    customer_approval = await workspace_client.post(
        approval_url,
        headers={"Authorization": f"Bearer {customer_secret}"},
    )
    manager_approval = await workspace_client.post(
        approval_url,
        headers={"Authorization": f"Bearer {company_secret}"},
    )
    assert customer_approval.status_code == manager_approval.status_code == 201
    locked_patch = await workspace_client.patch(
        f"/api/v1/move-jobs/{job_id}",
        json={
            "locations": [
                {
                    "kind": "origin",
                    "conditions": origin_conditions,
                }
            ]
        },
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert locked_patch.status_code == 409
    label_only = await workspace_client.patch(
        f"/api/v1/move-jobs/{job_id}",
        json={
            "locations": [
                {
                    "kind": "destination",
                    "label": "수정된 도착지",
                    "detail_address": None,
                }
            ]
        },
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert label_only.status_code == 200
    destination = next(
        item for item in label_only.json()["locations"] if item["kind"] == "destination"
    )
    assert destination["detail_address"] is None
    unscheduled = await workspace_client.patch(
        f"/api/v1/move-jobs/{job_id}",
        json={"scheduled_at": None},
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert unscheduled.status_code == 200
    assert unscheduled.json()["scheduled_at"] is None

    no_consent = await workspace_client.put(
        "/api/v1/session/contact-points/email",
        json={"destination": "owner@example.com", "delivery_consent": False},
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert no_consent.status_code == 422
    assert "owner@example.com" not in no_consent.text
    validation_detail = no_consent.json()["detail"]
    assert validation_detail
    assert all(set(item) == {"type", "loc", "msg"} for item in validation_detail)
    invalid_phone = await workspace_client.put(
        "/api/v1/session/contact-points/sms",
        json={"destination": "010-1234-5678", "delivery_consent": True},
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert invalid_phone.status_code == 422
    foreign_phone = await workspace_client.put(
        "/api/v1/session/contact-points/sms",
        json={"destination": "+12025550123", "delivery_consent": True},
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert foreign_phone.status_code == 422
    invalid_email = await workspace_client.put(
        "/api/v1/session/contact-points/email",
        json={"destination": "invalid-email", "delivery_consent": True},
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert invalid_email.status_code == 422
    oversized_destination = f"private+{'x' * 301}@example.com"
    oversized_email = await workspace_client.put(
        "/api/v1/session/contact-points/email",
        json={"destination": oversized_destination, "delivery_consent": True},
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert oversized_email.status_code == 422
    assert oversized_destination not in oversized_email.text
    email = await workspace_client.put(
        "/api/v1/session/contact-points/email",
        json={"destination": "Owner@Example.com", "delivery_consent": True},
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert email.status_code == 200
    assert email.json()["masked_destination"] == "ow***@example.com"
    assert "destination" not in email.json()
    contacts = await workspace_client.get("/api/v1/session/contact-points")
    assert contacts.status_code == 200
    assert contacts.json()["contacts"] == [email.json()]
    sms = await workspace_client.put(
        "/api/v1/session/contact-points/sms",
        json={"destination": "+82 (10) 1234-5678", "delivery_consent": True},
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert sms.status_code == 200
    assert sms.json()["masked_destination"] == "+82******5678"

    removed = await workspace_client.delete(
        "/api/v1/session/contact-points/email",
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert removed.status_code == 204
    removed_sms = await workspace_client.delete(
        "/api/v1/session/contact-points/sms",
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert removed_sms.status_code == 204
    assert (await workspace_client.get("/api/v1/session/contact-points")).json() == {"contacts": []}
    missing = await workspace_client.delete(
        "/api/v1/session/contact-points/email",
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert missing.status_code == 404
    restored_email = await workspace_client.put(
        "/api/v1/session/contact-points/email",
        json={"destination": "restored@example.com", "delivery_consent": True},
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert restored_email.status_code == 200
    assert restored_email.json()["masked_destination"] == "re******@example.com"
    assert (
        await workspace_client.delete(
            "/api/v1/session/contact-points/email",
            headers={"X-SEQRET-CSRF": csrf_token},
        )
    ).status_code == 204

    canceled = await workspace_client.delete(
        f"/api/v1/move-jobs/{job_id}",
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert canceled.status_code == 204
    empty_workspace = await workspace_client.get("/api/v1/session")
    assert empty_workspace.status_code == 200
    assert empty_workspace.json()["members"] == []
    assert (await workspace_client.get(f"/api/v1/move-jobs/{job_id}")).status_code == 401

    old_cookie = workspace_client.cookies.get("seqret_workspace_session")
    assert old_cookie is not None
    logged_out = await workspace_client.delete(
        "/api/v1/session",
        headers={"X-SEQRET-CSRF": csrf_token},
    )
    assert logged_out.status_code == 204
    assert "Max-Age=0" in logged_out.headers["set-cookie"]
    assert (await workspace_client.get("/api/v1/session")).status_code == 401
    revoked = await workspace_client.get(
        "/api/v1/session",
        headers={"Cookie": f"seqret_workspace_session={old_cookie}"},
    )
    assert revoked.status_code == 401


@pytest.mark.anyio
async def test_workspace_rejects_missing_or_invalid_cookie(workspace_client: AsyncClient) -> None:
    assert (await workspace_client.get("/api/v1/move-jobs")).status_code == 401
    assert (await workspace_client.get("/api/v1/session/contact-points")).status_code == 401
    for cookie in ("short", "x" * 43):
        headers = {"Cookie": f"seqret_workspace_session={cookie}"}
        assert (await workspace_client.get("/api/v1/session", headers=headers)).status_code == 401
        assert (
            await workspace_client.get("/api/v1/session/contact-points", headers=headers)
        ).status_code == 401
        assert (await workspace_client.get("/api/v1/move-jobs", headers=headers)).status_code == 401


@pytest.mark.anyio
async def test_contact_revoke_uses_runtime_update_privileges() -> None:
    session = AsyncMock(spec=AsyncSession)
    contact = SimpleNamespace(
        destination="owner@example.com",
        enabled=True,
        updated_at=datetime(2026, 8, 19, tzinfo=UTC),
    )
    session.scalar = AsyncMock(return_value=contact)
    session.execute = AsyncMock()
    session.flush = AsyncMock()

    await delete_contact_point(
        session,
        uuid4(),
        NotificationContactChannel.EMAIL,
    )

    assert contact.destination == "revoked:email"
    assert contact.enabled is False
    assert contact.updated_at > datetime(2026, 8, 19, tzinfo=UTC)


@pytest.mark.anyio
async def test_accepted_invitation_is_restored_in_workspace(workspace_client: AsyncClient) -> None:
    created_response = await workspace_client.post(
        "/api/v1/move-jobs/onboarding",
        json={
            "title": "초대 이사",
            "customer_display_name": "초대 고객",
            "locations": [
                {
                    "kind": "origin",
                    "label": "출발지",
                    "room_zones": [{"name": "거실", "sort_order": 0}],
                }
            ],
        },
    )
    assert created_response.status_code == 201
    created = created_response.json()
    job_id = created["job"]["id"]
    invitation_response = await workspace_client.post(
        f"/api/v1/move-jobs/{job_id}/invitations",
        json={"role": "company_manager", "display_name": "초대 업체"},
        headers={"Authorization": f"Bearer {created['customer_access_link']['secret']}"},
    )
    assert invitation_response.status_code == 201
    invitation = invitation_response.json()
    manager_headers = {"Authorization": f"Bearer {invitation['access_link']['secret']}"}
    accepted = await workspace_client.post(
        f"/api/v1/move-jobs/{job_id}/invitations/{invitation['invitation']['id']}/accept",
        headers=manager_headers,
    )
    assert accepted.status_code == 200
    workspace = await workspace_client.post("/api/v1/sessions", headers=manager_headers)
    assert workspace.status_code == 201
    restored_invitation = workspace.json()["members"][0]["invitation"]
    assert restored_invitation["status"] == "accepted"
    assert restored_invitation["display_name"] == "초대 업체"


@pytest.mark.anyio
async def test_workspace_service_rejects_invalid_participants_and_memberships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=None)
    with pytest.raises(InvalidWorkspaceSessionError):
        await create_or_extend_workspace_session(
            session,
            uuid4(),
            current_cookie_secret=None,
        )

    participant = SimpleNamespace(
        id=uuid4(),
        job_id=uuid4(),
        role=ParticipantRole.COMPANY_MANAGER,
        display_name="Pending manager",
    )
    session.scalar = AsyncMock(
        side_effect=[participant, SimpleNamespace(status=InvitationStatus.PENDING)]
    )
    with pytest.raises(WorkspaceConflictError, match="must be accepted"):
        await create_or_extend_workspace_session(
            session,
            participant.id,
            current_cookie_secret=None,
        )

    now = datetime(2026, 8, 19, tzinfo=UTC)
    principal = WorkspacePrincipal(
        session_id=uuid4(),
        account_id=uuid4(),
        role=ParticipantRole.COMPANY_MANAGER,
        display_name="Manager",
        expires_at=now,
        csrf_token="x" * 43,
    )
    monkeypatch.setattr(
        "app.modules.access.workspace.authenticate_workspace_account",
        AsyncMock(return_value=principal),
    )
    session.scalar = AsyncMock(return_value=None)
    with pytest.raises(InvalidWorkspaceSessionError):
        await authenticate_workspace_actor(session, "x" * 43, uuid4())

    session.scalar = AsyncMock(
        side_effect=[participant, SimpleNamespace(status=InvitationStatus.PENDING)]
    )
    with pytest.raises(InvalidWorkspaceSessionError):
        await authenticate_workspace_actor(session, "x" * 43, participant.job_id)

    session.scalar = AsyncMock(side_effect=[participant, None])
    actor = await authenticate_workspace_actor(session, "x" * 43, participant.job_id)
    assert actor.participant_id == participant.id
    assert actor.request_id is not None
    assert actor.trace_id
    assert principal.expires_at == now


def test_workspace_response_helpers_cover_pending_invitation_and_deployed_cookie() -> None:
    now = datetime(2026, 8, 19, tzinfo=UTC)
    invitation = SimpleNamespace(
        id=uuid4(),
        job_id=uuid4(),
        issuer_participant_id=uuid4(),
        invitee_participant_id=uuid4(),
        role=ParticipantRole.COMPANY_MANAGER,
        invitee=SimpleNamespace(display_name="Pending manager"),
        status=InvitationStatus.PENDING,
        issued_at=now,
        expires_at=now,
        resolved_at=None,
    )
    assert _invitation_response(cast(Any, invitation)).resolved_at is None
    application = SimpleNamespace(
        state=SimpleNamespace(
            runtime_context=SimpleNamespace(settings=Settings(environment=AppEnvironment.STAGING))
        )
    )
    request = Request({"type": "http", "app": application})
    assert _workspace_cookie_security(request) == (True, "none")


def test_contact_destination_normalization_is_linear_and_preserves_contract() -> None:
    assert (
        _normalized_destination(
            NotificationContactChannel.EMAIL,
            " Owner.Tag+Alerts@Sub.Example.com ",
        )
        == "owner.tag+alerts@sub.example.com"
    )
    adversarial = "!@!." + "!." * 10_000
    with pytest.raises(ValueError, match="email destination is invalid"):
        _normalized_destination(NotificationContactChannel.EMAIL, adversarial)
    for invalid in (
        "owner@example",
        "owner@@example.com",
        ".owner@example.com",
        "owner.@example.com",
        "owner..tag@example.com",
        "owner@.example.com",
        "owner@example..com",
        "owner@example.com.",
        "owner@exa mple.com",
        "owner@\x00example.com",
    ):
        with pytest.raises(ValueError, match="email destination is invalid"):
            _normalized_destination(NotificationContactChannel.EMAIL, invalid)


@pytest.mark.anyio
async def test_workspace_auth_rejects_missing_bearer_and_expired_session() -> None:
    with pytest.raises(HTTPException) as bearer_error:
        await get_bearer_secret(None)
    assert bearer_error.value.status_code == 401

    expired = SimpleNamespace(
        revoked_at=None,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    account = SimpleNamespace()
    result = SimpleNamespace(one_or_none=lambda: (expired, account))
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=result)
    with pytest.raises(InvalidWorkspaceSessionError):
        await _load_session(session, "x" * 43, touch=False)
