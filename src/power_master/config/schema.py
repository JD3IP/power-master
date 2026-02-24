"""Pydantic configuration models for all system settings."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BatteryConfig(BaseModel):
    capacity_wh: int = 10000
    max_charge_rate_w: int = 5000
    max_discharge_rate_w: int = 5000
    max_grid_import_w: int = 0  # 0 = no limit; max watts from grid before load shedding
    soc_min_hard: float = Field(0.05, ge=0.0, le=1.0)
    soc_max_hard: float = Field(0.95, ge=0.0, le=1.0)
    soc_min_soft: float = Field(0.10, ge=0.0, le=1.0)
    soc_max_soft: float = Field(0.90, ge=0.0, le=1.0)
    round_trip_efficiency: float = Field(0.90, ge=0.0, le=1.0)


class LoadProfileConfig(BaseModel):
    """Default load profile in 4-hour blocks (watts).

    Used as fallback when insufficient historical data for prediction.
    Blocks: 00-04 (overnight), 04-08 (morning), 08-12 (mid-morning),
    12-16 (afternoon), 16-20 (evening peak), 20-24 (night).
    """
    block_00_04_w: int = 500
    block_04_08_w: int = 800
    block_08_12_w: int = 1200
    block_12_16_w: int = 1500
    block_16_20_w: int = 2500
    block_20_24_w: int = 1500
    min_history_records: int = 48  # Minimum records to trust historic prediction
    timezone: str = "Australia/Brisbane"  # IANA tz for local load profile mapping

    def get_for_hour(self, hour: int) -> float:
        """Return configured load watts for a given hour of day."""
        if hour < 4:
            return float(self.block_00_04_w)
        elif hour < 8:
            return float(self.block_04_08_w)
        elif hour < 12:
            return float(self.block_08_12_w)
        elif hour < 16:
            return float(self.block_12_16_w)
        elif hour < 20:
            return float(self.block_16_20_w)
        else:
            return float(self.block_20_24_w)


class PlanningConfig(BaseModel):
    horizon_hours: int = 48
    slot_duration_minutes: int = 30
    evaluation_interval_seconds: int = 300
    periodic_rebuild_interval_seconds: int = 3600
    forecast_delta_threshold_pct: float = 15.0
    soc_deviation_tolerance: float = Field(0.10, ge=0.0, le=1.0)
    solver_timeout_seconds: int = 25


class BatteryTargetsConfig(BaseModel):
    evening_soc_target: float = Field(0.90, ge=0.0, le=1.0)
    evening_target_hour: int = Field(16, ge=0, le=23)
    morning_soc_minimum: float = Field(0.20, ge=0.0, le=1.0)
    morning_minimum_hour: int = Field(6, ge=0, le=23)
    overnight_charge_threshold_cents: int = 10


class ArbitrageConfig(BaseModel):
    break_even_delta_cents: int = 5
    spike_threshold_cents: int = 100
    spike_response_mode: str = "aggressive"
    price_dampen_threshold_cents: int = 100
    price_dampen_factor: float = Field(0.5, ge=0.0, le=1.0)


class FixedCostsConfig(BaseModel):
    monthly_supply_charge_cents: int = 9000
    daily_access_fee_cents: int = 100
    hedging_per_kwh_cents: int = 2


class AntiOscillationConfig(BaseModel):
    min_command_duration_seconds: int = 300
    hysteresis_band: float = Field(0.05, ge=0.0, le=0.5)
    rate_limit_window_seconds: int = 900
    max_commands_per_window: int = 3


class StormConfig(BaseModel):
    enabled: bool = True
    reserve_soc_target: float = Field(0.80, ge=0.0, le=1.0)
    probability_threshold: float = Field(0.70, ge=0.0, le=1.0)
    horizon_hours: int = 24


class WeatherProviderConfig(BaseModel):
    type: str = "openmeteo"
    update_interval_seconds: int = 3600
    validity_seconds: int = 3600
    latitude: float = -27.4698
    longitude: float = 153.0251


class SolarProviderConfig(BaseModel):
    latitude: float = -27.4698
    longitude: float = 153.0251
    declination: float = 20.0
    azimuth: float | None = None
    kwp: float = 5.0
    timezone: str = "Australia/Brisbane"
    update_interval_seconds: int = 21600
    validity_seconds: int = 21600
    system_size_kw: float = 0.0  # 0 = no fallback; set to PV size for bell curve fallback


class StormProviderConfig(BaseModel):
    type: str = "bom"
    state_code: str = "IDQ11295"
    location_aac: str = ""
    warning_product_ids: list[str] = Field(
        default_factory=lambda: ["IDQ21033", "IDQ21035", "IDQ21037", "IDQ21038"]
    )
    update_interval_seconds: int = 21600
    validity_seconds: int = 21600


class TariffProviderConfig(BaseModel):
    type: str = "amber"
    api_key: str = ""
    site_id: str = ""
    update_interval_seconds: int = 300
    validity_seconds: int = 300
    max_requests_per_5min: int = 50


class ProvidersConfig(BaseModel):
    weather: WeatherProviderConfig = WeatherProviderConfig()
    solar: SolarProviderConfig = SolarProviderConfig()
    storm: StormProviderConfig = StormProviderConfig()
    tariff: TariffProviderConfig = TariffProviderConfig()


class FoxESSConfig(BaseModel):
    host: str = "192.168.1.100"
    port: int = 502
    unit_id: int = 247
    poll_interval_seconds: int = 15
    watchdog_timeout_seconds: int = 3600  # Remote control watchdog (seconds); must exceed tick interval


class HardwareConfig(BaseModel):
    adapter: str = "foxess"
    foxess: FoxESSConfig = FoxESSConfig()


class ShellyDeviceConfig(BaseModel):
    name: str
    host: str
    relay_id: int = 0
    power_w: int
    priority_class: int = 5
    enabled: bool = True
    earliest_start: str = "00:00"
    latest_end: str = "23:59"
    duration_minutes: int = 60
    prefer_solar: bool = True
    days_of_week: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    min_runtime_minutes: int = 0
    ideal_runtime_minutes: int = 0  # 0 = use min_runtime_minutes (or default runtime)
    max_runtime_minutes: int = 0  # 0 = no max cap
    allow_split_shifts: bool = False
    completion_deadline: str = ""  # HH:MM format, empty = no deadline


class MQTTLoadEndpointConfig(BaseModel):
    name: str
    command_topic: str
    state_topic: str
    power_w: int
    priority_class: int = 5
    enabled: bool = True
    earliest_start: str = "00:00"
    latest_end: str = "23:59"
    duration_minutes: int = 60
    prefer_solar: bool = True
    days_of_week: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    min_runtime_minutes: int = 0
    ideal_runtime_minutes: int = 0  # 0 = use min_runtime_minutes (or default runtime)
    max_runtime_minutes: int = 0  # 0 = no max cap
    allow_split_shifts: bool = False
    completion_deadline: str = ""  # HH:MM format, empty = no deadline


class LoadsConfig(BaseModel):
    shelly_devices: list[ShellyDeviceConfig] = Field(default_factory=list)
    mqtt_load_endpoints: list[MQTTLoadEndpointConfig] = Field(default_factory=list)


class MQTTConfig(BaseModel):
    enabled: bool = True
    broker_host: str = "localhost"
    broker_port: int = 1883
    username: str = ""
    password: str = ""
    topic_prefix: str = "power_master"
    publish_interval_seconds: int = 5
    ha_discovery_enabled: bool = True
    ha_discovery_prefix: str = "homeassistant"


class UserConfig(BaseModel):
    username: str
    password_hash: str  # format: "salt_hex:sha256_hex"
    role: str = "viewer"  # "admin" or "viewer"
    enabled: bool = True


class AuthConfig(BaseModel):
    users: list[UserConfig] = Field(default_factory=list)  # Empty = auth disabled
    session_secret: str = ""  # Auto-generated on first authenticated startup
    session_max_age_seconds: int = 86400  # 24 hours


class DashboardConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    sse_interval_seconds: int = 5
    rolling_chart_power_max_kw: float = 20.0
    rolling_chart_window_hours: int = 12
    auth: AuthConfig = AuthConfig()


class ResilienceConfig(BaseModel):
    health_check_interval_seconds: int = 60
    max_consecutive_failures: int = 3
    stale_forecast_max_age_seconds: int = 7200
    degraded_safety_margin: float = 0.05
    stale_telemetry_max_age_seconds: int = 120


class AccountingConfig(BaseModel):
    billing_cycle_day: int = Field(1, ge=1, le=28)
    currency_code: str = "AUD"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "json"
    file: str = ""


class DBConfig(BaseModel):
    path: str = "power_master.db"


class AppConfig(BaseModel):
    """Root configuration model containing all system settings."""

    battery: BatteryConfig = BatteryConfig()
    load_profile: LoadProfileConfig = LoadProfileConfig()
    planning: PlanningConfig = PlanningConfig()
    battery_targets: BatteryTargetsConfig = BatteryTargetsConfig()
    arbitrage: ArbitrageConfig = ArbitrageConfig()
    fixed_costs: FixedCostsConfig = FixedCostsConfig()
    anti_oscillation: AntiOscillationConfig = AntiOscillationConfig()
    storm: StormConfig = StormConfig()
    providers: ProvidersConfig = ProvidersConfig()
    hardware: HardwareConfig = HardwareConfig()
    loads: LoadsConfig = LoadsConfig()
    mqtt: MQTTConfig = MQTTConfig()
    dashboard: DashboardConfig = DashboardConfig()
    resilience: ResilienceConfig = ResilienceConfig()
    accounting: AccountingConfig = AccountingConfig()
    logging: LoggingConfig = LoggingConfig()
    db: DBConfig = DBConfig()
