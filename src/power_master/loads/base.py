"""Protocol for load controllers (Shelly, MQTT, etc.)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class LoadState(str, Enum):
    """Current state of a controllable load."""

    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"
    ERROR = "error"


@dataclass
class LoadStatus:
    """Status report from a load controller."""

    load_id: str
    name: str
    state: LoadState
    power_w: int = 0
    is_available: bool = True
    error: str | None = None


@runtime_checkable
class LoadController(Protocol):
    """Protocol for all load control adapters.

    Implementations: ShellyAdapter, MQTTLoadAdapter.
    """

    @property
    def load_id(self) -> str:
        """Unique identifier for this load."""
        ...

    @property
    def name(self) -> str:
        """Human-readable name."""
        ...

    @property
    def power_w(self) -> int:
        """Rated power consumption in watts."""
        ...

    @property
    def priority_class(self) -> int:
        """Priority class (1=critical, 5=opportunistic)."""
        ...

    async def turn_on(self) -> bool:
        """Turn the load on. Returns True on success."""
        ...

    async def turn_off(self) -> bool:
        """Turn the load off. Returns True on success."""
        ...

    async def get_status(self) -> LoadStatus:
        """Get current load status."""
        ...

    async def is_available(self) -> bool:
        """Check if the load controller is reachable."""
        ...
