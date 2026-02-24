"""Tests for Application lifecycle wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from power_master.config.manager import ConfigManager
from power_master.config.schema import AppConfig
from power_master.main import Application


@pytest.fixture
def config():
    return AppConfig()


@pytest.fixture
def config_manager(tmp_path: Path):
    defaults = tmp_path / "config.defaults.yaml"
    defaults.write_text("db:\n  path: ':memory:'\n")
    user = tmp_path / "config.yaml"
    mgr = ConfigManager(defaults_path=defaults, user_path=user)
    mgr.load()
    return mgr


class TestApplicationConstruction:
    def test_create_application(self, config, config_manager) -> None:
        app = Application(config, config_manager)
        assert app.config is config
        assert app.config_manager is config_manager
        assert app._running is False

    def test_initial_state(self, config, config_manager) -> None:
        app = Application(config, config_manager)
        assert app._tasks == []
        assert app._adapter is None
        assert app._mqtt_client is None
        assert app._control_loop is None
        assert app._providers == []


class TestProviderCreation:
    def test_creates_weather_provider_always(self, config, config_manager) -> None:
        app = Application(config, config_manager)
        solar, weather, storm, tariff = app._create_providers()
        # Weather (Open-Meteo) always created
        assert weather is not None
        # Forecast.Solar default provider does not require API key
        assert solar is not None
        # Storm requires location_aac (empty by default)
        assert storm is None
        # Tariff requires API key (empty by default)
        assert tariff is None

    def test_creates_solar_with_forecast_solar_config(self, config_manager) -> None:
        config = AppConfig(
            providers={"solar": {"latitude": -27.4, "longitude": 153.0, "kwp": 7.0}},
        )
        app = Application(config, config_manager)
        solar, weather, storm, tariff = app._create_providers()
        assert solar is not None
        assert weather is not None

    def test_creates_tariff_with_api_key(self, config_manager) -> None:
        config = AppConfig(
            providers={"tariff": {"api_key": "test-key", "site_id": "test-site"}},
        )
        app = Application(config, config_manager)
        solar, weather, storm, tariff = app._create_providers()
        assert tariff is not None

    def test_creates_storm_with_location(self, config_manager) -> None:
        config = AppConfig(
            storm={"enabled": True},
            providers={"storm": {"location_aac": "QLD_PT001"}},
        )
        app = Application(config, config_manager)
        solar, weather, storm, tariff = app._create_providers()
        assert storm is not None

    def test_no_storm_when_disabled(self, config_manager) -> None:
        config = AppConfig(
            storm={"enabled": False},
            providers={"storm": {"location_aac": "QLD_PT001"}},
        )
        app = Application(config, config_manager)
        solar, weather, storm, tariff = app._create_providers()
        assert storm is None


class TestLoadRegistration:
    def test_register_shelly_loads(self, config_manager) -> None:
        config = AppConfig(
            loads={
                "shelly_devices": [
                    {
                        "name": "Pool Pump",
                        "host": "192.168.1.50",
                        "power_w": 1200,
                        "priority_class": 4,
                    },
                    {
                        "name": "Hot Water",
                        "host": "192.168.1.51",
                        "power_w": 2400,
                        "priority_class": 3,
                        "enabled": False,
                    },
                ],
            },
        )
        app = Application(config, config_manager)

        from power_master.loads.manager import LoadManager

        load_manager = LoadManager(config)
        app._register_loads(load_manager)

        # Only enabled devices registered
        assert len(load_manager.controllers) == 1
        assert "shelly_Pool Pump" in load_manager.controllers

    def test_register_no_loads(self, config, config_manager) -> None:
        app = Application(config, config_manager)

        from power_master.loads.manager import LoadManager

        load_manager = LoadManager(config)
        app._register_loads(load_manager)
        assert len(load_manager.controllers) == 0


class TestAdapterCreation:
    @pytest.mark.asyncio
    async def test_adapter_created_even_on_connection_failure(self, config, config_manager) -> None:
        app = Application(config, config_manager)

        with patch(
            "power_master.hardware.adapters.foxess.FoxESSAdapter.connect",
            side_effect=ConnectionError("test failure"),
        ):
            adapter = await app._create_adapter()

        # Adapter exists but may not be connected
        assert adapter is not None

    @pytest.mark.asyncio
    async def test_adapter_connect_success(self, config, config_manager) -> None:
        app = Application(config, config_manager)

        with patch(
            "power_master.hardware.adapters.foxess.FoxESSAdapter.connect",
            new_callable=AsyncMock,
        ):
            adapter = await app._create_adapter()
        assert adapter is not None


class TestSolverInputBuilding:
    @pytest.mark.asyncio
    async def test_run_solver_builds_plan(self, config, config_manager, tmp_path) -> None:
        """Test that _run_solver creates a plan and updates the control loop."""
        from power_master.accounting.engine import AccountingEngine
        from power_master.control.loop import ControlLoop
        from power_master.db.engine import init_db
        from power_master.db.repository import Repository
        from power_master.forecast.aggregator import ForecastAggregator
        from power_master.optimisation.rebuild_evaluator import RebuildEvaluator
        from power_master.storm.monitor import StormMonitor
        from power_master.tariff.base import TariffSchedule, TariffSlot

        db = await init_db(tmp_path / "solver_test.db")
        repo = Repository(db)

        # Create a mock adapter
        mock_adapter = AsyncMock()

        # Create real components
        accounting = AccountingEngine(config)
        storm_monitor = StormMonitor(config.storm)
        rebuild_evaluator = RebuildEvaluator(config)
        control_loop = ControlLoop(config=config, adapter=mock_adapter)

        # Create aggregator with tariff data
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        tariff_slots = [
            TariffSlot(
                start=now + timedelta(minutes=i * 30),
                end=now + timedelta(minutes=(i + 1) * 30),
                import_price_cents=15.0,
                export_price_cents=5.0,
            )
            for i in range(96)
        ]
        aggregator = ForecastAggregator()
        aggregator._state.tariff = TariffSchedule(slots=tariff_slots)

        app = Application(config, config_manager)
        plan = await app._run_solver(
            aggregator, storm_monitor, accounting,
            rebuild_evaluator, control_loop, repo,
            trigger="test",
        )

        assert plan is not None
        assert plan.trigger_reason == "test"
        assert plan.version >= 1
        assert len(plan.slots) == 96  # 48h / 30min
        assert control_loop.state.current_plan is plan

        await db.close()


class TestStopLifecycle:
    @pytest.mark.asyncio
    async def test_stop_when_nothing_started(self, config, config_manager) -> None:
        """Stop should work gracefully even if start was never called."""
        app = Application(config, config_manager)
        # Should not raise
        await app.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_tasks(self, config, config_manager) -> None:
        app = Application(config, config_manager)
        app._running = True  # Simulate started state so stop() doesn't early-return

        # Create a dummy task
        async def dummy():
            await asyncio.sleep(3600)

        task = asyncio.create_task(dummy())
        app._tasks.append(task)

        await app.stop()
        # After stop, the task should be done (cancelled or finished with CancelledError)
        assert task.done()


class TestLoadProfileTimezone:
    @pytest.mark.asyncio
    async def test_load_profile_uses_local_timezone_for_fallback(
        self, config_manager
    ) -> None:
        """Default load profile hours should map from local timezone, not UTC."""

        class DummyPredictor:
            def __init__(self, repo, timezone_name: str = "UTC") -> None:
                self._profile = None

            async def rebuild_profile(self, lookback_days: int = 28) -> None:
                return None

        config = AppConfig(
            load_profile={
                "timezone": "Australia/Brisbane",
                "block_00_04_w": 111,
                "block_04_08_w": 222,
                "block_08_12_w": 333,
                "block_12_16_w": 444,
                "block_16_20_w": 555,
                "block_20_24_w": 666,
            }
        )
        app = Application(config, config_manager)
        slot_start_times = [datetime(2025, 6, 15, 14, 0, tzinfo=timezone.utc)]

        with patch("power_master.history.prediction.LoadPredictor", DummyPredictor):
            forecast = await app._build_load_forecast(
                repo=MagicMock(), slot_start_times=slot_start_times, n_slots=1
            )

        # 14:00 UTC == 00:00 local (Australia/Brisbane) => 00-04 block.
        assert forecast == [111.0]
