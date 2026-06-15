"""Daily credit tracker for conditional daily rewards.

Tracks whether the evening low-import window (e.g., ZEROHERO 18:00-20:59)
stays under threshold and the resulting $ earned or forfeited. Resets per
local day; persists to SQLite kv_store so mid-day restarts don't lose state.

Mirrors the FreeWindowCapTracker pattern for consistency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# KV store key for persisting daily credit state
KV_DAILY_CREDIT_STATE_KEY = "daily_credit_state"


@dataclass
class DailyCreditState:
    """Credit tracking state for a single day."""
    local_date_str: str  # YYYY-MM-DD in the configured timezone
    credit_name: str  # e.g., 'zerohero-evening'
    window_name: str  # e.g., '18:00-20:59'
    # Running total of grid import (in kWh) during the credit window today
    in_window_import_kwh: float
    # Threshold (in kWh) to earn the credit (e.g., 0.09 for 0.03 kWh/hour × 3 hours)
    threshold_kwh: float
    # Credit reward in dollars if earned
    reward_dollars: float
    # Status: 'on_track' | 'at_risk' | 'forfeited'
    status: str
    # ISO timestamp when last updated
    timestamp: str

    def to_dict(self) -> dict:
        """Convert to JSON-serialisable dict for kv_store."""
        return {
            "local_date_str": self.local_date_str,
            "credit_name": self.credit_name,
            "window_name": self.window_name,
            "in_window_import_kwh": self.in_window_import_kwh,
            "threshold_kwh": self.threshold_kwh,
            "reward_dollars": self.reward_dollars,
            "status": self.status,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DailyCreditState:
        """Restore from dict loaded from kv_store."""
        return cls(
            local_date_str=d.get("local_date_str", ""),
            credit_name=d.get("credit_name", ""),
            window_name=d.get("window_name", ""),
            in_window_import_kwh=float(d.get("in_window_import_kwh", 0.0)),
            threshold_kwh=float(d.get("threshold_kwh", 0.0)),
            reward_dollars=float(d.get("reward_dollars", 0.0)),
            status=d.get("status", "on_track"),
            timestamp=d.get("timestamp", ""),
        )


class DailyCreditTracker:
    """Tracks daily credit (e.g., ZEROHERO $1/day) with persistence.

    Per the tariff spec (§6), the daily credit rewards staying under an
    import threshold during a specific window (e.g., 18:00-20:59 grid import
    ≤ 0.03 kWh/hour → +$1/day). This tracker records in-window import and
    determines if the credit is earned or forfeited, resets per local day,
    and persists state.
    """

    def __init__(
        self,
        timezone_name: str,
        credit_name: str,
        # e.g., 'zerohero-evening'
        window_name: str,
        # e.g., '18:00-20:59'
        threshold_kwh: float,
        # e.g., 0.09 (0.03 kWh/hour × 3 hours)
        reward_dollars: float,
        # e.g., 1.00
        repo=None,
    ) -> None:
        """Initialize the daily credit tracker.

        Args:
            timezone_name: IANA timezone name (e.g., 'Australia/Brisbane')
            credit_name: Name of credit (e.g., 'zerohero-evening')
            window_name: Time window identifier (e.g., '18:00-20:59')
            threshold_kwh: Max kWh import in window to earn credit
            reward_dollars: Dollar reward if earned
            repo: Optional Repository instance (aiosqlite); if provided,
                  tracker will load/save state from kv_store
        """
        self._tz = ZoneInfo(timezone_name)
        self._credit_name = credit_name
        self._window_name = window_name
        self._threshold_kwh = threshold_kwh
        self._reward_dollars = reward_dollars
        self._repo = repo

        # In-memory state
        self._state: DailyCreditState | None = None

    async def init_persistence(self, repo) -> None:
        """Load persisted credit state from kv_store.

        Call this after the repository is initialized.

        Args:
            repo: Repository instance
        """
        self._repo = repo
        if repo is None:
            logger.warning("DailyCreditTracker: no repository provided; persistence disabled")
            return

        try:
            saved = await repo.kv_get(KV_DAILY_CREDIT_STATE_KEY)
            if saved:
                self._state = DailyCreditState.from_dict(saved)
                # Check if we're still in the same local day
                today_local_date = self._get_local_date_str(datetime.now(self._tz))
                if self._state.local_date_str != today_local_date:
                    logger.info(
                        "DailyCreditTracker: day boundary crossed (%s -> %s); resetting state",
                        self._state.local_date_str,
                        today_local_date,
                    )
                    self._state = None
                else:
                    logger.info(
                        "DailyCreditTracker: restored state for %s: "
                        "%.3f / %.3f kWh, status=%s",
                        self._state.local_date_str,
                        self._state.in_window_import_kwh,
                        self._state.threshold_kwh,
                        self._state.status,
                    )
            else:
                logger.info("DailyCreditTracker: no persisted state; starting fresh")
        except Exception as e:
            logger.warning("DailyCreditTracker: failed to restore persisted state: %s", e)

    def get_import_total(self) -> float:
        """Return total grid import in the credit window today (kWh).

        Returns:
            Cumulative kWh imported during window (0 if no state yet)
        """
        if self._state is None:
            return 0.0
        return self._state.in_window_import_kwh

    def get_status(self) -> str:
        """Return current credit status.

        Returns:
            'on_track' | 'at_risk' | 'forfeited' | None (no state yet)
        """
        if self._state is None:
            return None
        return self._state.status

    def is_credit_earned(self) -> bool:
        """Check if credit is still on track to be earned.

        Returns:
            True if in_window_import <= threshold_kwh
        """
        if self._state is None:
            return True  # Default: assume on track until proven otherwise
        return self._state.in_window_import_kwh <= self._threshold_kwh

    async def record_in_window_import(self, energy_wh: int) -> dict:
        """Record grid import during the credit window.

        Returns a dict with:
        - 'credit_name': name of credit
        - 'import_total_kwh': cumulative import in window so far
        - 'threshold_kwh': threshold to earn credit
        - 'status': 'on_track' | 'at_risk' | 'forfeited'
        - 'earned_dollars': 0.0 if forfeited, reward if still earning

        Args:
            energy_wh: Energy imported in Wh

        Returns:
            Dict with tracking details
        """
        if energy_wh <= 0:
            return {
                "credit_name": self._credit_name,
                "import_total_kwh": 0.0,
                "threshold_kwh": self._threshold_kwh,
                "status": "on_track",
                "earned_dollars": self._reward_dollars,
            }

        now = datetime.now(self._tz)
        today_local_date = self._get_local_date_str(now)

        # Initialize state if fresh day or first call
        if self._state is None or self._state.local_date_str != today_local_date:
            self._state = DailyCreditState(
                local_date_str=today_local_date,
                credit_name=self._credit_name,
                window_name=self._window_name,
                in_window_import_kwh=0.0,
                threshold_kwh=self._threshold_kwh,
                reward_dollars=self._reward_dollars,
                status="on_track",
                timestamp=datetime.now(self._tz).isoformat(),
            )
            logger.info(
                "DailyCreditTracker: starting fresh for %s (%s)",
                today_local_date,
                self._credit_name,
            )

        energy_kwh = energy_wh / 1000.0
        self._state.in_window_import_kwh += energy_kwh
        self._state.timestamp = datetime.now(self._tz).isoformat()

        # Update status based on import vs threshold
        # Note: >= threshold (not >) means exactly at or over threshold forfeits credit
        if self._state.in_window_import_kwh >= self._threshold_kwh:
            # At or above threshold; credit is forfeited
            if self._state.status != "forfeited":
                logger.warning(
                    "DailyCreditTracker[%s]: FORFEITED — %.3f / %.3f kWh in window",
                    self._credit_name,
                    self._state.in_window_import_kwh,
                    self._threshold_kwh,
                )
            self._state.status = "forfeited"
        elif (
            self._state.in_window_import_kwh > self._threshold_kwh * 0.8
        ):
            # Approaching threshold (80%)
            if self._state.status == "on_track":
                logger.warning(
                    "DailyCreditTracker[%s]: AT RISK — %.3f / %.3f kWh in window",
                    self._credit_name,
                    self._state.in_window_import_kwh,
                    self._threshold_kwh,
                )
            self._state.status = "at_risk"
        else:
            # Well below threshold
            self._state.status = "on_track"

        # Persist to kv_store
        if self._repo:
            try:
                await self._repo.kv_set(KV_DAILY_CREDIT_STATE_KEY, self._state.to_dict())
            except Exception as e:
                logger.warning("DailyCreditTracker: failed to persist state: %s", e)

        earned = (
            0.0
            if self._state.status == "forfeited"
            else self._reward_dollars
        )

        return {
            "credit_name": self._credit_name,
            "import_total_kwh": round(self._state.in_window_import_kwh, 3),
            "threshold_kwh": self._threshold_kwh,
            "status": self._state.status,
            "earned_dollars": round(earned, 2),
        }

    @staticmethod
    def _get_local_date_str(dt: datetime) -> str:
        """Return YYYY-MM-DD string for a datetime in its local timezone."""
        return dt.date().isoformat()

    async def reset_for_testing(self) -> None:
        """Reset state (for testing only)."""
        self._state = None
        if self._repo:
            try:
                await self._repo.kv_set(KV_DAILY_CREDIT_STATE_KEY, None)
            except Exception:
                pass
