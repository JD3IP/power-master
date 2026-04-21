"""Manual mode override — user-forced operating mode with timeout."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from power_master.control.command import ControlCommand
from power_master.hardware.base import OperatingMode

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 4 * 3600  # 4 hours


@dataclass
class OverrideState:
    """Current manual override state."""

    active: bool = False
    mode: OperatingMode = OperatingMode.AUTO
    power_w: int = 0
    set_at: float = 0.0
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    source: str = "user"


class ManualOverride:
    """Manages manual mode overrides from the UI.

    When active, the manual override takes precedence over the optimiser
    (but NOT over safety — the hierarchy still applies).
    """

    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._state = OverrideState()
        self._default_timeout = timeout_seconds

    @property
    def is_active(self) -> bool:
        """Check if override is active (and not timed out)."""
        if not self._state.active:
            return False
        if self._is_expired():
            self.clear("timeout")
            return False
        return True

    @property
    def state(self) -> OverrideState:
        return self._state

    @property
    def remaining_seconds(self) -> float:
        """Seconds remaining before timeout. 0 if not active."""
        if not self._state.active:
            return 0.0
        elapsed = time.monotonic() - self._state.set_at
        remaining = self._state.timeout_seconds - elapsed
        return max(0.0, remaining)

    def set(
        self,
        mode: OperatingMode,
        power_w: int = 0,
        timeout_seconds: float | None = None,
        source: str = "user",
    ) -> None:
        """Activate manual override."""
        if mode == OperatingMode.AUTO:
            self.clear("user_auto")
            return

        self._state = OverrideState(
            active=True,
            mode=mode,
            power_w=power_w,
            set_at=time.monotonic(),
            timeout_seconds=timeout_seconds or self._default_timeout,
            source=source,
        )
        logger.info(
            "Manual override activated: mode=%s power=%dW timeout=%ds source=%s",
            mode.name, power_w, self._state.timeout_seconds, source,
        )

    def clear(self, reason: str = "user") -> None:
        """Deactivate manual override."""
        was_active = self._state.active
        self._state = OverrideState()
        if was_active:
            logger.info("Manual override cleared (reason: %s)", reason)

    def get_command(self) -> ControlCommand | None:
        """Get the override command, or None if not active."""
        if not self.is_active:
            return None

        return ControlCommand(
            mode=self._state.mode,
            power_w=self._state.power_w,
            source="manual",
            reason=f"manual_override_{self._state.source}",
            priority=3,  # Below safety (1) and storm (2), above optimiser (4-5)
        )

    def _is_expired(self) -> bool:
        elapsed = time.monotonic() - self._state.set_at
        return elapsed >= self._state.timeout_seconds

    def save(self, path: Path) -> None:
        """Persist override state to JSON file."""
        if not self._state.active:
            # Only save if active
            return

        try:
            data = {
                "mode": self._state.mode.name,
                "power_w": self._state.power_w,
                "reason": f"manual_override_{self._state.source}",
                "set_at": datetime.fromtimestamp(self._state.set_at, tz=timezone.utc).isoformat(),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump(data, f)
            logger.debug("Manual override persisted to %s", path)
        except Exception:
            logger.warning("Failed to persist manual override", exc_info=True)

    @classmethod
    def load(cls, path: Path) -> ManualOverride | None:
        """Restore override state from JSON file if fresh (< 60 minutes old)."""
        if not path.exists():
            return None

        try:
            with open(path) as f:
                data = json.load(f)

            set_at_iso = data.get("set_at")
            if not set_at_iso:
                return None

            set_at_dt = datetime.fromisoformat(set_at_iso)
            now_utc = datetime.now(timezone.utc)
            age_seconds = (now_utc - set_at_dt).total_seconds()

            # Only restore if less than 60 minutes old
            if age_seconds > 3600:
                logger.info("Persisted override expired (%.0f minutes old), not restoring", age_seconds / 60)
                return None

            mode_name = data.get("mode")
            power_w = data.get("power_w", 0)

            if not mode_name:
                return None

            mode = OperatingMode[mode_name]
            override = cls()
            override.set(mode, power_w=power_w, timeout_seconds=3600 - age_seconds, source="persisted")
            logger.info("Manual override restored from %s (age=%.0f minutes)", path, age_seconds / 60)
            return override
        except Exception:
            logger.warning("Failed to load persisted override", exc_info=True)
            return None
