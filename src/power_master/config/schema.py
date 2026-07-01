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
    rebuild_on_forecast_staleness: bool | None = Field(
        default=None,
        description="Rebuild the plan when forecast data is stale (age-based). "
        "Default: True for dynamic-pricing (amber), False for TOU (solar 3-6h cadence is expected; "
        "re-solving stale data churns)."
    )
    rebuild_on_actuals_deviation: bool | None = Field(
        default=None,
        description="Full-rebuild when solar/load actuals deviate from forecast. "
        "Default: True for amber, False for TOU (routine load/consumption changes are absorbed by "
        "following the cached plan; soc_deviation remains the safety net)."
    )
    mode_switch_hysteresis_cents: float | None = Field(
        default=None,
        ge=0,
        description="Margin (cents/kWh) the alternative battery mode must beat the currently-committed "
        "mode by before the plan is allowed to flip it. Status-quo tie-break against near-degenerate "
        "solver optima. Default: 3.0 for TOU, 0.0 (off) for amber. 0 disables."
    )


class BatteryTargetsConfig(BaseModel):
    evening_soc_target: float = Field(0.90, ge=0.0, le=1.0)
    evening_target_hour: int = Field(16, ge=0, le=23)
    # Free-window fill: during 0c/free import windows the battery should top up
    # as full as the hardware allows (free energy is worth grabbing), rather than
    # stopping at evening_soc_target. This is the SOC the solver aims for by the
    # end of each free window; it is clamped to battery.soc_max_hard. Set to 0 to
    # disable free-window fill and let the evening target cap charging as before.
    free_window_soc_target: float = Field(1.0, ge=0.0, le=1.0)
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
    # Gate policy (§R2): how to apply the arbitrage gate.
    # - "spot" (default): for Amber/spot providers — block exports when export_rate < wacb + delta.
    # - "tou_aware": for TOU providers — disable the WACB-vs-export gate so economically-correct TOU
    #   exports (tiered peaks, fixed-rate FiT windows, credit-contribution exports) are not suppressed
    #   when WACB drifts high (e.g. after grid charge history). The fixed-rate price is deterministic
    #   and known to be good; the gate's protective "don't export when batteries are expensive" intent
    #   is not needed for planned, guaranteed-value exports.
    gate_policy: str = Field(default="spot", pattern="^(spot|tou_aware)$")

    @field_validator("gate_policy")
    @classmethod
    def validate_gate_policy(cls, v: str) -> str:
        """Ensure gate_policy is one of the allowed values."""
        if v not in ("spot", "tou_aware"):
            raise ValueError(
                f"gate_policy must be 'spot' (Amber/spot default) or 'tou_aware' (TOU default), "
                f"got '{v}'"
            )
        return v


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


