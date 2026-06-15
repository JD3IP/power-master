"""Free-window daily cap tracker with persistence and reset handling.

Tracks cumulative free-window kWh consumed in the current LOCAL day,
persisting to SQLite kv_store so mid-day restarts don't lose the count.
Resets automatically at local midnight in the configured timezone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# KV store key for persisting cap state
KV_FREE_WINDOW_CAP_KEY = "free_window_cap_state"


@dataclass
class FreeWindowCapState:
    """State of free-window cap consumption for a single day."""
    local_date_str: str  # YYYY-MM-DD in the configured timezone
    kwh_consumed: float  # Cumulative kWh consumed today
    timestamp: str  # ISO timestamp when last updated

    def to_dict(self) -> dict:
        """Convert to JSON-serialisable dict for kv_store."""
        return {
            "local_date_str": self.local_date_str,
            "kwh_consumed": self.kwh_consumed,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> FreeWindowCapState:
        """Restore from dict loaded from kv_store."""
        return cls(
            local_date_str=d.get("local_date_str", ""),
            kwh_consumed=float(d.get("kwh_consumed", 0.0)),
            timestamp=d.get("timestamp", ""),
        )


class FreeWindowCapTracker:
    """Tracks free-window daily cap consumption with persistence.

    Per the tariff spec (§3.4), the free allowance is 50 kWh/day,
    resetting at local midnight in the configured timezone.
    """

    def __init__(
        self,
        timezone_name: str,
        cap_kwh_per_day: float,
        repo=None,  # Optional Repository instance for persistence
    ) -> None:
        """Initialize the cap tracker.

        Args:
            timezone_name: IANA timezone name (e.g., 'Australia/Brisbane')
            cap_kwh_per_day: Daily cap in kWh (typically 50)
            repo: Optional Repository instance (aiosqlite); if provided,
                  tracker will load/save state from kv_store
        """
        self._tz = ZoneInfo(timezone_name)
        self._cap_kwh_per_day = cap_kwh_per_day
        self._repo = repo

        # In-memory state
        self._state: FreeWindowCapState | None = None

    async def init_persistence(self, repo) -> None:
        """Load persisted cap state from kv_store (if available).

        Call this after the repository is initialized.

        Args:
            repo: Repository instance
        """
        self._repo = repo
        if repo is None:
            logger.warning("FreeWindowCapTracker: no repository provided; persistence disabled")
            return

        try:
            saved = await repo.kv_get(KV_FREE_WINDOW_CAP_KEY)
            if saved:
                self._state = FreeWindowCapState.from_dict(saved)
                # Check if we're still in the same local day
                today_local_date = self._get_local_date_str(datetime.now(self._tz))
                if self._state.local_date_str != today_local_date:
                    logger.info(
                        "FreeWindowCapTracker: day boundary crossed (%s -> %s); resetting cap",
                        self._state.local_date_str,
                        today_local_date,
                    )
                    self._state = None
                else:
                    logger.info(
                        "FreeWindowCapTracker: restored state for %s: %.2f kWh consumed",
                        self._state.local_date_str,
                        self._state.kwh_consumed,
                    )
            else:
                logger.info("FreeWindowCapTracker: no persisted state; starting fresh")
        except Exception as e:
            logger.warning("FreeWindowCapTracker: failed to restore persisted state: %s", e)

    def get_remaining_cap(self) -> float:
        """Return remaining daily cap in kWh.

        Returns max(0, cap_kwh_per_day - consumed_today).
        If no state yet (fresh day), returns the full cap.
        """
        if self._state is None:
            return self._cap_kwh_per_day

        consumed = self._state.kwh_consumed
        remaining = max(0.0, self._cap_kwh_per_day - consumed)
        return remaining

    def get_consumed_today(self) -> float:
        """Return cumulative kWh consumed today.

        Returns 0 if starting fresh (no state yet).
        """
        if self._state is None:
            return 0.0
        return self._state.kwh_consumed

    async def increment(self, kwh: float) -> None:
        """Increment consumption and persist to kv_store.

        Args:
            kwh: Energy consumed (in kWh) during this tick
        """
        if kwh <= 0:
            return

        now = datetime.now(self._tz)
        today_local_date = self._get_local_date_str(now)

        # Initialize state if fresh day or first call
        if self._state is None or self._state.local_date_str != today_local_date:
            self._state = FreeWindowCapState(
                local_date_str=today_local_date,
                kwh_consumed=0.0,
                timestamp=datetime.now(self._tz).isoformat(),
            )
            logger.info(
                "FreeWindowCapTracker: starting fresh for %s",
                today_local_date,
            )

        # Increment and clamp to cap
        self._state.kwh_consumed = min(
            self._state.kwh_consumed + kwh,
            self._cap_kwh_per_day,
        )
        self._state.timestamp = datetime.now(self._tz).isoformat()

        # Persist to kv_store
        if self._repo:
            try:
                await self._repo.kv_set(KV_FREE_WINDOW_CAP_KEY, self._state.to_dict())
            except Exception as e:
                logger.warning("FreeWindowCapTracker: failed to persist state: %s", e)

    def is_cap_approaching(self, threshold_pct: float = 0.80) -> bool:
        """Check if consumption is at or above threshold % of cap.

        Args:
            threshold_pct: Threshold as fraction (default 0.80 = 80%)

        Returns:
            True if consumed >= cap * threshold_pct
        """
        if self._state is None:
            return False
        return self._state.kwh_consumed >= (self._cap_kwh_per_day * threshold_pct)

    def is_cap_exhausted(self) -> bool:
        """Check if daily cap is fully consumed.

        Returns:
            True if consumed >= cap_kwh_per_day
        """
        if self._state is None:
            return False
        return self._state.kwh_consumed >= self._cap_kwh_per_day

    @staticmethod
    def _get_local_date_str(dt: datetime) -> str:
        """Return YYYY-MM-DD string for a datetime in its local timezone."""
        return dt.date().isoformat()

    async def reset_for_testing(self) -> None:
        """Reset state (for testing only)."""
        self._state = None
        if self._repo:
            try:
                await self._repo.kv_set(KV_FREE_WINDOW_CAP_KEY, None)
            except Exception:
                pass
