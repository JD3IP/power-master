"""Seasonal and weekly pattern analysis from historical data."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class HourlyProfile:
    """Average value by hour-of-day."""

    values: dict[int, float] = field(default_factory=dict)  # hour -> avg value

    def get(self, hour: int, default: float = 0.0) -> float:
        return self.values.get(hour, default)


@dataclass
class DayOfWeekProfile:
    """Average values by day-of-week (0=Monday, 6=Sunday) and hour."""

    # day_of_week -> hour -> avg value
    profiles: dict[int, HourlyProfile] = field(default_factory=dict)

    def get(self, day: int, hour: int, default: float = 0.0) -> float:
        if day not in self.profiles:
            return default
        return self.profiles[day].get(hour, default)


def build_hourly_profile(records: list[dict]) -> HourlyProfile:
    """Build an average hourly profile from historical records.

    Args:
        records: List of dicts with 'recorded_at' (ISO string) and 'value' keys.
    """
    sums: dict[int, float] = defaultdict(float)
    counts: dict[int, int] = defaultdict(int)

    for rec in records:
        try:
            dt = datetime.fromisoformat(rec["recorded_at"])
            hour = dt.hour
            sums[hour] += rec["value"]
            counts[hour] += 1
        except (ValueError, KeyError):
            continue

    values = {h: sums[h] / counts[h] for h in sums if counts[h] > 0}
    return HourlyProfile(values=values)


def build_day_of_week_profile(records: list[dict]) -> DayOfWeekProfile:
    """Build profiles grouped by day-of-week and hour.

    Args:
        records: List of dicts with 'recorded_at' (ISO string) and 'value' keys.
    """
    # day -> hour -> list of values
    grouped: dict[int, dict[int, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for rec in records:
        try:
            dt = datetime.fromisoformat(rec["recorded_at"])
            day = dt.weekday()
            hour = dt.hour
            grouped[day][hour].append(rec["value"])
        except (ValueError, KeyError):
            continue

    profiles: dict[int, HourlyProfile] = {}
    for day, hours in grouped.items():
        values = {h: sum(vals) / len(vals) for h, vals in hours.items() if vals}
        profiles[day] = HourlyProfile(values=values)

    return DayOfWeekProfile(profiles=profiles)


def weighted_moving_average(
    values: list[float],
    weights: list[float] | None = None,
) -> float:
    """Compute weighted moving average.

    Recent values get higher weight by default (exponentially decaying).
    """
    if not values:
        return 0.0

    if weights is None:
        n = len(values)
        # Exponential decay: most recent has weight 1.0, oldest has ~0.1
        weights = [0.1 + 0.9 * (i / max(n - 1, 1)) for i in range(n)]

    total = sum(v * w for v, w in zip(values, weights))
    weight_sum = sum(weights)
    return total / weight_sum if weight_sum > 0 else 0.0
