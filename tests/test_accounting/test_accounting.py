"""Tests for accounting engine, WACB, billing cycles, events, and fixed costs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from power_master.accounting.billing_cycle import BillingCycleManager
from power_master.accounting.cost_basis import CostBasisTracker
from power_master.accounting.engine import AccountingEngine
from power_master.accounting.events import (
    create_export_event,
    create_import_event,
    create_self_consumption_event,
)
from power_master.accounting.fixed_costs import (
    calculate_fixed_costs,
    daily_arbitrage_target,
)
from power_master.config.schema import AppConfig, FixedCostsConfig


# ── WACB / Cost Basis Tests ──────────────────────────────────


class TestCostBasis:
    def test_initial_wacb(self) -> None:
        tracker = CostBasisTracker(capacity_wh=10000, initial_soc=0.5, initial_wacb=10.0)
        assert tracker.wacb_cents == 10.0
        assert tracker.state.stored_wh == 5000

    def test_charge_updates_wacb(self) -> None:
        tracker = CostBasisTracker(capacity_wh=10000, initial_soc=0.5, initial_wacb=10.0)
        # Charge 2000Wh (2kWh) at 5c/kWh
        tracker.record_charge(2000, 5.0)

        # Previous: 5kWh at 10c = 50c
        # New: 2kWh at 5c = 10c
        # Total: 7kWh at 60c → WACB = 60/7 ≈ 8.57
        assert abs(tracker.wacb_cents - (60 / 7)) < 0.01

    def test_charge_from_pv_uses_feed_in_rate(self) -> None:
        tracker = CostBasisTracker(capacity_wh=10000, initial_soc=0.0, initial_wacb=0.0)
        # Charge 5000Wh from PV at feed-in rate of 7c/kWh (opportunity cost)
        tracker.record_charge(5000, 7.0)
        assert tracker.wacb_cents == 7.0

    def test_discharge_returns_cost_basis(self) -> None:
        tracker = CostBasisTracker(capacity_wh=10000, initial_soc=0.5, initial_wacb=10.0)
        # Discharge 1000Wh (1kWh) — cost basis = 1 * 10 = 10c
        cost = tracker.record_discharge(1000)
        assert cost == 10.0

    def test_discharge_doesnt_change_wacb(self) -> None:
        tracker = CostBasisTracker(capacity_wh=10000, initial_soc=0.5, initial_wacb=10.0)
        tracker.record_discharge(1000)
        assert tracker.wacb_cents == 10.0

    def test_zero_charge_ignored(self) -> None:
        tracker = CostBasisTracker(capacity_wh=10000, initial_soc=0.5, initial_wacb=10.0)
        tracker.record_charge(0, 5.0)
        assert tracker.wacb_cents == 10.0

    def test_stored_value(self) -> None:
        tracker = CostBasisTracker(capacity_wh=10000, initial_soc=0.5, initial_wacb=10.0)
        # 5000Wh = 5kWh at 10c/kWh = 50c
        assert tracker.stored_value_cents == 50.0

    def test_sync_soc(self) -> None:
        tracker = CostBasisTracker(capacity_wh=10000, initial_soc=0.5, initial_wacb=10.0)
        tracker.sync_soc(0.8)
        assert tracker.state.stored_wh == 8000


# ── Fixed Costs Tests ─────────────────────────────────────────


class TestFixedCosts:
    def test_basic_calculation(self) -> None:
        config = FixedCostsConfig(
            monthly_supply_charge_cents=9000,
            daily_access_fee_cents=100,
            hedging_per_kwh_cents=2,
        )
        result = calculate_fixed_costs(config, days_in_cycle=30, total_consumption_kwh=600)

        assert result.supply_charge_cents == 9000
        assert result.access_fee_cents == 3000  # 30 * 100
        assert result.hedging_cents == 1200  # 600 * 2
        assert result.total_cents == 13200

    def test_daily_target(self) -> None:
        config = FixedCostsConfig(
            monthly_supply_charge_cents=9000,
            daily_access_fee_cents=100,
            hedging_per_kwh_cents=2,
        )
        target = daily_arbitrage_target(config, days_in_cycle=30, estimated_daily_consumption_kwh=20)

        # 9000/30 + 100 + 20*2 = 300 + 100 + 40 = 440
        assert target == 440.0


# ── Billing Cycle Tests ───────────────────────────────────────


class TestBillingCycle:
    def test_create_cycle(self) -> None:
        manager = BillingCycleManager(billing_day=1)
        now = datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc)
        cycle = manager.get_or_create_cycle(now)

        assert cycle.cycle_start.day == 1
        assert cycle.cycle_start.month == 2
        assert cycle.days_elapsed == 14

    def test_record_import(self) -> None:
        manager = BillingCycleManager(billing_day=1)
        manager.get_or_create_cycle()
        manager.record_import(500)

        assert manager.current is not None
        assert manager.current.total_import_cost_cents == 500
        assert manager.current.net_cost_cents == 500

    def test_record_export(self) -> None:
        manager = BillingCycleManager(billing_day=1)
        manager.get_or_create_cycle()
        manager.record_export(200)

        assert manager.current is not None
        assert manager.current.total_export_revenue_cents == 200
        assert manager.current.net_cost_cents == -200

    def test_net_cost_calculation(self) -> None:
        manager = BillingCycleManager(billing_day=1)
        manager.get_or_create_cycle()
        manager.record_import(1000)
        manager.record_export(300)
        manager.record_self_consumption(200)
        manager.set_fixed_costs(500)

        # net = import + fixed - export - self_consume - arbitrage
        # net = 1000 + 500 - 300 - 200 - 0 = 1000
        assert manager.current.net_cost_cents == 1000

    def test_same_cycle_reused(self) -> None:
        manager = BillingCycleManager(billing_day=1)
        now = datetime(2026, 2, 15, tzinfo=timezone.utc)
        c1 = manager.get_or_create_cycle(now)
        c2 = manager.get_or_create_cycle(now + timedelta(hours=1))
        assert c1 is c2


# ── Events Tests ──────────────────────────────────────────────


class TestEvents:
    def test_import_event(self) -> None:
        event = create_import_event(energy_wh=2000, rate_cents=20.0)
        assert event.event_type == "grid_import"
        assert event.cost_cents == 40  # 2kWh * 20c

    def test_export_event(self) -> None:
        event = create_export_event(energy_wh=1000, rate_cents=8.0, cost_basis_cents=5)
        assert event.event_type == "grid_export"
        assert event.cost_cents == -8  # Revenue
        assert event.profit_loss_cents == 3  # 8 - 5

    def test_self_consumption_event(self) -> None:
        event = create_self_consumption_event(energy_wh=3000, avoided_rate_cents=25.0)
        assert event.event_type == "self_consumption"
        assert event.cost_cents == -75  # Savings (3kWh * 25c)


# ── Engine Integration Tests ──────────────────────────────────


class TestAccountingEngine:
    def test_engine_tracks_wacb(self) -> None:
        config = AppConfig()
        engine = AccountingEngine(config, initial_soc=0.5, initial_wacb=10.0)

        engine.record_grid_charge(2000, 5.0)  # 2kWh at 5c
        assert engine.wacb_cents < 10.0  # Should decrease (cheaper charge)

    def test_engine_records_import(self) -> None:
        config = AppConfig()
        engine = AccountingEngine(config, initial_soc=0.5, initial_wacb=10.0)

        event = engine.record_grid_import(2000, 20.0)
        assert event.cost_cents == 40

        cycle = engine.billing.current
        assert cycle is not None
        assert cycle.total_import_cost_cents == 40

    def test_engine_records_export_with_pnl(self) -> None:
        config = AppConfig()
        engine = AccountingEngine(config, initial_soc=0.5, initial_wacb=10.0)

        event = engine.record_grid_export(1000, 25.0)

        # Cost basis: 1kWh * 10c = 10c
        # Revenue: 1kWh * 25c = 25c
        # Profit: 25 - 10 = 15c
        assert event.profit_loss_cents == 15

    def test_engine_records_self_consumption(self) -> None:
        config = AppConfig()
        engine = AccountingEngine(config, initial_soc=0.5, initial_wacb=10.0)

        event = engine.record_self_consumption(5000, 20.0)
        assert event.cost_cents == -100  # 5kWh * 20c savings

    def test_engine_summary(self) -> None:
        config = AppConfig()
        engine = AccountingEngine(config, initial_soc=0.5, initial_wacb=10.0)

        engine.record_grid_import(2000, 20.0)
        summary = engine.get_summary()

        assert summary.wacb_cents == 10.0  # Import doesn't change WACB
        assert summary.cycle is not None
        assert summary.daily_target_cents > 0
        assert summary.events_today == 1

    def test_engine_sync_soc(self) -> None:
        config = AppConfig()
        engine = AccountingEngine(config, initial_soc=0.5, initial_wacb=10.0)

        engine.sync_soc(0.8)
        assert engine.cost_basis.state.stored_wh == 8000
