"""Generic webhook notification channel."""

from __future__ import annotations

import logging

import httpx

from power_master.config.schema import WebhookChannelConfig
from power_master.notifications.bus import Event
from power_master.notifications.channels.base import NotificationChannel

logger = logging.getLogger(__name__)


class WebhookChannel(NotificationChannel):
    name = "webhook"

    def __init__(self, config: WebhookChannelConfig) -> None:
        self._config = config

    async def send(self, event: Event) -> None:
        payload = {
            "event": event.name,
            "severity": event.severity,
            "title": event.title,
            "message": event.message,
            "timestamp": event.timestamp.isoformat(),
            "data": event.data,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.request(
                method=self._config.method,
                url=self._config.url,
                json=payload,
                headers=self._config.headers,
            )
            resp.raise_for_status()
        logger.debug("Webhook notification sent: %s", event.name)
