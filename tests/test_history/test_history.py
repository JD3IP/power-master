"""Tests for historical data collection, patterns, and prediction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from power_master.hardware.telemetry import Telemetry
from power_master.history.collector import HistoryCollector
from power_master.history.patterns import (
    DayOfWeekProfile,
    HourlyProfile,
    build_day_of_week_profile,
    build_hourly_profile,
    weighted_moving_average,
)
from power_master.history.prediction import LoadPredictor, SolarPredictor


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TestHourlyProfile:
    def test_build_from_records(self) -> None:
        records = [
            {"recorded_at": "2025-06-15T10:00:00+00:00", "value": 1000},
            {"recorded_at": "2025-06-15T10:30:00+00:00", "value": 1200},
            {"recorded_at": "2025-06-15T14:00:00+00:00", "value": 3000},
        ]
        profile = build_hourly_profile(records)
        assert profile.get(10) == 1100.0  # avg of 1000 and 1200
        assert profile.get(14) == 3000.0
        assert profile.get(22) == 0.0  # no data, default

    def test_empty_records(self) -> None:
        profile = build_hourly_profile([])
        assert profile.get(12) == 0.0


class TestDayOfWeekProfile:
    def test_build_weekday_vs_weekend(self) -> None:
        # Monday records (2025-06-16 is a Monday)
        records = [
            {"recorded_at": "2025-06-16T10:00:00+00:00", "value": 500},
            {"recorded_at": "2025-06-16T10:30:00+00:00", "value": 600},
            # Saturday records (2025-06-21 is a Saturday)
            {"recorded_at": "2025-06-21T10:00:00+00:00", "value": 2000},
            {"recorded_at": "2025-06-21T10:30:00+00:00", "value": 2200},
        ]
        profile = build_day_of_week_profile(records)
        assert profile.get(0, 10) == 550.0  # Monday, 10am
        assert profile.get(5, 10) == 2100.0  # Saturday, 10am
        assert profile.get(2, 10) == 0.0  # Wednesday, no data


class TestWeightedMovingAverage:
    def test_uniform_weights(self) -> None:
        result = weighted_moving_average([100, 200, 300], [1, 1, 1])
        assert result == 200.0

    def test_recent_weighted_higher(self) -> None:
        result = weighted_moving_average([100, 200, 300], [0.1, 0.3, 0.6])
        assert result > 200  # Recent (300) weighted more

    def test_empty(self) -> None:
        assert weighted_moving_average([]) == 0.0

    def test_default_weights(self) -> None:
        result = weighted_moving_average([100, 200, 300])
        # Default weights favour recent values
        assert result > 0


class TestHistoryCollectorPrice:
    """Tests for record_price() storing all past slots and dedup."""

    @pytest.mark.asyncio
    async def test_record_price_stores_all_past_slots(self, repo) -> None:
        from power_master.tariff.base import TariffSchedule, TariffSlot

        collector = HistoryCollector(repo)
        now = _now()
        schedule = TariffSchedule(
            slots=[
                TariffSlot(
                    start=now - timedelta(hours=2),
                    end=now - timedelta(hours=1, minutes=30),
                    import_price_cents=10.0, export_price_cents=3.0,
                ),
                TariffSlot(
                    start=now - timedelta(hours=1),
                    end=now - timedelta(minutes=30),
                    import_price_cents=20.0, export_price_cents=5.0,
                ),
                TariffSlot(
                    start=now + timedelta(hours=1),
                    end=now + timedelta(hours=1, minutes=30),
                    import_price_cents=99.0, export_price_cents=99.0,
                ),
            ]
        )
        await collector.record_price(schedule)

        start = (now - timedelta(hours=3)).isoformat()
        end = now.isoformat()
        records = await repo.get_historical("import_price_cents", start, end)
        # Only 2 past slots stored, future slot skipped
        assert len(records) == 2
        values = sorted([r["value"] for r in records])
        assert values == [10.0, 20.0]

    @pytest.mark.asyncio
    async def test_record_price_dedup_on_repeat(self, repo) -> None:
        """Calling record_price() twice with same data should not duplicate."""
        from power_master.tariff.base import TariffSchedule, TariffSlot

        collector = HistoryCollector(repo)
        now = _now()
        schedule = TariffSchedule(
            slots=[
                TariffSlot(
                    start=now - timedelta(hours=1),
                    end=now - timedelta(minutes=30),
                    import_price_cents=25.0, export_price_cents=8.0,
                ),
            ]
        )
        await collector.record_price(schedule)
        await collector.record_price(schedule)

        start = (now - timedelta(hours=2)).isoformat()
        end = now.isoformat()
        records = await repo.get_historical("import_price_cents", start, end)
        assert len(records) == 1

    @pytest.mark.asyncio
    async def test_record_price_empty_schedule(self, repo) -> None:
        from power_master.tariff.base import TariffSchedule

        collector = HistoryCollector(repo)
        await collector.record_price(TariffSchedule(slots=[]))
        # No error, no records


class TestHistoryLoader:
    @pytest.mark.asyncio
    async def test_needs_backfill_empty_db(self, repo) -> None:
        from power_master.history.loader import HistoryLoader

        loader = HistoryLoader(repo)
        assert await loader.needs_backfill("import_price_cents") is True

    @pytest.mark.asyncio
    async def test_needs_backfill_sufficient_data(self, repo) -> None:
        from power_master.history.loader import HistoryLoader

        now = _now()
        for day in range(8):
            for slot in range(3):
                ts = (now - timedelta(days=day, hours=slot)).isoformat()
                await repo.store_historical("import_price_cents", 20.0, "amber", ts)

        loader = HistoryLoader(repo)
        assert await loader.needs_backfill("import_price_cents") is False

    @pytest.mark.asyncio
    async def test_backfill_prices_with_mock(self, repo) -> None:
        from unittest.mock import AsyncMock

        from power_master.history.loader import HistoryLoader
        from power_master.tariff.base import TariffProvider, TariffSchedule, TariffSlot

        now = _now()
        mock_provider = AsyncMock(spec=TariffProvider)
        mock_provider.fetch_historical.return_value = TariffSchedule(
            slots=[
                TariffSlot(
                    start=now - timedelta(days=1),
                    end=now - timedelta(days=1) + timedelta(minutes=30),
                    import_price_cents=15.0, export_price_cents=4.0,
                ),
                TariffSlot(
                    start=now - timedelta(days=2),
                    end=now - timedelta(days=2) + timedelta(minutes=30),
                    import_price_cents=12.0, export_price_cents=3.0,
                ),
            ]
        )

        loader = HistoryLoader(repo)
        count = await loader.backfill_prices(mock_provider, months=1)
        assert count == 2

        start = (now - timedelta(days=3)).isoformat()
        records = await repo.get_historical("import_price_cents", start, now.isoformat())
        assert len(records) == 2


class TestHistoryCollector:
    @pytest.mark.asyncio
    async def test_flush_telemetry(self, repo) -> None:
        collector = HistoryCollector(repo)

        # Buffer some telemetry readings
        for i in range(4):
            t = Telemetry(
                soc=0.5,
                battery_power_w=0,
                solar_power_w=3000 + i * 100,
                grid_power_w=-500,
                load_power_w=2500,
            )
            collector.record_telemetry(t)

        await collector.flush_telemetry()

        # Verify stored
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=1)).isoformat()
        records = await repo.get_historical("solar_w", start, now.isoformat())
        assert len(records) == 1
        assert records[0]["value"] == 3150.0  # avg of 3000,3100,3200,3300

    @pytest.mark.asyncio
    async def test_flush_empty_buffer(self, repo) -> None:
        collector = HistoryCollector(repo)
        # Should not error on empty buffer
        await collector.flush_telemetry()


class TestLoadPredictor:
    @pytest.mark.asyncio
    async def test_predict_without_profile_returns_default(self, repo) -> None:
        predictor = LoadPredictor(repo)
        result = predictor.predict(_now(), default_w=750.0)
        assert result == 750.0

    @pytest.mark.asyncio
    async def test_predict_with_profile(self, repo) -> None:
        # Store enough recent load history to pass the minimum threshold (48 records)
        now = _now()
        for day_offset in range(7):
            dt_base = now - timedelta(days=day_offset)
            for hour in range(0, 24):
                dt = dt_base.replace(hour=hour, minute=0, second=0, microsecond=0)
                load_val = 1500.0 if hour == 10 else 800.0
                await repo.store_historical("load_w", load_val, "test", dt.isoformat())

        predictor = LoadPredictor(repo)
        await predictor.rebuild_profile(lookback_days=60)

        # Predict for same day-of-week, 10am
        predict_dt = now.replace(hour=10, minute=0, second=0, microsecond=0)
        result = predictor.predict(predict_dt)
        assert result == 1500.0

    @pytest.mark.asyncio
    async def test_predict_with_profile_local_timezone_mapping(self, repo) -> None:
        # Store values keyed to local noon in Brisbane (02:00 UTC)
        # and verify prediction converts query time to local hour.
        now = _now()
        for day_offset in range(7):
            base = (now - timedelta(days=day_offset)).replace(
                hour=2, minute=0, second=0, microsecond=0
            )
            for sample in range(8):  # 56 total samples (>48 threshold)
                dt_utc = base + timedelta(minutes=sample)
                await repo.store_historical("load_w", 2100.0, "test", dt_utc.isoformat())

        predictor = LoadPredictor(repo, timezone_name="Australia/Brisbane")
        await predictor.rebuild_profile(lookback_days=60)

        query_utc = now.replace(hour=2, minute=0, second=0, microsecond=0)  # local 12:00
        result = predictor.predict(query_utc, default_w=500.0)
        assert result == 2100.0

    @pytest.mark.asyncio
    async def test_predict_range(self, repo) -> None:
        predictor = LoadPredictor(repo)
        start = _now()
        slots = predictor.predict_range(start, hours=2, default_w=500.0)
        assert len(slots) == 4  # 2 hours / 30-min slots
        assert all(v == 500.0 for _, v in slots)


class TestLoadProfileConfig:
    def test_get_for_hour_overnight(self) -> None:
        from power_master.config.schema import LoadProfileConfig
        cfg = LoadProfileConfig()
        assert cfg.get_for_hour(0) == 500.0
        assert cfg.get_for_hour(3) == 500.0

    def test_get_for_hour_morning(self) -> None:
        from power_master.config.schema import LoadProfileConfig
        cfg = LoadProfileConfig()
        assert cfg.get_for_hour(4) == 800.0
        assert cfg.get_for_hour(7) == 800.0

    def test_get_for_hour_evening_peak(self) -> None:
        from power_master.config.schema import LoadProfileConfig
        cfg = LoadProfileConfig()
        assert cfg.get_for_hour(16) == 2500.0
        assert cfg.get_for_hour(19) == 2500.0

    def test_get_for_hour_night(self) -> None:
        from power_master.config.schema import LoadProfileConfig
        cfg = LoadProfileConfig()
        assert cfg.get_for_hour(20) == 1500.0
        assert cfg.get_for_hour(23) == 1500.0

    def test_custom_values(self) -> None:
        from power_master.config.schema import LoadProfileConfig
        cfg = LoadProfileConfig(block_16_20_w=3500)
        assert cfg.get_for_hour(17) == 3500.0

    def test_config_includes_load_profile(self) -> None:
        from power_master.config.schema import AppConfig
        config = AppConfig()
        assert hasattr(config, "load_profile")
        assert config.load_profile.block_16_20_w == 2500
        assert config.load_profile.timezone == "Australia/Brisbane"


class TestSolarPredictor:
    @pytest.mark.asyncio
    async def test_predict_without_profile_returns_zero(self, repo) -> None:
        predictor = SolarPredictor(repo)
        result = predictor.predict(_now())
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_cloud_adjustment(self, repo) -> None:
        # Store enough recent solar history to pass the minimum threshold
        now = _now()
        for day_offset in range(7):
            dt_base = now - timedelta(days=day_offset)
            for hour in range(6, 19):  # Daylight hours
                dt = dt_base.replace(hour=hour, minute=0, second=0, microsecond=0)
                solar_val = 4000.0 if hour == 12 else 2000.0
                await repo.store_historical("solar_w", solar_val, "test", dt.isoformat())

        predictor = SolarPredictor(repo)
        await predictor.rebuild_profile(lookback_days=60)

        # Predict for same day-of-week, noon
        predict_dt = now.replace(hour=12, minute=0, second=0, microsecond=0)

        # Clear sky (0% cloud) should increase estimate
        clear = predictor.predict(predict_dt, cloud_cover_pct=0.0)
        # Overcast (100% cloud) should decrease estimate
        cloudy = predictor.predict(predict_dt, cloud_cover_pct=100.0)

        assert clear > cloudy
        assert clear > 0
        assert cloudy >= 0
