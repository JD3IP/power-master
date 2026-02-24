"""Resilience state machine — manages system degradation and recovery."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from power_master.config.schema import AppConfig
from power_master.resilience.health_check import HealthChecker
from power_master.resilience.modes import ResilienceLevel

logger = logging.getLogger(__name__)


@dataclass
class ResilienceState:
    """Current resilience state."""

    level: ResilienceLevel = ResilienceLevel.NORMAL
    unhealthy_providers: list[str] = field(default_factory=list)
    last_evaluation_at: float = 0.0
    level_changed_at: float = 0.0
    transition_count: int = 0


class ResilienceManager:
    """State machine managing system resilience levels.

    Evaluates provider health and determines the appropriate operating level.
    """

    def __init__(self, config: AppConfig, health_checker: HealthChecker) -> None:
        self._config = config
        self._health = health_checker
        self._state = ResilienceState()

    @property
    def state(self) -> ResilienceState:
        return self._state

    @property
    def level(self) -> ResilienceLevel:
        return self._state.level

    @property
    def is_normal(self) -> bool:
        return self._state.level == ResilienceLevel.NORMAL

    def evaluate(self) -> bool:
        """Evaluate current health and update resilience level.

        Returns:
            True if the resilience level changed.
        """
        now = time.monotonic()
        self._state.last_evaluation_at = now

        unhealthy = self._health.get_unhealthy()
        self._state.unhealthy_providers = unhealthy

        new_level = self._determine_level(unhealthy)
        old_level = self._state.level

        if new_level != old_level:
            self._state.level = new_level
            self._state.level_changed_at = now
            self._state.transition_count += 1

            if new_level.value > old_level.value:
                logger.warning(
                    "Resilience degraded: %s → %s (unhealthy: %s)",
                    old_level.value, new_level.value, unhealthy,
                )
            else:
                logger.info(
                    "Resilience improved: %s → %s",
                    old_level.value, new_level.value,
                )
            return True

        return False

    def _determine_level(self, unhealthy: list[str]) -> ResilienceLevel:
        """Determine resilience level from unhealthy providers."""
        if not unhealthy:
            return ResilienceLevel.NORMAL

        has_inverter = "inverter" in unhealthy
        has_tariff = "tariff" in unhealthy
        has_forecast = any(
            p in unhealthy for p in ("solar_forecast", "weather_forecast")
        )

        # Hardware failure is most critical
        if has_inverter:
            return ResilienceLevel.DEGRADED_HARDWARE

        # Multiple failures → safe mode
        if has_tariff and has_forecast:
            return ResilienceLevel.SAFE_MODE

        if has_tariff:
            return ResilienceLevel.DEGRADED_TARIFF

        if has_forecast:
            return ResilienceLevel.DEGRADED_FORECAST

        # Other providers — degraded but functional
        return ResilienceLevel.DEGRADED_FORECAST

    def force_level(self, level: ResilienceLevel) -> None:
        """Force a specific resilience level (for testing/emergency)."""
        old = self._state.level
        self._state.level = level
        self._state.level_changed_at = time.monotonic()
        self._state.transition_count += 1
        logger.warning("Resilience forced: %s → %s", old.value, level.value)
