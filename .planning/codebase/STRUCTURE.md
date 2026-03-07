# Power Master Codebase Structure

## Directory Layout

```
power-master/
├── src/
│   ├── main.py                             # Entry point (if running directly)
│   └── power_master/
│       ├── __init__.py                     # Package init, version constant
│       ├── __main__.py                     # python -m power_master entry
│       ├── main.py                         # Application class + startup sequence
│       ├── settings.py                     # Global settings (paths, etc.)
│       ├── timezone_utils.py               # Timezone resolution helpers
│       ├── updater.py                      # Auto-update check for Docker images
│       │
│       ├── accounting/                     # Financial tracking & accounting
│       │   ├── __init__.py
│       │   ├── engine.py                   # Main AccountingEngine orchestrator
│       │   ├── billing_cycle.py            # Monthly cycle tracking + summaries
│       │   ├── cost_basis.py               # WACB (Weighted Avg Cost Basis) tracker
│       │   ├── events.py                   # AccountingEvent dataclass + factories
│       │   └── fixed_costs.py              # Daily target calculations
│       │
│       ├── config/                         # Configuration management
│       │   ├── __init__.py
│       │   ├── schema.py                   # Pydantic BaseModel definitions (all config classes)
│       │   ├── manager.py                  # ConfigManager (load, merge, validate)
│       │   └── defaults.py                 # Default values for config fallback
│       │
│       ├── control/                        # Real-time control logic
│       │   ├── __init__.py
│       │   ├── loop.py                     # ControlLoop (5-min tick orchestrator)
│       │   ├── command.py                  # ControlCommand creation + dispatch
│       │   ├── hierarchy.py                # Control priority evaluation (Safety > Storm > SOC > Plan)
│       │   ├── manual_override.py          # Manual command override handling
│       │   └── anti_oscillation.py         # Anti-oscillation guard (penalize mode switching)
│       │
│       ├── dashboard/                      # Web UI (FastAPI + Jinja2 + HTMX)
│       │   ├── __init__.py
│       │   ├── app.py                      # FastAPI factory + middleware setup
│       │   ├── auth.py                     # Session-based auth + AuthMiddleware
│       │   ├── log_buffer.py               # Circular log buffer for SSE streaming
│       │   ├── routes/                     # Blueprint-style route handlers
│       │   │   ├── __init__.py
│       │   │   ├── api.py                  # JSON API endpoints (programmatic access)
│       │   │   ├── overview.py             # Dashboard home page + real-time status
│       │   │   ├── graphs.py               # Historical graphs (price, SOC, load)
│       │   │   ├── accounting.py           # P&L + cost tracking views
│       │   │   ├── plans.py                # Plan history + details
│       │   │   ├── settings.py             # Config editor
│       │   │   ├── logs.py                 # Log streaming + search
│       │   │   ├── sse.py                  # Server-Sent Events endpoints
│       │   │   ├── optimiser_lab.py        # Advanced optimization lab (backtest, what-if)
│       │   │   └── setup.py                # Initial setup wizard (first-run)
│       │   ├── static/                     # CSS, JS, images
│       │   │   └── weather/                # Weather icon assets
│       │   └── templates/                  # Jinja2 HTML templates
│       │       ├── base.html               # Base layout (nav, sidebar, etc.)
│       │       ├── *.html                  # Page templates
│       │       └── partials/               # Reusable template components (HTMX)
│       │
│       ├── db/                             # Data access layer (SQLite)
│       │   ├── __init__.py
│       │   ├── engine.py                   # SQLite connection, WAL mode, migrations
│       │   ├── models.py                   # SQL table definitions (17 tables)
│       │   ├── migrations.py               # Schema versioning + upgrade logic
│       │   └── repository.py               # DAO (CRUD methods for all tables)
│       │
│       ├── forecast/                       # Forecast providers & aggregation
│       │   ├── __init__.py
│       │   ├── base.py                     # Abstract protocols (SolarProvider, WeatherProvider, etc.)
│       │   ├── aggregator.py               # ForecastAggregator (merges providers)
│       │   ├── solar_estimate.py           # Cloud cover confidence scoring
│       │   └── providers/                  # Concrete provider implementations
│       │       ├── __init__.py
│       │       ├── forecast_solar.py       # Solcast integration (irradiance, cloud cover)
│       │       ├── openmeteo.py            # OpenMeteo integration (weather: cloud, wind)
│       │       └── bom_storm.py            # Bureau of Meteorology (storm warnings)
│       │
│       ├── hardware/                       # Hardware abstraction layer
│       │   ├── __init__.py
│       │   ├── base.py                     # InverterAdapter protocol + enums
│       │   ├── telemetry.py                # Telemetry dataclass (SOC, power, battery)
│       │   └── adapters/                   # Concrete adapter implementations
│       │       ├── __init__.py
│       │       └── foxess.py               # Fox ESS KH8 Modbus TCP implementation
│       │
│       ├── history/                        # Historical analysis & load prediction
│       │   ├── __init__.py
│       │   ├── collector.py                # Telemetry collection + storage
│       │   ├── loader.py                   # Load forecast from history
│       │   ├── patterns.py                 # Daily pattern analysis (hour-of-day)
│       │   └── prediction.py               # Predict future load profile
│       │
│       ├── loads/                          # Controllable load management
│       │   ├── __init__.py
│       │   ├── base.py                     # LoadController protocol + LoadState
│       │   ├── manager.py                  # LoadManager orchestration
│       │   └── adapters/                   # Concrete load controller implementations
│       │       ├── __init__.py
│       │       ├── mqtt_load.py            # MQTT-based smart plugs
│       │       └── shelly.py               # Shelly relay integration
│       │
│       ├── logging/                        # Structured logging
│       │   ├── __init__.py
│       │   ├── structured.py               # Structlog setup (JSON-friendly)
│       │   └── context.py                  # Context injection (request ID, user, etc.)
│       │
│       ├── mqtt/                           # MQTT integration (Mosquitto)
│       │   ├── __init__.py
│       │   ├── client.py                   # MQTTClient wrapper (aiomqtt)
│       │   ├── publisher.py                # Publish telemetry, plans, status
│       │   ├── subscriber.py               # Subscribe to external commands
│       │   ├── discovery.py                # Home Assistant MQTT discovery
│       │   └── topics.py                   # Topic constants + naming conventions
│       │
│       ├── optimisation/                   # MILP solver & plan generation
│       │   ├── __init__.py
│       │   ├── solver.py                   # PuLP solver (CBC backend)
│       │   ├── plan.py                     # OptimisationPlan + PlanSlot dataclasses
│       │   ├── objective.py                # Objective function (cost minimization)
│       │   ├── constraints.py              # Constraint functions (safety, targets, etc.)
│       │   ├── load_scheduler.py           # Second-pass load assignment to slots
│       │   └── rebuild_evaluator.py        # Determine when to trigger plan rebuild
│       │
│       ├── optimiser_lab/                  # Advanced optimization UI
│       │   ├── __init__.py
│       │   └── app.py                      # Fastapi app for Optimiser Lab dashboard
│       │
│       ├── resilience/                     # Health checking & degradation modes
│       │   ├── __init__.py
│       │   ├── manager.py                  # ResilienceManager state machine
│       │   ├── health_check.py             # HealthChecker (provider monitoring)
│       │   ├── modes.py                    # ResilienceLevel enum
│       │   └── fallback.py                 # Fallback strategies
│       │
│       ├── storm/                          # Storm monitoring & reserve management
│       │   ├── __init__.py
│       │   ├── monitor.py                  # Real-time storm tracking
│       │   └── reserve.py                  # Reserve SOC management during storms
│       │
│       └── tariff/                         # Tariff/pricing data
│           ├── __init__.py
│           ├── base.py                     # TariffProvider protocol + TariffSlot/Schedule
│           ├── schedule.py                 # Tariff schedule management
│           ├── spike.py                    # Spike detection (price > threshold)
│           └── providers/                  # Concrete provider implementations
│               ├── __init__.py
│               └── amber.py                # Amber Electric API integration
│
├── tests/                                  # Unit & integration tests
│   ├── conftest.py                         # Pytest fixtures + configuration
│   ├── test_accounting/
│   ├── test_config/
│   ├── test_control/
│   ├── test_db/
│   ├── test_forecast/
│   ├── test_hardware/
│   ├── test_optimisation/
│   ├── test_resilience/
│   └── ... (organized by module)
│
├── scripts/                                # Utility scripts (not core app)
│   ├── setup.sh                            # Environment setup
│   └── ... (deployment helpers)
│
├── deploy/                                 # Deployment configs
│   └── ... (Docker, Kubernetes, systemd)
│
├── examples/                               # Example configurations + use cases
│   └── ... (sample YAML configs)
│
├── .claude/                                # Claude-specific working files
│   ├── vault/                              # Knowledge base (documentation, decisions)
│   └── ... (session notes)
│
├── .planning/                              # Architecture & planning docs
│   └── codebase/                           # Generated by this task
│       ├── ARCHITECTURE.md                 # System design + data flows
│       └── STRUCTURE.md                    # Directory structure (this file)
│
├── .github/                                # GitHub workflows (CI/CD)
│   └── workflows/
│       └── ... (tests, linting, build)
│
├── config.yaml                             # User-specific runtime config
├── config.defaults.yaml                    # Default config template
├── pyproject.toml                          # Project metadata + dependencies
├── Dockerfile                              # Docker image definition
├── docker-compose.yml                      # Docker Compose setup
├── docker-entrypoint.sh                    # Container startup script
├── README.md                               # Project overview
├── DEPLOY_PI.md                            # Raspberry Pi deployment guide
├── DEPLOY_SYNOLOGY.md                      # Synology NAS deployment guide
├── Functional Description.txt              # Detailed feature documentation
└── power_master.db                         # SQLite database (runtime-created)
```

