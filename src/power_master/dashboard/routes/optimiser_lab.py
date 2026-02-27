"""Optimiser lab: backtest tuning UI on historical datasets."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict, is_dataclass
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from power_master.config.manager import ConfigManager
from power_master.config.schema import AppConfig
from power_master.dashboard.auth import require_admin
from power_master.optimisation.backtest_lab import BacktestResult, run_backtest

router = APIRouter()

_TUNABLE_FIELDS = [
    "battery.capacity_wh",
    "battery.max_charge_rate_w",
    "battery.max_discharge_rate_w",
    "battery.max_grid_import_w",
    "battery_targets.evening_soc_target",
    "battery_targets.evening_target_hour",
    "battery_targets.morning_soc_minimum",
    "battery_targets.morning_minimum_hour",
    "battery_targets.daytime_reserve_soc_target",
    "battery_targets.daytime_reserve_start_hour",
    "battery_targets.daytime_reserve_end_hour",
    "arbitrage.break_even_delta_cents",
    "arbitrage.spike_threshold_cents",
    "arbitrage.spike_response_mode",
    "arbitrage.price_dampen_threshold_cents",
    "arbitrage.price_dampen_factor",
    "fixed_costs.hedging_per_kwh_cents",
    "planning.horizon_hours",
]

_DEFAULT_LAB_PARAMS = {
    "start_iso": "2025-01-01T00:00:00+00:00",
    "end_iso": "2025-12-31T23:30:00+00:00",
    "initial_soc": "0.50",
    "initial_wacb_cents": "10.0",
    "use_forecast_pricing": False,
    "replan_every_slots": "6",
}


@dataclass
class LabJob:
    id: str
    status: str = "running"  # running, done, cancelled, error
    message: str = ""
    completed_slots: int = 0
    total_slots: int = 0
    current_ts: str = ""
    started_at: str = ""
    finished_at: str = ""
    results: dict[str, Any] | None = None
    config: AppConfig | None = None
    params: dict[str, Any] | None = None
    cancel_requested: bool = False
    task: asyncio.Task[Any] | None = None


_LAB_JOBS: dict[str, LabJob] = {}


class JobCancelled(Exception):
    pass


def _parse_form_to_nested(form_data: dict[str, Any], allowed_keys: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in allowed_keys:
        if key not in form_data:
            continue
        parts = key.split(".")
        current = result
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = form_data[key]
    return result


def _build_candidate_config(base_config: AppConfig, form_data: dict[str, Any]) -> AppConfig:
    base_raw = base_config.model_dump(mode="python")
    updates = _parse_form_to_nested(form_data, _TUNABLE_FIELDS)
    merged = ConfigManager._deep_merge(base_raw, updates)
    return AppConfig.model_validate(merged)


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _build_run_params(form_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "start_iso": str(form_data.get("start_iso", _DEFAULT_LAB_PARAMS["start_iso"])),
        "end_iso": str(form_data.get("end_iso", _DEFAULT_LAB_PARAMS["end_iso"])),
        "initial_soc": str(form_data.get("initial_soc", _DEFAULT_LAB_PARAMS["initial_soc"])),
        "initial_wacb_cents": str(form_data.get("initial_wacb_cents", _DEFAULT_LAB_PARAMS["initial_wacb_cents"])),
        "use_forecast_pricing": str(form_data.get("use_forecast_pricing", "")).lower() in ("on", "1", "true", "yes"),
        "replan_every_slots": str(form_data.get("replan_every_slots", _DEFAULT_LAB_PARAMS["replan_every_slots"])),
    }


async def _ensure_experiments_table(repo) -> None:
    await repo.db.execute(
        """
        CREATE TABLE IF NOT EXISTS optimiser_lab_experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            params_json TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            results_json TEXT NOT NULL
        )
        """
    )
    await repo.db.commit()


async def _ensure_state_table(repo) -> None:
    await repo.db.execute(
        """
        CREATE TABLE IF NOT EXISTS optimiser_lab_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            updated_at TEXT NOT NULL,
            params_json TEXT NOT NULL,
            settings_json TEXT NOT NULL
        )
        """
    )
    await repo.db.commit()


async def _save_last_state(repo, config: AppConfig, params: dict[str, Any]) -> None:
    await _ensure_state_table(repo)
    await repo.db.execute(
        """
        INSERT OR REPLACE INTO optimiser_lab_state (id, updated_at, params_json, settings_json)
        VALUES (1, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            json.dumps(params),
            json.dumps(config.model_dump(mode="python")),
        ),
    )
    await repo.db.commit()


