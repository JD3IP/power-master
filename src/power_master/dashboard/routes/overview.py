"""Main dashboard overview page."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from power_master.timezone_utils import resolve_timezone

router = APIRouter()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _weather_icon(cloud_avg: float, precip_sum: float, wind_max: float) -> str:
    if precip_sum >= 3.0:
        return "rain"
    if wind_max >= 12.0:
        return "wind"
    if cloud_avg >= 75.0:
        return "cloud"
    if cloud_avg >= 40.0:
        return "partly"
    return "sun"


def _scheduled_load_names(slot: dict) -> list[str]:
    """Extract scheduled load names from a persisted plan slot row."""
    raw = slot.get("scheduled_loads_json")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except Exception:
            pass

    fallback = slot.get("scheduled_loads")
    if isinstance(fallback, list):
        return [str(x) for x in fallback]
    return []


def _mode_name(mode: int) -> str:
    names = {
        1: "Self-Use",
        2: "Zero Export",
        3: "Force Charge",
        4: "Force Discharge",
        5: "Battery Hold",
    }
    return names.get(int(mode), f"Mode {mode}")


def _build_plan_transition_events(plan_slots: list[dict], local_tz) -> list[dict]:
    """Build mode/load transition events at slot boundaries."""
    if not plan_slots:
        return []

    sorted_slots = sorted(plan_slots, key=lambda s: s.get("slot_start", ""))
    previous_mode: int | None = None
    previous_loads: set[str] = set()
    events: list[dict] = []

    for slot in sorted_slots:
        start_dt = _parse_dt(slot.get("slot_start"))
        if start_dt is None:
            continue

        mode = int(slot.get("operating_mode") or 1)
        loads_set = set(_scheduled_load_names(slot))
        mode_changed = previous_mode is not None and mode != previous_mode
        loads_changed = previous_mode is not None and loads_set != previous_loads

        if mode_changed or loads_changed:
            local_dt = start_dt.astimezone(local_tz)
            parts: list[str] = []
            if mode_changed:
                parts.append(f"Mode -> {_mode_name(mode)}")
            if loads_changed:
                added = sorted(loads_set - previous_loads)
                removed = sorted(previous_loads - loads_set)
                if added:
                    parts.append("Devices ON -> " + ", ".join(added))
                if removed:
                    parts.append("Devices OFF -> " + ", ".join(removed))

            events.append(
                {
                    "time_utc": start_dt,
                    "time_label": local_dt.strftime("%a %H:%M"),
                    "summary": " | ".join(parts) if parts else "Plan update",
                }
            )

        previous_mode = mode
        previous_loads = loads_set

    return events


def _prev_next_significant_plan_events(plan_slots: list[dict], now_utc: datetime, local_tz) -> tuple[dict | None, dict | None]:
    """Return previous and next transition events around now."""
    events = _build_plan_transition_events(plan_slots, local_tz)
    prev_event: dict | None = None
    next_event: dict | None = None
    for event in events:
        event_time = event["time_utc"]
        if event_time <= now_utc:
            prev_event = event
        elif next_event is None:
            next_event = event
    return prev_event, next_event


def _tercile_bands(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    lo = min(values)
    hi = max(values)
    if hi <= lo:
        return {"low": lo, "high": hi}
    step = (hi - lo) / 3.0
    return {"low": lo + step, "high": lo + (2.0 * step)}


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request) -> HTMLResponse:
    """Render the main dashboard page."""
    templates = request.app.state.templates
    repo = request.app.state.repo
    config = request.app.state.config

    # Prefer live telemetry from control loop (matches diagnostics)
    # over DB rows which may be stale or pre-sign-fix
    control_loop = getattr(request.app.state, "control_loop", None)
    live_telem = control_loop.state.last_telemetry if control_loop else None
    telemetry = live_telem if live_telem is not None else await repo.get_latest_telemetry()
    active_plan = await repo.get_active_plan()
    # Use in-memory accounting engine first (authoritative live values),
    # fall back to DB billing row only if engine is unavailable.
    billing_cycle = None
    accounting_engine = getattr(request.app.state, "accounting", None)
    if accounting_engine:
        summary = accounting_engine.get_summary()
        billing_cycle = summary.cycle
    if billing_cycle is None:
        billing_cycle = await repo.get_active_billing_cycle()
    active_spike = await repo.get_active_spike()
    current_import_price_cents = await repo.get_latest_historical_value("import_price_cents")
    current_export_price_cents = await repo.get_latest_historical_value("export_price_cents")

    # Weather data from aggregator
    weather = None
    weather_forecast_days: list[dict] = []
    storm_active = False
    aggregator = getattr(request.app.state, "aggregator", None)
    if aggregator:
        agg_state = aggregator.state
        if agg_state.has_weather and agg_state.weather and agg_state.weather.slots:
            weather = agg_state.weather.slots[0]
            local_tz = resolve_timezone(getattr(config.load_profile, "timezone", "UTC"))
            now_local = datetime.now(timezone.utc).astimezone(local_tz)
            today = now_local.date()
            tomorrow = (now_local + timedelta(days=1)).date()
            buckets: dict[str, list] = {"today": [], "tomorrow": []}
            for slot in agg_state.weather.slots:
                slot_day = slot.time.astimezone(local_tz).date()
                if slot_day == today:
                    buckets["today"].append(slot)
                elif slot_day == tomorrow:
                    buckets["tomorrow"].append(slot)
            for key, label in (("today", "Today"), ("tomorrow", "Tomorrow")):
                day_slots = buckets[key]
                if not day_slots:
                    continue
                temps = [s.temperature_c for s in day_slots]
                clouds = [s.cloud_cover_pct for s in day_slots]
                precips = [s.precipitation_mm for s in day_slots]
                winds = [s.wind_speed_ms for s in day_slots]
                cloud_avg = sum(clouds) / max(len(clouds), 1)
                precip_sum = sum(precips)
                wind_max = max(winds) if winds else 0.0
                weather_forecast_days.append(
                    {
                        "label": label,
                        "icon": _weather_icon(cloud_avg, precip_sum, wind_max),
                        "temp_max_c": round(max(temps), 1),
                        "temp_min_c": round(min(temps), 1),
                        "cloud_avg_pct": round(cloud_avg, 0),
                        "precip_mm": round(precip_sum, 1),
                        "wind_max_ms": round(wind_max, 1),
                    }
                )
        storm_threshold = getattr(config, "storm", None)
        prob_threshold = getattr(storm_threshold, "probability_threshold", 0.5) if storm_threshold else 0.5
        storm_active = agg_state.storm_probability >= prob_threshold

    # Upcoming prices for next 3 hours (6 x 30-min slots), from plan slots
    # with tariff forecast fallback.
    local_tz = resolve_timezone(getattr(config.load_profile, "timezone", "UTC"))
    now_utc = datetime.now(timezone.utc)
    horizon_utc = now_utc + timedelta(hours=3)
    upcoming_prices: list[dict] = []
    upcoming_spike: dict | None = None
    spike_threshold = getattr(config.arbitrage, "spike_threshold_cents", 100)

    # Primary source: persisted plan slots.
    plan_slots: list[dict] = []
    if active_plan and isinstance(active_plan, dict):
        try:
            plan_slots = await repo.get_plan_slots(active_plan["id"])
        except Exception:
            plan_slots = []

    for slot in plan_slots:
        dt = _parse_dt(slot.get("slot_start"))
        if dt is None:
            continue
        if dt < now_utc or dt > horizon_utc:
            continue
        import_cents = float(slot.get("import_rate_cents") or 0.0)
        export_cents = float(slot.get("export_rate_cents") or 0.0)
        local_dt = dt.astimezone(local_tz)
        upcoming_prices.append(
            {
                "slot_start_ts": dt.timestamp(),
                "time_label": local_dt.strftime("%H:%M"),
                "import_cents": round(import_cents, 1),
                "export_cents": round(export_cents, 1),
            }
        )
        if upcoming_spike is None and import_cents >= spike_threshold:
            upcoming_spike = {
                "time_label": local_dt.strftime("%H:%M"),
                "import_cents": round(import_cents, 1),
            }

    # Fallback: tariff forecast directly from aggregator when plan not available yet.
    if not upcoming_prices and aggregator:
        tariff = getattr(aggregator.state, "tariff", None)
        if tariff and getattr(tariff, "slots", None):
            for slot in tariff.slots:
                dt = slot.start
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
                if dt < now_utc or dt > horizon_utc:
                    continue
                import_cents = float(getattr(slot, "import_price_cents", 0.0) or 0.0)
                export_cents = float(getattr(slot, "export_price_cents", 0.0) or 0.0)
                local_dt = dt.astimezone(local_tz)
                upcoming_prices.append(
                    {
                        "slot_start_ts": dt.timestamp(),
                        "time_label": local_dt.strftime("%H:%M"),
                        "import_cents": round(import_cents, 1),
                        "export_cents": round(export_cents, 1),
                    }
                )
                if upcoming_spike is None and import_cents >= spike_threshold:
                    upcoming_spike = {
                        "time_label": local_dt.strftime("%H:%M"),
                        "import_cents": round(import_cents, 1),
                    }

    if upcoming_prices:
        upcoming_prices.sort(key=lambda s: s["slot_start_ts"])
        upcoming_prices = [
            {
                "time_label": s["time_label"],
                "import_cents": s["import_cents"],
                "export_cents": s["export_cents"],
            }
            for s in upcoming_prices[:6]
        ]

    # Buy/sell price color bands from today's forecast slots (local day).
    buy_price_bands: dict[str, float] | None = None
    sell_price_bands: dict[str, float] | None = None
    today_import_forecast: list[float] = []
    today_export_forecast: list[float] = []
    for slot in plan_slots:
        dt = _parse_dt(slot.get("slot_start"))
        if dt is None:
            continue
        local_dt = dt.astimezone(local_tz)
        if local_dt.date() != now_utc.astimezone(local_tz).date():
            continue
        try:
            today_import_forecast.append(float(slot.get("import_rate_cents") or 0.0))
            today_export_forecast.append(float(slot.get("export_rate_cents") or 0.0))
        except Exception:
            continue

    if not today_export_forecast and aggregator:
        tariff = getattr(aggregator.state, "tariff", None)
        if tariff and getattr(tariff, "slots", None):
            for slot in tariff.slots:
                dt = slot.start
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                local_dt = dt.astimezone(local_tz)
                if local_dt.date() != now_utc.astimezone(local_tz).date():
                    continue
                try:
                    today_import_forecast.append(float(getattr(slot, "import_price_cents", 0.0) or 0.0))
                    today_export_forecast.append(float(getattr(slot, "export_price_cents", 0.0) or 0.0))
                except Exception:
                    continue

    buy_price_bands = _tercile_bands(today_import_forecast)
    sell_price_bands = _tercile_bands(today_export_forecast)

    # Device list from config
    devices = []
    loads_cfg = getattr(config, "loads", None)
    if loads_cfg:
        for dev in getattr(loads_cfg, "shelly_devices", []):
            devices.append({"name": dev.name, "power_w": dev.power_w, "enabled": dev.enabled, "type": "shelly"})
        for dev in getattr(loads_cfg, "mqtt_load_endpoints", []):
            devices.append({"name": dev.name, "power_w": dev.power_w, "enabled": dev.enabled, "type": "mqtt"})

    # Live load state from manager (best effort, avoid blocking UI on slow devices).
    load_manager = getattr(request.app.state, "load_manager", None)
    status_by_name: dict[str, dict] = {}
    if load_manager:
        controllers = list(load_manager.controllers.values())

        async def _safe_status(controller):
            try:
                return await asyncio.wait_for(controller.get_status(), timeout=2.5)
            except Exception:
                return None

        if controllers:
            statuses = await asyncio.gather(*[_safe_status(c) for c in controllers], return_exceptions=False)
            for st in statuses:
                if st is None:
                    continue
                status_by_name[st.name] = {
                    "state": st.state.value.upper(),
                    "power_w": st.power_w,
                    "available": st.is_available,
                }

    # Next planned run time by device name from future/current plan slots.
    next_run_by_name: dict[str, str] = {}
    if plan_slots:
        now_local = now_utc.astimezone(local_tz)
        for slot in sorted(plan_slots, key=lambda s: s.get("slot_start", "")):
            start_dt = _parse_dt(slot.get("slot_start"))
            end_dt = _parse_dt(slot.get("slot_end"))
            if start_dt is None or end_dt is None:
                continue
            if end_dt < now_utc:
                continue

            slot_local = start_dt.astimezone(local_tz)
            if start_dt <= now_utc < end_dt:
                label = "Now"
            elif slot_local.date() == now_local.date():
                label = slot_local.strftime("%H:%M")
            else:
                label = slot_local.strftime("%a %H:%M")

            for load_name in _scheduled_load_names(slot):
                if load_name and load_name not in next_run_by_name:
                    next_run_by_name[load_name] = label

    # Runtime tracking data from load manager
    runtime_by_id: dict[str, float] = {}
    if load_manager:
        runtime_by_id = load_manager.get_all_runtime_minutes()

    for d in devices:
        st = status_by_name.get(d["name"])
        if st:
            d["current_state"] = st["state"]
            d["current_power_w"] = st["power_w"]
            d["is_available"] = st["available"]
        else:
            d["current_state"] = "UNKNOWN"
            d["current_power_w"] = 0
            d["is_available"] = False
        d["next_run"] = next_run_by_name.get(d["name"], "Not scheduled")
        # Attach runtime info
        load_id = f"shelly_{d['name']}" if d["type"] == "shelly" else f"mqtt_{d['name']}"
        d["runtime_min"] = round(runtime_by_id.get(load_id, 0.0), 0)

    # Previous/next significant events in active plan: mode/device changes
    # at slot boundaries.
    prev_plan_event, next_plan_event = _prev_next_significant_plan_events(plan_slots, now_utc, local_tz)

    return templates.TemplateResponse(
        "overview.html",
        {
            "request": request,
            "telemetry": telemetry,
            "plan": active_plan,
            "billing_cycle": billing_cycle,
            "spike": active_spike,
            "current_import_price_cents": current_import_price_cents,
            "current_export_price_cents": current_export_price_cents,
            "config": config,
            "weather": weather,
            "weather_forecast_days": weather_forecast_days,
            "upcoming_prices": upcoming_prices,
            "upcoming_spike": upcoming_spike,
            "buy_price_bands": buy_price_bands,
            "sell_price_bands": sell_price_bands,
            "storm_active": storm_active,
            "devices": devices,
            "prev_plan_event": prev_plan_event,
            "next_plan_event": next_plan_event,
        },
    )

