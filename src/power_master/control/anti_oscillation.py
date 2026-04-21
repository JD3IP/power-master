"""Anti-oscillation guard — prevents rapid mode switching.

Three mechanisms:
1. Dwell time: Minimum time between mode changes
2. Pattern detection: Suppresses oscillation if same two modes alternate 3+ times
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
    mode_history: deque = field(default_factory=lambda: deque(maxlen=100))  # (timestamp, mode) tuples
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

        # 3. Hysteresis check (for pattern-based oscillation detection)
        if self._state.last_mode is not None:
            if not self._passes_hysteresis(command):
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
        # Record mode change in history for oscillation pattern detection
        self._state.mode_history.append((now, command.mode))
        # Prune entries older than the rate limit window
        cutoff = now - self._config.rate_limit_window_seconds
        while self._state.mode_history and self._state.mode_history[0][0] < cutoff:
            self._state.mode_history.popleft()

    def reset(self) -> None:
        """Reset state (e.g., after manual override ends)."""
        self._state = AntiOscillationState()

    def _prune_old_commands(self, now: float) -> None:
        """Remove commands outside the rate limit window."""
        cutoff = now - self._config.rate_limit_window_seconds
        while self._state.command_times and self._state.command_times[0] < cutoff:
            self._state.command_times.popleft()

    def _passes_hysteresis(self, command: ControlCommand) -> bool:
        """Check for rapid mode oscillation patterns.

        Detects if the same two modes have alternated 3+ times within the
        rate limit window and suppresses further switching if so. This prevents
        equipment damage from rapid mode flipping regardless of SOC, dwell time,
        or other conditions.
        """
        if len(self._state.mode_history) < 3:
            # Not enough history to detect oscillation
            return True

        last_mode = self._state.last_mode

        # Count alternations in mode_history between last_mode and command.mode
        alternation_count = 0
        last_recorded_mode = None

        for _, mode in self._state.mode_history:
            if last_recorded_mode is not None:
                # Check if this is an alternation between the two modes
                if (
                    (last_recorded_mode == last_mode and mode == command.mode)
                    or (last_recorded_mode == command.mode and mode == last_mode)
                ):
                    alternation_count += 1
            last_recorded_mode = mode

        if alternation_count >= 3:
            logger.warning(
                "Anti-oscillation: rapid oscillation detected (%d alternations between %s and %s), suppressing",
                alternation_count, last_mode.name, command.mode.name,
            )
            return False

        return True
