"""Tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from power_master.config.manager import ConfigManager
from power_master.config.schema import AppConfig


class TestAppConfig:
    def test_default_config_is_valid(self) -> None:
        config = AppConfig()
        assert config.battery.capacity_wh == 10000
        assert config.planning.horizon_hours == 48
        assert config.arbitrage.break_even_delta_cents == 5
        assert config.storm.probability_threshold == 0.70

    def test_battery_soc_limits(self) -> None:
        config = AppConfig()
        assert config.battery.soc_min_hard < config.battery.soc_min_soft
        assert config.battery.soc_max_soft < config.battery.soc_max_hard

    def test_fixed_costs_defaults(self) -> None:
        config = AppConfig()
        assert config.fixed_costs.monthly_supply_charge_cents == 9000
        assert config.fixed_costs.daily_access_fee_cents == 100
        assert config.fixed_costs.hedging_per_kwh_cents == 2

    def test_custom_values(self) -> None:
        config = AppConfig(
            battery={"capacity_wh": 20000, "max_charge_rate_w": 8000},
            arbitrage={"break_even_delta_cents": 8, "spike_threshold_cents": 200},
        )
        assert config.battery.capacity_wh == 20000
        assert config.arbitrage.break_even_delta_cents == 8
        assert config.arbitrage.spike_threshold_cents == 200


class TestConfigManager:
    def test_load_defaults_only(self, tmp_path: Path) -> None:
        defaults_file = tmp_path / "defaults.yaml"
        defaults_file.write_text(
            "battery:\n  capacity_wh: 15000\ndb:\n  path: test.db\n"
        )
        mgr = ConfigManager(defaults_path=defaults_file, user_path=tmp_path / "user.yaml")
        config = mgr.load()
        assert config.battery.capacity_wh == 15000

    def test_user_overrides(self, tmp_path: Path) -> None:
        defaults_file = tmp_path / "defaults.yaml"
        defaults_file.write_text("battery:\n  capacity_wh: 10000\n")
        user_file = tmp_path / "user.yaml"
        user_file.write_text("battery:\n  capacity_wh: 20000\n")
        mgr = ConfigManager(defaults_path=defaults_file, user_path=user_file)
        config = mgr.load()
        assert config.battery.capacity_wh == 20000

    def test_deep_merge(self) -> None:
        base = {"a": {"b": 1, "c": 2}, "d": 3}
        override = {"a": {"b": 10}, "e": 5}
        result = ConfigManager._deep_merge(base, override)
        assert result == {"a": {"b": 10, "c": 2}, "d": 3, "e": 5}

    def test_to_json(self, tmp_path: Path) -> None:
        defaults_file = tmp_path / "defaults.yaml"
        defaults_file.write_text("db:\n  path: test.db\n")
        mgr = ConfigManager(defaults_path=defaults_file, user_path=tmp_path / "u.yaml")
        mgr.load()
        json_str = mgr.to_json()
        assert '"capacity_wh"' in json_str
