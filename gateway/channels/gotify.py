import asyncio
import re

import httpx

from gateway.channels.base import DeliveryError, Receipt
from gateway.config import Settings
from gateway.schemas import Notification


class GotifyChannel:
    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client = client
        self.url = settings.gotify_url.rstrip("/")
        self.token = settings.gotify_token
        self.timeout = settings.timeout_seconds

    async def send(self, notification: Notification, *, priority: int) -> Receipt:
        # Plain text is deliberate: script output must not become remote markdown images.
        payload = {
            "title": f"[{notification.source}] {notification.title}",
            "message": notification.message,
            "priority": priority,
            "extras": {
                "client::display": {"contentType": "text/plain"},
                "notify-gateway::metadata": {
                    "source": notification.source,
                    "severity": notification.severity,
                    "tags": notification.tags,
                    "dedupe_key": notification.dedupe_key,
                },
            },
        }
        actions = {}
        if notification.action_url:
            actions["click"] = {"url": notification.action_url}
        if notification.image_url:
            actions["bigImageUrl"] = notification.image_url
        if notification.image_url or notification.action_url:
            escaped = re.sub(r"([\\`*_{}\[\]()#+.!>|-])", r"\\\1", notification.message)
            if notification.action_url:
                escaped = "[打开操作页面](" + notification.action_url + ")\n\n" + escaped
            if notification.image_url:
                escaped += "\n\n![图片](" + notification.image_url + ")"
            payload["message"] = escaped
            payload["extras"]["client::display"] = {"contentType": "text/markdown"}
        if actions:
            payload["extras"]["client::notification"] = actions
        try:
            # One deadline bounds the complete request, in addition to httpx phase timeouts.
            async with asyncio.timeout(self.timeout):
                response = await self.client.post(
                    f"{self.url}/message",
                    headers={"X-Gotify-Key": self.token},
                    json=payload,
                )
        except httpx.PoolTimeout:
            raise DeliveryError(503, "channel_busy", "Notification channel is busy") from None
        except (httpx.TimeoutException, TimeoutError):
            raise DeliveryError(
                504, "channel_timeout", "Gotify timed out; delivery outcome is unknown"
            ) from None
        except httpx.RequestError:
            raise DeliveryError(503, "channel_unavailable", "Gotify is unavailable") from None

        if response.status_code in {401, 403}:
            raise DeliveryError(
                502, "channel_auth_failed", "Gotify rejected the gateway credentials"
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise DeliveryError(503, "channel_unavailable", "Gotify is unavailable")
        if not 200 <= response.status_code < 300:
            raise DeliveryError(502, "channel_rejected", "Gotify rejected the notification")
        try:
            data = response.json()
            message_id = data.get("id") if isinstance(data, dict) else None
            if type(message_id) is not int or message_id <= 0:
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            raise DeliveryError(
                502, "invalid_channel_response", "Gotify returned an invalid receipt; delivery outcome is unknown"
            ) from None
        return Receipt(message_id)

    async def ready(self) -> bool:
        # Does not create messages or claim that the application token / phone works.
        try:
            async with asyncio.timeout(self.timeout):
                response = await self.client.get(f"{self.url}/health")
            if response.status_code != 200:
                return False
            data = response.json()
            return isinstance(data, dict) and data.get("health") == "green" and data.get("database") == "green"
        except (httpx.RequestError, TimeoutError, ValueError):
            return False
