"""Tests for resilience manager, health checks, and fallback."""

from __future__ import annotations

import pytest

from power_master.config.schema import AppConfig
from power_master.control.command import ControlCommand
from power_master.hardware.base import OperatingMode
from power_master.resilience.fallback import get_fallback_command
from power_master.resilience.health_check import HealthChecker
from power_master.resilience.manager import ResilienceManager
from power_master.resilience.modes import ResilienceLevel


class TestHealthChecker:
    def test_initial_state_healthy(self) -> None:
        checker = HealthChecker()
        checker.register("inverter")
        assert checker.is_healthy("inverter") is True
        assert checker.all_healthy() is True

    def test_single_failure_stays_healthy(self) -> None:
        checker = HealthChecker(max_consecutive_failures=3)
        checker.register("tariff")
        checker.record_failure("tariff", "timeout")
        assert checker.is_healthy("tariff") is True

    def test_consecutive_failures_mark_unhealthy(self) -> None:
        checker = HealthChecker(max_consecutive_failures=3)
        checker.register("tariff")
        for _ in range(3):
            checker.record_failure("tariff", "timeout")
        assert checker.is_healthy("tariff") is False
        assert "tariff" in checker.get_unhealthy()

    def test_success_resets_consecutive(self) -> None:
        checker = HealthChecker(max_consecutive_failures=3)
        checker.register("tariff")
        checker.record_failure("tariff", "error1")
        checker.record_failure("tariff", "error2")
        checker.record_success("tariff")
        checker.record_failure("tariff", "error3")
        # Only 1 consecutive failure after success
        assert checker.is_healthy("tariff") is True

    def test_get_unhealthy_multiple(self) -> None:
        checker = HealthChecker(max_consecutive_failures=2)
        checker.register("inverter")
        checker.register("tariff")
        for _ in range(2):
            checker.record_failure("inverter", "err")
            checker.record_failure("tariff", "err")
        unhealthy = checker.get_unhealthy()
        assert "inverter" in unhealthy
        assert "tariff" in unhealthy

    def test_unknown_provider_assumed_healthy(self) -> None:
        checker = HealthChecker()
        assert checker.is_healthy("nonexistent") is True

    def test_get_health_details(self) -> None:
        checker = HealthChecker()
        checker.register("solar_forecast")
        checker.record_failure("solar_forecast", "API error")
        health = checker.get_health("solar_forecast")
        assert health is not None
        assert health.consecutive_failures == 1
        assert health.last_error == "API error"


class TestResilienceManager:
    def test_initial_state_normal(self) -> None:
        config = AppConfig()
        checker = HealthChecker()
        manager = ResilienceManager(config, checker)
        assert manager.level == ResilienceLevel.NORMAL
        assert manager.is_normal is True

    def test_forecast_failure_degrades(self) -> None:
        config = AppConfig()
        checker = HealthChecker(max_consecutive_failures=2)
        checker.register("solar_forecast")
        for _ in range(2):
            checker.record_failure("solar_forecast", "err")
        manager = ResilienceManager(config, checker)

        changed = manager.evaluate()
        assert changed is True
        assert manager.level == ResilienceLevel.DEGRADED_FORECAST

    def test_tariff_failure_degrades(self) -> None:
        config = AppConfig()
        checker = HealthChecker(max_consecutive_failures=2)
        checker.register("tariff")
        for _ in range(2):
            checker.record_failure("tariff", "err")
        manager = ResilienceManager(config, checker)

        changed = manager.evaluate()
        assert changed is True
        assert manager.level == ResilienceLevel.DEGRADED_TARIFF

    def test_inverter_failure_is_hardware_degraded(self) -> None:
        config = AppConfig()
        checker = HealthChecker(max_consecutive_failures=2)
        checker.register("inverter")
        for _ in range(2):
            checker.record_failure("inverter", "err")
        manager = ResilienceManager(config, checker)

        manager.evaluate()
        assert manager.level == ResilienceLevel.DEGRADED_HARDWARE

    def test_multiple_failures_trigger_safe_mode(self) -> None:
        config = AppConfig()
        checker = HealthChecker(max_consecutive_failures=2)
        checker.register("tariff")
        checker.register("solar_forecast")
        for _ in range(2):
            checker.record_failure("tariff", "err")
            checker.record_failure("solar_forecast", "err")
        manager = ResilienceManager(config, checker)

        manager.evaluate()
        assert manager.level == ResilienceLevel.SAFE_MODE

    def test_recovery_to_normal(self) -> None:
        config = AppConfig()
        checker = HealthChecker(max_consecutive_failures=2)
        checker.register("tariff")
        for _ in range(2):
            checker.record_failure("tariff", "err")
        manager = ResilienceManager(config, checker)

        manager.evaluate()
        assert manager.level == ResilienceLevel.DEGRADED_TARIFF

        # Recover
        checker.record_success("tariff")
        changed = manager.evaluate()
        assert changed is True
        assert manager.level == ResilienceLevel.NORMAL

    def test_no_change_returns_false(self) -> None:
        config = AppConfig()
        checker = HealthChecker()
        manager = ResilienceManager(config, checker)

        changed = manager.evaluate()
        assert changed is False  # Already NORMAL

    def test_force_level(self) -> None:
        config = AppConfig()
        checker = HealthChecker()
        manager = ResilienceManager(config, checker)

        manager.force_level(ResilienceLevel.SAFE_MODE)
        assert manager.level == ResilienceLevel.SAFE_MODE
        assert manager.state.transition_count == 1


class TestFallback:
    def test_normal_returns_self_use(self) -> None:
        config = AppConfig()
        cmd = get_fallback_command(ResilienceLevel.NORMAL, 0.5, config)
        assert cmd.mode == OperatingMode.SELF_USE

    def test_degraded_forecast_returns_self_use(self) -> None:
        config = AppConfig()
        cmd = get_fallback_command(ResilienceLevel.DEGRADED_FORECAST, 0.5, config)
        assert cmd.mode == OperatingMode.SELF_USE
        assert cmd.source == "fallback"

    def test_degraded_tariff_returns_self_use(self) -> None:
        config = AppConfig()
        cmd = get_fallback_command(ResilienceLevel.DEGRADED_TARIFF, 0.5, config)
        assert cmd.mode == OperatingMode.SELF_USE

    def test_safe_mode_returns_zero_export(self) -> None:
        config = AppConfig()
        cmd = get_fallback_command(ResilienceLevel.SAFE_MODE, 0.5, config)
        assert cmd.mode == OperatingMode.SELF_USE_ZERO_EXPORT
        assert cmd.priority == 2  # High priority

    def test_hardware_degraded_returns_self_use(self) -> None:
        config = AppConfig()
        cmd = get_fallback_command(ResilienceLevel.DEGRADED_HARDWARE, 0.5, config)
        assert cmd.mode == OperatingMode.SELF_USE
