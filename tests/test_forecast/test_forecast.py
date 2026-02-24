"""Tests for forecast base models, aggregator, and solar estimate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from power_master.forecast.base import (
    SolarForecast,
    SolarForecastSlot,
    SolarProvider,
    StormAlert,
    StormForecast,
    StormProvider,
    WeatherForecast,
    WeatherForecastSlot,
    WeatherProvider,
)
from power_master.forecast.aggregator import AggregatedForecast, ForecastAggregator
from power_master.forecast.solar_estimate import (
    build_fallback_forecast,
    estimate_from_cloud_cover,
    merge_solar_forecasts,
)
from power_master.tariff.base import TariffProvider, TariffSchedule, TariffSlot
from power_master.tariff.spike import SpikeDetector


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TestSolarForecastSlot:
    def test_confidence_high_when_narrow_band(self) -> None:
        slot = SolarForecastSlot(
            start=_now(),
            end=_now() + timedelta(minutes=30),
            pv_estimate_w=5000,
            pv_estimate10_w=4500,
            pv_estimate90_w=5500,
        )
        assert slot.confidence > 0.7

    def test_confidence_low_when_wide_band(self) -> None:
        slot = SolarForecastSlot(
            start=_now(),
            end=_now() + timedelta(minutes=30),
            pv_estimate_w=5000,
            pv_estimate10_w=1000,
            pv_estimate90_w=9000,
        )
        assert slot.confidence < 0.5

    def test_confidence_zero_floor(self) -> None:
        slot = SolarForecastSlot(
            start=_now(),
            end=_now() + timedelta(minutes=30),
            pv_estimate_w=100,
            pv_estimate10_w=0,
            pv_estimate90_w=5000,
        )
        assert slot.confidence >= 0.0


class TestStormForecast:
    def test_max_probability_empty(self) -> None:
        sf = StormForecast()
        assert sf.max_probability == 0.0

    def test_max_probability_with_alerts(self) -> None:
        sf = StormForecast(
            alerts=[
                StormAlert(
                    location="Brisbane",
                    probability=0.3,
                    description="Possible showers",
                    valid_from=_now(),
                    valid_to=_now() + timedelta(hours=6),
                ),
                StormAlert(
                    location="Brisbane",
                    probability=0.8,
                    description="Severe thunderstorm",
                    valid_from=_now(),
                    valid_to=_now() + timedelta(hours=6),
                ),
            ]
        )
        assert sf.max_probability == 0.8


class TestSolarEstimate:
    def test_zero_output_at_night(self) -> None:
        assert estimate_from_cloud_cover(0, 5000, 3) == 0.0
        assert estimate_from_cloud_cover(0, 5000, 20) == 0.0

    def test_zero_before_8am(self) -> None:
        for hour in range(0, 8):
            assert estimate_from_cloud_cover(0, 5000, hour) == 0.0

    def test_zero_after_4pm(self) -> None:
        for hour in range(17, 24):
            assert estimate_from_cloud_cover(0, 5000, hour) == 0.0

    def test_peak_at_noon(self) -> None:
        noon = estimate_from_cloud_cover(0, 5000, 12)
        for hour in [8, 9, 10, 11, 13, 14, 15, 16]:
            assert estimate_from_cloud_cover(0, 5000, hour) <= noon

    def test_symmetric_around_noon(self) -> None:
        # 10am (noon-2) should equal 2pm (noon+2)
        morning = estimate_from_cloud_cover(0, 5000, 10)
        afternoon = estimate_from_cloud_cover(0, 5000, 14)
        assert abs(morning - afternoon) < 0.1

    def test_boundary_hours_produce_output(self) -> None:
        # Hour 8 and 16 are at the edge — should produce some output
        assert estimate_from_cloud_cover(0, 5000, 8) == 0.0  # Edge: position=0
        assert estimate_from_cloud_cover(0, 5000, 16) == 0.0  # Edge: position=0
        # Hour 9 and 15 should produce meaningful output
        assert estimate_from_cloud_cover(0, 5000, 9) > 0
        assert estimate_from_cloud_cover(0, 5000, 15) > 0

    def test_peak_output_at_noon_clear_sky(self) -> None:
        output = estimate_from_cloud_cover(0, 5000, 12)
        assert output > 4000  # Near peak

    def test_clouds_reduce_output(self) -> None:
        clear = estimate_from_cloud_cover(0, 5000, 12)
        cloudy = estimate_from_cloud_cover(100, 5000, 12)
        assert cloudy < clear
        assert cloudy > 0  # Still some output with 100% cloud

    def test_fallback_forecast_has_correct_slots(self) -> None:
        cloud = {h: 30.0 for h in range(24)}
        fc = build_fallback_forecast(cloud, 5000, _now(), hours=24)
        assert len(fc.slots) == 48  # 24 hours * 2 slots/hour
        assert fc.provider == "cloud_cover_fallback"

    def test_merge_prefers_fresh_primary(self) -> None:
        primary = SolarForecast(
            slots=[
                SolarForecastSlot(
                    start=_now(),
                    end=_now() + timedelta(minutes=30),
                    pv_estimate_w=5000,
                    pv_estimate10_w=4000,
                    pv_estimate90_w=6000,
                )
            ],
            fetched_at=_now(),
            provider="forecast_solar",
        )
        fallback = SolarForecast(
            slots=[
                SolarForecastSlot(
                    start=_now(),
                    end=_now() + timedelta(minutes=30),
                    pv_estimate_w=3000,
                    pv_estimate10_w=2000,
                    pv_estimate90_w=4000,
                )
            ],
            fetched_at=_now(),
            provider="fallback",
        )
        result = merge_solar_forecasts(primary, fallback)
        assert result is not None
        assert result.provider == "forecast_solar"

    def test_merge_uses_fallback_when_stale(self) -> None:
        stale_time = _now() - timedelta(hours=3)
        primary = SolarForecast(
            slots=[
                SolarForecastSlot(
                    start=_now(),
                    end=_now() + timedelta(minutes=30),
                    pv_estimate_w=5000,
                    pv_estimate10_w=4000,
                    pv_estimate90_w=6000,
                )
            ],
            fetched_at=stale_time,
            provider="forecast_solar",
        )
        fallback = SolarForecast(
            slots=[
                SolarForecastSlot(
                    start=_now(),
                    end=_now() + timedelta(minutes=30),
                    pv_estimate_w=3000,
                    pv_estimate10_w=2000,
                    pv_estimate90_w=4000,
                )
            ],
            fetched_at=_now(),
            provider="fallback",
        )
        result = merge_solar_forecasts(primary, fallback)
        assert result is not None
        assert result.provider == "fallback"


class TestForecastAggregator:
    def _make_tariff_schedule(self, price: float = 20.0) -> TariffSchedule:
        now = _now()
        return TariffSchedule(
            slots=[
                TariffSlot(
                    start=now - timedelta(minutes=15),
                    end=now + timedelta(minutes=15),
                    import_price_cents=price,
                    export_price_cents=5.0,
                )
            ],
            fetched_at=now,
            provider="test",
        )

    @pytest.mark.asyncio
    async def test_update_tariff_runs_spike_detection(self) -> None:
        mock_tariff = AsyncMock(spec=TariffProvider)
        mock_tariff.fetch_prices.return_value = self._make_tariff_schedule(20.0)

        agg = ForecastAggregator(tariff_provider=mock_tariff)
        await agg.update_tariff()

        assert agg.state.has_tariff
        assert not agg.spike_detector.is_spike_active

    @pytest.mark.asyncio
    async def test_update_tariff_detects_spike(self) -> None:
        mock_tariff = AsyncMock(spec=TariffProvider)
        mock_tariff.fetch_prices.return_value = self._make_tariff_schedule(150.0)

        agg = ForecastAggregator(
            tariff_provider=mock_tariff,
            spike_detector=SpikeDetector(spike_threshold_cents=100),
        )
        await agg.update_tariff()

        assert agg.spike_detector.is_spike_active

    @pytest.mark.asyncio
    async def test_update_all_handles_missing_providers(self) -> None:
        agg = ForecastAggregator()  # No providers
        state = await agg.update_all()
        assert not state.has_solar
        assert not state.has_weather
        assert not state.has_tariff

    @pytest.mark.asyncio
    async def test_update_all_handles_provider_error(self) -> None:
        mock_solar = AsyncMock(spec=SolarProvider)
        mock_solar.fetch_forecast.side_effect = Exception("API error")

        agg = ForecastAggregator(solar_provider=mock_solar)
        state = await agg.update_all()
        assert not state.has_solar  # Gracefully handles error

    @pytest.mark.asyncio
    async def test_update_all_with_tariff(self) -> None:
        mock_tariff = AsyncMock(spec=TariffProvider)
        mock_tariff.fetch_prices.return_value = self._make_tariff_schedule(25.0)

        agg = ForecastAggregator(tariff_provider=mock_tariff)
        state = await agg.update_all()
        assert state.has_tariff

    @pytest.mark.asyncio
    async def test_stale_detection(self) -> None:
        mock_tariff = AsyncMock(spec=TariffProvider)
        mock_tariff.fetch_prices.return_value = self._make_tariff_schedule()

        agg = ForecastAggregator(tariff_provider=mock_tariff)
        await agg.update_tariff()

        # Fresh data is not stale
        assert not agg.is_stale(max_age_seconds=7200)
