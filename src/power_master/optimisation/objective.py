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
    morning_soc_shortfall: float = 300.0
    daytime_soc_shortfall: float = 20.0  # Applied per-slot across many hours → strong cumulative effect
    # Small reward for self-consumption (cents/kWh equivalent)
    self_consume_reward: float = 0.5


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
    morning_soc_slack: list[pulp.LpVariable],
    daytime_soc_slack: list[pulp.LpVariable],
    weights: ObjectiveWeights | None = None,
) -> None:
    """Add the minimisation objective to the problem.

    Net Cost = SUM over t:
      + import_rate_t * grid_import_t * slot_hours  (import cost)
      - export_rate_t * grid_export_t * slot_hours  (export revenue)
      + hedging_rate * grid_import_t * slot_hours    (hedging cost)
      + penalties - rewards
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
        cost_terms.append(-export_rate[t] * grid_export[t] * kwh)
        # Self-consumption reward
        cost_terms.append(-w.self_consume_reward * self_consumed_solar[t] * kwh)

    # Penalty terms
    for t in range(n_slots):
        cost_terms.append(w.safety_violation * safety_slack[t])

    for t in range(len(storm_slack)):
        cost_terms.append(w.storm_violation * storm_slack[t])

    for t in range(len(evening_soc_slack)):
        cost_terms.append(w.evening_soc_shortfall * evening_soc_slack[t])

    for t in range(len(morning_soc_slack)):
        cost_terms.append(w.morning_soc_shortfall * morning_soc_slack[t])

    for t in range(len(daytime_soc_slack)):
        cost_terms.append(w.daytime_soc_shortfall * daytime_soc_slack[t])

    prob += pulp.lpSum(cost_terms), "MinimiseNetCost"
