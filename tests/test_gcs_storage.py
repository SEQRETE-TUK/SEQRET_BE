"""B-01 Google Cloud Storage adapter tests without network access."""

import asyncio
import time
from datetime import timedelta

import pytest
from google.api_core import exceptions as google_exceptions
from google.auth import exceptions as auth_exceptions
from google.auth.credentials import AnonymousCredentials, Credentials, Signing

from app.contracts.ports import (
    ObjectStoragePort,
    ProviderError,
    ProviderErrorKind,
    StorageUploadTarget,
)
from app.contracts.primitives import IdempotencyKey
from app.platform.storage.gcs import GoogleCloudStorage


class StubBlob:
    def __init__(
        self,
        *,
        generation: int | None = 7,
        size: int | None = 10,
        content_type: str | None = "image/jpeg",
        signed_url: str = "https://storage.example/signed",
        reload_error: Exception | None = None,
        delete_error: Exception | None = None,
        signed_url_error: Exception | None = None,
        sleep_seconds: float = 0.0,
    ) -> None:
        self.generation = generation
        self.size = size
        self.content_type = content_type
        self._signed_url = signed_url
        self._reload_error = reload_error
        self._delete_error = delete_error
        self._signed_url_error = signed_url_error
        self._sleep_seconds = sleep_seconds
        self.signed_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []
        self.reloaded = False

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
    ) -> str:
        self.signed_calls.append(
            {
                "version": version,
                "expiration": expiration,
                "method": method,
                "content_type": content_type,
                "generation": generation,
                "headers": headers,
                "credentials": credentials,
            }
        )
        if self._signed_url_error is not None:
            raise self._signed_url_error
        return self._signed_url

    def reload(self, *, timeout: float) -> None:
        if self._sleep_seconds:
            time.sleep(self._sleep_seconds)
        if self._reload_error is not None:
            raise self._reload_error
        self.reloaded = True

    def delete(self, *, timeout: float, if_generation_match: int | None = None) -> None:
        self.delete_calls.append({"timeout": timeout, "if_generation_match": if_generation_match})
        if self._delete_error is not None:
            raise self._delete_error


class StubBucket:
    def __init__(self, blob: StubBlob) -> None:
        self._blob = blob
        self.requested: list[str] = []

    def blob(self, blob_name: str) -> StubBlob:
        self.requested.append(blob_name)
        return self._blob


class StubClient:
    def __init__(self, bucket: StubBucket) -> None:
        self._bucket = bucket
        self.requested: list[str] = []

    def bucket(self, bucket_name: str) -> StubBucket:
        self.requested.append(bucket_name)
        return self._bucket


def _adapter(blob: StubBlob) -> tuple[GoogleCloudStorage, StubBucket]:
    bucket = StubBucket(blob)
    adapter = GoogleCloudStorage("seqret-media", client_factory=lambda: StubClient(bucket))
    return adapter, bucket