---

## Module Organization & Relationships

### Layer 1: Entry Point
- `__main__.py` → `main.py` → `Application.start()`

### Layer 2: Configuration
- `config/schema.py` (Pydantic models) ← `config/manager.py` (load & validate) ← YAML files

### Layer 3: Infrastructure
- `db/engine.py` (SQLite) ← `db/models.py` (schema)
- `db/repository.py` (DAO layer) uses `db/engine.py`
- `logging/structured.py` (Structlog setup)
- `mqtt/client.py` (MQTT wrapper) ← `mqtt/publisher.py`, `mqtt/subscriber.py`

### Layer 4: Hardware & Providers
- `hardware/adapters/foxess.py` (implements `hardware/base.py` protocol)
- `forecast/providers/*.py` (implement `forecast/base.py` protocol)
- `tariff/providers/amber.py` (implements `tariff/base.py` protocol)
- `loads/adapters/*.py` (implement `loads/base.py` protocol)

### Layer 5: Data Aggregation
- `forecast/aggregator.py` (merges forecasts from providers)
- `history/loader.py` (load predictions from historical patterns)

### Layer 6: Optimization & Planning
- `optimisation/solver.py` (main MILP orchestrator)
  ├─ Uses `optimisation/constraints.py` (constraint builders)
  ├─ Uses `optimisation/objective.py` (objective function)
  ├─ Returns `optimisation/plan.py` (OptimisationPlan)
