"""NHN Cloud Email, SMS, and Kakao adapter contract tests."""

import json
from collections.abc import Mapping
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from uuid import uuid4

import pytest

from app.contracts.notification import ExternalNotificationChannel, OutboundNotification
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.contracts.primitives import IdempotencyKey
from app.platform.notification.nhn_cloud import (
    EMAIL_API,
    KAKAO_API,
    LMS_API,
    SMS_API,
    NhnCloudNotificationConfig,
    NhnCloudNotificationProvider,
    _provider_message_id,
)


def _config() -> NhnCloudNotificationConfig:
    return NhnCloudNotificationConfig(
        email_app_key="email-app",
        email_secret_key="email-secret",
        email_sender_address="notice@seqret.example.com",
        email_sender_name="SEQRET",
        sms_app_key="sms-app",
        sms_secret_key="sms-secret",
        sms_sender_number="0212345678",
        kakao_app_key="kakao-app",
        kakao_secret_key="kakao-secret",
        kakao_sender_key="sender-key",
        kakao_template_code="SEQRET_NOTICE",
    )


def _message(channel: ExternalNotificationChannel, destination: str) -> OutboundNotification:
    return OutboundNotification(
        notification_id=uuid4(),
        event_id=uuid4(),
        job_id=uuid4(),
        channel=channel,
        destination=destination,
        subject="[SEQRET] 알림",
        body="작업 상태가 변경되었습니다.",
        deep_link="https://seqret.example.com/jobs/current",
    )


@pytest.mark.anyio
async def test_provider_builds_official_channel_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases: list[
        tuple[
            ExternalNotificationChannel,
            str,
            str,
            str,
            dict[str, object],
            str,
        ]
    ] = [
        (
            ExternalNotificationChannel.EMAIL,
            "owner@example.com",
            EMAIL_API.format(app_key="email-app"),
            "email-secret",
            {
                "header": {"isSuccessful": True, "resultCode": 0},
                "body": {
                    "data": {
                        "requestId": "email-request",
                        "results": [{"resultCode": 0}],
                    }
                },
            },
            "email-request",
        ),
        (
            ExternalNotificationChannel.SMS,
            "+821012345678",
            SMS_API.format(app_key="sms-app"),
            "sms-secret",
            {
                "header": {"isSuccessful": True, "resultCode": 0},
                "body": {
                    "data": {
                        "requestId": "sms-request",
                        "sendResultList": [{"resultCode": 0}],
                    }
                },
            },
            "sms-request",
        ),
        (
            ExternalNotificationChannel.KAKAO,
            "+821087654321",
            KAKAO_API.format(app_key="kakao-app"),
            "kakao-secret",
            {
                "header": {"isSuccessful": True, "resultCode": 0},
                "message": {
                    "requestId": "kakao-request",
                    "sendResults": [{"resultCode": 0}],
                },
            },
            "kakao-request",
        ),
    ]
    for channel, destination, expected_url, secret, response, provider_id in cases:
        captured: dict[str, object] = {}

        def fake_post(
            url: str,
            headers: Mapping[str, str],
            payload: Mapping[str, object],
            timeout_seconds: float,
            *,
            response: dict[str, object] = response,
            captured: dict[str, object] = captured,
        ) -> dict[str, Any]:
            captured.update(
                url=url,
                headers=dict(headers),
                payload=dict(payload),
                timeout_seconds=timeout_seconds,
            )
            return response

        with monkeypatch.context() as patch:
            patch.setattr(NhnCloudNotificationProvider, "_post_json", staticmethod(fake_post))
            provider = NhnCloudNotificationProvider(_config())
            message = _message(channel, destination)
            result = await provider.send(
                message=message,
                idempotency_key=IdempotencyKey("notification:test"),
                timeout_seconds=2.0,
            )

        assert result.provider_message_id == provider_id
        assert captured["url"] == expected_url
        assert captured["timeout_seconds"] == 2.0
        headers = captured["headers"]
        assert isinstance(headers, dict)
        assert headers["X-Secret-Key"] == secret
        if channel is ExternalNotificationChannel.KAKAO:
            assert headers["X-NC-API-IDEMPOTENCY-KEY"] == "notification:test"
        else:
            assert "X-NC-API-IDEMPOTENCY-KEY" not in headers
        payload = captured["payload"]
        assert isinstance(payload, dict)
        assert secret not in json.dumps(payload, ensure_ascii=False)
        assert payload["senderGroupingKey"] == "notification:test"
        if channel is ExternalNotificationChannel.EMAIL:
            assert payload["receiverList"] == [
                {"receiveMailAddr": destination, "receiveType": "MRT0"}
            ]
            assert payload["senderAddress"] == "notice@seqret.example.com"
        elif channel is ExternalNotificationChannel.SMS:
            assert payload["recipientList"] == [
                {
                    "recipientNo": "01012345678",
                    "recipientGroupingKey": str(message.notification_id),
                }
            ]
            assert payload["sendNo"] == "0212345678"
            assert "title" not in payload
        else:
            recipients = payload["recipientList"]
            assert isinstance(recipients, list)
            assert recipients[0]["recipientNo"] == "01087654321"
            assert recipients[0]["templateParameter"] == {
                "message": message.body,
                "deepLink": message.deep_link,
            }


