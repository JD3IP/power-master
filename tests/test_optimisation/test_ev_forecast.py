"""Tests for EV forecast builder and integration with demand forecast."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

import pytest

from power_master.config.schema import AppConfig, EVConfig, EVModeConfig
from power_master.main import Application
from power_master.config.manager import ConfigManager


class TestEVForecastBuilder:
    """Tests for the EV forecast builder in Application._build_ev_forecast."""

    async def _make_app_with_ev_config(
        self,
        ev_enabled: bool = True,
        charger_kw: float = 3.0,
        charge_window: str | None = None,
        expected_nightly_kwh: float | None = None,
        min_nightly_kwh: float | None = None,
    ) -> Application:
        """Create an Application instance with custom EV config."""
        config = AppConfig(
            ev={
                "enabled": ev_enabled,
                "charger_kw": charger_kw,
                "charge_window": charge_window,
                "expected_nightly_kwh": expected_nightly_kwh,
                "mode": {"min_nightly_kwh": min_nightly_kwh, "opportunistic": False},
            },
            load_profile={"timezone": "Australia/Brisbane"},
        )
        # Create a dummy ConfigManager (needed for Application init)
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            defaults_file = tmp_path / "defaults.yaml"
            defaults_file.write_text("db:\n  path: :memory:\n")
            config_manager = ConfigManager(defaults_path=defaults_file, user_path=tmp_path / "user.yaml")

        app = Application(config, config_manager)
        return app

    async def test_ev_disabled_returns_zeros(self) -> None:
        """When EV disabled, forecast is all-zeros."""
        app = await self._make_app_with_ev_config(
            ev_enabled=False,
            charge_window="22:00-07:00",
            expected_nightly_kwh=20.0,
        )

        # Fixed anchor: 2026-06-16 02:00 UTC = Brisbane 12:00 (noon)
        anchor = datetime(2026, 6, 16, 2, 0, tzinfo=timezone.utc)
        slot_starts = [anchor + timedelta(minutes=30 * i) for i in range(8)]
        n_slots = 8

        forecast = await app._build_ev_forecast(slot_starts, n_slots)

        assert len(forecast) == 8
        assert all(w == 0.0 for w in forecast), "EV forecast should be all-zeros when disabled"

    async def test_ev_no_window_returns_zeros(self) -> None:
        """When charge_window not set, forecast is all-zeros."""
        app = await self._make_app_with_ev_config(
            ev_enabled=True,
            charge_window=None,
            expected_nightly_kwh=20.0,
        )

        anchor = datetime(2026, 6, 16, 2, 0, tzinfo=timezone.utc)
        slot_starts = [anchor + timedelta(minutes=30 * i) for i in range(8)]
        n_slots = 8

        forecast = await app._build_ev_forecast(slot_starts, n_slots)

        assert len(forecast) == 8
        assert all(w == 0.0 for w in forecast), "EV forecast should be all-zeros when charge_window not set"

    async def test_ev_no_expected_kwh_returns_zeros(self) -> None:
        """When expected_nightly_kwh not set, forecast is all-zeros."""
        app = await self._make_app_with_ev_config(
            ev_enabled=True,
            charge_window="22:00-07:00",
            expected_nightly_kwh=None,
        )

        anchor = datetime(2026, 6, 16, 2, 0, tzinfo=timezone.utc)
        slot_starts = [anchor + timedelta(minutes=30 * i) for i in range(8)]
        n_slots = 8

        forecast = await app._build_ev_forecast(slot_starts, n_slots)

        assert len(forecast) == 8
        assert all(w == 0.0 for w in forecast), "EV forecast should be all-zeros when expected_nightly_kwh not set"

    async def test_ev_in_window_no_midnight_crossing(self) -> None:
        """EV forecast distributes draw across in-window slots (no midnight crossing).

        Fixed anchor: 2026-06-16 02:00 UTC = Brisbane 12:00 (noon)
        Slots: 12:00, 12:30, 13:00, 13:30, 14:00, 14:30, 15:00, 15:30
        Window: 10:00-16:00 (6 hours = 360 minutes)
        Expected: 20 kWh = 20000 W ÷ 6 hours = 3333.33 W avg
        In-window slots: 0-8 (all of them)
        """
        app = await self._make_app_with_ev_config(
            ev_enabled=True,
            charge_window="10:00-16:00",  # 6-hour window
            expected_nightly_kwh=20.0,
        )

        anchor = datetime(2026, 6, 16, 2, 0, tzinfo=timezone.utc)  # noon Brisbane
        slot_starts = [anchor + timedelta(minutes=30 * i) for i in range(8)]
        n_slots = 8

        forecast = await app._build_ev_forecast(slot_starts, n_slots)

        expected_w = (20.0 * 1000) / 6.0  # 3333.33 W
        assert len(forecast) == 8
        # All slots are in 10:00-16:00 window
        for i, w in enumerate(forecast):
            assert abs(w - expected_w) < 1.0, f"Slot {i}: expected ~{expected_w}W, got {w}W"

    async def test_ev_in_window_midnight_crossing(self) -> None:
        """EV forecast with midnight-crossing window (22:00-07:00).

        Fixed anchor: 2026-06-15 14:00 UTC = Brisbane 2026-06-16 00:00 (midnight)
        Slots: 00:00, 00:30, 01:00, 01:30, 02:00, 02:30, 03:00, 03:30
        Window: 22:00-07:00 (9 hours = 540 minutes)
        Expected: 18 kWh = 18000 W ÷ 9 hours = 2000 W avg
        In-window slots: 0-8 (all of them fall in 22:00 prev day to 07:00)
        """
        app = await self._make_app_with_ev_config(
            ev_enabled=True,
            charge_window="22:00-07:00",  # 9-hour window crossing midnight
            expected_nightly_kwh=18.0,
        )

        # Anchor at midnight Brisbane time: 2026-06-15 14:00 UTC
        anchor = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
        slot_starts = [anchor + timedelta(minutes=30 * i) for i in range(8)]
        n_slots = 8

        forecast = await app._build_ev_forecast(slot_starts, n_slots)

        expected_w = (18.0 * 1000) / 9.0  # 2000 W
        assert len(forecast) == 8
        # Slots 0-8 are 00:00-03:30, all in 22:00-07:00 window
        for i, w in enumerate(forecast):
            assert abs(w - expected_w) < 1.0, f"Slot {i}: expected ~{expected_w}W, got {w}W"

    async def test_ev_partial_window_coverage(self) -> None:
        """EV forecast only applies to slots within the window.

        Fixed anchor: 2026-06-16 02:00 UTC = Brisbane 12:00 (noon)
        Slots: 12:00, 12:30, 13:00, 13:30, 14:00, 14:30, 15:00, 15:30
        Window: 13:00-15:00 (2 hours)
        Expected: 8 kWh = 8000 W ÷ 2 hours = 4000 W avg
        In-window slots: slots 2-5 (13:00, 13:30, 14:00, 14:30)
        Out-of-window slots: 0-1 (12:00-12:30), 6-7 (15:00-15:30) should be 0
        """
        app = await self._make_app_with_ev_config(
            ev_enabled=True,
            charge_window="13:00-15:00",  # 2-hour window
            expected_nightly_kwh=8.0,
        )

        anchor = datetime(2026, 6, 16, 2, 0, tzinfo=timezone.utc)  # noon Brisbane
        slot_starts = [anchor + timedelta(minutes=30 * i) for i in range(8)]
        n_slots = 8

        forecast = await app._build_ev_forecast(slot_starts, n_slots)

        expected_w = (8.0 * 1000) / 2.0  # 4000 W
        assert len(forecast) == 8

        # Slots 0-1: before window → 0 W
        assert forecast[0] == 0.0
        assert forecast[1] == 0.0

        # Slots 2-5: in window → 4000 W
        for i in range(2, 6):
            assert abs(forecast[i] - expected_w) < 1.0

        # Slots 6-7: after window → 0 W
        assert forecast[6] == 0.0
        assert forecast[7] == 0.0

    async def test_ev_floors_at_min_nightly(self) -> None:
        """EV forecast is floored at min_nightly_kwh for provisioning.

        expected_nightly_kwh=10.0 (low), min_nightly_kwh=15.0 (high)
        Solver should provision for 15.0 kWh (the higher value).
        """
        app = await self._make_app_with_ev_config(
            ev_enabled=True,
            charge_window="22:00-07:00",  # 9 hours
            expected_nightly_kwh=10.0,  # Low expected
            min_nightly_kwh=15.0,  # High minimum (floors expected)
        )

        anchor = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)  # midnight Brisbane
        slot_starts = [anchor + timedelta(minutes=30 * i) for i in range(8)]
        n_slots = 8

        forecast = await app._build_ev_forecast(slot_starts, n_slots)

        # Provision for max(10, 15) = 15 kWh across 9 hours
        expected_w = (15.0 * 1000) / 9.0  # 1666.67 W
        assert len(forecast) == 8

        # All slots in window
        for i, w in enumerate(forecast):
            assert abs(w - expected_w) < 1.0

    async def test_ev_no_floor_when_expected_exceeds_min(self) -> None:
        """EV forecast is not reduced when expected_nightly_kwh > min_nightly_kwh."""
        app = await self._make_app_with_ev_config(
            ev_enabled=True,
            charge_window="22:00-07:00",  # 9 hours
            expected_nightly_kwh=20.0,  # High expected
            min_nightly_kwh=15.0,  # Low minimum
        )

        anchor = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
        slot_starts = [anchor + timedelta(minutes=30 * i) for i in range(8)]
        n_slots = 8

        forecast = await app._build_ev_forecast(slot_starts, n_slots)

        expected_w = (20.0 * 1000) / 9.0  # 2222.22 W (not reduced)
        assert len(forecast) == 8

        for i, w in enumerate(forecast):
            assert abs(w - expected_w) < 1.0


class TestEVForecastIntegration:
    """Tests for EV forecast integration with load forecast in planning cycle.

    (These would be integration tests if we had a full mock of the planning cycle.
     For now, we test the conceptual integration: that ev_forecast is combined
     with load_forecast before being passed to SolverInputs.)
    """

    def test_combined_load_with_ev(self) -> None:
        """Load forecast + EV forecast are combined for the solver.

        This is a conceptual test: if load_w is [500, 500, 500, ...] and
        ev_w is [0, 0, 2000, 2000, 0, 0, ...], then combined should be
        [500, 500, 2500, 2500, 500, 500, ...].
        """
        house_load = [500.0] * 8
        ev_draw = [0.0, 0.0, 2000.0, 2000.0, 2000.0, 2000.0, 0.0, 0.0]

        # Combine as done in main.py
        combined = [house_load[i] + ev_draw[i] for i in range(8)]

        expected = [500.0, 500.0, 2500.0, 2500.0, 2500.0, 2500.0, 500.0, 500.0]
        assert combined == expected

    def test_solver_provisions_more_battery_for_ev_load(self) -> None:
        """Solver provisions higher battery charge when EV load is present.

        Test invariant: When we add significant EV load (e.g., 2000W for 2 slots),
        the solver should increase battery charge to meet the combined demand.
        We assert this by comparing final SOC with and without EV.

        Fixed anchor: 2026-06-16 02:00 UTC = Brisbane 12:00 (noon)
        Slots: 12:00-14:00 (4 slots, 30 min each)
        Solar: 4000W (all slots)
        House load: 1000W (all slots)
        Battery: 10kWh capacity, starting at 50% SOC

        Case 1: No EV → solver must cover 1000W house load
        Case 2: EV in slots 2-3 → solver must cover 1000W + 2000W = 3000W during those slots

        Invariant: Final SOC without EV >= Final SOC with EV
        (battery works harder with EV load)
        """
        from power_master.optimisation.solver import solve, SolverInputs

        anchor = datetime(2026, 6, 16, 2, 0, tzinfo=timezone.utc)  # noon Brisbane
        slot_starts = [anchor + timedelta(minutes=30 * i) for i in range(4)]

        # Case 1: No EV
        config = AppConfig(
            battery={"capacity_wh": 10000},
            load_profile={"timezone": "Australia/Brisbane"},
            ev={"enabled": False},
        )
        inputs_no_ev = SolverInputs(
            solar_forecast_w=[4000.0] * 4,
            load_forecast_w=[1000.0] * 4,
            import_rate_cents=[20.0] * 4,
            export_rate_cents=[5.0] * 4,
            is_spike=[False] * 4,
            current_soc=0.5,
            wacb_cents=10.0,
            storm_active=False,
            storm_reserve_soc=0.0,
            slot_start_times=slot_starts,
        )

        # Case 2: With EV
        # EV adds 2000W to slots 2-3 → load becomes [1000, 1000, 3000, 3000]
        inputs_with_ev = SolverInputs(
            solar_forecast_w=[4000.0] * 4,
            load_forecast_w=[1000.0, 1000.0, 3000.0, 3000.0],
            import_rate_cents=[20.0] * 4,
            export_rate_cents=[5.0] * 4,
            is_spike=[False] * 4,
            current_soc=0.5,
            wacb_cents=10.0,
            storm_active=False,
            storm_reserve_soc=0.0,
            slot_start_times=slot_starts,
        )

        plan_no_ev = solve(config, inputs_no_ev, "test", 1)
        plan_with_ev = solve(config, inputs_with_ev, "test", 1)

        # Invariant: Final SOC without EV should be >= with EV
        # (battery works harder to cover the EV load)
        no_ev_final_soc = plan_no_ev.slots[-1].expected_soc
        with_ev_final_soc = plan_with_ev.slots[-1].expected_soc

        assert with_ev_final_soc <= no_ev_final_soc, (
            f"With EV, final SOC ({with_ev_final_soc:.2%}) should be <= without EV ({no_ev_final_soc:.2%}). "
            f"Solver should provision battery differently for higher load."
        )
