"""Storm probability monitoring — tracks state transitions."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from power_master.config.schema import StormConfig
from power_master.storm.reserve import calculate_reserve_soc

logger = logging.getLogger(__name__)


@dataclass
class StormState:
    """Current storm monitoring state."""

    probability: float = 0.0
    is_active: bool = False
    reserve_soc: float = 0.0
    activated_at: float | None = None
    deactivated_at: float | None = None
    transition_count: int = 0


class StormMonitor:
    """Monitors storm probability and manages reserve state transitions."""

    def __init__(self, config: StormConfig) -> None:
        self._config = config
        self._state = StormState()

    @property
    def state(self) -> StormState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state.is_active

    @property
    def reserve_soc(self) -> float:
        return self._state.reserve_soc

    def update(self, storm_probability: float) -> bool:
        """Update storm probability and return True if state changed.

        Args:
            storm_probability: Current storm probability (0-1).

        Returns:
            True if the active/inactive state transitioned.
        """
        self._state.probability = storm_probability
        reserve = calculate_reserve_soc(storm_probability, self._config)
        self._state.reserve_soc = reserve

        was_active = self._state.is_active
        is_now_active = reserve > 0

        if is_now_active and not was_active:
            self._state.is_active = True
            self._state.activated_at = time.monotonic()
            self._state.transition_count += 1
            logger.warning(
                "Storm reserve ACTIVATED: probability=%.0f%% reserve_soc=%.0f%%",
                storm_probability * 100, reserve * 100,
            )
            return True

        if not is_now_active and was_active:
            self._state.is_active = False
            self._state.deactivated_at = time.monotonic()
            self._state.transition_count += 1
            logger.info("Storm reserve DEACTIVATED: probability=%.0f%%", storm_probability * 100)
            return True

        return False

    def reset(self) -> None:
        """Reset storm state."""
        self._state = StormState()
