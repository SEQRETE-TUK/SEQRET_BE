"""Google Cloud Storage adapter isolated behind the provider-neutral port.

Blocking client calls run in a worker thread with an explicit timeout so the
event loop never stalls. Object keys and generations are the only object detail
this adapter touches; signed URLs are generated on demand and never persisted.
"""

import asyncio
import hashlib
from collections.abc import Callable
from datetime import timedelta
from typing import BinaryIO, Protocol, cast

import google.auth
import google.cloud.storage as storage  # type: ignore[import-untyped]
from google.api_core import exceptions as google_exceptions
from google.auth import impersonated_credentials
from google.auth.credentials import Credentials, Signing

from app.contracts.ports import (
    ProviderError,
    ProviderErrorKind,
    StorageObjectMetadata,
    StorageUploadTarget,
)
from app.contracts.primitives import IdempotencyKey

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class Blob(Protocol):
    """Subset of the Cloud Storage blob surface this adapter relies on."""

    generation: int | None
    size: int | None
    content_type: str | None

    def generate_signed_url(
        self,
        *,
        version: str,
        expiration: timedelta,
        method: str,
        content_type: str | None = None,
        generation: str | None = None,
        headers: dict[str, str] | None = None,
        credentials: Signing | None = None,
    ) -> str: ...

    def reload(self, *, timeout: float) -> None: ...

    def open(
        self,
        mode: str,
        *,
        chunk_size: int,
        timeout: float,
    ) -> BinaryIO: ...

    def delete(self, *, timeout: float, if_generation_match: int | None = None) -> None: ...


class Bucket(Protocol):
    def blob(self, blob_name: str, generation: int | None = None) -> Blob: ...


class StorageClient(Protocol):
    def bucket(self, bucket_name: str) -> Bucket: ...


def _map_storage_error(error: Exception) -> ProviderError:
    if isinstance(error, google_exceptions.NotFound):
        kind, retryable = ProviderErrorKind.NOT_FOUND, False
    elif isinstance(error, (google_exceptions.Conflict, google_exceptions.AlreadyExists)):
        kind, retryable = ProviderErrorKind.CONFLICT, False
    elif isinstance(
        error,
        (
            google_exceptions.BadRequest,
            google_exceptions.InvalidArgument,
            google_exceptions.PreconditionFailed,
        ),
    ):
        kind, retryable = ProviderErrorKind.INVALID_INPUT, False
    elif isinstance(error, (google_exceptions.Forbidden, google_exceptions.Unauthenticated)):
        kind, retryable = ProviderErrorKind.PERMISSION_DENIED, False
    elif isinstance(error, google_exceptions.DeadlineExceeded):
        kind, retryable = ProviderErrorKind.DEADLINE_EXCEEDED, True
    else:
        kind, retryable = ProviderErrorKind.UNAVAILABLE, True
    return ProviderError(kind, "storage operation failed", retryable=retryable)


