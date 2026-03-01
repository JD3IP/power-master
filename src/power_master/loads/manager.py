"""Load orchestration manager — coordinates all load controllers."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from power_master.config.schema import AppConfig
from power_master.loads.base import LoadController, LoadState, LoadStatus
from power_master.optimisation.load_scheduler import ScheduledLoad

logger = logging.getLogger(__name__)

MANUAL_LOAD_OVERRIDE_SECONDS = 3600  # 60 minutes


@dataclass
class LoadCommand:
    """A command issued to a load."""

    load_id: str
    action: str  # "on" or "off"
    reason: str
    issued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    success: bool = False


@dataclass
class LoadOverride:
    """A manual override for a specific load."""

    load_id: str
    state: str  # "on" or "off"
    set_at: float = field(default_factory=time.monotonic)
    timeout_seconds: float = MANUAL_LOAD_OVERRIDE_SECONDS
    source: str = "user"

    @property
    def is_expired(self) -> bool:
        return (time.monotonic() - self.set_at) >= self.timeout_seconds

    @property
    def remaining_seconds(self) -> float:
        remaining = self.timeout_seconds - (time.monotonic() - self.set_at)
        return max(0.0, remaining)


class LoadManager:
    """Manages all controllable loads.

    Responsibilities:
    - Register and track load controllers
    - Execute scheduled load commands from the load scheduler
    - Handle spike responses (shed non-essential loads)
    - Track command history
    - Track actual runtime via state change detection
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._controllers: dict[str, LoadController] = {}
        self._command_history: list[LoadCommand] = []
        self._shed_loads: set[str] = set()  # Loads currently shed for spike
        self._load_overrides: dict[str, LoadOverride] = {}  # Manual load overrides

        # Runtime tracking: detect state transitions and accumulate ON time
        self._last_known_state: dict[str, LoadState] = {}
        self._on_since: dict[str, datetime] = {}  # When load turned ON
        self._daily_runtime_s: dict[str, float] = {}  # Accumulated seconds today
        self._runtime_date: datetime | None = None  # Date of current accumulation

    def register(self, controller: LoadController) -> None:
        """Register a load controller."""
        self._controllers[controller.load_id] = controller
        logger.info("Registered load controller: %s (%s)", controller.load_id, controller.name)

    def unregister(self, load_id: str) -> None:
        """Remove a load controller."""
        self._controllers.pop(load_id, None)

    @property
    def controllers(self) -> dict[str, LoadController]:
        return dict(self._controllers)

    @property
    def command_history(self) -> list[LoadCommand]:
        return list(self._command_history)

    async def get_all_statuses(self) -> list[LoadStatus]:
        """Query status from all registered controllers concurrently."""
        if not self._controllers:
            return []
        results = await asyncio.gather(
            *(ctrl.get_status() for ctrl in self._controllers.values()),
            return_exceptions=True,
        )
        statuses = []
        for ctrl, result in zip(self._controllers.values(), results):
            if isinstance(result, Exception):
                logger.warning("Status poll failed for '%s': %s", ctrl.name, result)
                statuses.append(LoadStatus(
                    load_id=ctrl.load_id,
                    name=ctrl.name,
                    state=LoadState.ERROR,
                    is_available=False,
                    error=str(result),
                ))
            else:
                statuses.append(result)
        return statuses

    # ── Manual Load Overrides ────────────────────────────────

    async def set_load_override(
        self,
        load_id: str,
        state: str,
        timeout_seconds: float = MANUAL_LOAD_OVERRIDE_SECONDS,
        source: str = "user",
    ) -> bool:
        """Set a manual override for a load and apply it immediately.

        Args:
            load_id: The load controller ID.
            state: "on" or "off".
            timeout_seconds: How long the override lasts (default 60 min).
            source: Who set the override.

        Returns:
            True if the load command was applied successfully.
        """
        controller = self._controllers.get(load_id)
        if controller is None:
            logger.warning("set_load_override: unknown load_id '%s'", load_id)
            return False

        override = LoadOverride(
            load_id=load_id,
            state=state,
            set_at=time.monotonic(),
            timeout_seconds=timeout_seconds,
            source=source,
        )
        self._load_overrides[load_id] = override

        if state == "on":
            success = await controller.turn_on()
        else:
            success = await controller.turn_off()

        cmd = LoadCommand(
            load_id=load_id,
            action=state,
            reason=f"manual_override_{source}",
            success=success,
        )
        self._command_history.append(cmd)
        logger.info(
            "Manual load override set: load=%s state=%s timeout=%ds source=%s success=%s",
            load_id, state, timeout_seconds, source, success,
        )
        return success

    def clear_load_override(self, load_id: str) -> None:
        """Remove a manual override for a load."""
        if load_id in self._load_overrides:
            del self._load_overrides[load_id]
            logger.info("Manual load override cleared for '%s'", load_id)

    def get_load_override(self, load_id: str) -> LoadOverride | None:
        """Return the active override for a load, or None if not overridden / expired."""
        override = self._load_overrides.get(load_id)
        if override is None:
            return None
        if override.is_expired:
            del self._load_overrides[load_id]
            logger.info("Manual load override expired for '%s'", load_id)
            return None
        return override

    def get_active_override_load_ids(self) -> set[str]:
        """Return the set of load IDs that have active (non-expired) overrides."""
        expired = [lid for lid, ov in self._load_overrides.items() if ov.is_expired]
        for lid in expired:
            del self._load_overrides[lid]
        return set(self._load_overrides.keys())

    def get_command_history_for_load(self, load_id: str, limit: int = 20) -> list[LoadCommand]:
        """Return recent command history for a specific load."""
        history = [c for c in self._command_history if c.load_id == load_id]
        return history[-limit:]

    async def execute_schedule(self, scheduled: list[ScheduledLoad], current_slot_index: int) -> list[LoadCommand]:
        """Execute load commands based on the current schedule and slot.

        Turns on loads assigned to the current slot, turns off loads not assigned.
        Manual overrides take precedence over the schedule.
        """
        commands: list[LoadCommand] = []

        # Build set of loads that should be ON in current slot
        active_load_ids: set[str] = set()
        for sched in scheduled:
            if current_slot_index in sched.assigned_slots:
                active_load_ids.add(sched.load_id)

        overridden_ids = self.get_active_override_load_ids()

        for load_id, controller in self._controllers.items():
            status = await controller.get_status()

            # Manual override takes precedence — skip scheduler changes
            if load_id in overridden_ids:
                override = self.get_load_override(load_id)
                if override is not None:
                    desired = LoadState.ON if override.state == "on" else LoadState.OFF
                    if status.state != desired:
                        if override.state == "on":
                            success = await controller.turn_on()
                        else:
                            success = await controller.turn_off()
                        cmd = LoadCommand(
                            load_id=load_id,
                            action=override.state,
                            reason="manual_override",
                            success=success,
                        )
                        commands.append(cmd)
                        self._command_history.append(cmd)
                continue

            if load_id in active_load_ids:
                # Should be ON
                if status.state != LoadState.ON:
                    success = await controller.turn_on()
                    cmd = LoadCommand(load_id=load_id, action="on", reason="scheduled", success=success)
                    commands.append(cmd)
                    self._command_history.append(cmd)
            elif load_id in self._shed_loads:
                # Shed for spike — ensure OFF
                if status.state != LoadState.OFF:
                    success = await controller.turn_off()
                    cmd = LoadCommand(load_id=load_id, action="off", reason="spike_shed", success=success)
                    commands.append(cmd)
                    self._command_history.append(cmd)

        return commands

    async def shed_for_spike(self, max_priority: int = 2) -> list[LoadCommand]:
        """Shed all non-essential loads during a price spike.

        Args:
            max_priority: Loads with priority > max_priority will be shed.
                         Priority 1-2 = essential, 3-5 = deferrable.
        """
        commands: list[LoadCommand] = []

        for load_id, controller in self._controllers.items():
            if controller.priority_class > max_priority:
                status = await controller.get_status()
                if status.state == LoadState.ON:
                    success = await controller.turn_off()
                    cmd = LoadCommand(
                        load_id=load_id,
                        action="off",
                        reason=f"spike_shed (priority {controller.priority_class} > {max_priority})",
                        success=success,
                    )
                    commands.append(cmd)
                    self._command_history.append(cmd)
                self._shed_loads.add(load_id)

        if commands:
            logger.warning("Shed %d loads for spike event", len(commands))
        return commands

    async def restore_after_spike(self) -> list[LoadCommand]:
        """Restore loads that were shed during a spike."""
        commands: list[LoadCommand] = []

        for load_id in list(self._shed_loads):
            controller = self._controllers.get(load_id)
            if controller:
                # Don't automatically turn back on — just clear the shed flag.
                # The load scheduler will turn them on if appropriate.
                self._shed_loads.discard(load_id)

        if self._shed_loads:
            logger.info("Cleared spike shed flags for %d loads", len(commands))
        self._shed_loads.clear()
        return commands

    async def shed_for_overload(self, grid_import_w: int, max_grid_import_w: int) -> list[LoadCommand]:
        """Shed loads by priority when grid import exceeds configured maximum.

        Sheds highest-priority-number (least essential) loads first until
        grid import would be reduced below the threshold.
        """
        if max_grid_import_w <= 0:
            return []  # No limit configured

        excess_w = grid_import_w - max_grid_import_w
        if excess_w <= 0:
            return []  # Under limit

        commands: list[LoadCommand] = []

        # Sort controllers by priority (highest number = least essential = shed first)
        sorted_controllers = sorted(
            self._controllers.items(),
            key=lambda kv: kv[1].priority_class,
            reverse=True,
        )

        shed_total = 0
        for load_id, controller in sorted_controllers:
            if shed_total >= excess_w:
                break
            status = await controller.get_status()
            if status.state == LoadState.ON:
                success = await controller.turn_off()
                cmd = LoadCommand(
                    load_id=load_id,
                    action="off",
                    reason=f"overload_shed (grid {grid_import_w}W > max {max_grid_import_w}W)",
                    success=success,
                )
                commands.append(cmd)
                self._command_history.append(cmd)
                shed_total += controller.power_w
                self._shed_loads.add(load_id)

        if commands:
            logger.warning(
                "Shed %d loads (%dW) for grid overload: %dW > %dW max",
                len(commands), shed_total, grid_import_w, max_grid_import_w,
            )
        return commands

    async def execute_current_slot(self, plan, repo=None) -> list[LoadCommand]:
        """Execute load commands based on the current plan slot.

        Reads the current slot's scheduled_loads list and turns on/off
        loads accordingly. Manual overrides take precedence.
        """
        if plan is None:
            return []

        slot = plan.get_current_slot()
        if slot is None:
            return []

        scheduled_names: set[str] = set(slot.scheduled_loads or [])
        commands: list[LoadCommand] = []
        overridden_ids = self.get_active_override_load_ids()

        for load_id, controller in self._controllers.items():
            # Manual override takes precedence
            if load_id in overridden_ids:
                override = self.get_load_override(load_id)
                if override is not None:
                    desired = LoadState.ON if override.state == "on" else LoadState.OFF
                    status = await controller.get_status()
                    if status.state != desired:
                        success = await self._verified_command(
                            controller, override.state, "manual_override", repo=repo,
                        )
                        cmd = LoadCommand(
                            load_id=load_id, action=override.state,
                            reason="manual_override", success=success,
                        )
                        commands.append(cmd)
                        self._command_history.append(cmd)
                continue

            # Determine desired state from schedule
            should_be_on = controller.name in scheduled_names
            status = await controller.get_status()

            if should_be_on and status.state != LoadState.ON:
                success = await self._verified_command(
                    controller, "on", "scheduled", repo=repo,
                )
                cmd = LoadCommand(load_id=load_id, action="on", reason="scheduled", success=success)
                commands.append(cmd)
                self._command_history.append(cmd)
            elif not should_be_on and status.state == LoadState.ON:
                # Load is ON but not scheduled — turn it off
                if load_id not in self._shed_loads:
                    success = await self._verified_command(
                        controller, "off", "schedule_ended", repo=repo,
                    )
                    cmd = LoadCommand(
                        load_id=load_id, action="off", reason="schedule_ended", success=success,
                    )
                    commands.append(cmd)
                    self._command_history.append(cmd)

        return commands

    async def _verified_command(
        self, controller, action: str, reason: str,
        retries: int = 1, verify_delay: float = 1.5, repo=None,
    ) -> bool:
        """Send a command and verify the state change, retrying on failure."""
        desired = LoadState.ON if action == "on" else LoadState.OFF

        for attempt in range(1 + retries):
            if action == "on":
                cmd_success = await controller.turn_on()
            else:
                cmd_success = await controller.turn_off()

            if not cmd_success:
                logger.warning(
                    "Command '%s' failed for '%s' (attempt %d/%d)",
                    action, controller.name, attempt + 1, 1 + retries,
                )
                continue

            # Wait and verify
            await asyncio.sleep(verify_delay)
            status = await controller.get_status()

            if status.state == desired:
                if attempt > 0:
                    logger.info(
                        "Verified '%s' for '%s' after retry (attempt %d)",
                        action, controller.name, attempt + 1,
                    )
                return True

            logger.warning(
                "Verification failed for '%s' on '%s': expected %s, got %s (attempt %d/%d)",
                action, controller.name, desired.value, status.state.value,
                attempt + 1, 1 + retries,
            )

        logger.error(
            "Load command FAILED after %d attempts: '%s' for '%s' (reason: %s)",
            1 + retries, action, controller.name, reason,
        )
        # Persist failure as system event
        if repo is not None:
            try:
                await repo.log_system_event(
                    event_type="load_command_failed",
                    source_module="loads.manager",
                    details={
                        "load_id": controller.load_id,
                        "name": controller.name,
                        "action": action,
                        "reason": reason,
                        "attempts": 1 + retries,
                    },
                    operating_mode="normal",
                    severity="warning",
                )
            except Exception:
                pass
        return False

    async def turn_all_off(self, reason: str = "safety") -> list[LoadCommand]:
        """Emergency: turn off all controllable loads."""
        commands: list[LoadCommand] = []

        for load_id, controller in self._controllers.items():
            success = await controller.turn_off()
            cmd = LoadCommand(load_id=load_id, action="off", reason=reason, success=success)
            commands.append(cmd)
            self._command_history.append(cmd)

        logger.warning("All loads turned OFF (reason: %s)", reason)
        return commands

    async def update_runtime_tracking(self, statuses: list[LoadStatus], repo=None) -> None:
        """Detect state transitions and accumulate ON-time for each load.

        Call this periodically (e.g. every telemetry poll) with fresh statuses.
        When repo is provided, persists runtime to historical_data on OFF transitions.
        """
        now = datetime.now(timezone.utc)

        # Reset daily counters at midnight UTC
        today = now.date()
        if self._runtime_date is None or self._runtime_date != today:
            self._daily_runtime_s.clear()
            # For loads still ON across midnight, reset their start time to now
            # so today's runtime only counts from midnight forward
            for lid in list(self._on_since):
                self._on_since[lid] = now
            self._runtime_date = today

        for status in statuses:
            lid = status.load_id
            prev_state = self._last_known_state.get(lid)
            cur_state = status.state

            if cur_state == LoadState.ON:
                if prev_state != LoadState.ON:
                    # Transition to ON — start tracking
                    self._on_since[lid] = now
                    logger.debug("Load '%s' turned ON (runtime tracking)", status.name)
            else:
                if prev_state == LoadState.ON and lid in self._on_since:
                    # Transition from ON to OFF — accumulate runtime
                    elapsed = (now - self._on_since[lid]).total_seconds()
                    self._daily_runtime_s[lid] = self._daily_runtime_s.get(lid, 0.0) + elapsed
                    logger.info(
                        "Load '%s' ran for %.0fs (total today: %.0fs)",
                        status.name, elapsed, self._daily_runtime_s[lid],
                    )
                    self._on_since.pop(lid, None)

                    # Persist runtime to historical_data for dashboard charts
                    if repo is not None:
                        try:
                            await repo.store_historical(
                                data_type=f"load_runtime_minutes_{lid}",
                                value=self._daily_runtime_s[lid] / 60.0,
                                source="runtime_tracker",
                                resolution="1day",
                            )
                        except Exception:
                            logger.debug("Failed to persist runtime for '%s'", lid, exc_info=True)

            self._last_known_state[lid] = cur_state

    def get_runtime_minutes(self, load_id: str) -> float:
        """Get accumulated runtime in minutes for a load today.

        Includes currently-running time if the load is ON.
        """
        total_s = self._daily_runtime_s.get(load_id, 0.0)
        # Add current running time if ON
        if load_id in self._on_since:
            total_s += (datetime.now(timezone.utc) - self._on_since[load_id]).total_seconds()
        return total_s / 60.0

    def get_all_runtime_minutes(self) -> dict[str, float]:
        """Get runtime in minutes for all tracked loads today."""
        return {lid: self.get_runtime_minutes(lid) for lid in self._last_known_state}

    def get_load_configs(self) -> list[dict]:
        """Build load config dicts for the load scheduler."""
        configs = []
        config_by_name: dict[str, object] = {}
        for dev in self._config.loads.shelly_devices:
            config_by_name[dev.name] = dev
        for dev in self._config.loads.mqtt_load_endpoints:
            config_by_name[dev.name] = dev

        for controller in self._controllers.values():
            cfg = config_by_name.get(controller.name)
            configs.append({
                "id": controller.load_id,
                "name": controller.name,
                "power_w": controller.power_w,
                "priority_class": controller.priority_class,
                "enabled": bool(getattr(cfg, "enabled", True)),
                "earliest_start": getattr(cfg, "earliest_start", "00:00"),
                "latest_end": getattr(cfg, "latest_end", "23:59"),
                "min_runtime_minutes": int(getattr(cfg, "min_runtime_minutes", 0)),
                "ideal_runtime_minutes": int(getattr(cfg, "ideal_runtime_minutes", 0)),
                "max_runtime_minutes": int(getattr(cfg, "max_runtime_minutes", 0)),
                "prefer_solar": bool(getattr(cfg, "prefer_solar", True)),
                "days_of_week": list(getattr(cfg, "days_of_week", [0, 1, 2, 3, 4, 5, 6])),
                "timezone": getattr(self._config.load_profile, "timezone", "UTC"),
            })
        return configs
