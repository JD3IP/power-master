"""Solar production estimation — Solcast primary, historical fallback."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from power_master.forecast.base import SolarForecast, SolarForecastSlot

logger = logging.getLogger(__name__)


def estimate_from_cloud_cover(
    cloud_cover_pct: float,
    peak_capacity_w: float,
    hour: int,
) -> float:
    """Rough solar estimate from cloud cover when Solcast is unavailable.

    Uses a conservative bell curve from dawn+2h (8am) to dusk-2h (4pm)
    scaled by cloud cover.
    """
    # Conservative bell curve: 8am–4pm (approx dawn+2h to dusk-2h)
    # Peak at solar noon (hour 12)
    if hour < 8 or hour > 16:
        return 0.0

    # Normalized hour position (0 at edges, 1 at noon)
    if hour <= 12:
        position = (hour - 8) / 4.0
    else:
        position = (16 - hour) / 4.0

    clear_sky_factor = position * (2 - position)  # Parabolic curve
    cloud_factor = 1.0 - (cloud_cover_pct / 100.0) * 0.75  # Clouds reduce by up to 75%
    return peak_capacity_w * clear_sky_factor * max(0.0, cloud_factor)


def build_fallback_forecast(
    cloud_cover_by_hour: dict[int, float],
    peak_capacity_w: float,
    start: datetime,
    hours: int = 48,
) -> SolarForecast:
    """Build a basic solar forecast from weather cloud cover data.

    Used as fallback when Solcast is stale or unavailable.
    """
    slots = []
    for h in range(hours * 2):  # 30-min slots
        slot_start = start + timedelta(minutes=h * 30)
        slot_end = slot_start + timedelta(minutes=30)
        hour = slot_start.hour

        cloud = cloud_cover_by_hour.get(hour, 50.0)
        estimate = estimate_from_cloud_cover(cloud, peak_capacity_w, hour)

        # Apply uncertainty band: P10 = 70% of estimate, P90 = 130%
        slots.append(
            SolarForecastSlot(
                start=slot_start,
                end=slot_end,
                pv_estimate_w=estimate,
                pv_estimate10_w=estimate * 0.7,
                pv_estimate90_w=estimate * 1.3,
            )
        )

    return SolarForecast(
        slots=slots,
        fetched_at=datetime.now(timezone.utc),
        provider="cloud_cover_fallback",
    )


def merge_solar_forecasts(
    primary: SolarForecast | None,
    fallback: SolarForecast | None,
    max_primary_age_seconds: int = 7200,
) -> SolarForecast | None:
    """Use primary (Solcast) if fresh, otherwise fall back to cloud-cover estimate."""
    if primary and primary.slots:
        age = (datetime.now(timezone.utc) - primary.fetched_at).total_seconds()
        if age <= max_primary_age_seconds:
            return primary
        logger.warning(
            "Primary solar forecast is stale (%.0fs old), using fallback", age
        )

    if fallback and fallback.slots:
        return fallback

    return primary  # Return stale data rather than nothing