@pytest.mark.anyio
async def test_provider_rejects_unsuccessful_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        NhnCloudNotificationProvider,
        "_post_json",
        staticmethod(lambda *_: {"header": {"isSuccessful": False, "resultCode": -1}}),
    )
    provider = NhnCloudNotificationProvider(_config())

    with pytest.raises(ProviderError) as error_info:
        await provider.send(
            message=_message(ExternalNotificationChannel.EMAIL, "owner@example.com"),
            idempotency_key=IdempotencyKey("notification:rejected"),
            timeout_seconds=1,
        )

    assert error_info.value.kind is ProviderErrorKind.INVALID_INPUT
    assert not error_info.value.retryable


def test_provider_message_id_uses_only_provider_request_id() -> None:
    cases: list[tuple[dict[str, object], str | None]] = [
        ({"requestId": "root"}, "root"),
        ({"body": {"requestId": "body"}}, "body"),
        ({"body": {"data": {"requestId": "nested"}}}, "nested"),
        ({"message": {"requestId": "message"}}, "message"),
        ({"requestId": 0, "body": []}, None),
    ]
    for payload, expected in cases:
        assert _provider_message_id(payload) == expected


def test_provider_uses_sms_only_for_short_euc_kr_body() -> None:
    provider = NhnCloudNotificationProvider(_config())
    message = OutboundNotification(
        notification_id=uuid4(),
        event_id=uuid4(),
        job_id=uuid4(),
        channel=ExternalNotificationChannel.SMS,
        destination="+821012345678",
        subject="알림",
        body="확인",
        deep_link="x",
    )

    url, _, payload = provider._request_for(
        message,
        IdempotencyKey("notification:short"),
    )

    assert url == SMS_API.format(app_key="sms-app")
    assert "title" not in payload


def test_provider_uses_lms_for_long_euc_kr_body() -> None:
    provider = NhnCloudNotificationProvider(_config())
    message = OutboundNotification(
        notification_id=uuid4(),
        event_id=uuid4(),
        job_id=uuid4(),
        channel=ExternalNotificationChannel.SMS,
        destination="+821012345678",
        subject="알림",
        body="긴 알림 " * 20,
        deep_link="https://seqret.example.com/?job=00000000-0000-0000-0000-000000000000",
    )

    url, _, payload = provider._request_for(
        message,
        IdempotencyKey("notification:long"),
    )

    assert url == LMS_API.format(app_key="sms-app")
    assert payload["title"] == "알림"


def test_provider_rejects_text_body_over_lms_limit() -> None:
    provider = NhnCloudNotificationProvider(_config())
    message = OutboundNotification(
        notification_id=uuid4(),
        event_id=uuid4(),
        job_id=uuid4(),
        channel=ExternalNotificationChannel.SMS,
        destination="+821012345678",
        subject="알림",
        body="a" * 2_000,
        deep_link="b" * 2_000,
    )

    with pytest.raises(ProviderError) as error_info:
        provider._request_for(
            message,
            IdempotencyKey("notification:too-long"),
        )

    assert error_info.value.kind is ProviderErrorKind.INVALID_INPUT
    assert not error_info.value.retryable


def test_provider_rejects_non_korean_phone_destination() -> None:
    for channel in (ExternalNotificationChannel.SMS, ExternalNotificationChannel.KAKAO):
        provider = NhnCloudNotificationProvider(_config())
        with pytest.raises(ProviderError) as error_info:
            provider._request_for(
                _message(channel, "+12025550123"),
                IdempotencyKey("notification:foreign"),
            )
        assert error_info.value.kind is ProviderErrorKind.INVALID_INPUT
        assert not error_info.value.retryable


@pytest.mark.anyio
async def test_provider_rejects_failed_recipient_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases: list[tuple[ExternalNotificationChannel, dict[str, object]]] = [
        (
            ExternalNotificationChannel.EMAIL,
            {
                "header": {"isSuccessful": True, "resultCode": 0},
                "body": {
                    "data": {
                        "requestId": "email-request",
                        "results": [{"resultCode": 7}],
                    }
                },
            },
        ),
        (
            ExternalNotificationChannel.SMS,
            {
                "header": {"isSuccessful": True, "resultCode": 0},
                "body": {
                    "data": {
                        "requestId": "sms-request",
                        "sendResultList": [{"resultCode": 7}],
                    }
                },
            },
        ),
        (
            ExternalNotificationChannel.KAKAO,
            {
                "header": {"isSuccessful": True, "resultCode": 0},
                "message": {
                    "requestId": "kakao-request",
                    "sendResults": [{"resultCode": 7}],
                },
            },
        ),
    ]
    for channel, response in cases:
        with monkeypatch.context() as patch:
            patch.setattr(
                NhnCloudNotificationProvider,
                "_post_json",
                staticmethod(lambda *_, response=response: response),
            )
            destination = (
                "owner@example.com"
                if channel is ExternalNotificationChannel.EMAIL
                else "+821012345678"
            )
            with pytest.raises(ProviderError) as error_info:
                await NhnCloudNotificationProvider(_config()).send(
                    message=_message(channel, destination),
                    idempotency_key=IdempotencyKey("notification:recipient-failure"),
                    timeout_seconds=1,
                )
        assert error_info.value.kind is ProviderErrorKind.INVALID_INPUT
        assert not error_info.value.retryable


