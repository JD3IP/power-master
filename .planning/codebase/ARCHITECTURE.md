# Power Master Architecture

## Executive Summary

Power Master is a **Solar + Battery Optimisation System** for residential deployments. It minimises electricity costs over a billing cycle by intelligently controlling charge/discharge modes, managing controllable loads, and exploiting arbitrage opportunities through a **48-hour rolling MILP (Mixed Integer Linear Programming) solver**.

### Core Technology Stack
- **Runtime:** Python 3.11+ on Raspberry Pi (Linux) + Windows
- **Web:** FastAPI + Uvicorn + Jinja2 + HTMX + Server-Sent Events (SSE)
- **Database:** SQLite (WAL mode) with async SQLite bindings (`aiosqlite`)
- **Hardware Control:** Fox ESS KH8 inverter via Modbus TCP
- **Messaging:** MQTT (Mosquitto) for load control + Home Assistant auto-discovery
- **Optimization:** PuLP MILP solver (CBC backend) for 48h rolling plans
- **Configuration:** YAML + Pydantic v2 for strict schema validation

---

## System Layers

### 1. **Entry Point & Lifecycle Management**
**Files:** `src/power_master/__main__.py`, `src/power_master/main.py`

The `Application` class orchestrates the entire system startup and shutdown sequence:

```
Startup Order:
  1. Config loading (YAML + Pydantic validation)
  2. SQLite database init (migrations, WAL mode)
  3. Fox-ESS adapter connection (Modbus TCP)
  4. Provider initialization (solar, weather, storm, tariff)
  5. Forecast aggregator setup
  6. Resilience manager (health checks)
  7. Storm monitoring
  8. Accounting engine (WACB, billing cycles)
  9. Load manager (MQTT + Shelly controllers)
 10. MQTT client + Home Assistant discovery
 11. Initial forecast fetch
 12. Initial plan generation
 13. Control loop start (5-min ticks)
 14. Dashboard server (FastAPI on port 8000)
```

**Key Pattern:** Every component holds a reference for cleanup. The `_stop_event` asyncio event coordinates graceful shutdown.

---

### 2. **Configuration Layer**
**Files:**
- `src/power_master/config/schema.py` - Pydantic models for all system settings
- `src/power_master/config/manager.py` - Config loading, merging, persistence
- `src/power_master/config/defaults.py` - Default values
- `config.yaml`, `config.defaults.yaml` - YAML configuration files

**Design:**
- Pydantic v2 `BaseModel` subclasses define strict schema with validation
- `ConfigManager` loads from YAML, merges user overrides, validates via Pydantic
- Supports hot-reload: config changes trigger plan rebuilds but don't crash the system
- Session secrets auto-generated if missing (for dashboard auth)

**Key Config Objects:**
- `AppConfig` - Top-level root
- `BatteryConfig` - Capacity, SOC limits (hard/soft), charge/discharge rates
- `PlanningConfig` - Horizon (48h), slot duration (30min), rebuild intervals
- `ArbitrageConfig` - Price thresholds for arbitrage decisions
- `LoadProfileConfig` - Default loads by time-of-day (fallback when insufficient history)
- `BatteryTargetsConfig` - Target SOC for evening, morning, daytime reserve
- `DashboardConfig` - Web UI settings, auth config
- `MQTTConfig` - Broker connection details

---

### 3. **Database Layer**
**Files:**
- `src/power_master/db/models.py` - 17 SQL table definitions (schema version = 1)
- `src/power_master/db/engine.py` - SQLite connection, WAL mode, migrations
- `src/power_master/db/repository.py` - Data access object (DAO) for all tables
- `power_master.db` - SQLite database file

**Table Organization:**

| Category | Tables | Purpose |
|----------|--------|---------|
| **Config** | `config_versions` | Audit trail of config changes |
| **Telemetry** | `telemetry` | Historical inverter readings (SOC, power, battery, etc.) |
| **Forecasts** | `forecast_snapshots` | Solar, weather, storm forecasts + metadata |
| **Tariffs** | `tariff_schedules` | Import/export prices by time slot (30-min blocks) |
| **Plans** | `optimisation_plans`, `plan_slots`, `plan_events` | MILP solver outputs + plan history |
| **Loads** | `load_schedules`, `load_command_history` | Scheduled load commands + execution log |
| **Accounting** | `accounting_events`, `cost_basis_history` | Energy flows (import/export/self-use) + WACB tracking |
| **History** | `price_history`, `load_history` | Historical prices, load patterns for prediction |

