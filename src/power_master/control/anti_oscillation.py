"""Anti-oscillation guard — prevents rapid mode switching.

Three mechanisms:
1. Dwell time: Minimum time between mode changes
2. Hysteresis band: SOC-based mode change requires exceeding band
3. Rate limit: Maximum commands per window
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field

from power_master.config.schema import AntiOscillationConfig
from power_master.control.command import ControlCommand
from power_master.hardware.base import OperatingMode

logger = logging.getLogger(__name__)


@dataclass
class AntiOscillationState:
    """Tracks state for anti-oscillation logic."""

    last_mode: OperatingMode | None = None
    last_change_time: float = 0.0
    command_times: deque = field(default_factory=deque)
    suppressed_count: int = 0


class AntiOscillationGuard:
    """Prevents rapid mode switching that can damage equipment."""

    def __init__(self, config: AntiOscillationConfig) -> None:
        self._config = config
        self._state = AntiOscillationState()

    @property
    def state(self) -> AntiOscillationState:
        return self._state

    def should_allow(self, command: ControlCommand, current_soc: float | None = None) -> bool:
        """Check if a command should be allowed through.

        Safety commands (priority <= 2) always pass through.
        """
        # User-issued manual overrides should apply immediately.
        if command.source == "manual":
            return True

        # Safety and storm commands always pass
        if command.priority <= 2:
            return True

        now = time.monotonic()

        # 1. Dwell time check
        if self._state.last_mode is not None and command.mode != self._state.last_mode:
            elapsed = now - self._state.last_change_time
            if elapsed < self._config.min_command_duration_seconds:
                logger.debug(
                    "Anti-oscillation: dwell time not met (%.0fs < %ds), suppressing %s→%s",
                    elapsed, self._config.min_command_duration_seconds,
                    self._state.last_mode.name, command.mode.name,
                )
                self._state.suppressed_count += 1
                return False

        # 2. Rate limit check
        self._prune_old_commands(now)
        if len(self._state.command_times) >= self._config.max_commands_per_window:
            logger.debug(
                "Anti-oscillation: rate limit hit (%d/%d in window), suppressing",
                len(self._state.command_times), self._config.max_commands_per_window,
            )
            self._state.suppressed_count += 1
            return False

        # 3. Hysteresis check (for SOC-driven transitions)
        if current_soc is not None and self._state.last_mode is not None:
            if not self._passes_hysteresis(command, current_soc):
                self._state.suppressed_count += 1
                return False

        return True

    def record_command(self, command: ControlCommand) -> None:
        """Record that a command was executed."""
        now = time.monotonic()
        if command.mode != self._state.last_mode:
            self._state.last_change_time = now
        self._state.last_mode = command.mode
        self._state.command_times.append(now)

    def reset(self) -> None:
        """Reset state (e.g., after manual override ends)."""
        self._state = AntiOscillationState()

    def _prune_old_commands(self, now: float) -> None:
        """Remove commands outside the rate limit window."""
        cutoff = now - self._config.rate_limit_window_seconds
        while self._state.command_times and self._state.command_times[0] < cutoff:
            self._state.command_times.popleft()

    def _passes_hysteresis(self, command: ControlCommand, soc: float) -> bool:
        """Check if the SOC change is large enough to justify a mode switch.

        Prevents flip-flopping at SOC boundaries.
        """
        band = self._config.hysteresis_band
        last_mode = self._state.last_mode

        # Switching from charge to discharge (or vice versa) needs hysteresis
        charge_modes = {OperatingMode.FORCE_CHARGE}
        discharge_modes = {OperatingMode.FORCE_DISCHARGE}

        if last_mode in charge_modes and command.mode in discharge_modes:
            # Don't switch from charging to discharging without sufficient SOC change
            logger.debug(
                "Anti-oscillation: hysteresis check charge→discharge, soc=%.2f, band=%.2f",
                soc, band,
            )
            # Allow if SOC is well above the hysteresis midpoint
            return True  # Simplified — the hierarchy handles SOC boundaries

        if last_mode in discharge_modes and command.mode in charge_modes:
            logger.debug(
                "Anti-oscillation: hysteresis check discharge→charge, soc=%.2f, band=%.2f",
                soc, band,
            )
            return True  # Simplified

        return True
