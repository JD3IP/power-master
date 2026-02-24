"""Control command model and dispatch."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from power_master.hardware.base import CommandResult, InverterAdapter, InverterCommand, OperatingMode
from power_master.optimisation.plan import PlanSlot, SlotMode

logger = logging.getLogger(__name__)

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
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def command_from_slot(slot: PlanSlot, source: str = "optimiser") -> ControlCommand:
    """Build a ControlCommand from a plan slot."""
    mode = _SLOT_TO_OP.get(slot.mode, OperatingMode.SELF_USE)
    return ControlCommand(
        mode=mode,
        power_w=slot.target_power_w,
        source=source,
        reason=f"plan_slot_{slot.index}",
        priority=5,
    )


async def dispatch_command(
    adapter: InverterAdapter,
    command: ControlCommand,
) -> CommandResult:
    """Send a control command to the inverter adapter."""
    inverter_cmd = InverterCommand(
        mode=command.mode,
        power_w=command.power_w,
        export_limit_w=0 if command.mode == OperatingMode.SELF_USE_ZERO_EXPORT else None,
    )

    logger.info(
        "Dispatching command: mode=%s power=%dW source=%s reason=%s",
        command.mode.name, command.power_w, command.source, command.reason,
    )

    result = await adapter.send_command(inverter_cmd)

    if not result.success:
        logger.error("Command dispatch failed: %s", result.message)

    return result
