"""NHN Cloud Email, SMS, and Kakao Alimtalk adapter."""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.contracts.notification import (
    ExternalNotificationChannel,
    NotificationSendResult,
    OutboundNotification,
)
from app.contracts.ports import NotificationProviderPort, ProviderError, ProviderErrorKind
from app.contracts.primitives import IdempotencyKey

EMAIL_API = "https://email.api.nhncloudservice.com/email/v2.0/appKeys/{app_key}/sender/mail"
SMS_API = "https://sms.api.nhncloudservice.com/sms/v3.0/appKeys/{app_key}/sender/sms"
LMS_API = "https://sms.api.nhncloudservice.com/sms/v3.0/appKeys/{app_key}/sender/mms"
KAKAO_API = (
    "https://kakaotalk-bizmessage.api.nhncloudservice.com/alimtalk/v2.2/appkeys/{app_key}/messages"
)
SMS_BODY_MAX_BYTES = 90
LMS_BODY_MAX_CHARACTERS = 4_000


@dataclass(frozen=True, slots=True)
class NhnCloudNotificationConfig:
    email_app_key: str
    email_secret_key: str = field(repr=False)
    email_sender_address: str
    email_sender_name: str
    sms_app_key: str
    sms_secret_key: str = field(repr=False)
    sms_sender_number: str
    kakao_app_key: str
    kakao_secret_key: str = field(repr=False)
    kakao_sender_key: str
    kakao_template_code: str


