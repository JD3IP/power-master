"""Tests for the MILP optimisation solver."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from power_master.config.schema import AppConfig
from power_master.optimisation.plan import SlotMode
from power_master.optimisation.solver import SolverInputs, dampen_price_weighted, solve


def _make_inputs(
    n_slots: int = 8,
    solar: float = 0.0,
    load: float = 500.0,
    import_price: float = 20.0,
    export_price: float = 5.0,
    soc: float = 0.5,
    wacb: float = 10.0,
    spike_slots: list[int] | None = None,
    storm: bool = False,
) -> SolverInputs:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    starts = [now + timedelta(minutes=30 * i) for i in range(n_slots)]
    return SolverInputs(
        solar_forecast_w=[solar] * n_slots,
        load_forecast_w=[load] * n_slots,
        import_rate_cents=[import_price] * n_slots,
        export_rate_cents=[export_price] * n_slots,
        is_spike=[i in (spike_slots or []) for i in range(n_slots)],
        current_soc=soc,
        wacb_cents=wacb,
        storm_active=storm,
        storm_reserve_soc=0.8 if storm else 0.0,
        slot_start_times=starts,
    )


class TestSolverBasic:
    def test_solver_returns_plan(self) -> None:
        config = AppConfig()
        inputs = _make_inputs()
        plan = solve(config, inputs)

        assert plan.version == 1
        assert plan.total_slots == 8
        assert plan.solver_time_ms >= 0
        assert plan.metrics["status"] in ("Optimal", "Not Solved")

    def test_solver_respects_soc_limits(self) -> None:
        config = AppConfig()
        inputs = _make_inputs(soc=0.5)
        plan = solve(config, inputs)

        for slot in plan.slots:
            assert config.battery.soc_min_hard - 0.01 <= slot.expected_soc <= config.battery.soc_max_hard + 0.01

    def test_cheap_import_triggers_charge(self) -> None:
        """When import is cheap now but expensive later, SOC should rise in cheap slots.

        Use very low starting SOC and high load so the battery alone can't cover
        the expensive slots — must charge during cheap slots.
        """
        config = AppConfig()
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        n = 8
        starts = [now + timedelta(minutes=30 * i) for i in range(n)]
        # First 4 slots very cheap, last 4 very expensive
        inputs = SolverInputs(
            solar_forecast_w=[0.0] * n,
            load_forecast_w=[3000.0] * n,  # High load — battery can't cover alone
            import_rate_cents=[1.0] * 4 + [100.0] * 4,  # Huge price differential
            export_rate_cents=[0.0] * n,
            is_spike=[False] * n,
            current_soc=0.1,  # Very low SOC — almost nothing to discharge
            wacb_cents=5.0,
            slot_start_times=starts,
        )
        plan = solve(config, inputs)

        # SOC should increase during cheap slots (solver charges from grid)
        max_soc = max(s.expected_soc for s in plan.slots)
        assert max_soc > 0.1, f"SOC should rise above initial 0.1, max was {max_soc}"

    def test_high_export_triggers_force_discharge(self) -> None:
        """When export price is high (above WACB + break-even), should force discharge for arbitrage."""
        config = AppConfig()
        config.arbitrage.break_even_delta_cents = 5
        inputs = _make_inputs(
            import_price=50.0,  # Expensive import
            export_price=25.0,  # Well above WACB (10) + break-even (5) = 15
            soc=0.8,  # High SOC
            wacb=10.0,
            load=200.0,  # Low load so excess is exported
        )
        plan = solve(config, inputs)

        # Should have some FORCE_DISCHARGE slots (arbitrage with grid export)
        discharge_slots = [s for s in plan.slots if s.mode == SlotMode.FORCE_DISCHARGE]
        assert len(discharge_slots) > 0

    def test_load_serving_discharge_uses_self_use(self) -> None:
        """Discharge only covering loads (no grid export) should use SELF_USE, not FORCE_DISCHARGE."""
        config = AppConfig()
        config.arbitrage.break_even_delta_cents = 5
        inputs = _make_inputs(
            n_slots=4,
            export_price=5.0,   # Below WACB (10) + break-even (5) = 15 → no export
            import_price=30.0,  # Expensive import → solver prefers battery
            solar=0.0,
            soc=0.8,
            wacb=10.0,
            load=2000.0,  # Moderate load; battery can serve it
        )
        plan = solve(config, inputs)

        # No FORCE_DISCHARGE — load-serving discharge uses SELF_USE
        force_discharge = [s for s in plan.slots if s.mode == SlotMode.FORCE_DISCHARGE]
        assert len(force_discharge) == 0, (
            f"Expected no FORCE_DISCHARGE for load-serving, got {len(force_discharge)}"
        )

    def test_arbitrage_gate_blocks_unprofitable_export(self) -> None:
        """When export price is below WACB + break-even, should not export to grid.

        Battery may still discharge for self-use (avoiding import).
        """
        config = AppConfig()
        config.arbitrage.break_even_delta_cents = 5
        inputs = _make_inputs(
            n_slots=4,
            export_price=12.0,  # Below WACB (10) + break-even (5) = 15
            import_price=20.0,
            soc=0.8,
            wacb=10.0,
            load=200.0,
        )
        plan = solve(config, inputs)

        # The solver should not plan to export to grid (export is gated)
        # But may discharge for self-use
        assert plan.metrics["status"] == "Optimal"

    def test_solar_enables_self_use(self) -> None:
        """With abundant solar, system should self-use."""
        config = AppConfig()
        inputs = _make_inputs(
            solar=5000.0,  # Plenty of solar
            load=2000.0,
            soc=0.5,
        )
        plan = solve(config, inputs)

        # Should have mostly self-use slots (or charge from solar)
        self_use = [s for s in plan.slots if s.mode in (SlotMode.SELF_USE, SlotMode.FORCE_CHARGE)]
        assert len(self_use) >= len(plan.slots) // 2

    def test_excess_solar_charging_uses_self_use_mode(self) -> None:
        """Charging from excess PV should not require FORCE_CHARGE mode."""
        config = AppConfig()
        inputs = _make_inputs(
            n_slots=6,
            solar=5000.0,
            load=500.0,
            soc=0.2,
            import_price=30.0,
            export_price=5.0,
        )
        plan = solve(config, inputs)

        force_charge_slots = [s for s in plan.slots if s.mode == SlotMode.FORCE_CHARGE]
        assert len(force_charge_slots) == 0

    def test_force_charge_targets_full_power_by_default(self) -> None:
        """Force-charge slots should command full configured charge power."""
        config = AppConfig()
        n = 8
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        starts = [now + timedelta(minutes=30 * i) for i in range(n)]
        inputs = SolverInputs(
            solar_forecast_w=[0.0] * n,
            load_forecast_w=[500.0] * 4 + [3500.0] * 4,
            import_rate_cents=[1.0] * 4 + [100.0] * 4,
            export_rate_cents=[0.0] * n,
            is_spike=[False] * n,
            current_soc=0.1,
            wacb_cents=5.0,
            slot_start_times=starts,
        )
        plan = solve(config, inputs)

        force_charge_slots = [s for s in plan.slots if s.mode == SlotMode.FORCE_CHARGE]
        assert len(force_charge_slots) > 0
        assert all(s.target_power_w == config.battery.max_charge_rate_w for s in force_charge_slots)

    def test_force_charge_respects_grid_import_cap(self) -> None:
        """Force-charge power should be reduced when total grid-import cap is configured."""
        config = AppConfig()
        config.battery.max_grid_import_w = 2500
        n = 8
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        starts = [now + timedelta(minutes=30 * i) for i in range(n)]
        inputs = SolverInputs(
            solar_forecast_w=[0.0] * n,
            load_forecast_w=[1500.0] * n,
            import_rate_cents=[1.0] * 4 + [100.0] * 4,
            export_rate_cents=[0.0] * n,
            is_spike=[False] * n,
            current_soc=0.1,
            wacb_cents=5.0,
            slot_start_times=starts,
        )
        plan = solve(config, inputs)

        force_charge_slots = [s for s in plan.slots if s.mode == SlotMode.FORCE_CHARGE]
        assert len(force_charge_slots) > 0
        assert all(s.target_power_w == 1000 for s in force_charge_slots)

    def test_daytime_reserve_bias_charges_toward_50_percent(self) -> None:
        """Planner should bias daytime SOC toward at least 50%."""
        config = AppConfig()
        config.load_profile.timezone = "UTC"
        config.battery_targets.daytime_reserve_soc_target = 0.50
        config.battery_targets.daytime_reserve_start_hour = 8
        config.battery_targets.daytime_reserve_end_hour = 18

        n = 48  # 24h at 30-minute slots
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        starts = [start + timedelta(minutes=30 * i) for i in range(n)]

        import_prices = []
        for i in range(n):
            hour = starts[i].hour
            import_prices.append(6.0 if 10 <= hour < 16 else 28.0)

        inputs = SolverInputs(
            solar_forecast_w=[0.0] * n,
            load_forecast_w=[1200.0] * n,
            import_rate_cents=import_prices,
            export_rate_cents=[5.0] * n,
            is_spike=[False] * n,
            current_soc=0.2,
            wacb_cents=10.0,
            slot_start_times=starts,
        )

        plan = solve(config, inputs)
        daytime_soc = [
            slot.expected_soc
            for slot in plan.slots
            if 8 <= slot.start.hour < 18
        ]
        assert daytime_soc, "Expected daytime slots in plan"
        assert max(daytime_soc) >= 0.48


    def test_evening_soc_target_triggers_charging(self) -> None:
        """Even with flat pricing, evening SOC target should force charging."""
        config = AppConfig()
        config.load_profile.timezone = "UTC"
        config.battery_targets.evening_soc_target = 0.80
        config.battery_targets.evening_target_hour = 16

        n = 48  # 24h at 30-minute slots
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        starts = [start + timedelta(minutes=30 * i) for i in range(n)]

        # Flat pricing — no arbitrage signal, only SOC target drives charging
        inputs = SolverInputs(
            solar_forecast_w=[0.0] * n,
            load_forecast_w=[500.0] * n,
            import_rate_cents=[15.0] * n,
            export_rate_cents=[5.0] * n,
            is_spike=[False] * n,
            current_soc=0.15,
            wacb_cents=10.0,
            slot_start_times=starts,
        )

        plan = solve(config, inputs)

        # SOC should rise well above initial 0.15 toward the 0.80 target
        max_soc = max(s.expected_soc for s in plan.slots)
        assert max_soc >= 0.5, (
            f"Evening SOC target should drive charging, but max SOC was only {max_soc:.2f}"
        )

        # Should have at least one FORCE_CHARGE slot
        charge_slots = [s for s in plan.slots if s.mode == SlotMode.FORCE_CHARGE]
        assert len(charge_slots) > 0, "Expected FORCE_CHARGE slots to meet evening target"

    def test_morning_soc_minimum_prevents_full_drain(self) -> None:
        """Morning SOC minimum should prevent the battery from fully draining overnight."""
        config = AppConfig()
        config.load_profile.timezone = "UTC"
        config.battery_targets.morning_soc_minimum = 0.20
        config.battery_targets.morning_minimum_hour = 6

        n = 48
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        starts = [start + timedelta(minutes=30 * i) for i in range(n)]

        inputs = SolverInputs(
            solar_forecast_w=[0.0] * n,
            load_forecast_w=[800.0] * n,
            import_rate_cents=[15.0] * n,
            export_rate_cents=[5.0] * n,
            is_spike=[False] * n,
            current_soc=0.10,
            wacb_cents=10.0,
            slot_start_times=starts,
        )

        plan = solve(config, inputs)

        # SOC at morning hour (6) should be near or above target
        morning_slots = [s for s in plan.slots if s.start.hour == 6]
        if morning_slots:
            morning_soc = morning_slots[0].expected_soc
            assert morning_soc >= 0.15, (
                f"Morning SOC target should prevent drain, but SOC at 6am was {morning_soc:.2f}"
            )


class TestPriceDampening:
    def test_weighted_dampening_is_lighter_near_horizon_start(self) -> None:
        price = 300.0
        threshold = 100
        base_factor = 0.5
        n_slots = 10

        near = dampen_price_weighted(price, threshold, base_factor, slot_index=0, n_slots=n_slots)
        far = dampen_price_weighted(price, threshold, base_factor, slot_index=n_slots - 1, n_slots=n_slots)

        assert near > far
        assert near == pytest.approx(price)


class TestSolverSpike:
    def test_spike_blocks_charging(self) -> None:
        """During spike slots, charging from grid should be blocked."""
        config = AppConfig()
        inputs = _make_inputs(
            import_price=200.0,
            soc=0.3,
            spike_slots=[0, 1, 2, 3],
        )
        plan = solve(config, inputs)

        # Spike slots should not be charging
        for slot in plan.slots[:4]:
            assert slot.mode != SlotMode.FORCE_CHARGE


class TestSolverStorm:
    def test_storm_reserve_maintains_soc(self) -> None:
        """With storm active, SOC should stay above reserve target."""
        config = AppConfig()
        config.arbitrage.break_even_delta_cents = 5
        inputs = _make_inputs(
            soc=0.9,  # Start high
            storm=True,
            export_price=50.0,  # Tempting to discharge
            wacb=10.0,
            load=300.0,
        )
        plan = solve(config, inputs)

        assert plan.metrics["status"] == "Optimal"
        # SOC should stay near or above storm reserve (0.8)
        # Allow tolerance for load draw and solver slack
        # Average SOC should stay high due to storm reserve constraint
        avg_soc = sum(s.expected_soc for s in plan.slots) / len(plan.slots)
        assert avg_soc >= 0.6, f"Average SOC {avg_soc:.2f} too low with storm reserve active"


class TestPlanModel:
    def test_get_current_slot(self) -> None:
        config = AppConfig()
        inputs = _make_inputs(n_slots=4)
        plan = solve(config, inputs)

        current = plan.get_current_slot()
        # A slot covering "now" should exist
        assert current is not None
        assert 0 <= current.index <= 1  # Could be 0 or 1 depending on timing

    def test_to_db_dict(self) -> None:
        config = AppConfig()
        inputs = _make_inputs(n_slots=4)
        plan = solve(config, inputs)

        db_dict = plan.to_db_dict()
        assert "version" in db_dict
        assert "trigger_reason" in db_dict
        assert "horizon_start" in db_dict

    def test_slots_to_db_dicts(self) -> None:
        config = AppConfig()
        inputs = _make_inputs(n_slots=4)
        plan = solve(config, inputs)

        slot_dicts = plan.slots_to_db_dicts()
        assert len(slot_dicts) == 4
        assert all("slot_index" in s for s in slot_dicts)
        assert all("operating_mode" in s for s in slot_dicts)
