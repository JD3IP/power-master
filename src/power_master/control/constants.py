"""Control loop and system constants — replaces magic numbers throughout codebase."""

from __future__ import annotations

# ─── Control Loop Timing ────────────────────────────────────────────────────
# Main control tick interval in seconds (evaluates plan, dispatches commands)
CONTROL_TICK_INTERVAL_SECONDS = 300  # 5 minutes

# Command refresh interval in seconds (re-sends remote mode commands to maintain state)
COMMAND_REFRESH_INTERVAL_SECONDS = 20

# Plan staleness warning threshold (multiple of evaluation interval)
PLAN_STALENESS_THRESHOLD_MULTIPLIER = 2
PLAN_STALENESS_WARNING_COOLDOWN_SECONDS = 600  # 10 minutes

# ─── Telemetry & Buffering ──────────────────────────────────────────────────
# Maximum number of telemetry samples to buffer before forcing flush
TELEMETRY_BUFFER_MAX_RECORDS = 10

# Maximum age of telemetry buffer before forced flush in seconds
TELEMETRY_BUFFER_FLUSH_SECONDS = 30

# ─── Database & Persistence ─────────────────────────────────────────────────
# Interval for storing telemetry to database in seconds
DB_STORE_INTERVAL_SECONDS = 60

# Interval for flushing historical data in seconds
HISTORY_FLUSH_INTERVAL_SECONDS = 1800  # 30 minutes

# ─── Load Management ────────────────────────────────────────────────────────
# Load polling interval in seconds
LOAD_POLL_INTERVAL_SECONDS = 30

# ─── Connection & Resilience ────────────────────────────────────────────────
# Reconnect interval in seconds
RECONNECT_INTERVAL_SECONDS = 30

# Circuit breaker threshold: max consecutive failures before opening
CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5

# Provider update retry count
PROVIDER_RETRY_COUNT = 3

# ─── Modbus RTU Inverter (FoxESS KH) ────────────────────────────────────────
# Modbus unit ID for FoxESS KH series (default)
FOXESS_UNIT_ID_DEFAULT = 247

# PV Power Registers (I32 pairs, little-endian)
FOXESS_REG_PV1_POWER_HI = 39280
FOXESS_REG_PV1_POWER_LO = 39279
FOXESS_REG_PV2_POWER_HI = 39282
FOXESS_REG_PV2_POWER_LO = 39281
FOXESS_REG_PV3_POWER_HI = 39284
FOXESS_REG_PV3_POWER_LO = 39283
FOXESS_REG_PV4_POWER_HI = 39286
FOXESS_REG_PV4_POWER_LO = 39285

# Battery Power Registers (I32 pair)
FOXESS_REG_BATTERY_POWER_HI = 31068
FOXESS_REG_BATTERY_POWER_LO = 31067

# Grid Power Registers (I32 pair)
FOXESS_REG_GRID_POWER_HI = 31070
FOXESS_REG_GRID_POWER_LO = 31069

# Load Power Register (single I16)
FOXESS_REG_LOAD_POWER = 31072

# Battery SOC Register
FOXESS_REG_BATTERY_SOC = 31007

# Battery Voltage Register
FOXESS_REG_BATTERY_VOLTAGE = 31010

# Battery Temperature Register
FOXESS_REG_BATTERY_TEMP = 31011

# Inverter Work Mode Status Register
FOXESS_REG_INVERTER_STATUS = 31008

# Remote Control Power Setting (write)
FOXESS_REG_REMOTE_CONTROL_POWER = 44000

# Remote Control Mode Setting (write)
FOXESS_REG_REMOTE_CONTROL_MODE = 44001

# Remote Control Enable Flag (write)
FOXESS_REG_REMOTE_CONTROL_ENABLE = 44002

# Remote Mode Enable Value
FOXESS_REMOTE_ENABLE_VALUE = 1

# Remote Mode Disable Value
FOXESS_REMOTE_DISABLE_VALUE = 0

# FoxESS KH Inverter Work Modes (register values)
FOXESS_MODE_SELF_USE = 0
FOXESS_MODE_FORCE_CHARGE = 1
FOXESS_MODE_FORCE_DISCHARGE = 2
FOXESS_MODE_ZERO_EXPORT = 3
FOXESS_MODE_ZERO_IMPORT = 4

# Modbus TCP Timeout in seconds
MODBUS_TCP_TIMEOUT_SECONDS = 5

# Modbus RTU Timeout in seconds
MODBUS_RTU_TIMEOUT_SECONDS = 5

# Maximum retry count for failed Modbus operations
MODBUS_MAX_RETRIES = 3

# ─── Forecast & Weather ─────────────────────────────────────────────────────
# Forecast minimum confidence threshold
FORECAST_MIN_CONFIDENCE = 0.5

# ─── Storm Detection & Response ──────────────────────────────────────────────
# Storm probability threshold for activation
STORM_PROBABILITY_THRESHOLD = 0.7

# ─── Anti-Oscillation Guard ─────────────────────────────────────────────────
# Default oscillation cooldown in seconds
DEFAULT_OSCILLATION_COOLDOWN_SECONDS = 120

# Minimum power change threshold in watts before allowing mode switches
MIN_POWER_CHANGE_THRESHOLD_W = 500
