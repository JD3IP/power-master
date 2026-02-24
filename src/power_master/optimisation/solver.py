"""MILP solver for battery optimisation using PuLP (CBC)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pulp

from power_master.config.schema import (
    AppConfig,
)
from power_master.optimisation.constraints import (
    add_arbitrage_gate,
    add_energy_balance,
    add_evening_soc_target,
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

    @property
    def n_slots(self) -> int:
        return len(self.solar_forecast_w)


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
        dampen_price(p, arb.price_dampen_threshold_cents, arb.price_dampen_factor)
        for p in inputs.import_rate_cents
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

    # Slack variables
    safety_slack = [pulp.LpVariable(f"safety_slack_{t}", 0) for t in range(n)]
    storm_slack = []
    evening_slack = []
    morning_slack = []

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

        # Energy balance
        add_energy_balance(
            prob, t, inputs.solar_forecast_w[t], inputs.load_forecast_w[t],
            grid_import[t], grid_export[t], charge[t], discharge[t], self_consumed[t],
        )

        # Arbitrage gate (blocks grid export, not self-use discharge)
        add_arbitrage_gate(
            prob, t, grid_export[t],
            inputs.export_rate_cents[t], inputs.wacb_cents,
            config.arbitrage.break_even_delta_cents,
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

    # ── Objective ── (uses dampened import prices to avoid overreaction to spikes)
    build_objective(
        prob, n, slot_hours,
        dampened_import, inputs.export_rate_cents,
        config.fixed_costs.hedging_per_kwh_cents,
        grid_import, grid_export, self_consumed,
        safety_slack, storm_slack, evening_slack, morning_slack,
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

    for t in range(n):
        charge_val = pulp.value(charge[t]) or 0
        discharge_val = pulp.value(discharge[t]) or 0
        soc_val = pulp.value(soc[t]) or 0

        # Determine mode from solution
        export_val = pulp.value(grid_export[t]) or 0
        mode = _determine_mode(charge_val, discharge_val, export_val, inputs.is_spike[t])
        power = abs(int(charge_val)) if mode == SlotMode.FORCE_CHARGE else abs(int(discharge_val))

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
    charge_w: float, discharge_w: float, grid_export_w: float, is_spike: bool,
) -> SlotMode:
    """Determine the operating mode from solver decision variables.

    Discharge for arbitrage (grid export > 0) uses FORCE_DISCHARGE so the
    inverter actively pushes power to the grid.  Discharge that only serves
    local loads (no grid export) uses SELF_USE — the inverter handles load-
    serving from battery natively without remote control.
    """
    threshold = 50  # Minimum power to consider active

    if charge_w > threshold:
        return SlotMode.FORCE_CHARGE
    elif discharge_w > threshold and grid_export_w > threshold:
        # Arbitrage: actively exporting to grid for profit
        return SlotMode.FORCE_DISCHARGE
    elif is_spike:
        return SlotMode.SELF_USE
    else:
        # Self-use covers both idle and load-serving discharge
        return SlotMode.SELF_USE
