"""Tests for solar forecast calibration."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from power_master.forecast.solar_calibration import (
    MIN_TRAINING_SAMPLES,
    TrainingSample,
    apply_calibration,
    build_training_set,
    fit_calibration_model,
)


def _sample(forecast_w: float, actual_w: float, local_hour: float, age_days: float = 0.5) -> TrainingSample:
    return TrainingSample(
        slot_start_utc=datetime(2025, 1, 1, tzinfo=timezone.utc),
        local_solar_hour=local_hour,
        forecast_w=forecast_w,
        actual_w=actual_w,
        age_days=age_days,
    )


class TestFitCalibration:
    def test_returns_none_when_too_few_samples(self) -> None:
        samples = [_sample(1000.0, 1000.0, 12.0) for _ in range(10)]
        model = fit_calibration_model(samples, system_peak_w=5000.0, tz_name="UTC")
        assert model is None

    def test_perfect_forecast_fits_with_near_zero_mae(self) -> None:
        # When forecast matches actual perfectly, the fitted model should
        # reproduce it (allowing for ridge shrinkage to split attribution
        # between correlated features — forecast magnitude and time-of-day
        # harmonics both describe the same bell curve).
        samples = []
        for i in range(MIN_TRAINING_SAMPLES + 20):
            hour = 6.0 + (i % 12) * 0.5
            forecast = 4000.0 * math.sin(math.pi * (hour - 6.0) / 12.0)
            samples.append(_sample(forecast, forecast, hour))
        model = fit_calibration_model(samples, system_peak_w=5000.0, tz_name="UTC")
        assert model is not None
        assert model.calibrated_mae_w < 30.0
        assert model.raw_mae_w < 1e-6  # raw forecast was perfect
        # Calibrated predictions don't wander far from raw:
        times = [datetime(2025, 1, 1, int(h), int((h % 1) * 60), tzinfo=timezone.utc)
                 for h in (6.0, 9.0, 12.0, 15.0)]
        forecasts = [4000.0 * math.sin(math.pi * (h - 6.0) / 12.0)
                     for h in (6.0, 9.0, 12.0, 15.0)]
        # Fake UTC local: tz="UTC" so local hour matches the datetime hour
        calibrated = apply_calibration(forecasts, times, model)
        for raw, cal in zip(forecasts, calibrated):
            if raw > 100:
                assert abs(cal - raw) / raw < 0.05

    def test_systematically_biased_forecast_reduces_mae(self) -> None:
        # Forecast consistently 40% too high.  After calibration the MAE
        # against actuals should fall dramatically.
        samples = []
        for i in range(MIN_TRAINING_SAMPLES * 2):
            hour = 8.0 + (i % 10) * 0.5
            true_w = 3500.0 * math.sin(math.pi * (hour - 8.0) / 10.0) + 200.0
            forecast_w = true_w / 0.7
            samples.append(_sample(forecast_w, true_w, hour))
        model = fit_calibration_model(samples, system_peak_w=5000.0, tz_name="UTC")
        assert model is not None
        assert model.calibrated_mae_w < model.raw_mae_w * 0.1
        # Applied to an in-distribution slot, the calibrated value should
        # come in close to the actual.
        hour = 10.5
        true_w = 3500.0 * math.sin(math.pi * (hour - 8.0) / 10.0) + 200.0
        forecast_w = true_w / 0.7
        t = datetime(2025, 1, 1, int(hour), int((hour % 1) * 60), tzinfo=timezone.utc)
        calibrated = apply_calibration([forecast_w], [t], model)
        assert abs(calibrated[0] - true_w) / true_w < 0.05


class TestApplyCalibration:
    def test_passthrough_when_model_is_none(self) -> None:
        forecasts = [100.0, 500.0, 2000.0, 0.0]
        times = [datetime(2025, 1, 1, h, tzinfo=timezone.utc) for h in (6, 10, 12, 20)]
        out = apply_calibration(forecasts, times, None)
        assert out == forecasts

    def test_never_invents_solar_at_night(self) -> None:
        samples = [_sample(3000.0, 2100.0, 12.0) for _ in range(MIN_TRAINING_SAMPLES * 2)]
        model = fit_calibration_model(samples, system_peak_w=5000.0, tz_name="UTC")
        assert model is not None
        # raw forecast zero → calibrated must remain zero
        times = [datetime(2025, 1, 1, 2, tzinfo=timezone.utc)]
        out = apply_calibration([0.0], times, model)
        assert out == [0.0]

    def test_respects_ceiling(self) -> None:
        # Train on over-optimistic samples so the model predicts > peak
        samples = []
        for i in range(MIN_TRAINING_SAMPLES * 2):
            hour = 11.0 + (i % 4) * 0.25
            # Force model to predict > peak: teach it that large forecasts
            # correspond to even larger actuals capped by telemetry.
            forecast_w = 2000.0
            samples.append(_sample(forecast_w, 6000.0, hour))
        model = fit_calibration_model(samples, system_peak_w=5000.0, tz_name="UTC")
        assert model is not None
        times = [datetime(2025, 1, 1, 11, tzinfo=timezone.utc)]
        out = apply_calibration([2000.0], times, model)
        assert out[0] <= 1.2 * 5000.0 + 1e-6


class TestTimezoneBucketing:
    def test_local_solar_hour_respects_tz(self) -> None:
        from power_master.forecast.solar_calibration import _local_solar_hour

        # 22:00 UTC on 15 Jan is 09:00 AEDT (UTC+11)
        t = datetime(2025, 1, 15, 22, 0, tzinfo=timezone.utc)
        h = _local_solar_hour(t, "Australia/Brisbane")
        # Brisbane is UTC+10 year-round → 08:00 local
        assert abs(h - 8.0) < 0.01


class TestBuildTrainingSet:
    @pytest.mark.asyncio
    async def test_pairs_near_term_forecast_with_telemetry(self, repo) -> None:
        # "now" is well after the forecast target times so every sample qualifies
        now = datetime(2025, 6, 15, 18, 0, tzinfo=timezone.utc)
        system_peak = 5000.0
        fetched = now - timedelta(hours=5)  # fetched at 13:00
        fetched_iso = fetched.isoformat()

        forecast_samples = []
        slot_starts = []
        for i in range(6):
            slot_start = fetched + timedelta(minutes=30 * (i + 1))
            slot_starts.append(slot_start)
            forecast_samples.append({
                "provider_type": "solar",
                "metric": "pv_estimate_w",
                "fetched_at": fetched_iso,
                "horizon_hours": 0.5 * (i + 1),
                "target_time": slot_start.isoformat(),
                "predicted_value": 2000.0 + i * 100,
            })
        await repo.store_forecast_samples(forecast_samples)

        for slot_start, fs in zip(slot_starts, forecast_samples):
            actual = fs["predicted_value"] * 0.6
            await repo.db.execute(
                """INSERT INTO telemetry
                   (recorded_at, soc, battery_power_w, solar_power_w, grid_power_w,
                    load_power_w, battery_voltage, battery_temp_c, inverter_mode,
                    grid_available, raw_data_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    slot_start.isoformat(), 0.5, 0, int(actual), 0, 0,
                    None, None, None, 1, None,
                ),
            )
        await repo.db.commit()

        samples = await build_training_set(
            repo, window_days=30,
            system_peak_w=system_peak, tz_name="UTC",
            reference_time=now,
        )
        assert len(samples) == 6
        for s in samples:
            # actual ≈ 0.6 × forecast
            assert abs(s.actual_w / s.forecast_w - 0.6) < 0.05

    @pytest.mark.asyncio
    async def test_rejects_samples_below_min_forecast(self, repo) -> None:
        now = datetime(2025, 6, 15, 18, 0, tzinfo=timezone.utc)
        slot_start = now - timedelta(hours=5) + timedelta(minutes=30)
        # Sub-floor forecast (50W) — should be rejected; min is max(100W, 5% of peak)
        await repo.store_forecast_samples([{
            "provider_type": "solar",
            "metric": "pv_estimate_w",
            "fetched_at": (now - timedelta(hours=1)).isoformat(),
            "horizon_hours": 0.5,
            "target_time": slot_start.isoformat(),
            "predicted_value": 50.0,
        }])
        await repo.db.execute(
            """INSERT INTO telemetry
               (recorded_at, soc, battery_power_w, solar_power_w, grid_power_w,
                load_power_w, battery_voltage, battery_temp_c, inverter_mode,
                grid_available, raw_data_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (slot_start.isoformat(), 0.5, 0, 400, 0, 0, None, None, None, 1, None),
        )
        await repo.db.commit()

        samples = await build_training_set(
            repo, window_days=30,
            system_peak_w=5000.0, tz_name="UTC",
            reference_time=now,
        )
        assert samples == []
