"""User-defined inverter-mode schedule.

Applies time-of-day rules that force a specific inverter mode (e.g. export
priority / Feed-in First during the evening peak), overriding the optimiser
plan while active. Sits below the safety/storm/SOC-floor hierarchy — a
scheduled command is still passed through `evaluate_hierarchy`.
"""

from __future__ import annotations

import logging
from datetime import datetime

from power_master.config.schema import ModeScheduleConfig, ModeScheduleRule
from power_master.control.command import ControlCommand
from power_master.hardware.base import OperatingMode
from power_master.timezone_utils import resolve_timezone

logger = logging.getLogger(__name__)

# Above the optimiser plan (priority 5), below safety (1) and storm (2).
SCHEDULE_PRIORITY = 4

_MODE_MAP: dict[str, OperatingMode] = {
    "self_use": OperatingMode.SELF_USE,
    "self_use_zero_export": OperatingMode.SELF_USE_ZERO_EXPORT,
    "feed_in_first": OperatingMode.FEED_IN_FIRST,
    "force_charge": OperatingMode.FORCE_CHARGE,
    "force_discharge": OperatingMode.FORCE_DISCHARGE,
}


def _time_in_window(hm: tuple[int, int], window: str) -> bool:
    """True if local (hour, minute) falls in an "HH:MM-HH:MM" window.

    Half-open [start, end); midnight-crossing windows (start > end) are supported.
    """
    start_str, end_str = window.split("-")
    sh, sm = map(int, start_str.split(":"))
    eh, em = map(int, end_str.split(":"))
    start, end = (sh, sm), (eh, em)
    if start > end:
        return hm >= start or hm < end
    return start <= hm < end


class ModeScheduler:
    """Resolves the active mode-schedule rule for the current local time."""

    def __init__(self, config: ModeScheduleConfig, timezone_name: str = "UTC") -> None:
        self._config = config
        self._tz = resolve_timezone(timezone_name)

    def update_config(self, config: ModeScheduleConfig, timezone_name: str | None = None) -> None:
        self._config = config
        if timezone_name is not None:
            self._tz = resolve_timezone(timezone_name)

    def active_rule(self, now_local: datetime) -> ModeScheduleRule | None:
        """Return the first enabled rule whose day + window match, else None."""
        if not self._config.enabled:
            return None
        hm = (now_local.hour, now_local.minute)
        dow = now_local.weekday()  # 0=Mon … 6=Sun
        for rule in self._config.rules:
            if not rule.enabled or not rule.windows:
                continue
            if rule.days and dow not in rule.days:
                continue
            if any(_time_in_window(hm, w) for w in rule.windows):
                return rule
        return None

    def get_command(self, now_utc: datetime) -> ControlCommand | None:
        """Build the ControlCommand for the active rule, or None if no rule applies."""
        rule = self.active_rule(now_utc.astimezone(self._tz))
        if rule is None:
            return None
        mode = _MODE_MAP.get(rule.mode)
        if mode is None:  # guarded by schema, but stay safe
            logger.warning("Mode-schedule rule '%s' has unknown mode '%s'", rule.name, rule.mode)
            return None
        return ControlCommand(
            mode=mode,
            power_w=int(rule.power_w) if rule.power_w is not None else 0,
            source="schedule",
            reason=f"schedule:{rule.name or rule.mode}",
            priority=SCHEDULE_PRIORITY,
            export_limit_w=rule.export_limit_w,
        )
