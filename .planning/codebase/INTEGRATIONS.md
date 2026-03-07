# External Integrations

## Tariff/Pricing APIs

### Amber Electric
- **File**: `src/power_master/tariff/providers/amber.py`
- **API Base URL**: `https://api.amber.com.au/v1`
- **Authentication**: Bearer token in Authorization header
- **Rate Limits**: 50 calls per 5 minutes
- **Configuration**:
  ```yaml
  providers:
    tariff:
      type: "amber"
      api_key: "Bearer token from app.amber.com.au/developers"
      site_id: "Auto-discovered if not set"
      update_interval_seconds: 300  # 5 minutes
  ```
- **API Endpoints**:
  - `GET /sites` - List all sites on account
  - `GET /sites/{site_id}/prices/current` - Current + 144 intervals (48h forecast)
  - `GET /sites/{site_id}/prices` - Historical date range pricing
- **Data Format**:
  - 30-minute interval slots
  - Separate import (GENERAL) and export (feedIn) pricing
  - Prices in cents/kWh including all fees
  - `startTime` in UTC (NEM convention = end-of-interval)
- **Validation**:
  - Detects price gaps and logs warnings
  - Normalizes export price sign (positive = revenue)
  - Handles both `startTime` and legacy `nemTime` fields
  - UTC timezone validation with warnings for naive datetimes

### Solar Forecast Providers

#### Forecast.Solar
- **File**: `src/power_master/forecast/providers/forecast_solar.py`
- **API Base URL**: `https://api.forecast.solar`
- **Authentication**: None required
- **Configuration**:
  ```yaml
  providers:
    solar:
      latitude: -27.4698
      longitude: 153.0251
      declination: 20.0          # Panel tilt angle
      azimuth: 0.0               # 0=south (NH), 180=north (SH)
      kwp: 5.0                   # Installed PV size
      timezone: "Australia/Brisbane"
      update_interval_seconds: 21600  # 6 hours
  ```
- **API Endpoint**:
  - `GET /estimate/{lat}/{lon}/{declination}/{azimuth}/{kwp}` - Solar generation forecast
- **Data Format**:
  - Watts array (hourly estimates) or watt_hours_period
  - Message includes detected timezone
  - Fallback to bell curve if data unavailable
- **Response**:
  - Result object with watts (dict of timestamp -> watts)
  - Message metadata with API info and timezone

### Weather Providers

#### Open-Meteo
- **File**: `src/power_master/forecast/providers/openmeteo.py`
- **API Base URLs**:
  - Current: `https://api.open-meteo.com/v1/forecast`
  - Historical: `https://archive-api.open-meteo.com/v1/archive`
- **Authentication**: None required (free tier available)
- **Configuration**:
  ```yaml
  providers:
    weather:
      type: "openmeteo"
      latitude: -27.4698
      longitude: 153.0251
      update_interval_seconds: 3600  # 1 hour
      validity_seconds: 3600
  ```
- **API Parameters**:
  - `hourly`: temperature_2m, cloud_cover, wind_speed_10m, precipitation, relative_humidity_2m
  - `forecast_hours`: 48 (default)
  - `timezone`: UTC
- **Data Format**:
  - Hourly time series for each parameter
  - Cloud cover impacts solar irradiance calculations
  - Temperature for inverter efficiency adjustments
- **Response**:
  - Hourly array with timestamps and values
  - Timezone support in response metadata

### Storm Alert Providers

#### Bureau of Meteorology (BOM)
- **File**: `src/power_master/forecast/providers/bom_storm.py`
- **API Base URL**: `https://www.bom.gov.au/fwo`
- **Authentication**: None required
- **Configuration**:
  ```yaml
  providers:
    storm:
      type: "bom"
      state_code: "IDQ11295"           # Queensland default
      location_aac: "set via settings UI"
      warning_product_ids:
        - "IDQ21033"  # Severe Thunderstorm Warning
        - "IDQ21035"  # Severe Weather Warning
        - "IDQ21037"  # Tropical Cyclone Warning
        - "IDQ21038"  # Flood Warning
      update_interval_seconds: 21600   # 6 hours
  ```
