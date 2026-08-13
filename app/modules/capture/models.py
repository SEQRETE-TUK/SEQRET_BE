"""A-owned capture session and media asset persistence."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.platform.db.base import Base


class CaptureSession(Base):
    """One participant-owned collection of media for a move job."""

    __tablename__ = "capture_session"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class MediaAsset(Base):
    """Private object metadata whose signed URLs are never persisted."""

    __tablename__ = "media_asset"
    __table_args__ = (
        UniqueConstraint("object_key"),
        CheckConstraint(
            "expected_size_bytes > 0 AND expected_size_bytes <= 209715200",
            name="expected_size_within_limit",
        ),
        CheckConstraint(
            "content_type IN ('image/jpeg', 'image/png', 'video/mp4')",
            name="content_type_allowed",
        ),
        CheckConstraint(
            "content_type NOT LIKE 'image/%' OR expected_size_bytes <= 20971520",
            name="image_size_within_limit",
        ),
        CheckConstraint(
            "actual_size_bytes IS NULL OR actual_size_bytes >= 0",
            name="actual_size_nonnegative",
        ),
        CheckConstraint(
            "sha256_hex IS NULL OR length(sha256_hex) = 64",
            name="sha256_length",
        ),
        CheckConstraint(
            "media_purpose IN ('INVENTORY', 'CONDITION', 'CHANGE_EVIDENCE', 'COMPLETION')",
            name="media_purpose",
        ),
        CheckConstraint(
            "status IN ('PENDING_UPLOAD', 'UPLOADED', 'PROCESSING', 'READY', 'FAILED', 'DELETED')",
            name="media_asset_status",
        ),
        CheckConstraint(
            "(status = 'PENDING_UPLOAD' AND actual_size_bytes IS NULL "
            "AND sha256_hex IS NULL AND generation IS NULL AND uploaded_at IS NULL) OR "
            "(status <> 'PENDING_UPLOAD' AND actual_size_bytes IS NOT NULL "
            "AND generation IS NOT NULL AND length(trim(generation)) > 0 "
            "AND uploaded_at IS NOT NULL)",
            name="media_asset_metadata_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    capture_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("capture_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    room_zone_id: Mapped[UUID] = mapped_column(
        ForeignKey("room_zone.id", ondelete="RESTRICT"),
        nullable=False,
    )
    media_purpose: Mapped[MediaPurpose] = mapped_column(
        Enum(MediaPurpose, name="media_purpose", native_enum=False),
        nullable=False,
    )
    status: Mapped[MediaAssetStatus] = mapped_column(
        Enum(MediaAssetStatus, name="media_asset_status", native_enum=False),
        nullable=False,
        default=MediaAssetStatus.PENDING_UPLOAD,
    )
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    expected_size_bytes: Mapped[int] = mapped_column(nullable=False)
    actual_size_bytes: Mapped[int | None] = mapped_column()
    sha256_hex: Mapped[str | None] = mapped_column(String(64))
    generation: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
