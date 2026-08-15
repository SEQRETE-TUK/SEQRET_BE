"""Immutable job-scoped dispatch snapshots and field check-ins."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base


class DispatchSetup(Base):
    """One immutable availability and requirement snapshot for a move job."""

    __tablename__ = "dispatch_setup"
    __table_args__ = (
        UniqueConstraint("job_id"),
        CheckConstraint("expected_duration_minutes BETWEEN 1 AND 720", name="duration_range"),
        CheckConstraint("required_vehicle_capacity_m2 >= 0", name="vehicle_capacity_nonnegative"),
        CheckConstraint("required_worker_count BETWEEN 1 AND 50", name="worker_count_range"),
        CheckConstraint("length(command_hash) = 64", name="command_hash_length"),
        Index("ix_dispatch_setup_start", "start_at", "job_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_reference: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_scope_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("scope_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_by_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    required_vehicle_capacity_m2: Mapped[int] = mapped_column(Integer, nullable=False)
    required_worker_count: Mapped[int] = mapped_column(Integer, nullable=False)
    required_skills: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    required_certifications: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    check_in_items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    completion_check_items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        server_default=text(
            '\'[{"key":"tools_removed","label":"작업 도구와 자재 회수"},'
            '{"key":"site_restored","label":"출발지와 도착지 정리"},'
            '{"key":"changes_recorded","label":"변경·이슈 기록 확인"}]\''
        ),
        default=lambda: [
            {"key": "tools_removed", "label": "작업 도구와 자재 회수"},
            {"key": "site_restored", "label": "출발지와 도착지 정리"},
            {"key": "changes_recorded", "label": "변경·이슈 기록 확인"},
        ],
    )
    origin_conditions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    safety_notice: Mapped[str] = mapped_column(String(2000), nullable=False)
    vehicle_options: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    worker_options: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class DispatchPlan(Base):
    """One final assignment against an immutable dispatch setup."""

    __tablename__ = "dispatch_plan"
    __table_args__ = (
        UniqueConstraint("job_id"),
        UniqueConstraint("setup_id"),
        CheckConstraint("length(command_hash) = 64", name="command_hash_length"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="CASCADE"),
        nullable=False,
    )
    setup_id: Mapped[UUID] = mapped_column(
        ForeignKey("dispatch_setup.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_scope_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("scope_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    vehicle_option_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    lead_worker_option_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    selected_worker_option_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    worker_note: Mapped[str | None] = mapped_column(String(2000))
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_by_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class FieldCheckIn(Base):
    """One replay-safe representative worker arrival record."""

    __tablename__ = "field_check_in"
    __table_args__ = (
        UniqueConstraint("job_id", "participant_id"),
        UniqueConstraint("dispatch_plan_id", "participant_id"),
        Index("ix_field_check_in_job_checked", "job_id", "checked_in_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="CASCADE"),
        nullable=False,
    )
    dispatch_plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("dispatch_plan.id", ondelete="RESTRICT"),
        nullable=False,
    )
    participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    worker_option_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    confirmed_check_keys: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    checked_in_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