class GoogleCloudStorage:
    """StoragePort adapter that keeps GCP types out of the application layer."""

    def __init__(
        self,
        bucket_name: str,
        *,
        signing_service_account_email: str | None = None,
        client_factory: Callable[[], StorageClient] | None = None,
    ) -> None:
        factory = client_factory or (lambda: cast(StorageClient, storage.Client()))
        self._bucket = factory().bucket(bucket_name)
        self._signing_credentials: Signing | None = None
        if signing_service_account_email is not None:
            source_credentials: Credentials = google.auth.default(scopes=(CLOUD_PLATFORM_SCOPE,))[0]
            self._signing_credentials = impersonated_credentials.Credentials(
                source_credentials=source_credentials,
                target_principal=signing_service_account_email,
                target_scopes=(CLOUD_PLATFORM_SCOPE,),
            )  # type: ignore[no-untyped-call]

    @staticmethod
    def _require_positive(value: int | float, name: str) -> None:
        if value <= 0:
            raise ValueError(f"{name} must be positive")

    async def _run[T](self, timeout_seconds: float, operation: Callable[[], T]) -> T:
        self._require_positive(timeout_seconds, "timeout_seconds")
        try:
            return await asyncio.wait_for(asyncio.to_thread(operation), timeout=timeout_seconds)
        except TimeoutError:
            raise ProviderError(
                ProviderErrorKind.DEADLINE_EXCEEDED,
                "storage operation failed",
                retryable=True,
            ) from None
        except Exception as error:
            raise _map_storage_error(error) from None

    @staticmethod
    def _generation_number(generation: str) -> int:
        if (
            not isinstance(generation, str)
            or generation != generation.strip()
            or not generation.isdecimal()
            or len(generation) > 255
        ):
            raise ValueError("generation must be a 1..255 digit string")
        value = int(generation)
        if value <= 0:
            raise ValueError("generation must be positive")
        return value

    async def create_upload_url(
        self,
        *,
        object_key: str,
        content_type: str,
        content_length: int,
        expires_in_seconds: int,
        timeout_seconds: float,
    ) -> StorageUploadTarget:
        self._require_positive(content_length, "content_length")
        self._require_positive(expires_in_seconds, "expires_in_seconds")

        def operation() -> StorageUploadTarget:
            return StorageUploadTarget(
                url=self._bucket.blob(object_key).generate_signed_url(
                    version="v4",
                    expiration=timedelta(seconds=expires_in_seconds),
                    method="PUT",
                    content_type=content_type,
                    headers={"x-goog-if-generation-match": "0"},
                    credentials=self._signing_credentials,
                ),
                headers=(
                    ("Content-Type", content_type),
                    ("x-goog-if-generation-match", "0"),
                ),
            )

        return await self._run(timeout_seconds, operation)

    async def create_read_url(
        self,
        *,
        object_key: str,
        generation: str,
        expires_in_seconds: int,
        timeout_seconds: float,
    ) -> str:
        self._generation_number(generation)
        self._require_positive(expires_in_seconds, "expires_in_seconds")

        def operation() -> str:
            return self._bucket.blob(object_key).generate_signed_url(
                version="v4",
                expiration=timedelta(seconds=expires_in_seconds),
                method="GET",
                generation=generation,
                credentials=self._signing_credentials,
            )

        return await self._run(timeout_seconds, operation)

    async def get_metadata(
        self,
        *,
        object_key: str,
        timeout_seconds: float,
    ) -> StorageObjectMetadata:
        def operation() -> StorageObjectMetadata:
            blob = self._bucket.blob(object_key)
            blob.reload(timeout=timeout_seconds)
            return StorageObjectMetadata(
                object_key=object_key,
                content_type=cast(str, blob.content_type),
                size_bytes=cast(int, blob.size),
                generation=None if blob.generation is None else str(blob.generation),
            )

        return await self._run(timeout_seconds, operation)

    async def calculate_sha256(
        self,
        *,
        object_key: str,
        generation: str,
        timeout_seconds: float,
    ) -> str:
        generation_number = self._generation_number(generation)

        def operation() -> str:
            digest = hashlib.sha256()
            blob = self._bucket.blob(object_key, generation=generation_number)
            with blob.open(
                "rb",
                chunk_size=1024 * 1024,
                timeout=timeout_seconds,
            ) as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            return digest.hexdigest()

        return await self._run(timeout_seconds, operation)

    async def delete_object(
        self,
        *,
        object_key: str,
        generation: str,
        idempotency_key: IdempotencyKey,
        timeout_seconds: float,
    ) -> None:
        # A generation-pinned delete is idempotent by construction, so the
        # idempotency key is not needed to converge two attempts.
        del idempotency_key
        generation_number = self._generation_number(generation)

        def operation() -> None:
            blob = self._bucket.blob(object_key)
            try:
                blob.delete(
                    timeout=timeout_seconds,
                    if_generation_match=generation_number,
                )
            except (google_exceptions.NotFound, google_exceptions.PreconditionFailed):
                # The snapshot generation is already gone; never delete a
                # different generation, and treat the absence as success.
                return

        await self._run(timeout_seconds, operation)
