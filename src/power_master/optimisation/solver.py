"""MILP solver for battery optimisation using PuLP (CBC)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pulp

from power_master.config.schema import (
    AppConfig,
)
from power_master.optimisation.constraints import (
    add_arbitrage_gate,
    add_charge_taper,
    add_daytime_soc_minimum,
    add_energy_balance,
    add_evening_soc_target,
    add_grid_charge_policy,
    add_morning_soc_minimum,
    add_power_limits,
    add_safety_limits,
    add_soc_dynamics,
    add_spike_constraints,
    add_storm_reserve,
)
from power_master.optimisation.objective import ObjectiveWeights, build_objective
from power_master.optimisation.plan import OptimisationPlan, PlanSlot, SlotMode
from power_master.timezone_utils import resolve_timezone

logger = logging.getLogger(__name__)


def _add_export_tier_constraints(
    prob: pulp.LpProblem,
    t: int,
    grid_export: pulp.LpVariable,
    export_tier_vars: list[pulp.LpVariable],
    tier_struct: "ExportTierStructure",
    slot_hours: float,
) -> None:
    """Add per-slot tier decomposition constraints.

    For slots in tiered windows: sum_k export_tier_k[t] == grid_export[t].
    For non-tiered slots: export_tier_vars[t] is empty; no constraint needed.
    """
    if not export_tier_vars:
        return

    # Decomposition: sum of tier exports = total export
    prob += pulp.lpSum(export_tier_vars) == grid_export, f"export_tier_decomp_{t}"


def _add_export_tier_caps(
    prob: pulp.LpProblem,
    n_slots: int,
    slot_hours: float,
    export_tier_vars: list[list[pulp.LpVariable]],
    tier_structs: list["ExportTierStructure"],
) -> None:
    """Add per-day cumulative tier cap constraints.

    For each tier k with a non-null cap, sum the kWh exported in that tier
    across all slots of the same local day, and enforce <= cap_kwh_per_day.

    Open-ended tiers (cap=None) have no constraint.
    """
    from datetime import date
    from collections import defaultdict

    # Group slots by (local_date, tier_index)
    # tier_slots[date][tier_index] = list of slot indices
    tier_slots: dict[tuple[date, int], list[int]] = defaultdict(list)

    for t in range(n_slots):
        struct = tier_structs[t]
        if not struct or not struct.in_tiered_window or not struct.tiers:
            continue

        local_date = struct.local_date
        if not local_date:
            continue

        for k in range(len(struct.tiers)):
            tier_slots[(local_date, k)].append(t)

    # For each (date, tier) with a cap, add cumulative constraint
    for (local_date, tier_idx), slot_list in tier_slots.items():
        struct = tier_structs[slot_list[0]]  # Any slot on this date works
        tier = struct.tiers[tier_idx]

        # Only add cap constraint if tier has a non-None cap
        if tier.up_to_kwh_per_day is None:
            continue

        # Sum of kWh for this tier on this day <= cap_kwh_per_day
        cap_kwh = tier.up_to_kwh_per_day
        export_terms = []
        for t in slot_list:
            if t < len(export_tier_vars) and tier_idx < len(export_tier_vars[t]):
                # Convert watts to kWh: power_W * slot_hours / 1000
                export_terms.append(export_tier_vars[t][tier_idx] * slot_hours / 1000)

        if export_terms:
            prob += (
                pulp.lpSum(export_terms) <= cap_kwh,
                f"export_tier_cap_{local_date}_{tier_idx}",
            )


def dampen_price(price_cents: float, threshold_cents: int, factor: float) -> float:
    """Dampen extreme prices above the threshold.

    Prices below the threshold pass through unchanged.  For prices above,
    only a fraction (factor) of the excess is kept:
        dampened = threshold + (price - threshold) * factor

    This prevents the solver from overreacting to extreme price spikes
    while still incentivising the correct behaviour.
    """
    if price_cents <= threshold_cents:
        return price_cents
    return threshold_cents + (price_cents - threshold_cents) * factor


def dampen_price_weighted(
    price_cents: float,
    threshold_cents: int,
    base_factor: float,
    slot_index: int,
    n_slots: int,
) -> float:
    """Apply less dampening to near-term slots and more to far-term slots."""
    if n_slots <= 1:
        effective_factor = base_factor
    else:
        horizon_pos = slot_index / (n_slots - 1)
        effective_factor = 1.0 - (1.0 - base_factor) * horizon_pos
    return dampen_price(price_cents, threshold_cents, effective_factor)


@dataclass
class ExportTier:
    """Per-tier export rate cap and pricing.

    Attributes:
        up_to_kwh_per_day: Cumulative cap for this tier (None = open-ended, last tier only).
        rate_c_per_kwh: Rate in cents/kWh for kWh in this tier.
    """
    up_to_kwh_per_day: float | None
    rate_c_per_kwh: float


@dataclass
class ExportTierStructure:
    """Per-slot tiered export structure (if any).

    Attributes:
        in_tiered_window: bool — True if this slot is in a tiered feed-in window.
        tiers: list[ExportTier] — tiers for this slot (empty if flat FiT).
        local_date: date — local calendar date (for per-day cap resetting).
    """
    in_tiered_window: bool = False
    tiers: list[ExportTier] | None = None
    local_date: date | None = None


@dataclass
class CreditWindowInfo:
    """Per-slot low-import credit window info (Phase 2).

    Attributes:
        in_window: bool — True if this slot is in a credit window.
        credit_name: str — credit name (e.g., 'zerohero-evening').
        max_import_kwh_per_hour: float — hourly import threshold.
        reward_dollars_per_day: float — daily reward if earned.
        enforcement: str — 'soft' or 'hard'.
        credit_priority_weight: float — [0, 1] weight tuning credit vs export.
        local_date: date — local calendar date (for daily credit state).
    """
    in_window: bool = False
    credit_name: str = ""
    max_import_kwh_per_hour: float = 0.0
    reward_dollars_per_day: float = 0.0
    enforcement: str = "soft"
    credit_priority_weight: float = 0.5
    local_date: "date | None" = None


@dataclass
class SolverInputs:
    """All inputs needed by the solver for a single optimisation run."""

    # Per-slot arrays (length = n_slots)
    solar_forecast_w: list[float]
    load_forecast_w: list[float]
    import_rate_cents: list[float]
    export_rate_cents: list[float]
    is_spike: list[bool]

    # Current state
    current_soc: float
    wacb_cents: float  # Current weighted average cost basis

    # Storm
    storm_active: bool = False
    storm_reserve_soc: float = 0.0

    # Timing
    slot_start_times: list[datetime] | None = None

    # Volume-tiered export (Phase 2)
    # Per-slot tier structure for tiered feed-in windows (empty/None = flat FiT).
    export_tier_structures: list[ExportTierStructure] | None = None

    # Low-import credit windows (Phase 2)
    # Per-slot credit window info for low-import evening credits (e.g., ZEROHERO).
    credit_windows: list[CreditWindowInfo] | None = None

    # Hysteresis: the mode of the current slot in the incumbent plan (status-quo tie-break)
    # If None, no incumbent exists; if set, carries the current slot's mode for mode-switch hysteresis.
    incumbent_mode: SlotMode | None = None

    @property
    def n_slots(self) -> int:
        return len(self.solar_forecast_w)

    @property
    def has_tiered_export(self) -> bool:
        """True if any slot has tiered export structure."""
        if not self.export_tier_structures:
            return False
        return any(s.in_tiered_window for s in self.export_tier_structures)

    @property
    def has_credit_windows(self) -> bool:
        """True if any slot is in a low-import credit window."""
        if not self.credit_windows:
            return False
        return any(c.in_window for c in self.credit_windows)


def solve(
    config: AppConfig,
    inputs: SolverInputs,
    trigger_reason: str = "periodic",
    plan_version: int = 1,
) -> OptimisationPlan:
    """Run the MILP optimisation and return a plan.

    Uses PuLP with CBC solver. Timeout configurable via config.planning.solver_timeout_seconds.
    """
    start_time = time.monotonic()
    n = inputs.n_slots
    slot_minutes = config.planning.slot_duration_minutes
    slot_hours = slot_minutes / 60.0
    cap = config.battery.capacity_wh
    eff = config.battery.round_trip_efficiency ** 0.5  # Split efficiency between charge/discharge
    planner_tz = _resolve_planner_timezone(config)

    # ── Apply price dampening to import rates ──
    arb = config.arbitrage
    dampened_import = [
        dampen_price_weighted(
            price_cents=p,
            threshold_cents=arb.price_dampen_threshold_cents,
            base_factor=arb.price_dampen_factor,
            slot_index=t,
            n_slots=n,
        )
        for t, p in enumerate(inputs.import_rate_cents)
    ]

    # ── Create problem ──
    prob = pulp.LpProblem("PowerMaster", pulp.LpMinimize)

    # ── Decision variables ──
    charge = [pulp.LpVariable(f"charge_{t}", 0, config.battery.max_charge_rate_w) for t in range(n)]
    discharge = [pulp.LpVariable(f"discharge_{t}", 0, config.battery.max_discharge_rate_w) for t in range(n)]
    is_charging = [pulp.LpVariable(f"is_charging_{t}", cat="Binary") for t in range(n)]
    # Grid bounded by inverter capacity + load headroom
    max_grid = config.battery.max_charge_rate_w + config.battery.max_discharge_rate_w
    grid_import = [pulp.LpVariable(f"grid_import_{t}", 0, max_grid) for t in range(n)]
    grid_export = [pulp.LpVariable(f"grid_export_{t}", 0, max_grid) for t in range(n)]
    soc = [pulp.LpVariable(f"soc_{t}", 0, 1) for t in range(n)]
    self_consumed = [pulp.LpVariable(f"self_consumed_{t}", 0) for t in range(n)]
    # Curtailment: excess solar that can't be absorbed (battery full + export blocked)
    curtail = [pulp.LpVariable(f"curtail_{t}", 0) for t in range(n)]

    # ── Volume-tiered export variables (Phase 2) ──
    # Per-tier export power per slot: export_tier_k[t] (watts, for slots in tiered windows)
    export_tier_vars = []  # List[List[LpVariable]] — [slot][tier]
    if inputs.has_tiered_export:
        for t in range(n):
            tier_vars = []
            struct = inputs.export_tier_structures[t] if inputs.export_tier_structures else None
            if struct and struct.in_tiered_window and struct.tiers:
                for k in range(len(struct.tiers)):
                    var = pulp.LpVariable(f"export_tier_{k}_{t}", 0, max_grid)
                    tier_vars.append(var)
            export_tier_vars.append(tier_vars)

    # ── Low-import credit window variables (Phase 2) ──
    # Per-day binary missed_credit[d] (1 = credit missed for day d; 0 = earned)
    # and per-slot sum of in-window grid_import for each day and credit
    credit_missed_vars: dict[tuple[str, "date"], pulp.LpVariable] = {}
    credit_daily_import: dict[tuple[str, "date"], list[int]] = {}  # (credit_name, date) -> list of slot indices
    if inputs.has_credit_windows:
        # First pass: identify (credit_name, local_date) pairs and collect slot indices
        from datetime import date as dt_date
        credit_slots: dict[tuple[str, dt_date], list[int]] = {}
        for t in range(n):
            cw = inputs.credit_windows[t] if inputs.credit_windows else None
            if cw and cw.in_window:
                key = (cw.credit_name, cw.local_date)
                if key not in credit_slots:
                    credit_slots[key] = []
                credit_slots[key].append(t)

        # Create per-day missed credit binary vars (only for days with credit windows)
        for (credit_name, local_date), slot_list in credit_slots.items():
            var = pulp.LpVariable(f"credit_missed_{credit_name}_{local_date}", cat="Binary")
            key = (credit_name, local_date)
            credit_missed_vars[key] = var
            credit_daily_import[key] = slot_list

    # Slack variables
    safety_slack = [pulp.LpVariable(f"safety_slack_{t}", 0) for t in range(n)]
    storm_slack = []
    evening_slack = []
    morning_slack = []
    daytime_slack = []
    credit_slack = []  # Hard-enforcement credit slack vars

    # Taper zone binary variables (1 = SOC is above taper threshold)
    taper_start = config.battery.taper_start_soc
    taper_factor = config.battery.taper_factor
    in_taper = [pulp.LpVariable(f"in_taper_{t}", cat="Binary") for t in range(n)]

    # ── Constraints per slot ──
    for t in range(n):
        # SOC dynamics
        soc_prev = inputs.current_soc if t == 0 else soc[t - 1]
        add_soc_dynamics(prob, t, soc[t], soc_prev, charge[t], discharge[t], cap, slot_hours, eff, eff)

        # Safety limits
        add_safety_limits(prob, t, soc[t], config.battery.soc_min_hard, config.battery.soc_max_hard, safety_slack[t])

        # Power limits + exclusivity
        add_power_limits(
            prob, t, charge[t], discharge[t], is_charging[t],
            config.battery.max_charge_rate_w, config.battery.max_discharge_rate_w,
        )

        # Battery charge taper (CC→CV transition)
        add_charge_taper(
            prob, t, soc[t], charge[t], in_taper[t],
            taper_start, config.battery.max_charge_rate_w, taper_factor,
        )

        # Energy balance
        add_energy_balance(
            prob, t, inputs.solar_forecast_w[t], inputs.load_forecast_w[t],
            grid_import[t], grid_export[t], charge[t], discharge[t],
            self_consumed[t], curtail[t],
        )

        # Volume-tiered export decomposition constraint (Phase 2)
        if export_tier_vars and export_tier_vars[t]:
            _add_export_tier_constraints(
                prob, t, grid_export[t], export_tier_vars[t],
                inputs.export_tier_structures[t],
                slot_hours,
            )

        # Arbitrage gate (provider-aware, §R2)
        add_arbitrage_gate(
            prob, t, grid_export[t],
            inputs.export_rate_cents[t], inputs.wacb_cents,
            config.arbitrage.break_even_delta_cents,
            gate_policy=config.arbitrage.gate_policy,
        )

        # Grid-charge policy: free-window + solar only (or allow arbitrage)
        add_grid_charge_policy(
            prob, t, grid_import[t], charge[t],
            inputs.import_rate_cents[t],
            policy=config.providers.tariff.grid_charge_policy,
            free_rate_threshold_cents=1.0,  # ~0c: allow charging in free/0c windows
        )

        # Spike constraints
        add_spike_constraints(prob, t, charge[t], inputs.is_spike[t])

        # Storm reserve
        if inputs.storm_active:
            ss = pulp.LpVariable(f"storm_slack_{t}", 0)
            storm_slack.append(ss)
            add_storm_reserve(prob, t, soc[t], inputs.storm_reserve_soc, ss)

        # Time-based soft targets
        if inputs.slot_start_times:
            hour = inputs.slot_start_times[t].astimezone(planner_tz).hour
            # Evening SOC target (at peak start hour)
            if hour == config.battery_targets.evening_target_hour:
                es = pulp.LpVariable(f"evening_slack_{t}", 0)
                evening_slack.append(es)
                add_evening_soc_target(prob, t, soc[t], config.battery_targets.evening_soc_target, es)
            # Morning minimum
            if hour == config.battery_targets.morning_minimum_hour:
                ms = pulp.LpVariable(f"morning_slack_{t}", 0)
                morning_slack.append(ms)
                add_morning_soc_minimum(prob, t, soc[t], config.battery_targets.morning_soc_minimum, ms)
            reserve_start = config.battery_targets.daytime_reserve_start_hour
            reserve_end = config.battery_targets.daytime_reserve_end_hour
            reserve_target = config.battery_targets.daytime_reserve_soc_target
            if reserve_start <= hour < reserve_end and reserve_target > 0:
                ds = pulp.LpVariable(f"daytime_slack_{t}", 0)
                daytime_slack.append(ds)
                add_daytime_soc_minimum(prob, t, soc[t], reserve_target, ds)

        # Low-import credit window constraints (Phase 2)
        if inputs.has_credit_windows:
            cw = inputs.credit_windows[t] if inputs.credit_windows else None
            if cw and cw.in_window and cw.enforcement == "hard":
                # Hard enforcement: grid_import[t] == 0 with penalised slack
                cs = pulp.LpVariable(f"credit_slack_{t}", 0)
                credit_slack.append(cs)
                prob += grid_import[t] <= cs, f"credit_hard_constraint_{cw.credit_name}_{t}"

    # ── Per-day cumulative tier cap constraints (Phase 2) ──
    if inputs.has_tiered_export and export_tier_vars:
        _add_export_tier_caps(
            prob, n, slot_hours, export_tier_vars,
            inputs.export_tier_structures,
        )

    # ── Low-import credit window per-day constraints (Phase 2) ──
    # For soft enforcement: binary missed_credit[d] with threshold-based big-M constraint
    # For hard enforcement: slack was added per-slot above
    if inputs.has_credit_windows and credit_daily_import:
        BIG_M = 1e6  # Large penalty for missed credit
        for (credit_name, local_date), slot_list in credit_daily_import.items():
            cw = inputs.credit_windows[slot_list[0]] if inputs.credit_windows and slot_list else None
            if cw and cw.enforcement == "soft" and (credit_name, local_date) in credit_missed_vars:
                missed_var = credit_missed_vars[(credit_name, local_date)]
                threshold_kwh = cw.max_import_kwh_per_hour * len(slot_list) * slot_hours
                # Sum of in-window import in kWh
                import_sum = pulp.lpSum([grid_import[t] * slot_hours / 1000.0 for t in slot_list])
                # If import_sum > threshold: missed[d] must be 1; else can be 0
                prob += import_sum <= threshold_kwh + BIG_M * missed_var, \
                    f"credit_soft_threshold_{credit_name}_{local_date}"

    # ── Hysteresis bias for slot-0 mode stability (status-quo tie-break) ──
    # If incumbent_mode is set and hysteresis is enabled, compute a signed bias to reward
    # staying in the current mode when export price is marginal.
    incumbent_export_bias_cents = 0.0
    hyst = config.planning.mode_switch_hysteresis_cents
    if hyst > 0 and inputs.incumbent_mode is not None:
        incumbent_mode = inputs.incumbent_mode
        # Normalise string -> enum if needed (robustness)
        if isinstance(incumbent_mode, str):
            incumbent_mode = SlotMode[incumbent_mode.upper()]

        if incumbent_mode == SlotMode.FORCE_DISCHARGE:
            # Currently discharging to grid: penalise switching away (negative bias rewards export)
            incumbent_export_bias_cents = -hyst
        elif incumbent_mode == SlotMode.SELF_USE:
            # Currently self-using: penalise switching to export (positive bias penalises export)
            incumbent_export_bias_cents = hyst
        # else: FORCE_CHARGE incumbent or other modes → no bias (only discharge/self-use drive export)

    # ── Objective ── (uses dampened import prices to avoid overreaction to spikes)
    build_objective(
        prob, n, slot_hours,
        dampened_import, inputs.export_rate_cents,
        config.fixed_costs.hedging_per_kwh_cents,
        grid_import, grid_export, self_consumed,
        safety_slack, storm_slack, evening_slack, morning_slack, daytime_slack,
        export_tier_vars, inputs.export_tier_structures,
        incumbent_export_bias_cents=incumbent_export_bias_cents,
        credit_missed_vars=credit_missed_vars,
        credit_windows=inputs.credit_windows,
        credit_slack=credit_slack,
    )

    # ── Solve ──
    solver = pulp.PULP_CBC_CMD(
        msg=0,
        timeLimit=config.planning.solver_timeout_seconds,
    )
    prob.solve(solver)

    solver_time_ms = int((time.monotonic() - start_time) * 1000)
    status = pulp.LpStatus[prob.status]

    if status not in ("Optimal", "Not Solved"):
        logger.warning("Solver status: %s (time: %dms)", status, solver_time_ms)

    # ── Build plan from solution ──
    now = datetime.now(timezone.utc)
    horizon_start = inputs.slot_start_times[0] if inputs.slot_start_times else now
    horizon_end = horizon_start + timedelta(minutes=slot_minutes * n)

    plan_slots = []
    active_constraints = []
    if inputs.storm_active:
        active_constraints.append("storm_reserve")

    force_charge_threshold = config.battery_targets.force_charge_below_price_cents
    grid_charge_policy = config.providers.tariff.grid_charge_policy

    for t in range(n):
        charge_val = pulp.value(charge[t]) or 0
        discharge_val = pulp.value(discharge[t]) or 0
        soc_val = pulp.value(soc[t]) or 0

        # Determine mode from solution
        export_val = pulp.value(grid_export[t]) or 0
        import_val = pulp.value(grid_import[t]) or 0
        mode = _determine_mode(
            charge_val, discharge_val, export_val, import_val, inputs.is_spike[t],
        )
        # Cheap-price override: force grid charging whenever buy price is at or
        # below the configured threshold, regardless of solver decision.
        # Under "free_window_and_solar_only" policy: only allow force-charge at ~0c
        # (the free window), not at paid rates. This prevents panic-import.
        if force_charge_threshold > 0:
            slot_import_rate = float(inputs.import_rate_cents[t])
            allow_force_charge = False

            if grid_charge_policy == "free_window_and_solar_only":
                # Only force-charge if at the free/0c rate AND below the configured threshold
                free_rate_threshold = 1.0  # ~0c tolerance
                allow_force_charge = (
                    slot_import_rate <= free_rate_threshold and
                    slot_import_rate <= force_charge_threshold
                )
            else:  # "allow_arbitrage" — original unrestricted behaviour
                allow_force_charge = slot_import_rate <= force_charge_threshold

            if allow_force_charge:
                mode = SlotMode.FORCE_CHARGE
        power = _determine_target_power(
            mode=mode,
            discharge_w=discharge_val,
            config=config,
            inputs=inputs,
            slot_index=t,
        )

        slot_start = horizon_start + timedelta(minutes=t * slot_minutes)
        slot_end = slot_start + timedelta(minutes=slot_minutes)

        flags = []
        if inputs.is_spike[t]:
            flags.append("spike")
        if inputs.storm_active:
            flags.append("storm_reserve")

        plan_slots.append(PlanSlot(
            index=t,
            start=slot_start,
            end=slot_end,
            mode=mode,
            target_power_w=power,
            expected_soc=round(soc_val, 4),
            import_rate_cents=inputs.import_rate_cents[t],
            export_rate_cents=inputs.export_rate_cents[t],
            solar_forecast_w=inputs.solar_forecast_w[t],
            load_forecast_w=inputs.load_forecast_w[t],
            constraint_flags=flags if flags else None,
        ))

    objective_val = pulp.value(prob.objective) or 0.0

    plan = OptimisationPlan(
        version=plan_version,
        created_at=now,
        trigger_reason=trigger_reason,
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        slots=plan_slots,
        objective_score=round(objective_val, 2),
        solver_time_ms=solver_time_ms,
        active_constraints=active_constraints,
        metrics={
            "status": status,
            "n_slots": n,
            "current_soc": inputs.current_soc,
            "wacb_cents": inputs.wacb_cents,
            "storm_active": inputs.storm_active,
        },
    )

    logger.info(
        "Solver complete: status=%s objective=%.2f time=%dms slots=%d",
        status, objective_val, solver_time_ms, n,
    )

    return plan


def _resolve_planner_timezone(config: AppConfig):
    """Resolve planner local timezone from config."""
    tz_name = getattr(config.load_profile, "timezone", "UTC")
    return resolve_timezone(tz_name)


def _determine_mode(
    charge_w: float, discharge_w: float, grid_export_w: float, grid_import_w: float, is_spike: bool,
) -> SlotMode:
    """Determine the operating mode from solver decision variables.

    Discharge for arbitrage (grid export > 0) uses FORCE_DISCHARGE so the
    inverter actively pushes power to the grid.  Discharge that only serves
    local loads (no grid export) uses SELF_USE — the inverter handles load-
    serving from battery natively without remote control.
    """
    threshold = 50  # Minimum power to consider active

    if charge_w > threshold and grid_import_w > threshold:
        return SlotMode.FORCE_CHARGE
    elif discharge_w > threshold and grid_export_w > threshold:
        # Arbitrage: actively exporting to grid for profit
        return SlotMode.FORCE_DISCHARGE
    elif is_spike:
        return SlotMode.SELF_USE
    else:
        # Self-use covers both idle and load-serving discharge
        return SlotMode.SELF_USE


def _determine_target_power(
    mode: SlotMode,
    discharge_w: float,
    config: AppConfig,
    inputs: SolverInputs,
    slot_index: int,
) -> int:
    """Map solver flows to inverter command power for the selected mode."""
    if mode == SlotMode.FORCE_CHARGE:
        return _force_charge_target_power(config, inputs, slot_index)
    if mode == SlotMode.FORCE_DISCHARGE:
        return max(0, int(discharge_w))
    return max(0, int(discharge_w))


def _force_charge_target_power(config: AppConfig, inputs: SolverInputs, slot_index: int) -> int:
    """Use full force-charge power unless total grid import is capped."""
    full_power = max(0, int(config.battery.max_charge_rate_w))
    import_cap = int(config.battery.max_grid_import_w)
    if import_cap <= 0:
        return full_power

    load_w = max(0.0, float(inputs.load_forecast_w[slot_index]))
    solar_w = max(0.0, float(inputs.solar_forecast_w[slot_index]))
    base_grid_import_w = max(0.0, load_w - solar_w)
    headroom_w = max(0.0, float(import_cap) - base_grid_import_w)
    return min(full_power, int(headroom_w))