def _iam_adapter(
    blob: StubBlob,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[GoogleCloudStorage, StubBucket]:
    source_credentials: Credentials = AnonymousCredentials()  # type: ignore[no-untyped-call]

    def default_credentials(*, scopes: tuple[str, ...]) -> tuple[Credentials, None]:
        assert scopes == ("https://www.googleapis.com/auth/cloud-platform",)
        return source_credentials, None

    monkeypatch.setattr("app.platform.storage.gcs.google.auth.default", default_credentials)
    bucket = StubBucket(blob)
    return (
        GoogleCloudStorage(
            "seqret-media",
            signing_service_account_email="seqret-api@seqret-test.iam.gserviceaccount.com",
            client_factory=lambda: StubClient(bucket),
        ),
        bucket,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_create_upload_url_signs_and_returns_exact_create_only_headers() -> None:
    signed_url = "  https://storage.example/upload?X-Signature=A%2B  "
    blob = StubBlob(signed_url=signed_url)
    adapter, bucket = _adapter(blob)

    assert isinstance(adapter, ObjectStoragePort)
    target = await adapter.create_upload_url(
        object_key="jobs/1/bedroom.mp4",
        content_type="video/mp4",
        content_length=1024,
        expires_in_seconds=300,
        timeout_seconds=2,
    )

    assert target == StorageUploadTarget(
        url=signed_url,
        headers=(
            ("Content-Type", "video/mp4"),
            ("x-goog-if-generation-match", "0"),
        ),
    )
    assert bucket.requested == ["jobs/1/bedroom.mp4"]
    call = blob.signed_calls[0]
    assert call["method"] == "PUT"
    assert call["content_type"] == "video/mp4"
    assert call["headers"] == {"x-goog-if-generation-match": "0"}
    assert call["version"] == "v4"


@pytest.mark.anyio
async def test_create_read_url_pins_generation() -> None:
    blob = StubBlob(signed_url="https://storage.example/read")
    adapter, _ = _adapter(blob)

    url = await adapter.create_read_url(
        object_key="jobs/1/photo.jpg",
        generation="7",
        expires_in_seconds=60,
        timeout_seconds=2,
    )

    assert url == "https://storage.example/read"
    call = blob.signed_calls[0]
    assert call["method"] == "GET"
    assert call["generation"] == "7"


@pytest.mark.anyio
async def test_signed_urls_use_explicit_iam_signing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blob = StubBlob()
    adapter, _ = _iam_adapter(blob, monkeypatch)

    await adapter.create_upload_url(
        object_key="jobs/1/photo.jpg",
        content_type="image/jpeg",
        content_length=10,
        expires_in_seconds=60,
        timeout_seconds=2,
    )
    await adapter.create_read_url(
        object_key="jobs/1/photo.jpg",
        generation="7",
        expires_in_seconds=60,
        timeout_seconds=2,
    )

    signing_credentials = blob.signed_calls[0]["credentials"]
    assert isinstance(signing_credentials, Signing)
    assert signing_credentials is blob.signed_calls[1]["credentials"]
    assert signing_credentials.signer_email == "seqret-api@seqret-test.iam.gserviceaccount.com"


@pytest.mark.anyio
async def test_iam_signing_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    provider_message = "signBlob denied for secret-token"
    blob = StubBlob(
        signed_url_error=auth_exceptions.TransportError(  # type: ignore[no-untyped-call]
            provider_message
        ),
    )
    adapter, _ = _iam_adapter(blob, monkeypatch)

    with pytest.raises(ProviderError, match="storage operation failed") as error_info:
        await adapter.create_read_url(
            object_key="jobs/1/photo.jpg",
            generation="7",
            expires_in_seconds=60,
            timeout_seconds=2,
        )

    assert error_info.value.kind is ProviderErrorKind.UNAVAILABLE
    assert error_info.value.retryable
    assert error_info.value.__cause__ is None
    assert provider_message not in str(error_info.value)


@pytest.mark.anyio
async def test_get_metadata_reads_blob_attributes() -> None:
    blob = StubBlob(generation=11, size=2048, content_type="image/png")
    adapter, _ = _adapter(blob)

    metadata = await adapter.get_metadata(object_key="jobs/1/photo.png", timeout_seconds=2)

    assert blob.reloaded
    assert metadata.object_key == "jobs/1/photo.png"
    assert metadata.content_type == "image/png"
    assert metadata.size_bytes == 2048
    assert metadata.generation == "11"


@pytest.mark.anyio
async def test_get_metadata_without_generation() -> None:
    blob = StubBlob(generation=None)
    adapter, _ = _adapter(blob)

    metadata = await adapter.get_metadata(object_key="jobs/1/photo.jpg", timeout_seconds=2)

    assert metadata.generation is None


@pytest.mark.anyio
async def test_delete_object_pins_generation() -> None:
    blob = StubBlob()
    adapter, _ = _adapter(blob)

    await adapter.delete_object(
        object_key="jobs/1/photo.jpg",
        generation="7",
        idempotency_key=IdempotencyKey("media-delete:1"),
        timeout_seconds=2,
    )

    assert blob.delete_calls[0]["if_generation_match"] == 7


@pytest.mark.parametrize(
    "delete_error",
    [
        google_exceptions.NotFound("gone"),  # type: ignore[no-untyped-call]
        google_exceptions.PreconditionFailed("stale generation"),  # type: ignore[no-untyped-call]
    ],
)
@pytest.mark.anyio
async def test_delete_object_treats_missing_snapshot_as_success(
    delete_error: Exception,
) -> None:
    blob = StubBlob(delete_error=delete_error)
    adapter, _ = _adapter(blob)

    await adapter.delete_object(
        object_key="jobs/1/photo.jpg",
        generation="7",
        idempotency_key=IdempotencyKey("media-delete:3"),
        timeout_seconds=2,
    )


@pytest.mark.parametrize(
    ("provider_error", "kind", "retryable"),
    [
        (google_exceptions.NotFound("missing"), ProviderErrorKind.NOT_FOUND, False),  # type: ignore[no-untyped-call]
        (google_exceptions.Conflict("conflict"), ProviderErrorKind.CONFLICT, False),  # type: ignore[no-untyped-call]
        (google_exceptions.BadRequest("bad"), ProviderErrorKind.INVALID_INPUT, False),  # type: ignore[no-untyped-call]
        (google_exceptions.Forbidden("denied"), ProviderErrorKind.PERMISSION_DENIED, False),  # type: ignore[no-untyped-call]
        (google_exceptions.DeadlineExceeded("late"), ProviderErrorKind.DEADLINE_EXCEEDED, True),  # type: ignore[no-untyped-call]
        (RuntimeError("offline"), ProviderErrorKind.UNAVAILABLE, True),
    ],
)
@pytest.mark.anyio
async def test_get_metadata_maps_provider_errors(
    provider_error: Exception,
    kind: ProviderErrorKind,
    retryable: bool,
) -> None:
    blob = StubBlob(reload_error=provider_error)
    adapter, _ = _adapter(blob)

    with pytest.raises(ProviderError, match="storage operation failed") as error_info:
        await adapter.get_metadata(object_key="jobs/1/photo.jpg", timeout_seconds=2)

    assert error_info.value.kind is kind
    assert error_info.value.retryable is retryable
    assert error_info.value.__cause__ is None
    assert str(provider_error) not in str(error_info.value)


@pytest.mark.anyio
async def test_operation_timeout_maps_to_deadline_exceeded() -> None:
    blob = StubBlob(sleep_seconds=0.2)
    adapter, _ = _adapter(blob)

    with pytest.raises(ProviderError) as error_info:
        await adapter.get_metadata(object_key="jobs/1/photo.jpg", timeout_seconds=0.01)

    assert error_info.value.kind is ProviderErrorKind.DEADLINE_EXCEEDED
    await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        (
            {
                "object_key": "k",
                "content_type": "image/jpeg",
                "content_length": 0,
                "expires_in_seconds": 60,
                "timeout_seconds": 2,
            },
            "content_length must be positive",
        ),
        (
            {
                "object_key": "k",
                "content_type": "image/jpeg",
                "content_length": 1,
                "expires_in_seconds": 0,
                "timeout_seconds": 2,
            },
            "expires_in_seconds must be positive",
        ),
        (
            {
                "object_key": "k",
                "content_type": "image/jpeg",
                "content_length": 1,
                "expires_in_seconds": 60,
                "timeout_seconds": 0,
            },
            "timeout_seconds must be positive",
        ),
    ],
)
@pytest.mark.anyio
async def test_create_upload_url_rejects_invalid_arguments(
    kwargs: dict[str, object],
    match: str,
) -> None:
    adapter, _ = _adapter(StubBlob())

    with pytest.raises(ValueError, match=match):
        await adapter.create_upload_url(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("generation", ["", " ", "abc", "0", "7 ", "1" * 256])
@pytest.mark.anyio
async def test_create_read_url_rejects_invalid_generation(generation: str) -> None:
    adapter, _ = _adapter(StubBlob())

    with pytest.raises(ValueError, match="generation must be"):
        await adapter.create_read_url(
            object_key="k",
            generation=generation,
            expires_in_seconds=60,
            timeout_seconds=2,
        )


@pytest.mark.anyio
async def test_delete_object_rejects_missing_generation_without_provider_call() -> None:
    blob = StubBlob()
    adapter, _ = _adapter(blob)

    with pytest.raises(ValueError, match="generation"):
        await adapter.delete_object(
            object_key="jobs/1/photo.jpg",
            generation=None,  # type: ignore[arg-type]
            idempotency_key=IdempotencyKey("media-delete:missing-generation"),
            timeout_seconds=2,
        )

    assert blob.delete_calls == []
