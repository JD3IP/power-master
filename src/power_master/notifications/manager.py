"""Notification manager — incident dedup, channel dispatch, persistent log."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from power_master.config.schema import NotificationsConfig
from power_master.notifications.bus import Action, Event, EventBus, Tier
from power_master.notifications.channels.base import NotificationChannel
from power_master.notifications.narrators import render_plain

logger = logging.getLogger(__name__)

# Cap update re-fires per incident within one hour
MAX_UPDATES_PER_INCIDENT_HOUR = 3


class NotificationManager:
    """Subscribes to the event bus and dispatches to enabled channels.

    Cooldown applies per (incident_id or event_name) so an incident whose
    details evolve (storm window shifting, spike price rising) can emit
    update notifications when Action content materially changes, but is
    rate-limited to MAX_UPDATES_PER_INCIDENT_HOUR.

    Every emission is persisted to `notification_log` for history + debug.
    """

    def __init__(
        self,
        config: NotificationsConfig,
        event_bus: EventBus,
        repo: Any | None = None,
    ) -> None:
        self._config = config
        self._bus = event_bus
        self._repo = repo
        self._channels: list[NotificationChannel] = []
        # Per-key: (last_sent_monotonic, last_action_hash, recent_timestamps)
        self._state: dict[str, dict[str, Any]] = {}
        self._bus.subscribe(self._on_event)
        self._build_channels()

    def set_repo(self, repo: Any) -> None:
        """Wire the repository for persistence after construction."""
        self._repo = repo

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
        return getattr(self._config.events, event_name, None)

    def _incident_key(self, event: Event) -> str:
        return event.incident_id or event.name

    def _should_emit(self, event: Event, event_cfg: Any) -> bool:
        """Decide whether to emit this event given cooldown + update rules.

        Allows "update" re-fires when Action.content_hash() changes, capped
        at MAX_UPDATES_PER_INCIDENT_HOUR.
        """
        key = self._incident_key(event)
        state = self._state.get(key)
        cooldown = event_cfg.cooldown_seconds if event_cfg else 300
        now_mono = time.monotonic()

        if state is None:
            return True

        last_sent = state["last_sent"]
        in_cooldown = (now_mono - last_sent) < cooldown
        if not in_cooldown:
            return True

        # Inside cooldown — allow only if Action content materially changed
        new_hash = event.action.content_hash() if event.action else None
        if new_hash is None or new_hash == state.get("last_hash"):
            return False

        # Rate-limit update re-fires to MAX_UPDATES_PER_INCIDENT_HOUR within 1h
        recent = [t for t in state.get("recent_updates", []) if (now_mono - t) < 3600]
        if len(recent) >= MAX_UPDATES_PER_INCIDENT_HOUR:
            logger.debug("Incident %s update rate-limited", key)
            return False
        return True

    def _remember(self, event: Event, now_mono: float) -> None:
        key = self._incident_key(event)
        state = self._state.setdefault(key, {"recent_updates": []})
        state["last_sent"] = now_mono
        state["last_hash"] = event.action.content_hash() if event.action else None
        state.setdefault("recent_updates", []).append(now_mono)
        # Trim to 1h
        state["recent_updates"] = [t for t in state["recent_updates"] if (now_mono - t) < 3600]

    async def _on_event(self, event: Event) -> None:
        """Handle an event from the bus."""
        event_cfg = self._get_event_config(event.name)
        if event_cfg is not None:
            if not event_cfg.enabled:
                # Still persist to the log so history is complete even if
                # channels are silent for this event type.
                await self._persist(event, channels_sent="")
                return
            event.severity = event_cfg.severity

        if not self._should_emit(event, event_cfg):
            logger.debug("Notification suppressed (cooldown): %s", event.name)
            return

        # Build the channel-facing message.  If the event carries an Action,
        # the rendered text replaces the bare `message` for channel delivery.
        rendered_message = render_plain(event.title, event.action, event.message)
        channels_sent: list[str] = []
        if self._config.enabled and self._channels:
            # Temporarily swap rendered text onto the event for the channel
            # send.  Channels read event.message directly today.
            original_message = event.message
            event.message = rendered_message
            try:
                for channel in self._channels:
                    try:
                        await channel.send(event)
                        channels_sent.append(channel.name)
                    except Exception:
                        logger.warning(
                            "Failed to send %s via %s", event.name, channel.name,
                            exc_info=True,
                        )
            finally:
                event.message = original_message

        self._remember(event, time.monotonic())
        await self._persist(event, channels_sent=",".join(channels_sent),
                            rendered_message=rendered_message)

    async def _persist(
        self, event: Event, *, channels_sent: str = "",
        rendered_message: str | None = None,
    ) -> None:
        """Append the event to notification_log (if repo available)."""
        if self._repo is None:
            return
        try:
            action_json = json.dumps(event.action.as_dict()) if event.action else None
            await self._repo.store_notification(
                emitted_at=event.timestamp.astimezone(timezone.utc).isoformat(),
                event_name=event.name,
                severity=event.severity,
                tier=event.tier.value if isinstance(event.tier, Tier) else str(event.tier),
                title=event.title,
                message=rendered_message if rendered_message is not None else event.message,
                action_json=action_json,
                incident_id=event.incident_id,
                correlation_id=event.correlation_id,
                channels_sent=channels_sent,
            )
        except Exception:
            logger.debug("Failed to persist notification", exc_info=True)

    async def send_test(self, channel_name: str) -> dict[str, str]:
        """Send a test notification to a specific channel. Returns status dict."""
        test_event = Event(
            name="test",
            severity="info",
            title="Test Notification",
            message="This is a test notification from Power Master.",
            tier=Tier.INFORMATIONAL,
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
    """Logging handler that forwards log records to the notification event bus.

    min_level gates at the handler so "silence routine log_error" is separate
    from "log_error event disabled entirely" — routine noise is filtered here,
    critical-level failures still reach the bus.
    """

    def __init__(self, event_bus: EventBus, min_level: str = "CRITICAL") -> None:
        super().__init__()
        self._bus = event_bus
        self.setLevel(getattr(logging, min_level.upper(), logging.CRITICAL))

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("power_master.notifications"):
            return
        event = Event(
            name="log_error",
            severity="warning" if record.levelno < logging.CRITICAL else "critical",
            title=f"Log {record.levelname}: {record.name}",
            message=self.format(record) if self.formatter else record.getMessage(),
            data={"logger": record.name, "level": record.levelname},
            tier=Tier.ATTENTION if record.levelno >= logging.CRITICAL else Tier.INFORMATIONAL,
        )
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._bus.publish(event))
        except RuntimeError:
            pass
