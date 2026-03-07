# Technology Stack

## Runtime & Build

- **Language**: Python 3.11+
- **Build System**: Hatchling
- **Package Manager**: pip
- **Container Runtime**: Docker (multi-stage builds)
- **Base Images**: `python:3.11-slim` (runtime), includes `coinor-cbc` for ARM/x86_64 support

## Core Framework & Web

- **Web Framework**: FastAPI 0.115+ (`src/power_master/dashboard/app.py`)
  - Async ASGI server with Uvicorn 0.34+
  - Jinja2 3.1+ templating for HTML rendering (`src/power_master/dashboard/templates/`)
  - Static file serving via StaticFiles middleware
  - Server-Sent Events (SSE) via sse-starlette 2.2+ for real-time dashboard updates

- **Configuration**: Pydantic 2.10+ with Pydantic Settings 2.7+
  - Type-safe config models in `src/power_master/config/schema.py`
  - YAML parsing via PyYAML 6.0+
  - Settings loader in `src/power_master/settings.py`

## Database

- **Primary DB**: SQLite with aiosqlite 0.21+
  - WAL (Write-Ahead Logging) mode for concurrent read access
  - Path: `power_master.db` (configurable via `db.path`)
  - Async queries with connection pooling via aiosqlite
  - Migration system in `src/power_master/db/migrations.py`
  - Schema & models in `src/power_master/db/models.py`
  - Repository pattern in `src/power_master/db/repository.py`

## Optimization & Algorithms

- **Linear Programming**: PuLP 2.9+
  - CBC solver (COIN-OR) for ARM/x86_64 optimal load scheduling
  - Solver integration in `src/power_master/optimisation/solver.py`
  - Constraint system in `src/power_master/optimisation/constraints.py`
  - Objective formulation in `src/power_master/optimisation/objective.py`
  - Load scheduling in `src/power_master/optimisation/load_scheduler.py`

## Hardware Communication

- **Modbus Protocol**: Pymodbus 3.7+
  - TCP and RTU (serial) client modes
  - Fox-ESS KH series inverter adapter in `src/power_master/hardware/adapters/foxess.py`
  - Register mapping for telemetry collection in `src/power_master/hardware/telemetry.py`

- **Serial Communication**: Pyserial 3.5+ (for Modbus RTU)

## External APIs & Integrations

### HTTP Client
- **httpx 0.28+**: Async HTTP client for all external API calls
  - Connection pooling, timeout management, bearer token auth

### Tariff Data
- **Amber Electric**: `src/power_master/tariff/providers/amber.py`
  - REST API at `https://api.amber.com.au/v1`
  - Bearer token authentication
  - 30-minute interval pricing data
  - Rate limit: 50 calls per 5 minutes
  - Both import (general) and export (feedIn) pricing channels

### Weather & Solar Forecasts
- **Open-Meteo**: `src/power_master/forecast/providers/openmeteo.py`
  - Free API, no authentication required
  - Hourly weather data (temperature, cloud cover, wind, precipitation)
  - Historical archive access

- **Forecast.Solar**: `src/power_master/forecast/providers/forecast_solar.py`
  - Solar PV generation forecasts
  - Configurable panel declination/azimuth angles
  - kWp sizing for parameterized estimates

### Storm Alerts
- **Bureau of Meteorology (BOM)**: `src/power_master/forecast/providers/bom_storm.py`
  - XML feed parsing from `https://www.bom.gov.au/fwo/`
  - Precis forecasts & warning products (IDQ21033, IDQ21035, IDQ21037, IDQ21038)
  - Storm probability & severity scoring

## MQTT & Home Assistant

- **MQTT Client**: aiomqtt 2.3+ (`src/power_master/mqtt/client.py`)
  - Async message broker integration
  - Topic-based pub/sub in `src/power_master/mqtt/topics.py`
  - Publisher/subscriber handlers in `src/power_master/mqtt/publisher.py` and `src/power_master/mqtt/subscriber.py`
  - Home Assistant MQTT Discovery in `src/power_master/mqtt/discovery.py`
  - Configurable broker host/port/auth in config

## Smart Load Control

- **Shelly Devices**: `src/power_master/loads/adapters/shelly.py`
  - Gen2 RPC endpoints (`/rpc/Switch.Set`, `/rpc/Switch.GetStatus`)
  - Local HTTP API over async httpx

- **MQTT Load Endpoints**: `src/power_master/loads/adapters/mqtt_load.py`
  - Generic MQTT-controlled loads via pub/sub
  - Custom command/state topics per load

- **Load Manager**: `src/power_master/loads/manager.py`
  - Base load abstraction in `src/power_master/loads/base.py`
  - Load scheduling integration with optimizer

## Logging & Monitoring

- **Structured Logging**: structlog 25.1+ (`src/power_master/logging/structured.py`)
  - JSON or text output formats
  - Context injection in `src/power_master/logging/context.py`
  - Log buffering for dashboard streaming in `src/power_master/dashboard/log_buffer.py`
  - Configurable log level & output (file/stdout)

## Updates & Container Management

- **Docker SDK**: docker 7.0+ (`src/power_master/updater.py`)
  - GitHub Container Registry (GHCR) image checking
  - In-place container updates without downtime
  - Version info baking at build time in `version.json`
  - Release notes from GitHub API

## Timezone Support

- **tzdata 2024.1+** (required on Windows for IANA timezone database)
- **Timezone utilities**: `src/power_master/timezone_utils.py` for UTC/local conversion

## Development & Testing