- `optimisation/load_scheduler.py` (second-pass load scheduling)
- `optimisation/rebuild_evaluator.py` (when to trigger rebuild)

### Layer 7: Control & Execution
- `control/loop.py` (main 5-min control loop)
  ├─ Uses `control/hierarchy.py` (priority evaluation)
  ├─ Uses `control/anti_oscillation.py` (mode-switch penalty)
  ├─ Uses `control/command.py` (command creation)
  ├─ Calls `hardware/adapters/foxess.py` (execute commands)
- `loads/manager.py` (manages load controllers)
  └─ Uses `loads/adapters/*.py` (Shelly, MQTT)

### Layer 8: Financial Tracking
- `accounting/engine.py` (main orchestrator)
  ├─ Uses `accounting/cost_basis.py` (WACB)
  ├─ Uses `accounting/billing_cycle.py` (cycles)
  ├─ Uses `accounting/events.py` (event creation)
  └─ Uses `accounting/fixed_costs.py` (daily targets)

### Layer 9: Resilience & Health
- `resilience/manager.py` (state machine)
  ├─ Uses `resilience/health_check.py` (evaluate provider health)
  ├─ Uses `resilience/modes.py` (ResilienceLevel enum)
  └─ Uses `resilience/fallback.py` (degradation strategies)

### Layer 10: Storm Management
- `storm/monitor.py` (tracks storm forecast)
- `storm/reserve.py` (manages reserve SOC)

### Layer 11: Web UI
- `dashboard/app.py` (FastAPI factory)
  ├─ Uses `dashboard/auth.py` (auth middleware)
  ├─ Uses `dashboard/log_buffer.py` (SSE logs)
  └─ Mounts `dashboard/routes/*.py` (all endpoints)

---

## Key File Descriptions

### Configuration & Startup
| File | Lines | Purpose |
|------|-------|---------|
| `src/power_master/main.py` | 1312 | Application class + 14-step startup sequence |
| `src/power_master/config/schema.py` | ~400 | Pydantic BaseModel definitions (AppConfig, BatteryConfig, etc.) |
| `src/power_master/config/manager.py` | ~200 | ConfigManager (load, merge, validate, persist) |

