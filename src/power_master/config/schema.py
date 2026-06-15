"""Pydantic configuration models for all system settings."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal
from zoneinfo import ZoneInfo, available_timezones

from pydantic import BaseModel, Field, field_validator, model_validator


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
    taper_start_soc: float = Field(0.90, ge=0.5, le=1.0)
    taper_factor: float = Field(0.5, ge=0.1, le=1.0)


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
    optimiser_enabled: bool = True
    horizon_hours: int = 48
    slot_duration_minutes: int = 30
    evaluation_interval_seconds: int = 300
    periodic_rebuild_interval_seconds: int = 3600
    forecast_delta_threshold_pct: float = 15.0
    soc_deviation_tolerance: float = Field(0.05, ge=0.0, le=1.0)
    soc_deviation_cooldown_seconds: int = 300
    solver_timeout_seconds: int = 25


class BatteryTargetsConfig(BaseModel):
    evening_soc_target: float = Field(0.90, ge=0.0, le=1.0)
    evening_target_hour: int = Field(16, ge=0, le=23)
    morning_soc_minimum: float = Field(0.20, ge=0.0, le=1.0)
    morning_minimum_hour: int = Field(6, ge=0, le=23)
    daytime_reserve_soc_target: float = Field(0.50, ge=0.0, le=1.0)
    daytime_reserve_start_hour: int = Field(8, ge=0, le=23)
    daytime_reserve_end_hour: int = Field(18, ge=0, le=24)
    overnight_charge_threshold_cents: int = 10
    # Force grid charge whenever buy price is at or below this value (c/kWh).
    # 0 disables the override and lets the solver decide normally.
    force_charge_below_price_cents: float = Field(0.0, ge=0.0)


class ArbitrageConfig(BaseModel):
    break_even_delta_cents: int = 5
    spike_threshold_cents: int = 100
    spike_response_mode: str = "aggressive"
    price_dampen_threshold_cents: int = 100
    price_dampen_factor: float = Field(0.5, ge=0.0, le=1.0)
    # Price colour thresholds (c/kWh).  0 = use automatic tercile bands.
    price_color_buy_low_cents: float = 0
    price_color_buy_high_cents: float = 0
    price_color_sell_low_cents: float = 0
    price_color_sell_high_cents: float = 0


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
    # Solar forecast calibration — fits a small ridge regression against recent
    # telemetry to correct systematic provider bias before the planner consumes it.
    calibration_enabled: bool = False
    calibration_window_days: int = Field(21, ge=3, le=90)
    calibration_refit_interval_seconds: int = Field(3600, ge=60)


class StormProviderConfig(BaseModel):
    type: str = "bom"
    state_code: str = "IDQ11295"
    location_aac: str = ""
    warning_product_ids: list[str] = Field(
        default_factory=lambda: ["IDQ21033", "IDQ21035", "IDQ21037", "IDQ21038"]
    )
    update_interval_seconds: int = 21600
    validity_seconds: int = 21600


# ============================================================================
# TOU Tariff DSL Models (per plan §3.1)
# ============================================================================

class BandBase(BaseModel):
    """Base band definition shared by import and free windows."""
    descriptor: str = Field(description="Band name/identifier (e.g., 'peak', 'off-peak')")
    windows: list[str] = Field(
        default_factory=list,
        description="List of time windows in HH:MM-HH:MM format. Empty list = default/shoulder band."
    )
    rate_c_per_kwh: float = Field(description="Rate in cents per kWh")

    @field_validator("windows")
    @classmethod
    def validate_windows(cls, v: list[str]) -> list[str]:
        """Validate window format HH:MM-HH:MM with valid hour/minute ranges.

        Midnight-crossing windows (e.g., 22:00-07:00) are allowed.
        """
        for window in v:
            if not isinstance(window, str):
                raise ValueError(f"Window must be string, got {type(window)}")
            if not re.match(r"^\d{2}:\d{2}-\d{2}:\d{2}$", window):
                raise ValueError(f"Window must match HH:MM-HH:MM format, got '{window}'")
            start_str, end_str = window.split("-")
            start_h, start_m = map(int, start_str.split(":"))
            end_h, end_m = map(int, end_str.split(":"))

            if not (0 <= start_h <= 23) or not (0 <= start_m <= 59):
                raise ValueError(f"Invalid start time in window '{window}': {start_str}")
            if not (0 <= end_h <= 23) or not (0 <= end_m <= 59):
                raise ValueError(f"Invalid end time in window '{window}': {end_str}")

        return v


class FreeWindowConfig(BaseModel):
    """Free/subsidised import window (capped per day)."""
    name: str = Field(description="Window name (e.g., 'four4free')")
    windows: list[str] = Field(description="Time windows in HH:MM-HH:MM format")
    rate_c_per_kwh: float = Field(description="Rate (typically 0.0 for free)")
    cap_kwh_per_day: float = Field(description="Daily cap in kWh (resets at local midnight)")
    applies_to_channel: str = Field(
        default="general",
        description="Channel this applies to (e.g., 'general', 'controlled_load')"
    )
    over_cap_falls_back_to: str = Field(
        description="Band descriptor to use when daily cap is exhausted"
    )

    @field_validator("windows")
    @classmethod
    def validate_windows(cls, v: list[str]) -> list[str]:
        """Validate window format; delegate to BandBase validator."""
        BandBase.validate_windows(v)
        return v


class FeedInTier(BaseModel):
    """Per-day volume tier for feed-in (export) rates."""
    up_to_kwh_per_day: float | None = Field(
        default=None,
        description="Tier cap in kWh/day; None = open-ended (must be last tier)"
    )
    rate_c_per_kwh: float = Field(description="Rate in cents per kWh for this tier")


class FeedInBand(BaseModel):
    """Feed-in (export) band with optional volume tiers."""
    name: str = Field(description="Band name (e.g., 'evening-premium')")
    windows: list[str] = Field(
        default_factory=list,
        description="Time windows in HH:MM-HH:MM format. Empty = applies to all times (fallback)."
    )
    # Support both flat rate (rate_c_per_kwh) and tiered (tiers) shapes
    tiers: list[FeedInTier] = Field(
        default_factory=list,
        description="Volume tiers (mutually exclusive with rate_c_per_kwh)"
    )
    rate_c_per_kwh: float | None = Field(
        default=None,
        description="Flat rate (if tiers not used). Mutually exclusive with tiers."
    )

    @field_validator("windows")
    @classmethod
    def validate_windows(cls, v: list[str]) -> list[str]:
        """Validate window format."""
        BandBase.validate_windows(v)
        return v

    @model_validator(mode="after")
    def validate_shape_and_tiers(self) -> FeedInBand:
        """Ensure either tiers or flat rate is set (not both), and tiers are ordered."""
        # Check that exactly one of tiers or rate_c_per_kwh is set
        has_tiers = len(self.tiers) > 0
        has_flat_rate = self.rate_c_per_kwh is not None

        if has_tiers and has_flat_rate:
            raise ValueError(
                f"FeedInBand '{self.name}': cannot specify both 'tiers' and 'rate_c_per_kwh'"
            )
        if not has_tiers and not has_flat_rate:
            raise ValueError(
                f"FeedInBand '{self.name}': must specify either 'tiers' or 'rate_c_per_kwh'"
            )

        # Validate tier ordering: non-null up_to_kwh_per_day must be ascending
        # and at most one null (open-ended) tier, which must be last
        non_null_tiers = [(t.up_to_kwh_per_day, i) for i, t in enumerate(self.tiers) if t.up_to_kwh_per_day is not None]
        null_tiers = [i for i, t in enumerate(self.tiers) if t.up_to_kwh_per_day is None]

        if len(null_tiers) > 1:
            raise ValueError(
                f"FeedInBand '{self.name}': at most one tier with null up_to_kwh_per_day allowed"
            )

        if null_tiers and non_null_tiers:
            last_non_null_idx = non_null_tiers[-1][1]
            null_idx = null_tiers[0]
            if last_non_null_idx > null_idx:
                raise ValueError(
                    f"FeedInBand '{self.name}': null up_to_kwh_per_day tier must come last"
                )

        # Check ascending order of non-null caps
        for i in range(len(non_null_tiers) - 1):
            curr_cap, curr_idx = non_null_tiers[i]
            next_cap, next_idx = non_null_tiers[i + 1]
            if curr_cap >= next_cap:
                raise ValueError(
                    f"FeedInBand '{self.name}': tier caps must be strictly ascending, "
                    f"but tier {curr_idx} ({curr_cap} kWh) >= tier {next_idx} ({next_cap} kWh)"
                )

        return self


class CreditConfig(BaseModel):
    """Conditional daily reward (e.g., ZEROHERO evening low-import credit)."""
    name: str = Field(description="Credit name (e.g., 'zerohero-evening')")
    type: str = Field(description="Credit type (e.g., 'low_import_window')")
    windows: list[str] = Field(description="Time windows in HH:MM-HH:MM format")
    max_import_kwh_per_hour: float = Field(
        description="Max hourly import threshold to earn the credit"
    )
    reward_dollars_per_day: float = Field(description="Daily reward in dollars if earned")
    enforcement: str = Field(
        default="soft",
        description="'soft' (penalty) or 'hard' (constraint+slack)"
    )
    credit_priority_weight: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Weight [0,1] to trade credit vs. other objectives (e.g., export revenue)"
    )

    @field_validator("windows")
    @classmethod
    def validate_windows(cls, v: list[str]) -> list[str]:
        """Validate window format."""
        BandBase.validate_windows(v)
        return v

    @field_validator("enforcement")
    @classmethod
    def validate_enforcement(cls, v: str) -> str:
        """Validate enforcement mode."""
        if v not in ("soft", "hard"):
            raise ValueError(f"enforcement must be 'soft' or 'hard', got '{v}'")
        return v


class BillingCycleConfig(BaseModel):
    """Billing cycle metadata (for supply charge and reporting; free allowance is per-DAY)."""
    length_days: int = Field(description="Cycle length in days")
    anchor_date: date = Field(description="Cycle start reference (YYYY-MM-DD)")


class VPPConfig(BaseModel):
    """VPP seam (stub only; no active logic this milestone)."""
    enabled: bool = Field(
        default=False,
        description="Future: yield control / accept event pricing (OpenADR-style)"
    )


class TariffVersion(BaseModel):
    """Versioned tariff definition (valid between dates)."""
    valid_from: date = Field(description="Version effective date (YYYY-MM-DD)")
    valid_until: date | None = Field(
        default=None,
        description="Version end date (None = open-ended, superseded by next version)"
    )
    import_bands: list[BandBase] = Field(
        description="Import bands; one with empty windows = default/shoulder band"
    )
    free_windows: list[FreeWindowConfig] = Field(
        default_factory=list,
        description="Free/subsidised import windows with daily caps"
    )
    feed_in_bands: list[FeedInBand] = Field(
        default_factory=list,
        description="Export bands with optional volume tiers"
    )
    credits: list[CreditConfig] = Field(
        default_factory=list,
        description="Conditional daily rewards"
    )

    @model_validator(mode="after")
    def validate_version(self) -> TariffVersion:
        """Validate version: must have at least one import band (incl. default)."""
        if not self.import_bands:
            raise ValueError(
                f"TariffVersion valid_from={self.valid_from}: must have at least one import_band"
            )

        # Check that there is at least one default band (with no windows)
        default_bands = [b for b in self.import_bands if not b.windows]
        if not default_bands:
            raise ValueError(
                f"TariffVersion valid_from={self.valid_from}: must have at least one "
                "import_band with no windows (default/shoulder band)"
            )

        # Validate band name references in free_windows
        import_band_descriptors = {b.descriptor for b in self.import_bands}
        for fw in self.free_windows:
            if fw.over_cap_falls_back_to not in import_band_descriptors:
                raise ValueError(
                    f"TariffVersion valid_from={self.valid_from}, "
                    f"free_window '{fw.name}': over_cap_falls_back_to '{fw.over_cap_falls_back_to}' "
                    f"does not match any import_band descriptor. Available: {import_band_descriptors}"
                )

        return self


class TariffPlanConfig(BaseModel):
    """TOU tariff plan definition (selected via providers.tariff.type: 'tou')."""
    versions: list[TariffVersion] = Field(
        description="Versioned definitions ordered by valid_from date"
    )
    billing_cycle: BillingCycleConfig = Field(
        description="Billing cycle for supply charge / reporting"
    )
    vpp: VPPConfig = Field(
        default_factory=VPPConfig,
        description="VPP seam (stub only)"
    )
    supply_charge_c_per_day: float = Field(
        description="Daily supply charge in cents"
    )

    @model_validator(mode="after")
    def validate_plan(self) -> TariffPlanConfig:
        """Validate plan: must have at least one version."""
        if not self.versions:
            raise ValueError("TariffPlanConfig: must have at least one version")
        return self


class TariffProviderConfig(BaseModel):
    """Tariff provider configuration (supports 'amber' legacy path and new 'tou')."""
    type: str = Field(
        default="amber",
        description="'amber' (legacy spot pricing) | 'tou' (TOU tariff from DSL) | future types"
    )
    # Amber legacy fields (kept for backward compatibility)
    api_key: str = Field(default="", description="Amber API key (for type='amber')")
    site_id: str = Field(default="", description="Amber site ID (for type='amber')")
    update_interval_seconds: int = Field(default=300, description="Update interval in seconds")
    validity_seconds: int = Field(default=300, description="Data validity in seconds")
    max_requests_per_5min: int = Field(default=50, description="Rate limit for Amber API")

    # TOU tariff fields
    timezone: str | None = Field(
        default=None,
        description="IANA timezone (REQUIRED when type='tou'). E.g., 'Australia/Brisbane'"
    )
    plan: TariffPlanConfig | None = Field(
        default=None,
        description="TOU tariff plan DSL (REQUIRED when type='tou')"
    )

    # Grid charge policy (universal knob per plan §5.6)
    # Default is None, resolved in model_validator based on type:
    #   - type="tou" -> "free_window_and_solar_only" (no panic-import)
    #   - type="amber" (or other) -> "allow_arbitrage" (legacy behaviour)
    # User can always set explicitly to override.
    grid_charge_policy: str | None = Field(
        default=None,
        description="'free_window_and_solar_only' (no panic-import, for TOU) | 'allow_arbitrage' (legacy Amber behaviour) | None (auto-resolved)"
    )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str | None, info) -> str | None:
        """Validate timezone is a valid IANA zone (required when type='tou')."""
        data = info.data
        if data.get("type") == "tou" and not v:
            raise ValueError(
                "timezone is REQUIRED when type='tou' (e.g., 'Australia/Brisbane')"
            )

        if v is not None:
            # Check if it's a valid IANA timezone
            try:
                ZoneInfo(v)
            except KeyError:
                raise ValueError(
                    f"Invalid IANA timezone: '{v}'. "
                    f"Must be a valid timezone name (e.g., 'Australia/Brisbane')"
                )

        return v

    @model_validator(mode="after")
    def validate_tou_requirements(self) -> TariffProviderConfig:
        """Validate TOU-specific requirements and resolve grid_charge_policy default."""
        if self.type == "tou":
            if not self.plan:
                raise ValueError(
                    "plan is REQUIRED when type='tou'"
                )
            if not self.timezone:
                raise ValueError(
                    "timezone is REQUIRED when type='tou'"
                )

        # Resolve grid_charge_policy: None -> type-dependent default, else validate explicit value
        if self.grid_charge_policy is None:
            # Safe default for TOU: no panic-import (free window + solar only).
            # Legacy default for Amber: allow arbitrage (for backward compatibility).
            self.grid_charge_policy = (
                "free_window_and_solar_only" if self.type == "tou" else "allow_arbitrage"
            )
        else:
            # Explicit value: validate it's in the allowed set (fail-loud)
            if self.grid_charge_policy not in ("free_window_and_solar_only", "allow_arbitrage"):
                raise ValueError(
                    f"grid_charge_policy must be 'free_window_and_solar_only' or 'allow_arbitrage', "
                    f"got '{self.grid_charge_policy}'"
                )

        return self


class ProvidersConfig(BaseModel):
    weather: WeatherProviderConfig = WeatherProviderConfig()
    solar: SolarProviderConfig = SolarProviderConfig()
    storm: StormProviderConfig = StormProviderConfig()
    tariff: TariffProviderConfig = TariffProviderConfig()
    # Forecast persistence — stores per-horizon forecast samples so we can
    # measure accuracy and feed calibration.
    forecast_persistence_enabled: bool = True
    forecast_retention_days: int = Field(365, ge=7, le=1825)
    forecast_horizons_hours: list[float] = Field(
        default_factory=lambda: [1.0, 4.0, 10.0, 18.0, 24.0]
    )


class FoxESSConfig(BaseModel):
    connection_type: Literal["tcp", "rtu"] = "tcp"
    # TCP settings
    host: str = "192.168.1.100"
    port: int = 502
    # RTU (serial) settings
    serial_port: str = "/dev/ttyUSB0"
    baudrate: int = 9600
    # Shared
    unit_id: int = 247
    poll_interval_seconds: int = 15
    watchdog_timeout_seconds: int = 3600  # Remote control watchdog (seconds); must exceed tick interval
    remote_refresh_interval_seconds: int = 20  # Re-send active command this often to keep inverter in remote mode


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
    session_max_age_seconds: int = 2592000  # 30 days


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


class NotificationChannelConfig(BaseModel):
    """Base fields shared by all notification channels."""
    enabled: bool = False


class TelegramChannelConfig(NotificationChannelConfig):
    bot_token: str = ""
    chat_id: str = ""


class EmailChannelConfig(NotificationChannelConfig):
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    use_tls: bool = True
    from_address: str = ""
    to_address: str = ""


class PushoverChannelConfig(NotificationChannelConfig):
    api_token: str = ""
    user_key: str = ""


class NtfyChannelConfig(NotificationChannelConfig):
    server_url: str = "https://ntfy.sh"
    topic: str = ""
    token: str = ""


class WebhookChannelConfig(NotificationChannelConfig):
    url: str = ""
    method: str = "POST"
    headers: dict[str, str] = Field(default_factory=dict)


class NotificationChannelsConfig(BaseModel):
    telegram: TelegramChannelConfig = TelegramChannelConfig()
    email: EmailChannelConfig = EmailChannelConfig()
    pushover: PushoverChannelConfig = PushoverChannelConfig()
    ntfy: NtfyChannelConfig = NtfyChannelConfig()
    webhook: WebhookChannelConfig = WebhookChannelConfig()


class NotificationEventConfig(BaseModel):
    """Per-event notification rule."""
    enabled: bool = True
    severity: Literal["info", "warning", "critical"] = "warning"
    cooldown_seconds: int = 3600


class NotificationEventsConfig(BaseModel):
    # Exceptional events — on by default.  Fire when something abnormal
    # happens; each carries an Action describing the system's response.
    price_spike: NotificationEventConfig = NotificationEventConfig(
        severity="critical", cooldown_seconds=300, enabled=True,
    )
    inverter_offline: NotificationEventConfig = NotificationEventConfig(
        severity="critical", cooldown_seconds=600, enabled=True,
    )
    resilience_degraded: NotificationEventConfig = NotificationEventConfig(
        severity="warning", cooldown_seconds=600, enabled=True,
    )
    storm_plan_active: NotificationEventConfig = NotificationEventConfig(
        severity="warning", cooldown_seconds=900, enabled=True,
    )
    grid_outage: NotificationEventConfig = NotificationEventConfig(
        severity="critical", cooldown_seconds=600, enabled=True,
    )
    force_charge_triggered: NotificationEventConfig = NotificationEventConfig(
        severity="info", cooldown_seconds=1800, enabled=True,
    )
    # Resolution / closing events — on by default so incidents get closed out.
    price_spike_end: NotificationEventConfig = NotificationEventConfig(
        severity="info", cooldown_seconds=60, enabled=True,
    )
    storm_resolved: NotificationEventConfig = NotificationEventConfig(
        severity="info", cooldown_seconds=60, enabled=True,
    )
    grid_outage_resolved: NotificationEventConfig = NotificationEventConfig(
        severity="info", cooldown_seconds=60, enabled=True,
    )
    # Routine events — off by default.  Flip on only if you specifically
    # want the noise.
    battery_low: NotificationEventConfig = NotificationEventConfig(
        severity="warning", cooldown_seconds=3600, enabled=False,
    )
    battery_full: NotificationEventConfig = NotificationEventConfig(
        severity="info", cooldown_seconds=3600, enabled=False,
    )
    inverter_online: NotificationEventConfig = NotificationEventConfig(
        severity="info", cooldown_seconds=60, enabled=False,
    )
    resilience_recovered: NotificationEventConfig = NotificationEventConfig(
        severity="info", cooldown_seconds=60, enabled=False,
    )
    # log_error is disabled by default but the log handler filters at
    # log_min_level (CRITICAL by default) so silent failures still surface
    # iff they reach CRITICAL severity.
    log_error: NotificationEventConfig = NotificationEventConfig(
        severity="warning", cooldown_seconds=300, enabled=False,
    )
    # Daily briefing digest — off by default.
    daily_briefing: NotificationEventConfig = NotificationEventConfig(
        severity="info", cooldown_seconds=60, enabled=False,
    )


class NotificationsConfig(BaseModel):
    enabled: bool = False
    battery_low_threshold: float = Field(0.10, ge=0.0, le=1.0)
    battery_full_threshold: float = Field(0.95, ge=0.0, le=1.0)
    # Minimum log level that will reach the event bus (separate from
    # the `log_error` event rule).  Set to CRITICAL by default so routine
    # errors are logged but not notified; CRITICAL-level failures always
    # surface even when the `log_error` event rule is off.
    log_min_level: str = "CRITICAL"
    # Daily briefing configuration
    daily_briefing_enabled: bool = False
    daily_briefing_hour_local: int = Field(7, ge=0, le=23)
    # Persistence retention (days) for notification_log rows
    notification_retention_days: int = Field(90, ge=1, le=3650)
    channels: NotificationChannelsConfig = NotificationChannelsConfig()
    events: NotificationEventsConfig = NotificationEventsConfig()


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "json"
    file: str = ""


class DBConfig(BaseModel):
    path: str = "power_master.db"


class AppConfig(BaseModel):
    """Root configuration model containing all system settings."""

    setup_completed: bool = False  # Set True after initial setup wizard completes
    auto_update_stable: bool = False  # Automatically update when a stable release is detected
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
    notifications: NotificationsConfig = NotificationsConfig()
    logging: LoggingConfig = LoggingConfig()
    db: DBConfig = DBConfig()
