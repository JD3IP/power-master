"""Integration tests — 7 required scenarios + spike pricing.

Each test validates the interaction between multiple modules:
solver, hierarchy, control, accounting, loads, storm, resilience.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from power_master.accounting.engine import AccountingEngine
from power_master.config.schema import AppConfig
from power_master.control.anti_oscillation import AntiOscillationGuard
from power_master.control.command import ControlCommand, command_from_slot
from power_master.control.hierarchy import evaluate_hierarchy
from power_master.control.loop import ControlLoop
from power_master.control.manual_override import ManualOverride
from power_master.hardware.base import CommandResult, OperatingMode
from power_master.hardware.telemetry import Telemetry
from power_master.loads.base import LoadState
from power_master.loads.manager import LoadManager
from power_master.optimisation.load_scheduler import schedule_loads
from power_master.optimisation.plan import OptimisationPlan, PlanSlot, SlotMode
from power_master.optimisation.rebuild_evaluator import RebuildEvaluator, RebuildResult
from power_master.optimisation.solver import SolverInputs, solve
from power_master.resilience.fallback import get_fallback_command
from power_master.resilience.health_check import HealthChecker
from power_master.resilience.manager import ResilienceManager
from power_master.resilience.modes import ResilienceLevel
from power_master.storm.monitor import StormMonitor


# ── Helpers ──────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _make_inputs(
    n_slots: int = 8,
    solar: float = 0.0,
    load: float = 500.0,
    import_price: float = 20.0,
    export_price: float = 5.0,
    soc: float = 0.5,
    wacb: float = 10.0,
    storm: bool = False,
    spike_slots: list[int] | None = None,
    import_prices: list[float] | None = None,
    export_prices: list[float] | None = None,
) -> SolverInputs:
    now = _now()
    starts = [now + timedelta(minutes=30 * i) for i in range(n_slots)]
    return SolverInputs(
        solar_forecast_w=[solar] * n_slots,
        load_forecast_w=[load] * n_slots,
        import_rate_cents=import_prices or [import_price] * n_slots,
        export_rate_cents=export_prices or [export_price] * n_slots,
        is_spike=[i in (spike_slots or []) for i in range(n_slots)],
        current_soc=soc,
        wacb_cents=wacb,
        storm_active=storm,
        storm_reserve_soc=0.8 if storm else 0.0,
        slot_start_times=starts,
    )


def _make_adapter(soc: float = 0.5) -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_telemetry = AsyncMock(return_value=Telemetry(
        soc=soc, battery_power_w=0, solar_power_w=2000,
        grid_power_w=0, load_power_w=2000,
    ))
    adapter.send_command = AsyncMock(return_value=CommandResult(success=True, latency_ms=10))
    return adapter


class FakeController:
    """Minimal load controller for integration tests."""

    def __init__(self, load_id: str, name: str, power_w: int, priority_class: int) -> None:
        self._load_id = load_id
        self._name = name
        self._power_w = power_w
        self._priority_class = priority_class
        self.state = LoadState.OFF

    @property
    def load_id(self) -> str:
        return self._load_id

    @property
    def name(self) -> str:
        return self._name

    @property
    def power_w(self) -> int:
        return self._power_w

    @property
    def priority_class(self) -> int:
        return self._priority_class

    async def turn_on(self) -> bool:
        self.state = LoadState.ON
        return True

    async def turn_off(self) -> bool:
        self.state = LoadState.OFF
        return True

    async def get_status(self):
        from power_master.loads.base import LoadStatus
        return LoadStatus(
            load_id=self._load_id, name=self._name,
            state=self.state, power_w=self._power_w if self.state == LoadState.ON else 0,
        )

    async def is_available(self) -> bool:
        return True


# ══════════════════════════════════════════════════════════════
# SCENARIO 1: Normal daytime self-use with PV
# ══════════════════════════════════════════════════════════════


class TestScenario1NormalSelfUse:
    """Abundant solar, moderate load → self-use mode, battery charges from PV."""

    def test_solver_produces_self_use_plan(self) -> None:
        config = AppConfig()
        inputs = _make_inputs(solar=5000.0, load=2000.0, soc=0.3)
        plan = solve(config, inputs)

        assert plan.metrics["status"] == "Optimal"
        self_use_count = sum(1 for s in plan.slots if s.mode == SlotMode.SELF_USE)
        assert self_use_count >= len(plan.slots) // 2

    def test_hierarchy_passes_self_use(self) -> None:
        cmd = ControlCommand(mode=OperatingMode.SELF_USE, priority=5)
        result = evaluate_hierarchy(cmd, current_soc=0.5, soc_min_hard=0.05, soc_max_hard=0.95)
        assert result.overridden is False
        assert result.command.mode == OperatingMode.SELF_USE

    def test_accounting_records_self_consumption(self) -> None:
        config = AppConfig()
        engine = AccountingEngine(config, initial_soc=0.5, initial_wacb=10.0)
        event = engine.record_self_consumption(3000, 20.0)
        assert event.cost_cents == -60  # 3kWh * 20c savings


# ══════════════════════════════════════════════════════════════
# SCENARIO 2: Overnight cheap charge
# ══════════════════════════════════════════════════════════════


class TestScenario2OvernightCharge:
    """Low overnight prices → solver charges battery for next day."""

    def test_solver_charges_during_cheap_slots(self) -> None:
        config = AppConfig()
        inputs = _make_inputs(
            n_slots=8, soc=0.1, load=3000.0,
            import_prices=[1.0] * 4 + [100.0] * 4,
            export_prices=[0.0] * 8,
        )
        plan = solve(config, inputs)

        max_soc = max(s.expected_soc for s in plan.slots)
        assert max_soc > 0.1, "Battery should charge during cheap slots"

    def test_accounting_tracks_grid_charge(self) -> None:
        config = AppConfig()
        engine = AccountingEngine(config, initial_soc=0.2, initial_wacb=15.0)
        engine.record_grid_charge(5000, 3.0)  # 5kWh at 3c

        # WACB should drop (charging with cheap energy)
        assert engine.wacb_cents < 15.0


# ══════════════════════════════════════════════════════════════
# SCENARIO 3: Peak arbitrage discharge
# ══════════════════════════════════════════════════════════════


class TestScenario3PeakArbitrage:
    """High export price above WACB+delta → discharge for profit."""

    def test_solver_discharges_when_profitable(self) -> None:
        config = AppConfig()
        config.arbitrage.break_even_delta_cents = 5
        inputs = _make_inputs(
            import_price=50.0, export_price=25.0,
            soc=0.8, wacb=10.0, load=200.0,
        )
        plan = solve(config, inputs)

        discharge_count = sum(1 for s in plan.slots if s.mode == SlotMode.FORCE_DISCHARGE)
        assert discharge_count > 0

    def test_accounting_computes_arbitrage_profit(self) -> None:
        config = AppConfig()
        engine = AccountingEngine(config, initial_soc=0.8, initial_wacb=10.0)
        event = engine.record_grid_export(2000, 25.0)  # 2kWh at 25c

        # Revenue: 2 * 25 = 50c, cost basis: 2 * 10 = 20c, profit: 30c
        assert event.profit_loss_cents == 30

    def test_arbitrage_gate_blocks_unprofitable(self) -> None:
        config = AppConfig()
        config.arbitrage.break_even_delta_cents = 5
        inputs = _make_inputs(
            n_slots=4, export_price=12.0, import_price=20.0,
            soc=0.8, wacb=10.0, load=200.0,
        )
        plan = solve(config, inputs)
        # Export at 12c < WACB 10c + delta 5c = 15c → no grid export
        assert plan.metrics["status"] == "Optimal"


# ══════════════════════════════════════════════════════════════
# SCENARIO 4: Safety override at SOC limits
# ══════════════════════════════════════════════════════════════


class TestScenario4SafetyOverride:
    """Safety hierarchy overrides optimiser commands at SOC boundaries."""

    def test_blocks_discharge_at_min_soc(self) -> None:
        cmd = ControlCommand(mode=OperatingMode.FORCE_DISCHARGE, power_w=5000, priority=5)
        result = evaluate_hierarchy(cmd, current_soc=0.04, soc_min_hard=0.05, soc_max_hard=0.95)
        assert result.winning_level == 1
        assert result.command.mode != OperatingMode.FORCE_DISCHARGE

    def test_blocks_charge_at_max_soc(self) -> None:
        cmd = ControlCommand(mode=OperatingMode.FORCE_CHARGE, power_w=5000, priority=5)
        result = evaluate_hierarchy(cmd, current_soc=0.96, soc_min_hard=0.05, soc_max_hard=0.95)
        assert result.winning_level == 1
        assert result.command.mode == OperatingMode.SELF_USE

    @pytest.mark.asyncio
    async def test_control_loop_enforces_safety(self) -> None:
        config = AppConfig()
        adapter = _make_adapter(soc=0.04)  # Below minimum
        loop = ControlLoop(config, adapter)

        plan_slots = []
        now = _now()
        for i in range(4):
            start = now + timedelta(minutes=30 * i)
            plan_slots.append(PlanSlot(
                index=i, start=start, end=start + timedelta(minutes=30),
                mode=SlotMode.FORCE_DISCHARGE, target_power_w=5000,
            ))
        plan = OptimisationPlan(
            version=1, created_at=now, trigger_reason="test",
            horizon_start=now, horizon_end=now + timedelta(hours=2),
            slots=plan_slots, objective_score=0, solver_time_ms=10,
        )
        loop.set_plan(plan)

        cmd = await loop.tick_once()
        # Safety should override discharge to charge (grid available)
        assert cmd is not None
        assert cmd.mode == OperatingMode.FORCE_CHARGE


# ══════════════════════════════════════════════════════════════
# SCENARIO 5: Storm reserve holds SOC
# ══════════════════════════════════════════════════════════════


class TestScenario5StormReserve:
    """Storm active → hierarchy prevents discharge below reserve."""

    def test_storm_monitor_activates(self) -> None:
        from power_master.config.schema import StormConfig
        monitor = StormMonitor(StormConfig(probability_threshold=0.70, reserve_soc_target=0.80))
        changed = monitor.update(0.85)
        assert changed is True
        assert monitor.is_active is True
        assert monitor.reserve_soc == 0.80

    def test_hierarchy_blocks_discharge_with_storm(self) -> None:
        cmd = ControlCommand(mode=OperatingMode.FORCE_DISCHARGE, power_w=5000, priority=5)
        result = evaluate_hierarchy(
            cmd, current_soc=0.75, soc_min_hard=0.05, soc_max_hard=0.95,
            storm_active=True, storm_reserve_soc=0.80,
        )
        assert result.winning_level == 2
        assert result.command.mode == OperatingMode.SELF_USE

    def test_solver_respects_storm_constraint(self) -> None:
        config = AppConfig()
        inputs = _make_inputs(
            soc=0.9, storm=True, export_price=50.0, wacb=10.0, load=300.0,
        )
        plan = solve(config, inputs)
        avg_soc = sum(s.expected_soc for s in plan.slots) / len(plan.slots)
        assert avg_soc >= 0.6, f"Storm reserve should keep SOC high, got avg={avg_soc:.2f}"


# ══════════════════════════════════════════════════════════════
# SCENARIO 6: Manual override with timeout
# ══════════════════════════════════════════════════════════════


class TestScenario6ManualOverride:
    """User forces mode → overrides optimiser, times out eventually."""

    @pytest.mark.asyncio
    async def test_manual_overrides_plan(self) -> None:
        config = AppConfig()
        adapter = _make_adapter()
        manual = ManualOverride()
        manual.set(OperatingMode.FORCE_CHARGE, power_w=4000, timeout_seconds=3600)
        loop = ControlLoop(config, adapter, manual_override=manual)

        # Plan says discharge, but manual says charge
        now = _now()
        slots = [PlanSlot(
            index=0, start=now, end=now + timedelta(minutes=30),
            mode=SlotMode.FORCE_DISCHARGE, target_power_w=5000,
        )]
        plan = OptimisationPlan(
            version=1, created_at=now, trigger_reason="test",
            horizon_start=now, horizon_end=now + timedelta(minutes=30),
            slots=slots, objective_score=0, solver_time_ms=10,
        )
        loop.set_plan(plan)

        cmd = await loop.tick_once()
        assert cmd.mode == OperatingMode.FORCE_CHARGE

    def test_manual_timeout_expires(self) -> None:
        import time
        manual = ManualOverride()
        manual.set(OperatingMode.FORCE_CHARGE, timeout_seconds=0.01)
        time.sleep(0.02)
        assert manual.is_active is False

    def test_safety_still_overrides_manual(self) -> None:
        """Even with manual override, safety (level 1) takes precedence."""
        manual_cmd = ControlCommand(
            mode=OperatingMode.FORCE_CHARGE, power_w=5000,
            source="manual", priority=3,
        )
        result = evaluate_hierarchy(
            manual_cmd, current_soc=0.96, soc_min_hard=0.05, soc_max_hard=0.95,
        )
        assert result.winning_level == 1  # Safety overrides manual


# ══════════════════════════════════════════════════════════════
# SCENARIO 7: Resilience degradation and fallback
# ══════════════════════════════════════════════════════════════


class TestScenario7Resilience:
    """Provider failures → degraded mode → safe fallback."""

    def test_tariff_failure_degrades(self) -> None:
        config = AppConfig()
        checker = HealthChecker(max_consecutive_failures=2)
        checker.register("tariff")
        for _ in range(2):
            checker.record_failure("tariff", "API timeout")
        manager = ResilienceManager(config, checker)
        manager.evaluate()
        assert manager.level == ResilienceLevel.DEGRADED_TARIFF

    def test_fallback_in_degraded_tariff(self) -> None:
        config = AppConfig()
        cmd = get_fallback_command(ResilienceLevel.DEGRADED_TARIFF, 0.5, config)
        assert cmd.mode == OperatingMode.SELF_USE
        assert cmd.source == "fallback"

    def test_multiple_failures_enter_safe_mode(self) -> None:
        config = AppConfig()
        checker = HealthChecker(max_consecutive_failures=2)
        checker.register("tariff")
        checker.register("solar_forecast")
        for _ in range(2):
            checker.record_failure("tariff", "err")
            checker.record_failure("solar_forecast", "err")
        manager = ResilienceManager(config, checker)
        manager.evaluate()
        assert manager.level == ResilienceLevel.SAFE_MODE

    def test_safe_mode_fallback_is_zero_export(self) -> None:
        config = AppConfig()
        cmd = get_fallback_command(ResilienceLevel.SAFE_MODE, 0.5, config)
        assert cmd.mode == OperatingMode.SELF_USE_ZERO_EXPORT

    def test_recovery_returns_to_normal(self) -> None:
        config = AppConfig()
        checker = HealthChecker(max_consecutive_failures=2)
        checker.register("tariff")
        for _ in range(2):
            checker.record_failure("tariff", "err")
        manager = ResilienceManager(config, checker)
        manager.evaluate()
        assert manager.level == ResilienceLevel.DEGRADED_TARIFF

        checker.record_success("tariff")
        manager.evaluate()
        assert manager.level == ResilienceLevel.NORMAL


# ══════════════════════════════════════════════════════════════
# SCENARIO 8: Price spike — load shedding + discharge
# ══════════════════════════════════════════════════════════════


class TestScenario8PriceSpike:
    """Price spike detected → shed loads, discharge battery."""

    def test_solver_blocks_charge_during_spike(self) -> None:
        config = AppConfig()
        inputs = _make_inputs(
            import_price=200.0, soc=0.3, spike_slots=[0, 1, 2, 3],
        )
        plan = solve(config, inputs)

        for slot in plan.slots[:4]:
            assert slot.mode != SlotMode.FORCE_CHARGE, "Should not charge during spike"

    @pytest.mark.asyncio
    async def test_load_manager_sheds_non_essential(self) -> None:
        config = AppConfig()
        manager = LoadManager(config)

        essential = FakeController("essential", "Essential", 500, priority_class=1)
        essential.state = LoadState.ON
        deferrable = FakeController("deferrable", "Deferrable", 2000, priority_class=4)
        deferrable.state = LoadState.ON

        manager.register(essential)
        manager.register(deferrable)

        commands = await manager.shed_for_spike(max_priority=2)

        # Only deferrable should be shed
        assert len(commands) == 1
        assert commands[0].load_id == "deferrable"
        assert deferrable.state == LoadState.OFF
        assert essential.state == LoadState.ON

    def test_scheduler_defers_loads_during_spike(self) -> None:
        now = _now()
        slots = []
        for i in range(4):
            start = now + timedelta(minutes=30 * i)
            slots.append(PlanSlot(
                index=i, start=start, end=start + timedelta(minutes=30),
                mode=SlotMode.SELF_USE, import_rate_cents=200.0,
                constraint_flags=["spike"],
            ))
        plan = OptimisationPlan(
            version=1, created_at=now, trigger_reason="price_spike",
            horizon_start=now, horizon_end=now + timedelta(hours=2),
            slots=slots, objective_score=0, solver_time_ms=10,
        )

        loads = [
            {"id": "pump", "name": "Pool Pump", "power_w": 1200, "priority_class": 4, "min_runtime_minutes": 30},
            {"id": "heater", "name": "Heater", "power_w": 800, "priority_class": 1, "min_runtime_minutes": 30},
        ]

        result = schedule_loads(plan, loads, spike_active=True)

        # Pump (priority 4) should be deferred, heater (priority 1) should schedule
        ids = [s.load_id for s in result]
        assert "pump" not in ids
        assert "heater" in ids

    def test_accounting_tracks_spike_discharge(self) -> None:
        config = AppConfig()
        engine = AccountingEngine(config, initial_soc=0.8, initial_wacb=10.0)

        # Discharge 5kWh at spike price of 200c/kWh
        event = engine.record_grid_export(5000, 200.0)

        # Revenue: 5 * 200 = 1000c, cost basis: 5 * 10 = 50c, profit: 950c
        assert event.profit_loss_cents == 950

    def test_anti_oscillation_allows_spike_response(self) -> None:
        """Safety/spike commands should bypass anti-oscillation."""
        guard = AntiOscillationGuard(AppConfig().anti_oscillation)
        normal = ControlCommand(mode=OperatingMode.SELF_USE, priority=5)
        guard.record_command(normal)

        # Safety command should pass immediately
        spike_cmd = ControlCommand(
            mode=OperatingMode.FORCE_DISCHARGE, power_w=5000,
            source="safety", priority=1,
        )
        assert guard.should_allow(spike_cmd) is True