**Key Patterns:**
- All timestamps use ISO 8601 format (UTC)
- Foreign keys link plans to forecast/tariff snapshots (snapshot-based optimization)
- JSON columns store complex nested data (forecasts, metrics, constraints)
- Indexes on `created_at`, `status`, `provider_type` for query performance
- Async SQLite via `aiosqlite.Connection` (non-blocking I/O)

**Repository Methods:** CRUD operations organized by table group (telemetry, forecasts, plans, etc.)

---

### 4. **Hardware Abstraction Layer**
**Files:**
- `src/power_master/hardware/base.py` - Protocol definition for inverter adapters
- `src/power_master/hardware/adapters/foxess.py` - Fox ESS KH8 Modbus TCP implementation
- `src/power_master/hardware/telemetry.py` - Telemetry dataclass (SOC, power, battery metrics)

**Design:**
- `InverterAdapter` Protocol allows multiple hardware backends
- `OperatingMode` enum: AUTO, SELF_USE, SELF_USE_ZERO_EXPORT, FORCE_CHARGE, FORCE_DISCHARGE, FORCE_CHARGE_ZERO_IMPORT
- `InverterCommand` contains mode + power target + export limit
- `CommandResult` indicates success/latency/error message
- Remote modes (FORCE_*) require periodic refresh (~30s timeout on Fox ESS)

**Telemetry Fields:** SOC (%), battery power (W), solar power (W), grid power (W), load power (W), battery voltage, temperature, grid availability

---

### 5. **Forecast & Provider Layer**
**Files:**
- `src/power_master/forecast/base.py` - Abstract SolarProvider, WeatherProvider, StormProvider protocols
- `src/power_master/forecast/providers/forecast_solar.py` - Solcast integration (solar)
- `src/power_master/forecast/providers/openmeteo.py` - OpenMeteo integration (weather)
- `src/power_master/forecast/providers/bom_storm.py` - Bureau of Meteorology (storm)
- `src/power_master/forecast/aggregator.py` - Merges all provider outputs
- `src/power_master/forecast/solar_estimate.py` - Cloud cover confidence scoring

**Data Flow:**
```
Provider (Forecast Solar)
  ↓ (30-min slots, W)
Forecast Snapshots (DB)
  ↓
Aggregator (single unified state)
  ↓ (SolarForecast, WeatherForecast, StormForecast)
Control Loop + MILP Solver
```

**Key Classes:**
- `SolarForecast` - Array of 30-min slots with power (W) and confidence
- `WeatherForecast` - Cloud cover (%), wind speed
- `StormForecast` - Probability, window start/end
- `AggregatedForecast` - Unified view with last-update timestamps

---

### 6. **Tariff & Pricing Layer**
**Files:**
- `src/power_master/tariff/base.py` - TariffProvider, TariffSlot, TariffSchedule
- `src/power_master/tariff/providers/amber.py` - Amber Electric integration
- `src/power_master/tariff/schedule.py` - Tariff schedule management
- `src/power_master/tariff/spike.py` - Spike detection (price > threshold)

**Data Structure:**
- `TariffSlot` - 30-min period with import_price_cents, export_price_cents, descriptor
- Descriptors: "general", "controlled_load", "spike", "off-peak", "peak"
- Fetched hourly, stored in DB, queried by plan solver

**Spike Detection:**
- Threshold configurable (default 100 c/kWh)
- Triggers load shedding and resilience mode upgrade
- Dampening applied to avoid solver overreaction

---

