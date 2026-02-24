"""Tests for storm reserve and monitoring."""

from __future__ import annotations

import pytest

from power_master.config.schema import StormConfig
from power_master.storm.monitor import StormMonitor
from power_master.storm.reserve import calculate_reserve_soc, estimate_hours_at_reserve


class TestReserveSoc:
    def test_above_threshold_returns_target(self) -> None:
        config = StormConfig(reserve_soc_target=0.80, probability_threshold=0.70)
        result = calculate_reserve_soc(0.85, config)
        assert result == 0.80

    def test_below_threshold_returns_zero(self) -> None:
        config = StormConfig(reserve_soc_target=0.80, probability_threshold=0.70)
        result = calculate_reserve_soc(0.50, config)
        assert result == 0.0

    def test_at_threshold_returns_target(self) -> None:
        config = StormConfig(reserve_soc_target=0.80, probability_threshold=0.70)
        result = calculate_reserve_soc(0.70, config)
        assert result == 0.80

    def test_disabled_returns_zero(self) -> None:
        config = StormConfig(enabled=False, reserve_soc_target=0.80, probability_threshold=0.70)
        result = calculate_reserve_soc(0.90, config)
        assert result == 0.0


class TestEstimateHours:
    def test_basic_calculation(self) -> None:
        # 50% SOC on 10kWh battery = 5000Wh, at 1000W load = 5 hours
        hours = estimate_hours_at_reserve(0.80, 0.50, 1000.0, 10000)
        assert hours == 5.0

    def test_zero_load_returns_zero(self) -> None:
        hours = estimate_hours_at_reserve(0.80, 0.50, 0.0, 10000)
        assert hours == 0.0

    def test_zero_soc_returns_zero(self) -> None:
        hours = estimate_hours_at_reserve(0.80, 0.0, 1000.0, 10000)
        assert hours == 0.0


class TestStormMonitor:
    def test_initial_state_inactive(self) -> None:
        config = StormConfig()
        monitor = StormMonitor(config)
        assert monitor.is_active is False
        assert monitor.reserve_soc == 0.0

    def test_activation(self) -> None:
        config = StormConfig(probability_threshold=0.70, reserve_soc_target=0.80)
        monitor = StormMonitor(config)

        changed = monitor.update(0.85)
        assert changed is True
        assert monitor.is_active is True
        assert monitor.reserve_soc == 0.80
        assert monitor.state.transition_count == 1

    def test_deactivation(self) -> None:
        config = StormConfig(probability_threshold=0.70, reserve_soc_target=0.80)
        monitor = StormMonitor(config)

        monitor.update(0.85)  # Activate
        changed = monitor.update(0.30)  # Deactivate

        assert changed is True
        assert monitor.is_active is False
        assert monitor.reserve_soc == 0.0
        assert monitor.state.transition_count == 2

    def test_no_change_when_staying_active(self) -> None:
        config = StormConfig(probability_threshold=0.70, reserve_soc_target=0.80)
        monitor = StormMonitor(config)

        monitor.update(0.85)
        changed = monitor.update(0.90)

        assert changed is False
        assert monitor.is_active is True
        assert monitor.state.transition_count == 1

    def test_no_change_when_staying_inactive(self) -> None:
        config = StormConfig(probability_threshold=0.70, reserve_soc_target=0.80)
        monitor = StormMonitor(config)

        changed = monitor.update(0.30)
        assert changed is False
        assert monitor.is_active is False

    def test_reset(self) -> None:
        config = StormConfig(probability_threshold=0.70, reserve_soc_target=0.80)
        monitor = StormMonitor(config)

        monitor.update(0.85)
        monitor.reset()

        assert monitor.is_active is False
        assert monitor.state.transition_count == 0
