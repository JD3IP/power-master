"""Async control loop — 5-minute tick orchestrating all subsystems."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from power_master.config.schema import AppConfig
from power_master.control.anti_oscillation import AntiOscillationGuard
from power_master.control.command import ControlCommand, command_from_slot, dispatch_command
from power_master.control.constants import (
    COMMAND_REFRESH_INTERVAL_SECONDS,
    CONTROL_TICK_INTERVAL_SECONDS,
    PLAN_STALENESS_THRESHOLD_MULTIPLIER,
    PLAN_STALENESS_WARNING_COOLDOWN_SECONDS,
)
from power_master.control.hierarchy import evaluate_hierarchy
from power_master.control.manual_override import ManualOverride
from power_master.control.mode_schedule import ModeScheduler
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
    current_source: str = "default"  # source of the last dispatched command (schedule/manual/optimiser/...)
    last_command_result: str = ""
    is_running: bool = False
    free_window_battery_setpoint_w: int | None = None  # Throttled battery setpoint from free-window orchestrator


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

    # Modes that use remote power control and need periodic refresh.
    # The FoxESS KH reverts to self-use when remote commands stop (~30s).
    _REMOTE_MODES = frozenset({
        OperatingMode.FORCE_CHARGE,
        OperatingMode.FORCE_DISCHARGE,
        OperatingMode.FORCE_CHARGE_ZERO_IMPORT,
    })

    def __init__(
        self,
        config: AppConfig,
        adapter: InverterAdapter,
        manual_override: ManualOverride | None = None,
        anti_oscillation: AntiOscillationGuard | None = None,
        repo: Any | None = None,
    ) -> None:
        self._config: AppConfig = config
        self._adapter: InverterAdapter = adapter
        self._manual_override: ManualOverride = manual_override or ManualOverride()
        self._anti_oscillation: AntiOscillationGuard = anti_oscillation or AntiOscillationGuard(config.anti_oscillation)
        self._mode_scheduler: ModeScheduler = ModeScheduler(
            config.mode_schedule, getattr(config.load_profile, "timezone", "UTC"),
        )
        self._state: LoopState = LoopState()
        self._stop_event: asyncio.Event = asyncio.Event()
        self._wake_event: asyncio.Event = asyncio.Event()  # request an immediate re-tick
        self._repo: Any = repo

        # External state for hierarchy evaluation (updated by main app)
        self._storm_active: bool = False
        self._storm_reserve_soc: float = 0.0

        # Last dispatched command — used by the refresh loop to re-send
        self._last_dispatched_command: ControlCommand | None = None

        # Callbacks for extensibility
        self._on_telemetry: list[Callable[[Telemetry], Awaitable[None]]] = []
        self._on_command: list[Callable[[ControlCommand, Any], Awaitable[None]]] = []
        self._on_plan_needed: list[Callable[[], Awaitable[None]]] = []

        # Track last staleness warning to avoid spam
        self._last_staleness_warned_at: float | None = None

    @property
    def state(self) -> LoopState:
        return self._state

    @property
    def manual_override(self) -> ManualOverride:
        return self._manual_override

    def set_plan(self, plan: OptimisationPlan) -> None:
        """Update the current plan (called by rebuild evaluator under plan_lock)."""
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
        interval: int = self._config.planning.evaluation_interval_seconds

        logger.info("Control loop starting (interval: %ds)", interval)

        refresh_task = asyncio.create_task(self._command_refresh_loop())

        try:
            while not self._stop_event.is_set():
                await self._tick()
                # Sleep until the interval elapses, a stop is requested, or an
                # immediate re-tick is requested (e.g. a schedule change saved
                # from the UI so it applies without waiting for the next tick).
                # Re-read the interval each loop so config changes take effect.
                interval = self._config.planning.evaluation_interval_seconds
                if await self._wait_for_next_tick(interval):
                    break  # stop_event was set
        except asyncio.CancelledError:
            raise
        finally:
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            self._state.is_running = False
            logger.info("Control loop stopped after %d ticks", self._state.tick_count)

    async def _wait_for_next_tick(self, timeout: float) -> bool:
        """Wait until timeout, a stop, or a wake request. Returns True if stopped."""
        stop_task = asyncio.ensure_future(self._stop_event.wait())
        wake_task = asyncio.ensure_future(self._wake_event.wait())
        try:
            await asyncio.wait(
                {stop_task, wake_task}, timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (stop_task, wake_task):
                if not t.done():
                    t.cancel()
        if self._wake_event.is_set():
            self._wake_event.clear()
        return self._stop_event.is_set()

    def request_tick(self) -> None:
        """Wake the control loop to re-evaluate immediately (e.g. after a
        schedule change), instead of waiting for the next interval."""
        self._wake_event.set()

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

        # 2a. Check plan staleness
        plan: OptimisationPlan | None = self._state.current_plan
        if plan is not None:
            now: float = time.monotonic()
            stale_threshold: int = PLAN_STALENESS_THRESHOLD_MULTIPLIER * self._config.planning.evaluation_interval_seconds
            plan_age_s: float = (datetime.now(timezone.utc) - plan.created_at).total_seconds()
            if plan_age_s > stale_threshold:
                # Warn at most once every configured cooldown period
                if self._last_staleness_warned_at is None or (now - self._last_staleness_warned_at) >= PLAN_STALENESS_WARNING_COOLDOWN_SECONDS:
                    logger.warning(
                        "Tick %d: plan is stale (age=%ds, threshold=%ds)",
                        self._state.tick_count, int(plan_age_s), int(stale_threshold),
                    )
                    self._last_staleness_warned_at = now
            # Warn if horizon has passed with no current slot
            if plan.horizon_end < datetime.now(timezone.utc):
                current_slot = plan.get_current_slot()
                if current_slot is None:
                    logger.warning(
                        "Tick %d: plan horizon has passed (end=%s) with no current slot",
                        self._state.tick_count, plan.horizon_end.isoformat(),
                    )

        # 2b. Skip dispatch if optimiser is disabled and this isn't a manual override
        if not self._config.planning.optimiser_enabled and command.source != "manual":
            logger.debug("Tick %d: optimiser disabled, skipping auto command", self._state.tick_count)
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

        # 4b. Power bounds check — clamp to configured limits
        max_charge = self._config.battery.max_charge_rate_w
        max_discharge = self._config.battery.max_discharge_rate_w
        clamped_power = max(-max_discharge, min(final_command.power_w, max_charge))
        if clamped_power != final_command.power_w:
            logger.warning(
                "Tick %d: power clamped from %dW to %dW (charge_limit=%dW, discharge_limit=%dW)",
                self._state.tick_count,
                final_command.power_w,
                clamped_power,
                max_charge,
                max_discharge,
            )
            final_command.power_w = clamped_power

        # 5. Dispatch
        result = await dispatch_command(self._adapter, final_command)
        if result.success:
            self._anti_oscillation.record_command(final_command)
            self._state.current_mode = final_command.mode
            self._state.current_source = final_command.source
            self._last_dispatched_command = final_command
        self._state.last_command_result = "ok" if result.success else f"error: {result.message}"

        # 6. Audit logging
        if self._repo:
            try:
                await self._repo.log_command_audit(
                    mode=final_command.mode.name,
                    power_w=final_command.power_w,
                    source=final_command.source,
                    source_type=final_command.get_source_type().value,
                    reason=final_command.reason,
                    priority=final_command.priority,
                    result="ok" if result.success else "error",
                    latency_ms=result.latency_ms,
                )
            except Exception as e:
                # Root cause unconfirmed: in the debug bundle this failed on every
                # committed tick and db_logs was also empty, yet telemetry writes
                # succeeded — so it's specific to the audit/db-log writes on the
                # deployed instance, not a global DB outage. Log the exception
                # TYPE+MESSAGE inline (shows in the in-memory log bundle) AND the
                # full traceback via exception() (file logs) to name it next time.
                logger.exception(
                    "Failed to log command audit: mode=%s power=%dW source=%s "
                    "source_type=%s reason=%s [%s: %s]",
                    final_command.mode.name,
                    final_command.power_w,
                    final_command.source,
                    final_command.get_source_type().value,
                    final_command.reason,
                    type(e).__name__,
                    e,
                )

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
        don't need refresh since remote control is disabled for those.  Mode
        CHANGES seen between ticks are gated by the anti-oscillation dwell so
        plan rebuilds can't flip force charge/discharge on and off; refreshing
        the SAME mode always proceeds.
        """
        interval: int = self._config.hardware.foxess.remote_refresh_interval_seconds
        logger.info("Command refresh loop starting (interval: %ds)", interval)

        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # Normal — interval elapsed

            try:
                await self._refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Command refresh error")

    async def _refresh_once(self) -> ControlCommand | None:
        """Run a single command-refresh iteration.

        Re-derives the desired command from override/plan, gates genuine mode
        changes through the anti-oscillation guard, and re-sends the resulting
        command for remote-control modes.  Returns the dispatched command, or
        None if nothing was sent.  Extracted from the refresh loop so the
        gating behaviour is unit-testable.
        """
        # Re-evaluate the current desired command from override/plan so
        # the refresh always reflects the latest state (e.g. override still
        # active, plan slot changed).  Falls back to last-dispatched if
        # telemetry is unavailable.
        telemetry = self._state.last_telemetry
        cmd = self._determine_command(telemetry) if telemetry else self._last_dispatched_command
        if cmd is None:
            return None

        # Skip refresh if optimiser disabled and not a manual command
        if not self._config.planning.optimiser_enabled and cmd.source != "manual":
            return None

        # Guard mode CHANGES with the anti-oscillation dwell.  Plan rebuilds
        # (SOC deviation, forecast/price deltas) can flip the current slot's
        # mode between the slower control ticks; without this guard the refresh
        # loop would enact every flip within ~20s, causing force-discharge to
        # start and stop repeatedly.  Refreshing the SAME mode always proceeds
        # (keeps the inverter's remote watchdog fed); switching to a DIFFERENT
        # mode must satisfy the guard, otherwise we hold the previous command.
        last = self._last_dispatched_command
        changed_mode = last is not None and cmd.mode != last.mode
        if changed_mode and not self._anti_oscillation.should_allow(
            cmd, telemetry.soc if telemetry else None
        ):
            logger.debug(
                "Command refresh: mode change %s→%s suppressed by anti-oscillation; "
                "holding previous command",
                last.mode.name, cmd.mode.name,
            )
            cmd = last
            changed_mode = False

        if cmd.mode not in self._REMOTE_MODES:
            return None

        result = await dispatch_command(self._adapter, cmd)
        if result.success:
            # Record genuine mode changes so the dwell timer restarts and the
            # guard tracks the switch for subsequent oscillation checks.
            if changed_mode:
                self._anti_oscillation.record_command(cmd)
                self._state.current_mode = cmd.mode
                self._state.current_source = cmd.source
            self._last_dispatched_command = cmd
            logger.debug(
                "Command refresh: mode=%s power=%dW source=%s latency=%dms",
                cmd.mode.name, cmd.power_w, cmd.source, result.latency_ms,
            )
            return cmd

        logger.warning(
            "Command refresh failed: mode=%s error=%s",
            cmd.mode.name, result.message,
        )
        return None

    def update_config(self, config: AppConfig) -> None:
        """Refresh config after a hot-reload (e.g. mode-schedule edits)."""
        self._config = config
        self._mode_scheduler.update_config(
            config.mode_schedule, getattr(config.load_profile, "timezone", "UTC"),
        )

    def _determine_command(self, telemetry: Telemetry) -> ControlCommand | None:
        """Determine the command to execute.

        Priority: manual override > mode schedule > plan slot > default self-use.
        The scheduled command still passes through the safety/storm hierarchy in
        `_tick`, so it can never override safety or drain past reserves.
        """
        # Manual override (explicit user action) wins outright.
        override_cmd: ControlCommand | None = self._manual_override.get_command()
        if override_cmd is not None:
            return override_cmd

        # User-defined mode schedule overrides the optimiser plan while active.
        sched_cmd: ControlCommand | None = self._mode_scheduler.get_command(
            datetime.now(timezone.utc)
        )
        if sched_cmd is not None:
            return sched_cmd

        # Plan slot
        plan: OptimisationPlan | None = self._state.current_plan
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
            return None  # noqa: TRY300
