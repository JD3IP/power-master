"""Pushover notification channel."""

from __future__ import annotations

import logging

import httpx

from power_master.config.schema import PushoverChannelConfig
from power_master.notifications.bus import Event
from power_master.notifications.channels.base import NotificationChannel

logger = logging.getLogger(__name__)

SEVERITY_TO_PRIORITY = {
    "info": -1,       # low priority / no alert
    "warning": 0,     # normal priority
    "critical": 1,    # high priority / bypass quiet hours
}


class PushoverChannel(NotificationChannel):
    name = "pushover"

    def __init__(self, config: PushoverChannelConfig) -> None:
        self._config = config

    async def send(self, event: Event) -> None:
        title, body = self.format_message(event)
        priority = SEVERITY_TO_PRIORITY.get(event.severity, 0)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post("https://api.pushover.net/1/messages.json", data={
                "token": self._config.api_token,
                "user": self._config.user_key,
                "title": title,
                "message": body,
                "priority": priority,
            })
            resp.raise_for_status()
        logger.debug("Pushover notification sent: %s", event.name)