- **API Endpoints**:
  - `GET /{state_code}.xml` - Precis forecast (location-specific)
  - `GET /{product_id}.xml` - Warning product feeds
- **Data Format**:
  - XML with forecast text and metadata
  - Keyword matching for storm probability scoring
  - Warning products with severity mapping
- **Severity Scoring**:
  - Precis keywords: thunderstorm (0.7), severe (0.8), hail (0.6), flash flood (0.8)
  - Warning products: severe (0.85-0.95), moderate (0.75)
  - Threshold for reserve trigger: 0.70 probability

## Hardware Integrations

### Fox-ESS KH Series Inverter
- **File**: `src/power_master/hardware/adapters/foxess.py`
- **Protocol**: Modbus TCP or RTU (serial)
- **Configuration**:
  ```yaml
  hardware:
    adapter: "foxess"
    foxess:
      connection_type: "tcp"          # "tcp" or "rtu"
      host: "192.168.1.100"           # TCP only
      port: 502                       # TCP only (standard Modbus)
      serial_port: "/dev/ttyUSB0"    # RTU only
      baudrate: 9600                  # RTU only
      unit_id: 247                    # Modbus slave ID
      poll_interval_seconds: 15       # Telemetry collection frequency
      watchdog_timeout_seconds: 3600  # Remote control watchdog
      remote_refresh_interval_seconds: 20  # Keep-alive for remote mode
  ```
