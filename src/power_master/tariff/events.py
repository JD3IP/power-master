"""Structured event logging for tariff features (cap, credit, export tier).

This is the backbone (U4-lite) that emits events for:
- Free-window cap consumption, approaching, and exhausted states
- ZEROHERO evening window credit tracking (on-track / at-risk / forfeited)
- Export-tier progress (scaffold; full implementation Phase 2)

Events are logged structurally and can be persisted/queried for dashboard
and alert consumption.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

logger = logging.getLogger(__name__)


@dataclass
class TariffEvent:
    """Structured tariff event."""
    event_type: str  # 'free_window_cap_*', 'credit_*', 'export_tier_*'
    timestamp: datetime
    details: dict  # Event-specific data (cap, threshold, status, etc.)

    def to_dict(self) -> dict:
        """Convert to JSON-serialisable dict."""
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "details": self.details,
        }


class TariffEventEmitter:
    """Emits structured tariff events.

    Maintains in-memory event history for recent queries (e.g., dashboard).
    Can be extended to persist events to DB via the repo.
    """

    def __init__(self, max_history: int = 1000) -> None:
        """Initialize emitter.

        Args:
            max_history: Max events to retain in memory
        """
        self._events: list[TariffEvent] = []
        self._max_history = max_history
        self._repo = None

    async def init_persistence(self, repo) -> None:
        """Wire up optional repository for persistence.

        Args:
            repo: Repository instance (optional)
        """
        self._repo = repo

    def emit_free_window_cap_consumed(
        self,
        cap_name: str,
        kwh_consumed: float,
        cap_kwh_per_day: float,
    ) -> None:
        """Emit event when free-window cap consumption is recorded.

        Args:
            cap_name: Name of the cap (e.g., 'four4free')
            kwh_consumed: Cumulative consumption today
            cap_kwh_per_day: Daily cap limit
        """
        event = TariffEvent(
            event_type="free_window_cap_consumed",
            timestamp=datetime.now(timezone.utc),
            details={
                "cap_name": cap_name,
                "kwh_consumed": kwh_consumed,
                "cap_kwh_per_day": cap_kwh_per_day,
                "percent_used": round(100.0 * kwh_consumed / cap_kwh_per_day, 1),
            },
        )
        self._add_event(event)
        logger.info(
            "FreeWindowCap[%s]: %.2f / %.2f kWh (%.1f%%)",
            cap_name,
            kwh_consumed,
            cap_kwh_per_day,
            event.details["percent_used"],
        )

    def emit_free_window_cap_approaching(
        self,
        cap_name: str,
        kwh_consumed: float,
        cap_kwh_per_day: float,
        threshold_pct: float = 0.80,
    ) -> None:
        """Emit event when consumption reaches 80% (or configured threshold) of cap.

        Args:
            cap_name: Name of the cap
            kwh_consumed: Current consumption
            cap_kwh_per_day: Daily cap limit
            threshold_pct: Threshold as fraction (default 0.80)
        """
        event = TariffEvent(
            event_type="free_window_cap_approaching",
            timestamp=datetime.now(timezone.utc),
            details={
                "cap_name": cap_name,
                "kwh_consumed": kwh_consumed,
                "cap_kwh_per_day": cap_kwh_per_day,
                "percent_used": round(100.0 * kwh_consumed / cap_kwh_per_day, 1),
                "threshold_pct": round(100.0 * threshold_pct, 0),
            },
        )
        self._add_event(event)
        logger.warning(
            "FreeWindowCap[%s] APPROACHING: %.2f / %.2f kWh (%.1f%% of cap)",
            cap_name,
            kwh_consumed,
            cap_kwh_per_day,
            event.details["percent_used"],
        )

    def emit_free_window_cap_exhausted(
        self,
        cap_name: str,
        cap_kwh_per_day: float,
    ) -> None:
        """Emit event when free-window cap is fully consumed.

        Args:
            cap_name: Name of the cap
            cap_kwh_per_day: Daily cap limit (now exhausted)
        """
        event = TariffEvent(
            event_type="free_window_cap_exhausted",
            timestamp=datetime.now(timezone.utc),
            details={
                "cap_name": cap_name,
                "cap_kwh_per_day": cap_kwh_per_day,
            },
        )
        self._add_event(event)
        logger.warning(
            "FreeWindowCap[%s] EXHAUSTED: daily cap of %.2f kWh reached",
            cap_name,
            cap_kwh_per_day,
        )

    # ── ZEROHERO Evening Credit Events (Scaffold for Phase 2) ──

    def emit_credit_window_on_track(
        self,
        credit_name: str,
        window_name: str,
        current_import_kwh: float,
        threshold_kwh: float,
    ) -> None:
        """Emit event when credit window is on track to earn the reward.

        SCAFFOLD: Phase 1 emits the event; full enforcement is Phase 2.

        Args:
            credit_name: Name of credit (e.g., 'zerohero-evening')
            window_name: Window name (e.g., '18:00-20:59')
            current_import_kwh: Grid import in window so far
            threshold_kwh: Import threshold to earn credit
        """
        event = TariffEvent(
            event_type="credit_window_on_track",
            timestamp=datetime.now(timezone.utc),
            details={
                "credit_name": credit_name,
                "window_name": window_name,
                "current_import_kwh": round(current_import_kwh, 3),
                "threshold_kwh": round(threshold_kwh, 3),
                "status": "on_track",
            },
        )
        self._add_event(event)
        logger.info(
            "Credit[%s] ON TRACK: %.3f / %.3f kWh in %s",
            credit_name,
            current_import_kwh,
            threshold_kwh,
            window_name,
        )

    def emit_credit_window_at_risk(
        self,
        credit_name: str,
        window_name: str,
        current_import_kwh: float,
        threshold_kwh: float,
    ) -> None:
        """Emit event when credit window is at risk of not earning the reward.

        SCAFFOLD: Phase 1 emits; full logic is Phase 2.

        Args:
            credit_name: Name of credit
            window_name: Window name
            current_import_kwh: Grid import in window
            threshold_kwh: Import threshold
        """
        event = TariffEvent(
            event_type="credit_window_at_risk",
            timestamp=datetime.now(timezone.utc),
            details={
                "credit_name": credit_name,
                "window_name": window_name,
                "current_import_kwh": round(current_import_kwh, 3),
                "threshold_kwh": round(threshold_kwh, 3),
                "status": "at_risk",
            },
        )
        self._add_event(event)
        logger.warning(
            "Credit[%s] AT RISK: %.3f / %.3f kWh in %s",
            credit_name,
            current_import_kwh,
            threshold_kwh,
            window_name,
        )

    def emit_credit_window_forfeited(
        self,
        credit_name: str,
        window_name: str,
        final_import_kwh: float,
        threshold_kwh: float,
        reward_dollars: float,
    ) -> None:
        """Emit event when credit window closes and reward is forfeited.

        SCAFFOLD: Phase 1 emits; full enforcement and logging is Phase 2.

        Args:
            credit_name: Name of credit
            window_name: Window name
            final_import_kwh: Final grid import in window
            threshold_kwh: Import threshold
            reward_dollars: Forfeited reward
        """
        event = TariffEvent(
            event_type="credit_window_forfeited",
            timestamp=datetime.now(timezone.utc),
            details={
                "credit_name": credit_name,
                "window_name": window_name,
                "final_import_kwh": round(final_import_kwh, 3),
                "threshold_kwh": round(threshold_kwh, 3),
                "forfeited_reward_dollars": round(reward_dollars, 2),
                "status": "forfeited",
            },
        )
        self._add_event(event)
        logger.warning(
            "Credit[%s] FORFEITED: %.3f / %.3f kWh in %s; lost $%.2f",
            credit_name,
            final_import_kwh,
            threshold_kwh,
            window_name,
            reward_dollars,
        )

    # ── Export Tier Progress Events (Scaffold for Phase 2) ──

    def emit_export_tier_progress(
        self,
        tier_name: str,
        current_export_kwh: float,
        tier_cap_kwh_per_day: float,
    ) -> None:
        """Emit event on export tier progress tracking.

        SCAFFOLD: Phase 1 emits the structure; full tiering enforcement is Phase 2.

        Args:
            tier_name: Name of export tier (e.g., 'evening-premium')
            current_export_kwh: Cumulative export in tier today
            tier_cap_kwh_per_day: Tier cap (or None if unlimited)
        """
        if tier_cap_kwh_per_day is None or tier_cap_kwh_per_day <= 0:
            # Unlimited tier
            pct_used = 0.0
        else:
            pct_used = round(100.0 * current_export_kwh / tier_cap_kwh_per_day, 1)

        event = TariffEvent(
            event_type="export_tier_progress",
            timestamp=datetime.now(timezone.utc),
            details={
                "tier_name": tier_name,
                "current_export_kwh": round(current_export_kwh, 2),
                "tier_cap_kwh_per_day": tier_cap_kwh_per_day,
                "percent_used": pct_used,
            },
        )
        self._add_event(event)
        logger.info(
            "ExportTier[%s]: %.2f kWh (%.1f%%)",
            tier_name,
            current_export_kwh,
            pct_used,
        )

    def emit_export_tier_exhausted(
        self,
        tier_name: str,
        tier_cap_kwh_per_day: float,
    ) -> None:
        """Emit event when export tier cap is reached.

        SCAFFOLD.

        Args:
            tier_name: Name of export tier
            tier_cap_kwh_per_day: Tier cap
        """
        event = TariffEvent(
            event_type="export_tier_exhausted",
            timestamp=datetime.now(timezone.utc),
            details={
                "tier_name": tier_name,
                "tier_cap_kwh_per_day": tier_cap_kwh_per_day,
            },
        )
        self._add_event(event)
        logger.warning(
            "ExportTier[%s] EXHAUSTED: tier cap of %.2f kWh reached",
            tier_name,
            tier_cap_kwh_per_day,
        )

    def get_recent_events(self, count: int = 50) -> list[TariffEvent]:
        """Get the most recent N events.

        Args:
            count: Number of recent events to return

        Returns:
            List of recent events (most recent last)
        """
        return self._events[-count:]

    def get_events_by_type(self, event_type: str) -> list[TariffEvent]:
        """Get all events of a specific type.

        Args:
            event_type: Event type string

        Returns:
            List of matching events
        """
        return [e for e in self._events if e.event_type == event_type]

    def _add_event(self, event: TariffEvent) -> None:
        """Add event to history and trim if needed.

        Args:
            event: TariffEvent to add
        """
        self._events.append(event)
        if len(self._events) > self._max_history:
            self._events = self._events[-self._max_history :]
