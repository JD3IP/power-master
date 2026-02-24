"""Fixed energy cost calculations."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from power_master.config.schema import FixedCostsConfig

logger = logging.getLogger(__name__)


@dataclass
class FixedCostBreakdown:
    """Breakdown of fixed costs for a billing period."""

    supply_charge_cents: int
    access_fee_cents: int
    hedging_cents: int
    total_cents: int


def calculate_fixed_costs(
    config: FixedCostsConfig,
    days_in_cycle: int,
    total_consumption_kwh: float,
) -> FixedCostBreakdown:
    """Calculate fixed costs for a billing cycle.

    Args:
        config: Fixed cost configuration.
        days_in_cycle: Number of days in the billing cycle.
        total_consumption_kwh: Total consumption for hedging calculation.
    """
    supply = config.monthly_supply_charge_cents
    access = config.daily_access_fee_cents * days_in_cycle
    hedging = int(total_consumption_kwh * config.hedging_per_kwh_cents)
    total = supply + access + hedging

    return FixedCostBreakdown(
        supply_charge_cents=supply,
        access_fee_cents=access,
        hedging_cents=hedging,
        total_cents=total,
    )


def daily_arbitrage_target(
    config: FixedCostsConfig,
    days_in_cycle: int,
    estimated_daily_consumption_kwh: float,
) -> float:
    """Calculate daily arbitrage target to offset fixed costs.

    Returns the daily profit target in cents needed to offset fixed costs.
    """
    supply_daily = config.monthly_supply_charge_cents / max(days_in_cycle, 1)
    access_daily = config.daily_access_fee_cents
    hedging_daily = estimated_daily_consumption_kwh * config.hedging_per_kwh_cents

    return supply_daily + access_daily + hedging_daily
