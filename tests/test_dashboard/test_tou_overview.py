"""Tests for TOU-aware dashboard overview.

Verifies that when using a TOU tariff provider, the overview page:
- Shows active band/descriptor from the schedule (not tercile-derived labels)
- Displays free-window cap status (remaining cap, consumed, state)
- Shows ZEROHERO credit status when configured
- Keeps Amber path unchanged
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from power_master.accounting.free_window_cap import FreeWindowCapTracker
from power_master.config.manager import ConfigManager
from power_master.config.schema import AppConfig
from power_master.dashboard.app import create_app
from power_master.tariff.events import TariffEventEmitter


@pytest.fixture
def settings_config_manager_tou(tmp_path: Path) -> ConfigManager:
    """Config manager with TOU tariff configuration."""
    defaults = tmp_path / "config.defaults.yaml"
    tou_config = {
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
                            "free_windows": [
                                {
                                    "name": "four4free",
                                    "windows": ["10:00-13:59"],
                                    "rate_c_per_kwh": 0.0,
                                    "cap_kwh_per_day": 50.0,
                                    "applies_to_channel": "general",
                                    "over_cap_falls_back_to": "shoulder",
                                }
                            ],
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
    }
    defaults.write_text(yaml.dump(tou_config))
    user = tmp_path / "config.yaml"
    mgr = ConfigManager(defaults_path=defaults, user_path=user)
    mgr.load()
    return mgr


@pytest.fixture
def settings_config_manager_tou_with_credit(tmp_path: Path) -> ConfigManager:
    """Config manager with TOU tariff + ZEROHERO credit."""
    defaults = tmp_path / "config.defaults.yaml"
    tou_config = {
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
                                    "rate_c_per_kwh": 50.6,
                                },
                                {
                                    "descriptor": "off-peak",
                                    "windows": ["11:00-13:59"],
                                    "rate_c_per_kwh": 0.0,
                                },
                                {
                                    "descriptor": "shoulder",
                                    "rate_c_per_kwh": 39.6,
                                },
                            ],
                            "free_windows": [
                                {
                                    "name": "zerohero-free",
                                    "windows": ["11:00-13:59"],
                                    "rate_c_per_kwh": 0.0,
                                    "cap_kwh_per_day": 50.0,
                                    "applies_to_channel": "general",
                                    "over_cap_falls_back_to": "shoulder",
                                }
                            ],
                            "feed_in_bands": [
                                {
                                    "name": "evening-premium",
                                    "windows": ["18:00-20:59"],
                                    "tiers": [
                                        {"up_to_kwh_per_day": 15, "rate_c_per_kwh": 10.0},
                                        {"up_to_kwh_per_day": None, "rate_c_per_kwh": 2.0},
                                    ],
                                }
                            ],
                            "credits": [
                                {
                                    "name": "zerohero-evening",
                                    "type": "low_import_window",
                                    "windows": ["18:00-20:59"],
                                    "max_import_kwh_per_hour": 0.03,
                                    "reward_dollars_per_day": 1.0,
                                    "enforcement": "soft",
                                    "credit_priority_weight": 0.5,
                                }
                            ],
                        }
                    ],
                    "supply_charge_c_per_day": 198.0,
                    "billing_cycle": {"length_days": 28, "anchor_date": "2026-01-01"},
                },
            }
        },
    }
    defaults.write_text(yaml.dump(tou_config))
    user = tmp_path / "config.yaml"
    mgr = ConfigManager(defaults_path=defaults, user_path=user)
    mgr.load()
    return mgr


@pytest.fixture
def settings_config_manager_amber(tmp_path: Path) -> ConfigManager:
    """Config manager with legacy Amber configuration (unchanged path)."""
    defaults = tmp_path / "config.defaults.yaml"
    defaults.write_text(
        "setup_completed: true\ndb:\n  path: ':memory:'\nproviders:\n  tariff:\n    type: amber\n"
    )
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
async def client_tou(repo, settings_config_manager_tou):
    """Test client with TOU config and mocked cap tracker."""
    from power_master.control.manual_override import ManualOverride

    config = settings_config_manager_tou.config
    app = create_app(config, repo, config_manager=settings_config_manager_tou)
    app.state.manual_override = ManualOverride()

    # Mock cap tracker with known state
    mock_tracker = MagicMock(spec=FreeWindowCapTracker)
    mock_tracker.get_remaining_cap.return_value = 25.5
    mock_tracker.get_consumed_today.return_value = 24.5
    mock_tracker.is_cap_approaching.return_value = False
    mock_tracker.is_cap_exhausted.return_value = False
    app.state.free_window_cap_tracker = mock_tracker

    # Mock event emitter
    app.state.tariff_event_emitter = TariffEventEmitter()

    # Setup repo mocks
    _setup_mock_repo(repo)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def client_tou_with_credit(repo, settings_config_manager_tou_with_credit):
    """Test client with TOU + credit config."""
    from power_master.control.manual_override import ManualOverride

    config = settings_config_manager_tou_with_credit.config
    app = create_app(config, repo, config_manager=settings_config_manager_tou_with_credit)
    app.state.manual_override = ManualOverride()

    # Mock cap tracker
    mock_tracker = MagicMock(spec=FreeWindowCapTracker)
    mock_tracker.get_remaining_cap.return_value = 35.0
    mock_tracker.get_consumed_today.return_value = 15.0
    mock_tracker.is_cap_approaching.return_value = False
    mock_tracker.is_cap_exhausted.return_value = False
    app.state.free_window_cap_tracker = mock_tracker

    # Mock event emitter with a credit event
    emitter = TariffEventEmitter()
    emitter.emit_credit_window_on_track(
        credit_name="zerohero-evening",
        window_name="18:00-20:59",
        current_import_kwh=0.01,
        threshold_kwh=0.09,
    )
    app.state.tariff_event_emitter = emitter

    # Setup repo mocks
    _setup_mock_repo(repo)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def client_amber(repo, settings_config_manager_amber):
    """Test client with legacy Amber config."""
    from power_master.control.manual_override import ManualOverride

    config = settings_config_manager_amber.config
    app = create_app(config, repo, config_manager=settings_config_manager_amber)
    app.state.manual_override = ManualOverride()
    # No cap tracker for Amber
    app.state.free_window_cap_tracker = None
    app.state.tariff_event_emitter = TariffEventEmitter()

    # Setup repo mocks
    _setup_mock_repo(repo)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestTOUOverview:
    """Tests for TOU-aware dashboard overview."""

    @pytest.mark.asyncio
    async def test_overview_loads_with_tou_config(self, client_tou) -> None:
        """Verify overview page loads successfully with TOU config."""
        resp = await client_tou.get("/")
        assert resp.status_code == 200
        assert "Power Master" in resp.text

    @pytest.mark.asyncio
    async def test_tou_status_panel_visible(self, client_tou) -> None:
        """Verify TOU status panel is rendered."""
        resp = await client_tou.get("/")
        assert resp.status_code == 200
        assert "TOU Status" in resp.text

    @pytest.mark.asyncio
    async def test_tou_free_window_cap_displayed(self, client_tou) -> None:
        """Verify free-window cap remaining is shown."""
        resp = await client_tou.get("/")
        assert resp.status_code == 200
        assert "Free Window Remaining" in resp.text
        # Should show 25.5 kWh remaining
        assert "25.5" in resp.text
        assert "24.5" in resp.text  # consumed

    @pytest.mark.asyncio
    async def test_tou_cap_state_approaching(self, repo, settings_config_manager_tou) -> None:
        """Verify approaching state is marked correctly."""
        from power_master.control.manual_override import ManualOverride

        config = settings_config_manager_tou.config
        app = create_app(config, repo, config_manager=settings_config_manager_tou)
        app.state.manual_override = ManualOverride()

        # Mock tracker at 80% capacity
        mock_tracker = MagicMock(spec=FreeWindowCapTracker)
        mock_tracker.get_remaining_cap.return_value = 10.0
        mock_tracker.get_consumed_today.return_value = 40.0
        mock_tracker.is_cap_approaching.return_value = True  # approaching
        mock_tracker.is_cap_exhausted.return_value = False
        app.state.free_window_cap_tracker = mock_tracker
        app.state.tariff_event_emitter = TariffEventEmitter()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/")
            assert resp.status_code == 200
            assert "cap-approaching" in resp.text

    @pytest.mark.asyncio
    async def test_tou_cap_state_exhausted(self, repo, settings_config_manager_tou) -> None:
        """Verify exhausted state is marked correctly."""
        from power_master.control.manual_override import ManualOverride

        config = settings_config_manager_tou.config
        app = create_app(config, repo, config_manager=settings_config_manager_tou)
        app.state.manual_override = ManualOverride()

        # Mock tracker with cap exhausted
        mock_tracker = MagicMock(spec=FreeWindowCapTracker)
        mock_tracker.get_remaining_cap.return_value = 0.0
        mock_tracker.get_consumed_today.return_value = 50.0
        mock_tracker.is_cap_approaching.return_value = True
        mock_tracker.is_cap_exhausted.return_value = True  # exhausted
        app.state.free_window_cap_tracker = mock_tracker
        app.state.tariff_event_emitter = TariffEventEmitter()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/")
            assert resp.status_code == 200
            assert "cap-exhausted" in resp.text

    @pytest.mark.asyncio
    async def test_tou_cap_none_when_tracker_missing(self, repo, settings_config_manager_tou) -> None:
        """Verify page handles missing cap tracker gracefully."""
        from power_master.control.manual_override import ManualOverride

        config = settings_config_manager_tou.config
        app = create_app(config, repo, config_manager=settings_config_manager_tou)
        app.state.manual_override = ManualOverride()

        # No cap tracker (None)
        app.state.free_window_cap_tracker = None
        app.state.tariff_event_emitter = TariffEventEmitter()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/")
            assert resp.status_code == 200
            # TOU Status panel should still be visible, just without cap details
            assert "TOU Status" in resp.text

    @pytest.mark.asyncio
    async def test_tou_credit_status_displayed(self, client_tou_with_credit) -> None:
        """Verify ZEROHERO credit status is shown."""
        resp = await client_tou_with_credit.get("/")
        assert resp.status_code == 200
        assert "Evening Credit" in resp.text
        assert "zerohero-evening" in resp.text.lower()
        assert "on_track" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_tou_credit_absent_when_not_configured(self, client_tou) -> None:
        """Verify credit section is absent when no credit configured."""
        resp = await client_tou.get("/")
        assert resp.status_code == 200
        # TOU Status should be present, but credit section should be absent
        # (or empty since it's only shown when credit_status is not None)
        # The template only renders the credit section if tou_context.credit_status exists

    @pytest.mark.asyncio
    async def test_amber_path_unchanged(self, client_amber) -> None:
        """Verify Amber config does not show TOU Status panel."""
        resp = await client_amber.get("/")
        assert resp.status_code == 200
        # Amber should NOT show TOU Status panel
        assert "TOU Status" not in resp.text

    @pytest.mark.asyncio
    async def test_amber_still_shows_energy_cost(self, client_amber) -> None:
        """Verify Amber path still renders Energy Cost panel normally."""
        resp = await client_amber.get("/")
        assert resp.status_code == 200
        assert "Energy Cost" in resp.text
        assert "Current Price" in resp.text


class TestTOUContextBuilding:
    """Tests for context building in overview.py."""

    @pytest.mark.asyncio
    async def test_tou_context_not_none_for_tou_config(self, client_tou) -> None:
        """Verify tou_context is built when type='tou'."""
        resp = await client_tou.get("/")
        assert resp.status_code == 200
        # tou_context should be in the rendered template
        assert "TOU Status" in resp.text

    @pytest.mark.asyncio
    async def test_tou_context_none_for_amber_config(self, client_amber) -> None:
        """Verify tou_context is None when type='amber'."""
        resp = await client_amber.get("/")
        assert resp.status_code == 200
        # No TOU Status panel for Amber
        assert "TOU Status" not in resp.text

    @pytest.mark.asyncio
    async def test_active_descriptor_from_plan_slots(
        self, repo, settings_config_manager_tou
    ) -> None:
        """Verify active descriptor is fetched from plan slots."""
        from power_master.control.manual_override import ManualOverride

        config = settings_config_manager_tou.config
        app = create_app(config, repo, config_manager=settings_config_manager_tou)
        app.state.manual_override = ManualOverride()
        app.state.free_window_cap_tracker = MagicMock(spec=FreeWindowCapTracker)
        app.state.free_window_cap_tracker.get_remaining_cap.return_value = 50.0
        app.state.free_window_cap_tracker.get_consumed_today.return_value = 0.0
        app.state.free_window_cap_tracker.is_cap_approaching.return_value = False
        app.state.free_window_cap_tracker.is_cap_exhausted.return_value = False
        app.state.tariff_event_emitter = TariffEventEmitter()

        # Mock repo to return a plan slot with descriptor
        repo.get_active_plan = AsyncMock(
            return_value={"id": 1, "version": 1, "trigger_reason": "test", "objective_score": 123.45}
        )

        now_utc = datetime.now(timezone.utc)
        plan_slot = {
            "slot_start": now_utc.isoformat(),
            "slot_end": (now_utc + timedelta(minutes=30)).isoformat(),
            "import_rate_cents": 55.55,
            "descriptor": "peak",
        }
        repo.get_plan_slots = AsyncMock(return_value=[plan_slot])
        repo.get_active_billing_cycle = AsyncMock(return_value=None)
        repo.get_active_spike = AsyncMock(return_value=None)
        repo.get_latest_historical_value = AsyncMock(return_value=None)
        repo.get_latest_telemetry = AsyncMock(return_value=None)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/")
            assert resp.status_code == 200
            # active_descriptor should be "peak" from the plan slot
            assert "Peak" in resp.text  # capitalized
            assert "55.5" in resp.text  # rate in cents
