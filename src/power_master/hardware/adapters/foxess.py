"""Fox-ESS KH series inverter adapter via Modbus TCP or RTU.

Register map based on the official FoxESS KH Series Modbus PDF.
See KH_MODBUS_REGISTERS.md for the full register reference.

Key registers:
  Read (input):  31002 (PV1 power), 31014 (meter power), 31016 (load),
                 31022 (battery power), 31024 (SOC)
  Read/Write (holding): 41000 (work mode), 41012 (export limit)
  Write: 44000 (remote enable), 44001 (remote timeout), 44002 (active power)
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import time
from dataclasses import dataclass

from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.framer import FramerType

from power_master.config.schema import FoxESSConfig
from power_master.hardware.base import (
    CommandResult,
    InverterCommand,
    OperatingMode,
)
from power_master.hardware.telemetry import Telemetry

logger = logging.getLogger(__name__)


# ── Register Addresses ───────────────────────────────
@dataclass(frozen=True)
class Registers:
    """Fox-ESS KH series Modbus register addresses.

    Input registers (31xxx) are read via function code 4.
    Holding registers (41xxx, 44xxx) are read/written via function codes 3/6.
    """

    # Input registers — live measurements (read_input_registers, FC 4)
    PV1_POWER = 31002       # I16, watts
    PV2_POWER = 31005       # I16, watts
    GRID_METER = 31014      # I16, watts (KH raw: positive=export, negative=import; we negate)
    LOAD_POWER = 31016      # I16, watts (home consumption)
    BATTERY_VOLTAGE = 31020  # I16, volts (gain 10)
    BATTERY_CURRENT = 31021  # I16, amps (gain 10, positive=charging)
    BATTERY_POWER = 31022   # I16, watts (KH: positive=discharge, negative=charge — we flip)
    BATTERY_TEMP = 31023    # I16, celsius (gain 10)
    BATTERY_SOC = 31024     # U16, 0-100 percentage
    INVERTER_STATE = 31027  # U16, 0=Self-Test … 3=Normal … 5=Fault

    # Device information registers (RO)
    FIRMWARE_MASTER = 30016   # U16
    FIRMWARE_SLAVE = 30017    # U16
    FIRMWARE_MANAGER = 30018  # U16

    # Holding registers — control (read_holding_registers / write_register, FC 3/6)
    # Addresses verified against working KH firmware v1.55
    WORK_MODE = 41000       # U16 RW: 0=Self-Use, 1=Feed-in, 2=Backup
    MAX_CHARGE_CURRENT = 41009   # U16 RW: amps (gain 10, raw 500 = 50.0A)
    MAX_DISCHARGE_CURRENT = 41010  # U16 RW: amps (gain 10, raw 500 = 50.0A)
    MIN_SOC = 41011         # U16 RW, % (battery won't discharge below)
    EXPORT_LIMIT = 41012    # U16 RW, watts (grid export cap; 0 = no export)

    # Remote power control registers (FC 6)
    REMOTE_ENABLE = 44000   # U16, 0=off, 1=on
    REMOTE_TIMEOUT = 44001  # U16, seconds (watchdog)
    ACTIVE_POWER = 44002    # I16, watts (per FoxESS doc: positive=charge, negative=discharge)
    # NOTE: The TCP gateway inverts the sign, so TCP code sends positive=discharge.
    # Direct RTU follows the documented convention: positive=charge, negative=discharge.


# KH work mode register values → human-readable names
KH_WORK_MODES = {
    0: "Self-Use",
    1: "Feed-in First",
    2: "Backup",
    3: "Force Charge",
    4: "Force Discharge",
}

# Our OperatingMode → KH work mode register value
MODE_TO_KH: dict[OperatingMode, int] = {
    OperatingMode.SELF_USE: 0,
    OperatingMode.SELF_USE_ZERO_EXPORT: 0,  # Self-Use + export limit = 0W
    OperatingMode.FORCE_CHARGE: 3,
    OperatingMode.FORCE_DISCHARGE: 4,
}


class FoxESSAdapter:
    """Modbus TCP/RTU adapter for Fox-ESS KH series inverter.

    Implements the InverterAdapter protocol for reading telemetry
    and sending charge/discharge commands via Modbus TCP or RTU (serial).
    """

    _MAX_REASONABLE_POWER_W = 50000

    def __init__(self, config: FoxESSConfig) -> None:
        self._config = config
        self._client: AsyncModbusTcpClient | AsyncModbusSerialClient | None = None
        self._connected = False
        self._lock = asyncio.Lock()
        self.firmware: dict[str, str] = {}

    async def connect(self) -> None:
        """Establish Modbus connection to the inverter (TCP or RTU)."""
        # Close any existing client before reconnecting
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
            self._connected = False

        if self._config.connection_type == "rtu":
            # Validate serial port before attempting connection
            self._validate_serial_port(self._config.serial_port)

            self._client = AsyncModbusSerialClient(
                port=self._config.serial_port,
                framer=FramerType.RTU,
                baudrate=self._config.baudrate,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=10,
                retries=3,
                reconnect_delay=0,  # We handle reconnection ourselves
            )
            logger.info(
                "RTU: opening serial port %s (%d baud, 8N1, unit %d)",
                self._config.serial_port,
                self._config.baudrate,
                self._config.unit_id,
            )
            connected = await self._client.connect()
            if not connected:
                raise ConnectionError(
                    f"Failed to open serial port {self._config.serial_port} "
                    f"({self._config.baudrate} baud). Check the device is "
                    f"connected and not in use by another process."
                )
            # Verify the transport is active after connect
            if not self._client.connected:
                raise ConnectionError(
                    f"Serial port {self._config.serial_port} opened but transport "
                    f"is not active. The port may have closed immediately."
                )
            self._connected = True
            logger.info(
                "RTU: serial port opened successfully at %s (%d baud, unit %d)",
                self._config.serial_port,
                self._config.baudrate,
                self._config.unit_id,
            )

            # Attempt a test read to verify Modbus communication
            await self._verify_rtu_communication()
        else:
            self._client = AsyncModbusTcpClient(
                host=self._config.host,
                port=self._config.port,
                timeout=10,
                retries=3,
            )
            connected = await self._client.connect()
            if not connected:
                raise ConnectionError(
                    f"Failed to connect to Fox-ESS at {self._config.host}:{self._config.port}"
                )
            self._connected = True
            logger.info(
                "Connected to Fox-ESS KH at %s:%d (unit %d)",
                self._config.host,
                self._config.port,
                self._config.unit_id,
            )
            if self._config.port not in (502, 8899):
                logger.warning(
                    "FoxESS control may be blocked on port %d. KH write control is typically "
                    "available on Modbus TCP port 502 (direct) or 8899 (serial gateway).",
                    self._config.port,
                )

    async def disconnect(self) -> None:
        """Close the Modbus TCP connection."""
        if self._client:
            try:
                await self._write_register(Registers.REMOTE_ENABLE, 0)
            except Exception:
                pass
            self._client.close()
            self._connected = False
            logger.info("Disconnected from Fox-ESS")

    async def is_connected(self) -> bool:
        return self._connected and self._client is not None and self._client.connected

    async def get_telemetry(self) -> Telemetry:
        """Read current telemetry from the inverter via Modbus.

        All power readings are I16 input registers at 31xxx.
        Battery: KH register positive=discharging, negative=charging (opposite of PDF).
                 We flip the sign so our Telemetry convention holds:
                 positive=charging, negative=discharging.
        Grid: KH register positive=export, negative=import (opposite of PDF).
              We flip the sign so our Telemetry convention holds:
              positive=import, negative=export.
        """
        async with self._lock:
            # PV power — sum PV1 + PV2
            pv1_power = await self._read_input_int16(Registers.PV1_POWER)
            pv2_power = await self._read_input_int16(Registers.PV2_POWER)

            # Grid meter — KH: positive=export, negative=import
            grid_power_raw = await self._read_input_int16(Registers.GRID_METER)

            # Load power — home consumption
            load_power_raw = await self._read_input_int16(Registers.LOAD_POWER)

            # Battery power raw — KH: positive=discharge, negative=charge
            battery_power_raw = await self._read_input_int16(Registers.BATTERY_POWER)

            # Battery SOC — 0-100%
            soc_pct = await self._read_input_uint16(Registers.BATTERY_SOC)

            # Battery voltage (gain 10) and temperature (gain 10)
            bat_voltage_raw = await self._read_input_int16(Registers.BATTERY_VOLTAGE)
            bat_temp_raw = await self._read_input_int16(Registers.BATTERY_TEMP)

            # Read actual work mode from holding register 41000
            work_mode_raw = await self._read_uint16(Registers.WORK_MODE)

        # Flip battery sign: KH positive=discharge → our positive=charging
        battery_power = -battery_power_raw
        # Flip grid sign: KH positive=export → our positive=import
        grid_power = -grid_power_raw

        solar_power = max(0, pv1_power) + max(0, pv2_power)
        load_power = max(0, load_power_raw)
        work_mode_name = KH_WORK_MODES.get(work_mode_raw, f"Unknown({work_mode_raw})")

        # Infer detailed mode from power flow for more granularity
        hw_mode = self.infer_hw_mode(battery_power, solar_power, grid_power)

        return Telemetry(
            soc=soc_pct / 100.0,
            battery_power_w=battery_power,
            solar_power_w=solar_power,
            grid_power_w=grid_power,
            load_power_w=load_power,
            battery_voltage=bat_voltage_raw / 10.0,
            battery_temp_c=bat_temp_raw / 10.0,
            inverter_mode=hw_mode,
            raw_data={
                "pv1_power": pv1_power,
                "pv2_power": pv2_power,
                "solar_power": solar_power,
                "battery_power_raw": battery_power_raw,
                "battery_power": battery_power,
                "soc_pct": soc_pct,
                "grid_power_raw": grid_power_raw,
                "grid_power": grid_power,
                "load_power": load_power_raw,
                "battery_voltage_raw": bat_voltage_raw,
                "battery_temp_raw": bat_temp_raw,
                "work_mode_register": work_mode_raw,
                "work_mode_name": work_mode_name,
            },
        )

    async def send_command(self, command: InverterCommand) -> CommandResult:
        """Send a control command to the inverter.

        FoxESS KH control strategy — matches verified working example:
          - Remote power control (44000-44002) is the sole mechanism for
            commanding charge/discharge on KH series inverters.
          - ACTIVE_POWER (44002): negative = charge from grid,
            positive = discharge to loads.
          - For Self-Use: disable remote, set work mode 0.
          - For Zero Export: disable remote, set work mode 0, export limit 0.
          - Watchdog timeout must exceed the control loop tick interval.

        Register write sequence for force charge/discharge matches the
        proven working example (foxess_tcp.py):
          1. REMOTE_ENABLE = 1
          2. TIMEOUT_SET = timeout_s
          3. ACTIVE_POWER = signed watts
        No other registers are written during remote control activation.
        """
        start = time.monotonic()

        try:
            async with self._lock:
                if command.mode == OperatingMode.SELF_USE:
                    # Matches example mode_self_use(): remote_disable + set_work_mode(0)
                    await self._write_register(Registers.REMOTE_ENABLE, 0)
                    await self._write_register(Registers.WORK_MODE, 0)
                    logger.info("SELF_USE: remote=off, work_mode=0")

                elif command.mode == OperatingMode.SELF_USE_ZERO_EXPORT:
                    await self._write_register(Registers.REMOTE_ENABLE, 0)
                    await self._write_register(Registers.WORK_MODE, 0)
                    await self._write_register(Registers.EXPORT_LIMIT, 0)
                    logger.info("ZERO_EXPORT: remote=off, work_mode=0, export_limit=0W")

                elif command.mode == OperatingMode.FORCE_CHARGE:
                    charge_w = abs(command.power_w)
                    # Matches example remote_set(): exactly 3 writes, no extras
                    # Negative active power = charge from grid
                    await self._write_register(Registers.REMOTE_ENABLE, 1)
                    await self._write_register(
                        Registers.REMOTE_TIMEOUT,
                        self._config.watchdog_timeout_seconds,
                    )
                    await self._write_s16(Registers.ACTIVE_POWER, -charge_w)
                    logger.info(
                        "FORCE_CHARGE: remote=1, timeout=%ds, "
                        "active_power=-%dW (charge from grid)",
                        self._config.watchdog_timeout_seconds, charge_w,
                    )
                    await self._verify_remote_state(expected_remote=1, expected_active=-charge_w)

                elif command.mode == OperatingMode.FORCE_DISCHARGE:
                    discharge_w = abs(command.power_w)
                    # Matches example remote_set(): exactly 3 writes, no extras
                    # Positive active power = discharge to loads
                    await self._write_register(Registers.REMOTE_ENABLE, 1)
                    await self._write_register(
                        Registers.REMOTE_TIMEOUT,
                        self._config.watchdog_timeout_seconds,
                    )
                    await self._write_register(Registers.ACTIVE_POWER, discharge_w)
                    logger.info(
                        "FORCE_DISCHARGE: remote=1, timeout=%ds, "
                        "active_power=+%dW (discharge to loads)",
                        self._config.watchdog_timeout_seconds, discharge_w,
                    )
                    await self._verify_remote_state(expected_remote=1, expected_active=discharge_w)

                elif command.mode == OperatingMode.FORCE_CHARGE_ZERO_IMPORT:
                    # Matches example mode_battery_off_best_effort():
                    # remote_disable + set_limits(max_discharge_a=0)
                    await self._write_register(Registers.REMOTE_ENABLE, 0)
                    await self._write_register(Registers.WORK_MODE, 0)
                    await self._write_register(Registers.MAX_DISCHARGE_CURRENT, 0)
                    logger.info(
                        "FORCE_CHARGE_ZERO_IMPORT: remote=off, work_mode=0, "
                        "max_discharge_current=0 (discharge disabled)"
                    )

            latency = int((time.monotonic() - start) * 1000)
            logger.info(
                "Command sent: mode=%s power=%dW latency=%dms",
                command.mode.name,
                command.power_w,
                latency,
            )
            return CommandResult(success=True, latency_ms=latency)

        except Exception as e:
            latency = int((time.monotonic() - start) * 1000)
            logger.error("Command failed: %s", e)
            return CommandResult(
                success=False,
                latency_ms=latency,
                message=str(e),
            )

    @staticmethod
    def infer_hw_mode(battery_power_w: int, solar_power_w: int, grid_power_w: int) -> str:
        """Infer the inverter's working mode from power flow values.

        Uses our sign convention: battery positive = charging, grid positive = import.
        """
        if battery_power_w > 100 and solar_power_w > 100:
            return "PV Charging"
        elif battery_power_w > 100:
            return "Grid Charging"
        elif battery_power_w < -100 and grid_power_w < -50:
            return "Discharging + Export"
        elif battery_power_w < -100:
            return "Discharging"
        elif solar_power_w > 100 and grid_power_w < -50:
            return "Exporting"
        elif solar_power_w > 100:
            return "Self-Use"
        else:
            return "Idle"

    # ── Serial port validation & verification ─────────────

    @staticmethod
    def _validate_serial_port(port: str) -> None:
        """Check that the serial port device exists and is accessible.

        Raises ConnectionError with a helpful message if the port is not usable.
        """
        if not os.path.exists(port):
            raise ConnectionError(
                f"Serial port {port} does not exist. "
                f"Check the USB-to-RS485 adapter is connected. "
                f"If running in Docker, ensure the device is passed through "
                f"(e.g. devices: ['{port}:{port}'] in docker-compose.yml)."
            )
        try:
            port_stat = os.stat(port)
        except OSError as e:
            raise ConnectionError(f"Cannot stat serial port {port}: {e}") from e
        if not stat.S_ISCHR(port_stat.st_mode):
            raise ConnectionError(
                f"{port} is not a character device. "
                f"Expected a serial port (e.g. /dev/ttyUSB0)."
            )
        if not os.access(port, os.R_OK | os.W_OK):
            raise ConnectionError(
                f"No read/write permission on {port}. "
                f"Run 'sudo usermod -a -G dialout $USER' and re-login, "
                f"or 'sudo chmod 666 {port}' for a quick fix."
            )
        logger.info("RTU: serial port %s exists and is accessible", port)

    async def _verify_rtu_communication(self) -> None:
        """Attempt a test register read to verify the Modbus RTU bus is working.

        Reads inverter state register (31027) as a lightweight probe.
        Logs result but does not raise on failure — the inverter may be
        powered off or the register map may differ, but at least TXD activity
        confirms the serial adapter is transmitting.
        """
        assert self._client is not None
        logger.info(
            "RTU: sending test read to unit %d (register %d) to verify serial TX...",
            self._config.unit_id,
            Registers.INVERTER_STATE,
        )
        try:
            result = await self._client.read_input_registers(
                Registers.INVERTER_STATE, count=1, device_id=self._config.unit_id
            )
            if result.isError():
                logger.warning(
                    "RTU: test read got error response: %s. "
                    "Serial TX is working but the inverter did not respond correctly. "
                    "Check unit_id (%d), baud rate (%d), and wiring.",
                    result,
                    self._config.unit_id,
                    self._config.baudrate,
                )
            else:
                logger.info(
                    "RTU: test read successful — inverter state=%d. "
                    "Serial communication verified.",
                    result.registers[0],
                )
        except Exception as e:
            logger.warning(
                "RTU: test read failed: %s. "
                "Serial port is open but no response from inverter at unit %d. "
                "Check: (1) RS485 A/B wiring, (2) baud rate %d matches inverter, "
                "(3) unit_id %d is correct, (4) inverter is powered on.",
                e,
                self._config.unit_id,
                self._config.baudrate,
                self._config.unit_id,
            )

    # Minimum firmware versions required for remote control registers (44000-44002)
    MIN_FIRMWARE_MASTER = 160   # 1.60
    MIN_FIRMWARE_MANAGER = 158  # 1.58

    async def read_firmware(self) -> dict[str, str]:
        """Read inverter firmware versions from input registers 30016-30018."""
        assert self._client is not None
        try:
            async with self._lock:
                master = await self._read_input_uint16(Registers.FIRMWARE_MASTER)
                slave = await self._read_input_uint16(Registers.FIRMWARE_SLAVE)
                manager = await self._read_input_uint16(Registers.FIRMWARE_MANAGER)
            # Format: raw value like 155 → "1.55"
            def _fmt(v: int) -> str:
                return f"{v / 100:.2f}" if v > 99 else str(v)

            self.firmware = {
                "master": _fmt(master),
                "slave": _fmt(slave),
                "manager": _fmt(manager),
                "master_raw": master,
                "slave_raw": slave,
                "manager_raw": manager,
            }
            logger.info(
                "Inverter firmware: master=%s, slave=%s, manager=%s",
                self.firmware["master"], self.firmware["slave"], self.firmware["manager"],
            )
            self._check_firmware_version(master, manager)
        except Exception as e:
            logger.warning("Failed to read firmware versions: %s", e)
            self.firmware = {"master": "unknown", "slave": "unknown", "manager": "unknown"}
        return self.firmware

    def _check_firmware_version(self, master_raw: int, manager_raw: int) -> None:
        """Warn or raise if firmware is too old for remote control registers."""
        issues = []
        if master_raw < self.MIN_FIRMWARE_MASTER:
            issues.append(
                f"master {master_raw / 100:.2f} < {self.MIN_FIRMWARE_MASTER / 100:.2f}"
            )
        if manager_raw < self.MIN_FIRMWARE_MANAGER:
            issues.append(
                f"manager {manager_raw / 100:.2f} < {self.MIN_FIRMWARE_MANAGER / 100:.2f}"
            )
        if issues:
            msg = (
                f"Inverter firmware too old for remote control: {', '.join(issues)}. "
                f"Minimum required: master >= {self.MIN_FIRMWARE_MASTER / 100:.2f}, "
                f"manager >= {self.MIN_FIRMWARE_MANAGER / 100:.2f}. "
                f"Force charge/discharge commands will not work. "
                f"Update inverter firmware via the FoxESS installer app."
            )
            logger.error(msg)
            raise ConnectionError(msg)

    # ── Low-level Modbus operations ──────────────────────

    async def _read_uint16(self, address: int) -> int:
        """Read a single uint16 holding register (function code 3)."""
        assert self._client is not None
        try:
            result = await self._client.read_holding_registers(
                address, count=1, device_id=self._config.unit_id
            )
        except Exception:
            self._connected = False
            raise
        if result.isError():
            self._connected = False
            raise IOError(f"Modbus read error at holding register {address}: {result}")
        return result.registers[0]

    async def _read_input_uint16(self, address: int) -> int:
        """Read a single uint16 input register (function code 4)."""
        assert self._client is not None
        try:
            result = await self._client.read_input_registers(
                address, count=1, device_id=self._config.unit_id
            )
        except Exception:
            self._connected = False
            raise
        if result.isError():
            self._connected = False
            raise IOError(f"Modbus read error at input register {address}: {result}")
        return result.registers[0]

    async def _read_input_int16(self, address: int) -> int:
        """Read a single int16 input register (function code 4)."""
        value = await self._read_input_uint16(address)
        return value - 0x10000 if value >= 0x8000 else value

    async def _write_register(self, address: int, value: int) -> None:
        """Write a single unsigned 16-bit holding register value.

        Control registers (41xxx, 44xxx) should be written individually,
        not in blocks, per Fox-ESS Modbus protocol requirements.
        For RTU connections, a 100ms inter-frame delay is enforced to
        prevent frame collisions on USB-serial adapters.
        """
        assert self._client is not None
        raw = value & 0xFFFF
        signed = raw - 0x10000 if raw >= 0x8000 else raw
        if self._config.connection_type == "rtu":
            # RTU inter-frame delay: USB-serial adapters can buffer/merge
            # frames if writes are too rapid, causing the inverter to miss
            # commands even though pymodbus reports success.
            await asyncio.sleep(0.1)
            logger.info(
                "Modbus WRITE: port=%s unit=%d addr=%d raw=%d (0x%04X, signed=%d)",
                self._config.serial_port,
                self._config.unit_id,
                address, raw, raw, signed,
            )
        else:
            logger.info(
                "Modbus WRITE: host=%s:%d unit=%d addr=%d raw=%d (0x%04X, signed=%d)",
                self._config.host,
                self._config.port,
                self._config.unit_id,
                address, raw, raw, signed,
            )
        try:
            result = await self._client.write_register(
                address, raw, device_id=self._config.unit_id
            )
        except Exception:
            self._connected = False
            raise
        if result.isError():
            self._connected = False
            raise IOError(f"Modbus write error at register {address}: {result}")
        logger.info(
            "Modbus WRITE OK: addr=%d raw=%d (0x%04X, signed=%d)",
            address, raw, raw, signed,
        )

    async def _write_s16(self, address: int, value: int) -> None:
        """Write a signed 16-bit value to a holding register.

        Handles two's complement encoding for negative values.
        """
        await self._write_register(address, value & 0xFFFF)

    async def _verify_remote_state(self, expected_remote: int, expected_active: int) -> None:
        """Read back remote-control registers to confirm writes were applied.

        Some gateways/inverter firmwares report REMOTE_ENABLE as 0 even while
        ACTIVE_POWER is honored. Treat REMOTE_ENABLE mismatch as warning-only.
        The inverter may clamp ACTIVE_POWER to its rated maximum — treat a
        same-sign, lower-magnitude readback as accepted (clamped).
        """
        remote = await self._read_uint16(Registers.REMOTE_ENABLE)
        active_raw = await self._read_uint16(Registers.ACTIVE_POWER)
        active = active_raw - 0x10000 if active_raw >= 0x8000 else active_raw
        timeout = await self._read_uint16(Registers.REMOTE_TIMEOUT)

        logger.info(
            "VERIFY readback: remote=%d, active_raw=%d (0x%04X, signed=%d), "
            "timeout=%d | expected: remote=%d, active=%d",
            remote, active_raw, active_raw, active,
            timeout, expected_remote, expected_active,
        )

        # Check if inverter clamped to its rated power (same sign, lower magnitude)
        clamped = (
            active != expected_active
            and expected_active != 0
            and (active > 0) == (expected_active > 0)
            and abs(active) < abs(expected_active)
        )
        if clamped:
            logger.warning(
                "Inverter clamped active power: requested=%d, applied=%d "
                "(rated power limit), timeout=%d",
                expected_active, active, timeout,
            )
        elif active != expected_active:
            raise IOError(
                "Remote control write not applied "
                f"(remote={remote}, active_raw={active_raw} (0x{active_raw:04X}), "
                f"active_signed={active}, timeout={timeout}, "
                f"expected_remote={expected_remote}, expected_active={expected_active})"
            )
        if remote != expected_remote:
            logger.warning(
                "Remote enable readback mismatch (remote=%d expected=%d) but "
                "active power command is applied (active=%d timeout=%d)",
                remote,
                expected_remote,
                active,
                timeout,
            )
