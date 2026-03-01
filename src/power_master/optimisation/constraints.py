"""Constraint builders for the MILP solver."""

from __future__ import annotations

import pulp


def add_energy_balance(
    prob: pulp.LpProblem,
    t: int,
    solar_w: float,
    load_w: float,
    grid_import: pulp.LpVariable,
    grid_export: pulp.LpVariable,
    charge: pulp.LpVariable,
    discharge: pulp.LpVariable,
    self_consumed: pulp.LpVariable,
    curtail: pulp.LpVariable,
) -> None:
    """Energy balance: solar + grid_import + discharge = load + grid_export + charge + curtail.

    All power sources (solar, grid import, battery discharge) must equal
    all power sinks (load, grid export, battery charge, curtailment).
    Excess solar flows into battery charge, grid export, or is curtailed
    when neither sink is available (e.g. battery full + export blocked).
    """
    prob += (
        solar_w + grid_import + discharge
        == load_w + grid_export + charge + curtail,
        f"energy_balance_{t}",
    )
    # Curtailment can't exceed available solar
    prob += curtail <= solar_w, f"curtail_cap_{t}"
    # Self-consumed solar is an accounting variable for the objective
    # (rewards using solar locally instead of importing).
    prob += self_consumed <= solar_w, f"self_consume_cap_solar_{t}"
    prob += self_consumed <= load_w, f"self_consume_cap_load_{t}"


def add_soc_dynamics(
    prob: pulp.LpProblem,
    t: int,
    soc: pulp.LpVariable,
    soc_prev: pulp.LpVariable | float,
    charge: pulp.LpVariable,
    discharge: pulp.LpVariable,
    capacity_wh: float,
    slot_hours: float,
    charge_efficiency: float,
    discharge_efficiency: float,
) -> None:
    """SOC transition: soc_t = soc_{t-1} + charge*eff/cap - discharge/(eff*cap)."""
    prob += (
        soc
        == soc_prev
        + (charge * slot_hours * charge_efficiency) / capacity_wh
        - (discharge * slot_hours) / (discharge_efficiency * capacity_wh),
        f"soc_dynamics_{t}",
    )


def add_safety_limits(
    prob: pulp.LpProblem,
    t: int,
    soc: pulp.LpVariable,
    soc_min: float,
    soc_max: float,
    safety_slack: pulp.LpVariable,
) -> None:
    """Hard SOC limits with safety slack for feasibility."""
    prob += soc >= soc_min - safety_slack, f"soc_min_hard_{t}"
    prob += soc <= soc_max + safety_slack, f"soc_max_hard_{t}"


def add_power_limits(
    prob: pulp.LpProblem,
    t: int,
    charge: pulp.LpVariable,
    discharge: pulp.LpVariable,
    is_charging: pulp.LpVariable,
    max_charge_w: float,
    max_discharge_w: float,
) -> None:
    """Inverter power limits and charge/discharge exclusivity."""
    prob += charge <= max_charge_w * is_charging, f"charge_limit_{t}"
    prob += discharge <= max_discharge_w * (1 - is_charging), f"discharge_limit_{t}"


def add_storm_reserve(
    prob: pulp.LpProblem,
    t: int,
    soc: pulp.LpVariable,
    reserve_soc: float,
    storm_slack: pulp.LpVariable,
) -> None:
    """Storm reserve: SOC >= reserve target (with slack)."""
    prob += soc >= reserve_soc - storm_slack, f"storm_reserve_{t}"


def add_arbitrage_gate(
    prob: pulp.LpProblem,
    t: int,
    grid_export: pulp.LpVariable,
    export_rate: float,
    wacb: float,
    break_even_delta: float,
) -> None:
    """Only allow grid export when profitable above break-even.

    If export_rate < wacb + break_even_delta, block grid export.
    Battery discharge for self-use (avoiding grid import) is always allowed.
    """
    if export_rate < wacb + break_even_delta:
        prob += grid_export == 0, f"arbitrage_gate_{t}"


def add_evening_soc_target(
    prob: pulp.LpProblem,
    t: int,
    soc: pulp.LpVariable,
    target_soc: float,
    slack: pulp.LpVariable,
) -> None:
    """Soft penalty for SOC below target at evening peak start."""
    prob += soc >= target_soc - slack, f"evening_soc_{t}"


def add_morning_soc_minimum(
    prob: pulp.LpProblem,
    t: int,
    soc: pulp.LpVariable,
    min_soc: float,
    slack: pulp.LpVariable,
) -> None:
    """Soft penalty for SOC below minimum at morning."""
    prob += soc >= min_soc - slack, f"morning_soc_{t}"


def add_daytime_soc_minimum(
    prob: pulp.LpProblem,
    t: int,
    soc: pulp.LpVariable,
    min_soc: float,
    slack: pulp.LpVariable,
) -> None:
    """Soft penalty for SOC below daytime reserve target."""
    prob += soc >= min_soc - slack, f"daytime_soc_{t}"


def add_spike_constraints(
    prob: pulp.LpProblem,
    t: int,
    charge: pulp.LpVariable,
    is_spike: bool,
) -> None:
    """During price spikes: block grid charging."""
    if is_spike:
        prob += charge == 0, f"spike_no_charge_{t}"


def add_charge_taper(
    prob: pulp.LpProblem,
    t: int,
    soc: pulp.LpVariable,
    charge: pulp.LpVariable,
    in_taper: pulp.LpVariable,
    taper_start_soc: float,
    max_charge_w: float,
    taper_factor: float,
) -> None:
    """Reduce charge rate when SOC is in the taper zone.

    Models the CC→CV transition in lithium batteries where the BMS
    tapers charging current as SOC approaches 100%.  Uses big-M
    linearization so the constraint remains MILP-compatible.

    When SOC >= taper_start_soc, charge is limited to max_charge_w * taper_factor.
    """
    M = 1.0  # SOC is in [0, 1] so M=1 is sufficient

    # Link in_taper binary to SOC threshold
    prob += soc <= taper_start_soc + M * in_taper, f"taper_link_upper_{t}"
    prob += soc >= taper_start_soc - M * (1 - in_taper), f"taper_link_lower_{t}"

    # Reduce charge rate when in taper zone
    reduction = max_charge_w * (1 - taper_factor)
    prob += charge <= max_charge_w - reduction * in_taper, f"taper_charge_limit_{t}"
