"""REST API endpoints returning JSON data."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from power_master.dashboard.auth import require_admin

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Request models ───────────────────────────────────

class ModeRequest(BaseModel):
    mode: int
    power_w: int = 0
    timeout_s: float | None = None


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
        return {
            "connected": False,
            "error": "Not connected to inverter",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "config": {
                "host": request.app.state.config.hardware.foxess.host,
                "port": request.app.state.config.hardware.foxess.port,
                "unit_id": request.app.state.config.hardware.foxess.unit_id,
            },
        }

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

    # Read all KH input registers under the adapter lock
    try:
        async with adapter._lock:
            await read_input_i16("PV1 Power", Registers.PV1_POWER, "W")
            await read_input_i16("PV2 Power", Registers.PV2_POWER, "W")
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
        "config": {
            "host": request.app.state.config.hardware.foxess.host,
            "port": request.app.state.config.hardware.foxess.port,
            "unit_id": request.app.state.config.hardware.foxess.unit_id,
        },
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


@router.post("/system/update")
async def trigger_update(request: Request) -> dict:
    """Trigger a self-update: pull latest image and restart."""
    require_admin(request)
    updater = getattr(request.app.state, "updater", None)
    if updater is None:
        return JSONResponse({"status": "error", "message": "Update manager not available"}, 503)
    result = await updater.execute_update()
    return result
