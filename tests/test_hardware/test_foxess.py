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


class TestRemoteEnableRetry:
    """Test remote-enable readback retry logic."""

    @pytest.mark.asyncio
    async def test_remote_enable_recovers_on_retry(self) -> None:
        """Simulate first readback failing, second succeeding after retry."""
        adapter = _make_adapter()

        # Set up: first read returns remote=0, second (after retry) returns remote=1
        read_sequence = [
            0,  # First read: REMOTE_ENABLE = 0 (mismatch)
            5000,  # First read: ACTIVE_POWER = 5000
            60,  # First read: REMOTE_TIMEOUT = 60
            1,  # Second read (after write retry): REMOTE_ENABLE = 1 (now matches!)
            5000,  # Second read: ACTIVE_POWER = 5000
        ]
        adapter._client.read_holding_registers.side_effect = [
            MagicMock(isError=lambda: False, registers=[r])
            for r in read_sequence
        ]

        # Setup write_register mock to always succeed
        ok_write = MagicMock(isError=lambda: False)
        adapter._client.write_register.return_value = ok_write

        # Should not raise, should log recovery
        await adapter._verify_remote_state(
            expected_remote=1, expected_active=5000, active_power_value=5000
        )
        # Verify write was called (at least for the retry)
        assert adapter._client.write_register.call_count >= 1

    @pytest.mark.asyncio
    async def test_remote_enable_persistent_mismatch_warns(self) -> None:
        """Persistent remote readback mismatch after retry logs warning, doesn't raise."""
        adapter = _make_adapter()

        # Set up: both reads return remote=0 (persistent mismatch)
        read_sequence = [
            0,  # First read: REMOTE_ENABLE = 0 (mismatch)
            5000,  # First read: ACTIVE_POWER = 5000
            60,  # First read: REMOTE_TIMEOUT = 60
            0,  # Second read (after write retry): REMOTE_ENABLE still 0 (still mismatches)
            5000,  # Second read: ACTIVE_POWER = 5000
        ]
        adapter._client.read_holding_registers.side_effect = [
            MagicMock(isError=lambda: False, registers=[r])
            for r in read_sequence
        ]

        # Setup write_register mock to always succeed
        ok_write = MagicMock(isError=lambda: False)
        adapter._client.write_register.return_value = ok_write

        # Should not raise (just warn)
        await adapter._verify_remote_state(
            expected_remote=1, expected_active=5000, active_power_value=5000
        )

    @pytest.mark.asyncio
    async def test_remote_enable_no_retry_on_first_match(self) -> None:
        """If first readback matches, no retry happens."""
        adapter = _make_adapter()

        # First read matches: remote=1, active=5000
        adapter._client.read_holding_registers.side_effect = [
            MagicMock(isError=lambda: False, registers=[1]),   # REMOTE_ENABLE = 1
            MagicMock(isError=lambda: False, registers=[5000]),  # ACTIVE_POWER = 5000
            MagicMock(isError=lambda: False, registers=[60]),   # REMOTE_TIMEOUT = 60
        ]

        # Should succeed without retry
        await adapter._verify_remote_state(
            expected_remote=1, expected_active=5000, active_power_value=5000
        )
        # Write should NOT have been called (no retry)
        adapter._client.write_register.assert_not_called()


