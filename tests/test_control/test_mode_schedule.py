"""Tests for the user-defined inverter mode schedule."""

from __future__ import annotations

from datetime import datetime, timezone

from power_master.config.schema import ModeScheduleConfig, ModeScheduleRule
from power_master.control.mode_schedule import ModeScheduler
from power_master.hardware.base import OperatingMode


def _sched(rules, enabled=True, tz="Australia/Brisbane"):
    return ModeScheduler(ModeScheduleConfig(enabled=enabled, rules=rules), tz)


# Brisbane is UTC+10 (no DST): local = UTC + 10h.
def _utc(local_h, local_m=0):
    return datetime(2026, 7, 1, (local_h - 10) % 24, local_m, tzinfo=timezone.utc)


class TestModeScheduler:
    def test_disabled_returns_none(self) -> None:
        s = _sched([ModeScheduleRule(mode="feed_in_first", windows=["16:00-22:00"])], enabled=False)
        assert s.get_command(_utc(17)) is None

    def test_in_window_returns_command(self) -> None:
        s = _sched([ModeScheduleRule(
            name="peak", mode="feed_in_first", windows=["16:00-22:00"], export_limit_w=500,
        )])
        cmd = s.get_command(_utc(17))
        assert cmd is not None
        assert cmd.mode == OperatingMode.FEED_IN_FIRST
        assert cmd.export_limit_w == 500
        assert cmd.source == "schedule"
        # Below safety(1)/storm(2), above optimiser plan(5).
        assert 2 < cmd.priority < 5

    def test_outside_window_returns_none(self) -> None:
        s = _sched([ModeScheduleRule(mode="feed_in_first", windows=["16:00-22:00"])])
        assert s.get_command(_utc(12)) is None
        # End is exclusive: 22:00 is out.
        assert s.get_command(_utc(22, 0)) is None
        assert s.get_command(_utc(21, 59)) is not None

    def test_day_filter(self) -> None:
        # 2026-07-01 is a Wednesday (weekday()==2).
        rule = ModeScheduleRule(mode="feed_in_first", windows=["16:00-22:00"], days=[5, 6])
        assert _sched([rule]).get_command(_utc(17)) is None
        rule_wed = ModeScheduleRule(mode="feed_in_first", windows=["16:00-22:00"], days=[2])
        assert _sched([rule_wed]).get_command(_utc(17)) is not None

    def test_midnight_crossing_window(self) -> None:
        s = _sched([ModeScheduleRule(mode="force_charge", windows=["22:00-06:00"], power_w=4000)])
        assert s.get_command(_utc(23)) is not None
        assert s.get_command(_utc(3)) is not None
        assert s.get_command(_utc(12)) is None
        cmd = s.get_command(_utc(23))
        assert cmd.mode == OperatingMode.FORCE_CHARGE
        assert cmd.power_w == 4000

    def test_first_matching_rule_wins(self) -> None:
        s = _sched([
            ModeScheduleRule(name="a", mode="feed_in_first", windows=["16:00-22:00"], export_limit_w=500),
            ModeScheduleRule(name="b", mode="force_discharge", windows=["16:00-22:00"], power_w=3000),
        ])
        cmd = s.get_command(_utc(17))
        assert cmd.reason == "schedule:a"
        assert cmd.mode == OperatingMode.FEED_IN_FIRST

    def test_disabled_rule_skipped(self) -> None:
        s = _sched([
            ModeScheduleRule(name="off", enabled=False, mode="feed_in_first", windows=["16:00-22:00"]),
            ModeScheduleRule(name="on", mode="force_discharge", windows=["16:00-22:00"], power_w=3000),
        ])
        cmd = s.get_command(_utc(17))
        assert cmd.reason == "schedule:on"

    def test_update_config(self) -> None:
        s = _sched([], enabled=False)
        assert s.get_command(_utc(17)) is None
        s.update_config(ModeScheduleConfig(
            enabled=True,
            rules=[ModeScheduleRule(mode="feed_in_first", windows=["16:00-22:00"], export_limit_w=250)],
        ), "Australia/Brisbane")
        cmd = s.get_command(_utc(17))
        assert cmd is not None and cmd.export_limit_w == 250
