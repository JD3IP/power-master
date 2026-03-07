"""ntfy.sh notification channel."""

from __future__ import annotations

import logging

import httpx

from power_master.config.schema import NtfyChannelConfig
from power_master.notifications.bus import Event
from power_master.notifications.channels.base import NotificationChannel

logger = logging.getLogger(__name__)

SEVERITY_TO_PRIORITY = {
    "info": "low",
    "warning": "default",
    "critical": "urgent",
}


class NtfyChannel(NotificationChannel):
    name = "ntfy"

    def __init__(self, config: NtfyChannelConfig) -> None:
        self._config = config

    async def send(self, event: Event) -> None:
        title, body = self.format_message(event)
        url = f"{self._config.server_url.rstrip('/')}/{self._config.topic}"
        headers: dict[str, str] = {
            "Title": title,
            "Priority": SEVERITY_TO_PRIORITY.get(event.severity, "default"),
            "Tags": f"power_master,{event.severity}",
        }
        if self._config.token:
            headers["Authorization"] = f"Bearer {self._config.token}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, content=body, headers=headers)
            resp.raise_for_status()
        logger.debug("ntfy notification sent: %s", event.name)
