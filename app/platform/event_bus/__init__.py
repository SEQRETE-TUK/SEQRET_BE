"""Transactional event delivery infrastructure."""

from app.platform.event_bus.service import enqueue_domain_event

__all__ = ["enqueue_domain_event"]
