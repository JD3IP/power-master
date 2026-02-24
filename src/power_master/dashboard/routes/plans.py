"""Plan history browser routes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

# AEST is UTC+10 (Brisbane has no DST)
AEST = timezone(timedelta(hours=10))

MODE_NAMES = {
    1: "Self-Use",
    2: "Zero Export",
    3: "Charge",
    4: "Discharge",
}


def _format_slot_for_display(slot: dict) -> dict:
    """Transform a raw plan slot dict for template display."""
    slot = dict(slot)

    # Mode friendly name
    slot["mode_name"] = MODE_NAMES.get(slot.get("operating_mode"), f"Mode {slot.get('operating_mode')}")

    # Timestamps to AEST
    for field in ("slot_start", "slot_end"):
        raw = slot.get(field, "")
        if raw:
            try:
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                local = dt.astimezone(AEST)
                slot[field + "_local"] = local.strftime("%H:%M")
                slot[field + "_date"] = local.strftime("%d/%m %H:%M")
            except (ValueError, TypeError):
                slot[field + "_local"] = raw
                slot[field + "_date"] = raw

    # Signed battery power: negative for discharge
    mode = slot.get("operating_mode", 1)
    power = slot.get("target_power_w", 0)
    if mode == 4:
        slot["signed_power_w"] = -abs(power)
    else:
        slot["signed_power_w"] = abs(power)

    # Prices rounded to 1 decimal
    for price_field in ("import_rate_cents", "export_rate_cents"):
        val = slot.get(price_field)
        if val is not None:
            slot[price_field + "_fmt"] = f"{float(val):.1f}"
        else:
            slot[price_field + "_fmt"] = "--"

    # Scheduled loads from JSON
    loads_raw = slot.get("scheduled_loads_json")
    if loads_raw:
        try:
            slot["scheduled_loads"] = json.loads(loads_raw)
        except (json.JSONDecodeError, TypeError):
            slot["scheduled_loads"] = []
    else:
        slot["scheduled_loads"] = []

    return slot


@router.get("/plans", response_class=HTMLResponse)
async def plans_page(request: Request) -> HTMLResponse:
    """Render plan history page."""
    templates = request.app.state.templates
    repo = request.app.state.repo

    plans = await repo.get_plan_history(limit=50)
    active = await repo.get_active_plan()
    active_slots = []
    if active:
        raw_slots = await repo.get_plan_slots(active["id"])
        active_slots = [_format_slot_for_display(s) for s in raw_slots]

    return templates.TemplateResponse(
        "plans.html",
        {
            "request": request,
            "plans": plans,
            "active_plan": active,
            "active_slots": active_slots,
        },
    )
