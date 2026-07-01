"""Control command model and dispatch."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from power_master.hardware.base import CommandResult, InverterAdapter, InverterCommand, OperatingMode
from power_master.optimisation.plan import PlanSlot, SlotMode

logger = logging.getLogger(__name__)


class CommandSourceType(str, Enum):
    """Classification of command sources for audit logging."""

    MANUAL = "MANUAL"
    SAFETY = "SAFETY"
    STORM = "STORM"
    OPTIMIZER = "OPTIMIZER"

# Map SlotMode to OperatingMode
_SLOT_TO_OP: dict[SlotMode, OperatingMode] = {
    SlotMode.SELF_USE: OperatingMode.SELF_USE,
    SlotMode.SELF_USE_ZERO_EXPORT: OperatingMode.SELF_USE_ZERO_EXPORT,
    SlotMode.FORCE_CHARGE: OperatingMode.FORCE_CHARGE,
    SlotMode.FORCE_DISCHARGE: OperatingMode.FORCE_DISCHARGE,
}


@dataclass
class ControlCommand:
    """A high-level control command with metadata."""

    mode: OperatingMode
    power_w: int = 0
    source: str = "optimiser"  # optimiser, manual, safety, storm
    reason: str = ""
    priority: int = 5  # 1=highest (safety), 5=lowest (opportunistic)
    # Free-window force-charge: when True, the safety hierarchy will NOT cut
    # charging at max SOC — keep pushing max charge current (BMS limits actual
    # absorption) to soak up all available free energy.
    allow_charge_at_max_soc: bool = False
    # Optional grid export cap (W) to apply with this command. Used by
    # FEED_IN_FIRST / zero-export modes; None leaves the inverter's export limit
    # at its default (max).
    export_limit_w: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def get_source_type(self) -> CommandSourceType:
        """Derive the audit classification of the command source."""
        if self.source == "manual":
            return CommandSourceType.MANUAL
        if self.source == "safety" or self.priority <= 2:
            return CommandSourceType.SAFETY
        if self.source == "storm":
            return CommandSourceType.STORM
        return CommandSourceType.OPTIMIZER


def command_from_slot(slot: PlanSlot, source: str = "optimiser") -> ControlCommand:
    """Build a ControlCommand from a plan slot."""
    mode = _SLOT_TO_OP.get(slot.mode, OperatingMode.SELF_USE)
    return ControlCommand(
        mode=mode,
        power_w=slot.target_power_w,
        source=source,
        reason=f"plan_slot_{slot.index}",
        priority=5,
        allow_charge_at_max_soc=slot.allow_charge_at_max_soc,
    )


async def dispatch_command(
    adapter: InverterAdapter,
    command: ControlCommand,
) -> CommandResult:
    """Send a control command to the inverter adapter."""
    # Resolve the export limit to apply: explicit command value wins; zero-export
    # mode forces 0; otherwise leave it to the adapter's default.
    if command.export_limit_w is not None:
        export_limit_w = command.export_limit_w
    elif command.mode == OperatingMode.SELF_USE_ZERO_EXPORT:
        export_limit_w = 0
    else:
        export_limit_w = None

    inverter_cmd = InverterCommand(
        mode=command.mode,
        power_w=command.power_w,
        export_limit_w=export_limit_w,
    )

    logger.info(
        "Dispatching command: mode=%s power=%dW source=%s reason=%s",
        command.mode.name, command.power_w, command.source, command.reason,
    )

    result = await adapter.send_command(inverter_cmd)

    if not result.success:
        logger.error("Command dispatch failed: %s", result.message)

    return result