def _provider_message_id(payload: Mapping[str, Any]) -> str | None:
    candidates: list[object] = [payload.get("requestId")]
    body = payload.get("body")
    if isinstance(body, Mapping):
        candidates.append(body.get("requestId"))
        data = body.get("data")
        if isinstance(data, Mapping):
            candidates.append(data.get("requestId"))
    message = payload.get("message")
    if isinstance(message, Mapping):
        candidates.append(message.get("requestId"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate[:255]
    return None


def _single_result_code(
    payload: Mapping[str, Any], channel: ExternalNotificationChannel
) -> int | None:
    container: object
    result_key: str
    if channel is ExternalNotificationChannel.KAKAO:
        container = payload.get("message")
        result_key = "sendResults"
    else:
        body = payload.get("body")
        container = body.get("data") if isinstance(body, Mapping) else None
        result_key = "results" if channel is ExternalNotificationChannel.EMAIL else "sendResultList"
    if not isinstance(container, Mapping):
        return None
    results = container.get(result_key)
    if not isinstance(results, list) or len(results) != 1:
        return None
    result = results[0]
    if not isinstance(result, Mapping):
        return None
    result_code = result.get("resultCode")
    return (
        result_code if isinstance(result_code, int) and not isinstance(result_code, bool) else None
    )


def _domestic_number(destination: str) -> str | None:
    if not destination.startswith("+82"):
        return None
    return f"0{destination[3:]}"


class NhnCloudNotificationProvider(NotificationProviderPort):
    """Call only official transactional send APIs and never expose provider secrets."""

    def __init__(self, config: NhnCloudNotificationConfig) -> None:
        self._config = config

    def _request_for(
        self,
        message: OutboundNotification,
        idempotency_key: IdempotencyKey,
    ) -> tuple[str, dict[str, str], dict[str, object]]:
        common_headers = {"Content-Type": "application/json;charset=UTF-8"}
        grouping_key = str(idempotency_key)
        if message.channel is ExternalNotificationChannel.EMAIL:
            return (
                EMAIL_API.format(app_key=self._config.email_app_key),
                common_headers | {"X-Secret-Key": self._config.email_secret_key},
                {
                    "senderAddress": self._config.email_sender_address,
                    "senderName": self._config.email_sender_name,
                    "title": message.subject,
                    "body": f"{message.body}\n\n{message.deep_link}",
                    "receiverList": [
                        {
                            "receiveMailAddr": message.destination,
                            "receiveType": "MRT0",
                        }
                    ],
                    "senderGroupingKey": grouping_key,
                },
            )
        recipient_number = _domestic_number(message.destination)
        if recipient_number is None:
            raise ProviderError(
                ProviderErrorKind.INVALID_INPUT,
                "NHN SMS and Kakao delivery require a Korean E.164 destination",
                retryable=False,
            )
        if message.channel is ExternalNotificationChannel.SMS:
            body = f"{message.body} {message.deep_link}"
            if len(body) > LMS_BODY_MAX_CHARACTERS:
                raise ProviderError(
                    ProviderErrorKind.INVALID_INPUT,
                    "NHN text message body exceeds the provider limit",
                    retryable=False,
                )
            is_lms = len(body.encode("euc-kr", errors="replace")) > SMS_BODY_MAX_BYTES
            return (
                (LMS_API if is_lms else SMS_API).format(app_key=self._config.sms_app_key),
                common_headers | {"X-Secret-Key": self._config.sms_secret_key},
                {
                    **({"title": message.subject[:120]} if is_lms else {}),
                    "body": body,
                    "sendNo": self._config.sms_sender_number,
                    "senderGroupingKey": grouping_key,
                    "recipientList": [
                        {
                            "recipientNo": recipient_number,
                            "recipientGroupingKey": str(message.notification_id),
                        }
                    ],
                },
            )
        return (
            KAKAO_API.format(app_key=self._config.kakao_app_key),
            common_headers
            | {
                "X-Secret-Key": self._config.kakao_secret_key,
                "X-NC-API-IDEMPOTENCY-KEY": grouping_key,
            },
            {
                "senderKey": self._config.kakao_sender_key,
                "templateCode": self._config.kakao_template_code,
                "senderGroupingKey": grouping_key,
                "recipientList": [
                    {
                        "recipientNo": recipient_number,
                        "templateParameter": {
                            "message": message.body,
                            "deepLink": message.deep_link,
                        },
                        "recipientGroupingKey": str(message.notification_id),
                    }
                ],
            },
        )

    @staticmethod
    def _post_json(
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
            headers=dict(headers),
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw_body = response.read(1_000_001)
        except HTTPError as error:
            if error.code in {401, 403}:
                kind = ProviderErrorKind.PERMISSION_DENIED
                retryable = False
            elif error.code == 429 or error.code >= 500:
                kind = ProviderErrorKind.UNAVAILABLE
                retryable = True
            else:
                kind = ProviderErrorKind.INVALID_INPUT
                retryable = False
            raise ProviderError(
                kind, "NHN notification request failed", retryable=retryable
            ) from None
        except TimeoutError:
            raise ProviderError(
                ProviderErrorKind.DEADLINE_EXCEEDED,
                "NHN notification request timed out",
                retryable=True,
            ) from None
        except URLError:
            raise ProviderError(
                ProviderErrorKind.UNAVAILABLE,
                "NHN notification provider unavailable",
                retryable=True,
            ) from None
        if len(raw_body) > 1_000_000:
            raise ProviderError(
                ProviderErrorKind.UNAVAILABLE,
                "NHN notification response too large",
                retryable=True,
            )
        try:
            decoded = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderError(
                ProviderErrorKind.UNAVAILABLE,
                "NHN notification response invalid",
                retryable=True,
            ) from None
        if not isinstance(decoded, dict):
            raise ProviderError(
                ProviderErrorKind.UNAVAILABLE,
                "NHN notification response invalid",
                retryable=True,
            )
        return cast(dict[str, Any], decoded)

    async def send(
        self,
        *,
        message: OutboundNotification,
        idempotency_key: IdempotencyKey,
        timeout_seconds: float,
    ) -> NotificationSendResult:
        url, headers, payload = self._request_for(message, idempotency_key)
        response = await asyncio.wait_for(
            asyncio.to_thread(
                self._post_json,
                url,
                headers,
                payload,
                timeout_seconds,
            ),
            timeout=timeout_seconds + 0.5,
        )
        header = response.get("header")
        if (
            not isinstance(header, Mapping)
            or header.get("isSuccessful") is not True
            or header.get("resultCode") != 0
        ):
            raise ProviderError(
                ProviderErrorKind.INVALID_INPUT,
                "NHN notification provider rejected the message",
                retryable=False,
            )
        result_code = _single_result_code(response, message.channel)
        if result_code is None:
            raise ProviderError(
                ProviderErrorKind.UNAVAILABLE,
                "NHN notification response missing recipient result",
                retryable=True,
            )
        if result_code != 0:
            raise ProviderError(
                ProviderErrorKind.INVALID_INPUT,
                "NHN notification provider rejected the recipient",
                retryable=False,
            )
        provider_message_id = _provider_message_id(response)
        if provider_message_id is None:
            raise ProviderError(
                ProviderErrorKind.UNAVAILABLE,
                "NHN notification response missing request ID",
                retryable=True,
            )
        return NotificationSendResult(provider_message_id=provider_message_id)
