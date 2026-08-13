"""B-owned Cloud Storage adapter behind the provider-neutral StoragePort."""

from app.platform.storage.gcs import GoogleCloudStorage

__all__ = ["GoogleCloudStorage"]
