"""Nominal identifiers and time helpers for shared contracts."""

from datetime import UTC, datetime
from typing import Annotated, NewType
from uuid import UUID

from pydantic import StringConstraints

AggregateId = NewType("AggregateId", UUID)
AnalysisRunId = NewType("AnalysisRunId", UUID)
CaptureSessionId = NewType("CaptureSessionId", UUID)
EventId = NewType("EventId", UUID)
JobId = NewType("JobId", UUID)
MediaAssetId = NewType("MediaAssetId", UUID)
ParticipantId = NewType("ParticipantId", UUID)
RequestId = NewType("RequestId", UUID)
RoomZoneId = NewType("RoomZoneId", UUID)

IdempotencyKey = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
TraceId = Annotated[
    str,
    StringConstraints(min_length=32, max_length=32, pattern=r"^[0-9a-f]+$"),
]


def utc_now() -> datetime:
    """Return an aware UTC timestamp for contract defaults."""

    return datetime.now(UTC)