@pytest.mark.anyio
async def test_provider_retries_incomplete_success_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: list[dict[str, object]] = [
        {"header": {"isSuccessful": True, "resultCode": 0}},
        {
            "header": {"isSuccessful": True, "resultCode": 0},
            "body": {"data": {"requestId": "empty", "results": []}},
        },
        {
            "header": {"isSuccessful": True, "resultCode": 0},
            "body": {"data": {"requestId": "malformed", "results": ["bad"]}},
        },
        {
            "header": {"isSuccessful": True, "resultCode": 0},
            "body": {"data": {"results": [{"resultCode": 0}]}},
        },
    ]
    for response in responses:
        with monkeypatch.context() as patch:
            patch.setattr(
                NhnCloudNotificationProvider,
                "_post_json",
                staticmethod(lambda *_, response=response: response),
            )
            with pytest.raises(ProviderError) as error_info:
                await NhnCloudNotificationProvider(_config()).send(
                    message=_message(ExternalNotificationChannel.EMAIL, "owner@example.com"),
                    idempotency_key=IdempotencyKey("notification:incomplete"),
                    timeout_seconds=1,
                )
        assert error_info.value.kind is ProviderErrorKind.UNAVAILABLE
        assert error_info.value.retryable


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _: int) -> bytes:
        return self.body


def test_post_json_classifies_http_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    cases = [
        (401, ProviderErrorKind.PERMISSION_DENIED, False),
        (429, ProviderErrorKind.UNAVAILABLE, True),
        (500, ProviderErrorKind.UNAVAILABLE, True),
        (400, ProviderErrorKind.INVALID_INPUT, False),
    ]
    for status_code, kind, retryable in cases:

        def fail(
            *_: object,
            status_code: int = status_code,
            **__: object,
        ) -> None:
            raise HTTPError("https://provider.invalid", status_code, "failed", Message(), None)

        with monkeypatch.context() as patch:
            patch.setattr("app.platform.notification.nhn_cloud.urlopen", fail)
            with pytest.raises(ProviderError) as error_info:
                NhnCloudNotificationProvider._post_json(
                    "https://provider.invalid",
                    {},
                    {},
                    1,
                )
        assert error_info.value.kind is kind
        assert error_info.value.retryable is retryable


def test_post_json_classifies_transport_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    cases: list[tuple[Exception, ProviderErrorKind]] = [
        (TimeoutError(), ProviderErrorKind.DEADLINE_EXCEEDED),
        (URLError("offline"), ProviderErrorKind.UNAVAILABLE),
    ]
    for failure, kind in cases:

        def fail(
            *_: object,
            failure: Exception = failure,
            **__: object,
        ) -> None:
            raise failure

        with monkeypatch.context() as patch:
            patch.setattr("app.platform.notification.nhn_cloud.urlopen", fail)
            with pytest.raises(ProviderError) as error_info:
                NhnCloudNotificationProvider._post_json(
                    "https://provider.invalid",
                    {},
                    {},
                    1,
                )
        assert error_info.value.kind is kind
        assert error_info.value.retryable


def test_post_json_rejects_oversized_or_invalid_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = [
        ("oversized", b"x" * 1_000_001),
        ("invalid-utf8", b"\xff"),
        ("invalid-json", b"{"),
        ("non-object-json", b"[]"),
    ]
    for case_id, body in cases:
        with monkeypatch.context() as patch:
            patch.setattr(
                "app.platform.notification.nhn_cloud.urlopen",
                lambda *_args, body=body, **_kwargs: _Response(body),
            )
            with pytest.raises(ProviderError) as error_info:
                NhnCloudNotificationProvider._post_json(
                    "https://provider.invalid",
                    {},
                    {},
                    1,
                )
        assert error_info.value.kind is ProviderErrorKind.UNAVAILABLE, case_id
        assert error_info.value.retryable, case_id


def test_post_json_returns_object_and_config_repr_hides_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.platform.notification.nhn_cloud.urlopen",
        lambda *_args, **_kwargs: _Response(b'{"header":{"isSuccessful":true}}'),
    )
    assert NhnCloudNotificationProvider._post_json(
        "https://provider.invalid",
        {},
        {},
        1,
    ) == {"header": {"isSuccessful": True}}
    rendered = repr(_config())
    assert "email-secret" not in rendered
    assert "sms-secret" not in rendered
    assert "kakao-secret" not in rendered
