"""Tests for EV-aware dashboard overview.

Verifies that when EV charger is enabled in config, the overview page:
- Shows EV charger capacity (kW) and controllable state
- Displays charge_window and expected_nightly_kwh
- Shows configured modes (min_nightly_kwh, opportunistic)
- Shows "monitored — dumb timer, not yet controllable" when controllable=False
- Includes a note that the solver provisions for EV draw
- When EV disabled (default), shows no EV section and rest of overview unchanged
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from power_master.config.manager import ConfigManager
from power_master.dashboard.app import create_app


@pytest.fixture
def config_manager_ev_enabled(tmp_path: Path) -> ConfigManager:
    """Config manager with EV charger enabled."""
    defaults = tmp_path / "config.defaults.yaml"
    config = {
        "setup_completed": True,
        "db": {"path": ":memory:"},
        "providers": {
            "tariff": {
                "type": "tou",
                "timezone": "Australia/Brisbane",
                "plan": {
                    "versions": [
                        {
                            "valid_from": "2026-01-01",
                            "valid_until": None,
                            "import_bands": [
                                {
                                    "descriptor": "peak",
                                    "windows": ["16:00-22:59"],
                                    "rate_c_per_kwh": 55.55,
                                },
                                {
                                    "descriptor": "off-peak",
                                    "windows": ["10:00-13:59"],
                                    "rate_c_per_kwh": 0.0,
                                },
                                {
                                    "descriptor": "shoulder",
                                    "rate_c_per_kwh": 34.1,
                                },
                            ],
                            "free_windows": [],
                            "feed_in_bands": [
                                {
                                    "name": "evening-fit",
                                    "windows": ["16:00-22:59"],
                                    "rate_c_per_kwh": 8.0,
                                }
                            ],
                            "credits": [],
                        }
                    ],
                    "supply_charge_c_per_day": 148.5,
                    "billing_cycle": {"length_days": 28, "anchor_date": "2026-01-01"},
                },
            }
        },
        "ev": {
            "enabled": True,
            "charger_kw": 2.5,
            "controllable": False,
            "charge_window": "22:00-07:00",
            "expected_nightly_kwh": 15.0,
            "mode": {
                "min_nightly_kwh": None,
                "opportunistic": False,
            },
        },
    }
    defaults.write_text(yaml.dump(config))
    user = tmp_path / "config.yaml"
    mgr = ConfigManager(defaults_path=defaults, user_path=user)
    mgr.load()
    return mgr


@pytest.fixture
def config_manager_ev_enabled_with_modes(tmp_path: Path) -> ConfigManager:
    """Config manager with EV enabled and both modes active."""
    defaults = tmp_path / "config.defaults.yaml"
    config = {
        "setup_completed": True,
        "db": {"path": ":memory:"},
        "providers": {
            "tariff": {
                "type": "tou",
                "timezone": "Australia/Brisbane",
                "plan": {
                    "versions": [
                        {
                            "valid_from": "2026-01-01",
                            "valid_until": None,
                            "import_bands": [
                                {
                                    "descriptor": "peak",
                                    "windows": ["16:00-22:59"],
                                    "rate_c_per_kwh": 55.55,
                                },
                                {
                                    "descriptor": "shoulder",
                                    "rate_c_per_kwh": 34.1,
                                },
                            ],
                            "free_windows": [],
                            "feed_in_bands": [],
                            "credits": [],
                        }
                    ],
                    "supply_charge_c_per_day": 148.5,
                    "billing_cycle": {"length_days": 28, "anchor_date": "2026-01-01"},
                },
            }
        },
        "ev": {
            "enabled": True,
            "charger_kw": 3.5,
            "controllable": False,
            "charge_window": "22:00-07:00",
            "expected_nightly_kwh": 18.0,
            "mode": {
                "min_nightly_kwh": 10.0,
                "opportunistic": True,
            },
        },
    }
    defaults.write_text(yaml.dump(config))
    user = tmp_path / "config.yaml"
    mgr = ConfigManager(defaults_path=defaults, user_path=user)
    mgr.load()
    return mgr


@pytest.fixture
def config_manager_ev_disabled(tmp_path: Path) -> ConfigManager:
    """Config manager with EV disabled (default)."""
    defaults = tmp_path / "config.defaults.yaml"
    config = {
        "setup_completed": True,
        "db": {"path": ":memory:"},
        "providers": {
            "tariff": {
                "type": "amber",
            }
        },
        # ev is not specified, so it defaults to enabled=False
    }
    defaults.write_text(yaml.dump(config))
    user = tmp_path / "config.yaml"
    mgr = ConfigManager(defaults_path=defaults, user_path=user)
    mgr.load()
    return mgr


def _setup_mock_repo(repo):
    """Setup mock repo with required async methods."""
    repo.get_active_plan = AsyncMock(return_value=None)
    repo.get_plan_slots = AsyncMock(return_value=[])
    repo.get_active_billing_cycle = AsyncMock(return_value=None)
    repo.get_active_spike = AsyncMock(return_value=None)
    repo.get_latest_historical_value = AsyncMock(return_value=None)
    repo.get_latest_telemetry = AsyncMock(return_value=None)


@pytest.fixture
async def client_ev_enabled(repo, config_manager_ev_enabled):
    """Test client with EV enabled."""
    from power_master.control.manual_override import ManualOverride

    config = config_manager_ev_enabled.config
    app = create_app(config, repo, config_manager=config_manager_ev_enabled)
    app.state.manual_override = ManualOverride()

    # Setup repo mocks
    _setup_mock_repo(repo)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def client_ev_enabled_with_modes(repo, config_manager_ev_enabled_with_modes):
    """Test client with EV enabled and modes configured."""
    from power_master.control.manual_override import ManualOverride

    config = config_manager_ev_enabled_with_modes.config
    app = create_app(config, repo, config_manager=config_manager_ev_enabled_with_modes)
    app.state.manual_override = ManualOverride()

    # Setup repo mocks
    _setup_mock_repo(repo)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def client_ev_disabled(repo, config_manager_ev_disabled):
    """Test client with EV disabled."""
    from power_master.control.manual_override import ManualOverride

    config = config_manager_ev_disabled.config
    app = create_app(config, repo, config_manager=config_manager_ev_disabled)
    app.state.manual_override = ManualOverride()

    # Setup repo mocks
    _setup_mock_repo(repo)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestEVOverview:
    """Tests for EV-aware dashboard overview."""

    @pytest.mark.asyncio
    async def test_overview_loads_with_ev_enabled(self, client_ev_enabled) -> None:
        """Verify overview page loads with EV enabled."""
        resp = await client_ev_enabled.get("/")
        assert resp.status_code == 200
        assert "Power Master" in resp.text

    @pytest.mark.asyncio
    async def test_ev_charger_panel_visible_when_enabled(self, client_ev_enabled) -> None:
        """Verify EV Charger panel is rendered when enabled."""
        resp = await client_ev_enabled.get("/")
        assert resp.status_code == 200
        assert "EV Charger" in resp.text

    @pytest.mark.asyncio
    async def test_ev_charger_kw_displayed(self, client_ev_enabled) -> None:
        """Verify charger capacity is shown."""
        resp = await client_ev_enabled.get("/")
        assert resp.status_code == 200
        # Config specifies 2.5 kW
        assert "2.5 kW" in resp.text

    @pytest.mark.asyncio
    async def test_ev_charger_not_controllable_message(self, client_ev_enabled) -> None:
        """Verify 'dumb timer' message when not controllable."""
        resp = await client_ev_enabled.get("/")
        assert resp.status_code == 200
        # controllable=False, so should show the dumb timer text
        assert "dumb timer" in resp.text
        assert "not yet controllable" in resp.text

    @pytest.mark.asyncio
    async def test_ev_charge_window_displayed(self, client_ev_enabled) -> None:
        """Verify charge window is shown."""
        resp = await client_ev_enabled.get("/")
        assert resp.status_code == 200
        # Config specifies 22:00-07:00
        assert "22:00-07:00" in resp.text

    @pytest.mark.asyncio
    async def test_ev_expected_nightly_kwh_displayed(self, client_ev_enabled) -> None:
        """Verify expected nightly energy is shown."""
        resp = await client_ev_enabled.get("/")
        assert resp.status_code == 200
        # Config specifies 15.0 kWh
        assert "15.0 kWh expected" in resp.text

    @pytest.mark.asyncio
    async def test_ev_solver_provisions_message(self, client_ev_enabled) -> None:
        """Verify solver provisions message is present."""
        resp = await client_ev_enabled.get("/")
        assert resp.status_code == 200
        assert "Solver provisions battery for EV draw" in resp.text

    @pytest.mark.asyncio
    async def test_ev_modes_with_both_enabled(self, client_ev_enabled_with_modes) -> None:
        """Verify modes are shown when both min_nightly and opportunistic are set."""
        resp = await client_ev_enabled_with_modes.get("/")
        assert resp.status_code == 200
        assert "Min 10.0 kWh" in resp.text
        assert "Opportunistic" in resp.text

    @pytest.mark.asyncio
    async def test_ev_panel_absent_when_disabled(self, client_ev_disabled) -> None:
        """Verify EV panel is not rendered when disabled."""
        resp = await client_ev_disabled.get("/")
        assert resp.status_code == 200
        # Should NOT have EV Charger section marker (the panel title + subtitle combo)
        assert "dash-panel-title\">EV Charger<" not in resp.text
        assert "dumb timer" not in resp.text

    @pytest.mark.asyncio
    async def test_overview_otherwise_unchanged_when_ev_disabled(self, client_ev_disabled) -> None:
        """Verify rest of overview is unchanged when EV disabled."""
        resp = await client_ev_disabled.get("/")
        assert resp.status_code == 200
        # Basic page elements should still exist
        assert "Power Master" in resp.text
        assert "Manual Control" in resp.text