class TestDeviceSettings:
    """Curated named settings and generic register read/write (firmware panel)."""

    def _ok(self, value: int) -> MagicMock:
        m = MagicMock()
        m.isError.return_value = False
        m.registers = [value]
        return m

    def _ok_write(self) -> MagicMock:
        m = MagicMock()
        m.isError.return_value = False
        return m

    def test_control_register_addresses(self) -> None:
        # Locked against live register readback on a KH inverter — see
        # KH_MODBUS_REGISTERS.md §5. Guards against the earlier off-by-two
        # mislabelling of the current/SoC block.
        assert Registers.MAX_CHARGE_CURRENT == 41007
        assert Registers.MAX_DISCHARGE_CURRENT == 41008
        assert Registers.MIN_SOC == 41009
        assert Registers.MAX_SOC == 41010
        assert Registers.MIN_SOC_ON_GRID == 41011
        assert Registers.EXPORT_LIMIT == 41012

    @pytest.mark.asyncio
    async def test_read_device_settings_scales_and_labels(self) -> None:
        adapter = _make_adapter()
        # Reads in DEVICE_SETTINGS order: work_mode, min_soc, max_soc,
        # min_soc_on_grid, max_charge_current (500=50.0A), max_discharge (400=40.0A),
        # export_limit.
        adapter._client.read_holding_registers.side_effect = [
            self._ok(0), self._ok(15), self._ok(95), self._ok(10),
            self._ok(500), self._ok(400), self._ok(5000),
        ]
        settings = await adapter.read_device_settings()
        by_key = {s["key"]: s for s in settings}

        assert by_key["work_mode"]["value_label"] == "Self-Use"
        assert by_key["min_soc"]["value"] == 15
        assert by_key["max_soc"]["value"] == 95
        assert by_key["min_soc_on_grid"]["value"] == 10
        assert by_key["max_charge_current"]["value"] == 50.0
        assert by_key["max_discharge_current"]["value"] == 40.0
        assert by_key["export_limit"]["value"] == 5000

    @pytest.mark.asyncio
    async def test_read_device_settings_reports_per_setting_error(self) -> None:
        adapter = _make_adapter()
        # First read raises; the rest succeed — error is per-row, not fatal.
        err = MagicMock()
        err.isError.return_value = True
        adapter._client.read_holding_registers.side_effect = [
            err, self._ok(20), self._ok(95), self._ok(10),
            self._ok(500), self._ok(500), self._ok(0),
        ]
        settings = await adapter.read_device_settings()
        assert "error" in settings[0]
        assert settings[1]["value"] == 20

    @pytest.mark.asyncio
    async def test_write_device_setting_applies_scale(self) -> None:
        adapter = _make_adapter()
        adapter._client.write_register.return_value = self._ok_write()

        raw = await adapter.write_device_setting("max_charge_current", 50.0)

        assert raw == 500  # 50.0 A / 0.1 scale
        addr, val = adapter._client.write_register.call_args.args[:2]
        assert addr == Registers.MAX_CHARGE_CURRENT
        assert val == 500

    @pytest.mark.asyncio
    async def test_write_device_setting_rejects_out_of_bounds(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ValueError, match="above maximum"):
            await adapter.write_device_setting("min_soc", 150)
        adapter._client.write_register.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_device_setting_rejects_bad_enum(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ValueError, match="not a valid option"):
            await adapter.write_device_setting("work_mode", 9)

    @pytest.mark.asyncio
    async def test_write_device_setting_unknown_key(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ValueError, match="Unknown device setting"):
            await adapter.write_device_setting("nope", 1)

    @pytest.mark.asyncio
    async def test_generic_write_rejects_out_of_range_address(self) -> None:
        adapter = _make_adapter()
        # 44002 is the remote power-control block — off-limits to the generic tool.
        with pytest.raises(ValueError, match="outside the writable settings range"):
            await adapter.write_holding_register(44002, 100)
        adapter._client.write_register.assert_not_called()

    @pytest.mark.asyncio
    async def test_generic_write_within_range(self) -> None:
        adapter = _make_adapter()
        adapter._client.write_register.return_value = self._ok_write()
        await adapter.write_holding_register(41011, 20)
        addr, val = adapter._client.write_register.call_args.args[:2]
        assert addr == 41011
        assert val == 20

    @pytest.mark.asyncio
    async def test_generic_read(self) -> None:
        adapter = _make_adapter()
        adapter._client.read_holding_registers.return_value = self._ok(1234)
        value = await adapter.read_holding_register(41000)
        assert value == 1234

    @pytest.mark.asyncio
    async def test_scan_returns_values_and_errors_per_row(self) -> None:
        adapter = _make_adapter()
        err = MagicMock()
        err.isError.return_value = True
        # 41000 ok=5, 41001 unmapped (error), 41002 ok=65506 (i.e. signed -30)
        adapter._client.read_holding_registers.side_effect = [
            self._ok(5), err, self._ok(65506),
        ]
        rows = await adapter.scan_holding_registers(41000, 3)

        assert rows[0] == {"address": 41000, "value": 5}
        assert rows[1]["address"] == 41001 and "error" in rows[1]
        assert rows[2] == {"address": 41002, "value": 65506}
        # A per-register Modbus error must NOT flip the connection state.
        assert adapter._connected is True

    @pytest.mark.asyncio
    async def test_scan_rejects_bad_count(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ValueError):
            await adapter.scan_holding_registers(41000, 0)
        with pytest.raises(ValueError):
            await adapter.scan_holding_registers(41000, 999)

    @pytest.mark.asyncio
    async def test_scan_transport_failure_marks_disconnected(self) -> None:
        adapter = _make_adapter()
        adapter._client.read_holding_registers.side_effect = OSError("timeout")
        with pytest.raises(IOError):
            await adapter.scan_holding_registers(41000, 4)
        assert adapter._connected is False


class TestFeedInFirst:
    """Export-priority (Feed-in First) mode drives work mode 1 + export limit."""

    @pytest.mark.asyncio
    async def test_feed_in_first_sets_work_mode_and_export_limit(self) -> None:
        adapter = _make_adapter()
        ok = MagicMock()
        ok.isError.return_value = False
        adapter._client.write_register.return_value = ok

        res = await adapter.send_command(
            InverterCommand(mode=OperatingMode.FEED_IN_FIRST, export_limit_w=500)
        )
        assert res.success
        writes = [(c.args[0], c.args[1]) for c in adapter._client.write_register.call_args_list]
        assert (Registers.REMOTE_ENABLE, 0) in writes    # autonomous, no remote power control
        assert (Registers.WORK_MODE, 1) in writes         # Feed-in First
        assert (Registers.EXPORT_LIMIT, 500) in writes    # export cap

    @pytest.mark.asyncio
    async def test_feed_in_first_defaults_export_limit_to_max(self) -> None:
        adapter = _make_adapter()  # max_export defaults to 10000
        ok = MagicMock()
        ok.isError.return_value = False
        adapter._client.write_register.return_value = ok

        await adapter.send_command(InverterCommand(mode=OperatingMode.FEED_IN_FIRST))
        writes = dict((c.args[0], c.args[1]) for c in adapter._client.write_register.call_args_list)
        assert writes[Registers.EXPORT_LIMIT] == adapter._max_export_w
