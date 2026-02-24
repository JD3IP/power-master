"""Billing cycle management."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


@dataclass
class BillingCycleSummary:
    """Summary of a billing cycle."""

    cycle_start: datetime
    cycle_end: datetime
    days_elapsed: int
    days_remaining: int
    total_import_cost_cents: int = 0
    total_export_revenue_cents: int = 0
    total_self_consumption_value_cents: int = 0
    total_arbitrage_profit_cents: int = 0
    total_fixed_costs_cents: int = 0
    net_cost_cents: int = 0


class BillingCycleManager:
    """Manages billing cycle boundaries and accumulation."""

    def __init__(self, billing_day: int = 1) -> None:
        self._billing_day = billing_day
        self._current: BillingCycleSummary | None = None

    @property
    def current(self) -> BillingCycleSummary | None:
        return self._current

    def get_or_create_cycle(self, now: datetime | None = None) -> BillingCycleSummary:
        """Get the current billing cycle, creating one if needed."""
        now = now or datetime.now(timezone.utc)
        start, end = self._cycle_boundaries(now)

        if self._current is not None:
            # Check if we're still in the same cycle
            if self._current.cycle_start <= now < self._current.cycle_end:
                days_elapsed = (now - self._current.cycle_start).days
                self._current.days_elapsed = days_elapsed
                self._current.days_remaining = max(0, (self._current.cycle_end - now).days)
                return self._current

        # New cycle
        days_total = (end - start).days
        days_elapsed = (now - start).days
        self._current = BillingCycleSummary(
            cycle_start=start,
            cycle_end=end,
            days_elapsed=days_elapsed,
            days_remaining=max(0, days_total - days_elapsed),
        )
        return self._current

    def record_import(self, cost_cents: int) -> None:
        """Record an import cost to the current cycle."""
        if self._current:
            self._current.total_import_cost_cents += cost_cents
            self._update_net()

    def record_export(self, revenue_cents: int) -> None:
        """Record export revenue to the current cycle."""
        if self._current:
            self._current.total_export_revenue_cents += revenue_cents
            self._update_net()

    def record_self_consumption(self, value_cents: int) -> None:
        """Record self-consumption value (avoided import)."""
        if self._current:
            self._current.total_self_consumption_value_cents += value_cents
            self._update_net()

    def record_arbitrage_profit(self, profit_cents: int) -> None:
        """Record arbitrage profit (buy low, sell high)."""
        if self._current:
            self._current.total_arbitrage_profit_cents += profit_cents
            self._update_net()

    def set_fixed_costs(self, fixed_costs_cents: int) -> None:
        """Set fixed costs for the current cycle."""
        if self._current:
            self._current.total_fixed_costs_cents = fixed_costs_cents
            self._update_net()

    def _update_net(self) -> None:
        if self._current:
            self._current.net_cost_cents = (
                self._current.total_import_cost_cents
                + self._current.total_fixed_costs_cents
                - self._current.total_export_revenue_cents
                - self._current.total_self_consumption_value_cents
                - self._current.total_arbitrage_profit_cents
            )

    def _cycle_boundaries(self, now: datetime) -> tuple[datetime, datetime]:
        """Calculate start and end of the current billing cycle."""
        year = now.year
        month = now.month

        # Cycle starts on billing_day of the current or previous month
        try:
            start = datetime(year, month, self._billing_day, tzinfo=timezone.utc)
        except ValueError:
            # Day doesn't exist in this month (e.g., Feb 30) — use last day
            if month == 12:
                next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                next_month = datetime(year, month + 1, 1, tzinfo=timezone.utc)
            start = next_month - timedelta(days=1)
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)

        if start > now:
            # We're before the billing day — cycle started last month
            if month == 1:
                prev_year, prev_month = year - 1, 12
            else:
                prev_year, prev_month = year, month - 1
            try:
                start = datetime(prev_year, prev_month, self._billing_day, tzinfo=timezone.utc)
            except ValueError:
                if prev_month == 12:
                    next_m = datetime(prev_year + 1, 1, 1, tzinfo=timezone.utc)
                else:
                    next_m = datetime(prev_year, prev_month + 1, 1, tzinfo=timezone.utc)
                start = next_m - timedelta(days=1)
                start = start.replace(hour=0, minute=0, second=0, microsecond=0)

        # End is billing_day of the next month
        if month == 12:
            end_year, end_month = year + 1, 1
        else:
            end_year, end_month = year, month + 1
        try:
            end = datetime(end_year, end_month, self._billing_day, tzinfo=timezone.utc)
        except ValueError:
            if end_month == 12:
                next_m = datetime(end_year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                next_m = datetime(end_year, end_month + 1, 1, tzinfo=timezone.utc)
            end = next_m - timedelta(days=1)
            end = end.replace(hour=0, minute=0, second=0, microsecond=0)

        if end <= now:
            # We've passed the end — advance to next cycle
            if end_month == 12:
                end = datetime(end_year + 1, 1, self._billing_day, tzinfo=timezone.utc)
            else:
                try:
                    end = datetime(end_year, end_month + 1, self._billing_day, tzinfo=timezone.utc)
                except ValueError:
                    end = datetime(end_year, end_month + 2, 1, tzinfo=timezone.utc) - timedelta(days=1)

        return start, end