### 7. **Optimization Layer (MILP Solver)**
**Files:**
- `src/power_master/optimisation/solver.py` - PuLP MILP model builder + CBC solver
- `src/power_master/optimisation/plan.py` - OptimisationPlan + PlanSlot dataclasses
- `src/power_master/optimisation/constraints.py` - All constraint functions
- `src/power_master/optimisation/objective.py` - Objective function (cost minimization)
- `src/power_master/optimisation/load_scheduler.py` - Second-pass load assignment
- `src/power_master/optimisation/rebuild_evaluator.py` - Decide if replan needed

**Solver Inputs:**
```python
@dataclass
class SolverInputs:
    solar_forecast_w: list[float]          # 96 slots (48h × 2)
    load_forecast_w: list[float]           # Per-slot
    import_rate_cents: list[float]         # Time-of-use pricing
    export_rate_cents: list[float]
    is_spike: list[bool]                   # Spike flags
    current_soc: float                     # 0.0-1.0
    wacb_cents: float                      # Weighted avg cost basis
    # ... config refs, constraints, targets
```

**Decision Variables:**
- `charge_power[t]` - Battery charge in slot t (W)
- `discharge_power[t]` - Battery discharge in slot t (W)
- `grid_import[t]` - Grid import in slot t (Wh)
- `grid_export[t]` - Grid export in slot t (Wh)
- `load_power[t]` - Scheduled load power in slot t (W)
- `soc[t]` - State of charge at slot end (fractional)
- `is_exporting[t]` - Binary: 1 if exporting, 0 if not
- `y_switch[t]` - Binary: 1 if mode change from t-1 to t

**Constraints:**
- Energy balance: `soc[t] = soc[t-1] + (charge - discharge) / capacity`
- Safety limits: SOC hard limits (5%-95%), soft limits (10%-90%)
- Power limits: Max charge/discharge rates
- Battery targets: Evening SOC ≥ 90% by 4pm, morning ≥ 20% by 6am
- Storm reserve: Keep SOC ≥ storm_reserve_percent if storm active
- Arbitrage: Buy low, sell high (price delta > break-even threshold)
- Anti-oscillation: Penalize mode switching
- Charge taper: Slow down near max SOC

**Objective:** Minimize `(grid_import * import_price) - (grid_export * export_price) + (mode_switch_penalty)`

**Output:** `OptimisationPlan` with 96 `PlanSlot` objects, one per 30-min interval

---

### 8. **Control Loop**
**Files:** `src/power_master/control/loop.py`

**5-Minute Tick Cycle:**
```
1. Read telemetry from inverter
2. Check manual overrides (user-initiated commands)
3. Fetch current plan slot
4. Derive command from slot (mode + power)
5. Evaluate control hierarchy
6. Apply anti-oscillation guard
7. Dispatch command to inverter
8. Record telemetry in DB
9. Trigger callbacks (listeners for SSE)
```

**Control Hierarchy (priority order):**
```
Safety (inverter limits, SOC hard limits)
  ↓
Storm mode (high reserve requirement)
  ↓
Spike response (shed non-essential loads, export threshold)
  ↓
Optimisation plan (MILP solver output)
```

**LoopState Snapshot:**
- `tick_count`, `last_tick_at`, `last_telemetry`, `current_plan`, `current_mode`, `last_command_result`

**Remote Mode Refresh:** Modes like FORCE_CHARGE need periodic refresh (~30s) because Fox ESS reverts to SELF_USE if no command received.

---

### 9. **Loads Management**
**Files:**
- `src/power_master/loads/base.py` - LoadController protocol + LoadState dataclass
- `src/power_master/loads/adapters/mqtt_load.py` - MQTT-based smart plugs
- `src/power_master/loads/adapters/shelly.py` - Shelly relay integration
- `src/power_master/loads/manager.py` - LoadManager orchestration

**Load Scheduling:**
- Each load has: ID, name, power_w, priority_class (1=critical, 5=deferrable)
- Time windows: `earliest_start_hour`, `latest_end_hour`
- Constraints: `min_runtime_minutes`, `max_per_day_count`
- Prefer solar periods or low-price slots
- Spike handling: Shed non-essential loads

**State Tracking:**
- Manual overrides (1-hour default timeout)
- Command history (who told it to do what, when)
- Actual ON time accumulation (detect state changes)
- Daily runtime tracking (credit toward fulfillment)

