"""Load and solar prediction from historical data.

Uses day-of-week + time-of-day weighted moving averages for load prediction,
and historical production adjusted by cloud cover for solar fallback.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from power_master.db.repository import Repository
from power_master.history.patterns import (
    DayOfWeekProfile,
    build_day_of_week_profile,
    build_hourly_profile,
    weighted_moving_average,
)
from power_master.timezone_utils import resolve_timezone

logger = logging.getLogger(__name__)


class LoadPredictor:
    """Predicts future load consumption from historical patterns."""

    def __init__(self, repo: Repository, timezone_name: str = "UTC") -> None:
        self._repo = repo
        self._profile: DayOfWeekProfile | None = None
        self._last_rebuild: datetime | None = None
        self._timezone_name = timezone_name
        self._tz = resolve_timezone(timezone_name)

    async def rebuild_profile(self, lookback_days: int = 28) -> None:
        """Rebuild load profile from recent history."""
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=lookback_days)).isoformat()
        records = await self._repo.get_historical("load_w", start, now.isoformat())

        if len(records) < 48:  # Need at least 1 day of data
            logger.warning(
                "Insufficient load history for prediction (%d records)", len(records)
            )
            return

        local_records: list[dict] = []
        for rec in records:
            try:
                dt = datetime.fromisoformat(rec["recorded_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt_local = dt.astimezone(self._tz)
                local_records.append(
                    {"recorded_at": dt_local.isoformat(), "value": rec["value"]}
                )
            except Exception:
                continue

        self._profile = build_day_of_week_profile(local_records)
        self._last_rebuild = now
        logger.info(
            "Load profile rebuilt from %d records (lookback: %d days, tz=%s)",
            len(local_records), lookback_days, self._timezone_name,
        )

    def predict(self, dt: datetime, default_w: float = 500.0) -> float:
        """Predict load at a given datetime.

        Returns watts based on day-of-week and hour-of-day pattern.
        Falls back to default if no profile is available.
        """
        if self._profile is None:
            return default_w
        dt_local = dt.astimezone(self._tz) if dt.tzinfo else dt.replace(tzinfo=timezone.utc).astimezone(self._tz)
        return self._profile.get(dt_local.weekday(), dt_local.hour, default_w)

    def predict_range(
        self,
        start: datetime,
        hours: int = 48,
        slot_minutes: int = 30,
        default_w: float = 500.0,
    ) -> list[tuple[datetime, float]]:
        """Predict load for a range of time slots.

        Returns list of (slot_start, predicted_load_w) tuples.
        """
        slots = []
        current = start
        for _ in range(hours * 60 // slot_minutes):
            load = self.predict(current, default_w)
            slots.append((current, load))
            current += timedelta(minutes=slot_minutes)
        return slots


class SolarPredictor:
    """Predicts solar production from historical data + cloud cover."""

    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        self._profile: DayOfWeekProfile | None = None
        self._last_rebuild: datetime | None = None

    async def rebuild_profile(self, lookback_days: int = 28) -> None:
        """Rebuild solar production profile from recent history."""
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=lookback_days)).isoformat()
        records = await self._repo.get_historical("solar_w", start, now.isoformat())

        if len(records) < 48:
            logger.warning(
                "Insufficient solar history for prediction (%d records)", len(records)
            )
            return

        self._profile = build_day_of_week_profile(records)
        self._last_rebuild = now
        logger.info(
            "Solar profile rebuilt from %d records (lookback: %d days)",
            len(records), lookback_days,
        )

    def predict(
        self,
        dt: datetime,
        cloud_cover_pct: float | None = None,
    ) -> float:
        """Predict solar production at a given datetime.

        If cloud_cover is provided, adjusts the historical average.
        """
        if self._profile is None:
            return 0.0

        base = self._profile.get(dt.weekday(), dt.hour, 0.0)

        if cloud_cover_pct is not None:
            # Assume historical average is at ~40% cloud cover baseline
            baseline_cloud = 40.0
            adjustment = 1.0 + (baseline_cloud - cloud_cover_pct) / 100.0 * 0.75
            return max(0.0, base * adjustment)

        return max(0.0, base)

    def predict_range(
        self,
        start: datetime,
        hours: int = 48,
        slot_minutes: int = 30,
        cloud_by_hour: dict[int, float] | None = None,
    ) -> list[tuple[datetime, float]]:
        """Predict solar for a range of time slots."""
        slots = []
        current = start
        for _ in range(hours * 60 // slot_minutes):
            cloud = None
            if cloud_by_hour:
                cloud = cloud_by_hour.get(current.hour)
            solar = self.predict(current, cloud)
            slots.append((current, solar))
            current += timedelta(minutes=slot_minutes)
        return slots
