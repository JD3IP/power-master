"""Persist per-horizon forecast samples to the database.

Solar forecasts are persisted at native resolution every fetch (all 30-min
slots, all three bands P10/P50/P90) so we can calibrate short-horizon bias
precisely.

Weather, tariff and storm forecasts are diluted to hourly cadence and
bucketed to a small set of fixed horizons (default 1h / 4h / 10h / 18h /
24h ahead) — that's enough to measure accuracy drift without bloating the
DB.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from power_master.forecast.base import (
    SolarForecast,
    StormForecast,
    WeatherForecast,
)
from power_master.tariff.base import TariffSchedule

logger = logging.getLogger(__name__)


# Persist non-solar forecasts only near the top of the hour (UTC minute < 5).
# Cheap deduplication that's robust to variable fetch cadences (5 min for
# Amber, 1 h for Open-Meteo, 6 h for storm).
def _is_top_of_hour(t: datetime) -> bool:
    return t.minute < 5


def _round_to_hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


async def persist_solar_forecast(repo: Any, forecast: SolarForecast) -> int:
    """Persist every slot of a solar forecast across all three bands."""
    if forecast is None or not forecast.slots:
        return 0
    fetched_at = forecast.fetched_at.astimezone(timezone.utc).isoformat()
    samples: list[dict[str, Any]] = []
    for slot in forecast.slots:
        target_utc = slot.start.astimezone(timezone.utc)
        horizon_h = (target_utc - forecast.fetched_at.astimezone(timezone.utc)).total_seconds() / 3600.0
        if horizon_h < 0:  # slot already in the past at fetch — skip
            continue
        for metric, value in (
            ("pv_estimate_w", slot.pv_estimate_w),
            ("pv_estimate10_w", slot.pv_estimate10_w),
            ("pv_estimate90_w", slot.pv_estimate90_w),
        ):
            samples.append({
                "provider_type": "solar",
                "metric": metric,
                "fetched_at": fetched_at,
                "horizon_hours": round(horizon_h, 3),
                "target_time": target_utc.isoformat(),
                "predicted_value": float(value),
            })
    n = await repo.store_forecast_samples(samples)
    await _touch_forecast_age(repo, "solar", forecast.fetched_at, forecast.provider or "solar")
    return n


async def _touch_forecast_age(
    repo: Any, provider_type: str, fetched_at: datetime, provider_name: str,
) -> None:
    """Write a single minimal forecast_snapshots row so
    repo.get_forecast_age_seconds("<provider>") keeps returning sensible
    values for the aggregator freshness check.  The snapshot JSON is a
    placeholder — the real per-horizon data lives in forecast_samples.
    """
    try:
        await repo.store_forecast(
            provider_type=provider_type,
            provider_name=provider_name,
            horizon_start=fetched_at.astimezone(timezone.utc).isoformat(),
            horizon_end=fetched_at.astimezone(timezone.utc).isoformat(),
            data={"note": "persistence touch — see forecast_samples for data"},
        )
    except Exception:
        logger.debug("Failed to touch forecast_snapshots for %s", provider_type, exc_info=True)


def _bucket_samples_at_horizons(
    forecast_fetched_at: datetime,
    slot_lookup,
    metrics: list[tuple[str, str]],  # [(metric_name, attr_on_slot), ...]
    horizons_hours: Sequence[float],
    provider_type: str,
) -> list[dict[str, Any]]:
    """Gather samples at each configured horizon by querying slot_lookup.

    slot_lookup(target_time_utc) -> slot_object_or_None.
    """
    fetched_hour = _round_to_hour(forecast_fetched_at.astimezone(timezone.utc))
    fetched_iso = fetched_hour.isoformat()
    samples: list[dict[str, Any]] = []
    for h in horizons_hours:
        target = fetched_hour + timedelta(hours=h)
        slot = slot_lookup(target)
        if slot is None:
            continue
        for metric_name, attr in metrics:
            value = getattr(slot, attr, None)
            if value is None:
                continue
            samples.append({
                "provider_type": provider_type,
                "metric": metric_name,
                "fetched_at": fetched_iso,
                "horizon_hours": float(h),
                "target_time": target.isoformat(),
                "predicted_value": float(value),
            })
    return samples


async def persist_weather_forecast(
    repo: Any, forecast: WeatherForecast, horizons_hours: Sequence[float],
) -> int:
    """Persist weather forecast at fixed horizons, hourly cadence."""
    if forecast is None or not forecast.slots:
        return 0
    if not _is_top_of_hour(forecast.fetched_at.astimezone(timezone.utc)):
        return 0

    def lookup(target_time):
        for slot in forecast.slots:
            slot_start = slot.time.astimezone(timezone.utc)
            # Weather slots are hourly — match by hour
            if abs((slot_start - target_time).total_seconds()) < 1800:
                return slot
        return None

    samples = _bucket_samples_at_horizons(
        forecast.fetched_at, lookup,
        [("temperature_c", "temperature_c"),
         ("cloud_cover_pct", "cloud_cover_pct")],
        horizons_hours,
        "weather",
    )
    n = await repo.store_forecast_samples(samples)
    await _touch_forecast_age(repo, "weather", forecast.fetched_at, forecast.provider or "weather")
    return n


async def persist_tariff_forecast(
    repo: Any, schedule: TariffSchedule, horizons_hours: Sequence[float],
) -> int:
    """Persist tariff forecast at fixed horizons, hourly cadence."""
    if schedule is None or not schedule.slots:
        return 0
    fetched_at = getattr(schedule, "fetched_at", None) or datetime.now(timezone.utc)
    if not _is_top_of_hour(fetched_at.astimezone(timezone.utc)):
        return 0

    def lookup(target_time):
        for slot in schedule.slots:
            if slot.start <= target_time < slot.end:
                return slot
        return None

    samples = _bucket_samples_at_horizons(
        fetched_at, lookup,
        [("import_price_cents", "import_price_cents"),
         ("export_price_cents", "export_price_cents")],
        horizons_hours,
        "tariff",
    )
    n = await repo.store_forecast_samples(samples)
    await _touch_forecast_age(repo, "tariff", fetched_at, getattr(schedule, "provider", "tariff") or "tariff")
    return n


async def persist_storm_forecast(
    repo: Any, forecast: StormForecast, horizons_hours: Sequence[float],
) -> int:
    """Persist storm max-probability at fixed horizons, hourly cadence."""
    if forecast is None:
        return 0
    fetched_at = forecast.fetched_at
    if not _is_top_of_hour(fetched_at.astimezone(timezone.utc)):
        return 0

    # Storm alerts have valid_from/valid_to windows; take the max probability
    # of any alert whose window covers the target time (or 0 if none).
    def probability_at(target_time) -> float:
        best = 0.0
        for alert in forecast.alerts:
            if alert.valid_from <= target_time <= alert.valid_to:
                best = max(best, alert.probability)
        return best

    fetched_hour = _round_to_hour(fetched_at.astimezone(timezone.utc))
    samples: list[dict[str, Any]] = []
    for h in horizons_hours:
        target = fetched_hour + timedelta(hours=h)
        samples.append({
            "provider_type": "storm",
            "metric": "max_probability",
            "fetched_at": fetched_hour.isoformat(),
            "horizon_hours": float(h),
            "target_time": target.isoformat(),
            "predicted_value": probability_at(target),
        })
    n = await repo.store_forecast_samples(samples)
    await _touch_forecast_age(repo, "storm", fetched_at, forecast.provider or "storm")
    return n