---

### 10. **MQTT Integration**
**Files:**
- `src/power_master/mqtt/client.py` - Async MQTT client wrapper (aiomqtt)
- `src/power_master/mqtt/publisher.py` - Publish telemetry, plan, status
- `src/power_master/mqtt/subscriber.py` - Listen for external commands
- `src/power_master/mqtt/discovery.py` - Home Assistant MQTT discovery
- `src/power_master/mqtt/topics.py` - Topic constants

**Design:**
- Non-blocking async operations
- Graceful fallback if broker unavailable
- MQTT discovery for Home Assistant integration (auto-add devices)
- Publish: telemetry (every 5 min), plan (on rebuild), status
- Subscribe: manual commands from Home Assistant

---

### 11. **Accounting & Financial Tracking**
**Files:**
- `src/power_master/accounting/engine.py` - AccountingEngine (main orchestrator)
- `src/power_master/accounting/events.py` - AccountingEvent dataclass
- `src/power_master/accounting/cost_basis.py` - WACB (Weighted Average Cost Basis) tracker
- `src/power_master/accounting/billing_cycle.py` - Monthly billing cycle tracking
- `src/power_master/accounting/fixed_costs.py` - Daily target cost calculations

**Core Concept: WACB (Weighted Average Cost Basis)**
- Tracks the "virtual cost" of stored energy in the battery
- Updated every time energy enters (grid import, solar charge)
- Affects profitability calculations

**Accounting Events:**
- Grid import: `+cost_cents`, updates WACB
- Grid export: `-revenue_cents` (gain), uses current WACB to compute profit
- Self-consumption: `+cost_cents` (opportunity cost), updates WACB
- Load activation: Energy consumed + cost

**Billing Cycle:**
- Configurable day-of-month (default: billing day from Amber tariff)
- Accumulates daily costs, weekly costs
- Compares actual vs. daily target

**Daily Target Calculation:**
```
Fixed costs (solar amortization, grid fees) +
Arbitrage margin (if applicable)
= Daily cost target
```

---

### 12. **Resilience & Health Checking**
**Files:**
- `src/power_master/resilience/manager.py` - ResilienceManager state machine
- `src/power_master/resilience/health_check.py` - HealthChecker (provider monitoring)
- `src/power_master/resilience/modes.py` - ResilienceLevel enum
- `src/power_master/resilience/fallback.py` - Fallback strategies

**Resilience Levels:**
- `NORMAL` - All providers healthy
- `DEGRADED` - One provider down (use defaults)
- `MINIMAL` - Multiple providers down (conservative mode)
- `OFFLINE` - No providers available (battery self-use only)

**Health Checks:**
- Forecast provider: Latest fetch age < threshold (e.g., 4 hours)
- Tariff provider: Latest fetch age < threshold (e.g., 2 hours)
- Hardware: Inverter connection active
- Database: Writable

**State Transition:** Hysteresis prevents oscillation (unhealthy → 2 evals before degraded)

---

### 13. **Storm Monitoring & Reserve**
**Files:**
- `src/power_master/storm/monitor.py` - Real-time storm tracking
- `src/power_master/storm/reserve.py` - Reserve SOC management

**Logic:**
- If storm forecast (BOM) shows probability > threshold + window overlaps planning horizon:
  - Increase minimum SOC (reserve) to e.g., 50-70%
  - Replan with higher reserve requirement
  - Export is discouraged
- Window-based: Reserves only apply during predicted window

---

### 14. **Logging & Structured Context**
**Files:**
- `src/power_master/logging/structured.py` - Structlog setup
- `src/power_master/logging/context.py` - Context injection (request IDs, user, etc.)
- `src/power_master/dashboard/log_buffer.py` - In-memory circular buffer for SSE

**Pattern:** All logs structured via Structlog (JSON-friendly), with context fields (request_id, user, component)

---

### 15. **Dashboard / Web UI**
**Files:**
- `src/power_master/dashboard/app.py` - FastAPI factory
- `src/power_master/dashboard/auth.py` - Session-based auth middleware
- `src/power_master/dashboard/routes/` - Route handlers (overview, graphs, accounting, settings, plans, logs, etc.)
- `src/power_master/dashboard/templates/` - Jinja2 templates
- `src/power_master/dashboard/static/` - CSS, JS, weather icons

