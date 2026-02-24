# Power Master

Solar + battery optimisation system for residential deployments. Minimises electricity costs over a billing cycle by intelligently controlling charge/discharge modes, managing controllable loads, and exploiting arbitrage opportunities.

## Stack

- **Runtime:** Python 3.12 on Raspberry Pi (Linux)
- **Web:** FastAPI + Uvicorn + Jinja2 + HTMX
- **Database:** SQLite (WAL mode)
- **Hardware:** Fox ESS KH8 inverter via Modbus TCP
- **Messaging:** MQTT (Mosquitto) for load control + Home Assistant discovery
- **Solver:** PuLP MILP for 48h rolling optimisation

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Power Master                         │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐            │
│  │ Forecast  │  │ Tariff   │  │ Storm     │  Providers │
│  │ Solcast   │  │ Amber    │  │ BOM       │            │
│  │ OpenMeteo │  │          │  │           │            │
│  └────┬─────┘  └────┬─────┘  └─────┬─────┘            │
│       │              │              │                   │
│       └──────┬───────┴──────┬───────┘                   │
│              ▼              ▼                            │
│  ┌────────────────────────────────────┐                 │
│  │        Forecast Aggregator         │                 │
│  └────────────────┬───────────────────┘                 │
│                   ▼                                     │
│  ┌────────────────────────────────────┐                 │
│  │     MILP Solver (48h horizon)      │                 │
│  │     30-min slots, 5-min ticks      │                 │
│  └────────────────┬───────────────────┘                 │
│                   ▼                                     │
│  ┌────────────────────────────────────┐                 │
│  │    Control Loop + Hierarchy        │                 │
│  │  Safety > Storm > SOC > Plan       │                 │
│  └──────┬──────────────┬──────────────┘                 │
│         ▼              ▼                                │
│  ┌────────────┐  ┌────────────┐                         │
│  │  Inverter   │  │   Loads    │                         │
│  │  Modbus TCP │  │  MQTT/     │                         │
│  │             │  │  Shelly    │                         │
│  └─────────────┘  └────────────┘                         │
│                                                         │
│  ┌────────────────────────────────────┐                 │
│  │   Dashboard (FastAPI + SSE)        │                 │
│  │   Overview · Graphs · Accounting   │                 │
│  │   Settings · Load Management       │                 │
│  └────────────────────────────────────┘                 │
└─────────────────────────────────────────────────────────┘
```

## Operating Modes

| Mode | Name | Description |
|------|------|-------------|
| 1 | Self-Use | Consume PV, discharge battery to avoid grid import, export surplus |
| 2 | Self-Use (Zero Export) | Same as Self-Use but limit export to 0W (for negative feed-in prices) |
| 3 | Force Charge | Charge battery from grid at configurable rate |
| 4 | Force Discharge | Discharge battery to grid for arbitrage |

## Control Priority Hierarchy

1. **Hardware safety** — inverter limits, hard SOC min/max
2. **Storm reserve** — maintain configurable SOC during storm risk
3. **Critical SOC** — global minimum SOC floor
4. **Optimisation** — MILP solver plan
5. **Opportunistic** — arbitrage when conditions allow

## Modules

| Module | Purpose |
|--------|---------|
| `accounting/` | WACB cost basis tracking, billing cycle management, P&L events |
| `config/` | YAML config schema (Pydantic), config manager with hot-reload |
| `control/` | 5-min tick control loop, anti-oscillation guard, manual override |
| `dashboard/` | FastAPI routes, SSE live updates, Jinja2 templates, Chart.js graphs |
| `db/` | SQLite engine, repository pattern, schema migrations |
| `forecast/` | Solcast (solar), Open-Meteo (weather), BOM (storm alerts) providers |
| `hardware/` | Fox ESS Modbus TCP adapter, telemetry model, operating modes |
| `history/` | Telemetry aggregation, price recording, data retention |
| `loads/` | Load manager, Shelly adapter, MQTT load adapter, state machine |
| `mqtt/` | MQTT client, HA auto-discovery, telemetry publisher, subscriber |
| `optimisation/` | PuLP MILP solver, plan model, rebuild evaluator |
| `resilience/` | Health checker, degraded/safety mode manager |
| `storm/` | Storm monitor, probability thresholds, reserve calculation |
| `tariff/` | Amber Electric provider, spike detector |

## Data Providers

| Provider | Source | Data |
|----------|--------|------|
| Solcast | API | Solar PV forecast (P10/P50/P90) |
| Open-Meteo | API | Weather forecast (temp, cloud, wind, rain) |
| BOM | FTP/HTTP | Storm alerts (precis + warning products IDQ21033/35/37/38) |
| Amber Electric | API | Import/export pricing, spike detection |

## Financial Tracking

- **WACB** (Weighted Average Cost Basis): Tracks the average c/kWh of energy stored in the battery, updated on every charge event (grid import cost or solar opportunity cost)
- **Billing cycles**: Configurable start day (1-28), tracks import cost, export revenue, self-consumption savings, arbitrage P&L, fixed costs
- **Arbitrage**: Configurable break-even delta between buy/sell prices accounting for battery degradation and round-trip efficiency

## Dashboard Pages

- **Overview**: Live SOC, power flows, buy/sell price, mode status with optimiser/user highlighting, 24h rolling chart with plan overlay
- **Graphs**: Energy, financial, battery, prices, solar, load — configurable time ranges (hours/days/weeks)
- **Accounting**: Current billing cycle summary, historical cycles, recent accounting events
- **Settings**: Provider API keys, hardware config, billing cycle day, storm warning products, Shelly/MQTT load devices

## Configuration

All settings in `config.yaml` (user overrides) merged with `config.defaults.yaml`. Hot-reload supported — settings changes apply without restart via the Settings UI.

Key configuration sections: `hardware`, `battery`, `providers`, `arbitrage`, `storm`, `planning`, `loads`, `mqtt`, `dashboard`, `accounting`, `fixed_costs`, `resilience`, `anti_oscillation`, `logging`.

## Running

```bash
pip install -e .
power-master          # or: python -m power_master
```

Dashboard available at `http://<host>:8080` by default.

## Testing

```bash
pytest tests/ -q      # 286 tests
```
