"""Free-window import-cap-aware orchestrator.

During free windows (and generally when "charge everything" is active), coordinates
controllable draws (battery grid-charge + controlled loads HWS/pool) so their SUM
never exceeds max_grid_import_w. Allocates headroom down a configurable priority ladder,
throttling the battery grid-charge setpoint and shedding/staggering loads to stay safe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from power_master.config.schema import AppConfig
    from power_master.control.command import ControlCommand
    from power_master.loads.base import LoadController

logger = logging.getLogger(__name__)


@dataclass
class AllocationResult:
    """Result of free-window allocation attempt."""

    allocated_w: int  # Total W allocated to controllable loads
    battery_grid_charge_setpoint_w: int  # Throttled battery grid-charge setpoint (0 if should stop)
    loads_to_shed: set[str]  # Load IDs to shed to stay under cap
    headroom_available_w: int  # Computed available headroom (measured uncontrollables excluded)
    is_cap_exhausted: bool  # True if free-window daily cap is exhausted


class FreeWindowOrchestrator:
    """Allocates headroom in free windows using a configurable priority ladder.

    Coordinates battery grid-charge + controlled loads so total import never
    exceeds max_grid_import_w. Throttles battery setpoint; sheds/stags loads
    by priority to stay under cap.

    Design:
    - Reuses the existing load-priority ladder (priority_class on LoadController).
    - Adds a config knob for "free-window load priority order" (battery > HWS > pool, etc).
    - In free window: compute headroom = max_grid_import_w - (uncontrollable house draw),
      allocate it to the ladder in order, shed/throttle anything that doesn't fit.
    - Coordinates with FreeWindowCapTracker: when daily cap is exhausted, battery
      grid-charge stops (setpoint = 0), slots become paid → no-panic policy prevents
      grid-charge there anyway.
    """

    def __init__(
        self,
        config: AppConfig,
        controllers: dict[str, LoadController],
    ) -> None:
        """Initialize the orchestrator.

        Args:
            config: AppConfig instance (contains max_grid_import_w, battery power limits).
            controllers: Dict of load_id -> LoadController.
        """
        self._config = config
        self._controllers = controllers
        self._max_grid_import_w = config.battery.max_grid_import_w

    async def allocate_for_free_window(
        self,
        current_grid_import_w: int,
        measured_uncontrollable_load_w: int,
        battery_max_charge_w: int,
        is_in_free_window: bool,
        cap_exhausted: bool = False,
    ) -> AllocationResult:
        """Allocate headroom in the free window.

        Computes available headroom = max_grid_import_w - measured_uncontrollable_load_w.
        If in free window and cap not exhausted: allocates battery grid-charge +
        controlled loads down the priority ladder. If cap exhausted: battery grid-charge
        setpoint = 0 (no grid-charge when cap hit, let no-panic policy prevent it anyway).

        Args:
            current_grid_import_w: Measured grid import at this tick (for logging).
            measured_uncontrollable_load_w: House base load (non-controllable); used to
                compute headroom.
            battery_max_charge_w: Configured battery max charge rate (e.g., 5000W).
            is_in_free_window: True if currently in a free-window tariff period.
            cap_exhausted: True if FreeWindowCapTracker.get_remaining_cap() == 0.

        Returns:
            AllocationResult with allocated_w, battery_setpoint, loads_to_shed, headroom.
        """
        if self._max_grid_import_w <= 0:
            # No limit configured; return full setpoints.
            return AllocationResult(
                allocated_w=0,
                battery_grid_charge_setpoint_w=battery_max_charge_w if is_in_free_window else 0,
                loads_to_shed=set(),
                headroom_available_w=999999,  # Effectively unlimited
                is_cap_exhausted=False,
            )

        # Compute headroom = max allowed - uncontrollable base load
        headroom_w = max(0, self._max_grid_import_w - measured_uncontrollable_load_w)

        # If cap is exhausted or not in free window, stop battery grid-charge
        if cap_exhausted or not is_in_free_window:
            return AllocationResult(
                allocated_w=0,
                battery_grid_charge_setpoint_w=0,
                loads_to_shed=set(),
                headroom_available_w=headroom_w,
                is_cap_exhausted=cap_exhausted,
            )

        # In free window with cap available: allocate down the priority ladder
        # Priority order (by priority_class, ascending = most important first):
        #   1. Battery grid-charge (virtual priority 0, highest)
        #   2. Controlled loads by their priority_class (1 = critical, 5 = opportunistic)

        # Start by trying to allocate full battery setpoint
        battery_setpoint_w = battery_max_charge_w
        allocated_w = battery_setpoint_w
        loads_to_shed = set()

        # If battery alone exceeds headroom, throttle it
        if allocated_w > headroom_w:
            battery_setpoint_w = headroom_w
            allocated_w = headroom_w
            logger.info(
                "Free-window: battery grid-charge throttled from %dW to %dW "
                "(headroom %dW; uncontrollable %dW)",
                battery_max_charge_w, battery_setpoint_w,
                headroom_w, measured_uncontrollable_load_w,
            )
            # Still check loads to mark them for shedding
            remaining_headroom_w = 0  # No room left for any loads
        else:
            # Battery fits; allocate remainder to controlled loads (by priority)
            remaining_headroom_w = headroom_w - allocated_w

        # Sort controllers by priority_class (ascending = highest priority first)
        # and check which loads can fit
        sorted_loads = sorted(
            self._controllers.items(),
            key=lambda kv: kv[1].priority_class,
        )

        for load_id, controller in sorted_loads:
            # Check if this load is ON (or intended to be ON by scheduler)
            # If it can fit in remaining headroom, keep it; else shed it.
            load_power_w = controller.power_w
            if load_power_w == 0:
                continue  # Skip loads with no rated power

            if load_power_w <= remaining_headroom_w:
                # Load fits; keep it
                allocated_w += load_power_w
                remaining_headroom_w -= load_power_w
                logger.debug(
                    "Free-window: load '%s' (priority %d, %dW) fits; remaining headroom %dW",
                    load_id, controller.priority_class, load_power_w, remaining_headroom_w,
                )
            else:
                # Load doesn't fit; shed it
                loads_to_shed.add(load_id)
                logger.info(
                    "Free-window: shedding load '%s' (priority %d, %dW) — exceeds headroom %dW",
                    load_id, controller.priority_class, load_power_w, remaining_headroom_w,
                )

        return AllocationResult(
            allocated_w=allocated_w,
            battery_grid_charge_setpoint_w=battery_setpoint_w,
            loads_to_shed=loads_to_shed,
            headroom_available_w=headroom_w,
            is_cap_exhausted=False,
        )

    async def shed_loads(self, load_ids: set[str]) -> list:
        """Turn off specified loads.

        Returns list of LoadCommand executed.
        """
        from power_master.loads.manager import LoadCommand

        commands = []
        for load_id in load_ids:
            controller = self._controllers.get(load_id)
            if controller is None:
                continue
            try:
                status = await controller.get_status()
                if status.state.value == "on":
                    success = await controller.turn_off()
                    cmd = LoadCommand(
                        load_id=load_id,
                        action="off",
                        reason="free_window_cap_shed",
                        success=success,
                    )
                    commands.append(cmd)
                    if success:
                        logger.info("Shed load '%s' for free-window cap", load_id)
            except Exception as e:
                logger.warning("Failed to shed load '%s': %s", load_id, e)

        return commands

    def throttle_battery_command(
        self,
        command: ControlCommand,
        allowed_battery_w: int,
    ) -> ControlCommand:
        """Throttle a battery grid-charge command to stay under cap.

        If the command is a FORCE_CHARGE and the requested power exceeds
        allowed_battery_w, clampes power_w to the allowed value.

        Args:
            command: The control command (may be modified in-place).
            allowed_battery_w: Max battery charge power allowed by the free-window cap.

        Returns:
            The (possibly modified) command.
        """
        from power_master.hardware.base import OperatingMode

        if command.mode == OperatingMode.FORCE_CHARGE:
            if command.power_w > allowed_battery_w:
                logger.info(
                    "Free-window: throttling battery grid-charge from %dW to %dW "
                    "(cap allocation)",
                    command.power_w, allowed_battery_w,
                )
                command.power_w = allowed_battery_w

        return command
