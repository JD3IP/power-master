"""Telegram notification channel via Bot API."""

from __future__ import annotations

import logging

import httpx

from power_master.config.schema import TelegramChannelConfig
from power_master.notifications.bus import Event
from power_master.notifications.channels.base import NotificationChannel

logger = logging.getLogger(__name__)


class TelegramChannel(NotificationChannel):
    name = "telegram"

    def __init__(self, config: TelegramChannelConfig) -> None:
        self._config = config

    async def send(self, event: Event) -> None:
        title, body = self.format_message(event)
        text = f"<b>{title}</b>\n{body}"
        url = f"https://api.telegram.org/bot{self._config.bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": self._config.chat_id,
                "text": text,
                "parse_mode": "HTML",
            })
            resp.raise_for_status()
        logger.debug("Telegram notification sent: %s", event.name)
