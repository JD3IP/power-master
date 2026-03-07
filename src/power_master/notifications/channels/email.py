"""Email notification channel via SMTP."""

from __future__ import annotations

import logging
from email.message import EmailMessage

from power_master.config.schema import EmailChannelConfig
from power_master.notifications.bus import Event
from power_master.notifications.channels.base import NotificationChannel

logger = logging.getLogger(__name__)


class EmailChannel(NotificationChannel):
    name = "email"

    def __init__(self, config: EmailChannelConfig) -> None:
        self._config = config

    async def send(self, event: Event) -> None:
        title, body = self.format_message(event)
        msg = EmailMessage()
        msg["Subject"] = f"[Power Master] {title}"
        msg["From"] = self._config.from_address
        msg["To"] = self._config.to_address
        msg.set_content(body)

        import aiosmtplib

        await aiosmtplib.send(
            msg,
            hostname=self._config.smtp_host,
            port=self._config.smtp_port,
            username=self._config.smtp_user or None,
            password=self._config.smtp_password or None,
            use_tls=self._config.use_tls,
            timeout=15,
        )
        logger.debug("Email notification sent: %s", event.name)
