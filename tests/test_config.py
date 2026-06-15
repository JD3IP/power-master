"""Tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from power_master.config.manager import ConfigManager
from power_master.config.schema import AppConfig, EVConfig, EVModeConfig


class TestAppConfig:
    def test_default_config_is_valid(self) -> None:
        config = AppConfig()
        assert config.battery.capacity_wh == 10000
        assert config.planning.horizon_hours == 48
        assert config.arbitrage.break_even_delta_cents == 5
        assert config.storm.probability_threshold == 0.70
        assert config.battery_targets.daytime_reserve_soc_target == 0.50

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
            battery_targets={"daytime_reserve_soc_target": 0.6, "daytime_reserve_start_hour": 9, "daytime_reserve_end_hour": 17},
        )
        assert config.battery.capacity_wh == 20000
        assert config.arbitrage.break_even_delta_cents == 8
        assert config.arbitrage.spike_threshold_cents == 200
        assert config.battery_targets.daytime_reserve_soc_target == 0.6


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


class TestEVConfig:
    """Tests for EV charger configuration."""

    def test_ev_default_disabled(self) -> None:
        """Default EV config is disabled and inert."""
        config = AppConfig()
        assert config.ev.enabled is False
        assert config.ev.charger_kw == 2.5  # Default fallback value
        assert config.ev.controllable is False
        assert config.ev.adapter is None
        assert config.ev.shed_priority == 5
        assert config.ev.mode.opportunistic is False
        assert config.ev.mode.min_nightly_kwh is None

    def test_ev_enabled_with_valid_charger_kw(self) -> None:
        """Enabled EV with valid charger_kw loads successfully."""
        config = AppConfig(
            ev={"enabled": True, "charger_kw": 3.5}
        )
        assert config.ev.enabled is True
        assert config.ev.charger_kw == 3.5

    def test_ev_enabled_with_zero_charger_kw_raises(self) -> None:
        """Enabled EV with charger_kw <= 0 raises validation error."""
        with pytest.raises(ValueError, match="charger_kw must be > 0 when enabled=True"):
            AppConfig(ev={"enabled": True, "charger_kw": 0})

    def test_ev_enabled_with_negative_charger_kw_raises(self) -> None:
        """Enabled EV with negative charger_kw raises validation error."""
        with pytest.raises(ValueError, match="charger_kw must be > 0 when enabled=True"):
            AppConfig(ev={"enabled": True, "charger_kw": -2.0})

    def test_ev_disabled_allows_zero_charger_kw(self) -> None:
        """Disabled EV allows charger_kw=0 (model is inert)."""
        config = AppConfig(ev={"enabled": False, "charger_kw": 0})
        assert config.ev.enabled is False
        # Zero is allowed when disabled; validation only fires on enabled=True

    def test_ev_mode_min_nightly_kwh_valid(self) -> None:
        """EVModeConfig with valid min_nightly_kwh loads."""
        mode = EVModeConfig(min_nightly_kwh=10.0, opportunistic=True)
        assert mode.min_nightly_kwh == 10.0
        assert mode.opportunistic is True

    def test_ev_mode_min_nightly_kwh_zero_raises(self) -> None:
        """EVModeConfig with min_nightly_kwh=0 raises."""
        with pytest.raises(ValueError, match="min_nightly_kwh must be > 0 if set"):
            EVModeConfig(min_nightly_kwh=0)

    def test_ev_mode_min_nightly_kwh_negative_raises(self) -> None:
        """EVModeConfig with negative min_nightly_kwh raises."""
        with pytest.raises(ValueError, match="min_nightly_kwh must be > 0 if set"):
            EVModeConfig(min_nightly_kwh=-5.0)

    def test_ev_mode_min_nightly_kwh_none_allowed(self) -> None:
        """EVModeConfig with min_nightly_kwh=None (disabled) is allowed."""
        mode = EVModeConfig(min_nightly_kwh=None)
        assert mode.min_nightly_kwh is None

    def test_ev_adapter_valid_values(self) -> None:
        """EVConfig with valid adapter values."""
        for adapter in ["shelly", "mqtt", "contactor"]:
            config = AppConfig(ev={"enabled": True, "charger_kw": 2.5, "adapter": adapter})
            assert config.ev.adapter == adapter

    def test_ev_adapter_invalid_value_raises(self) -> None:
        """EVConfig with invalid adapter value raises."""
        with pytest.raises(ValueError, match="adapter must be 'shelly', 'mqtt', 'contactor', or None"):
            AppConfig(ev={"enabled": True, "charger_kw": 2.5, "adapter": "invalid"})

    def test_ev_adapter_none_allowed(self) -> None:
        """EVConfig with adapter=None is allowed."""
        config = AppConfig(ev={"enabled": True, "charger_kw": 2.5, "adapter": None})
        assert config.ev.adapter is None

    def test_ev_shed_priority_valid_range(self) -> None:
        """EVConfig shed_priority in valid range [1, 5]."""
        for priority in [1, 2, 3, 4, 5]:
            config = AppConfig(ev={"shed_priority": priority})
            assert config.ev.shed_priority == priority

    def test_ev_shed_priority_out_of_range_raises(self) -> None:
        """EVConfig shed_priority outside [1, 5] raises."""
        with pytest.raises(ValueError):
            AppConfig(ev={"shed_priority": 0})
        with pytest.raises(ValueError):
            AppConfig(ev={"shed_priority": 6})

    def test_ev_controllable_false_by_default(self) -> None:
        """EVConfig controllable=False by default (Phase 4 not yet active)."""
        config = AppConfig(ev={"enabled": True, "charger_kw": 2.5})
        assert config.ev.controllable is False

    def test_ev_controllable_true_when_set(self) -> None:
        """EVConfig controllable can be set to True (Phase 4)."""
        config = AppConfig(
            ev={"enabled": True, "charger_kw": 2.5, "controllable": True, "adapter": "shelly"}
        )
        assert config.ev.controllable is True

    def test_ev_full_config_valid(self) -> None:
        """Full EV config with all fields set."""
        config = AppConfig(
            ev={
                "enabled": True,
                "charger_kw": 3.0,
                "controllable": False,
                "adapter": None,
                "mode": {
                    "min_nightly_kwh": 15.0,
                    "opportunistic": True,
                },
                "shed_priority": 5,
            }
        )
        assert config.ev.enabled is True
        assert config.ev.charger_kw == 3.0
        assert config.ev.controllable is False
        assert config.ev.adapter is None
        assert config.ev.mode.min_nightly_kwh == 15.0
        assert config.ev.mode.opportunistic is True
        assert config.ev.shed_priority == 5

    def test_example_configs_load_unchanged(self, tmp_path: Path) -> None:
        """Existing example configs (without ev block) still load unchanged."""
        # Create a minimal config without ev block (mimics Site A/Site B before EV, using Amber fallback)
        defaults_file = tmp_path / "defaults.yaml"
        defaults_file.write_text(
            """battery:
  capacity_wh: 42000
providers:
  tariff:
    type: "amber"
db:
  path: test.db
"""
        )
        mgr = ConfigManager(defaults_path=defaults_file, user_path=tmp_path / "user.yaml")
        config = mgr.load()

        # No EV block in YAML → default EVConfig is inert
        assert config.ev.enabled is False
        assert config.ev.charger_kw == 2.5  # Default fallback
        assert config.battery.capacity_wh == 42000  # Other config preserved

    def test_ev_enabled_default_charger_kw_valid(self) -> None:
        """Enabled EV uses default charger_kw=2.5 if not specified."""
        config = AppConfig(ev={"enabled": True})
        # Should use the default 2.5 kW
        assert config.ev.charger_kw == 2.5

    def test_ev_disabled_ignores_other_fields(self) -> None:
        """When enabled=False, other fields are present but model is inert."""
        config = AppConfig(
            ev={
                "enabled": False,
                "charger_kw": 2.5,
                "mode": {"min_nightly_kwh": 10.0, "opportunistic": True},
                "shed_priority": 3,
            }
        )
        # Model loads but is inert (enabled=False overrides everything)
        assert config.ev.enabled is False
