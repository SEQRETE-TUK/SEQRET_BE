"""B-owned task queue adapters."""

from app.platform.task_queue.cloud_tasks import GoogleCloudTasksQueue

__all__ = ["GoogleCloudTasksQueue"]