async def _load_last_state(repo) -> dict[str, Any] | None:
    await _ensure_state_table(repo)
    async with repo.db.execute(
        "SELECT params_json, settings_json FROM optimiser_lab_state WHERE id = 1",
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return None

    try:
        params_raw = json.loads(row["params_json"])
        settings_raw = json.loads(row["settings_json"])
        return {
            "params": _build_run_params(params_raw if isinstance(params_raw, dict) else {}),
            "config": AppConfig.model_validate(settings_raw),
        }
    except Exception:
        return None


async def _list_experiments(repo, limit: int = 100) -> list[dict[str, Any]]:
    await _ensure_experiments_table(repo)
    async with repo.db.execute(
        "SELECT id, name, created_at, results_json FROM optimiser_lab_experiments ORDER BY id DESC LIMIT ?",
        (limit,),
    ) as cursor:
        rows = await cursor.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        results = json.loads(row["results_json"])
        candidate = results.get("candidate", {})
        out.append(
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "created_at": str(row["created_at"]),
                "candidate_net_dollars": round(float(candidate.get("net_cost_cents", 0.0)) / 100.0, 2),
                "delta_dollars": round(float(results.get("delta_dollars", 0.0)), 2),
                "final_soc_pct": round(float(candidate.get("final_soc", 0.0)) * 100.0, 1),
            }
        )
    return out


