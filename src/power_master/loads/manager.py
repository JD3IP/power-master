"""Load orchestration manager — coordinates all load controllers."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from power_master.config.schema import AppConfig
from power_master.loads.base import LoadController, LoadState, LoadStatus
from power_master.optimisation.load_scheduler import ScheduledLoad

logger = logging.getLogger(__name__)


@dataclass
class LoadCommand:
    """A command issued to a load."""

    load_id: str
    action: str  # "on" or "off"
    reason: str
    issued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    success: bool = False


class LoadManager:
    """Manages all controllable loads.

    Responsibilities:
    - Register and track load controllers
    - Execute scheduled load commands from the load scheduler
    - Handle spike responses (shed non-essential loads)
    - Track command history
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._controllers: dict[str, LoadController] = {}
        self._command_history: list[LoadCommand] = []
        self._shed_loads: set[str] = set()  # Loads currently shed for spike

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
        """Query status from all registered controllers."""
        statuses = []
        for controller in self._controllers.values():
            status = await controller.get_status()
            statuses.append(status)
        return statuses

    async def execute_schedule(self, scheduled: list[ScheduledLoad], current_slot_index: int) -> list[LoadCommand]:
        """Execute load commands based on the current schedule and slot.

        Turns on loads assigned to the current slot, turns off loads not assigned.
        """
        commands: list[LoadCommand] = []

        # Build set of loads that should be ON in current slot
        active_load_ids: set[str] = set()
        for sched in scheduled:
            if current_slot_index in sched.assigned_slots:
                active_load_ids.add(sched.load_id)

        for load_id, controller in self._controllers.items():
            status = await controller.get_status()

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