### Database
| File | Lines | Purpose |
|------|-------|---------|
| `src/power_master/db/models.py` | ~200 | SQL table definitions (17 tables) |
| `src/power_master/db/repository.py` | ~600 | DAO methods for all tables |
| `src/power_master/db/engine.py` | ~150 | SQLite connection, WAL mode, migrations |

### Optimization
| File | Lines | Purpose |
|------|-------|---------|
| `src/power_master/optimisation/solver.py` | ~400 | PuLP MILP solver builder |
| `src/power_master/optimisation/constraints.py` | ~500 | All constraint functions |
| `src/power_master/optimisation/plan.py` | ~150 | OptimisationPlan + PlanSlot dataclasses |
| `src/power_master/optimisation/load_scheduler.py` | ~300 | Load assignment to slots |

### Control
| File | Lines | Purpose |
|------|-------|---------|
| `src/power_master/control/loop.py` | ~400 | 5-min control loop orchestrator |
| `src/power_master/control/hierarchy.py` | ~200 | Priority evaluation (Safety > Storm > SOC > Plan) |
| `src/power_master/control/command.py` | ~150 | Command creation + dispatch |

### Accounting
| File | Lines | Purpose |
|------|-------|---------|
| `src/power_master/accounting/engine.py` | ~300 | Main accounting orchestrator |
| `src/power_master/accounting/cost_basis.py` | ~200 | WACB tracker |
| `src/power_master/accounting/events.py` | ~150 | AccountingEvent dataclass + factories |

### Dashboard
| File | Lines | Purpose |
|------|-------|---------|
| `src/power_master/dashboard/app.py` | ~200 | FastAPI factory + middleware |
| `src/power_master/dashboard/routes/api.py` | ~300 | JSON API endpoints |
| `src/power_master/dashboard/routes/overview.py` | ~200 | Main dashboard view |

---

## Naming Conventions

### Classes
- **Manager:** Orchestrates multiple components (e.g., `LoadManager`, `ResilienceManager`)
- **Engine:** Core computational logic (e.g., `AccountingEngine`, `SolverEngine`)
- **Adapter:** Implements hardware or provider protocol (e.g., `FoxESSAdapter`)
- **Provider:** Fetches external data (e.g., `SolarProvider`, `TariffProvider`)
- **Tracker:** Accumulates state (e.g., `CostBasisTracker`)
- **Checker:** Evaluates conditions (e.g., `HealthChecker`)
- **Config:** Data class for configuration (e.g., `BatteryConfig`)

### Dataclasses (Value Objects)
- `Telemetry` - Inverter readings
- `OptimisationPlan`, `PlanSlot` - Plan outputs
- `SolarForecast`, `WeatherForecast`, `StormForecast` - Forecast data
- `TariffSchedule`, `TariffSlot` - Tariff data
- `LoadState`, `LoadCommand`, `LoadOverride` - Load state
- `LoopState` - Control loop snapshot
- `ResilienceState` - Resilience manager state
- `AccountingEvent` - Financial event
- `InverterCommand`, `CommandResult` - Hardware commands
- `SolverInputs` - Solver input bundle

### Enums
- `OperatingMode` - Inverter modes (SELF_USE, FORCE_CHARGE, etc.)
- `SlotMode` - Plan slot modes (mirrors OperatingMode)
- `ResilienceLevel` - NORMAL, DEGRADED, MINIMAL, OFFLINE
- `LoadStatus` - Load controller status

### Functions & Methods
- **Verb-focused:** `fetch_prices()`, `send_command()`, `evaluate_hierarchy()`
- **Compound names:** `store_telemetry()`, `get_current_slot()`, `is_healthy()`
- **Query methods:** `get_*()`, `find_*()`, `query_*()`
- **Builder methods:** `build_objective()`, `add_*_constraint()`

### Constants & Configuration
- `SCHEMA_VERSION` - Database schema version
- `_REMOTE_MODES` - Tuple of modes needing refresh
- `MANUAL_LOAD_OVERRIDE_SECONDS` - Timeout duration
- `TABLES` - List of SQL table definitions

### Files
- `*_base.py` - Abstract protocols or base classes
- `*_manager.py` - Orchestration classes
- `*_engine.py` - Core computational logic
- `*_adapter.py` or in `adapters/` folder - Concrete implementations
- `models.py` - Data structure definitions (SQL schema or Pydantic models)
- `schema.py` - Pydantic configuration models

---

## Test Structure

