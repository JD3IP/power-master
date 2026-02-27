"""Historical backtest harness for optimiser tuning."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from power_master.config.schema import AppConfig
from power_master.db.repository import Repository
from power_master.optimisation.plan import SlotMode
from power_master.optimisation.solver import SolverInputs, solve


@dataclass
class BacktestSummary:
    slots: int
    import_kwh: float
    export_kwh: float
    import_cost_cents: float
    export_revenue_cents: float
    net_cost_cents: float
    planner_net_cost_cents: float
    battery_throughput_kwh: float
    final_soc: float


@dataclass
class BacktestResult:
    summary: BacktestSummary
    daily_rows: list[dict[str, Any]]
    slot_rows: list[dict[str, Any]]


def _as_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _slot_key(ts: datetime) -> datetime:
    """Normalize a timestamp to its 30-minute slot boundary in UTC."""
    ts = ts.astimezone(timezone.utc)
    minute = 30 if ts.minute >= 30 else 0
    return ts.replace(minute=minute, second=0, microsecond=0)


def _interp(prev_val: float | None, current_val: float | None, fallback: float = 0.0) -> float:
    if current_val is not None:
        return float(current_val)
    if prev_val is not None:
        return float(prev_val)
    return fallback


async def _load_series(
    repo: Repository,
    start: datetime,
    end: datetime,
) -> list[dict[str, float | datetime]]:
    start_iso = start.astimezone(timezone.utc).isoformat()
    end_iso = end.astimezone(timezone.utc).isoformat()

    load_rows = await repo.get_historical("load_w", start_iso, end_iso)
    solar_rows = await repo.get_historical("solar_w", start_iso, end_iso)
    import_rows = await repo.get_historical("import_price_cents", start_iso, end_iso)
    export_rows = await repo.get_historical("export_price_cents", start_iso, end_iso)
    forecast_import_rows = await repo.get_historical("forecast_import_price_cents", start_iso, end_iso)
    forecast_export_rows = await repo.get_historical("forecast_export_price_cents", start_iso, end_iso)

    load_by = {_slot_key(_as_utc(r["recorded_at"])): float(r["value"]) for r in load_rows}
    solar_by = {_slot_key(_as_utc(r["recorded_at"])): float(r["value"]) for r in solar_rows}
    import_by = {_slot_key(_as_utc(r["recorded_at"])): float(r["value"]) for r in import_rows}
    export_by = {_slot_key(_as_utc(r["recorded_at"])): float(r["value"]) for r in export_rows}
    forecast_import_by = {_slot_key(_as_utc(r["recorded_at"])): float(r["value"]) for r in forecast_import_rows}
    forecast_export_by = {_slot_key(_as_utc(r["recorded_at"])): float(r["value"]) for r in forecast_export_rows}

    timeline = sorted(import_by.keys())
    if not timeline:
        return []

    rows: list[dict[str, float | datetime]] = []
    prev_load: float | None = None
    prev_solar: float | None = None
    prev_export: float | None = None
    prev_forecast_import: float | None = None
    prev_forecast_export: float | None = None
    for ts in timeline:
        load_w = _interp(prev_load, load_by.get(ts))
        solar_w = _interp(prev_solar, solar_by.get(ts))
        export_cents = _interp(prev_export, export_by.get(ts))
        forecast_import_cents = _interp(prev_forecast_import, forecast_import_by.get(ts), import_by[ts])
        forecast_export_cents = _interp(prev_forecast_export, forecast_export_by.get(ts), export_cents)
        prev_load = load_w
        prev_solar = solar_w
        prev_export = export_cents
        prev_forecast_import = forecast_import_cents
        prev_forecast_export = forecast_export_cents
        rows.append(
            {
                "ts": ts,
                "load_w": max(0.0, load_w),
                "solar_w": max(0.0, solar_w),
                "import_cents": max(0.0, import_by[ts]),
                "export_cents": max(0.0, export_cents),
                "forecast_import_cents": max(0.0, forecast_import_cents),
                "forecast_export_cents": max(0.0, forecast_export_cents),
            }
        )
    return rows


def _apply_first_slot(
    slot_mode: SlotMode,
    target_power_w: int,
    load_w: float,
    solar_w: float,
    soc: float,
    config: AppConfig,
    slot_hours: float,
) -> dict[str, float]:
    cap_wh = float(config.battery.capacity_wh)
    eff = config.battery.round_trip_efficiency ** 0.5

    max_charge = float(config.battery.max_charge_rate_w)
    max_discharge = float(config.battery.max_discharge_rate_w)

    max_charge_by_soc = max(0.0, (config.battery.soc_max_hard - soc) * cap_wh / (slot_hours * eff))
    max_discharge_by_soc = max(0.0, (soc - config.battery.soc_min_hard) * cap_wh * eff / slot_hours)

    charge_w = 0.0
    discharge_w = 0.0
    net_load = max(0.0, load_w - solar_w)
    excess_solar = max(0.0, solar_w - load_w)

    if slot_mode == SlotMode.FORCE_CHARGE:
        charge_w = min(float(target_power_w), max_charge, max_charge_by_soc)
    elif slot_mode == SlotMode.FORCE_DISCHARGE:
        discharge_w = min(float(target_power_w), max_discharge, max_discharge_by_soc)
    else:
        if net_load > 0:
            discharge_w = min(net_load, max_discharge, max_discharge_by_soc)
        elif excess_solar > 0:
            charge_w = min(excess_solar, max_charge, max_charge_by_soc)

    grid_w = load_w + charge_w - solar_w - discharge_w
    soc_next = soc + (charge_w * slot_hours * eff) / cap_wh - (discharge_w * slot_hours) / (eff * cap_wh)
    soc_next = min(config.battery.soc_max_hard, max(config.battery.soc_min_hard, soc_next))

    return {
        "charge_w": charge_w,
        "discharge_w": discharge_w,
        "grid_import_w": max(0.0, grid_w),
        "grid_export_w": max(0.0, -grid_w),
        "soc_next": soc_next,
    }


def _horizon_slice(values: list[float], start_idx: int, n_slots: int) -> list[float]:
    out: list[float] = []
    last = values[-1] if values else 0.0
    for i in range(start_idx, start_idx + n_slots):
        out.append(values[i] if i < len(values) else last)
    return out


async def run_backtest(
    repo: Repository,
    config: AppConfig,
    start: datetime,
    end: datetime,
    initial_soc: float = 0.50,
    initial_wacb_cents: float = 10.0,
    use_forecast_prices_for_planning: bool = False,
    replan_every_slots: int = 1,
    progress_callback: Callable[[int, int, datetime], None] | None = None,
    progress_heartbeat_seconds: float = 5.0,
) -> BacktestResult:
    rows = await _load_series(repo, start, end)
    if not rows:
        return BacktestResult(
            summary=BacktestSummary(
                slots=0,
                import_kwh=0.0,
                export_kwh=0.0,
                import_cost_cents=0.0,
                export_revenue_cents=0.0,
                net_cost_cents=0.0,
                planner_net_cost_cents=0.0,
                battery_throughput_kwh=0.0,
                final_soc=initial_soc,
            ),
            daily_rows=[],
            slot_rows=[],
        )

    slot_hours = config.planning.slot_duration_minutes / 60.0
    horizon_slots = max(1, int(config.planning.horizon_hours / slot_hours))
    replan_every_slots = max(1, int(replan_every_slots))

    ts_list = [r["ts"] for r in rows]
    load_list = [float(r["load_w"]) for r in rows]
    solar_list = [float(r["solar_w"]) for r in rows]
    import_list = [float(r["import_cents"]) for r in rows]
    export_list = [float(r["export_cents"]) for r in rows]
    forecast_import_list = [float(r["forecast_import_cents"]) for r in rows]
    forecast_export_list = [float(r["forecast_export_cents"]) for r in rows]
    planner_import_list = forecast_import_list if use_forecast_prices_for_planning else import_list
    planner_export_list = forecast_export_list if use_forecast_prices_for_planning else export_list

    soc = max(config.battery.soc_min_hard, min(config.battery.soc_max_hard, initial_soc))

    import_wh_total = 0.0
    export_wh_total = 0.0
    import_cost_cents = 0.0
    export_revenue_cents = 0.0
    planner_import_cost_cents = 0.0
    planner_export_revenue_cents = 0.0
    throughput_wh = 0.0

    daily: dict[str, dict[str, float]] = {}
    slot_rows: list[dict[str, Any]] = []
    current_plan = None
    plan_offset = 0

    for i, ts in enumerate(ts_list):
        if progress_callback:
            progress_callback(i + 1, len(ts_list), ts)
        need_replan = (
            current_plan is None
            or plan_offset >= replan_every_slots
            or plan_offset >= len(current_plan.slots)
        )
        if need_replan:
            horizon_inputs = SolverInputs(
                solar_forecast_w=_horizon_slice(solar_list, i, horizon_slots),
                load_forecast_w=_horizon_slice(load_list, i, horizon_slots),
                import_rate_cents=_horizon_slice(planner_import_list, i, horizon_slots),
                export_rate_cents=_horizon_slice(planner_export_list, i, horizon_slots),
                is_spike=[
                    p >= float(config.arbitrage.spike_threshold_cents)
                    for p in _horizon_slice(import_list, i, horizon_slots)
                ],
                current_soc=soc,
                wacb_cents=initial_wacb_cents,
                storm_active=False,
                storm_reserve_soc=0.0,
                slot_start_times=[
                    ts + timedelta(minutes=config.planning.slot_duration_minutes * j)
                    for j in range(horizon_slots)
                ],
            )
            solve_task = asyncio.create_task(
                asyncio.to_thread(solve, config, horizon_inputs, "lab_backtest")
            )
            heartbeat = max(1.0, float(progress_heartbeat_seconds))
            while True:
                try:
                    # Wait for completion, but emit progress heartbeats for long solves.
                    current_plan = await asyncio.wait_for(solve_task, timeout=heartbeat)
                    break
                except TimeoutError:
                    if progress_callback:
                        progress_callback(i + 1, len(ts_list), ts)
            plan_offset = 0

        first = current_plan.slots[min(plan_offset, len(current_plan.slots) - 1)]

        flows = _apply_first_slot(
            slot_mode=first.mode,
            target_power_w=first.target_power_w,
            load_w=load_list[i],
            solar_w=solar_list[i],
            soc=soc,
            config=config,
            slot_hours=slot_hours,
        )
        soc = flows["soc_next"]

        import_wh = flows["grid_import_w"] * slot_hours
        export_wh = flows["grid_export_w"] * slot_hours
        import_wh_total += import_wh
        export_wh_total += export_wh
        import_cost_cents += (import_wh / 1000.0) * import_list[i]
        export_revenue_cents += (export_wh / 1000.0) * export_list[i]
        planner_import_cost_cents += (import_wh / 1000.0) * planner_import_list[i]
        planner_export_revenue_cents += (export_wh / 1000.0) * planner_export_list[i]
        throughput_wh += (flows["charge_w"] + flows["discharge_w"]) * slot_hours
        slot_value_cents = (export_wh / 1000.0) * export_list[i] - (import_wh / 1000.0) * import_list[i]
        planner_slot_value_cents = (
            (export_wh / 1000.0) * planner_export_list[i]
            - (import_wh / 1000.0) * planner_import_list[i]
        )

        day_key = ts.date().isoformat()
        d = daily.setdefault(
            day_key,
            {"import_cost_cents": 0.0, "export_revenue_cents": 0.0, "import_kwh": 0.0, "export_kwh": 0.0},
        )
        d["import_cost_cents"] += (import_wh / 1000.0) * import_list[i]
        d["export_revenue_cents"] += (export_wh / 1000.0) * export_list[i]
        d["import_kwh"] += import_wh / 1000.0
        d["export_kwh"] += export_wh / 1000.0

        slot_rows.append(
            {
                "ts": ts.isoformat(),
                "soc": round(soc, 4),
                "load_kw": round(load_list[i] / 1000.0, 4),
                "solar_kw": round(solar_list[i] / 1000.0, 4),
                "charge_kw": round(flows["charge_w"] / 1000.0, 4),
                "discharge_kw": round(flows["discharge_w"] / 1000.0, 4),
                "grid_import_kw": round(flows["grid_import_w"] / 1000.0, 4),
                "grid_export_kw": round(flows["grid_export_w"] / 1000.0, 4),
                "import_cents": round(import_list[i], 4),
                "export_cents": round(export_list[i], 4),
                "planner_import_cents": round(planner_import_list[i], 4),
                "planner_export_cents": round(planner_export_list[i], 4),
                "slot_value_cents": round(slot_value_cents, 4),
                "planner_slot_value_cents": round(planner_slot_value_cents, 4),
                "mode": int(first.mode),
            }
        )
        plan_offset += 1

    daily_rows = []
    for day, vals in sorted(daily.items()):
        net = vals["import_cost_cents"] - vals["export_revenue_cents"]
        daily_rows.append(
            {
                "day": day,
                "import_kwh": round(vals["import_kwh"], 3),
                "export_kwh": round(vals["export_kwh"], 3),
                "import_cost_cents": round(vals["import_cost_cents"], 2),
                "export_revenue_cents": round(vals["export_revenue_cents"], 2),
                "net_cost_cents": round(net, 2),
            }
        )

    summary = BacktestSummary(
        slots=len(ts_list),
        import_kwh=round(import_wh_total / 1000.0, 3),
        export_kwh=round(export_wh_total / 1000.0, 3),
        import_cost_cents=round(import_cost_cents, 2),
        export_revenue_cents=round(export_revenue_cents, 2),
        net_cost_cents=round(import_cost_cents - export_revenue_cents, 2),
        planner_net_cost_cents=round(planner_import_cost_cents - planner_export_revenue_cents, 2),
        battery_throughput_kwh=round(throughput_wh / 1000.0, 3),
        final_soc=round(soc, 4),
    )
    return BacktestResult(summary=summary, daily_rows=daily_rows, slot_rows=slot_rows)
