"""Objective function for the MILP solver.

Primary objective: Minimise net billing cost over the planning horizon.
"""

from __future__ import annotations

from dataclasses import dataclass

import pulp


@dataclass
class ObjectiveWeights:
    """Penalty/reward weights for the objective function.

    Cost terms in the objective are in cents (after Wh→kWh conversion).
    Penalty weights compete directly with cent values, so a weight of 500
    means "missing 1 unit of SOC costs 500 cents in the objective".
    """

    # High penalties for constraint violations
    safety_violation: float = 1e6
    storm_violation: float = 1e4
    load_miss: float = 1e1
    # Soft target penalties (cents per unit SOC shortfall)
    evening_soc_shortfall: float = 500.0
    # Free-window fill reward: pull SOC up to the free-window target while import
    # is free. Weaker than the evening target and safety/storm so it never
    # overrides them, but strong enough to overcome the round-trip-efficiency
    # disincentive to holding extra charge.
    free_window_soc_shortfall: float = 200.0
    morning_soc_shortfall: float = 300.0
    daytime_soc_shortfall: float = 20.0  # Applied per-slot across many hours → strong cumulative effect
    # Small reward for self-consumption (cents/kWh equivalent)
    self_consume_reward: float = 0.5
    # Early-charge bias: small cost per slot index to prefer charging earlier
    # in the horizon when prices are equal (cents/kWh per slot position)
    early_charge_bias: float = 0.02


def build_objective(
    prob: pulp.LpProblem,
    n_slots: int,
    slot_hours: float,
    import_rate: list[float],
    export_rate: list[float],
    hedging_rate: float,
    grid_import: list[pulp.LpVariable],
    grid_export: list[pulp.LpVariable],
    self_consumed_solar: list[pulp.LpVariable],
    safety_slack: list[pulp.LpVariable],
    storm_slack: list[pulp.LpVariable],
    evening_soc_slack: list[pulp.LpVariable],
    free_window_soc_slack: list[pulp.LpVariable],
    morning_soc_slack: list[pulp.LpVariable],
    daytime_soc_slack: list[pulp.LpVariable],
    export_tier_vars: list[list[pulp.LpVariable]] | None = None,
    tier_structs: list[any] | None = None,
    weights: ObjectiveWeights | None = None,
    incumbent_export_bias_cents: float = 0.0,
    credit_missed_vars: dict | None = None,
    credit_windows: list[any] | None = None,
    credit_slack: list[pulp.LpVariable] | None = None,
) -> None:
    """Add the minimisation objective to the problem.

    Net Cost = SUM over t:
      + import_rate_t * grid_import_t * slot_hours  (import cost)
      - export_revenue_t * slot_hours               (export revenue, tiered or flat)
      + hedging_rate * grid_import_t * slot_hours    (hedging cost)
      + penalties - rewards
      + credit penalties (soft) + credit slack penalties (hard)

    For tiered export: revenue = SUM_k (tier_k_rate * export_tier_k[t]).
    For flat export: revenue = export_rate[t] * grid_export[t] (backward compat).
    """
    w = weights or ObjectiveWeights()

    cost_terms = []
    for t in range(n_slots):
        # Energy per slot in kWh: power_W * slot_hours / 1000
        # All cost terms are in cents: rate_cents_per_kWh * kWh
        kwh = slot_hours / 1000  # multiply by power_W later via LP variable
        # Import cost
        cost_terms.append(import_rate[t] * grid_import[t] * kwh)
        # Hedging cost on all imports
        cost_terms.append(hedging_rate * grid_import[t] * kwh)

        # Export revenue (subtract = good)
        # Check if this slot has tiered export
        has_tiers = (
            export_tier_vars
            and t < len(export_tier_vars)
            and export_tier_vars[t]
            and tier_structs
            and t < len(tier_structs)
            and tier_structs[t].in_tiered_window
        )

        if has_tiers:
            # Tiered export revenue: sum_k (tier_rate_k * export_tier_k[t])
            for k in range(len(export_tier_vars[t])):
                tier = tier_structs[t].tiers[k]
                tier_rate = tier.rate_c_per_kwh
                cost_terms.append(-tier_rate * export_tier_vars[t][k] * kwh)
        else:
            # Flat export revenue (backward compat for non-tiered plans)
            cost_terms.append(-export_rate[t] * grid_export[t] * kwh)

        # Self-consumption reward
        cost_terms.append(-w.self_consume_reward * self_consumed_solar[t] * kwh)
        # Early-charge bias: penalise later grid imports slightly so the solver
        # prefers to charge earlier in the window when prices are otherwise equal
        cost_terms.append(w.early_charge_bias * t * grid_import[t] * kwh)

    # Penalty terms
    for t in range(n_slots):
        cost_terms.append(w.safety_violation * safety_slack[t])

    for t in range(len(storm_slack)):
        cost_terms.append(w.storm_violation * storm_slack[t])

    for t in range(len(evening_soc_slack)):
        cost_terms.append(w.evening_soc_shortfall * evening_soc_slack[t])

    for t in range(len(free_window_soc_slack)):
        cost_terms.append(w.free_window_soc_shortfall * free_window_soc_slack[t])

    for t in range(len(morning_soc_slack)):
        cost_terms.append(w.morning_soc_shortfall * morning_soc_slack[t])

    for t in range(len(daytime_soc_slack)):
        cost_terms.append(w.daytime_soc_shortfall * daytime_soc_slack[t])

    # Low-import credit penalty/reward terms (Phase 2)
    # Soft enforcement: penalise missed credit; reward earned credit (scaled by priority weight)
    if credit_missed_vars:
        for (credit_name, local_date), missed_var in credit_missed_vars.items():
            # Find one representative window to get reward and priority weight
            cw = None
            if credit_windows:
                for c in credit_windows:
                    if c and c.credit_name == credit_name and c.local_date == local_date:
                        cw = c
                        break
            if cw:
                # Convert reward dollars to cents for objective consistency
                reward_cents = cw.reward_dollars_per_day * 100.0
                # Missed var = 1: lose the reward (cost += reward); missed var = 0: earn reward (cost -= reward)
                # With credit_priority_weight scaling: lower weight = less aggressively pursue the credit
                weighted_reward = reward_cents * cw.credit_priority_weight
                cost_terms.append(weighted_reward * missed_var)

    # Hard enforcement: penalise slack (grid import during hard-constraint windows)
    if credit_slack:
        # Large penalty for violating hard constraints (similar to safety_slack)
        for cs in credit_slack:
            cost_terms.append(w.safety_violation * cs)

    # Status-quo tie-break (hysteresis): bias slot-0 export toward incumbent mode.
    # Positive bias discourages exporting (hold SELF_USE); negative bias encourages exporting (hold FORCE_DISCHARGE).
    # Only applied when incumbent is set and hysteresis is enabled; default 0.0 keeps behaviour unchanged.
    if incumbent_export_bias_cents != 0.0 and n_slots > 0:
        kwh_slot0 = slot_hours / 1000
        cost_terms.append(incumbent_export_bias_cents * grid_export[0] * kwh_slot0)

    prob += pulp.lpSum(cost_terms), "MinimiseNetCost"