```
tests/
├── conftest.py                         # Shared fixtures
├── test_accounting/
│   ├── test_engine.py                 # AccountingEngine tests
│   ├── test_cost_basis.py             # WACB tracker tests
│   └── test_billing_cycle.py          # Billing cycle tests
├── test_control/
│   ├── test_loop.py                   # Control loop tests
│   ├── test_hierarchy.py              # Hierarchy evaluation tests
│   └── test_command.py                # Command creation tests
├── test_db/
│   ├── test_repository.py             # DAO tests
│   └── test_migrations.py             # Schema migration tests
├── test_forecast/
│   ├── test_aggregator.py             # Aggregator tests
│   └── test_providers/                # Individual provider tests
├── test_optimisation/
│   ├── test_solver.py                 # MILP solver tests
│   ├── test_constraints.py            # Constraint validation tests
│   └── test_load_scheduler.py         # Load scheduler tests
└── test_resilience/
    ├── test_manager.py                # Resilience manager tests
    └── test_health_check.py           # Health checker tests
```

---

## Database Schema Overview

### Telemetry Tables
- `telemetry` - 5-min inverter readings (SOC, power, battery metrics)
- `load_history` - Load controller state changes + runtime

### Forecast Tables
- `forecast_snapshots` - Solar, weather, storm forecasts (with metadata)

### Tariff Tables
- `tariff_schedules` - Import/export prices by time slot

### Plan Tables
- `optimisation_plans` - Plan metadata (version, scores, constraints)
- `plan_slots` - 30-min slot decisions (mode, power, expected SOC)
- `plan_events` - Plan lifecycle events (created, rebuilt, applied)

### Accounting Tables
- `accounting_events` - Energy flow events (import, export, self-use)
- `cost_basis_history` - WACB snapshots over time

### Config & Management
- `config_versions` - Config audit trail (who changed what, when)

---

## Entry Points & Execution Paths

### Application Startup
```
1. Entry: python -m power_master
   ↓
2. __main__.py: from power_master.main import main; main()
   ↓
3. main.py: main() function parses args, creates Application, runs async loop
   ↓
4. Application.start(): 14-step initialization (config → DB → hardware → providers → ... → dashboard)
   ↓
5. Application.run(): Main event loop (control tick + forecast fetch + plan rebuild + dashboard server)
```

### Web UI Access
```
Browser → http://localhost:8000
  ↓
FastAPI (dashboard/app.py)
  ├─ / (overview)
  ├─ /graphs
  ├─ /accounting
  ├─ /plans
  ├─ /settings
  ├─ /logs (SSE stream)
  └─ /api/* (JSON endpoints)
```

### Control Loop (5-min Tick)
```
Timer fires → ControlLoop.tick()
  ├─ Read telemetry (Modbus TCP)
  ├─ Get current plan slot
  ├─ Evaluate hierarchy
  ├─ Dispatch command
  ├─ Store telemetry
  └─ Trigger callbacks (SSE)
```

### Plan Optimization
```
Trigger event detected → Application._on_plan_needed()
  ├─ ForecastAggregator.fetch_all()
  ├─ Solver.build_and_solve()
  ├─ LoadScheduler.schedule_loads()
  ├─ Store plan in DB
  └─ Update control loop state
```

---

## Configuration Hierarchy

```
1. Defaults (hardcoded in code)
   ↓
2. config.defaults.yaml (bundled template)
   ↓
3. config.yaml (user configuration)
   ↓
4. ConfigManager.load() + Pydantic validation
   ↓
5. AppConfig object (in-memory, typed)
```

**Hot reload:** Config changes are detected and trigger plan rebuild, but don't crash the system.

---

## Key Locations Summary

| Purpose | Location |
|---------|----------|
| **Main entry** | `src/power_master/main.py` (1312 lines) |
| **Solver** | `src/power_master/optimisation/solver.py` |
| **Control loop** | `src/power_master/control/loop.py` |
| **Hardware** | `src/power_master/hardware/adapters/foxess.py` |
| **Accounting** | `src/power_master/accounting/engine.py` |
| **Dashboard** | `src/power_master/dashboard/app.py` |
| **Database** | `src/power_master/db/repository.py` |
| **Config schema** | `src/power_master/config/schema.py` |
| **Config files** | `config.yaml`, `config.defaults.yaml` |
| **SQL schema** | `src/power_master/db/models.py` |
| **Tests** | `tests/` (organized by module) |
| **Deployment** | `deploy/`, `Dockerfile`, `docker-compose.yml` |
| **Docs** | `README.md`, `DEPLOY_PI.md`, `DEPLOY_SYNOLOGY.md` |

