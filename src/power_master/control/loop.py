"""Async control loop — 5-minute tick orchestrating all subsystems."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from power_master.config.schema import AppConfig
from power_master.control.anti_oscillation import AntiOscillationGuard
from power_master.control.command import ControlCommand, command_from_slot, dispatch_command
from power_master.control.hierarchy import evaluate_hierarchy
from power_master.control.manual_override import ManualOverride
from power_master.hardware.base import InverterAdapter, OperatingMode
from power_master.hardware.telemetry import Telemetry
from power_master.optimisation.plan import OptimisationPlan

logger = logging.getLogger(__name__)


@dataclass
class LoopState:
    """Snapshot of the control loop state."""

    tick_count: int = 0
    last_tick_at: datetime | None = None
    last_telemetry: Telemetry | None = None
    current_plan: OptimisationPlan | None = None
    current_mode: OperatingMode = OperatingMode.SELF_USE
    last_command_result: str = ""
    is_running: bool = False


class ControlLoop:
    """Main async control loop.

    Every tick (default 5 minutes):
    1. Read telemetry from inverter
    2. Check manual override
    3. Determine command from plan
    4. Apply control hierarchy
    5. Apply anti-oscillation guard
    6. Dispatch command to inverter
    """

    # Modes that use remote power control and need periodic refresh
    _REMOTE_MODES = frozenset({OperatingMode.FORCE_CHARGE, OperatingMode.FORCE_DISCHARGE})

    def __init__(
        self,
        config: AppConfig,
        adapter: InverterAdapter,
        manual_override: ManualOverride | None = None,
        anti_oscillation: AntiOscillationGuard | None = None,
    ) -> None:
        self._config = config
        self._adapter = adapter
        self._manual_override = manual_override or ManualOverride()
        self._anti_oscillation = anti_oscillation or AntiOscillationGuard(config.anti_oscillation)
        self._state = LoopState()
        self._stop_event = asyncio.Event()

        # External state for hierarchy evaluation (updated by main app)
        self._storm_active: bool = False
        self._storm_reserve_soc: float = 0.0

        # Last dispatched command — used by the refresh loop to re-send
        self._last_dispatched_command: ControlCommand | None = None

        # Callbacks for extensibility
        self._on_telemetry: list = []
        self._on_command: list = []
        self._on_plan_needed: list = []

    @property
    def state(self) -> LoopState:
        return self._state

    @property
    def manual_override(self) -> ManualOverride:
        return self._manual_override

    def set_plan(self, plan: OptimisationPlan) -> None:
        """Update the current plan (called by rebuild evaluator)."""
        self._state.current_plan = plan

    async def run(self) -> None:
        """Run the control loop until stopped.

        Starts two concurrent tasks:
        1. Main tick loop (every evaluation_interval_seconds, default 300s)
        2. Command refresh loop (every remote_refresh_interval_seconds, default 20s)

        The refresh loop re-sends the last dispatched command for modes that
        use remote power control (FORCE_CHARGE, FORCE_DISCHARGE).  The FoxESS KH
        inverter reverts to self-use when remote commands stop arriving, so
        continuous refresh is required to maintain force charge/discharge.
        """
        self._state.is_running = True
        self._stop_event.clear()
        interval = self._config.planning.evaluation_interval_seconds

        logger.info("Control loop starting (interval: %ds)", interval)

        refresh_task = asyncio.create_task(self._command_refresh_loop())

        try:
            while not self._stop_event.is_set():
                await self._tick()
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                    break  # stop_event was set
                except asyncio.TimeoutError:
                    pass  # Normal — just means interval elapsed
        finally:
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            self._state.is_running = False
            logger.info("Control loop stopped after %d ticks", self._state.tick_count)

    def stop(self) -> None:
        """Signal the loop to stop."""
        self._stop_event.set()

    async def tick_once(self, bypass_anti_oscillation: bool = False) -> ControlCommand | None:
        """Execute a single tick (for testing)."""
        return await self._tick(bypass_anti_oscillation=bypass_anti_oscillation)

    async def _tick(self, bypass_anti_oscillation: bool = False) -> ControlCommand | None:
        """Execute one control loop iteration."""
        self._state.tick_count += 1
        self._state.last_tick_at = datetime.now(timezone.utc)
        tick_start = time.monotonic()

        # 1. Read telemetry
        telemetry = await self._read_telemetry()
        if telemetry is None:
            logger.warning("Tick %d: failed to read telemetry, skipping", self._state.tick_count)
            return None
        self._state.last_telemetry = telemetry

        # Notify telemetry callbacks
        for cb in self._on_telemetry:
            try:
                await cb(telemetry)
            except Exception:
                logger.exception("Telemetry callback error")

        # 2. Determine command
        command = self._determine_command(telemetry)
        if command is None:
            return None

        # 3. Apply hierarchy
        hierarchy_result = evaluate_hierarchy(
            plan_command=command,
            current_soc=telemetry.soc,
            soc_min_hard=self._config.battery.soc_min_hard,
            soc_max_hard=self._config.battery.soc_max_hard,
            storm_active=self._storm_active,
            storm_reserve_soc=self._storm_reserve_soc,
            grid_available=telemetry.grid_available,
        )
        final_command = hierarchy_result.command

        # 4. Anti-oscillation guard
        if (
            not bypass_anti_oscillation
            and not self._anti_oscillation.should_allow(final_command, telemetry.soc)
        ):
            logger.debug("Tick %d: command suppressed by anti-oscillation", self._state.tick_count)
            return None

        # 5. Dispatch
        result = await dispatch_command(self._adapter, final_command)
        if result.success:
            self._anti_oscillation.record_command(final_command)
            self._state.current_mode = final_command.mode
            self._last_dispatched_command = final_command
        self._state.last_command_result = "ok" if result.success else f"error: {result.message}"

        elapsed_ms = int((time.monotonic() - tick_start) * 1000)
        log_fn = logger.info if result.success else logger.warning
        log_fn(
            "Tick %d: mode=%s power=%dW source=%s hierarchy_level=%d elapsed=%dms result=%s",
            self._state.tick_count,
            final_command.mode.name,
            final_command.power_w,
            final_command.source,
            hierarchy_result.winning_level,
            elapsed_ms,
            self._state.last_command_result,
        )

        # Notify command callbacks
        for cb in self._on_command:
            try:
                await cb(final_command, result)
            except Exception:
                logger.exception("Command callback error")

        return final_command

    async def _command_refresh_loop(self) -> None:
        """Periodically re-send the active command to keep the inverter in remote mode.

        The FoxESS KH inverter reverts to self-use when remote control commands
        stop arriving (~30s observed).  This loop re-sends the last dispatched
        command at a configurable interval (default 20s) so force charge and
        force discharge are maintained between the slower control ticks (300s).

        Only refreshes for modes that use remote power control; SELF_USE modes
        don't need refresh since remote control is disabled for those.
        Bypasses anti-oscillation since this is a refresh, not a mode change.
        """
        interval = self._config.hardware.foxess.remote_refresh_interval_seconds
        logger.info("Command refresh loop starting (interval: %ds)", interval)

        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # Normal — interval elapsed

            cmd = self._last_dispatched_command
            if cmd is None:
                continue

            if cmd.mode not in self._REMOTE_MODES:
                continue

            try:
                result = await dispatch_command(self._adapter, cmd)
                if result.success:
                    logger.debug(
                        "Command refresh: mode=%s power=%dW latency=%dms",
                        cmd.mode.name, cmd.power_w, result.latency_ms,
                    )
                else:
                    logger.warning(
                        "Command refresh failed: mode=%s error=%s",
                        cmd.mode.name, result.message,
                    )
            except Exception:
                logger.exception("Command refresh error")

    def _determine_command(self, telemetry: Telemetry) -> ControlCommand | None:
        """Determine the command to execute.

        Priority: manual override > plan slot > default self-use.
        """
        # Manual override
        override_cmd = self._manual_override.get_command()
        if override_cmd is not None:
            return override_cmd

        # Plan slot
        plan = self._state.current_plan
        if plan is not None:
            slot = plan.get_current_slot()
            if slot is not None:
                return command_from_slot(slot)

        # Default: self-use
        return ControlCommand(
            mode=OperatingMode.SELF_USE,
            power_w=0,
            source="default",
            reason="no_plan",
            priority=5,
        )

    def update_storm_state(self, active: bool, reserve_soc: float = 0.0) -> None:
        """Update storm state for hierarchy evaluation."""
        self._storm_active = active
        self._storm_reserve_soc = reserve_soc

    def update_adapter(self, adapter: InverterAdapter) -> None:
        """Hot-swap the inverter adapter (for config reload)."""
        self._adapter = adapter

    def update_live_telemetry(self, telemetry: Telemetry) -> None:
        """Update the latest telemetry snapshot outside the control tick."""
        self._state.last_telemetry = telemetry

    async def _read_telemetry(self) -> Telemetry | None:
        """Read telemetry from the inverter adapter."""
        try:
            return await self._adapter.get_telemetry()
        except Exception:
            logger.exception("Failed to read telemetry")
            return None
