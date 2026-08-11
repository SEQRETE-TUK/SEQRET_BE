"""Common runtime bootstrap contract."""

from dataclasses import dataclass
from enum import StrEnum

from app.config import Settings, get_settings


class RuntimeKind(StrEnum):
    """Deployable process types in the modular monolith."""

    API = "api"
    WORKER = "worker"
    JOB = "job"


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Immutable settings and identity for one process runtime."""

    kind: RuntimeKind
    settings: Settings

    @property
    def service_name(self) -> str:
        """Return the runtime-specific service identity."""

        return f"{self.settings.service_name}-{self.kind.value}"


def create_runtime_context(
    kind: RuntimeKind,
    settings: Settings | None = None,
) -> RuntimeContext:
    """Create a runtime context from the shared settings contract."""

    resolved_settings = settings if settings is not None else get_settings()
    return RuntimeContext(kind=kind, settings=resolved_settings)
