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


KV_WACB_KEY = "wacb_state"
KV_BILLING_KEY = "billing_cycle_state"


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
        self._repo = None

    async def init_persistence(self, repo) -> None:
        """Load persisted accounting state and wire up auto-save."""
        self._repo = repo

        # Ensure kv_store table exists (handles existing DBs pre-migration)
        await repo.db.execute("""
            CREATE TABLE IF NOT EXISTS kv_store (
                key         TEXT PRIMARY KEY,
                value_json  TEXT NOT NULL,
                updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )
        """)
        await repo.db.commit()

        # ── Restore WACB state ──
        saved = await repo.kv_get(KV_WACB_KEY)
        if saved:
            self._cost_basis.restore_state(
                wacb_cents=saved.get("wacb_cents", 0.0),
                stored_wh=saved.get("stored_wh", 0.0),
                total_charged_wh=saved.get("total_charged_wh", 0.0),
                total_cost_cents=saved.get("total_cost_cents", 0.0),
            )
        else:
            logger.info("No persisted WACB state found, starting fresh")

        # ── Restore billing cycle totals ──
        billing_saved = await repo.kv_get(KV_BILLING_KEY)
        if billing_saved:
            cycle = self._billing.get_or_create_cycle()
            saved_start = billing_saved.get("cycle_start", "")
            # Only restore if we're still in the same billing cycle
            if saved_start == cycle.cycle_start.isoformat():
                cycle.total_import_cost_cents = billing_saved.get("total_import_cost_cents", 0)
                cycle.total_export_revenue_cents = billing_saved.get("total_export_revenue_cents", 0)
                cycle.total_self_consumption_value_cents = billing_saved.get("total_self_consumption_value_cents", 0)
                cycle.total_arbitrage_profit_cents = billing_saved.get("total_arbitrage_profit_cents", 0)
                cycle.total_fixed_costs_cents = billing_saved.get("total_fixed_costs_cents", 0)
                cycle.net_cost_cents = billing_saved.get("net_cost_cents", 0)
                logger.info(
                    "Billing cycle restored: net=%dc import=%dc export=%dc",
                    cycle.net_cost_cents, cycle.total_import_cost_cents,
                    cycle.total_export_revenue_cents,
                )
            else:
                logger.info("Billing cycle boundary crossed since last run, starting fresh cycle")

        # ── Restore recent events from DB for today/week calculations ──
        now = datetime.now(timezone.utc)
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        db_events = await repo.get_accounting_events_since(week_start.isoformat())
        for row in reversed(db_events):  # oldest first
            self._events.append(AccountingEvent(
                event_type=row["event_type"],
                energy_wh=row["energy_wh"],
                rate_cents=row.get("rate_cents", 0) or 0,
                cost_cents=row.get("cost_cents", 0) or 0,
                cost_basis_cents=row.get("cost_basis_cents", 0) or 0,
                profit_loss_cents=row.get("profit_loss_cents", 0) or 0,
                timestamp=datetime.fromisoformat(row["started_at"]),
            ))
        if self._events:
            logger.info("Restored %d accounting events from this week", len(self._events))

        # ── Wire up WACB auto-save ──
        import asyncio

        def _on_wacb_change(state):
            asyncio.ensure_future(self._save_wacb(state))

        self._cost_basis.set_on_change(_on_wacb_change)

    async def _save_wacb(self, state) -> None:
        """Persist current WACB state to the database."""
        if self._repo is None:
            return
        try:
            await self._repo.kv_set(KV_WACB_KEY, {
                "wacb_cents": state.wacb_cents,
                "stored_wh": state.stored_wh,
                "total_charged_wh": state.total_charged_wh,
                "total_cost_cents": state.total_cost_cents,
            })
        except Exception:
            logger.warning("Failed to persist WACB state", exc_info=True)

    async def _save_billing_cycle(self) -> None:
        """Persist current billing cycle totals."""
        if self._repo is None:
            return
        cycle = self._billing.current
        if cycle is None:
            return
        try:
            await self._repo.kv_set(KV_BILLING_KEY, {
                "cycle_start": cycle.cycle_start.isoformat(),
                "total_import_cost_cents": cycle.total_import_cost_cents,
                "total_export_revenue_cents": cycle.total_export_revenue_cents,
                "total_self_consumption_value_cents": cycle.total_self_consumption_value_cents,
                "total_arbitrage_profit_cents": cycle.total_arbitrage_profit_cents,
                "total_fixed_costs_cents": cycle.total_fixed_costs_cents,
                "net_cost_cents": cycle.net_cost_cents,
            })
        except Exception:
            logger.warning("Failed to persist billing cycle", exc_info=True)

    async def _persist_event(self, event: AccountingEvent) -> None:
        """Write an accounting event to the database."""
        if self._repo is None:
            return
        try:
            await self._repo.store_accounting_event(
                event_type=event.event_type,
                energy_wh=event.energy_wh,
                cost_cents=event.cost_cents,
                rate_cents=int(event.rate_cents),
                cost_basis_cents=event.cost_basis_cents,
                profit_loss_cents=event.profit_loss_cents,
            )
        except Exception:
            logger.warning("Failed to persist accounting event", exc_info=True)

    @property
    def wacb_cents(self) -> float:
        return self._cost_basis.wacb_cents

    @property
    def cost_basis(self) -> CostBasisTracker:
        return self._cost_basis

    @property
    def billing(self) -> BillingCycleManager:
        return self._billing

    async def record_grid_import(self, energy_wh: int, rate_cents: float) -> AccountingEvent:
        """Record grid import: cost to buy + WACB update if charging."""
        event = create_import_event(energy_wh, rate_cents)
        self._events.append(event)

        cycle = self._billing.get_or_create_cycle()
        self._billing.record_import(event.cost_cents)

        await self._persist_event(event)
        await self._save_billing_cycle()
        return event

    def record_grid_charge(self, energy_wh: int, rate_cents: float) -> None:
        """Record charging from grid — updates WACB."""
        self._cost_basis.record_charge(energy_wh, rate_cents)

    def record_solar_charge(self, energy_wh: int, feed_in_rate_cents: float) -> None:
        """Record charging from PV — updates WACB using opportunity cost."""
        self._cost_basis.record_charge(energy_wh, feed_in_rate_cents)

    async def record_grid_export(self, energy_wh: int, rate_cents: float) -> AccountingEvent:
        """Record grid export: revenue + P&L calculation."""
        cost_basis = round(self._cost_basis.record_discharge(energy_wh))
        event = create_export_event(energy_wh, rate_cents, cost_basis)
        self._events.append(event)

        cycle = self._billing.get_or_create_cycle()
        self._billing.record_export(abs(event.cost_cents))

        if event.profit_loss_cents > 0:
            self._billing.record_arbitrage_profit(event.profit_loss_cents)

        await self._persist_event(event)
        await self._save_billing_cycle()
        return event

    async def record_self_consumption(self, energy_wh: int, avoided_rate_cents: float) -> AccountingEvent:
        """Record self-consumption: savings from using PV/battery instead of grid."""
        event = create_self_consumption_event(energy_wh, avoided_rate_cents)
        self._events.append(event)

        cycle = self._billing.get_or_create_cycle()
        self._billing.record_self_consumption(abs(event.cost_cents))

        await self._persist_event(event)
        await self._save_billing_cycle()
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
