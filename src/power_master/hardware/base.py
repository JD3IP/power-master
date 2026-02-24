"""Abstract hardware adapter protocol for inverter control."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol, runtime_checkable

from power_master.hardware.telemetry import Telemetry


class OperatingMode(IntEnum):
    """Inverter operating modes."""

    AUTO = 0
    SELF_USE = 1
    SELF_USE_ZERO_EXPORT = 2
    FORCE_CHARGE = 3
    FORCE_DISCHARGE = 4
    FORCE_CHARGE_ZERO_IMPORT = 5


@dataclass
class InverterCommand:
    """Command to send to the inverter."""

    mode: OperatingMode
    power_w: int = 0  # Absolute value. Direction determined by mode.
    export_limit_w: int | None = None  # None = no limit change


@dataclass
class CommandResult:
    """Result of an inverter command execution."""

    success: bool
    latency_ms: int
    message: str = ""
    raw_response: dict | None = None


@runtime_checkable
class InverterAdapter(Protocol):
    """Protocol for hardware adapters to implement."""

    async def connect(self) -> None:
        """Establish connection to the inverter."""
        ...

    async def disconnect(self) -> None:
        """Close connection to the inverter."""
        ...

    async def get_telemetry(self) -> Telemetry:
        """Read current telemetry from the inverter."""
        ...

    async def send_command(self, command: InverterCommand) -> CommandResult:
        """Send a control command to the inverter."""
        ...

    async def is_connected(self) -> bool:
        """Check if the connection is active."""
        ...
