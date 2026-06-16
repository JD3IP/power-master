"""Tests for rebuild evaluator rebuild cadence gating (TOU vs. Amber).

Tests validate that forecast staleness and actuals deviation triggers are:
- Gated OFF for TOU tariffs (stable, expected ~3-6h solar forecast cadence)
- Gated ON for Amber/spot tariffs (dynamic pricing needs high-resolution reactivity)
- Configurable via explicit PlanningConfig fields (override type defaults)
- Safety nets (soc_deviation, periodic, plan_expired) always active
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock

import pytest

from power_master.config.schema import AppConfig
from power_master.optimisation.plan import OptimisationPlan, PlanSlot, SlotMode
from power_master.optimisation.rebuild_evaluator import RebuildEvaluator, RebuildResult


# ── Helpers ──────────────────────────────────────────────────

def _now() -> datetime:
    """Fixed anchor: 2026-06-16 02:00 UTC = Brisbane 12:00 (noon)."""
    return datetime(2026, 6, 16, 2, 0, tzinfo=timezone.utc)


def _make_tou_config() -> AppConfig:
    """Create a TOU tariff config."""
    from datetime import date

    return AppConfig(
        providers={
            "tariff": {
                "type": "tou",
                "timezone": "Australia/Brisbane",
                "plan": {
                    "supply_charge_c_per_day": 148.5,
                    "billing_cycle": {
                        "length_days": 28,
                        "anchor_date": date(2026, 6, 1),
                    },
                    "versions": [
                        {
                            "valid_from": date(2026, 6, 1),
                            "import_bands": [
                                {
                                    "descriptor": "shoulder",
                                    "windows": [],
                                    "rate_c_per_kwh": 34.1,
                                }
                            ],
                        }
                    ],
                },
            }
        }
    )


def _make_amber_config() -> AppConfig:
    """Create an Amber tariff config."""
    return AppConfig(
        providers={
            "tariff": {
                "type": "amber",
                "api_key": "test-key",
                "site_id": "test-site",
            }
        }
    )


def _make_plan(
    start: datetime = None,
    n_slots: int = 8,
    solar_forecast_w: float = 2000.0,
    load_forecast_w: float = 1500.0,
    import_rate_cents: float = 20.0,
) -> OptimisationPlan:
    """Create a minimal test plan with slots that contain the current time.

    Slots are anchored around now() so get_current_slot() will find a valid slot.
    """
    if start is None:
        # Start from a slot that includes "now"
        now = datetime.now(timezone.utc)
        # Round down to nearest 30-minute boundary
        start = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)

    slots = []
    for i in range(n_slots):
        slot_start = start + timedelta(minutes=30 * i)
        slot_end = slot_start + timedelta(minutes=30)
        slot = PlanSlot(
            index=i,
            start=slot_start,
            end=slot_end,
            mode=SlotMode.SELF_USE,
            expected_soc=0.5 + 0.01 * i,
            solar_forecast_w=solar_forecast_w,
            load_forecast_w=load_forecast_w,
            import_rate_cents=import_rate_cents,
            export_rate_cents=5.0,
            target_power_w=0,
        )
        slots.append(slot)

    horizon_end = start + timedelta(hours=4)
    return OptimisationPlan(
        version=1,
        created_at=start,
        horizon_start=start,
        horizon_end=horizon_end,
        slots=slots,
        trigger_reason="initial",
        objective_score=0.0,
        solver_time_ms=0,
        metrics={},
    )


def _make_aggregator(stale: bool = False) -> Mock:
    """Create a mock ForecastAggregator."""
    agg = Mock()
    agg.is_stale = Mock(return_value=stale)
    agg.spike_detector = Mock(is_spike_active=False)
    agg.state = Mock(storm_probability=0.0, tariff=None)
    return agg


# ═══════════════════════════════════════════════════════════════
# Config defaults resolution tests
# ═══════════════════════════════════════════════════════════════


class TestConfigDefaultsResolution:
    """Verify that PlanningConfig fields are resolved by tariff type."""

    def test_tou_config_resolves_rebuild_flags_false(self) -> None:
        """TOU config should resolve rebuild flags to False."""
        config = _make_tou_config()
        assert config.planning.rebuild_on_forecast_staleness is False
        assert config.planning.rebuild_on_actuals_deviation is False
        assert config.planning.mode_switch_hysteresis_cents == 3.0

    def test_amber_config_resolves_rebuild_flags_true(self) -> None:
        """Amber config should resolve rebuild flags to True."""
        config = _make_amber_config()
        assert config.planning.rebuild_on_forecast_staleness is True
        assert config.planning.rebuild_on_actuals_deviation is True
        assert config.planning.mode_switch_hysteresis_cents == 0.0

    def test_explicit_tou_values_preserved(self) -> None:
        """Explicit non-None values should override TOU defaults."""
        config = _make_tou_config()
        # Manually set explicit values before validation (simulate user override)
        config.planning.rebuild_on_forecast_staleness = True
        config.planning.rebuild_on_actuals_deviation = True
        config.planning.mode_switch_hysteresis_cents = 1.5

        # Values should be preserved
        assert config.planning.rebuild_on_forecast_staleness is True
        assert config.planning.rebuild_on_actuals_deviation is True
        assert config.planning.mode_switch_hysteresis_cents == 1.5

    def test_explicit_amber_values_preserved(self) -> None:
        """Explicit non-None values on Amber should override defaults."""
        config = _make_amber_config()
        config.planning.rebuild_on_forecast_staleness = False
        config.planning.rebuild_on_actuals_deviation = False
        config.planning.mode_switch_hysteresis_cents = 5.0

        assert config.planning.rebuild_on_forecast_staleness is False
        assert config.planning.rebuild_on_actuals_deviation is False
        assert config.planning.mode_switch_hysteresis_cents == 5.0


# ═══════════════════════════════════════════════════════════════
# TOU rebuild gating tests
# ═══════════════════════════════════════════════════════════════


class TestTOURebuilds:
    """TOU tariffs should NOT rebuild on stale forecast or actuals deviation."""

    def test_tou_no_rebuild_on_stale_forecast(self) -> None:
        """TOU evaluator should skip rebuild when only forecast is stale."""
        import time
        config = _make_tou_config()
        evaluator = RebuildEvaluator(config)

        plan = _make_plan()
        aggregator = _make_aggregator(stale=True)

        # Mark rebuild time to recent (skip periodic interval)
        recent_time = time.monotonic()
        evaluator._last_rebuild_time = recent_time

        result = evaluator.evaluate(
            current_plan=plan,
            current_soc=0.5,
            aggregator=aggregator,
            manual_override_active=False,
        )

        # Should NOT rebuild for forecast staleness (gated off for TOU)
        assert result.should_rebuild is False
        assert result.trigger != "forecast_delta"

    def test_tou_no_rebuild_on_actuals_deviation(self) -> None:
        """TOU evaluator should skip rebuild when solar/load deviates."""
        import time
        config = _make_tou_config()
        evaluator = RebuildEvaluator(config)

        plan = _make_plan(solar_forecast_w=2000.0, load_forecast_w=1500.0)
        aggregator = _make_aggregator(stale=False)

        # Mark rebuild time to recent (skip periodic interval)
        recent_time = time.monotonic()
        evaluator._last_rebuild_time = recent_time

        # Huge actuals deviation (50% off)
        result = evaluator.evaluate(
            current_plan=plan,
            current_soc=0.5,
            aggregator=aggregator,
            manual_override_active=False,
            actual_solar_w=1000.0,  # 50% below forecast
            actual_load_w=3000.0,   # 100% above forecast
        )

        # Should NOT rebuild despite large deviations (gated off for TOU)
        assert result.should_rebuild is False
        assert result.trigger != "actuals_deviation"

    def test_tou_safety_net_soc_deviation_fires(self) -> None:
        """TOU should still rebuild on SOC deviation (safety net)."""
        import time
        config = _make_tou_config()
        evaluator = RebuildEvaluator(config)

        plan = _make_plan()
        aggregator = _make_aggregator(stale=False)

        # Mark rebuild times to recent (skip periodic interval and cooldowns)
        recent_time = time.monotonic()
        evaluator._last_rebuild_time = recent_time
        evaluator._last_soc_rebuild_time = recent_time - config.planning.soc_deviation_cooldown_seconds - 1

        # SOC deviates beyond tolerance
        result = evaluator.evaluate(
            current_plan=plan,
            current_soc=0.3,  # 20% below expected 0.5
            aggregator=aggregator,
            manual_override_active=False,
        )

        # Should rebuild due to SOC deviation (safety net always active)
        assert result.should_rebuild is True
        assert result.trigger == "soc_deviation"

    def test_tou_periodic_rebuild_still_fires(self) -> None:
        """TOU should rebuild on periodic interval (safety net)."""
        config = _make_tou_config()
        evaluator = RebuildEvaluator(config)

        plan = _make_plan()
        aggregator = _make_aggregator(stale=False)

        # Force periodic interval to have elapsed
        import time
        old_time = time.monotonic() - config.planning.periodic_rebuild_interval_seconds - 10
        evaluator._last_rebuild_time = old_time

        result = evaluator.evaluate(
            current_plan=plan,
            current_soc=0.5,
            aggregator=aggregator,
            manual_override_active=False,
        )

        # Should rebuild due to periodic interval
        assert result.should_rebuild is True
        assert result.trigger == "periodic"


# ═══════════════════════════════════════════════════════════════
# Amber rebuild gating tests
# ═══════════════════════════════════════════════════════════════


class TestAmberRebuilds:
    """Amber tariffs should rebuild on stale forecast and actuals deviation."""

    def test_amber_rebuilds_on_stale_forecast(self) -> None:
        """Amber evaluator should rebuild when forecast is stale."""
        import time
        config = _make_amber_config()
        evaluator = RebuildEvaluator(config)

        plan = _make_plan()
        aggregator = _make_aggregator(stale=True)

        # Mark rebuild time to recent (skip periodic interval)
        recent_time = time.monotonic()
        evaluator._last_rebuild_time = recent_time

        result = evaluator.evaluate(
            current_plan=plan,
            current_soc=0.5,
            aggregator=aggregator,
            manual_override_active=False,
        )

        # Should rebuild for forecast staleness (enabled for Amber)
        assert result.should_rebuild is True
        assert result.trigger == "forecast_delta"

    def test_amber_rebuilds_on_actuals_deviation(self) -> None:
        """Amber evaluator should rebuild when solar/load deviates."""
        import time
        config = _make_amber_config()
        evaluator = RebuildEvaluator(config)

        plan = _make_plan(solar_forecast_w=2000.0, load_forecast_w=1500.0)
        aggregator = _make_aggregator(stale=False)

        # Mark rebuild and actuals times to recent (skip periodic interval and cooldowns)
        recent_time = time.monotonic()
        evaluator._last_rebuild_time = recent_time
        evaluator._last_actuals_rebuild_time = recent_time - config.planning.soc_deviation_cooldown_seconds - 1

        # Large solar deviation (50% above forecast)
        result = evaluator.evaluate(
            current_plan=plan,
            current_soc=0.5,
            aggregator=aggregator,
            manual_override_active=False,
            actual_solar_w=3000.0,  # 50% above forecast
        )

        # Should rebuild due to actuals deviation (enabled for Amber)
        assert result.should_rebuild is True
        assert result.trigger == "actuals_deviation"

    def test_amber_rebuilds_on_load_deviation(self) -> None:
        """Amber should rebuild on load actuals deviation too."""
        import time
        config = _make_amber_config()
        evaluator = RebuildEvaluator(config)

        plan = _make_plan(load_forecast_w=1500.0)
        aggregator = _make_aggregator(stale=False)

        recent_time = time.monotonic()
        evaluator._last_rebuild_time = recent_time
        evaluator._last_actuals_rebuild_time = recent_time - config.planning.soc_deviation_cooldown_seconds - 1

        # Large load deviation (40% below forecast)
        result = evaluator.evaluate(
            current_plan=plan,
            current_soc=0.5,
            aggregator=aggregator,
            manual_override_active=False,
            actual_load_w=900.0,  # 40% below forecast
        )

        assert result.should_rebuild is True
        assert result.trigger == "actuals_deviation"


# ═══════════════════════════════════════════════════════════════
# Mixed tests (edge cases)
# ═══════════════════════════════════════════════════════════════


class TestMixedBehaviours:
    """Test edge cases and mixed trigger scenarios."""

    def test_tou_respects_explicit_true_override(self) -> None:
        """TOU config with explicit rebuild_on_forecast_staleness=True should allow staleness rebuilds."""
        import time
        config = _make_tou_config()
        config.planning.rebuild_on_forecast_staleness = True
        evaluator = RebuildEvaluator(config)

        plan = _make_plan()
        aggregator = _make_aggregator(stale=True)
        recent_time = time.monotonic()
        evaluator._last_rebuild_time = recent_time

        result = evaluator.evaluate(
            current_plan=plan,
            current_soc=0.5,
            aggregator=aggregator,
        )

        # Should rebuild because we overrode the TOU default
        assert result.should_rebuild is True
        assert result.trigger == "forecast_delta"

    def test_amber_respects_explicit_false_override(self) -> None:
        """Amber config with explicit rebuild_on_actuals_deviation=False should suppress actuals rebuilds."""
        import time
        config = _make_amber_config()
        config.planning.rebuild_on_actuals_deviation = False
        evaluator = RebuildEvaluator(config)

        plan = _make_plan(solar_forecast_w=2000.0)
        aggregator = _make_aggregator(stale=False)
        recent_time = time.monotonic()
        evaluator._last_rebuild_time = recent_time

        result = evaluator.evaluate(
            current_plan=plan,
            current_soc=0.5,
            aggregator=aggregator,
            actual_solar_w=3000.0,  # Large deviation
        )

        # Should NOT rebuild because we overrode the Amber default
        assert result.should_rebuild is False
        assert result.trigger != "actuals_deviation"

    def test_small_deviation_below_threshold(self) -> None:
        """Deviations below forecast_delta_threshold_pct should not trigger rebuild."""
        import time
        config = _make_amber_config()
        evaluator = RebuildEvaluator(config)

        plan = _make_plan(solar_forecast_w=2000.0)
        aggregator = _make_aggregator(stale=False)
        recent_time = time.monotonic()
        evaluator._last_rebuild_time = recent_time

        # 5% deviation (threshold is 15% by default)
        actual_solar = 2000.0 * 1.05  # 5% higher
        result = evaluator.evaluate(
            current_plan=plan,
            current_soc=0.5,
            aggregator=aggregator,
            actual_solar_w=actual_solar,
        )

        # Should NOT rebuild (deviation below threshold)
        assert result.should_rebuild is False
        assert result.trigger != "actuals_deviation"
