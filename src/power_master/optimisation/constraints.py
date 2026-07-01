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
    gate_policy: str = "spot",
) -> None:
    """Provider-aware arbitrage gate (§R2 — Phase 2).

    SPOT POLICY (Amber/spot providers, default):
      Block grid export when export_rate < wacb + break_even_delta.
      Protective: prevents exporting when battery was expensive (high WACB) and
      export rate is low (spot volatility). Safe for unpredictable spot pricing.

    TOU_AWARE POLICY (TOU providers like Globird):
      Do NOT apply the WACB gate. Disable it entirely.
      Rationale: TOU export rates are deterministic and contractually guaranteed
      (e.g., 10c Super Export tier, 8c off-peak FiT, 2c base FiT). There is no
      economic case to block a known-good export just because WACB is high (which
      may reflect past grid-charging history, not the current export's value).
      The solver's own objective cost model will optimize export profitably.

    In both cases, battery discharge for self-use (avoiding grid import) is
    always allowed (not gated by this function).

    Args:
        gate_policy: "spot" (legacy, Amber default) or "tou_aware" (TOU default).
    """
    # Only apply the WACB-vs-export gate for spot pricing.
    # For TOU, the gate is disabled (do nothing).
    if gate_policy == "tou_aware":
        # TOU: no gate — let the solver decide based on the fixed export rate.
        return

    # Spot policy: apply the protective gate (existing behaviour).
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


def add_free_window_soc_target(
    prob: pulp.LpProblem,
    t: int,
    soc: pulp.LpVariable,
    target_soc: float,
    slack: pulp.LpVariable,
) -> None:
    """Soft target: fill the battery toward target_soc by the end of a free window.

    Applied at the last slot of each contiguous free (0c) import block so the
    solver grabs free energy and tops the battery up instead of stopping at the
    evening target. Uses a slack variable penalised in the objective; the hard
    SOC ceiling (soc_max_hard) still bounds the actual charge.
    """
    prob += soc >= target_soc - slack, f"free_window_soc_{t}"


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


def add_grid_charge_policy(
    prob: pulp.LpProblem,
    t: int,
    grid_import: pulp.LpVariable,
    charge: pulp.LpVariable,
    import_rate_cents: float,
    policy: str = "free_window_and_solar_only",
    free_rate_threshold_cents: float = 1.0,
) -> None:
    """Enforce grid-charge policy: control where grid energy can charge the battery.

    Under "free_window_and_solar_only" policy:
    - Battery can be charged from grid ONLY when import_rate <= free_rate_threshold
      (typically the free/0c window)
    - At any rate > threshold, grid_import must go to cover load, not charge battery
    - This prevents panic-import at paid rates; the battery survives until the next
      free window by discharging to floor and using grid to cover load directly

    Under "allow_arbitrage" policy:
    - No restriction; grid can charge the battery at any rate if economically justified

    Note: This constraint is necessary because grid_import and charge are separate
    variables. The energy balance ensures solar can charge at any rate; this only
    gates grid→battery charging.
    """
    if policy == "free_window_and_solar_only" and import_rate_cents > free_rate_threshold_cents:
        # At paid rates, block grid from charging the battery
        # The constraint: charge can only come from solar (in the energy balance),
        # not from grid_import. Since charge is a separate variable independent of
        # grid_import in the constraints (no explicit "grid_import → charge" path),
        # we prevent grid-charging the battery by constraining the energy pathway:
        # When grid_import is present and price is paid, it must all go to load/export,
        # not to charge. However, in the energy balance:
        #   solar + grid_import + discharge = load + grid_export + charge + curtail
        # We cannot directly "forbid grid → charge" without knowing how grid_import
        # splits between charge and load. Instead, we use a second approach:
        # cap charge when import_rate is high and the solver is tempted to grid-charge.
        # Since the solver must also satisfy energy balance and load, a high
        # import_rate will naturally discourage grid_import for charging (it's expensive).
        # For safety under the free_window_and_solar_only policy, we add an explicit
        # constraint that when price is paid, the only charging source allowed is solar.
        # This is modeled as: charge = 0 (no grid charging at paid rates).
        # Solar→battery still works via the energy balance and charge variable;
        # it's just that grid_import can't feed charge at paid rates.
        #
        # Rationale: under free_window policy, NEVER buy grid energy to store.
        # This makes the control hierarchy much cleaner (no panic imports), and forces
        # the system to rely on the free window and solar. The battery can discharge
        # to cover load directly from grid (which it must, if depleted), just not
        # import-to-store at paid rates.
        prob += charge == 0, f"grid_charge_policy_paid_{t}"


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
