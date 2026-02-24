"""Tests for control loop, hierarchy, anti-oscillation, and manual override."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from power_master.config.schema import AntiOscillationConfig, AppConfig
from power_master.control.anti_oscillation import AntiOscillationGuard
from power_master.control.command import ControlCommand, command_from_slot
from power_master.control.hierarchy import evaluate_hierarchy
from power_master.control.loop import ControlLoop
from power_master.control.manual_override import ManualOverride
from power_master.hardware.base import CommandResult, InverterCommand, OperatingMode
from power_master.hardware.telemetry import Telemetry
from power_master.optimisation.plan import OptimisationPlan, PlanSlot, SlotMode


# ── Helpers ──────────────────────────────────────────────────


def _make_telemetry(soc: float = 0.5, grid: bool = True) -> Telemetry:
    return Telemetry(
        soc=soc,
        battery_power_w=0,
        solar_power_w=2000,
        grid_power_w=500,
        load_power_w=2500,
        grid_available=grid,
    )


def _make_plan(n_slots: int = 4, mode: SlotMode = SlotMode.SELF_USE) -> OptimisationPlan:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    slots = []
    for i in range(n_slots):
        start = now + timedelta(minutes=30 * i)
        end = start + timedelta(minutes=30)
        slots.append(PlanSlot(
            index=i,
            start=start,
            end=end,
            mode=mode,
            target_power_w=3000 if mode in (SlotMode.FORCE_CHARGE, SlotMode.FORCE_DISCHARGE) else 0,
        ))
    return OptimisationPlan(
        version=1,
        created_at=now,
        trigger_reason="periodic",
        horizon_start=now,
        horizon_end=now + timedelta(minutes=30 * n_slots),
        slots=slots,
        objective_score=0.0,
        solver_time_ms=10,
    )


def _make_adapter() -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_telemetry = AsyncMock(return_value=_make_telemetry())
    adapter.send_command = AsyncMock(return_value=CommandResult(success=True, latency_ms=10))
    adapter.is_connected = AsyncMock(return_value=True)
    return adapter


# ── Command Tests ─────────────────────────────────────────────


class TestCommand:
    def test_command_from_slot_self_use(self) -> None:
        slot = PlanSlot(
            index=0,
            start=datetime.now(timezone.utc),
            end=datetime.now(timezone.utc) + timedelta(minutes=30),
            mode=SlotMode.SELF_USE,
        )
        cmd = command_from_slot(slot)
        assert cmd.mode == OperatingMode.SELF_USE
        assert cmd.source == "optimiser"

    def test_command_from_slot_force_charge(self) -> None:
        slot = PlanSlot(
            index=1,
            start=datetime.now(timezone.utc),
            end=datetime.now(timezone.utc) + timedelta(minutes=30),
            mode=SlotMode.FORCE_CHARGE,
            target_power_w=5000,
        )
        cmd = command_from_slot(slot)
        assert cmd.mode == OperatingMode.FORCE_CHARGE
        assert cmd.power_w == 5000

    def test_command_from_slot_zero_export(self) -> None:
        slot = PlanSlot(
            index=2,
            start=datetime.now(timezone.utc),
            end=datetime.now(timezone.utc) + timedelta(minutes=30),
            mode=SlotMode.SELF_USE_ZERO_EXPORT,
        )
        cmd = command_from_slot(slot)
        assert cmd.mode == OperatingMode.SELF_USE_ZERO_EXPORT


# ── Hierarchy Tests ───────────────────────────────────────────


class TestHierarchy:
    def test_normal_plan_passes_through(self) -> None:
        cmd = ControlCommand(mode=OperatingMode.SELF_USE, source="optimiser", priority=5)
        result = evaluate_hierarchy(cmd, current_soc=0.5, soc_min_hard=0.05, soc_max_hard=0.95)
        assert result.command.mode == OperatingMode.SELF_USE
        assert result.winning_level == 4
        assert result.overridden is False

    def test_safety_blocks_discharge_at_min_soc(self) -> None:
        cmd = ControlCommand(mode=OperatingMode.FORCE_DISCHARGE, power_w=5000, source="optimiser", priority=5)
        result = evaluate_hierarchy(cmd, current_soc=0.05, soc_min_hard=0.05, soc_max_hard=0.95)
        assert result.winning_level == 1
        assert result.overridden is True
        assert result.command.mode == OperatingMode.FORCE_CHARGE  # Grid available, so charge

    def test_safety_blocks_charge_at_max_soc(self) -> None:
        cmd = ControlCommand(mode=OperatingMode.FORCE_CHARGE, power_w=5000, source="optimiser", priority=5)
        result = evaluate_hierarchy(cmd, current_soc=0.95, soc_min_hard=0.05, soc_max_hard=0.95)
        assert result.winning_level == 1
        assert result.overridden is True
        assert result.command.mode == OperatingMode.SELF_USE

    def test_safety_forces_self_use_when_grid_lost(self) -> None:
        cmd = ControlCommand(mode=OperatingMode.FORCE_CHARGE, source="optimiser", priority=5)
        result = evaluate_hierarchy(
            cmd, current_soc=0.5, soc_min_hard=0.05, soc_max_hard=0.95,
            grid_available=False,
        )
        assert result.winning_level == 1
        assert result.command.mode == OperatingMode.SELF_USE

    def test_storm_blocks_discharge_below_reserve(self) -> None:
        cmd = ControlCommand(mode=OperatingMode.FORCE_DISCHARGE, power_w=5000, source="optimiser", priority=5)
        result = evaluate_hierarchy(
            cmd, current_soc=0.75, soc_min_hard=0.05, soc_max_hard=0.95,
            storm_active=True, storm_reserve_soc=0.80,
        )
        assert result.winning_level == 2
        assert result.overridden is True
        assert result.command.mode == OperatingMode.SELF_USE

    def test_storm_allows_discharge_above_reserve(self) -> None:
        cmd = ControlCommand(mode=OperatingMode.FORCE_DISCHARGE, power_w=5000, source="optimiser", priority=5)
        result = evaluate_hierarchy(
            cmd, current_soc=0.90, soc_min_hard=0.05, soc_max_hard=0.95,
            storm_active=True, storm_reserve_soc=0.80,
        )
        assert result.winning_level == 4
        assert result.overridden is False

    def test_safety_overrides_storm(self) -> None:
        """Safety (level 1) takes precedence over storm (level 2)."""
        cmd = ControlCommand(mode=OperatingMode.FORCE_DISCHARGE, source="optimiser", priority=5)
        result = evaluate_hierarchy(
            cmd, current_soc=0.05, soc_min_hard=0.05, soc_max_hard=0.95,
            storm_active=True, storm_reserve_soc=0.80,
        )
        assert result.winning_level == 1  # Safety wins


# ── Anti-Oscillation Tests ────────────────────────────────────


class TestAntiOscillation:
    def test_first_command_always_passes(self) -> None:
        guard = AntiOscillationGuard(AntiOscillationConfig())
        cmd = ControlCommand(mode=OperatingMode.SELF_USE, priority=5)
        assert guard.should_allow(cmd) is True

    def test_dwell_time_blocks_rapid_switch(self) -> None:
        config = AntiOscillationConfig(min_command_duration_seconds=300)
        guard = AntiOscillationGuard(config)

        # Record initial command
        cmd1 = ControlCommand(mode=OperatingMode.SELF_USE, priority=5)
        guard.record_command(cmd1)

        # Try to switch immediately — should be blocked
        cmd2 = ControlCommand(mode=OperatingMode.FORCE_CHARGE, priority=5)
        assert guard.should_allow(cmd2) is False
        assert guard.state.suppressed_count == 1

    def test_same_mode_passes_dwell_check(self) -> None:
        config = AntiOscillationConfig(min_command_duration_seconds=300)
        guard = AntiOscillationGuard(config)

        cmd1 = ControlCommand(mode=OperatingMode.SELF_USE, priority=5)
        guard.record_command(cmd1)

        # Same mode — no dwell needed
        cmd2 = ControlCommand(mode=OperatingMode.SELF_USE, priority=5)
        assert guard.should_allow(cmd2) is True

    def test_safety_bypasses_anti_oscillation(self) -> None:
        config = AntiOscillationConfig(min_command_duration_seconds=300)
        guard = AntiOscillationGuard(config)

        cmd1 = ControlCommand(mode=OperatingMode.SELF_USE, priority=5)
        guard.record_command(cmd1)

        # Safety command (priority 1) should always pass
        cmd_safety = ControlCommand(mode=OperatingMode.FORCE_CHARGE, priority=1, source="safety")
        assert guard.should_allow(cmd_safety) is True

    def test_manual_bypasses_anti_oscillation(self) -> None:
        config = AntiOscillationConfig(min_command_duration_seconds=300)
        guard = AntiOscillationGuard(config)

        cmd1 = ControlCommand(mode=OperatingMode.SELF_USE, priority=5, source="optimiser")
        guard.record_command(cmd1)

        # Manual command should not be blocked by dwell time.
        cmd_manual = ControlCommand(
            mode=OperatingMode.FORCE_CHARGE,
            priority=3,
            source="manual",
        )
        assert guard.should_allow(cmd_manual) is True

    def test_rate_limit(self) -> None:
        config = AntiOscillationConfig(
            min_command_duration_seconds=0,
            max_commands_per_window=2,
            rate_limit_window_seconds=900,
        )
        guard = AntiOscillationGuard(config)

        # Execute 2 commands
        for _ in range(2):
            cmd = ControlCommand(mode=OperatingMode.SELF_USE, priority=5)
            guard.record_command(cmd)

        # Third should be blocked
        cmd3 = ControlCommand(mode=OperatingMode.SELF_USE, priority=5)
        assert guard.should_allow(cmd3) is False

    def test_reset_clears_state(self) -> None:
        guard = AntiOscillationGuard(AntiOscillationConfig())
        cmd = ControlCommand(mode=OperatingMode.FORCE_CHARGE, priority=5)
        guard.record_command(cmd)

        guard.reset()
        assert guard.state.last_mode is None
        assert guard.state.suppressed_count == 0


# ── Manual Override Tests ──────────────────────────────────────


class TestManualOverride:
    def test_not_active_by_default(self) -> None:
        override = ManualOverride()
        assert override.is_active is False
        assert override.get_command() is None

    def test_set_and_get(self) -> None:
        override = ManualOverride()
        override.set(OperatingMode.FORCE_CHARGE, power_w=5000)

        assert override.is_active is True
        cmd = override.get_command()
        assert cmd is not None
        assert cmd.mode == OperatingMode.FORCE_CHARGE
        assert cmd.power_w == 5000
        assert cmd.source == "manual"
        assert cmd.priority == 3

    def test_clear(self) -> None:
        override = ManualOverride()
        override.set(OperatingMode.FORCE_DISCHARGE)
        override.clear()

        assert override.is_active is False
        assert override.get_command() is None

    def test_set_auto_clears(self) -> None:
        override = ManualOverride()
        override.set(OperatingMode.FORCE_CHARGE)
        override.set(OperatingMode.AUTO)

        assert override.is_active is False

    def test_timeout(self) -> None:
        override = ManualOverride()
        override.set(OperatingMode.FORCE_CHARGE, timeout_seconds=0.01)

        time.sleep(0.02)

        assert override.is_active is False
        assert override.get_command() is None

    def test_remaining_seconds(self) -> None:
        override = ManualOverride()
        override.set(OperatingMode.FORCE_CHARGE, timeout_seconds=100)

        remaining = override.remaining_seconds
        assert 99 <= remaining <= 100

    def test_remaining_zero_when_inactive(self) -> None:
        override = ManualOverride()
        assert override.remaining_seconds == 0.0


# ── Control Loop Tests ─────────────────────────────────────────


class TestControlLoop:
    @pytest.mark.asyncio
    async def test_tick_with_plan(self) -> None:
        config = AppConfig()
        adapter = _make_adapter()
        loop = ControlLoop(config, adapter)

        plan = _make_plan(mode=SlotMode.FORCE_CHARGE)
        loop.set_plan(plan)

        cmd = await loop.tick_once()

        assert cmd is not None
        assert cmd.mode == OperatingMode.FORCE_CHARGE
        adapter.send_command.assert_called_once()

    @pytest.mark.asyncio
    async def test_tick_without_plan_defaults_self_use(self) -> None:
        config = AppConfig()
        adapter = _make_adapter()
        loop = ControlLoop(config, adapter)

        cmd = await loop.tick_once()

        assert cmd is not None
        assert cmd.mode == OperatingMode.SELF_USE

    @pytest.mark.asyncio
    async def test_manual_override_takes_precedence(self) -> None:
        config = AppConfig()
        adapter = _make_adapter()
        manual = ManualOverride()
        manual.set(OperatingMode.FORCE_DISCHARGE, power_w=4000)
        loop = ControlLoop(config, adapter, manual_override=manual)

        plan = _make_plan(mode=SlotMode.FORCE_CHARGE)
        loop.set_plan(plan)

        cmd = await loop.tick_once()

        assert cmd is not None
        assert cmd.mode == OperatingMode.FORCE_DISCHARGE
        assert cmd.power_w == 4000

    @pytest.mark.asyncio
    async def test_safety_overrides_manual(self) -> None:
        config = AppConfig()
        # Adapter returns telemetry with SOC at max
        adapter = _make_adapter()
        adapter.get_telemetry = AsyncMock(return_value=_make_telemetry(soc=0.95))

        manual = ManualOverride()
        manual.set(OperatingMode.FORCE_CHARGE, power_w=5000)
        loop = ControlLoop(config, adapter, manual_override=manual)

        cmd = await loop.tick_once()

        # Safety should override manual charge at max SOC
        assert cmd is not None
        assert cmd.mode == OperatingMode.SELF_USE  # Overridden by safety

    @pytest.mark.asyncio
    async def test_telemetry_failure_skips_tick(self) -> None:
        config = AppConfig()
        adapter = _make_adapter()
        adapter.get_telemetry = AsyncMock(side_effect=ConnectionError("lost"))
        loop = ControlLoop(config, adapter)

        cmd = await loop.tick_once()

        assert cmd is None
        adapter.send_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_state_tracking(self) -> None:
        config = AppConfig()
        adapter = _make_adapter()
        loop = ControlLoop(config, adapter)

        assert loop.state.tick_count == 0
        assert loop.state.is_running is False

        await loop.tick_once()

        assert loop.state.tick_count == 1
        assert loop.state.last_telemetry is not None
        assert loop.state.current_mode == OperatingMode.SELF_USE

    @pytest.mark.asyncio
    async def test_stop(self) -> None:
        config = AppConfig()
        config.planning.evaluation_interval_seconds = 1
        adapter = _make_adapter()
        loop = ControlLoop(config, adapter)

        async def stop_after_delay():
            await asyncio.sleep(0.1)
            loop.stop()

        task = asyncio.create_task(stop_after_delay())
        await loop.run()
        await task

        assert loop.state.is_running is False