**Key Routes:**
- `/` - Overview (real-time telemetry, current plan slot, status)
- `/graphs` - Historical graphs (price, SOC, load, export)
- `/accounting` - Monthly P&L, cost tracking
- `/plans` - Plan history, details, rebuild logs
- `/settings` - Config editor (reload on save)
- `/logs` - Streaming logs via SSE
- `/setup` - Initial setup wizard (first-run experience)
- `/api/*` - JSON APIs for programmatic access

**Tech Stack:**
- **FastAPI** - Async web framework
- **Uvicorn** - ASGI server
- **Jinja2** - Template rendering
- **HTMX** - Client-side interactivity
- **Server-Sent Events (SSE)** - Real-time updates without WebSocket overhead

**Auth:**
- Optional session-based authentication
- User roles: viewer, editor, admin
- Session secret auto-generated on first use

---

### 16. **History & Prediction**
**Files:**
- `src/power_master/history/collector.py` - Telemetry collection
- `src/power_master/history/loader.py` - Load forecast from historical patterns
- `src/power_master/history/patterns.py` - Pattern analysis (daily cycles)
- `src/power_master/history/prediction.py` - Load prediction for next N days

**Data Flow:**
1. Every 5 min: Store telemetry (SOC, power flows)
2. Every plan rebuild: Query 7-14 days of history
3. Analyze patterns by hour-of-day
4. Predict tomorrow's load profile
5. Feed to MILP solver

---

## Data Flow Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                    REAL-TIME DATA FLOWS                         │
└──────────────────────────────────────────────────────────────────┘

Hardware (Fox ESS)
  ↓ (every 5 min via Modbus TCP)

Inverter Telemetry (SOC, power, battery temp, grid available)
  ↓
Control Loop (5-min tick)
  ├─ Read telemetry
  ├─ Get current plan slot
  ├─ Evaluate hierarchy
  ├─ Dispatch command
  ├─ Store telemetry → DB
  └─ Trigger callbacks (SSE)

Storage (SQLite DB)
  ├─ telemetry (every 5 min)
  ├─ load_command_history (on dispatch)
  └─ accounting_events (on energy flow)

Dashboard (FastAPI + SSE)
  ├─ Overview: live telemetry + plan slot
  ├─ Graphs: historical data
  └─ Logs: streaming via SSE

┌──────────────────────────────────────────────────────────────────┐
│                  PLAN OPTIMIZATION FLOW                         │
└──────────────────────────────────────────────────────────────────┘

Trigger Events:
  - Periodic (hourly)
  - Forecast delta (> 15% change)
  - Tariff change
  - Storm detected
  - SOC deviation > tolerance
  - Price spike
  ↓
Forecast Aggregator
  ├─ Fetch solar forecast (Solcast)
  ├─ Fetch weather (OpenMeteo)
  ├─ Fetch storm (BOM)
  └─ Fetch tariff (Amber)
  ↓ (store in DB)

MILP Solver (PuLP)
  ├─ Build constraint matrix
  ├─ Solve 48h horizon (96 slots)
  ├─ Return slot decisions (mode, power)
  └─ Store plan in DB
  ↓
Load Scheduler (second pass)
  ├─ Assign deferrable loads to slots
  ├─ Prefer solar or low-price slots
  └─ Respect time windows + runtime limits
  ↓
OptimisationPlan
  ├─ 96 PlanSlots (30-min each)
  ├─ Metrics (objective score, solver time)
  └─ Constraints applied (reserve, targets, etc.)
  ↓
Control Loop
  ├─ Current slot → command
  └─ Dispatch to inverter

┌──────────────────────────────────────────────────────────────────┐
│               ACCOUNTING & FINANCIAL TRACKING                   │
└──────────────────────────────────────────────────────────────────┘

Energy Flow Events
  ├─ Grid import (tariff rate)
  ├─ Grid export (feed-in rate)
  └─ Self-consumption (PV load)
  ↓
