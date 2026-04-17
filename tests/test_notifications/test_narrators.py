"""Narrator unit tests — pure functions of plan + context."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from power_master.notifications.narrators import (
    NarratorContext,
    generate_daily_briefing,
    narrate_force_charge_triggered,
    narrate_grid_outage,
    narrate_price_spike,
    narrate_price_spike_resolved,
    narrate_storm_plan_active,
    narrate_storm_resolved,
    render_plain,
)
from power_master.optimisation.plan import OptimisationPlan, PlanSlot, SlotMode


def _plan_with(mode: SlotMode, start_offset_min: int = 0) -> OptimisationPlan:
    now = datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc)
    start = now + timedelta(minutes=start_offset_min)
    slots = [PlanSlot(
        index=0,
        start=start,
        end=start + timedelta(minutes=30),
        mode=mode,
        target_power_w=3000,
        expected_soc=0.75,
        import_rate_cents=2.0 if mode == SlotMode.FORCE_CHARGE else 50.0,
    )]
    return OptimisationPlan(
        version=1,
        created_at=now,
        trigger_reason="test",
        horizon_start=start,
        horizon_end=start + timedelta(hours=1),
        slots=slots,
        objective_score=0.0,
        solver_time_ms=10,
    )


class TestStormNarrator:
    def test_includes_reserve_and_charge_window(self) -> None:
        now = datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc)
        plan = _plan_with(SlotMode.FORCE_CHARGE)
        ctx = NarratorContext(
            now=now,
            current_soc=0.40,
            storm_active=True,
            storm_reserve_soc=0.80,
            storm_window_start=now + timedelta(hours=8),
            storm_window_end=now + timedelta(hours=12),
        )
        action = narrate_storm_plan_active(plan, ctx)
        assert "80%" in action.taken[0]
        # Charging window appears because plan has a FORCE_CHARGE slot
        assert any("Grid-charging" in t for t in action.taken)
        assert "18:00" in action.reason and "22:00" in action.reason
        assert action.expires_at == ctx.storm_window_end

    def test_no_window_if_times_missing(self) -> None:
        ctx = NarratorContext(
            now=datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc),
            storm_active=True,
            storm_reserve_soc=0.80,
        )
        action = narrate_storm_plan_active(None, ctx)
        assert "Storm forecast active" == action.reason


class TestPriceSpikeNarrator:
    def test_discharge_and_deferred_loads(self) -> None:
        now = datetime(2025, 6, 15, 17, 30, tzinfo=timezone.utc)
        plan = _plan_with(SlotMode.FORCE_DISCHARGE)
        ctx = NarratorContext(
            now=now,
            current_soc=0.75,
            spike_price_cents=240.0,
            deferred_load_names=["pool_pump", "hot_water"],
        )
        action = narrate_price_spike(plan, ctx)
        assert "240" in action.reason
        assert any("pool_pump" in t for t in action.taken)

    def test_resolved_restores_loads(self) -> None:
        ctx = NarratorContext(
            now=datetime(2025, 6, 15, 19, 0, tzinfo=timezone.utc),
            current_soc=0.55,
            deferred_load_names=["pool_pump"],
        )
        action = narrate_price_spike_resolved(None, ctx)
        assert any("Restoring" in t for t in action.taken)
        assert any("55%" in t for t in action.taken)


class TestGridOutageNarrator:
    def test_is_observation_not_action(self) -> None:
        since = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        ctx = NarratorContext(
            now=since + timedelta(minutes=7),
            current_soc=0.62,
            inverter_offline_since=since,
        )
        action = narrate_grid_outage(None, ctx)
        assert action.taken == []
        assert "Grid unreachable" in action.observation
        assert "62%" in action.observation


class TestForceChargeNarrator:
    def test_reason_includes_threshold_and_price(self) -> None:
        ctx = NarratorContext(
            now=datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc),
            current_soc=0.40,
            force_charge_threshold_cents=3.0,
            force_charge_price_cents=1.8,
            evening_target_soc=0.90,
            evening_target_hour=16,
        )
        action = narrate_force_charge_triggered(None, ctx)
        assert "1.8c/kWh" in action.reason
        assert "3.0c/kWh" in action.reason
        assert any("50%" in t for t in action.taken)  # 90-40=50% shortfall


class TestRenderPlain:
    def test_renders_action_as_bullet_list(self) -> None:
        from power_master.notifications.bus import Action
        action = Action(
            taken=["Do a", "Do b"],
            reason="Because reason",
        )
        text = render_plain("Title", action, "ignored")
        assert "Title" in text
        assert "Because reason" in text
        assert "• Do a" in text
        assert "• Do b" in text

    def test_passthrough_when_no_action(self) -> None:
        assert render_plain("Title", None, "msg") == "msg"


class TestDailyBriefing:
    def test_summarises_plan_peaks(self) -> None:
        now = datetime(2025, 6, 15, 7, 0, tzinfo=timezone.utc)
        plan = _plan_with(SlotMode.FORCE_CHARGE, start_offset_min=180)
        ctx = NarratorContext(
            now=now,
            current_soc=0.45,
        )
        action = generate_daily_briefing(plan, ctx)
        assert any("45%" in t for t in action.taken)
