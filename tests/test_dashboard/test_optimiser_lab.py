from __future__ import annotations

import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from power_master.config.manager import ConfigManager
from power_master.optimiser_lab.app import create_lab_app


@pytest.fixture
def lab_config_manager(tmp_path: Path) -> ConfigManager:
    defaults = tmp_path / "config.defaults.yaml"
    defaults.write_text("db:\n  path: ':memory:'\n")
    user = tmp_path / "config.yaml"
    mgr = ConfigManager(defaults_path=defaults, user_path=user)
    mgr.load()
    return mgr


@pytest.fixture
async def lab_client(repo, lab_config_manager):
    app = create_lab_app(lab_config_manager.config, repo, config_manager=lab_config_manager)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestOptimiserLabStandalone:
    @pytest.mark.asyncio
    async def test_page_loads(self, lab_client) -> None:
        resp = await lab_client.get("/optimiser-lab")
        assert resp.status_code == 200
        assert "Optimiser Lab" in resp.text

    @pytest.mark.asyncio
    async def test_backtest_run(self, lab_client, repo) -> None:
        start = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
        for i in range(6):
            ts = (start + timedelta(minutes=30 * i)).isoformat()
            await repo.store_historical("load_w", 1000.0, "test", ts)
            await repo.store_historical("solar_w", 200.0, "test", ts)
            await repo.store_historical("import_price_cents", 20.0, "test", ts)
            await repo.store_historical("export_price_cents", 5.0, "test", ts)

        resp = await lab_client.post(
            "/optimiser-lab",
            data={
                "action": "run",
                "start_iso": start.isoformat(),
                "end_iso": (start + timedelta(minutes=30 * 5)).isoformat(),
                "initial_soc": "0.5",
                "initial_wacb_cents": "10.0",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Financial Impact" in resp.text

    @pytest.mark.asyncio
    async def test_save_and_load_experiment(self, lab_client, repo) -> None:
        start = datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc)
        for i in range(6):
            ts = (start + timedelta(minutes=30 * i)).isoformat()
            await repo.store_historical("load_w", 900.0, "test", ts)
            await repo.store_historical("solar_w", 250.0, "test", ts)
            await repo.store_historical("import_price_cents", 18.0, "test", ts)
            await repo.store_historical("export_price_cents", 7.0, "test", ts)

        start_resp = await lab_client.post(
            "/optimiser-lab/start",
            data={
                "start_iso": start.isoformat(),
                "end_iso": (start + timedelta(minutes=30 * 5)).isoformat(),
                "initial_soc": "0.5",
                "initial_wacb_cents": "10.0",
            },
        )
        assert start_resp.status_code == 200
        job_id = start_resp.json()["job_id"]

        for _ in range(400):
            status_resp = await lab_client.get(f"/optimiser-lab/job/{job_id}")
            assert status_resp.status_code == 200
            payload = status_resp.json()
            if payload.get("status") == "done":
                break
            if payload.get("status") == "error":
                pytest.fail(f"backtest job failed: {payload.get('message')}")
            await asyncio.sleep(0.1)
        else:
            pytest.fail("backtest job did not complete")

        save_resp = await lab_client.post(
            "/optimiser-lab",
            data={
                "action": "save_experiment",
                "job_id": job_id,
                "experiment_name": "test-save-load",
            },
            follow_redirects=True,
        )
        assert save_resp.status_code == 200
        assert "Experiment saved." in save_resp.text
        assert "test-save-load" in save_resp.text

        async with repo.db.execute(
            "SELECT id FROM optimiser_lab_experiments WHERE name = ? ORDER BY id DESC LIMIT 1",
            ("test-save-load",),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        exp_id = int(row["id"])

        load_resp = await lab_client.get(f"/optimiser-lab?load_experiment_id={exp_id}")
        assert load_resp.status_code == 200
        assert "Loaded experiment: test-save-load" in load_resp.text
        assert "Financial Impact" in load_resp.text

    @pytest.mark.asyncio
    async def test_cancel_running_job(self, lab_client, repo) -> None:
        start = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
        for i in range(240):
            ts = (start + timedelta(minutes=30 * i)).isoformat()
            await repo.store_historical("load_w", 1000.0, "test", ts)
            await repo.store_historical("solar_w", 150.0, "test", ts)
            await repo.store_historical("import_price_cents", 20.0 + (i % 8), "test", ts)
            await repo.store_historical("export_price_cents", 6.0 + (i % 3), "test", ts)

        start_resp = await lab_client.post(
            "/optimiser-lab/start",
            data={
                "start_iso": start.isoformat(),
                "end_iso": (start + timedelta(minutes=30 * 239)).isoformat(),
                "initial_soc": "0.5",
                "initial_wacb_cents": "10.0",
                "replan_every_slots": "1",
            },
        )
        assert start_resp.status_code == 200
        job_id = start_resp.json()["job_id"]

        cancel_resp = await lab_client.post(f"/optimiser-lab/job/{job_id}/cancel")
        assert cancel_resp.status_code == 200
        assert cancel_resp.json().get("status") == "ok"

        for _ in range(200):
            status_resp = await lab_client.get(f"/optimiser-lab/job/{job_id}")
            payload = status_resp.json()
            if payload.get("status") in ("cancelled", "done", "error"):
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("job did not finish after cancellation request")

        assert payload.get("status") == "cancelled"

    @pytest.mark.asyncio
    async def test_persists_last_used_settings_on_reload(self, lab_client, repo) -> None:
        start = datetime(2025, 1, 4, 0, 0, tzinfo=timezone.utc)
        for i in range(6):
            ts = (start + timedelta(minutes=30 * i)).isoformat()
            await repo.store_historical("load_w", 1000.0, "test", ts)
            await repo.store_historical("solar_w", 200.0, "test", ts)
            await repo.store_historical("import_price_cents", 20.0, "test", ts)
            await repo.store_historical("export_price_cents", 5.0, "test", ts)

        start_resp = await lab_client.post(
            "/optimiser-lab/start",
            data={
                "start_iso": start.isoformat(),
                "end_iso": (start + timedelta(minutes=30 * 5)).isoformat(),
                "initial_soc": "0.61",
                "initial_wacb_cents": "12.3",
                "use_forecast_pricing": "on",
                "replan_every_slots": "9",
                "planning.horizon_hours": "72",
                "battery.capacity_wh": "12345",
                "arbitrage.spike_response_mode": "conservative",
            },
        )
        assert start_resp.status_code == 200

        page = await lab_client.get("/optimiser-lab")
        assert page.status_code == 200
        assert 'name="start_iso" value="2025-01-04T00:00:00+00:00"' in page.text
        assert 'name="initial_soc" value="0.61"' in page.text
        assert 'name="initial_wacb_cents" value="12.3"' in page.text
        assert 'id="use_forecast_pricing" name="use_forecast_pricing" checked' in page.text
        assert 'name="replan_every_slots" value="9"' in page.text
        assert 'name="planning.horizon_hours" value="72"' in page.text
        assert 'name="battery.capacity_wh" value="12345"' in page.text
        assert '<option value="conservative" selected>Conservative</option>' in page.text
