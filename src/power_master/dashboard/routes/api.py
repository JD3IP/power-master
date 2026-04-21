"""REST API endpoints returning JSON data."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from power_master.dashboard.auth import require_admin

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Request models ───────────────────────────────────

class ModeRequest(BaseModel):
    mode: int
    power_w: int = 0
    timeout_s: float | None = None

    @field_validator('mode')
    @classmethod
    def validate_mode(cls, v: int) -> int:
        if v < 0 or v > 4:
            raise ValueError('mode must be 0-4')
        return v

    @field_validator('power_w')
    @classmethod
    def validate_power(cls, v: int) -> int:
        if v < -10000 or v > 10000:
            raise ValueError('power_w must be between -10000 and 10000 watts')
        return v

    @field_validator('timeout_s')
    @classmethod
    def validate_timeout(cls, v: float | None) -> float | None:
        if v is not None and (v < 0 or v > 86400):
            raise ValueError('timeout_s must be between 0 and 86400 seconds (24 hours)')
        return v


class ResetWacbRequest(BaseModel):
    wacb_cents: float = Field(..., gt=0, description="Weighted average cost basis in cents/kWh")


class LoadOverrideRequest(BaseModel):
    state: str = Field(..., pattern='^(on|off)$', description="Load state: 'on' or 'off'")
    timeout_s: float = Field(3600, ge=0, le=86400, description="Override duration in seconds")

    @field_validator('state')
    @classmethod
    def validate_state(cls, v: str) -> str:
        if v.lower() not in ('on', 'off'):
            raise ValueError("state must be 'on' or 'off'")
        return v.lower()


class ShellyLoadRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    ip_address: str = Field(...)
    device_type: str = Field(..., pattern='^(plug|plus|pro)$')


class MqttLoadRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    topic: str = Field(..., min_length=1)
    power_w: int = Field(gt=0)
    on_payload: str = Field(default="ON")
    off_payload: str = Field(default="OFF")


# ── Status ───────────────────────────────────────────

@router.get("/status")
async def system_status(request: Request) -> dict:
    """Get current system status."""
    repo = request.app.state.repo

    telemetry = await repo.get_latest_telemetry()
    plan = await repo.get_active_plan()
    billing = await repo.get_active_billing_cycle()
    spike = await repo.get_active_spike()

    return {
        "status": "running",
        "telemetry": telemetry,
        "active_plan": {
            "version": plan["version"] if plan else None,
            "trigger": plan["trigger_reason"] if plan else None,
            "objective": plan["objective_score"] if plan else None,
        } if plan else None,
        "billing_cycle": {
            "net_cost_cents": billing["net_cost_cents"] if billing else 0,
            "import_cost_cents": billing["total_import_cost_cents"] if billing else 0,
            "export_revenue_cents": billing["total_export_revenue_cents"] if billing else 0,
        } if billing else None,
        "spike_active": spike is not None,
    }


# ── System Health ────────────────────────────────────

@router.get("/health")
async def system_health(request: Request) -> dict:
    """Get comprehensive system health status and metrics."""
    from power_master import __version__

    repo = request.app.state.repo
    control_loop = getattr(request.app.state, "control_loop", None)

    # Inverter connectivity
    adapter = getattr(request.app.state, "adapter", None)
    inverter_online = False
    if adapter:
        try:
            inverter_online = await adapter.is_connected()
        except Exception:
            pass

    # Last telemetry age
    last_telemetry_age_seconds = None
    telemetry = control_loop.state.last_telemetry if control_loop else None
    if telemetry and telemetry.timestamp:
        age = (datetime.now(timezone.utc) - telemetry.timestamp).total_seconds()
        last_telemetry_age_seconds = max(0, int(age))

    # Plan health
    plan_age_seconds = None
    plan_slots_remaining = 0
    if control_loop and control_loop.state.current_plan:
        plan = control_loop.state.current_plan
        plan_age = (datetime.now(timezone.utc) - plan.created_at).total_seconds()
        plan_age_seconds = max(0, int(plan_age))
        plan_slots_remaining = len([s for s in plan.slots if s.start > datetime.now(timezone.utc)])

    # Forecast data — query each configured provider individually
    forecasts: dict[str, Any] = {}
    try:
        config = request.app.state.config
        provider_types = [p for p in (
            "solcast", "open_meteo", "bom", "forecast_solar",
        ) if getattr(getattr(config.providers, p, None), "enabled", False)]
        for provider_type in provider_types:
            fc = await repo.get_latest_forecast(provider_type)
            if fc:
                forecasts[provider_type] = {
                    "fetched_at": fc.get("fetched_at"),
                    "status": "ok",
                }
            else:
                forecasts[provider_type] = {"fetched_at": None, "status": "no data"}
    except Exception:
        pass

    # Database size
    db_size_bytes = 0
    try:
        import os
        db_path = request.app.state.config.db.path
        if db_path != ":memory:" and os.path.exists(db_path):
            db_size_bytes = os.path.getsize(db_path)
    except Exception:
        pass

    return {
        "inverter_online": inverter_online,
        "last_telemetry_age_seconds": last_telemetry_age_seconds,
        "forecasts": forecasts,
        "plan_age_seconds": plan_age_seconds,
        "plan_slots_remaining": plan_slots_remaining,
        "db_size_bytes": db_size_bytes,
        "app_version": str(__version__),
    }


# ── Mode Control ─────────────────────────────────────

@router.get("/mode")
async def get_mode(request: Request) -> dict:
    """Get current operating mode and override status."""
    from power_master.hardware.base import OperatingMode

    control_loop = getattr(request.app.state, "control_loop", None)
    manual_override = getattr(request.app.state, "manual_override", None)

    if control_loop is None:
        return {"current_mode": 1, "mode_name": "SELF_USE", "override_active": False,
                "override_remaining_s": 0, "source": "default",
                "optimiser_mode": None, "optimiser_mode_name": None,
                "user_mode": None, "user_mode_name": None,
                "auto_active": True}

    state = control_loop.state
    override_active = manual_override.is_active if manual_override else False

    if override_active:
        source = "manual"
    elif state.current_plan:
        source = "plan"
    else:
        source = "default"

    # Get optimiser recommended mode from plan
    optimiser_mode = None
    optimiser_mode_name = None
    if state.current_plan:
        slot = state.current_plan.get_current_slot()
        if slot:
            try:
                opt_mode = OperatingMode(int(slot.mode))
                optimiser_mode = opt_mode.value
                optimiser_mode_name = opt_mode.name
            except (ValueError, TypeError):
                pass

    # Get user manual mode if active
    user_mode = None
    user_mode_name = None
    if override_active and manual_override:
        cmd = manual_override.get_command()
        if cmd:
            user_mode = cmd.mode.value
            user_mode_name = cmd.mode.name

    # Never expose AUTO to the UI — map to SELF_USE
    display_mode = state.current_mode
    if display_mode == OperatingMode.AUTO:
        display_mode = OperatingMode.SELF_USE

    config = getattr(request.app.state, "config", None)
    optimiser_enabled = config.planning.optimiser_enabled if config else True

    return {
        "current_mode": display_mode.value,
        "mode_name": display_mode.name,
        "override_active": override_active,
        "override_remaining_s": manual_override.remaining_seconds if manual_override else 0,
        "source": source,
        "optimiser_mode": optimiser_mode,
        "optimiser_mode_name": optimiser_mode_name,
        "user_mode": user_mode,
        "user_mode_name": user_mode_name,
        "auto_active": not override_active,
        "optimiser_enabled": optimiser_enabled,
    }


@router.post("/mode")
async def set_mode(request: Request, body: ModeRequest) -> dict:
    """Set manual mode override."""
    denied = require_admin(request)
    if denied:
        return denied

    from power_master.hardware.base import OperatingMode

    manual_override = getattr(request.app.state, "manual_override", None)
    if manual_override is None:
        return {"status": "error", "message": "Manual override not available"}

    try:
        mode = OperatingMode(body.mode)
    except ValueError:
        return {"status": "error", "message": f"Invalid mode: {body.mode}"}

    power_w = body.power_w

    # Apply sensible default power for force charge/discharge when none specified
    if power_w == 0 and mode in (OperatingMode.FORCE_CHARGE, OperatingMode.FORCE_DISCHARGE):
        config = request.app.state.config
        if mode == OperatingMode.FORCE_CHARGE:
            power_w = config.battery.max_charge_rate_w
        else:
            power_w = config.battery.max_discharge_rate_w

    manual_override.set(
        mode=mode,
        power_w=power_w,
        timeout_seconds=body.timeout_s,
        source="dashboard",
    )

    # Immediately dispatch the command to the inverter instead of waiting
    # for the next control loop tick (up to 5 minutes away)
    control_loop = getattr(request.app.state, "control_loop", None)
    immediate_dispatched = False
    immediate_result = ""
    if control_loop is not None:
        try:
            cmd = await control_loop.tick_once(
                bypass_anti_oscillation=(mode == OperatingMode.AUTO)
            )
            immediate_dispatched = cmd is not None
            immediate_result = control_loop.state.last_command_result
        except Exception:
            logger.warning("Immediate tick after mode set failed", exc_info=True)
            immediate_result = "error: immediate tick failed"

    # Rapid post-command reads are scheduled in background so this API
    # returns quickly even if Modbus is slow/unresponsive.
    post_reads = 0
    post_read_error = ""
    application = getattr(request.app.state, "application", None)
    adapter = getattr(application, "_adapter", None) if application else None
    if adapter is not None and control_loop is not None:
        async def _post_refresh_reads() -> None:
            for i in range(3):
                try:
                    telemetry = await asyncio.wait_for(adapter.get_telemetry(), timeout=0.8)
                    control_loop.update_live_telemetry(telemetry)
                    if i < 2:
                        await asyncio.sleep(0.15)
                except Exception as e:
                    logger.debug("Post-command telemetry read %d failed: %s", i + 1, e)
                    break

        asyncio.create_task(_post_refresh_reads())
        post_read_error = "scheduled_background"
        post_reads = -1

    response = {
        "status": "ok",
        "mode": mode.value,
        "mode_name": mode.name,
        "power_w": power_w,
        "override_active": manual_override.is_active,
        "immediate_dispatched": immediate_dispatched,
        "immediate_result": immediate_result,
        "post_command_reads": post_reads,
        "post_command_read_error": post_read_error,
    }
    if control_loop is not None and not immediate_dispatched:
        response["status"] = "warning"
        response["message"] = (
            "Override stored, but no command was dispatched immediately. "
            "Check telemetry/read failures or anti-oscillation suppression."
        )
    return response


# ── Telemetry ────────────────────────────────────────

@router.get("/telemetry/latest")
async def latest_telemetry(request: Request) -> dict:
    """Get the latest telemetry reading."""
    control_loop = getattr(request.app.state, "control_loop", None)
    if control_loop and control_loop.state.last_telemetry is not None:
        t = control_loop.state.last_telemetry
        last_tick = control_loop.state.last_tick_at
        stale = True
        if last_tick is not None:
            interval = getattr(request.app.state.config.planning, "evaluation_interval_seconds", 300)
            stale = (datetime.now(timezone.utc) - last_tick).total_seconds() > max(interval * 2, 20)
        if not stale:
            return {
                "soc": t.soc,
                "battery_power_w": t.battery_power_w,
                "solar_power_w": t.solar_power_w,
                "grid_power_w": t.grid_power_w,
                "load_power_w": t.load_power_w,
                "battery_voltage": t.battery_voltage,
                "battery_temp_c": t.battery_temp_c,
                "inverter_mode": t.inverter_mode,
                "grid_available": t.grid_available,
                "raw_data": t.raw_data,
            }

    # Do not perform direct Modbus reads in request path; control-loop runs in background.
    repo = request.app.state.repo
    telemetry = await repo.get_latest_telemetry()
    return telemetry or {}


@router.get("/telemetry/history")
async def telemetry_history(request: Request, hours: int = 24) -> list:
    """Get telemetry time series for the last N hours."""
    repo = request.app.state.repo
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return await repo.get_telemetry_since(cutoff)


# ── Plans ────────────────────────────────────────────

@router.get("/plan/active")
async def active_plan(request: Request) -> dict:
    """Get the active optimisation plan."""
    repo = request.app.state.repo
    plan = await repo.get_active_plan()
    if not plan:
        return {"plan": None, "slots": []}
    slots = await repo.get_plan_slots(plan["id"])
    return {"plan": plan, "slots": slots}


@router.get("/plan/slots")
async def plan_slots(request: Request) -> list:
    """Get current plan slot data."""
    repo = request.app.state.repo
    plan = await repo.get_active_plan()
    if not plan:
        return []
    return await repo.get_plan_slots(plan["id"])


@router.get("/plan/history")
async def plan_history(request: Request) -> list:
    """Get plan version history."""
    repo = request.app.state.repo
    return await repo.get_plan_history()


# ── Geocoding ────────────────────────────────────────

@router.get("/geocode")
async def geocode_address(request: Request, q: str = "") -> list:
    """Search for an address and return coordinates using OpenStreetMap Nominatim."""
    if not q or len(q) < 3:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": q, "format": "json", "limit": 5, "addressdetails": 1},
                headers={"User-Agent": "PowerMaster/1.0"},
            )
            resp.raise_for_status()
            results = resp.json()
        return [
            {
                "display_name": r.get("display_name", ""),
                "lat": float(r["lat"]),
                "lon": float(r["lon"]),
            }
            for r in results
            if "lat" in r and "lon" in r
        ]
    except Exception as e:
        logger.warning("Geocode lookup failed: %s", e)
        return []


# ── Tariff ───────────────────────────────────────────

@router.get("/tariff/history")
async def tariff_history(request: Request, hours: int = 24) -> list:
    """Get tariff price history for the last N hours."""
    repo = request.app.state.repo
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return await repo.get_tariff_since(cutoff)


@router.get("/prices/history")
async def prices_history(request: Request, hours: int = 12) -> list:
    """Get individual price data points from historical_data for charting."""
    repo = request.app.state.repo
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=hours)).isoformat()
    end = now.isoformat()

    import_records = await repo.get_historical("import_price_cents", start, end)
    export_records = await repo.get_historical("export_price_cents", start, end)
    export_by_time = {r["recorded_at"]: r["value"] for r in export_records}

    return [
        {
            "recorded_at": r["recorded_at"],
            "import_price_cents": r["value"],
            "export_price_cents": export_by_time.get(r["recorded_at"], 0.0),
        }
        for r in import_records
    ]


# ── Accounting / Billing ─────────────────────────────

@router.get("/billing/current")
async def billing_current(request: Request) -> dict:
    """Get current billing cycle summary from in-memory accounting engine."""
    accounting_engine = getattr(request.app.state, "accounting", None)
    if not accounting_engine:
        return {}
    summary = accounting_engine.get_summary()
    if not summary.cycle:
        return {}
    c = summary.cycle
    return {
        "cycle_start": c.cycle_start.isoformat(),
        "cycle_end": c.cycle_end.isoformat(),
        "days_elapsed": c.days_elapsed,
        "days_remaining": c.days_remaining,
        "total_import_cost_cents": c.total_import_cost_cents,
        "total_export_revenue_cents": c.total_export_revenue_cents,
        "total_self_consumption_value_cents": c.total_self_consumption_value_cents,
        "total_arbitrage_profit_cents": c.total_arbitrage_profit_cents,
        "total_fixed_costs_cents": c.total_fixed_costs_cents,
        "net_cost_cents": c.net_cost_cents,
    }


@router.get("/billing/history")
async def billing_history(request: Request) -> list:
    """Get past billing cycles."""
    repo = request.app.state.repo
    return await repo.get_billing_history()


@router.get("/billing/events")
async def billing_events(request: Request, days: int = 7) -> list:
    """Get recent accounting events."""
    repo = request.app.state.repo
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return await repo.get_accounting_events_since(cutoff)


@router.get("/accounting/daily")
async def accounting_daily(request: Request, days: int = 30) -> list:
    """Get daily cost/revenue breakdown."""
    repo = request.app.state.repo
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return await repo.get_daily_accounting(cutoff)


@router.get("/accounting/summary")
async def accounting_summary(request: Request) -> dict:
    """Get live accounting summary for dashboard aggregate pricing cards."""
    accounting_engine = getattr(request.app.state, "accounting", None)
    if not accounting_engine:
        return {}
    summary = accounting_engine.get_summary()
    data: dict = {
        "wacb_cents": round(summary.wacb_cents, 1),
        "stored_value_cents": round(summary.stored_value_cents, 1),
        "today_net_cost_cents": summary.today_net_cost_cents,
        "week_net_cost_cents": summary.week_net_cost_cents,
    }
    if summary.cycle:
        data["cycle"] = {
            "net_cost_cents": summary.cycle.net_cost_cents,
            "import_cost_cents": summary.cycle.total_import_cost_cents,
            "export_revenue_cents": summary.cycle.total_export_revenue_cents,
            "days_elapsed": summary.cycle.days_elapsed,
            "days_remaining": summary.cycle.days_remaining,
        }
    return data


@router.post("/accounting/reset-wacb")
async def reset_wacb(request: Request, body: ResetWacbRequest) -> dict:
    """Reset battery cost basis using c/kWh only; stored_wh derived from live SOC."""
    accounting_engine = getattr(request.app.state, "accounting", None)
    if not accounting_engine:
        return {"error": "Accounting engine not available"}
    new_wacb = body.wacb_cents

    # Derive stored_wh from current SOC and battery capacity
    config = request.app.state.config
    control_loop = getattr(request.app.state, "control_loop", None)
    capacity_wh = config.battery.capacity_wh
    soc = 0.5  # default fallback
    if control_loop and control_loop.state and control_loop.state.last_telemetry:
        soc = control_loop.state.last_telemetry.soc / 100.0
    stored_wh = soc * capacity_wh

    tracker = accounting_engine.cost_basis
    tracker._state.wacb_cents = new_wacb
    tracker._state.stored_wh = stored_wh
    tracker._notify_change()
    return {
        "wacb_cents": round(tracker.wacb_cents, 1),
        "stored_value_cents": round(tracker.stored_value_cents, 1),
    }


# ── Loads ────────────────────────────────────────────

@router.get("/loads")
async def list_loads(request: Request) -> dict:
    """List all configured loads."""
    config = request.app.state.config
    return {
        "shelly_devices": [d.model_dump() for d in config.loads.shelly_devices],
        "mqtt_load_endpoints": [e.model_dump() for e in config.loads.mqtt_load_endpoints],
    }


@router.post("/loads/shelly")
async def add_shelly_load(request: Request) -> dict:
    """Add a new Shelly load device."""
    denied = require_admin(request)
    if denied:
        return denied

    from power_master.config.schema import ShellyDeviceConfig

    body = await request.json()
    try:
        device = ShellyDeviceConfig(**body)
    except Exception as e:
        return {"status": "error", "message": str(e)}

    application = getattr(request.app.state, "application", None)
    if application is None:
        return {"status": "error", "message": "Application not available"}

    config = request.app.state.config
    current_devices = [d.model_dump() for d in config.loads.shelly_devices]
    current_devices.append(device.model_dump())
    await application.reload_config(
        {"loads": {"shelly_devices": current_devices}}, request.app,
    )
    return {"status": "ok", "device": device.model_dump()}


@router.put("/loads/shelly/{name}")
async def update_shelly_load(request: Request, name: str) -> dict:
    """Update an existing Shelly load device by name."""
    denied = require_admin(request)
    if denied:
        return denied

    from power_master.config.schema import ShellyDeviceConfig

    application = getattr(request.app.state, "application", None)
    if application is None:
        return {"status": "error", "message": "Application not available"}

    config = request.app.state.config
    body = await request.json()

    # Find existing device
    existing = None
    for d in config.loads.shelly_devices:
        if d.name == name:
            existing = d
            break
    if existing is None:
        return {"status": "error", "message": f"Device '{name}' not found"}

    # Merge existing with updates
    merged = existing.model_dump()
    merged.update(body)
    try:
        updated = ShellyDeviceConfig(**merged)
    except Exception as e:
        return {"status": "error", "message": str(e)}

    devices = [
        updated.model_dump() if d.name == name else d.model_dump()
        for d in config.loads.shelly_devices
    ]
    await application.reload_config(
        {"loads": {"shelly_devices": devices}}, request.app,
    )
    return {"status": "ok", "device": updated.model_dump()}


@router.delete("/loads/shelly/{name}")
async def delete_shelly_load(request: Request, name: str) -> dict:
    """Delete a Shelly load device by name."""
    denied = require_admin(request)
    if denied:
        return denied

    application = getattr(request.app.state, "application", None)
    if application is None:
        return {"status": "error", "message": "Application not available"}

    config = request.app.state.config
    remaining = [d.model_dump() for d in config.loads.shelly_devices if d.name != name]
    await application.reload_config(
        {"loads": {"shelly_devices": remaining}}, request.app,
    )
    return {"status": "ok"}


@router.post("/loads/mqtt")
async def add_mqtt_load(request: Request) -> dict:
    """Add a new MQTT load endpoint."""
    denied = require_admin(request)
    if denied:
        return denied

    from power_master.config.schema import MQTTLoadEndpointConfig

    body = await request.json()
    try:
        endpoint = MQTTLoadEndpointConfig(**body)
    except Exception as e:
        return {"status": "error", "message": str(e)}

    application = getattr(request.app.state, "application", None)
    if application is None:
        return {"status": "error", "message": "Application not available"}

    config = request.app.state.config
    current_endpoints = [e.model_dump() for e in config.loads.mqtt_load_endpoints]
    current_endpoints.append(endpoint.model_dump())
    await application.reload_config(
        {"loads": {"mqtt_load_endpoints": current_endpoints}}, request.app,
    )
    return {"status": "ok", "endpoint": endpoint.model_dump()}


@router.put("/loads/mqtt/{name}")
async def update_mqtt_load(request: Request, name: str) -> dict:
    """Update an existing MQTT load endpoint by name."""
    denied = require_admin(request)
    if denied:
        return denied

    from power_master.config.schema import MQTTLoadEndpointConfig

    application = getattr(request.app.state, "application", None)
    if application is None:
        return {"status": "error", "message": "Application not available"}

    config = request.app.state.config
    body = await request.json()

    # Find existing endpoint
    existing = None
    for e in config.loads.mqtt_load_endpoints:
        if e.name == name:
            existing = e
            break
    if existing is None:
        return {"status": "error", "message": f"Endpoint '{name}' not found"}

    # Merge existing with updates
    merged = existing.model_dump()
    merged.update(body)
    try:
        updated = MQTTLoadEndpointConfig(**merged)
    except Exception as e:
        return {"status": "error", "message": str(e)}

    endpoints = [
        updated.model_dump() if e.name == name else e.model_dump()
        for e in config.loads.mqtt_load_endpoints
    ]
    await application.reload_config(
        {"loads": {"mqtt_load_endpoints": endpoints}}, request.app,
    )
    return {"status": "ok", "endpoint": updated.model_dump()}


@router.delete("/loads/mqtt/{name}")
async def delete_mqtt_load(request: Request, name: str) -> dict:
    """Delete an MQTT load endpoint by name."""
    denied = require_admin(request)
    if denied:
        return denied

    application = getattr(request.app.state, "application", None)
    if application is None:
        return {"status": "error", "message": "Application not available"}

    config = request.app.state.config
    remaining = [e.model_dump() for e in config.loads.mqtt_load_endpoints if e.name != name]
    await application.reload_config(
        {"loads": {"mqtt_load_endpoints": remaining}}, request.app,
    )
    return {"status": "ok"}


# ── Load Detail & Manual Override ────────────────────


def _find_load_id_by_name(config, name: str) -> str | None:
    """Find the load_id for a configured device by name."""
    for dev in getattr(config.loads, "shelly_devices", []):
        if dev.name == name:
            return f"shelly_{dev.name}"
    for dev in getattr(config.loads, "mqtt_load_endpoints", []):
        if dev.name == name:
            return f"mqtt_{dev.name}"
    return None


@router.get("/loads/{name}/detail")
async def load_detail(request: Request, name: str) -> dict:
    """Get detail for a specific load: event history, planned events, and runtime."""
    config = request.app.state.config
    repo = request.app.state.repo
    load_manager = getattr(request.app.state, "load_manager", None)

    load_id = _find_load_id_by_name(config, name)
    if load_id is None:
        return {"status": "error", "message": f"Load '{name}' not found"}

    # Event history from in-memory command history
    event_history: list[dict] = []
    if load_manager:
        for cmd in load_manager.get_command_history_for_load(load_id, limit=20):
            event_history.append({
                "action": cmd.action,
                "reason": cmd.reason,
                "issued_at": cmd.issued_at.isoformat(),
                "success": cmd.success,
            })

    # Planned events for next 48h from active plan slots
    planned_events: list[dict] = []
    active_plan = await repo.get_active_plan()
    if active_plan:
        try:
            plan_slots = await repo.get_plan_slots(active_plan["id"])
        except Exception:
            plan_slots = []

        now_utc = datetime.now(timezone.utc)
        horizon_utc = now_utc + timedelta(hours=48)
        from power_master.timezone_utils import resolve_timezone
        local_tz = resolve_timezone(getattr(config.load_profile, "timezone", "UTC"))

        import json as _json
        prev_scheduled = False
        for slot in sorted(plan_slots, key=lambda s: s.get("slot_start", "")):
            raw = slot.get("scheduled_loads_json")
            scheduled_names: list[str] = []
            if raw:
                try:
                    parsed = _json.loads(raw)
                    if isinstance(parsed, list):
                        scheduled_names = [str(x) for x in parsed]
                except Exception:
                    pass
            is_scheduled = name in scheduled_names

            start_dt_str = slot.get("slot_start")
            end_dt_str = slot.get("slot_end")
            if not start_dt_str:
                continue
            try:
                start_dt = datetime.fromisoformat(start_dt_str.replace("Z", "+00:00"))
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            if start_dt > horizon_utc:
                break
            if start_dt < now_utc and not is_scheduled:
                prev_scheduled = False
                continue

            if is_scheduled != prev_scheduled:
                if start_dt >= now_utc:
                    local_dt = start_dt.astimezone(local_tz)
                    planned_events.append({
                        "slot_start": start_dt.isoformat(),
                        "time_label": local_dt.strftime("%a %H:%M"),
                        "scheduled": is_scheduled,
                        "import_rate_cents": slot.get("import_rate_cents"),
                    })
            prev_scheduled = is_scheduled

    # Daily runtime — today from load_manager, historical from historical_data
    today_runtime_min = 0.0
    if load_manager:
        today_runtime_min = load_manager.get_runtime_minutes(load_id)

    now_utc = datetime.now(timezone.utc)
    runtime_history: list[dict] = []
    start_7d = (now_utc - timedelta(days=7)).isoformat()
    end_now = now_utc.isoformat()
    try:
        data_type = f"load_runtime_minutes_{load_id}"
        rows = await repo.get_historical(data_type, start_7d, end_now, resolution="1day")
        for row in rows:
            runtime_history.append({
                "date": row["recorded_at"][:10],
                "runtime_minutes": row["value"],
            })
    except Exception:
        pass

    # Override status
    override_info: dict | None = None
    if load_manager:
        override = load_manager.get_load_override(load_id)
        if override:
            override_info = {
                "state": override.state,
                "remaining_seconds": override.remaining_seconds,
                "source": override.source,
            }

    return {
        "load_id": load_id,
        "name": name,
        "today_runtime_min": round(today_runtime_min, 1),
        "runtime_history": runtime_history,
        "event_history": list(reversed(event_history)),
        "planned_events": planned_events,
        "override": override_info,
    }


@router.post("/loads/{name}/override")
async def set_load_override(request: Request, name: str, body: LoadOverrideRequest) -> dict:
    """Manually set a load ON or OFF, bypassing the optimiser plan for up to 60 minutes."""
    denied = require_admin(request)
    if denied:
        return denied

    if body.state not in ("on", "off"):
        return {"status": "error", "message": "state must be 'on' or 'off'"}

    config = request.app.state.config
    load_manager = getattr(request.app.state, "load_manager", None)
    if load_manager is None:
        return {"status": "error", "message": "Load manager not available"}

    load_id = _find_load_id_by_name(config, name)
    if load_id is None:
        return {"status": "error", "message": f"Load '{name}' not found"}

    timeout_s = min(body.timeout_s, 3600)  # cap at 60 minutes
    success = await load_manager.set_load_override(
        load_id=load_id,
        state=body.state,
        timeout_seconds=timeout_s,
        source="dashboard",
    )

    return {
        "status": "ok",
        "load_id": load_id,
        "name": name,
        "state": body.state,
        "timeout_s": timeout_s,
        "success": success,
    }


@router.delete("/loads/{name}/override")
async def clear_load_override(request: Request, name: str) -> dict:
    """Clear a manual load override, returning control to the optimiser."""
    denied = require_admin(request)
    if denied:
        return denied

    config = request.app.state.config
    load_manager = getattr(request.app.state, "load_manager", None)
    if load_manager is None:
        return {"status": "error", "message": "Load manager not available"}

    load_id = _find_load_id_by_name(config, name)
    if load_id is None:
        return {"status": "error", "message": f"Load '{name}' not found"}

    load_manager.clear_load_override(load_id)
    return {"status": "ok", "load_id": load_id, "name": name}


def _foxess_config_info(config):
    """Return connection config for diagnostics, adapting to TCP vs RTU."""
    foxess = config.hardware.foxess
    info: dict = {"connection_type": foxess.connection_type, "unit_id": foxess.unit_id}
    if foxess.connection_type == "rtu":
        info["serial_port"] = foxess.serial_port
        info["baudrate"] = foxess.baudrate
    else:
        info["host"] = foxess.host
        info["port"] = foxess.port
    return info


# ── Inverter Diagnostics ────────────────────────────

@router.get("/inverter/diagnostics")
async def inverter_diagnostics(request: Request) -> dict:
    """Read raw Modbus registers from the inverter for diagnostics.

    Returns connection status, raw register values, and interpreted values
    with a timestamp so the user can confirm what's working and what isn't.
    """
    application = getattr(request.app.state, "application", None)
    adapter = getattr(application, "_adapter", None) if application else None

    if adapter is None:
        return {
            "connected": False,
            "error": "Inverter adapter not available",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # Check connection
    try:
        connected = await adapter.is_connected()
    except Exception:
        connected = False

    if not connected:
        diag: dict = {
            "connected": False,
            "error": "Not connected to inverter",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "config": _foxess_config_info(request.app.state.config),
        }
        # Add serial port diagnostics for RTU mode
        foxess = request.app.state.config.hardware.foxess
        if foxess.connection_type == "rtu":
            import os
            port = foxess.serial_port
            diag["serial_diagnostics"] = {
                "port": port,
                "port_exists": os.path.exists(port),
                "port_readable": os.access(port, os.R_OK) if os.path.exists(port) else False,
                "port_writable": os.access(port, os.W_OK) if os.path.exists(port) else False,
                "hint": (
                    f"Port {port} does not exist — check USB adapter is plugged in"
                    if not os.path.exists(port)
                    else f"No write permission on {port} — run 'sudo usermod -a -G dialout $USER'"
                    if not os.access(port, os.W_OK)
                    else "Port accessible but connection failed — check baud rate and wiring"
                ),
            }
        return diag

    # Read all registers individually so we can report per-register status
    from power_master.hardware.adapters.foxess import Registers, KH_WORK_MODES

    registers: dict = {}

    async def read_input_i16(name: str, address: int, unit: str = "W", gain: int = 1):
        try:
            raw = await adapter._read_input_int16(address)
            value = raw / gain if gain > 1 else raw
            registers[name] = {
                "address": address, "raw": raw, "value": value,
                "unit": unit, "type": "input_i16", "status": "ok",
            }
        except Exception as e:
            registers[name] = {
                "address": address, "raw": None, "value": None,
                "unit": unit, "type": "input_i16", "status": "error",
                "error": str(e),
            }

    async def read_input_u16(name: str, address: int, unit: str = "", gain: int = 1):
        try:
            raw = await adapter._read_input_uint16(address)
            value = raw / gain if gain > 1 else raw
            registers[name] = {
                "address": address, "raw": raw, "value": value,
                "unit": unit, "type": "input_u16", "status": "ok",
            }
        except Exception as e:
            registers[name] = {
                "address": address, "raw": None, "value": None,
                "unit": unit, "type": "input_u16", "status": "error",
                "error": str(e),
            }

    async def read_holding_u16(name: str, address: int, unit: str = "", gain: int = 1):
        try:
            raw = await adapter._read_uint16(address)
            value = raw / gain if gain > 1 else raw
            registers[name] = {
                "address": address, "raw": raw, "value": value,
                "unit": unit, "type": "holding_u16", "status": "ok",
            }
        except Exception as e:
            registers[name] = {
                "address": address, "raw": None, "value": None,
                "unit": unit, "type": "holding_u16", "status": "error",
                "error": str(e),
            }

    def _s32_auto(hi: int, lo: int) -> int:
        v_be = (hi << 16) | lo
        v_le = (lo << 16) | hi
        if v_be & 0x80000000:
            v_be -= 0x100000000
        if v_le & 0x80000000:
            v_le -= 0x100000000
        return v_be if abs(v_be) <= abs(v_le) else v_le

    async def read_input_i32(name: str, lo_addr: int, hi_addr: int, unit: str = "W"):
        """Read a 32-bit I32 value from two input registers (FC4) and show all 4 raw words."""
        try:
            lo = await adapter._read_input_uint16(lo_addr)
            registers[f"{name} LO ({lo_addr})"] = {
                "address": lo_addr, "raw": lo, "value": lo,
                "unit": "raw", "type": "input_u16", "status": "ok",
            }
        except Exception as e:
            lo = None
            registers[f"{name} LO ({lo_addr})"] = {
                "address": lo_addr, "raw": None, "value": None,
                "unit": "raw", "type": "input_u16", "status": "error", "error": str(e),
            }
        try:
            hi = await adapter._read_input_uint16(hi_addr)
            registers[f"{name} HI ({hi_addr})"] = {
                "address": hi_addr, "raw": hi, "value": hi,
                "unit": "raw", "type": "input_u16", "status": "ok",
            }
        except Exception as e:
            hi = None
            registers[f"{name} HI ({hi_addr})"] = {
                "address": hi_addr, "raw": None, "value": None,
                "unit": "raw", "type": "input_u16", "status": "error", "error": str(e),
            }
        if lo is not None and hi is not None:
            value = _s32_auto(hi, lo)
            registers[name] = {
                "address": lo_addr, "raw": f"HI={hi:#06x} LO={lo:#06x}", "value": value,
                "unit": unit, "type": "input_i32", "status": "ok",
            }
        else:
            registers[name] = {
                "address": lo_addr, "raw": None, "value": None,
                "unit": unit, "type": "input_i32", "status": "error",
                "error": "one or more component registers failed",
            }

    # Read all KH input registers under the adapter lock
    try:
        async with adapter._lock:
            await read_input_i32("PV1 Power", Registers.PV1_POWER_LO, Registers.PV1_POWER_HI, "W")
            await read_input_i32("PV2 Power", Registers.PV2_POWER_LO, Registers.PV2_POWER_HI, "W")
            await read_input_i32("PV3 Power", Registers.PV3_POWER_LO, Registers.PV3_POWER_HI, "W")
            await read_input_i32("PV4 Power", Registers.PV4_POWER_LO, Registers.PV4_POWER_HI, "W")
            await read_input_i16("Grid / Meter", Registers.GRID_METER, "W")
            await read_input_i16("Load Power", Registers.LOAD_POWER, "W")
            await read_input_i16("Battery Power", Registers.BATTERY_POWER, "W")
            await read_input_u16("Battery SOC", Registers.BATTERY_SOC, "%")
            await read_input_i16("Battery Voltage", Registers.BATTERY_VOLTAGE, "V", gain=10)
            await read_input_i16("Battery Current", Registers.BATTERY_CURRENT, "A", gain=10)
            await read_input_i16("Battery Temp", Registers.BATTERY_TEMP, "C", gain=10)
            await read_input_u16("Inverter State", Registers.INVERTER_STATE)
            await read_holding_u16("Work Mode", Registers.WORK_MODE)
            await read_holding_u16("Max Charge Current", Registers.MAX_CHARGE_CURRENT, "A", gain=10)
            await read_holding_u16("Max Discharge Current", Registers.MAX_DISCHARGE_CURRENT, "A", gain=10)
            await read_holding_u16("Min SOC", Registers.MIN_SOC, "%")
            await read_holding_u16("Export Limit", Registers.EXPORT_LIMIT, "W")
            await read_holding_u16("Remote Enable", Registers.REMOTE_ENABLE)
            await read_holding_u16("Remote Timeout", Registers.REMOTE_TIMEOUT, "s")
            await read_holding_u16("Active Power Cmd", Registers.ACTIVE_POWER, "W")
    except Exception as e:
        return {
            "connected": True,
            "error": f"Register read failed: {e}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "registers": registers,
        }

    # Add human-readable work mode name
    work_mode_reg = registers.get("Work Mode", {})
    work_mode_val = work_mode_reg.get("raw")
    work_mode_name = KH_WORK_MODES.get(work_mode_val, f"Unknown({work_mode_val})") if work_mode_val is not None else "N/A"

    # Inverter state names
    inv_states = {0: "Self-Test", 1: "WaitState", 2: "CheckState", 3: "Normal",
                  4: "EpsState", 5: "FaultState", 6: "Permanent Fault", 8: "FlashState"}
    inv_state_reg = registers.get("Inverter State", {})
    inv_state_val = inv_state_reg.get("raw")
    inv_state_name = inv_states.get(inv_state_val, f"Unknown({inv_state_val})") if inv_state_val is not None else "N/A"

    return {
        "connected": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": _foxess_config_info(request.app.state.config),
        "work_mode_name": work_mode_name,
        "inverter_state_name": inv_state_name,
        "registers": registers,
    }


# ── Provider Diagnostics ─────────────────────────────

@router.get("/providers/status")
async def provider_status(request: Request) -> dict:
    """Get health and status of all data providers."""
    import time as _time

    resilience_mgr = getattr(request.app.state, "resilience_mgr", None)
    aggregator = getattr(request.app.state, "aggregator", None)
    now_mono = _time.monotonic()

    providers = {}
    provider_map = {
        "solar_forecast": {"label": "Solcast Solar", "update_attr": "last_solar_update"},
        "weather_forecast": {"label": "Open-Meteo Weather", "update_attr": "last_weather_update"},
        "tariff": {"label": "Amber Tariff", "update_attr": "last_tariff_update"},
        "storm": {"label": "BOM Storm", "update_attr": "last_storm_update"},
    }

    health_checker = resilience_mgr._health if resilience_mgr else None

    for key, meta in provider_map.items():
        entry: dict = {"label": meta["label"]}

        # Health data from HealthChecker
        if health_checker:
            h = health_checker.get_health(key)
            if h:
                entry["healthy"] = h.healthy
                entry["consecutive_failures"] = h.consecutive_failures
                entry["total_failures"] = h.total_failures
                entry["last_error"] = h.last_error or ""
                entry["seconds_since_success"] = round(now_mono - h.last_success, 1) if h.last_success > 0 else None
                entry["seconds_since_failure"] = round(now_mono - h.last_failure, 1) if h.last_failure > 0 else None
            else:
                entry["healthy"] = None
                entry["configured"] = False

        # Last update time from aggregator
        if aggregator:
            state = aggregator.state
            last_update = getattr(state, meta["update_attr"], None)
            if last_update:
                entry["last_update"] = last_update.isoformat()
                age_s = (datetime.now(timezone.utc) - last_update).total_seconds()
                entry["data_age_seconds"] = round(age_s, 1)
            else:
                entry["last_update"] = None
                entry["data_age_seconds"] = None

        providers[key] = entry

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "providers": providers,
    }


# ── Logs ─────────────────────────────────────────────

@router.get("/logs")
async def get_logs(request: Request, limit: int = 200, level: str = "") -> dict:
    """Get recent application log entries from the in-memory buffer."""
    from power_master.dashboard.log_buffer import log_buffer

    records = log_buffer.get_records(limit=min(limit, 1000), level=level or None)
    return {"records": records}


@router.get("/logs/export")
async def export_logs(request: Request, level: str = "") -> Response:
    """Export all buffered log entries as a CSV download."""
    import csv
    import io

    from power_master.dashboard.log_buffer import log_buffer

    records = log_buffer.get_records(limit=10000, level=level or None)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Timestamp", "Level", "Logger", "Message"])
    for r in records:
        writer.writerow([r["timestamp"], r["level"], r["logger"], r["message"]])

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=power_master_logs.csv"},
    )


@router.get("/logs/export/db")
async def export_db_logs(request: Request, hours: int = 24, level: str = "") -> Response:
    """Export DB-backed logs as a CSV download."""
    import csv
    import io

    hours = min(max(hours, 1), 168)
    repo = request.app.state.repo

    # Flush pending logs so export is up-to-date
    db_log_handler = getattr(request.app.state, "db_log_handler", None)
    if db_log_handler is not None:
        try:
            await db_log_handler.flush_to_db()
        except Exception:
            pass

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    records = await repo.get_logs_since(cutoff, level=level or None)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Timestamp", "Level", "Logger", "Message"])
    for r in records:
        writer.writerow([r["recorded_at"], r["level"], r["logger_name"], r["message"]])

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=power_master_db_logs_{hours}h.csv"},
    )


@router.get("/plan/history/export")
async def export_plan_history(request: Request, hours: int = 24) -> Response:
    """Export plan history with slot details as a CSV download."""
    import csv
    import io

    hours = min(max(hours, 1), 168)
    repo = request.app.state.repo
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    # Get plans created since the cutoff
    all_plans = await repo.get_plan_history(limit=10000)
    plans = [p for p in all_plans if p["created_at"] >= cutoff]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "PlanVersion", "PlanCreated", "TriggerReason", "ObjectiveScore", "Status",
        "SlotIndex", "SlotStart", "SlotEnd", "OperatingMode", "TargetPowerW",
        "ExpectedSOC", "ImportRateCents", "ExportRateCents",
        "SolarForecastW", "LoadForecastW",
    ])

    for plan in plans:
        slots = await repo.get_plan_slots(plan["id"])
        if not slots:
            # Write plan row with empty slot columns
            writer.writerow([
                plan["version"], plan["created_at"], plan["trigger_reason"],
                plan["objective_score"], plan["status"],
                "", "", "", "", "", "", "", "", "", "",
            ])
        else:
            for slot in slots:
                writer.writerow([
                    plan["version"], plan["created_at"], plan["trigger_reason"],
                    plan["objective_score"], plan["status"],
                    slot["slot_index"], slot["slot_start"], slot["slot_end"],
                    slot["operating_mode"], slot["target_power_w"],
                    slot["expected_soc"], slot["import_rate_cents"],
                    slot["export_rate_cents"], slot["solar_forecast_w"],
                    slot["load_forecast_w"],
                ])

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=power_master_plan_history_{hours}h.csv"},
    )


@router.get("/forecast/accuracy")
async def forecast_accuracy(request: Request, days: int = 30) -> dict:
    """Forecast MAE per (provider, metric, horizon) over the last N days.

    Solar actuals come from telemetry.solar_power_w; weather/tariff come from
    historical_data rows stored by HistoryCollector.  Storm alerts have no
    actuals path and are reported with MAE=None.
    """
    days = min(max(days, 1), 365)
    repo = request.app.state.repo
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()
    end_iso = now.isoformat()

    # Actuals lookup tables keyed by 30-min UTC slot
    telemetry = await repo.get_telemetry_since(cutoff)
    solar_actual_by_slot: dict[str, list[float]] = {}
    for row in telemetry:
        ts = _parse_iso(row["recorded_at"])
        if ts is None:
            continue
        key = _slot_key(ts)
        solar_actual_by_slot.setdefault(key, []).append(float(row["solar_power_w"]))

    historical_by_type: dict[str, dict[str, float]] = {}
    for data_type in ("import_price_cents", "export_price_cents",
                      "temperature_c", "cloud_cover_pct"):
        rows = await repo.get_historical(data_type, cutoff, end_iso)
        lookup: dict[str, float] = {}
        for r in rows:
            ts = _parse_iso(r["recorded_at"])
            if ts is None:
                continue
            lookup[_slot_key(ts)] = float(r["value"])
        historical_by_type[data_type] = lookup

    def actual_for(provider: str, metric: str, slot_key: str) -> float | None:
        if provider == "solar" and metric == "pv_estimate_w":
            xs = solar_actual_by_slot.get(slot_key)
            return sum(xs) / len(xs) if xs else None
        if metric in historical_by_type:
            return historical_by_type[metric].get(slot_key)
        return None

    # Pull all forecast samples in the window (any provider, any metric)
    rows = await repo.get_forecast_samples(
        "solar", target_time_start=cutoff, target_time_end=end_iso,
    )
    rows += await repo.get_forecast_samples(
        "weather", target_time_start=cutoff, target_time_end=end_iso,
    )
    rows += await repo.get_forecast_samples(
        "tariff", target_time_start=cutoff, target_time_end=end_iso,
    )
    rows += await repo.get_forecast_samples(
        "storm", target_time_start=cutoff, target_time_end=end_iso,
    )

    # Group by (provider, metric, horizon_hours) and aggregate MAE against actuals
    buckets: dict[tuple[str, str, float], dict[str, Any]] = {}
    for r in rows:
        key = (r["provider_type"], r["metric"], round(float(r["horizon_hours"]), 2))
        b = buckets.setdefault(key, {"n_samples": 0, "errors": []})
        b["n_samples"] += 1
        target = _parse_iso(r["target_time"])
        if target is None:
            continue
        actual = actual_for(r["provider_type"], r["metric"], _slot_key(target))
        if actual is None:
            continue
        b["errors"].append(abs(float(r["predicted_value"]) - actual))

    result = []
    for (provider, metric, horizon), b in sorted(buckets.items()):
        errors = b["errors"]
        mae = sum(errors) / len(errors) if errors else None
        result.append({
            "provider": provider,
            "metric": metric,
            "horizon_hours": horizon,
            "n_samples": b["n_samples"],
            "n_with_actual": len(errors),
            "mae": round(mae, 3) if mae is not None else None,
        })

    return {"window_days": days, "buckets": result}


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _slot_key(t: datetime) -> str:
    minute = 30 * (t.minute // 30)
    return t.replace(minute=minute, second=0, microsecond=0).isoformat()


@router.get("/forecast/calibration")
async def forecast_calibration(request: Request) -> dict:
    """Inspect the current solar-forecast calibration model."""
    application = getattr(request.app.state, "application", None)
    if application is None:
        return {"enabled": False, "status": "application_not_available"}
    config = request.app.state.config
    solar_cfg = config.providers.solar
    model = getattr(application, "_solar_calibration_model", None)
    last_fit = getattr(application, "_solar_calibration_last_fit", None)
    return {
        "enabled": bool(solar_cfg.calibration_enabled),
        "window_days": solar_cfg.calibration_window_days,
        "refit_interval_seconds": solar_cfg.calibration_refit_interval_seconds,
        "last_fit": last_fit.isoformat() if last_fit else None,
        "model": model.as_dict() if model is not None else None,
    }


@router.get("/debug/export")
async def export_debug_bundle(request: Request, hours: int = 24) -> Response:
    """Bundle redacted config, current plan, last N hours of data and logs as a .zip."""
    from power_master.dashboard.debug_export import build_debug_bundle
    from power_master.dashboard.log_buffer import log_buffer

    denied = require_admin(request)
    if denied:
        return denied

    hours = min(max(hours, 1), 168)
    repo = request.app.state.repo
    config = request.app.state.config

    db_log_handler = getattr(request.app.state, "db_log_handler", None)
    if db_log_handler is not None:
        try:
            await db_log_handler.flush_to_db()
        except Exception:
            pass

    in_memory_logs = log_buffer.get_records(limit=10000)

    application = getattr(request.app.state, "application", None)
    solar_calibration = None
    if application is not None:
        model = getattr(application, "_solar_calibration_model", None)
        last_fit = getattr(application, "_solar_calibration_last_fit", None)
        solar_calibration = {
            "enabled": bool(config.providers.solar.calibration_enabled),
            "last_fit": last_fit.isoformat() if last_fit else None,
            "model": model.as_dict() if model is not None else None,
        }

    payload = await build_debug_bundle(
        config, repo, hours=hours, in_memory_logs=in_memory_logs,
        solar_calibration=solar_calibration,
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    filename = f"power_master_debug_{timestamp}.zip"
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── Config ───────────────────────────────────────────

@router.get("/config")
async def get_config(request: Request) -> dict:
    """Get current configuration."""
    config = request.app.state.config
    return config.model_dump()


# ── System / Updates ─────────────────────────────────

@router.get("/system/version")
async def system_version(request: Request) -> dict:
    """Get current and latest version info."""
    updater = getattr(request.app.state, "updater", None)
    if updater is None:
        return {"error": "Update manager not available"}
    return updater.to_dict()


@router.post("/system/check-update")
async def check_update(request: Request) -> dict:
    """Force an immediate version check against GHCR."""
    require_admin(request)
    updater = getattr(request.app.state, "updater", None)
    if updater is None:
        return JSONResponse({"status": "error", "message": "Update manager not available"}, 503)
    available = await updater.check_for_update()
    return {"status": "ok", "update_available": available, **updater.to_dict()}


@router.post("/notifications/test/{channel}")
async def test_notification(request: Request, channel: str) -> dict:
    """Send a test notification to a specific channel."""
    denied = require_admin(request)
    if denied:
        return denied

    nm = getattr(request.app.state, "notification_manager", None)
    if nm is None:
        return {"status": "error", "error": "Notification manager not available"}

    valid = {"telegram", "email", "pushover", "ntfy", "webhook"}
    if channel not in valid:
        return {"status": "error", "error": f"Unknown channel: {channel}"}

    result = await nm.send_test(channel)
    return result


@router.post("/system/update")
async def trigger_update(request: Request) -> dict:
    """Trigger a self-update: pull latest image and restart."""
    require_admin(request)
    updater = getattr(request.app.state, "updater", None)
    if updater is None:
        return JSONResponse({"status": "error", "message": "Update manager not available"}, 503)
    result = await updater.execute_update()
    return result
