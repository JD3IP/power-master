"""Per-event P&L tracking."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class AccountingEvent:
    """A single accounting event (import, export, self-consumption, etc.)."""

    event_type: str  # "grid_import", "grid_export", "self_consumption", "arbitrage"
    energy_wh: int
    rate_cents: float  # c/kWh
    cost_cents: int  # Positive = cost, negative = revenue
    cost_basis_cents: int = 0  # WACB-based cost of discharged energy
    profit_loss_cents: int = 0  # For arbitrage: revenue - cost_basis
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def create_import_event(energy_wh: int, rate_cents: float) -> AccountingEvent:
    """Create an event for grid import (cost)."""
    kwh = energy_wh / 1000
    cost = int(kwh * rate_cents)
    return AccountingEvent(
        event_type="grid_import",
        energy_wh=energy_wh,
        rate_cents=rate_cents,
        cost_cents=cost,
    )


def create_export_event(energy_wh: int, rate_cents: float, cost_basis_cents: int = 0) -> AccountingEvent:
    """Create an event for grid export (revenue)."""
    kwh = energy_wh / 1000
    revenue = int(kwh * rate_cents)
    profit = revenue - cost_basis_cents
    return AccountingEvent(
        event_type="grid_export",
        energy_wh=energy_wh,
        rate_cents=rate_cents,
        cost_cents=-revenue,  # Negative = revenue
        cost_basis_cents=cost_basis_cents,
        profit_loss_cents=profit,
    )


def create_self_consumption_event(energy_wh: int, avoided_rate_cents: float) -> AccountingEvent:
    """Create an event for self-consumption (avoided import cost)."""
    kwh = energy_wh / 1000
    value = int(kwh * avoided_rate_cents)
    return AccountingEvent(
        event_type="self_consumption",
        energy_wh=energy_wh,
        rate_cents=avoided_rate_cents,
        cost_cents=-value,  # Negative = savings
    )