- **Modbus Register Map**:
  - **Input Registers (FC4)** - Read-only telemetry:
    - `31002` - PV1 power (watts)
    - `31005` - PV2 power (watts)
    - `31014` - Grid meter (watts; inverter raw positive=export, negated to standard)
    - `31016` - Load power (watts; home consumption)
    - `31020` - Battery voltage (0.1V resolution)
    - `31021` - Battery current (0.1A resolution, positive=charging)
    - `31022` - Battery power (watts; KH raw inverted, converted to standard)
    - `31023` - Battery temperature (0.1°C resolution)
    - `31024` - Battery SOC (0-100 percentage)
    - `31027` - Inverter state (0=Self-Test, 3=Normal, 5=Fault)
  - **Holding Registers (FC3/6)** - Read/Write control:
    - `41000` - Work mode (0=Self-Use, 1=Feed-in, 2=Backup, 3=Force Charge, 4=Force Discharge)
    - `41009` - Max charge current (0.1A resolution, raw=50.0A)
    - `41010` - Max discharge current (0.1A resolution)
    - `41011` - Min SOC % (won't discharge below)
    - `41012` - Export limit (watts; 0=no export)
  - **Remote Control Registers (FC6)** - Write-only:
    - `44000` - Remote enable (0=off, 1=on)
    - `44001` - Remote timeout watchdog (seconds)
    - `44002` - Active power command (watts; negative=charge, positive=discharge)
- **Data Conversions**:
  - Sign flipping for battery power (KH convention: positive=discharge)
  - Grid meter negation (KH: positive=export → standard: negative=export)
  - 10x gain conversions for voltage/current/temperature
- **Operating Modes**:
  - Self-Use: normal optimization
  - Feed-in First: maximize grid export
  - Backup: maintain reserve for blackouts
  - Force Charge/Discharge: manual overrides

## Smart Load Control

### Shelly Devices
- **File**: `src/power_master/loads/adapters/shelly.py`
- **API Type**: Local HTTP REST API (Gen2 RPC)
- **Configuration**:
  ```yaml
  loads:
    shelly_devices:
      - name: "water_heater"
        host: "192.168.1.50"
        relay_id: 0
        power_w: 4000
        priority_class: 5
        enabled: true
        earliest_start: "00:00"
        latest_end: "23:59"
        duration_minutes: 60
        prefer_solar: true
        days_of_week: [0,1,2,3,4,5,6]
        min_runtime_minutes: 30
        ideal_runtime_minutes: 60
        max_runtime_minutes: 120
        allow_split_shifts: false
        completion_deadline: "18:00"
  ```
- **API Endpoints**:
  - `POST /rpc/Switch.Set` - Set relay state (on/off/toggle)
  - `GET /rpc/Switch.GetStatus` - Get current state and power
- **Parameters**:
  - `id`: Relay ID (0-based)
  - `on`: Boolean state
- **Response**:
  - Current relay state
  - Power consumption (if available)
  - Apower_active_w: Actual power draw

### Generic MQTT Load Endpoints
- **File**: `src/power_master/loads/adapters/mqtt_load.py`
- **Configuration**:
  ```yaml
  loads:
    mqtt_load_endpoints:
      - name: "ev_charger"
        command_topic: "home/ev/command"
        state_topic: "home/ev/state"
        power_w: 7000
        priority_class: 4
        enabled: true
        earliest_start: "22:00"
        latest_end: "06:00"
        duration_minutes: 480
        prefer_solar: false
        days_of_week: [0,1,2,3,4,5,6]
        min_runtime_minutes: 120
        ideal_runtime_minutes: 480
        max_runtime_minutes: 600
  ```
- **Message Format**:
  - Command topic: `{"action": "on"}` or `{"action": "off"}` (JSON)
  - State topic: JSON with power_w, status fields
- **Integration**:
  - Scheduler sends commands via MQTT publisher
  - Monitors state topic for feedback
  - Power estimation via configuration

## MQTT & Home Assistant

### MQTT Broker Connection
- **File**: `src/power_master/mqtt/client.py`
- **Configuration**:
  ```yaml
  mqtt:
    enabled: true
    broker_host: "localhost"
    broker_port: 1883
    username: ""
    password: ""
    topic_prefix: "power_master"
    publish_interval_seconds: 5
    ha_discovery_enabled: true
    ha_discovery_prefix: "homeassistant"
  ```
- **Client Library**: aiomqtt 2.3+ (async wrapper)
- **Features**:
  - Async connect/disconnect with error handling
  - Retained message support
  - Topic filtering via wildcards

### Topic Schema
- **File**: `src/power_master/mqtt/topics.py`
- **Pattern**: `{topic_prefix}/{component}/{entity}`
- **Published Topics** (Publisher: `src/power_master/mqtt/publisher.py`):
  - `power_master/battery/soc` - State of charge %
  - `power_master/battery/power_w` - Charging/discharge watts
  - `power_master/battery/voltage_v` - Battery voltage
  - `power_master/inverter/pv_power_w` - Solar generation
  - `power_master/inverter/grid_power_w` - Grid power (negative=export)
  - `power_master/inverter/load_power_w` - Home consumption
  - `power_master/system/status` - Overall health state
  - `power_master/plan/active_slot` - Current optimization slot
  - Per-load topics (Shelly, MQTT endpoints)

### Home Assistant MQTT Discovery
- **File**: `src/power_master/mqtt/discovery.py`
- **Discovery Prefix**: `homeassistant/{component}/{device_id}/{entity_id}/config`
- **Components Published**:
  - `sensor` - Battery SOC, power values, grid power
  - `switch` - Load control (if writable)
  - `number` - Set points (if configurable)
- **Device Class**: `energy`, `power`, `frequency`, `voltage`, `current`
- **Availability Topic**: `power_master/system/status`

### Command Subscriptions
- **File**: `src/power_master/mqtt/subscriber.py`
- **Topics**:
  - `power_master/control/+/command` - Load on/off commands
  - `power_master/config/+/set` - Config parameter updates
- **Callback Pattern**: `async def callback(topic: str, payload: str) -> None`

## Container & Updates

### GitHub Container Registry (GHCR)
- **Registry**: `ghcr.io/jd3ip/power-master`
- **File**: `src/power_master/updater.py`
- **APIs Used**:
  - `https://ghcr.io/v2/jd3ip/power-master/tags/list` - Tag enumeration
  - `https://api.github.com/repos/JD3IP/power-master/releases` - Release notes
  - GHCR token endpoint for manifest fetching
- **Update Flow**:
  1. Periodic check (1 hour interval) compares local version.json against GHCR latest
  2. User triggers update via Settings UI → POST /api/system/update
  3. Docker SDK pulls new image, tags old as rollback
  4. Recreates container with new image
  5. Startup health checks mark success/failure

### Docker Integration
- **Docker SDK**: `docker 7.0+`
- **Usage** in `src/power_master/updater.py`:
  - Container enumeration
  - Image pulling with progress callback
  - Container stop/start/remove
  - Volume inspection (for rollback)
- **Socket Access**: `/var/run/docker.sock` (requires docker group or sudo)

## Authentication & Authorization

### Session Authentication
- **File**: `src/power_master/dashboard/auth.py`
- **Storage**: User credentials in config.yaml (hashed)
- **Session Management**:
  - Random 64-byte session secret (auto-generated on first auth startup)
  - Session cookies with configurable max age (default 24 hours)
  - Role-based access: "admin" or "viewer"
- **Password Hash Format**: `salt_hex:sha256_hex`

### API Keys
- **Amber Electric**: Bearer token in `config.yaml` (providers.tariff.api_key)
- **No explicit API rate limiting** - relies on Amber's 50-per-5min server-side limit
- **Config validation** in schema prevents missing required keys

## Webhooks & Event Patterns

### No Explicit Webhook Integration
- System is pull-based for all external APIs
- MQTT pub/sub for local device communication
- Control loop driven by timer-based evaluation (300s default)

### Internal Event Streams
- **Server-Sent Events (SSE)**: `src/power_master/dashboard/routes/sse.py`
  - Uses sse-starlette for browser real-time updates
  - Publishes plan changes, telemetry updates, log events
  - Interval-based (5 seconds configurable via dashboard.sse_interval_seconds)

## Data Collection & History

### Load History
- **File**: `src/power_master/history/loader.py`
- **Storage**: SQLite table `historical_data`
- **Backfill**: Manual backfill from Amber historical API endpoint
- **Usage**: Load profile estimation via pattern matching

### Optimisation Plans
- **Storage**: SQLite tables `optimisation_plans`, `plan_slots`
- **Exported**: JSON via API and dashboard routes
- **Logging**: Cycle events in `optimisation_cycle_log` table

### Accounting Events
- **File**: `src/power_master/accounting/events.py`
- **Storage**: SQLite `accounting_events` table
- **Data**: Energy bought/sold at each price point, cost basis (FIFO)
- **API**: RESTful export via `src/power_master/dashboard/routes/accounting.py`

## Database Schema

### Key Tables
- `tariff_schedules` - Price slots from Amber
- `forecast_snapshots` - Multi-provider weather/solar/storm snapshots
- `optimisation_plans` - Linear programming solutions
- `plan_slots` - Individual 30-min load assignments
- `inverter_commands` - Control actions sent to Fox-ESS
- `telemetry` - Periodic hardware measurements
- `accounting_events` - Cost tracking per interval
- `scheduled_loads` - Shelly/MQTT load execution history
- `load_execution_log` - Detailed load start/stop events
- `system_events` - Errors, state transitions, alarms
- `historical_data` - Legacy load profile data
- `load_profile_estimates` - Predicted load per hour-of-day

## Configuration Management

### File-Based Config Hierarchy
1. **Defaults**: `config.defaults.yaml` (bundled, auto-copied by entrypoint)
2. **User Config**: `config.yaml` (in /data, takes precedence)
3. **Env Overrides**: Limited env var support (TZ only in Docker)
4. **Runtime Changes**: UI settings save to config.yaml via ConfigManager

### Config Validation
- Pydantic models enforce types, ranges, and required fields
- Schema versioning in database for migration tracking
- ConfigManager patches and merges user config with defaults

## Resilience & Health Checks

### Health Check Integration
- **File**: `src/power_master/resilience/health_check.py`
- **Interval**: 60 seconds (configurable)
- **Checks**:
  - Inverter connectivity (Modbus poll)
  - Tariff provider availability (sample API call)
  - Weather provider freshness (age check)
  - Database responsiveness (query test)
  - MQTT broker connection
- **Thresholds**:
  - 3 consecutive failures → degraded mode
  - 2+ hours stale forecast → fallback to defaults
  - 2 minutes stale telemetry → assume disconnected hardware

### Fallback Modes
- **File**: `src/power_master/resilience/fallback.py`
- **Degraded Mode**: Conservative optimization, no arbitrage
- **Offline Mode**: Load profile fallback, no real-time data
- **Storm Mode**: Battery reserve (80% SOC target) regardless of price
