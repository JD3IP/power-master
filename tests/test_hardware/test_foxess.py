"""Tests for Fox-ESS KH8 Modbus TCP adapter."""

from __future__ import annotations

import pytest

from power_master.hardware.adapters.foxess import FoxESSAdapter, Registers
from power_master.hardware.base import InverterCommand, OperatingMode
from power_master.hardware.telemetry import Telemetry


class TestTelemetry:
    def test_soc_percentage(self) -> None:
        t = Telemetry(soc=0.72, battery_power_w=0, solar_power_w=0, grid_power_w=0, load_power_w=0)
        assert t.soc_pct == 72.0

    def test_charging_state(self) -> None:
        t = Telemetry(soc=0.5, battery_power_w=3000, solar_power_w=0, grid_power_w=0, load_power_w=0)
        assert t.is_charging
        assert not t.is_discharging

    def test_discharging_state(self) -> None:
        t = Telemetry(soc=0.5, battery_power_w=-3000, solar_power_w=0, grid_power_w=0, load_power_w=0)
        assert t.is_discharging
        assert not t.is_charging

    def test_importing_state(self) -> None:
        t = Telemetry(soc=0.5, battery_power_w=0, solar_power_w=0, grid_power_w=1500, load_power_w=0)
        assert t.is_importing
        assert not t.is_exporting

    def test_exporting_state(self) -> None:
        t = Telemetry(soc=0.5, battery_power_w=0, solar_power_w=0, grid_power_w=-1500, load_power_w=0)
        assert t.is_exporting
        assert not t.is_importing


class TestSignedEncoding:
    """Test signed 16-bit encoding used for ACTIVE_POWER register."""

    def test_positive_value_unchanged(self) -> None:
        # Positive (discharge) passes through as-is
        assert 5000 & 0xFFFF == 5000

    def test_negative_value_twos_complement(self) -> None:
        # Negative (charge) encoded as two's complement
        assert (-5000) & 0xFFFF == 60536

    def test_zero_unchanged(self) -> None:
        assert 0 & 0xFFFF == 0

    def test_max_negative(self) -> None:
        # -32768 is the max negative for int16
        assert (-32768) & 0xFFFF == 32768


class TestOperatingMode:
    def test_mode_values(self) -> None:
        assert OperatingMode.AUTO == 0
        assert OperatingMode.SELF_USE == 1
        assert OperatingMode.SELF_USE_ZERO_EXPORT == 2
        assert OperatingMode.FORCE_CHARGE == 3
        assert OperatingMode.FORCE_DISCHARGE == 4

    def test_command_creation(self) -> None:
        cmd = InverterCommand(mode=OperatingMode.FORCE_CHARGE, power_w=5000)
        assert cmd.mode == OperatingMode.FORCE_CHARGE
        assert cmd.power_w == 5000


class TestInferHwMode:
    """Test FoxESSAdapter.infer_hw_mode() static method — all 7 modes."""

    def test_pv_charging(self) -> None:
        assert FoxESSAdapter.infer_hw_mode(3000, 5000, 0) == "PV Charging"

    def test_grid_charging(self) -> None:
        assert FoxESSAdapter.infer_hw_mode(3000, 0, 3000) == "Grid Charging"

    def test_discharging_plus_export(self) -> None:
        assert FoxESSAdapter.infer_hw_mode(-3000, 0, -500) == "Discharging + Export"

    def test_discharging(self) -> None:
        assert FoxESSAdapter.infer_hw_mode(-3000, 0, 500) == "Discharging"

    def test_exporting(self) -> None:
        assert FoxESSAdapter.infer_hw_mode(0, 5000, -500) == "Exporting"

    def test_self_use(self) -> None:
        assert FoxESSAdapter.infer_hw_mode(0, 5000, 0) == "Self-Use"

    def test_idle(self) -> None:
        assert FoxESSAdapter.infer_hw_mode(0, 0, 0) == "Idle"

    def test_near_zero_is_idle(self) -> None:
        # Values within deadband (±100W battery, ±50W grid) → Idle
        assert FoxESSAdapter.infer_hw_mode(50, 50, -30) == "Idle"

    def test_pv_charging_priority_over_grid(self) -> None:
        # Both solar and battery positive — PV Charging wins over Grid Charging
        assert FoxESSAdapter.infer_hw_mode(500, 500, 500) == "PV Charging"
