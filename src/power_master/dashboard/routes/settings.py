"""Admin settings routes — view and edit all configuration."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

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
    "storm.enabled",
    "mqtt.enabled",
    "mqtt.ha_discovery_enabled",
}

# Fields typed as list[str] — sent as comma-separated strings from HTML forms
LIST_FIELDS = {
    "providers.storm.warning_product_ids",
}

# Optional numeric fields where blank input should mean null
NULLABLE_FIELDS = {
    "providers.solar.azimuth",
}


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """Render settings page."""
    templates = request.app.state.templates
    config = request.app.state.config

    # Check for flash messages via query params
    saved = request.query_params.get("saved")
    error = request.query_params.get("error")

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "config": config,
            "saved": saved == "1",
            "error": error or "",
        },
    )


@router.post("/settings")
async def save_settings(request: Request) -> RedirectResponse:
    """Save settings and hot-reload all affected components."""
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