def _windows_overlap(window1: str, window2: str) -> bool:
    """Check if two time windows overlap, handling midnight-crossing windows.

    Window format: "HH:MM-HH:MM"

    Midnight-crossing windows (e.g., "22:00-07:00") are handled correctly by checking
    if the ranges overlap in a circular 24-hour timeline.

    Args:
        window1: First window in HH:MM-HH:MM format
        window2: Second window in HH:MM-HH:MM format

    Returns:
        True if the windows overlap, False otherwise.
    """
    def parse_window(w: str) -> tuple[int, int, int, int]:
        """Parse window string to (start_h, start_m, end_h, end_m)."""
        start_str, end_str = w.split("-")
        start_h, start_m = map(int, start_str.split(":"))
        end_h, end_m = map(int, end_str.split(":"))
        return start_h, start_m, end_h, end_m

    def time_to_minutes(h: int, m: int) -> int:
        """Convert time to minutes since midnight."""
        return h * 60 + m

    s1_h, s1_m, e1_h, e1_m = parse_window(window1)
    s2_h, s2_m, e2_h, e2_m = parse_window(window2)

    s1 = time_to_minutes(s1_h, s1_m)
    e1 = time_to_minutes(e1_h, e1_m)
    s2 = time_to_minutes(s2_h, s2_m)
    e2 = time_to_minutes(e2_h, e2_m)

    # Expand each window into one or two half-open [start, end) minute intervals,
    # splitting midnight-crossing windows (start >= end, e.g. "22:00-07:00") into
    # [start, 1440) and [0, end). This matches the band engine's half-open semantics
    # (start <= t < end), so windows that merely touch at a boundary (e.g. 10:00-14:00
    # and 14:00-18:00) are NOT treated as overlapping.
    def intervals(start: int, end: int) -> list[tuple[int, int]]:
        if start < end:
            return [(start, end)]
        # start >= end: crosses midnight (start == end degenerates to a full day)
        return [(start, 1440), (0, end)]

    for a_start, a_end in intervals(s1, e1):
        for b_start, b_end in intervals(s2, e2):
            if a_start < b_end and b_start < a_end:
                return True
    return False


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
    """Versioned tariff definition (valid between dates).

    Boundary semantics:
    - valid_from: INCLUSIVE — version is active from this date (local midnight onwards)
    - valid_until: INCLUSIVE — version is active through end-of-day (23:59:59) on this date.
      Next version's valid_from must be the following day or later.
      None = open-ended; version remains active until superseded.
    """
    valid_from: date = Field(description="Version effective date (YYYY-MM-DD); INCLUSIVE")
    valid_until: date | None = Field(
        default=None,
        description="Version end date (YYYY-MM-DD); INCLUSIVE (active through end of this date). "
                    "None = open-ended until superseded. Must be >= valid_from."
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
        """Validate version: must have exactly one import band with empty windows (default),
        and valid_until >= valid_from if specified.
        """
        if not self.import_bands:
            raise ValueError(
                f"TariffVersion valid_from={self.valid_from}: must have at least one import_band"
            )

        # Check that there is EXACTLY ONE default band (with no windows)
        default_bands = [b for b in self.import_bands if not b.windows]
        if len(default_bands) != 1:
            raise ValueError(
                f"TariffVersion valid_from={self.valid_from}: exactly one import_band must have "
                f"empty windows (the default/shoulder band); found {len(default_bands)}"
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

        # Validate valid_until >= valid_from
        if self.valid_until is not None and self.valid_until < self.valid_from:
            raise ValueError(
                f"TariffVersion: valid_until ({self.valid_until}) cannot be before "
                f"valid_from ({self.valid_from})"
            )

        # Validate free_windows: no two free_windows may have overlapping time ranges
        if len(self.free_windows) > 1:
            for i in range(len(self.free_windows)):
                for j in range(i + 1, len(self.free_windows)):
                    fw_a = self.free_windows[i]
                    fw_b = self.free_windows[j]
                    # Check if any window in fw_a overlaps with any window in fw_b
                    for w_a in fw_a.windows:
                        for w_b in fw_b.windows:
                            if _windows_overlap(w_a, w_b):
                                raise ValueError(
                                    f"TariffVersion valid_from={self.valid_from}: "
                                    f"free_windows '{fw_a.name}' and '{fw_b.name}' have "
                                    f"overlapping time ranges ('{w_a}' overlaps with '{w_b}')"
                                )

        return self


class TariffPlanConfig(BaseModel):
    """TOU tariff plan definition (selected via providers.tariff.type: 'tou').

    The version chain is validated for:
    - No overlaps: two versions both active on the same date.
    - No gaps: a date between the earliest valid_from and the latest bound that
      no version covers (only checks between versions; doesn't require coverage
      before the first version or after an open-ended last version).
    - Open-ended semantics: a version with valid_until=None must be the last
      (by date) in the chain.
    """
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
        """Validate plan: must have at least one version and no chain conflicts."""
        if not self.versions:
            raise ValueError("TariffPlanConfig: must have at least one version")

        # Sort by valid_from for chain analysis
        sorted_versions = sorted(self.versions, key=lambda v: v.valid_from)

        # Check for overlap: two versions both active on the same date
        for i in range(len(sorted_versions) - 1):
            v_curr = sorted_versions[i]
            v_next = sorted_versions[i + 1]

            # v_curr covers [valid_from, valid_until or infinity)
            # v_next covers [valid_from, valid_until or infinity)
            # They overlap if v_next.valid_from <= v_curr.valid_until
            if v_curr.valid_until is not None and v_next.valid_from <= v_curr.valid_until:
                raise ValueError(
                    f"TariffPlanConfig: version overlap detected. "
                    f"Version 1 (valid_from={v_curr.valid_from}, valid_until={v_curr.valid_until}) "
                    f"overlaps with version 2 (valid_from={v_next.valid_from}, "
                    f"valid_until={v_next.valid_until}). "
                    f"If v_next starts on or before v_curr.valid_until, they overlap."
                )

        # Check for gaps: a date between versions that no version covers
        for i in range(len(sorted_versions) - 1):
            v_curr = sorted_versions[i]
            v_next = sorted_versions[i + 1]

            # A gap exists if v_curr.valid_until < v_next.valid_from - 1 day
            # (i.e., there is at least one day in between)
            if v_curr.valid_until is not None:
                from datetime import timedelta
                day_after_curr_until = v_curr.valid_until + timedelta(days=1)
                if day_after_curr_until < v_next.valid_from:
                    raise ValueError(
                        f"TariffPlanConfig: version gap detected. "
                        f"Version 1 ends on {v_curr.valid_until}, "
                        f"but version 2 doesn't start until {v_next.valid_from}. "
                        f"Gap on {day_after_curr_until} to "
                        f"{v_next.valid_from - timedelta(days=1)}."
                    )

        # Check for open-ended version not being last
        # A version with valid_until=None must be the final/current version by date
        for i, version in enumerate(sorted_versions):
            if version.valid_until is None:
                # This version is open-ended; check that no later version exists
                if i < len(sorted_versions) - 1:
                    raise ValueError(
                        f"TariffPlanConfig: open-ended version (valid_from={version.valid_from}, "
                        f"valid_until=None) is not the last version. "
                        f"An open-ended version must be the final/current one in the chain. "
                        f"Later versions: {[f'valid_from={v.valid_from}' for v in sorted_versions[i+1:]]}"
                    )

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


class FreeWindowOrchestratorConfig(BaseModel):
    """Config for free-window import-cap-aware load orchestration (§7.5).

    During free windows, coordinates battery grid-charge + controlled loads
    so their total never exceeds max_grid_import_w. Uses a configurable
    priority ladder to shed/throttle in order.
    """

    enabled: bool = True
    # Load priority order for free-window allocation (by priority_class).
    # Battery is always highest priority (virtual priority 0).
    # Loads are shed in reverse order (highest priority_class first).
    # Example: battery (virtual 0) > hws (1) > pool (3) > ev (4)
    # Load IDs can be listed to override the natural priority_class order,
    # or left empty to use priority_class as-is.
    load_priority_order: list[str] = Field(
        default_factory=list,
        description="Optional load ID order for free-window shedding priority. "
        "Empty = use natural priority_class order.",
    )


class EVModeConfig(BaseModel):
    """EV charging mode configuration (both modes optional; not mutually exclusive)."""
    min_nightly_kwh: float | None = Field(
        default=None,
        description="OPTIONAL guaranteed minimum nightly charge in kWh. None = disabled (opportunistic only)."
    )
    opportunistic: bool = Field(
        default=False,
        description="Enable headroom-gated opportunistic charging (Phase 4 when controllable)"
    )

    @field_validator("min_nightly_kwh")
    @classmethod
    def validate_min_nightly_kwh(cls, v: float | None) -> float | None:
        """Ensure min_nightly_kwh > 0 if set."""
        if v is not None and v <= 0:
            raise ValueError(f"min_nightly_kwh must be > 0 if set, got {v}")
        return v


class EVConfig(BaseModel):
    """EV charger configuration (dumb timer today; control in Phase 4).

    The EV is fully opt-in: enabled=False (default) and all other fields are inert.
    When enabled=True, the solver provisions the battery for the EV's expected draw
    (Phase 3), but cannot yet switch the charger (Phase 4).

    Design notes:
    - enabled=False by default so existing configs without an EV block load unchanged.
    - charger_kw fed to the optimiser for provisioning + per-tick margin sizing.
    - charge_window and expected_nightly_kwh together specify the dumb timer's schedule
      and expected energy draw. The solver uses these to provision the battery.
      Either can be None/unset if not applicable.
    - controllable=False today; flips True at Phase 4 when hardware binding active.
    - adapter placeholder (shelly/mqtt/contactor enum) unused this milestone.
    - shed_priority places the EV in the load-pruning ladder (see loads/manager.py).
      Default 5 (opportunistic, first-to-shed) aligns with the priority_class system:
      1 = critical, 5 = opportunistic. EV is large + low-priority, so 5 is correct.
    - NO EV-specific SOC floor. The floor REUSES the global min-SOC reserve
      (battery_targets.morning_soc_minimum) evaluated at free-charge time.
    - learn_from_telemetry: seed (Phase 3.6, O2) for learn-the-charger (Phase 4+).
      When True, the system may observe the charger's real draw from telemetry and
      provision off learned behaviour (robust to drifting dumb timer). Default False;
      learning algorithm not implemented this milestone.

    Charging windows and expected energy:
    - charge_windows: List of time windows in HH:MM-HH:MM format (local time).
      Each window may cross midnight (e.g., "22:00-07:00"). Specifies when the
      dumb timer operates. Empty list = no charging window (forecast returns zeros).
    - expected_nightly_kwh: Expected total energy drawn across all charge_windows
      in kWh. If set, the solver uses this to provision the battery.
      Distinct from mode.min_nightly_kwh: this is the EXPECTED value for provisioning;
      min_nightly_kwh is a guaranteed MINIMUM (Phase 4 control enforces it).
    - min_nightly_kwh relationship: when both are set, expected_nightly_kwh is floored
      at min_nightly_kwh. This ensures the solver provisions AT LEAST the minimum.
    """
    enabled: bool = Field(
        default=False,
        description="Enable EV charger awareness in the solver. False = model is opt-in, entirely inert."
    )
    charger_kw: float = Field(
        default=2.5,
        description="Rated charger draw in kW. Used for provisioning + margin sizing. Ignored if enabled=False."
    )
    charge_windows: list[str] = Field(
        default_factory=list,
        description="OPTIONAL list of dumb timer charging windows in HH:MM-HH:MM format (local time). May cross midnight (e.g., '22:00-07:00'). Empty list = no windows."
    )
    expected_nightly_kwh: float | None = Field(
        default=None,
        description="OPTIONAL expected nightly energy draw in kWh (across all charge_windows). Solver uses this to provision battery. Floored at min_nightly_kwh if both set. None = not specified."
    )
    controllable: bool = Field(
        default=False,
        description="Controllable charger (Phase 4). False = dumb timer (Phase 3 awareness only)."
    )
    adapter: str | None = Field(
        default=None,
        description="Adapter binding placeholder for Phase 4 (e.g., 'shelly', 'mqtt', 'contactor'). Unused this milestone."
    )
    mode: EVModeConfig = Field(
        default_factory=EVModeConfig,
        description="Configurable charging modes (min_nightly, opportunistic)"
    )
    shed_priority: int = Field(
        default=5,
        ge=1,
        le=5,
        description="Load shedding priority (1=critical, 5=opportunistic/first-to-shed). EV defaults to 5 (large, low-priority load)."
    )
    learn_from_telemetry: bool = Field(
        default=False,
        description="SEED (Phase 3.6, O2): opt-in learn-the-charger. When True, the system observes charger draw from telemetry (7-14 days) and provisions off learned behaviour. Inert this milestone; algorithm not built until Phase 4+."
    )

    @field_validator("charger_kw")
    @classmethod
    def validate_charger_kw(cls, v: float, info) -> float:
        """Ensure charger_kw > 0 when enabled."""
        data = info.data
        if data.get("enabled") and v <= 0:
            raise ValueError(
                f"charger_kw must be > 0 when enabled=True, got {v}"
            )
        return v

    @field_validator("charge_windows")
    @classmethod
    def validate_charge_windows(cls, v: list[str]) -> list[str]:
        """Validate each charge window format HH:MM-HH:MM with valid hour/minute ranges.

        Midnight-crossing windows (e.g., 22:00-07:00) are allowed.
        """
        for window in v:
            if not isinstance(window, str):
                raise ValueError(f"charge_windows must contain strings, got {type(window)}")
            if not re.match(r"^\d{2}:\d{2}-\d{2}:\d{2}$", window):
                raise ValueError(f"charge_windows must match HH:MM-HH:MM format, got '{window}'")
            start_str, end_str = window.split("-")
            start_h, start_m = map(int, start_str.split(":"))
            end_h, end_m = map(int, end_str.split(":"))

            if not (0 <= start_h <= 23) or not (0 <= start_m <= 59):
                raise ValueError(f"Invalid start time in charge_windows '{window}': {start_str}")
            if not (0 <= end_h <= 23) or not (0 <= end_m <= 59):
                raise ValueError(f"Invalid end time in charge_windows '{window}': {end_str}")

        return v

    @field_validator("expected_nightly_kwh")
    @classmethod
    def validate_expected_nightly_kwh(cls, v: float | None) -> float | None:
        """Ensure expected_nightly_kwh > 0 if set."""
        if v is not None and v <= 0:
            raise ValueError(f"expected_nightly_kwh must be > 0 if set, got {v}")
        return v

    @field_validator("adapter")
    @classmethod
    def validate_adapter(cls, v: str | None) -> str | None:
        """Validate adapter is one of allowed values if set."""
        if v is not None and v not in ("shelly", "mqtt", "contactor"):
            raise ValueError(
                f"adapter must be 'shelly', 'mqtt', 'contactor', or None, got '{v}'"
            )
        return v

    @model_validator(mode="after")
    def validate_ev_config(self) -> EVConfig:
        """Validate EV configuration constraints.

        1. If enabled=True and expected_nightly_kwh is set, require at least one charge window.
        2. Floor expected_nightly_kwh at min_nightly_kwh if both are set.
        """
        # Constraint: enabled + expected_nightly_kwh requires at least one charge window
        if (self.enabled and self.expected_nightly_kwh is not None and
            not self.charge_windows):
            raise ValueError(
                "When ev.enabled=True and expected_nightly_kwh is set, "
                "at least one charge window must be specified in charge_windows"
            )

        # Floor expected_nightly_kwh at min_nightly_kwh
        if self.expected_nightly_kwh is not None and self.mode.min_nightly_kwh is not None:
            self.expected_nightly_kwh = max(
                self.expected_nightly_kwh, self.mode.min_nightly_kwh
            )
        return self


class LoadsConfig(BaseModel):
    shelly_devices: list[ShellyDeviceConfig] = Field(default_factory=list)
    mqtt_load_endpoints: list[MQTTLoadEndpointConfig] = Field(default_factory=list)
    free_window_orchestrator: FreeWindowOrchestratorConfig = Field(
        default_factory=FreeWindowOrchestratorConfig,
        description="Free-window import-cap-aware orchestration config.",
    )


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
    ev: EVConfig = Field(
        default_factory=EVConfig,
        description="EV charger configuration (Phase 3 awareness, Phase 4 control). Opt-in: enabled=False by default."
    )
    mqtt: MQTTConfig = MQTTConfig()
    dashboard: DashboardConfig = DashboardConfig()
    resilience: ResilienceConfig = ResilienceConfig()
    accounting: AccountingConfig = AccountingConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    logging: LoggingConfig = LoggingConfig()
    db: DBConfig = DBConfig()

    @model_validator(mode="after")
    def resolve_arbitrage_gate_policy(self) -> AppConfig:
        """Resolve arbitrage.gate_policy default based on provider type.

        - type='tou' -> gate_policy='tou_aware' (disable WACB gate for fixed TOU)
        - type='amber' (or other) -> gate_policy='spot' (keep legacy behaviour)

        User can always set arbitrage.gate_policy explicitly to override.
        """
        # If gate_policy is still at default "spot" AND provider is TOU, switch to tou_aware.
        # If user explicitly set gate_policy, respect it.
        if (self.arbitrage.gate_policy == "spot" and
            self.providers.tariff.type == "tou"):
            self.arbitrage.gate_policy = "tou_aware"

        return self

    @model_validator(mode="after")
    def resolve_rebuild_cadence_defaults(self) -> AppConfig:
        """Resolve rebuild cadence defaults based on provider tariff type.

        - type='tou' -> rebuild_on_forecast_staleness=False, rebuild_on_actuals_deviation=False,
          mode_switch_hysteresis_cents=3.0 (stable TOU with 3-6h solar forecasts; no churn needed)
        - type='amber' (or other) -> rebuild_on_forecast_staleness=True, rebuild_on_actuals_deviation=True,
          mode_switch_hysteresis_cents=0.0 (dynamic pricing requires high-resolution reactivity)

        User can always set these fields explicitly (non-None) to override the defaults.
        """
        tariff_type = self.providers.tariff.type

        # Only resolve fields that are None (unset); explicit values are preserved
        if self.planning.rebuild_on_forecast_staleness is None:
            self.planning.rebuild_on_forecast_staleness = (
                False if tariff_type == "tou" else True
            )

        if self.planning.rebuild_on_actuals_deviation is None:
            self.planning.rebuild_on_actuals_deviation = (
                False if tariff_type == "tou" else True
            )

        if self.planning.mode_switch_hysteresis_cents is None:
            self.planning.mode_switch_hysteresis_cents = (
                3.0 if tariff_type == "tou" else 0.0
            )

        return self
