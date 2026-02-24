"""Tests for database engine and repository."""

from __future__ import annotations

import pytest

from power_master.db.repository import Repository


@pytest.mark.asyncio
class TestRepository:
    async def test_store_and_get_telemetry(self, repo: Repository) -> None:
        row_id = await repo.store_telemetry(
            soc=0.72,
            battery_power_w=-2500,
            solar_power_w=4200,
            grid_power_w=-1100,
            load_power_w=1600,
        )
        assert row_id > 0

        latest = await repo.get_latest_telemetry()
        assert latest is not None
        assert latest["soc"] == 0.72
        assert latest["battery_power_w"] == -2500
        assert latest["solar_power_w"] == 4200

    async def test_store_and_get_plan(self, repo: Repository) -> None:
        version = await repo.get_next_plan_version()
        assert version == 1

        plan_id = await repo.store_plan(
            version=1,
            trigger_reason="startup",
            horizon_start="2026-02-23T00:00:00Z",
            horizon_end="2026-02-25T00:00:00Z",
            objective_score=42.5,
            solver_time_ms=850,
            metrics={"projected_cost": 1500},
            active_constraints=["safety", "storm"],
        )
        assert plan_id > 0

        active = await repo.get_active_plan()
        assert active is not None
        assert active["version"] == 1
        assert active["trigger_reason"] == "startup"

    async def test_plan_supersedes_previous(self, repo: Repository) -> None:
        await repo.store_plan(
            version=1, trigger_reason="startup",
            horizon_start="2026-02-23T00:00:00Z",
            horizon_end="2026-02-25T00:00:00Z",
            objective_score=10.0, solver_time_ms=100,
            metrics={}, active_constraints=[],
        )
        await repo.store_plan(
            version=2, trigger_reason="tariff_change",
            horizon_start="2026-02-23T00:00:00Z",
            horizon_end="2026-02-25T00:00:00Z",
            objective_score=20.0, solver_time_ms=200,
            metrics={}, active_constraints=[],
        )

        active = await repo.get_active_plan()
        assert active is not None
        assert active["version"] == 2

        history = await repo.get_plan_history()
        assert len(history) == 2
        assert history[0]["status"] == "active"
        assert history[1]["status"] == "superseded"

    async def test_store_and_get_forecast(self, repo: Repository) -> None:
        fid = await repo.store_forecast(
            provider_type="solar",
            provider_name="forecast_solar",
            horizon_start="2026-02-23T00:00:00Z",
            horizon_end="2026-02-25T00:00:00Z",
            data={"forecasts": []},
            confidence_score=0.85,
        )
        assert fid > 0

        latest = await repo.get_latest_forecast("solar")
        assert latest is not None
        assert latest["provider_name"] == "forecast_solar"
        assert latest["confidence_score"] == 0.85

    async def test_billing_cycle(self, repo: Repository) -> None:
        cycle_id = await repo.create_billing_cycle(
            "2026-02-01T00:00:00Z", "2026-02-28T23:59:59Z"
        )
        assert cycle_id > 0

        active = await repo.get_active_billing_cycle()
        assert active is not None
        assert active["status"] == "active"

        await repo.close_billing_cycle(cycle_id)
        closed = await repo.get_active_billing_cycle()
        assert closed is None

    async def test_log_command(self, repo: Repository) -> None:
        cmd_id = await repo.log_command(
            command_type="set_power",
            parameters={"power_w": 5000, "mode": "discharge"},
            source_reason="plan_execution",
        )
        assert cmd_id > 0

    async def test_store_historical(self, repo: Repository) -> None:
        await repo.store_historical("load", 1500.0, "telemetry")
        await repo.store_historical("load", 1600.0, "telemetry")

        data = await repo.get_historical(
            "load", "2000-01-01T00:00:00Z", "2099-12-31T23:59:59Z"
        )
        assert len(data) == 2

    async def test_spike_events(self, repo: Repository) -> None:
        spike_id = await repo.store_spike_event(
            peak_price_cents=350,
            trigger_price_cents=100,
            response_mode="aggressive",
        )
        assert spike_id > 0

        active = await repo.get_active_spike()
        assert active is not None
        assert active["peak_price_cents"] == 350

    async def test_system_event(self, repo: Repository) -> None:
        event_id = await repo.log_system_event(
            event_type="mode_change",
            source_module="control.loop",
            details={"from": "NORMAL", "to": "DEGRADED_FORECAST"},
            operating_mode="DEGRADED_FORECAST",
        )
        assert event_id > 0

    async def test_optimisation_cycle_log(self, repo: Repository) -> None:
        log_id = await repo.log_optimisation_cycle(
            trigger_reason="periodic",
            rebuild_performed=False,
            soc_at_evaluation=0.65,
            active_constraints=["safety"],
            reserve_state={"active": False},
            forecast_delta={"solar_delta_pct": 5.0},
            outcome="no_rebuild",
        )
        assert log_id > 0
