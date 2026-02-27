"""Tests for dashboard routes and API endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from power_master.config.manager import ConfigManager
from power_master.config.schema import AppConfig
from power_master.dashboard.app import create_app


@pytest.fixture
def settings_config_manager(tmp_path: Path) -> ConfigManager:
    """Config manager that writes to a real tmp directory for settings tests."""
    defaults = tmp_path / "config.defaults.yaml"
    defaults.write_text("db:\n  path: ':memory:'\n")
    user = tmp_path / "config.yaml"
    mgr = ConfigManager(defaults_path=defaults, user_path=user)
    mgr.load()
    return mgr


@pytest.fixture
async def client(repo, settings_config_manager):
    """Create a test client with the dashboard app."""
    from power_master.control.manual_override import ManualOverride

    config = settings_config_manager.config
    app = create_app(config, repo, config_manager=settings_config_manager)
    app.state.manual_override = ManualOverride()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestOverview:
    @pytest.mark.asyncio
    async def test_overview_page(self, client) -> None:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Power Master" in resp.text

    @pytest.mark.asyncio
    async def test_overview_with_no_data(self, client) -> None:
        resp = await client.get("/")
        assert resp.status_code == 200


class TestPlansPage:
    @pytest.mark.asyncio
    async def test_plans_page(self, client) -> None:
        resp = await client.get("/plans")
        assert resp.status_code == 200
        assert "Plans" in resp.text


class TestAccountingPage:
    @pytest.mark.asyncio
    async def test_accounting_page(self, client) -> None:
        resp = await client.get("/accounting")
        assert resp.status_code == 200
        assert "Accounting" in resp.text


class TestGraphsPage:
    @pytest.mark.asyncio
    async def test_graphs_page(self, client) -> None:
        resp = await client.get("/graphs")
        assert resp.status_code == 200
        assert "Graphs" in resp.text


class TestSettingsPage:
    @pytest.mark.asyncio
    async def test_settings_page(self, client) -> None:
        resp = await client.get("/settings")
        assert resp.status_code == 200
        assert "Settings" in resp.text

    @pytest.mark.asyncio
    async def test_settings_page_shows_all_tabs(self, client) -> None:
        resp = await client.get("/settings")
        assert resp.status_code == 200
        for tab in ["Battery", "Planning", "Arbitrage",
                     "Load Profile", "Providers", "Loads", "MQTT",
                     "Anti-Oscillation", "Storm", "Resilience"]:
            assert tab in resp.text

    @pytest.mark.asyncio
    async def test_settings_page_shows_current_values(self, client) -> None:
        resp = await client.get("/settings")
        # Default battery capacity
        assert "10000" in resp.text
        # Default SOC hard limits
        assert "0.05" in resp.text
        assert "0.95" in resp.text


class TestSettingsPost:
    @pytest.mark.asyncio
    async def test_save_battery_settings(self, client) -> None:
        """POST battery settings updates in-memory config."""
        resp = await client.post(
            "/settings",
            data={"battery.capacity_wh": "15000"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Settings saved" in resp.text

    @pytest.mark.asyncio
    async def test_save_updates_app_config(self, client) -> None:
        """POST updates the in-memory config visible via /api/config."""
        await client.post(
            "/settings",
            data={"battery.capacity_wh": "20000"},
            follow_redirects=False,
        )
        resp = await client.get("/api/config")
        data = resp.json()
        assert data["battery"]["capacity_wh"] == 20000

    @pytest.mark.asyncio
    async def test_save_provider_api_keys(
        self, client, settings_config_manager,
    ) -> None:
        """POST provider API key persists to YAML."""
        await client.post(
            "/settings",
            data={
                "providers.tariff.api_key": "test-amber-key",
            },
            follow_redirects=False,
        )
        # Verify written to YAML file
        raw = yaml.safe_load(settings_config_manager._user_path.read_text())
        assert raw["providers"]["tariff"]["api_key"] == "test-amber-key"

    @pytest.mark.asyncio
    async def test_save_nullable_solar_fields(self, client) -> None:
        await client.post(
            "/settings",
            data={
                "providers.solar.azimuth": "",
            },
            follow_redirects=False,
        )
        resp = await client.get("/api/config")
        data = resp.json()
        assert data["providers"]["solar"]["azimuth"] is None

    @pytest.mark.asyncio
    async def test_save_provider_no_restart_needed(self, client) -> None:
        """POST provider changes applies immediately without restart."""
        resp = await client.post(
            "/settings",
            data={"providers.solar.kwp": "9.5"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Settings saved" in resp.text
        assert "Restart required" not in resp.text

    @pytest.mark.asyncio
    async def test_save_daytime_reserve_settings(self, client) -> None:
        await client.post(
            "/settings",
            data={
                "battery_targets.daytime_reserve_soc_target": "0.55",
                "battery_targets.daytime_reserve_start_hour": "9",
                "battery_targets.daytime_reserve_end_hour": "17",
            },
            follow_redirects=False,
        )
        resp = await client.get("/api/config")
        data = resp.json()
        bt = data["battery_targets"]
        assert bt["daytime_reserve_soc_target"] == 0.55
        assert bt["daytime_reserve_start_hour"] == 9
        assert bt["daytime_reserve_end_hour"] == 17

    @pytest.mark.asyncio
    async def test_save_planning_no_restart(self, client) -> None:
        """POST planning changes does NOT show restart banner."""
        resp = await client.post(
            "/settings",
            data={"planning.horizon_hours": "72"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Settings saved" in resp.text
        assert "Restart required" not in resp.text

    @pytest.mark.asyncio
    async def test_save_checkbox_enabled(self, client) -> None:
        """POST with checkbox present sets True."""
        await client.post(
            "/settings",
            data={"storm.enabled": "on", "mqtt.enabled": "on", "mqtt.ha_discovery_enabled": "on"},
            follow_redirects=False,
        )
        resp = await client.get("/api/config")
        data = resp.json()
        assert data["storm"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_save_checkbox_unchecked(self, client) -> None:
        """POST without checkbox sets False."""
        # Don't send storm.enabled — simulates unchecked checkbox
        await client.post(
            "/settings",
            data={"storm.reserve_soc_target": "0.90"},
            follow_redirects=False,
        )
        resp = await client.get("/api/config")
        data = resp.json()
        assert data["storm"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_save_persists_to_yaml(
        self, client, settings_config_manager,
    ) -> None:
        """POST creates/updates config.yaml file."""
        await client.post(
            "/settings",
            data={"battery.capacity_wh": "12000"},
            follow_redirects=False,
        )
        assert settings_config_manager._user_path.exists()
        raw = yaml.safe_load(settings_config_manager._user_path.read_text())
        assert raw["battery"]["capacity_wh"] == "12000"

    @pytest.mark.asyncio
    async def test_settings_no_config_manager(self, repo) -> None:
        """POST without config_manager returns error redirect."""
        config = AppConfig()
        app = create_app(config, repo, config_manager=None)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/settings",
                data={"battery.capacity_wh": "5000"},
                follow_redirects=True,
            )
            assert "not available" in resp.text


class TestPricesHistory:
    @pytest.mark.asyncio
    async def test_prices_history_empty(self, client) -> None:
        resp = await client.get("/api/prices/history")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_prices_history_with_data(self, client, repo) -> None:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        ts = (now - timedelta(hours=1)).isoformat()
        await repo.store_historical("import_price_cents", 25.0, "amber", ts)
        await repo.store_historical("export_price_cents", 8.0, "amber", ts)

        resp = await client.get("/api/prices/history?hours=12")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["import_price_cents"] == 25.0
        assert data[0]["export_price_cents"] == 8.0

    @pytest.mark.asyncio
    async def test_prices_history_respects_hours(self, client, repo) -> None:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        recent = (now - timedelta(minutes=30)).isoformat()
        await repo.store_historical("import_price_cents", 20.0, "amber", recent)
        old = (now - timedelta(hours=10)).isoformat()
        await repo.store_historical("import_price_cents", 30.0, "amber", old)

        resp = await client.get("/api/prices/history?hours=2")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["import_price_cents"] == 20.0


class TestModeAutoActive:
    @pytest.mark.asyncio
    async def test_auto_active_in_mode_response(self, client) -> None:
        resp = await client.get("/api/mode")
        assert resp.status_code == 200
        data = resp.json()
        assert "auto_active" in data


class TestModeControl:
    @pytest.mark.asyncio
    async def test_force_discharge_with_power(self, client) -> None:
        resp = await client.post(
            "/api/mode", json={"mode": 4, "power_w": 3000, "timeout_s": 3600},
        )
        data = resp.json()
        assert data["status"] == "ok"
        assert data["power_w"] == 3000

    @pytest.mark.asyncio
    async def test_force_discharge_zero_power_gets_default(self, client) -> None:
        resp = await client.post("/api/mode", json={"mode": 4, "timeout_s": 3600})
        data = resp.json()
        assert data["status"] == "ok"
        assert data["power_w"] == 5000  # max_discharge_rate_w default

    @pytest.mark.asyncio
    async def test_force_charge_zero_power_gets_default(self, client) -> None:
        resp = await client.post("/api/mode", json={"mode": 3})
        data = resp.json()
        assert data["status"] == "ok"
        assert data["power_w"] == 5000  # max_charge_rate_w default

    @pytest.mark.asyncio
    async def test_self_use_keeps_zero_power(self, client) -> None:
        resp = await client.post("/api/mode", json={"mode": 1})
        data = resp.json()
        assert data["status"] == "ok"
        assert data["power_w"] == 0


class TestLoadEditing:
    @pytest.mark.asyncio
    async def test_update_shelly_no_application(self, client) -> None:
        """PUT without application returns error."""
        resp = await client.put(
            "/api/loads/shelly/TestDevice",
            json={"power_w": 2000},
        )
        data = resp.json()
        assert data["status"] == "error"
        assert "not available" in data["message"]

    @pytest.mark.asyncio
    async def test_update_mqtt_no_application(self, client) -> None:
        """PUT without application returns error."""
        resp = await client.put(
            "/api/loads/mqtt/TestEndpoint",
            json={"power_w": 2000},
        )
        data = resp.json()
        assert data["status"] == "error"
        assert "not available" in data["message"]

    @pytest.mark.asyncio
    async def test_update_shelly_device_not_found(self, repo, settings_config_manager) -> None:
        """PUT for nonexistent device returns error."""
        from unittest.mock import AsyncMock
        from power_master.control.manual_override import ManualOverride

        config = settings_config_manager.config
        app = create_app(config, repo, config_manager=settings_config_manager)
        app.state.manual_override = ManualOverride()
        app.state.application = AsyncMock()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.put(
                "/api/loads/shelly/NonExistent",
                json={"power_w": 2000},
            )
            data = resp.json()
            assert data["status"] == "error"
            assert "not found" in data["message"]

    @pytest.mark.asyncio
    async def test_update_mqtt_endpoint_not_found(self, repo, settings_config_manager) -> None:
        """PUT for nonexistent MQTT endpoint returns error."""
        from unittest.mock import AsyncMock
        from power_master.control.manual_override import ManualOverride

        config = settings_config_manager.config
        app = create_app(config, repo, config_manager=settings_config_manager)
        app.state.manual_override = ManualOverride()
        app.state.application = AsyncMock()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.put(
                "/api/loads/mqtt/NonExistent",
                json={"power_w": 2000},
            )
            data = resp.json()
            assert data["status"] == "error"
            assert "not found" in data["message"]


class TestAPI:
    @pytest.mark.asyncio
    async def test_status_endpoint(self, client) -> None:
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"

    @pytest.mark.asyncio
    async def test_latest_telemetry_empty(self, client) -> None:
        resp = await client.get("/api/telemetry/latest")
        assert resp.status_code == 200
        assert resp.json() == {}

    @pytest.mark.asyncio
    async def test_latest_telemetry_with_data(self, client, repo) -> None:
        await repo.store_telemetry(
            soc=0.72,
            battery_power_w=-1500,
            solar_power_w=4200,
            grid_power_w=-1100,
            load_power_w=2500,
        )
        resp = await client.get("/api/telemetry/latest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["soc"] == 0.72

    @pytest.mark.asyncio
    async def test_active_plan_empty(self, client) -> None:
        resp = await client.get("/api/plan/active")
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan"] is None

    @pytest.mark.asyncio
    async def test_plan_history_empty(self, client) -> None:
        resp = await client.get("/api/plan/history")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_config_endpoint(self, client) -> None:
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "battery" in data
        assert "planning" in data
        assert "arbitrage" in data

