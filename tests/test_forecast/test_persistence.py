"""Tests for forecast sample persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from power_master.forecast.base import (
    SolarForecast,
    SolarForecastSlot,
    StormAlert,
    StormForecast,
    WeatherForecast,
    WeatherForecastSlot,
)
from power_master.forecast.persistence import (
    persist_solar_forecast,
    persist_storm_forecast,
    persist_tariff_forecast,
    persist_weather_forecast,
)
from power_master.tariff.base import TariffSchedule, TariffSlot


HORIZONS = [1.0, 4.0, 10.0, 18.0, 24.0]


def _solar_forecast(fetched_at: datetime, n_slots: int = 4) -> SolarForecast:
    slots = []
    for i in range(n_slots):
        start = fetched_at + timedelta(minutes=30 * (i + 1))
        slots.append(SolarForecastSlot(
            start=start,
            end=start + timedelta(minutes=30),
            pv_estimate_w=2000.0 + i * 100,
            pv_estimate10_w=1600.0 + i * 100,
            pv_estimate90_w=2400.0 + i * 100,
        ))
    return SolarForecast(slots=slots, fetched_at=fetched_at, provider="test")


class TestPersistSolar:
    @pytest.mark.asyncio
    async def test_persists_all_slots_and_bands(self, repo) -> None:
        fetched = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        forecast = _solar_forecast(fetched, n_slots=4)
        n = await persist_solar_forecast(repo, forecast)
        # 4 slots × 3 bands
        assert n == 12
        rows = await repo.get_forecast_samples("solar")
        assert len(rows) == 12
        metrics = {r["metric"] for r in rows}
        assert metrics == {"pv_estimate_w", "pv_estimate10_w", "pv_estimate90_w"}

    @pytest.mark.asyncio
    async def test_skips_past_slots(self, repo) -> None:
        fetched = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        # Slot starts 1h before fetch — should be skipped
        slot = SolarForecastSlot(
            start=fetched - timedelta(hours=1),
            end=fetched - timedelta(minutes=30),
            pv_estimate_w=1000.0,
            pv_estimate10_w=800.0,
            pv_estimate90_w=1200.0,
        )
        forecast = SolarForecast(slots=[slot], fetched_at=fetched, provider="test")
        n = await persist_solar_forecast(repo, forecast)
        assert n == 0

    @pytest.mark.asyncio
    async def test_deduplicates_on_re_store(self, repo) -> None:
        fetched = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        forecast = _solar_forecast(fetched, n_slots=2)
        await persist_solar_forecast(repo, forecast)
        await persist_solar_forecast(repo, forecast)  # second call — INSERT OR IGNORE
        rows = await repo.get_forecast_samples("solar")
        assert len(rows) == 6  # 2 slots × 3 bands, no duplicates


class TestPersistWeather:
    @pytest.mark.asyncio
    async def test_hourly_dilution_skips_mid_hour_fetch(self, repo) -> None:
        fetched = datetime(2025, 6, 15, 12, 17, tzinfo=timezone.utc)  # mid-hour
        slots = [WeatherForecastSlot(
            time=fetched + timedelta(hours=int(h)),
            temperature_c=20.0 + h, cloud_cover_pct=50.0,
        ) for h in HORIZONS]
        forecast = WeatherForecast(slots=slots, fetched_at=fetched, provider="test")
        n = await persist_weather_forecast(repo, forecast, HORIZONS)
        assert n == 0

    @pytest.mark.asyncio
    async def test_persists_bucketed_horizons(self, repo) -> None:
        fetched = datetime(2025, 6, 15, 12, 2, tzinfo=timezone.utc)  # top-of-hour window
        # Populate weather slots at the exact horizon offsets (rounded to hour 12:00)
        hour_top = fetched.replace(minute=0, second=0, microsecond=0)
        slots = [WeatherForecastSlot(
            time=hour_top + timedelta(hours=int(h)),
            temperature_c=20.0 + h, cloud_cover_pct=50.0,
        ) for h in HORIZONS]
        forecast = WeatherForecast(slots=slots, fetched_at=fetched, provider="test")
        n = await persist_weather_forecast(repo, forecast, HORIZONS)
        # 5 horizons × 2 metrics
        assert n == 10
        rows = await repo.get_forecast_samples("weather")
        horizons_seen = sorted({r["horizon_hours"] for r in rows})
        assert horizons_seen == HORIZONS


class TestPersistTariff:
    @pytest.mark.asyncio
    async def test_persists_bucketed_horizons(self, repo) -> None:
        fetched = datetime(2025, 6, 15, 12, 3, tzinfo=timezone.utc)
        hour_top = fetched.replace(minute=0, second=0, microsecond=0)
        slots = []
        # Build slots covering 0–30h ahead so all horizons match
        for i in range(60):
            start = hour_top + timedelta(minutes=30 * i)
            slots.append(TariffSlot(
                start=start, end=start + timedelta(minutes=30),
                import_price_cents=10.0 + i * 0.5,
                export_price_cents=3.0,
            ))
        schedule = TariffSchedule(slots=slots, fetched_at=fetched, provider="amber")
        n = await persist_tariff_forecast(repo, schedule, HORIZONS)
        assert n == 10  # 5 horizons × 2 metrics
        rows = await repo.get_forecast_samples("tariff", metric="import_price_cents")
        assert len(rows) == 5


class TestPersistStorm:
    @pytest.mark.asyncio
    async def test_uses_max_alert_probability_at_each_horizon(self, repo) -> None:
        fetched = datetime(2025, 6, 15, 12, 1, tzinfo=timezone.utc)
        hour_top = fetched.replace(minute=0, second=0, microsecond=0)
        alerts = [StormAlert(
            location="test",
            probability=0.7,
            description="x",
            valid_from=hour_top + timedelta(hours=0),
            valid_to=hour_top + timedelta(hours=6),
        )]
        forecast = StormForecast(alerts=alerts, fetched_at=fetched, provider="test")
        n = await persist_storm_forecast(repo, forecast, HORIZONS)
        # 5 horizons × 1 metric, values 0.7 for <=6h, 0 after
        assert n == 5
        rows = await repo.get_forecast_samples("storm", metric="max_probability")
        by_horizon = {r["horizon_hours"]: r["predicted_value"] for r in rows}
        assert by_horizon[1.0] == 0.7
        assert by_horizon[4.0] == 0.7
        assert by_horizon[10.0] == 0.0
        assert by_horizon[24.0] == 0.0


class TestPrune:
    @pytest.mark.asyncio
    async def test_prune_drops_rows_before_cutoff(self, repo) -> None:
        fetched = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        old_target = (fetched - timedelta(days=400)).isoformat()
        recent_target = (fetched - timedelta(days=10)).isoformat()
        await repo.store_forecast_samples([
            {"provider_type": "solar", "metric": "pv_estimate_w",
             "fetched_at": (fetched - timedelta(days=401)).isoformat(),
             "horizon_hours": 1.0, "target_time": old_target,
             "predicted_value": 100.0},
            {"provider_type": "solar", "metric": "pv_estimate_w",
             "fetched_at": (fetched - timedelta(days=11)).isoformat(),
             "horizon_hours": 1.0, "target_time": recent_target,
             "predicted_value": 200.0},
        ])
        cutoff = (fetched - timedelta(days=365)).isoformat()
        n = await repo.prune_forecast_samples(cutoff)
        assert n == 1
        rows = await repo.get_forecast_samples("solar")
        assert len(rows) == 1
        assert rows[0]["target_time"] == recent_target
