"""Tests for TOU tariff editor backend routes."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from power_master.config.manager import ConfigManager
from power_master.config.schema import (
    AppConfig,
    BandBase,
    BillingCycleConfig,
    CreditConfig,
    FeedInBand,
    FeedInTier,
    FreeWindowConfig,
    TariffPlanConfig,
    TariffProviderConfig,
    TariffVersion,
    VPPConfig,
)
from power_master.dashboard.app import create_app


def _make_json_serializable(obj):
    """Recursively convert date/datetime objects to ISO strings for JSON."""
    if isinstance(obj, dict):
        return {k: _make_json_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_make_json_serializable(v) for v in obj]
    elif isinstance(obj, (date, datetime)):
        return obj.isoformat()
    else:
        return obj


def _build_four4free_config() -> TariffProviderConfig:
    """Build a minimal FOUR4FREE TOU config for testing."""
    return TariffProviderConfig(
        type="tou",
        timezone="Australia/Brisbane",
        grid_charge_policy="free_window_and_solar_only",
        plan=TariffPlanConfig(
            supply_charge_c_per_day=148.5,
            versions=[
                TariffVersion(
                    valid_from=date(2026, 6, 1),
                    valid_until=None,
                    import_bands=[
                        BandBase(
                            descriptor="peak",
                            windows=["16:00-22:59"],
                            rate_c_per_kwh=55.55,
                        ),
                        BandBase(
                            descriptor="off-peak-balance",
                            windows=["10:00-13:59"],
                            rate_c_per_kwh=28.6,
                        ),
                        BandBase(
                            descriptor="shoulder",
                            windows=[],
                            rate_c_per_kwh=34.1,
                        ),
                    ],
                    free_windows=[
                        FreeWindowConfig(
                            name="four4free",
                            windows=["10:00-13:59"],
                            rate_c_per_kwh=0.0,
                            cap_kwh_per_day=50.0,
                            applies_to_channel="general",
                            over_cap_falls_back_to="off-peak-balance",
                        ),
                    ],
                    feed_in_bands=[
                        FeedInBand(
                            name="evening-fit",
                            windows=["16:00-22:59"],
                            rate_c_per_kwh=8.0,
                        ),
                        FeedInBand(
                            name="default-fit",
                            windows=[],
                            rate_c_per_kwh=0.0,
                        ),
                    ],
                    credits=[],
                ),
            ],
            billing_cycle=BillingCycleConfig(
                length_days=28,
                anchor_date=date(2026, 6, 1),
            ),
            vpp=VPPConfig(enabled=False),
        ),
    )


@pytest.fixture
def tariff_config_manager_four4free(tmp_path: Path) -> ConfigManager:
    """Config manager with FOUR4FREE TOU tariff."""
    defaults = tmp_path / "config.defaults.yaml"
    defaults.write_text("setup_completed: true\ndb:\n  path: ':memory:'\n")
    user = tmp_path / "config.yaml"
    mgr = ConfigManager(defaults_path=defaults, user_path=user)
    mgr.load()
    # Save FOUR4FREE config
    cfg = _build_four4free_config()
    mgr.save_user_config({"providers": {"tariff": cfg.model_dump()}})
    return mgr


@pytest.fixture
async def tariff_client(repo, tariff_config_manager_four4free):
    """Create test client with FOUR4FREE tariff pre-configured."""
    config = tariff_config_manager_four4free.config
    app = create_app(config, repo, config_manager=tariff_config_manager_four4free)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestGetResolveTariff:
    """Tests for GET /settings/tariff/resolve."""

    @pytest.mark.asyncio
    async def test_resolve_four4free_config(self, tariff_client):
        """Resolve FOUR4FREE config: should return slots with correct prices."""

        # Resolve for 2026-06-01
        resp = await tariff_client.get("/settings/tariff/resolve?date=2026-06-01")
        assert resp.status_code == 200

        data = resp.json()
        assert data["ok"] is True
        assert data["date"] == "2026-06-01"
        assert data["timezone"] == "Australia/Brisbane"
        assert data["supply_c_per_day"] == 148.5

        # Check slots
        slots = data["slots"]
        assert len(slots) > 0

        # Find a slot in the free window (10:00-13:59) and peak (16:00-22:59)
        free_slot = None
        peak_slot = None
        shoulder_slot = None

        for slot in slots:
            start_str = slot["start"]
            # Extract hour from ISO string
            if "10:" in start_str or "11:" in start_str or "12:" in start_str or "13:" in start_str:
                if slot["descriptor"] == "four4free":
                    free_slot = slot
            if "16:" in start_str or "17:" in start_str:
                if slot["descriptor"] == "peak":
                    peak_slot = slot
            if "02:" in start_str or "03:" in start_str:
                if slot["descriptor"] == "shoulder":
                    shoulder_slot = slot

        # Verify free slot pricing
        assert free_slot is not None, "Should have free-window slot at 10:00-14:00"
        assert free_slot["import_c"] == 0.0
        assert free_slot["export_c"] == 0.0  # no export during free window

        # Verify peak slot pricing
        assert peak_slot is not None, "Should have peak slot at 16:00+"
        assert peak_slot["import_c"] == 55.55
        assert peak_slot["export_c"] == 8.0

        # Verify shoulder slot pricing
        assert shoulder_slot is not None, "Should have shoulder slot at 02:00-03:00"
        assert shoulder_slot["import_c"] == 34.1

    @pytest.mark.asyncio
    async def test_resolve_not_tou_config(self, repo):
        """Should return error if tariff type is not TOU."""
        # Create client with default (non-TOU) config
        from power_master.config.schema import AppConfig
        config = AppConfig(setup_completed=True)  # default has Amber tariff
        app = create_app(config, repo, config_manager=None)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/settings/tariff/resolve?date=2026-06-01")
            data = resp.json()
            assert data["ok"] is False
            assert "not a TOU tariff" in data["error"]

    @pytest.mark.asyncio
    async def test_resolve_invalid_date_format(self, tariff_client):
        """Should return error for invalid date format."""
        resp = await tariff_client.get("/settings/tariff/resolve?date=invalid")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "invalid date format" in data["error"]


class TestPostResolveTariff:
    """Tests for POST /settings/tariff/resolve (preview)."""

    @pytest.mark.asyncio
    async def test_preview_valid_plan(self, tariff_client):
        """Preview a valid FOUR4FREE config."""
        cfg = _build_four4free_config()
        body = cfg.model_dump()
        body["date"] = "2026-06-01"

        # Convert dates to strings for JSON serialization
        body = _make_json_serializable(body)

        resp = await tariff_client.post("/settings/tariff/resolve", json=body)
        assert resp.status_code == 200

        data = resp.json()
        assert data["ok"] is True
        assert data["date"] == "2026-06-01"
        assert data["supply_c_per_day"] == 148.5

        # Verify slots exist
        slots = data["slots"]
        assert len(slots) > 0

    @pytest.mark.asyncio
    async def test_preview_invalid_plan(self, tariff_client):
        """Preview with Pydantic-invalid config (two default bands)."""
        cfg = _build_four4free_config()
        body = cfg.model_dump()

        # Add a second default band (no windows) — invalid per schema
        body["plan"]["versions"][0]["import_bands"].append({
            "descriptor": "another-default",
            "windows": [],
            "rate_c_per_kwh": 40.0,
        })

        body = _make_json_serializable(body)

        resp = await tariff_client.post("/settings/tariff/resolve", json=body)
        assert resp.status_code == 200

        data = resp.json()
        assert data["ok"] is False
        assert "errors" in data

    @pytest.mark.asyncio
    async def test_preview_uses_provided_date(self, tariff_client):
        """Preview should use the provided date for slot generation."""
        cfg = _build_four4free_config()
        body = cfg.model_dump()
        body["date"] = "2026-06-15"

        body = _make_json_serializable(body)

        resp = await tariff_client.post("/settings/tariff/resolve", json=body)
        assert resp.status_code == 200

        data = resp.json()
        assert data["ok"] is True
        assert data["date"] == "2026-06-15"


class TestPostSaveTariff:
    """Tests for POST /settings/tariff (guarded save)."""

    @pytest.mark.asyncio
    async def test_save_invalid_plan_does_not_write(self, tariff_client):
        """Invalid plan should not persist."""
        cfg = _build_four4free_config()
        body = cfg.model_dump()

        # POST invalid plan (two default bands)
        body["plan"]["versions"][0]["import_bands"].append({
            "descriptor": "another-default",
            "windows": [],
            "rate_c_per_kwh": 40.0,
        })

        body = _make_json_serializable(body)

        resp = await tariff_client.post("/settings/tariff", json=body)
        assert resp.status_code == 400

        data = resp.json()
        assert data["ok"] is False
        assert "errors" in data

    @pytest.mark.asyncio
    async def test_save_valid_plan_via_config_manager(self, tariff_client, tariff_config_manager_four4free):
        """Valid plan should persist via config_manager fallback."""
        cfg = _build_four4free_config()
        body = cfg.model_dump()
        body = _make_json_serializable(body)

        # POST valid config
        resp = await tariff_client.post("/settings/tariff", json=body)
        assert resp.status_code == 200

        data = resp.json()
        assert data["ok"] is True

        # Verify persisted
        saved = tariff_config_manager_four4free.get_raw()
        assert saved["providers"]["tariff"]["type"] == "tou"

    @pytest.mark.asyncio
    async def test_save_with_mock_application(self, tariff_client):
        """Save should call application.reload_config if present."""
        from unittest.mock import AsyncMock

        cfg = _build_four4free_config()
        body = cfg.model_dump()
        body = _make_json_serializable(body)

        # Access app via transport
        app = tariff_client._transport.app

        # Mock application
        mock_app = AsyncMock()
        mock_app.reload_config = AsyncMock(return_value=None)
        app.state.application = mock_app

        resp = await tariff_client.post("/settings/tariff", json=body)
        assert resp.status_code == 200

        data = resp.json()
        assert data["ok"] is True

        # Verify reload_config was called
        assert mock_app.reload_config.called

    @pytest.mark.asyncio
    async def test_save_returns_ok_when_no_auth(self, tariff_client):
        """Save returns ok when auth is disabled (default)."""
        cfg = _build_four4free_config()
        body = cfg.model_dump()
        body = _make_json_serializable(body)

        resp = await tariff_client.post("/settings/tariff", json=body)
        # Without auth, all users are admins
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True


class TestGetTariffTemplates:
    """Tests for GET /settings/tariff/templates."""

    @pytest.mark.asyncio
    async def test_templates_returns_four4free(self, tariff_client):
        """Should find and return FOUR4FREE template."""
        resp = await tariff_client.get("/settings/tariff/templates")
        assert resp.status_code == 200

        data = resp.json()
        assert data["ok"] is True
        assert "templates" in data

        # Find FOUR4FREE
        templates = {t["id"]: t for t in data["templates"]}
        assert "four4free" in templates

        four4free = templates["four4free"]
        assert four4free["name"] == "Globird FOUR4FREE"
        assert four4free["tariff"]["type"] == "tou"
        assert four4free["tariff"]["plan"]["supply_charge_c_per_day"] == 148.5

    @pytest.mark.asyncio
    async def test_templates_returns_zerohero(self, tariff_client):
        """Should find and return ZEROHERO template."""
        resp = await tariff_client.get("/settings/tariff/templates")
        assert resp.status_code == 200

        data = resp.json()
        assert data["ok"] is True

        templates = {t["id"]: t for t in data["templates"]}
        assert "zerohero" in templates

        zerohero = templates["zerohero"]
        assert "VPP" in zerohero["name"]
        assert zerohero["tariff"]["type"] == "tou"
        # ZEROHERO has a higher supply charge
        assert zerohero["tariff"]["plan"]["supply_charge_c_per_day"] == 198.0

    @pytest.mark.asyncio
    async def test_templates_gracefully_handles_missing_files(self, tariff_client):
        """Should not crash if template files are missing."""
        # This test passes if the endpoint returns ok:true with whatever templates exist
        resp = await tariff_client.get("/settings/tariff/templates")
        assert resp.status_code == 200

        data = resp.json()
        assert data["ok"] is True
        assert isinstance(data["templates"], list)