async def _get_experiment(repo, experiment_id: int) -> dict[str, Any] | None:
    await _ensure_experiments_table(repo)
    async with repo.db.execute(
        "SELECT * FROM optimiser_lab_experiments WHERE id = ?",
        (experiment_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return None
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "created_at": str(row["created_at"]),
        "params": json.loads(row["params_json"]),
        "settings": json.loads(row["settings_json"]),
        "results": json.loads(row["results_json"]),
    }


def _compare_experiments(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    a_results = a.get("results", {})
    b_results = b.get("results", {})
    a_candidate = a_results.get("candidate", {})
    b_candidate = b_results.get("candidate", {})
    a_net = float(a_candidate.get("net_cost_cents", 0.0))
    b_net = float(b_candidate.get("net_cost_cents", 0.0))
    a_export = float(a_candidate.get("export_revenue_cents", 0.0))
    b_export = float(b_candidate.get("export_revenue_cents", 0.0))
    a_import = float(a_candidate.get("import_cost_cents", 0.0))
    b_import = float(b_candidate.get("import_cost_cents", 0.0))
    a_soc = float(a_candidate.get("final_soc", 0.0))
    b_soc = float(b_candidate.get("final_soc", 0.0))
    delta_cents = b_net - a_net
    return {
        "a": {"id": a["id"], "name": a["name"]},
        "b": {"id": b["id"], "name": b["name"]},
        "net_delta_dollars": round(delta_cents / 100.0, 2),
        "import_delta_dollars": round((b_import - a_import) / 100.0, 2),
        "export_delta_dollars": round((b_export - a_export) / 100.0, 2),
        "final_soc_delta_pct": round((b_soc - a_soc) * 100.0, 1),
        "b_is_better": delta_cents < 0,
    }


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value


@router.get("/optimiser-lab", response_class=HTMLResponse)
async def optimiser_lab_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    config: AppConfig = request.app.state.config
    repo = request.app.state.repo
    experiments = await _list_experiments(repo)
    load_experiment_id_raw = request.query_params.get("load_experiment_id", "")
    compare_a_raw = request.query_params.get("compare_a", "")
    compare_b_raw = request.query_params.get("compare_b", "")
    compare: dict[str, Any] | None = None
    last_state = await _load_last_state(repo)
    default_config = last_state["config"] if last_state else config
    default_params = last_state["params"] if last_state else _DEFAULT_LAB_PARAMS

    loaded_experiment: dict[str, Any] | None = None
    if load_experiment_id_raw:
        try:
            loaded_experiment = await _get_experiment(repo, int(load_experiment_id_raw))
        except ValueError:
            loaded_experiment = None

    if compare_a_raw and compare_b_raw:
        try:
            a = await _get_experiment(repo, int(compare_a_raw))
            b = await _get_experiment(repo, int(compare_b_raw))
            if a and b:
                compare = _compare_experiments(a, b)
        except ValueError:
            compare = None

    if loaded_experiment:
        loaded_config = AppConfig.model_validate(loaded_experiment["settings"])
        loaded_params = loaded_experiment["params"]
        loaded_results = loaded_experiment["results"]
        return templates.TemplateResponse(
            "optimiser_lab.html",
            {
                "request": request,
                "config": loaded_config,
                "results": loaded_results,
                "saved_kind": request.query_params.get("saved", ""),
                "error": request.query_params.get("error", ""),
                "default_start": loaded_params.get("start_iso", _DEFAULT_LAB_PARAMS["start_iso"]),
                "default_end": loaded_params.get("end_iso", _DEFAULT_LAB_PARAMS["end_iso"]),
                "initial_soc": loaded_params.get("initial_soc", _DEFAULT_LAB_PARAMS["initial_soc"]),
                "initial_wacb_cents": loaded_params.get("initial_wacb_cents", _DEFAULT_LAB_PARAMS["initial_wacb_cents"]),
                "use_forecast_pricing": bool(loaded_params.get("use_forecast_pricing", False)),
                "replan_every_slots": str(loaded_params.get("replan_every_slots", _DEFAULT_LAB_PARAMS["replan_every_slots"])),
                "experiments": experiments,
                "compare": compare,
                "loaded_experiment_id": int(loaded_experiment["id"]),
                "loaded_experiment_name": loaded_experiment["name"],
                "job_id": "",
                "compare_a": compare_a_raw,
                "compare_b": compare_b_raw,
            },
        )

    job_id = request.query_params.get("job_id", "")
    job = _LAB_JOBS.get(job_id) if job_id else None
    if job and job.status == "done" and job.results and job.config and job.params:
        return templates.TemplateResponse(
            "optimiser_lab.html",
            {
                "request": request,
                "config": job.config,
                "results": job.results,
                "saved_kind": request.query_params.get("saved", ""),
                "error": request.query_params.get("error", ""),
                "default_start": job.params.get("start_iso", _DEFAULT_LAB_PARAMS["start_iso"]),
                "default_end": job.params.get("end_iso", _DEFAULT_LAB_PARAMS["end_iso"]),
                "initial_soc": job.params.get("initial_soc", _DEFAULT_LAB_PARAMS["initial_soc"]),
                "initial_wacb_cents": job.params.get("initial_wacb_cents", _DEFAULT_LAB_PARAMS["initial_wacb_cents"]),
                "use_forecast_pricing": bool(job.params.get("use_forecast_pricing", False)),
                "replan_every_slots": str(job.params.get("replan_every_slots", _DEFAULT_LAB_PARAMS["replan_every_slots"])),
                "experiments": experiments,
                "compare": compare,
                "loaded_experiment_id": 0,
                "loaded_experiment_name": "",
                "job_id": job_id,
                "compare_a": compare_a_raw,
                "compare_b": compare_b_raw,
            },
        )
    return templates.TemplateResponse(
        "optimiser_lab.html",
        {
            "request": request,
            "config": default_config,
            "results": None,
            "saved_kind": request.query_params.get("saved", ""),
            "error": request.query_params.get("error", ""),
            "default_start": str(default_params["start_iso"]),
            "default_end": str(default_params["end_iso"]),
            "initial_soc": str(default_params["initial_soc"]),
            "initial_wacb_cents": str(default_params["initial_wacb_cents"]),
            "use_forecast_pricing": bool(default_params["use_forecast_pricing"]),
            "replan_every_slots": str(default_params["replan_every_slots"]),
            "experiments": experiments,
            "compare": compare,
            "loaded_experiment_id": 0,
            "loaded_experiment_name": "",
            "job_id": "",
            "compare_a": compare_a_raw,
            "compare_b": compare_b_raw,
        },
    )


async def _run_job(
    app,
    job_id: str,
    base_config: AppConfig,
    candidate_config: AppConfig,
    repo,
    start: datetime,
    end: datetime,
    initial_soc: float,
    initial_wacb_cents: float,
    use_forecast_pricing: bool,
    replan_every_slots: int,
    params: dict[str, Any],
) -> None:
    job = _LAB_JOBS[job_id]
    try:
        slot_minutes = max(1, int(base_config.planning.slot_duration_minutes))
        n_slots = max(1, int(((end - start).total_seconds() // 60) // slot_minutes) + 1)
        job.total_slots = n_slots * 2
        job.started_at = datetime.now(timezone.utc).isoformat()

        def baseline_progress(done: int, total: int, ts: datetime) -> None:
            if job.cancel_requested:
                raise JobCancelled("Cancelled by user")
            job.completed_slots = min(job.total_slots, done)
            job.current_ts = ts.isoformat()
            job.message = "Baseline simulation"

        baseline: BacktestResult = await run_backtest(
            repo=repo,
            config=base_config,
            start=start,
            end=end,
            initial_soc=initial_soc,
            initial_wacb_cents=initial_wacb_cents,
            use_forecast_prices_for_planning=use_forecast_pricing,
            replan_every_slots=replan_every_slots,
            progress_callback=baseline_progress,
        )
        if job.cancel_requested:
            raise JobCancelled("Cancelled by user")

        def candidate_progress(done: int, total: int, ts: datetime) -> None:
            if job.cancel_requested:
                raise JobCancelled("Cancelled by user")
            job.completed_slots = min(job.total_slots, n_slots + done)
            job.current_ts = ts.isoformat()
            job.message = "Candidate simulation"

        candidate: BacktestResult = await run_backtest(
            repo=repo,
            config=candidate_config,
            start=start,
            end=end,
            initial_soc=initial_soc,
            initial_wacb_cents=initial_wacb_cents,
            use_forecast_prices_for_planning=use_forecast_pricing,
            replan_every_slots=replan_every_slots,
            progress_callback=candidate_progress,
        )

        delta_cents = candidate.summary.net_cost_cents - baseline.summary.net_cost_cents
        results = {
            "baseline": baseline.summary,
            "candidate": candidate.summary,
            "daily_rows": candidate.daily_rows,
            "slot_rows": candidate.slot_rows,
            "delta_cents": round(delta_cents, 2),
            "delta_dollars": round(delta_cents / 100.0, 2),
            "forecast_error_cents": round(candidate.summary.net_cost_cents - candidate.summary.planner_net_cost_cents, 2),
            "forecast_error_dollars": round(
                (candidate.summary.net_cost_cents - candidate.summary.planner_net_cost_cents) / 100.0, 2,
            ),
            "improved": delta_cents < 0,
        }
        job.results = results
        job.config = candidate_config
        job.params = params
        job.status = "done"
        job.completed_slots = job.total_slots
        job.finished_at = datetime.now(timezone.utc).isoformat()
        job.message = "Completed"
    except JobCancelled:
        job.status = "cancelled"
        job.message = "Cancelled by user"
        job.finished_at = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        job.status = "error"
        job.message = str(exc)
        job.finished_at = datetime.now(timezone.utc).isoformat()


@router.post("/optimiser-lab/start", response_model=None)
async def optimiser_lab_start(request: Request) -> JSONResponse:
    base_config: AppConfig = request.app.state.config
    repo = request.app.state.repo
    form = await request.form()
    form_data = dict(form)
    start = _parse_dt(str(form_data.get("start_iso")))
    end = _parse_dt(str(form_data.get("end_iso")))
    if end <= start:
        return JSONResponse({"status": "error", "message": "End must be after start"}, status_code=400)
    initial_soc = float(form_data.get("initial_soc", "0.5"))
    initial_wacb_cents = float(form_data.get("initial_wacb_cents", "10.0"))
    use_forecast_pricing = str(form_data.get("use_forecast_pricing", "")).lower() in ("on", "1", "true", "yes")
    replan_every_slots = int(form_data.get("replan_every_slots", "6"))
    candidate_config = _build_candidate_config(base_config, form_data)

    job_id = uuid.uuid4().hex
    _LAB_JOBS[job_id] = LabJob(id=job_id)
    params = _build_run_params(form_data)
    await _save_last_state(repo, candidate_config, params)
    task = asyncio.create_task(
        _run_job(
            app=request.app,
            job_id=job_id,
            base_config=base_config,
            candidate_config=candidate_config,
            repo=repo,
            start=start,
            end=end,
            initial_soc=initial_soc,
            initial_wacb_cents=initial_wacb_cents,
            use_forecast_pricing=use_forecast_pricing,
            replan_every_slots=replan_every_slots,
            params=params,
        )
    )
    _LAB_JOBS[job_id].task = task
    return JSONResponse({"status": "ok", "job_id": job_id})


@router.get("/optimiser-lab/job/{job_id}", response_model=None)
async def optimiser_lab_job_status(job_id: str) -> JSONResponse:
    job = _LAB_JOBS.get(job_id)
    if not job:
        return JSONResponse({"status": "error", "message": "job not found"}, status_code=404)
    pct = 0.0
    if job.total_slots > 0:
        pct = min(100.0, (job.completed_slots / job.total_slots) * 100.0)
    return JSONResponse(
        {
            "status": job.status,
            "message": job.message,
            "completed_slots": job.completed_slots,
            "total_slots": job.total_slots,
            "percent": round(pct, 1),
            "current_ts": job.current_ts,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }
    )


@router.post("/optimiser-lab/job/{job_id}/cancel", response_model=None)
async def optimiser_lab_cancel(job_id: str) -> JSONResponse:
    job = _LAB_JOBS.get(job_id)
    if not job:
        return JSONResponse({"status": "error", "message": "job not found"}, status_code=404)
    if job.status != "running":
        return JSONResponse({"status": job.status, "message": "job is not running"})
    job.cancel_requested = True
    job.message = "Cancelling..."
    return JSONResponse({"status": "ok", "message": "cancel requested"})


@router.post("/optimiser-lab", response_class=HTMLResponse, response_model=None)
async def optimiser_lab_run(request: Request) -> Any:
    templates = request.app.state.templates
    base_config: AppConfig = request.app.state.config
    repo = request.app.state.repo
    form = await request.form()
    form_data = dict(form)
    action = str(form_data.get("action", "run"))

    if action == "export":
        denied = require_admin(request)
        if denied:
            return denied
        config_manager = request.app.state.config_manager
        if config_manager is None:
            return RedirectResponse("/optimiser-lab?error=Config+manager+not+available", status_code=303)
        updates = _parse_form_to_nested(form_data, _TUNABLE_FIELDS)
        application = getattr(request.app.state, "application", None)
        if application is not None:
            await application.reload_config(updates, request.app)
        else:
            request.app.state.config = config_manager.save_user_config(updates)
        await _save_last_state(repo, _build_candidate_config(base_config, form_data), _build_run_params(form_data))
        return RedirectResponse("/optimiser-lab?saved=export", status_code=303)

    if action == "save_experiment":
        repo = request.app.state.repo
        name = str(form_data.get("experiment_name", "")).strip() or f"Experiment {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
        job_id = str(form_data.get("job_id", "")).strip()
        job = _LAB_JOBS.get(job_id) if job_id else None
        if not job or job.status != "done" or not job.results or not job.config or not job.params:
            return RedirectResponse("/optimiser-lab?error=No+completed+run+to+save", status_code=303)
        await _ensure_experiments_table(repo)
        await repo.db.execute(
            """
            INSERT INTO optimiser_lab_experiments (name, created_at, params_json, settings_json, results_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                name,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(job.params),
                json.dumps(job.config.model_dump(mode="python")),
                json.dumps(_to_jsonable(job.results)),
            ),
        )
        await repo.db.commit()
        return RedirectResponse(f"/optimiser-lab?job_id={job_id}&saved=experiment", status_code=303)

    try:
        start = _parse_dt(str(form_data.get("start_iso")))
        end = _parse_dt(str(form_data.get("end_iso")))
        if end <= start:
            raise ValueError("End must be after start")
        initial_soc = float(form_data.get("initial_soc", "0.5"))
        initial_wacb_cents = float(form_data.get("initial_wacb_cents", "10.0"))
        use_forecast_pricing = str(form_data.get("use_forecast_pricing", "")).lower() in ("on", "1", "true", "yes")
        replan_every_slots = int(form_data.get("replan_every_slots", "6"))

        candidate_config = _build_candidate_config(base_config, form_data)
        await _save_last_state(repo, candidate_config, _build_run_params(form_data))
        baseline: BacktestResult = await run_backtest(
            repo=repo,
            config=base_config,
            start=start,
            end=end,
            initial_soc=initial_soc,
            initial_wacb_cents=initial_wacb_cents,
            use_forecast_prices_for_planning=use_forecast_pricing,
            replan_every_slots=replan_every_slots,
        )
        candidate: BacktestResult = await run_backtest(
            repo=repo,
            config=candidate_config,
            start=start,
            end=end,
            initial_soc=initial_soc,
            initial_wacb_cents=initial_wacb_cents,
            use_forecast_prices_for_planning=use_forecast_pricing,
            replan_every_slots=replan_every_slots,
        )

        delta_cents = candidate.summary.net_cost_cents - baseline.summary.net_cost_cents
        results = {
            "baseline": baseline.summary,
            "candidate": candidate.summary,
            "daily_rows": candidate.daily_rows,
            "slot_rows": candidate.slot_rows,
            "delta_cents": round(delta_cents, 2),
            "delta_dollars": round(delta_cents / 100.0, 2),
            "forecast_error_cents": round(candidate.summary.net_cost_cents - candidate.summary.planner_net_cost_cents, 2),
            "forecast_error_dollars": round(
                (candidate.summary.net_cost_cents - candidate.summary.planner_net_cost_cents) / 100.0, 2,
            ),
            "improved": delta_cents < 0,
        }
        return templates.TemplateResponse(
            "optimiser_lab.html",
            {
                "request": request,
                "config": candidate_config,
                "results": results,
                "saved_kind": "",
                "error": "",
                "default_start": start.isoformat(),
                "default_end": end.isoformat(),
                "initial_soc": f"{initial_soc:.4f}",
                "initial_wacb_cents": f"{initial_wacb_cents:.2f}",
                "use_forecast_pricing": use_forecast_pricing,
                "replan_every_slots": str(replan_every_slots),
                "experiments": await _list_experiments(repo),
                "compare": None,
                "loaded_experiment_id": 0,
                "loaded_experiment_name": "",
                "job_id": "",
                "compare_a": "",
                "compare_b": "",
            },
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "optimiser_lab.html",
            {
                "request": request,
                "config": base_config,
                "results": None,
                "saved_kind": "",
                "error": str(exc),
                "default_start": str(form_data.get("start_iso", _DEFAULT_LAB_PARAMS["start_iso"])),
                "default_end": str(form_data.get("end_iso", _DEFAULT_LAB_PARAMS["end_iso"])),
                "initial_soc": str(form_data.get("initial_soc", _DEFAULT_LAB_PARAMS["initial_soc"])),
                "initial_wacb_cents": str(form_data.get("initial_wacb_cents", _DEFAULT_LAB_PARAMS["initial_wacb_cents"])),
                "use_forecast_pricing": str(form_data.get("use_forecast_pricing", "")).lower() in ("on", "1", "true", "yes"),
                "replan_every_slots": str(form_data.get("replan_every_slots", _DEFAULT_LAB_PARAMS["replan_every_slots"])),
                "experiments": await _list_experiments(repo),
                "compare": None,
                "loaded_experiment_id": 0,
                "loaded_experiment_name": "",
                "job_id": "",
                "compare_a": "",
                "compare_b": "",
            },
        )
