"""TOU tariff editor backend routes."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from zoneinfo import ZoneInfo

from power_master.config.schema import AppConfig, TariffProviderConfig
from power_master.dashboard.auth import get_session
from power_master.tariff.providers.static_tou import StaticTariffProvider

router = APIRouter()
logger = logging.getLogger(__name__)


def _is_admin(request: Request) -> bool:
    """Check if current user is admin. Returns True if auth disabled or user is admin."""
    config = request.app.state.config
    if not config.dashboard.auth.users:
        return True
    session = get_session(request)
    if session and session.get("role") == "admin":
        return True
    return False


@router.get("/settings/tariff/resolve")
async def resolve_tariff(request: Request) -> dict:
    """Resolve the CURRENTLY SAVED config's tariff for ribbon rendering.

    Query param: date (YYYY-MM-DD), default = today in plan timezone
    """
    config = request.app.state.config

    if config.providers.tariff.type != "tou":
        return {"ok": False, "error": "not a TOU tariff"}

    # Parse date param
    date_str = request.query_params.get("date")
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return {"ok": False, "error": "invalid date format (use YYYY-MM-DD)"}
    else:
        tz = ZoneInfo(config.providers.tariff.timezone)
        target_date = datetime.now(tz).date()

    # Build transient provider with no cap tracker
    try:
        tariff_provider = StaticTariffProvider(config.providers.tariff)
    except Exception as e:
        return {"ok": False, "error": f"failed to create provider: {e}"}

    # Fetch historical for that date
    tz = ZoneInfo(config.providers.tariff.timezone)
    local_midnight_utc = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=tz).astimezone(timezone.utc)
    next_midnight_utc = local_midnight_utc + timedelta(hours=24)

    try:
        schedule = await tariff_provider.fetch_historical(local_midnight_utc, next_midnight_utc)
    except Exception as e:
        return {"ok": False, "error": f"failed to fetch tariff: {e}"}

    # Build response
    slots_data = []
    uncovered_count = 0

    for slot in schedule.slots:
        # Convert slot times back to plan timezone for ISO output
        start_local = slot.start.astimezone(tz)
        end_local = slot.end.astimezone(tz)

        descriptor = slot.descriptor or "unknown"
        if descriptor == "unknown" or slot.descriptor is None:
            uncovered_count += 1

        slots_data.append({
            "start": start_local.isoformat(),
            "end": end_local.isoformat(),
            "import_c": slot.import_price_cents,
            "export_c": slot.export_price_cents,
            "descriptor": descriptor,
        })

    ev_windows = []
    if config.ev.enabled and config.ev.charge_windows:
        ev_windows = config.ev.charge_windows

    return {
        "ok": True,
        "date": target_date.isoformat(),
        "timezone": config.providers.tariff.timezone,
        "supply_c_per_day": config.providers.tariff.plan.supply_charge_c_per_day,
        "slots": slots_data,
        "coverage": {"uncovered": uncovered_count},
        "ev_windows": ev_windows,
    }


@router.post("/settings/tariff/resolve")
async def resolve_tariff_preview(request: Request) -> dict:
    """Resolve an UNSAVED, in-editor tariff config (for live preview).

    JSON body: {type, timezone, grid_charge_policy, plan, ...}
    Optional: date (YYYY-MM-DD)
    """
    config = request.app.state.config
    body = await request.json()

    # Extract date if provided
    date_str = body.pop("date", None)
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return {"ok": False, "error": "invalid date format (use YYYY-MM-DD)"}
    else:
        # Default to today in the proposed timezone
        tz = ZoneInfo(body.get("timezone", config.providers.tariff.timezone or "UTC"))
        target_date = datetime.now(tz).date()

    # Validate the tariff config
    try:
        tariff_config = TariffProviderConfig.model_validate(body)
    except ValidationError as e:
        errors = [f"{err['loc']}: {err['msg']}" for err in e.errors()]
        return {"ok": False, "errors": errors}

    if tariff_config.type != "tou":
        return {"ok": False, "error": "only TOU tariffs supported"}

    # Create transient provider
    try:
        tariff_provider = StaticTariffProvider(tariff_config)
    except Exception as e:
        return {"ok": False, "error": f"failed to create provider: {e}"}

    # Fetch and generate slots
    tz = ZoneInfo(tariff_config.timezone)
    local_midnight_utc = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=tz).astimezone(timezone.utc)
    next_midnight_utc = local_midnight_utc + timedelta(hours=24)

    try:
        schedule = await tariff_provider.fetch_historical(local_midnight_utc, next_midnight_utc)
    except Exception as e:
        return {"ok": False, "error": f"failed to fetch tariff: {e}"}

    # Build response (same format as GET resolve)
    slots_data = []
    uncovered_count = 0

    for slot in schedule.slots:
        start_local = slot.start.astimezone(tz)
        end_local = slot.end.astimezone(tz)

        descriptor = slot.descriptor or "unknown"
        if descriptor == "unknown" or slot.descriptor is None:
            uncovered_count += 1

        slots_data.append({
            "start": start_local.isoformat(),
            "end": end_local.isoformat(),
            "import_c": slot.import_price_cents,
            "export_c": slot.export_price_cents,
            "descriptor": descriptor,
        })

    return {
        "ok": True,
        "date": target_date.isoformat(),
        "timezone": tariff_config.timezone,
        "supply_c_per_day": tariff_config.plan.supply_charge_c_per_day,
        "slots": slots_data,
        "coverage": {"uncovered": uncovered_count},
        "ev_windows": config.ev.charge_windows if config.ev.enabled else [],
    }


@router.post("/settings/tariff")
async def save_tariff(request: Request):
    """Guarded save of the complete tariff config."""
    config = request.app.state.config

    # Admin gate
    if not _is_admin(request):
        return JSONResponse(
            status_code=403,
            content={"ok": False, "error": "admin required"},
        )

    body = await request.json()

    # Pre-save dry-run
    cm = request.app.state.config_manager
    if cm is None:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "config manager not available"},
        )

    # Merge with current config
    updates = {"providers": {"tariff": body}}
    try:
        merged = cm._deep_merge(cm.get_raw(), updates)
        validated = AppConfig.model_validate(merged)
    except ValidationError as e:
        errors = [f"{err['loc']}: {err['msg']}" for err in e.errors()]
        return JSONResponse(
            status_code=400,
            content={"ok": False, "errors": errors},
        )

    # If TOU, validate provider creation and fetch_prices
    if validated.providers.tariff.type == "tou":
        try:
            tariff_provider = StaticTariffProvider(validated.providers.tariff)
            await tariff_provider.fetch_prices()
        except Exception as e:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": str(e)},
            )

    # Persist
    application = getattr(request.app.state, "application", None)
    try:
        if application is not None:
            await application.reload_config(updates, request.app)
        else:
            # Fallback: direct save (test environment)
            new_config = cm.save_user_config(updates)
            request.app.state.config = new_config
    except Exception as e:
        logger.exception("Failed to save tariff config")
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"save failed: {e}"},
        )

    logger.info("Tariff config saved: type=%s", validated.providers.tariff.type)
    return JSONResponse(status_code=200, content={"ok": True})


@router.get("/settings/tariff/templates")
async def tariff_templates(request: Request) -> dict:
    """Get preset tariff library."""
    templates = []

    # Search for example config files
    project_root = Path(__file__).parent.parent.parent.parent.parent
    examples = [
        ("four4free", "config.site-a-four4free.example.yaml", "Globird FOUR4FREE"),
        ("zerohero", "config.site-b-zerohero.example.yaml", "Globird ZEROHERO (VPP)"),
    ]

    for template_id, filename, display_name in examples:
        filepath = project_root / filename
        if not filepath.exists():
            logger.debug("Template file not found: %s", filepath)
            continue

        try:
            with open(filepath) as f:
                data = yaml.safe_load(f)

            # Extract tariff config
            if data and "providers" in data and "tariff" in data["providers"]:
                tariff_cfg = data["providers"]["tariff"]
                templates.append({
                    "id": template_id,
                    "name": display_name,
                    "tariff": tariff_cfg,
                })
        except Exception as e:
            logger.warning("Failed to load template %s: %s", filename, e)

    return {"ok": True, "templates": templates}
