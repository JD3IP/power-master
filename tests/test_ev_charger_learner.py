"""Tests for EV charger learner seed (Phase 3.6, O2).

Verifies that the learn-the-charger scaffold is well-formed, typed, inert (no behaviour
change), and properly defaults to off. The actual learning algorithm is Phase 4+.
"""

from __future__ import annotations

from datetime import datetime, timezone
import pytest

from power_master.config.schema import EVConfig, AppConfig
from power_master.ev.charger_learner import ChargerLearner, LearnedChargeProfile


class TestChargerLearnerSeed:
    """Tests for the ChargerLearner stub interface and LearnedChargeProfile dataclass."""

    def test_learned_charge_profile_instantiation(self) -> None:
        """LearnedChargeProfile dataclass can be instantiated with valid fields."""
        now = datetime.now(tz=timezone.utc)
        profile = LearnedChargeProfile(
            charge_window_start="22:00",
            charge_window_end="07:00",
            expected_kwh=18.5,
            confidence=0.85,
            sample_days=10,
            observed_at=now,
        )

        assert profile.charge_window_start == "22:00"
        assert profile.charge_window_end == "07:00"
        assert profile.expected_kwh == 18.5
        assert profile.confidence == 0.85
        assert profile.sample_days == 10
        assert profile.observed_at == now

    def test_charger_learner_instantiates(self) -> None:
        """ChargerLearner can be instantiated without error."""
        learner = ChargerLearner()
        assert learner is not None

    def test_charger_learner_observe_raises_not_implemented(self) -> None:
        """ChargerLearner.observe() raises NotImplementedError (stub, Phase 4+)."""
        learner = ChargerLearner()
        telemetry_window = []

        with pytest.raises(NotImplementedError, match="not implemented"):
            learner.observe(telemetry_window)

    def test_charger_learner_infer_charger_draw_returns_zero(self) -> None:
        """ChargerLearner.infer_charger_draw_from_telemetry() returns 0 (stub)."""
        telemetry_record = {
            "recorded_at": "2026-06-16T12:00:00+10:00",
            "battery_power_w": 500,
            "grid_power_w": 1500,
        }
        load_w = 800
        battery_w = 500

        result = ChargerLearner.infer_charger_draw_from_telemetry(
            telemetry_record, load_w, battery_w
        )

        assert result == 0.0, "Stub implementation should return 0"


class TestEVConfigLearnFromTelemetry:
    """Tests for learn_from_telemetry config flag in EVConfig."""

    def test_learn_from_telemetry_defaults_false(self) -> None:
        """EVConfig.learn_from_telemetry defaults to False (opt-in)."""
        ev_config = EVConfig()
        assert ev_config.learn_from_telemetry is False

    def test_learn_from_telemetry_can_be_set_true(self) -> None:
        """EVConfig.learn_from_telemetry can be set to True without error."""
        ev_config = EVConfig(
            enabled=True,
            learn_from_telemetry=True,
            charge_windows=["22:00-07:00"],
            expected_nightly_kwh=18.0,
        )
        assert ev_config.learn_from_telemetry is True

    def test_learn_from_telemetry_in_app_config(self) -> None:
        """AppConfig can be created with learn_from_telemetry=True."""
        app_config = AppConfig(
            ev={
                "enabled": True,
                "learn_from_telemetry": True,
                "charge_windows": ["22:00-07:00"],
                "expected_nightly_kwh": 18.0,
            }
        )
        assert app_config.ev.learn_from_telemetry is True

    def test_learn_from_telemetry_serialization(self) -> None:
        """learn_from_telemetry serializes/deserializes correctly."""
        ev_config = EVConfig(
            enabled=True,
            learn_from_telemetry=True,
        )

        # Convert to dict (simulating serialization)
        config_dict = ev_config.model_dump()
        assert config_dict["learn_from_telemetry"] is True

        # Recreate from dict (simulating deserialization)
        ev_config2 = EVConfig(**config_dict)
        assert ev_config2.learn_from_telemetry is True


class TestEVForecastInertWithLearnFlag:
    """Tests proving that learn_from_telemetry flag does NOT change forecast/provisioning.

    The seam is marked in main.py._build_ev_forecast but the learning algorithm is not
    implemented (Phase 4+). This test verifies the flag is inert: forecast is identical
    whether learn_from_telemetry=True or False (given the same config values).
    """

    async def test_ev_forecast_identical_with_learn_flag_on_vs_off(self) -> None:
        """EV forecast is identical whether learn_from_telemetry is True or False.

        This proves the seed is inert: the flag changes no behaviour this milestone.
        """
        from power_master.main import Application
        from power_master.config.manager import ConfigManager
        from datetime import timedelta
        from pathlib import Path
        import tempfile

        async def make_app_with_learn_flag(learn_flag: bool) -> Application:
            """Create an Application with learn_from_telemetry flag."""
            config = AppConfig(
                ev={
                    "enabled": True,
                    "learn_from_telemetry": learn_flag,
                    "charger_kw": 3.0,
                    "charge_windows": ["22:00-07:00"],
                    "expected_nightly_kwh": 18.0,
                },
                load_profile={"timezone": "Australia/Brisbane"},
            )

            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                defaults_file = tmp_path / "defaults.yaml"
                defaults_file.write_text("db:\n  path: :memory:\n")
                config_manager = ConfigManager(
                    defaults_path=defaults_file,
                    user_path=tmp_path / "user.yaml",
                )

            return Application(config, config_manager)

        # Create two apps: one with learn_from_telemetry=False, one=True
        app_off = await make_app_with_learn_flag(False)
        app_on = await make_app_with_learn_flag(True)

        # Same time window for both
        anchor = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)  # midnight Brisbane
        slot_starts = [anchor + timedelta(minutes=30 * i) for i in range(16)]  # 8 hours
        n_slots = len(slot_starts)

        # Get forecasts
        forecast_off = await app_off._build_ev_forecast(slot_starts, n_slots)
        forecast_on = await app_on._build_ev_forecast(slot_starts, n_slots)

        # Assert they are identical (flag is inert this milestone)
        assert len(forecast_off) == len(forecast_on)
        for i, (w_off, w_on) in enumerate(zip(forecast_off, forecast_on)):
            assert abs(w_off - w_on) < 0.01, (
                f"Slot {i}: learn_from_telemetry should be inert. "
                f"got {w_off}W (flag=False) vs {w_on}W (flag=True)"
            )
