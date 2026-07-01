"""Admin settings routes — view and edit all configuration."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError

from power_master.dashboard.auth import require_admin

router = APIRouter()
logger = logging.getLogger(__name__)


def _parse_form_to_config(form_data: dict[str, str]) -> dict[str, Any]:
    """Convert flat dot-notation form keys to a nested dict.

    E.g. {"battery.capacity_wh": "10000"} -> {"battery": {"capacity_wh": "10000"}}
    Checkbox fields that are absent from form_data are handled by the caller.
    """
    result: dict[str, Any] = {}
    for key, value in form_data.items():
        parts = key.split(".")
        current = result
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = value
    return result


# All checkbox field paths — these are sent as "on" if checked, absent if unchecked
CHECKBOX_FIELDS = {
    "auto_update_stable",
    "planning.optimiser_enabled",
    "providers.solar.calibration_enabled",
    "storm.enabled",
    "mqtt.enabled",
    "mqtt.ha_discovery_enabled",
    "notifications.enabled",
    "notifications.channels.telegram.enabled",
    "notifications.channels.email.enabled",
    "notifications.channels.email.use_tls",
    "notifications.channels.pushover.enabled",
    "notifications.channels.ntfy.enabled",
    "notifications.channels.webhook.enabled",
    "notifications.events.price_spike.enabled",
    "notifications.events.price_spike_end.enabled",
    "notifications.events.battery_low.enabled",
    "notifications.events.battery_full.enabled",
    "notifications.events.inverter_offline.enabled",
    "notifications.events.inverter_online.enabled",
    "notifications.events.resilience_degraded.enabled",
    "notifications.events.resilience_recovered.enabled",
    "notifications.events.log_error.enabled",
}

# Fields typed as list[str] — sent as comma-separated strings from HTML forms
LIST_FIELDS = {
    "providers.storm.warning_product_ids",
}

# Optional numeric fields where blank input should mean null
NULLABLE_FIELDS = {
    "providers.solar.azimuth",
}

# Fields displayed as percentage in UI but stored as 0-1 decimal.
# The settings form always sends these as whole-number percents (0-100);
# the route converts to the 0-1 fractions the schema/solver expect.
PERCENTAGE_FIELDS = {
    "battery.soc_min_hard",
    "battery.soc_max_hard",
    "battery.soc_min_soft",
    "battery.soc_max_soft",
    "battery.round_trip_efficiency",
    "battery_targets.evening_soc_target",
    "battery_targets.free_window_soc_target",
    "battery_targets.morning_soc_minimum",
    "battery_targets.daytime_reserve_soc_target",
    "planning.soc_deviation_tolerance",
    "anti_oscillation.hysteresis_band",
    "arbitrage.price_dampen_factor",
    "storm.reserve_soc_target",
    "storm.probability_threshold",
    "resilience.degraded_safety_margin",
    "notifications.battery_low_threshold",
    "notifications.battery_full_threshold",
}


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """Render settings page. Viewers can view read-only; admins can edit."""
    import json
    from datetime import date, datetime

    templates = request.app.state.templates
    config = request.app.state.config

    # Determine if current user can edit (admin or auth disabled)
    auth_config = config.dashboard.auth
    is_read_only = False
    tariff_can_edit = True
    if auth_config.users:
        from power_master.dashboard.auth import get_session
        session = get_session(request)
        if session and session.get("role") != "admin":
            is_read_only = True
            tariff_can_edit = False

    # Check for flash messages via query params
    saved = request.query_params.get("saved")
    error = request.query_params.get("error")

    # Get inverter firmware info if available
    adapter = getattr(request.app.state, "adapter", None)
    firmware = getattr(adapter, "firmware", {}) if adapter else {}

    # Serialize tariff config for TOU editor (convert dates to ISO strings)
    tariff_config = config.providers.tariff
    tariff_dict = tariff_config.model_dump(mode="json")
    tariff_config_json = json.dumps(tariff_dict)

    # Serialize the mode schedule for the Schedule editor.
    mode_schedule_json = json.dumps(config.mode_schedule.model_dump(mode="json"))

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "config": config,
            "saved": saved == "1",
            "error": error or "",
            "is_read_only": is_read_only,
            "tariff_can_edit": tariff_can_edit,
            "firmware": firmware,
            "tariff_config_json": tariff_config_json,
            "mode_schedule_json": mode_schedule_json,
        },
    )


@router.post("/settings")
async def save_settings(request: Request) -> RedirectResponse:
    """Save settings and hot-reload all affected components."""
    auth_config = request.app.state.config.dashboard.auth
    if auth_config.users:
        denied = require_admin(request)
        if denied:
            return denied

    config_manager = request.app.state.config_manager
    if config_manager is None:
        return RedirectResponse(
            "/settings?error=Config+manager+not+available", status_code=303,
        )

    form = await request.form()
    form_data = dict(form)

    # Handle checkboxes — absent = False, present = True
    for field_path in CHECKBOX_FIELDS:
        if field_path in form_data:
            form_data[field_path] = True
        else:
            form_data[field_path] = False

    # Handle list fields — split comma-separated strings into actual lists
    for field_path in LIST_FIELDS:
        if field_path in form_data and isinstance(form_data[field_path], str):
            form_data[field_path] = [
                s.strip() for s in form_data[field_path].split(",") if s.strip()
            ]

    for field_path in NULLABLE_FIELDS:
        if field_path in form_data and form_data[field_path] == "":
            form_data[field_path] = None

    # Handle percentage fields — convert from integer % to 0-1 decimal
    for field_path in PERCENTAGE_FIELDS:
        if field_path in form_data and isinstance(form_data[field_path], str):
            try:
                form_data[field_path] = float(form_data[field_path]) / 100.0
            except ValueError:
                pass

    # Parse flat form keys to nested dict
    updates = _parse_form_to_config(form_data)

    # Validate by attempting to build AppConfig from merged data
    try:
        from power_master.config.schema import AppConfig

        current_raw = config_manager.get_raw()
        merged = config_manager._deep_merge(current_raw, updates)
        AppConfig.model_validate(merged)
    except (ValidationError, ValueError) as e:
        error_msg = str(e).replace("\n", " ")[:200]
        logger.warning("Settings validation failed: %s", error_msg)
        return RedirectResponse(
            f"/settings?error={error_msg}", status_code=303,
        )

    # Use Application.reload_config() for hot-reload if available,
    # otherwise fall back to direct save
    application = getattr(request.app.state, "application", None)
    if application is not None:
        try:
            await application.reload_config(updates, request.app)
        except Exception as e:
            logger.exception("Failed to reload config")
            return RedirectResponse(
                f"/settings?error=Reload+failed:+{e}", status_code=303,
            )
    else:
        # Fallback: direct save (test environment without full Application)
        try:
            new_config = config_manager.save_user_config(updates)
        except Exception as e:
            logger.exception("Failed to save settings")
            return RedirectResponse(
                f"/settings?error=Save+failed:+{e}", status_code=303,
            )
        request.app.state.config = new_config

    logger.info("Settings saved: changed=%s", list(updates.keys()))
    return RedirectResponse("/settings?saved=1", status_code=303)


# ── Inverter firmware settings panel (curated + generic registers) ──────────
#
# These endpoints read/write the inverter's own holding registers live over
# Modbus — they do NOT touch config.yaml. Used by the "Inverter firmware
# settings" panel to expose parameters not otherwise available in the UI
# (e.g. CT/meter offset via the generic register tool).


def _inverter_adapter(request: Request):
    """Return the inverter adapter if it supports the firmware-settings API."""
    adapter = getattr(request.app.state, "adapter", None)
    if adapter is None or not hasattr(adapter, "read_device_settings"):
        return None
    return adapter


@router.get("/api/inverter/settings")
async def get_inverter_settings(request: Request) -> JSONResponse:
    """Read the curated named inverter settings live from the device."""
    adapter = _inverter_adapter(request)
    if adapter is None:
        return JSONResponse(
            {"error": "Inverter register access not supported by this adapter"},
            status_code=501,
        )
    try:
        settings = await adapter.read_device_settings()
    except Exception as e:  # noqa: BLE001 — surface as JSON error
        logger.warning("Failed to read inverter settings: %s", e)
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"settings": settings, "firmware": getattr(adapter, "firmware", {})})


@router.post("/api/inverter/settings")
async def write_inverter_setting(request: Request) -> JSONResponse:
    """Write a single curated named setting to the inverter (admin only)."""
    denied = require_admin(request)
    if denied:
        return denied
    adapter = _inverter_adapter(request)
    if adapter is None:
        return JSONResponse({"error": "Inverter register access not supported"}, status_code=501)

    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key or value is None:
        return JSONResponse({"error": "Both 'key' and 'value' are required"}, status_code=400)
    try:
        raw = await adapter.write_device_setting(str(key), float(value))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001 — device/IO failure
        logger.exception("Failed to write inverter setting %s", key)
        return JSONResponse({"error": str(e)}, status_code=502)
    logger.info("Inverter setting written via UI: %s=%s (raw=%d)", key, value, raw)
    return JSONResponse({"ok": True, "key": key, "value": value, "raw": raw})


@router.post("/api/inverter/register/read")
async def read_inverter_register(request: Request) -> JSONResponse:
    """Read an arbitrary holding register (generic advanced tool, admin only)."""
    denied = require_admin(request)
    if denied:
        return denied
    adapter = _inverter_adapter(request)
    if adapter is None or not hasattr(adapter, "read_holding_register"):
        return JSONResponse({"error": "Inverter register access not supported"}, status_code=501)

    body = await request.json()
    try:
        address = _parse_int(body.get("address"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid register address"}, status_code=400)
    try:
        raw = await adapter.read_holding_register(address)
    except Exception as e:  # noqa: BLE001
        logger.warning("Generic register read failed at %s: %s", body.get("address"), e)
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"ok": True, "address": address, "value": raw, "hex": f"0x{raw:04X}"})


@router.post("/api/inverter/register/write")
async def write_inverter_register(request: Request) -> JSONResponse:
    """Write an arbitrary holding register (generic advanced tool, admin only)."""
    denied = require_admin(request)
    if denied:
        return denied
    adapter = _inverter_adapter(request)
    if adapter is None or not hasattr(adapter, "write_holding_register"):
        return JSONResponse({"error": "Inverter register access not supported"}, status_code=501)

    body = await request.json()
    try:
        address = _parse_int(body.get("address"))
        value = _parse_int(body.get("value"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid address or value (use decimal or 0x hex)"}, status_code=400)
    try:
        await adapter.write_holding_register(address, value)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        logger.exception("Generic register write failed at %s", address)
        return JSONResponse({"error": str(e)}, status_code=502)
    logger.info("Generic register written via UI: addr=%d value=%d", address, value)
    return JSONResponse({"ok": True, "address": address, "value": value})


@router.post("/api/inverter/register/scan")
async def scan_inverter_registers(request: Request) -> JSONResponse:
    """Read a contiguous block of holding registers (read-only exploration).

    Used to locate registers not in the curated list by matching values against
    known settings (e.g. the installer app). Admin only.
    """
    denied = require_admin(request)
    if denied:
        return denied
    adapter = _inverter_adapter(request)
    if adapter is None or not hasattr(adapter, "scan_holding_registers"):
        return JSONResponse({"error": "Inverter register access not supported"}, status_code=501)

    body = await request.json()
    try:
        start = _parse_int(body.get("start"))
        count = _parse_int(body.get("count"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid start or count (use decimal or 0x hex)"}, status_code=400)
    try:
        rows = await adapter.scan_holding_registers(start, count)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        logger.warning("Register scan failed at start=%s count=%s: %s", start, count, e)
        return JSONResponse({"error": str(e)}, status_code=502)

    registers = []
    for row in rows:
        entry = {"address": row["address"]}
        if "error" in row:
            entry["error"] = row["error"]
        else:
            v = row["value"]
            entry["value"] = v
            entry["signed"] = v - 0x10000 if v >= 0x8000 else v
            entry["hex"] = f"0x{v:04X}"
        registers.append(entry)
    return JSONResponse({"ok": True, "registers": registers})


@router.post("/api/mode-schedule")
async def save_mode_schedule(request: Request) -> JSONResponse:
    """Validate and apply the inverter mode schedule, hot-reloaded (no restart)."""
    denied = require_admin(request)
    if denied:
        return denied

    from power_master.config.schema import ModeScheduleConfig

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    # Validate the whole schedule via the Pydantic model.
    try:
        validated = ModeScheduleConfig.model_validate(body)
    except ValidationError as e:
        msg = str(e).replace("\n", " ")[:300]
        return JSONResponse({"error": msg}, status_code=400)

    updates = {"mode_schedule": validated.model_dump(mode="json")}

    application = getattr(request.app.state, "application", None)
    if application is not None:
        try:
            await application.reload_config(updates, request.app)
        except Exception as e:
            logger.exception("Failed to apply mode schedule")
            return JSONResponse({"error": f"Reload failed: {e}"}, status_code=500)
    else:
        # Fallback for test/standalone: save directly.
        config_manager = getattr(request.app.state, "config_manager", None)
        if config_manager is None:
            return JSONResponse({"error": "Config manager not available"}, status_code=500)
        try:
            new_config = config_manager.save_user_config(updates)
            request.app.state.config = new_config
        except Exception as e:
            logger.exception("Failed to save mode schedule")
            return JSONResponse({"error": f"Save failed: {e}"}, status_code=500)

    # Nudge the control loop to re-evaluate now so the schedule change applies
    # immediately rather than at the next tick.
    control_loop = getattr(request.app.state, "control_loop", None)
    if control_loop is not None and hasattr(control_loop, "request_tick"):
        control_loop.request_tick()

    logger.info(
        "Mode schedule saved: enabled=%s, %d rule(s)", validated.enabled, len(validated.rules),
    )
    return JSONResponse({"ok": True, "enabled": validated.enabled, "rules": len(validated.rules)})


def _parse_int(raw) -> int:
    """Parse a decimal or 0x-hex integer from JSON (int or string)."""
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        raise ValueError("boolean not allowed")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            raise ValueError("empty")
        return int(s, 16) if s.lower().startswith("0x") else int(s, 10)
    raise TypeError(f"cannot parse int from {type(raw)}")
