"""Tests for Fox-ESS KH8 Modbus TCP/RTU adapter."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from power_master.config.schema import FoxESSConfig
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


class TestSerialPortValidation:
    """Test serial port pre-connect validation."""

    def test_nonexistent_port_raises(self) -> None:
        with pytest.raises(ConnectionError, match="does not exist"):
            FoxESSAdapter._validate_serial_port("/dev/ttyNONEXISTENT_12345")

    def test_regular_file_raises(self) -> None:
        with tempfile.NamedTemporaryFile() as f:
            with pytest.raises(ConnectionError, match="not a character device"):
                FoxESSAdapter._validate_serial_port(f.name)

    def test_valid_char_device_passes(self) -> None:
        # /dev/null is a character device available on all Linux systems
        FoxESSAdapter._validate_serial_port("/dev/null")


def _make_adapter(connection_type: str = "tcp") -> FoxESSAdapter:
    """Create an adapter with a mocked Modbus client for unit testing."""
    config = FoxESSConfig(connection_type=connection_type, host="127.0.0.1", port=502)
    adapter = FoxESSAdapter(config)
    mock_client = AsyncMock()
    type(mock_client).connected = PropertyMock(return_value=True)
    adapter._client = mock_client
    adapter._connected = True
    return adapter


class TestDisconnectOnError:
    """Verify that _connected is set to False when Modbus ops fail,
    so the reconnect logic in the poll loop will trigger."""

    @pytest.mark.asyncio
    async def test_read_holding_exception_marks_disconnected(self) -> None:
        adapter = _make_adapter()
        adapter._client.read_holding_registers.side_effect = OSError("serial timeout")

        with pytest.raises(OSError):
            await adapter._read_uint16(31000)

        assert adapter._connected is False
        assert not await adapter.is_connected()

    @pytest.mark.asyncio
    async def test_read_input_exception_marks_disconnected(self) -> None:
        adapter = _make_adapter()
        adapter._client.read_input_registers.side_effect = OSError("serial timeout")

        with pytest.raises(OSError):
            await adapter._read_input_uint16(31000)

        assert adapter._connected is False

    @pytest.mark.asyncio
    async def test_read_holding_modbus_error_marks_disconnected(self) -> None:
        adapter = _make_adapter()
        error_result = MagicMock()
        error_result.isError.return_value = True
        adapter._client.read_holding_registers.return_value = error_result

        with pytest.raises(IOError):
            await adapter._read_uint16(31000)

        assert adapter._connected is False

    @pytest.mark.asyncio
    async def test_read_input_modbus_error_marks_disconnected(self) -> None:
        adapter = _make_adapter()
        error_result = MagicMock()
        error_result.isError.return_value = True
        adapter._client.read_input_registers.return_value = error_result

        with pytest.raises(IOError):
            await adapter._read_input_uint16(31000)

        assert adapter._connected is False

    @pytest.mark.asyncio
    async def test_write_exception_marks_disconnected(self) -> None:
        adapter = _make_adapter()
        adapter._client.write_register.side_effect = OSError("serial timeout")

        with pytest.raises(OSError):
            await adapter._write_register(44000, 1)

        assert adapter._connected is False

    @pytest.mark.asyncio
    async def test_write_modbus_error_marks_disconnected(self) -> None:
        adapter = _make_adapter()
        error_result = MagicMock()
        error_result.isError.return_value = True
        adapter._client.write_register.return_value = error_result

        with pytest.raises(IOError):
            await adapter._write_register(44000, 1)

        assert adapter._connected is False

    @pytest.mark.asyncio
    async def test_successful_read_stays_connected(self) -> None:
        adapter = _make_adapter()
        ok_result = MagicMock()
        ok_result.isError.return_value = False
        ok_result.registers = [42]
        adapter._client.read_holding_registers.return_value = ok_result

        value = await adapter._read_uint16(31000)

        assert value == 42
        assert adapter._connected is True

    @pytest.mark.asyncio
    async def test_serial_port_missing_raises_clear_error(self) -> None:
        config = FoxESSConfig(
            connection_type="rtu",
            serial_port="/dev/ttyNONEXISTENT",
        )
        adapter = FoxESSAdapter(config)

        with pytest.raises(ConnectionError, match="does not exist"):
            await adapter.connect()
