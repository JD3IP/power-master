"""Main accounting orchestrator — ties together WACB, billing, events, fixed costs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from power_master.accounting.billing_cycle import BillingCycleManager, BillingCycleSummary
from power_master.accounting.cost_basis import CostBasisTracker
from power_master.accounting.events import (
    AccountingEvent,
    create_export_event,
    create_import_event,
    create_self_consumption_event,
)
from power_master.accounting.fixed_costs import calculate_fixed_costs, daily_arbitrage_target
from power_master.config.schema import AppConfig

logger = logging.getLogger(__name__)


@dataclass
class AccountingSummary:
    """Summary of current accounting state."""

    wacb_cents: float
    stored_value_cents: float
    daily_target_cents: float
    cycle: BillingCycleSummary | None
    events_today: int = 0
    today_net_cost_cents: int = 0
    week_net_cost_cents: int = 0


class AccountingEngine:
    """Orchestrates all financial tracking for the system.

    Called by the control loop to record energy flows and compute P&L.
    """

    def __init__(self, config: AppConfig, initial_soc: float = 0.5, initial_wacb: float = 0.0) -> None:
        self._config = config
        self._cost_basis = CostBasisTracker(
            config.battery.capacity_wh, initial_soc, initial_wacb,
        )
        self._billing = BillingCycleManager(config.accounting.billing_cycle_day)
        self._events: list[AccountingEvent] = []

    @property
    def wacb_cents(self) -> float:
        return self._cost_basis.wacb_cents

    @property
    def cost_basis(self) -> CostBasisTracker:
        return self._cost_basis

    @property
    def billing(self) -> BillingCycleManager:
        return self._billing

    def record_grid_import(self, energy_wh: int, rate_cents: float) -> AccountingEvent:
        """Record grid import: cost to buy + WACB update if charging."""
        event = create_import_event(energy_wh, rate_cents)
        self._events.append(event)

        cycle = self._billing.get_or_create_cycle()
        self._billing.record_import(event.cost_cents)

        return event

    def record_grid_charge(self, energy_wh: int, rate_cents: float) -> None:
        """Record charging from grid — updates WACB."""
        self._cost_basis.record_charge(energy_wh, rate_cents)

    def record_solar_charge(self, energy_wh: int, feed_in_rate_cents: float) -> None:
        """Record charging from PV — updates WACB using opportunity cost."""
        self._cost_basis.record_charge(energy_wh, feed_in_rate_cents)

    def record_grid_export(self, energy_wh: int, rate_cents: float) -> AccountingEvent:
        """Record grid export: revenue + P&L calculation."""
        cost_basis = round(self._cost_basis.record_discharge(energy_wh))
        event = create_export_event(energy_wh, rate_cents, cost_basis)
        self._events.append(event)

        cycle = self._billing.get_or_create_cycle()
        self._billing.record_export(abs(event.cost_cents))

        if event.profit_loss_cents > 0:
            self._billing.record_arbitrage_profit(event.profit_loss_cents)

        return event

    def record_self_consumption(self, energy_wh: int, avoided_rate_cents: float) -> AccountingEvent:
        """Record self-consumption: savings from using PV/battery instead of grid."""
        event = create_self_consumption_event(energy_wh, avoided_rate_cents)
        self._events.append(event)

        cycle = self._billing.get_or_create_cycle()
        self._billing.record_self_consumption(abs(event.cost_cents))

        return event

    def sync_soc(self, soc: float) -> None:
        """Sync WACB tracker with actual SOC reading."""
        self._cost_basis.sync_soc(soc)

    def _net_cost_since(self, since: datetime) -> int:
        """Sum net cost_cents for events after *since*."""
        return sum(e.cost_cents for e in self._events if e.timestamp >= since)

    def get_summary(self) -> AccountingSummary:
        """Get current accounting summary."""
        cycle = self._billing.get_or_create_cycle()
        daily_target = daily_arbitrage_target(
            self._config.fixed_costs,
            days_in_cycle=cycle.days_elapsed + cycle.days_remaining if cycle else 30,
            estimated_daily_consumption_kwh=20.0,  # Default estimate
        )

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=now.weekday())

        return AccountingSummary(
            wacb_cents=self._cost_basis.wacb_cents,
            stored_value_cents=self._cost_basis.stored_value_cents,
            daily_target_cents=daily_target,
            cycle=cycle,
            events_today=len([e for e in self._events if e.timestamp >= today_start]),
            today_net_cost_cents=self._net_cost_since(today_start),
            week_net_cost_cents=self._net_cost_since(week_start),
        )

    def get_recent_events(self, count: int = 50) -> list[AccountingEvent]:
        """Get the most recent accounting events."""
        return self._events[-count:]
