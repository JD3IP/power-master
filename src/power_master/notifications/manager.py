"""Notification manager — cooldowns, channel dispatch, and log handler."""

from __future__ import annotations

import logging
import time
from typing import Any

from power_master.config.schema import NotificationsConfig
from power_master.notifications.bus import Event, EventBus
from power_master.notifications.channels.base import NotificationChannel

logger = logging.getLogger(__name__)


class NotificationManager:
    """Subscribes to the event bus and dispatches to enabled channels.

    Enforces per-event cooldowns and respects event enable/disable config.
    """

    def __init__(self, config: NotificationsConfig, event_bus: EventBus) -> None:
        self._config = config
        self._bus = event_bus
        self._channels: list[NotificationChannel] = []
        self._last_sent: dict[str, float] = {}  # event_name → monotonic time
        self._bus.subscribe(self._on_event)
        self._build_channels()

    def _build_channels(self) -> None:
        self._channels.clear()
        ch = self._config.channels

        if ch.telegram.enabled and ch.telegram.bot_token and ch.telegram.chat_id:
            from power_master.notifications.channels.telegram import TelegramChannel
            self._channels.append(TelegramChannel(ch.telegram))

        if ch.email.enabled and ch.email.smtp_host and ch.email.to_address:
            from power_master.notifications.channels.email import EmailChannel
            self._channels.append(EmailChannel(ch.email))

        if ch.pushover.enabled and ch.pushover.api_token and ch.pushover.user_key:
            from power_master.notifications.channels.pushover import PushoverChannel
            self._channels.append(PushoverChannel(ch.pushover))

        if ch.ntfy.enabled and ch.ntfy.topic:
            from power_master.notifications.channels.ntfy import NtfyChannel
            self._channels.append(NtfyChannel(ch.ntfy))

        if ch.webhook.enabled and ch.webhook.url:
            from power_master.notifications.channels.webhook import WebhookChannel
            self._channels.append(WebhookChannel(ch.webhook))

        if self._channels:
            logger.info(
                "Notification channels active: %s",
                ", ".join(c.name for c in self._channels),
            )

    def reload(self, config: NotificationsConfig) -> None:
        """Rebuild channels after config change."""
        self._config = config
        self._build_channels()

    def _get_event_config(self, event_name: str) -> Any:
        """Look up per-event config from NotificationEventsConfig."""
        return getattr(self._config.events, event_name, None)

    def _is_cooled_down(self, event_name: str, cooldown_seconds: int) -> bool:
        """Return True if the event is still in cooldown."""
        last = self._last_sent.get(event_name)
        if last is None:
            return False
        return (time.monotonic() - last) < cooldown_seconds

    async def _on_event(self, event: Event) -> None:
        """Handle an event from the bus."""
        if not self._config.enabled:
            return
        if not self._channels:
            return

        event_cfg = self._get_event_config(event.name)
        if event_cfg is None:
            # Unknown event type — still send it if channels exist
            pass
        elif not event_cfg.enabled:
            return
        elif self._is_cooled_down(event.name, event_cfg.cooldown_seconds):
            logger.debug("Notification suppressed (cooldown): %s", event.name)
            return

        # Override severity from config if available
        if event_cfg is not None:
            event.severity = event_cfg.severity

        for channel in self._channels:
            try:
                await channel.send(event)
            except Exception:
                logger.warning(
                    "Failed to send %s via %s", event.name, channel.name,
                    exc_info=True,
                )

        self._last_sent[event.name] = time.monotonic()

    async def send_test(self, channel_name: str) -> dict[str, str]:
        """Send a test notification to a specific channel. Returns status dict."""
        test_event = Event(
            name="test",
            severity="info",
            title="Test Notification",
            message="This is a test notification from Power Master.",
        )
        for channel in self._channels:
            if channel.name == channel_name:
                try:
                    await channel.send(test_event)
                    return {"status": "ok", "channel": channel_name}
                except Exception as e:
                    return {"status": "error", "channel": channel_name, "error": str(e)}

        # Channel not in active list — try to build it temporarily for test
        result = await self._test_unconfigured_channel(channel_name, test_event)
        return result

    async def _test_unconfigured_channel(
        self, channel_name: str, event: Event,
    ) -> dict[str, str]:
        """Build a channel temporarily for testing even if not in active list."""
        ch = self._config.channels
        channel: NotificationChannel | None = None
        try:
            if channel_name == "telegram" and ch.telegram.bot_token:
                from power_master.notifications.channels.telegram import TelegramChannel
                channel = TelegramChannel(ch.telegram)
            elif channel_name == "email" and ch.email.smtp_host:
                from power_master.notifications.channels.email import EmailChannel
                channel = EmailChannel(ch.email)
            elif channel_name == "pushover" and ch.pushover.api_token:
                from power_master.notifications.channels.pushover import PushoverChannel
                channel = PushoverChannel(ch.pushover)
            elif channel_name == "ntfy" and ch.ntfy.topic:
                from power_master.notifications.channels.ntfy import NtfyChannel
                channel = NtfyChannel(ch.ntfy)
            elif channel_name == "webhook" and ch.webhook.url:
                from power_master.notifications.channels.webhook import WebhookChannel
                channel = WebhookChannel(ch.webhook)
        except Exception as e:
            return {"status": "error", "channel": channel_name, "error": str(e)}

        if channel is None:
            return {
                "status": "error",
                "channel": channel_name,
                "error": "Channel not configured (missing required fields)",
            }

        try:
            await channel.send(event)
            return {"status": "ok", "channel": channel_name}
        except Exception as e:
            return {"status": "error", "channel": channel_name, "error": str(e)}


class NotificationLogHandler(logging.Handler):
    """Logging handler that forwards log records to the notification event bus."""

    def __init__(self, event_bus: EventBus, min_level: str = "ERROR") -> None:
        super().__init__()
        self._bus = event_bus
        self.setLevel(getattr(logging, min_level.upper(), logging.ERROR))

    def emit(self, record: logging.LogRecord) -> None:
        # Skip notifications from the notification system itself to avoid loops
        if record.name.startswith("power_master.notifications"):
            return
        event = Event(
            name="log_error",
            severity="warning" if record.levelno < logging.CRITICAL else "critical",
            title=f"Log {record.levelname}: {record.name}",
            message=self.format(record) if self.formatter else record.getMessage(),
            data={"logger": record.name, "level": record.levelname},
        )
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._bus.publish(event))
        except RuntimeError:
            pass  # No event loop — skip