AccountingEngine
  ├─ Update WACB (weighted avg cost basis)
  ├─ Record billing cycle cost
  ├─ Calculate daily P&L
  └─ Track arbitrage margin
  ↓
Accounting Events Table
  └─ Historical cost tracking

```

---

## Key Abstractions & Patterns

### Protocol-Based Design (Duck Typing)
- `InverterAdapter` protocol allows multiple hardware backends
- `SolarProvider`, `WeatherProvider`, `StormProvider`, `TariffProvider` protocols
- `LoadController` protocol for different load types

**Benefit:** Easy to add new hardware or data sources without modifying core logic.

### Event-Driven Architecture
- Telemetry triggers callbacks in control loop
- Plan rebuild triggers forecasts fetches
- Config changes trigger plan rebuilds
- Manual overrides trigger load commands

### Snapshot-Based Optimization
- Each plan is linked to specific forecast/tariff snapshots in DB
- Prevents "stale data" issues when forecasts update mid-plan
- Allows replaying old plans with their original inputs

### Async/Await Throughout
- Non-blocking I/O for database, network, hardware
- `asyncio.gather()` for parallel operations
- `asyncio.Event` for coordination (stop_event)

### Configuration as Code (Pydantic)
- Single source of truth: `AppConfig` dataclass
- YAML → Pydantic validation → runtime objects
- No magic strings; all fields typed

### Retry Logic & Resilience
- Provider health checks before querying
- Fallback to defaults (load profile, reserve thresholds)
- Graceful degradation (MINIMAL mode)

---

## Integration Points

### External Systems
| System | Integration | Purpose |
|--------|------------ |---------|
| **Fox ESS KH8** | Modbus TCP | Inverter control + telemetry |
| **Amber Electric** | HTTP API | Real-time electricity pricing |
| **Solcast** | HTTP API | Solar forecast (irradiance) |
| **OpenMeteo** | HTTP API | Weather forecast (cloud cover) |
| **BOM** | HTTP API | Storm warnings (probability + window) |
| **Mosquitto MQTT** | MQTT protocol | Load control + Home Assistant |
| **Home Assistant** | MQTT discovery | Device registry + automation |
| **SQLite** | Local file | Data persistence |

### Entry Points for Customization
- Add new `InverterAdapter` in `hardware/adapters/`
- Add new `SolarProvider` in `forecast/providers/`
- Add new `LoadController` in `loads/adapters/`
- Extend `TariffProvider` for different markets
- Customize constraint functions in `optimisation/constraints.py`

---

## Performance Characteristics

| Operation | Latency | Frequency | Notes |
|-----------|---------|-----------|-------|
| Telemetry read | 200-500ms | Every 5 min | Modbus TCP round-trip |
| Plan rebuild | 5-25s | Every 1h+ | PuLP solver timeout = 25s |
| Forecast fetch | 1-3s | Every 1h+ | HTTP to external APIs |
| Tariff update | 500ms | Every 1h | Amber API call |
| Control command | 100-300ms | Every 5 min or on change | Modbus write |
| Dashboard render | 50-200ms | On request | Jinja2 + DB queries |

---

## Error Handling & Recovery

### Provider Failures
- If forecast fails: Use last cached forecast
- If tariff fails: Use fallback prices (config.arbitrage.break_even_delta_cents)
- If storm fails: Assume no storm risk

### Hardware Failures
- If inverter unreachable: Log error, keep last command active
- If telemetry read fails: Use last known state
- Graceful degradation to MINIMAL resilience mode

### Database Failures
- Connection pooling handles transient issues
- WAL mode provides durability
- Config versions allow rollback if needed

### Solver Failures
- If PuLP timeout: Use previous plan
- If no feasible solution: Use conservative defaults (discharge battery, avoid imports)

---

## Shutdown Sequence

1. Signal handler captures SIGTERM/SIGINT
2. `_stop_event.set()` wakes all async tasks
3. Cancel all running tasks (forecast fetches, MQTT listeners)
4. Close hardware adapter (disconnect inverter)
5. Close MQTT client
6. Close database connection
7. Exit gracefully