**Dev Dependencies** (optional):
- pytest 8.0+ with pytest-asyncio 0.25+ for async test support
- pytest-cov 6.0+ for coverage reporting
- ruff 0.9+ for linting (E, F, I, N, W, UP rules)
- mypy 1.14+ strict type checking
- time-machine 2.16+ for time-based testing

## Project Structure

```
src/power_master/
├── __init__.py
├── __main__.py              # Entry point
├── main.py                  # Application orchestrator
├── settings.py              # Config loader
├── timezone_utils.py        # TZ utilities
├── config/                  # Config management
│   ├── schema.py           # Pydantic models (13 config sections)
│   ├── manager.py          # YAML load/save
│   └── defaults.py         # Built-in defaults
├── db/                      # SQLite layer
│   ├── engine.py           # Async engine, WAL, migrations
│   ├── models.py           # Schema definitions
│   ├── repository.py       # Data access layer
│   └── migrations.py       # Schema versioning
├── dashboard/              # FastAPI web UI
│   ├── app.py             # App factory
│   ├── auth.py            # Session auth middleware
│   ├── log_buffer.py      # Ring buffer for logs
│   └── routes/            # API endpoints
│       ├── overview.py    # Real-time status
│       ├── plans.py       # Plan viewer
│       ├── accounting.py  # Cost analysis
│       ├── graphs.py      # Chart data
│       ├── sse.py         # Event streaming
│       ├── logs.py        # Log viewer
│       ├── settings.py    # Config editor
│       ├── optimiser_lab.py  # Solver lab
│       ├── setup.py       # Wizard
│       └── api.py         # Control API
├── hardware/               # Inverter control
│   ├── base.py            # Abstract adapter
│   ├── telemetry.py       # Measurement types
│   └── adapters/
│       └── foxess.py      # Modbus KH series
├── loads/                  # Smart load scheduling
│   ├── base.py            # Load abstraction
│   ├── manager.py         # Load coordination
│   └── adapters/
│       ├── shelly.py      # Shelly relay control
│       └── mqtt_load.py   # Generic MQTT load
├── mqtt/                   # Home Assistant integration
│   ├── client.py          # Connection wrapper
│   ├── topics.py          # Topic schema
│   ├── publisher.py       # Metrics publisher
│   ├── subscriber.py      # Command receiver
│   └── discovery.py       # HA MQTT discovery
├── forecast/              # Data forecasting
│   ├── base.py            # Provider interfaces
│   ├── aggregator.py      # Multi-provider fetch & merge
│   ├── solar_estimate.py  # PV irradiance models
│   └── providers/
│       ├── openmeteo.py   # Weather forecasts
│       ├── forecast_solar.py  # Solar generation
│       └── bom_storm.py   # Storm warnings
├── tariff/                # Price data
│   ├── base.py            # Provider interface
│   ├── schedule.py        # Time-slot container
│   ├── spike.py           # Spike detection
│   └── providers/
│       └── amber.py       # Amber Electric API
├── accounting/            # Cost tracking
│   ├── engine.py          # Period computation
│   ├── events.py          # Event recording
│   ├── cost_basis.py      # FIFO tracking
│   ├── fixed_costs.py     # Access fees
│   └── billing_cycle.py   # Period definition
├── optimisation/          # Load scheduler
│   ├── plan.py            # Plan representation
│   ├── solver.py          # PuLP integration
│   ├── constraints.py     # Constraint builders
│   ├── objective.py       # Objective functions
│   ├── load_scheduler.py  # Main scheduler
│   ├── rebuild_evaluator.py  # Delta detection
│   └── backtest_lab.py    # Simulation tools
├── control/               # Real-time execution
│   ├── loop.py            # Main control thread
│   ├── command.py         # Command execution
│   ├── hierarchy.py       # Multi-mode priorities
│   ├── anti_oscillation.py  # Hysteresis filtering
│   └── manual_override.py  # User intervention
├── resilience/            # Health & fallback
│   ├── manager.py         # Mode switching
│   ├── health_check.py    # Component checks
│   ├── fallback.py        # Degraded mode logic
│   └── modes.py           # Resilience states
├── storm/                 # Storm reserve mode
│   ├── monitor.py         # Alert tracking
│   └── reserve.py         # Reserve management
├── history/               # Data collection & prediction
│   ├── loader.py          # Historical backfill
│   ├── collector.py       # Real-time collection
│   ├── patterns.py        # Pattern recognition
│   └── prediction.py      # Load forecasting
└── logging/               # Structured logging
    ├── structured.py      # Formatters/handlers
    └── context.py         # Context vars
```

## Configuration Files

- **`pyproject.toml`**: Project metadata, dependencies, tool config (pytest, ruff, mypy)
- **`config.defaults.yaml`**: Built-in defaults for all settings
- **`config.yaml`** (user): Overrides for deployed instance (gitignored)
- **`.env`**: Optional environment variable overrides (not actively used in current codebase)

## Deployment

- **Docker**: Multi-stage Dockerfile with CBC solver included for ARM compatibility
- **Docker Compose**: `docker-compose.yml` (host networking for inverter/MQTT access)
- **Container Registry**: GHCR (`ghcr.io/jd3ip/power-master`)
- **Volumes**: `/data` for persistent config + database
- **Environment**: `TZ` timezone variable support

## System Dependencies (Docker)

- `gcc` (build-time for C extensions)
- `coinor-cbc` (runtime for PuLP LP solver on ARM/aarch64)
- `libatomic1` (required on ARMv7 for atomic operations)
