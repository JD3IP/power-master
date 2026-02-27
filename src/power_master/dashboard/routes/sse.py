"""Server-Sent Events for live dashboard updates."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/events")
async def event_stream(request: Request) -> StreamingResponse:
    """SSE endpoint for live telemetry and mode updates."""
    repo = request.app.state.repo
    config = request.app.state.config
    interval = config.dashboard.sse_interval_seconds

    async def generate():
        while True:
            if await request.is_disconnected():
                break

            try:
                # Prefer live telemetry from control loop (real-time) over DB (may be stale)
                control_loop_ref = getattr(request.app.state, "control_loop", None)
                live_telem = control_loop_ref.state.last_telemetry if control_loop_ref else None
                last_tick = control_loop_ref.state.last_tick_at if control_loop_ref else None
                stale_live = True
                if last_tick is not None:
                    stale_live = (datetime.now(timezone.utc) - last_tick).total_seconds() > max(
                        interval * 2, 20
                    )

                # Avoid direct Modbus reads in SSE loop; keep UX responsive and
                # let the background control loop own hardware polling.

                if live_telem is not None:
                    telemetry = {
                        "soc": live_telem.soc,
                        "battery_power_w": live_telem.battery_power_w,
                        "solar_power_w": live_telem.solar_power_w,
                        "grid_power_w": live_telem.grid_power_w,
                        "load_power_w": live_telem.load_power_w,
                        "inverter_mode": live_telem.inverter_mode,
                    }
                else:
                    telemetry = await repo.get_latest_telemetry()
                spike = await repo.get_active_spike()
                import_price_cents = await repo.get_latest_historical_value("import_price_cents")
                export_price_cents = await repo.get_latest_historical_value("export_price_cents")

                data: dict = {
                    "telemetry": telemetry,
                    "spike_active": spike is not None,
                    "price_import_cents": import_price_cents,
                    "price_export_cents": export_price_cents,
                }

                # Include accounting summary if engine is available
                accounting_engine = getattr(request.app.state, "accounting", None)
                if accounting_engine:
                    summary = accounting_engine.get_summary()
                    data["accounting"] = {
                        "wacb_cents": round(summary.wacb_cents, 1),
                        "stored_value_cents": round(summary.stored_value_cents, 1),
                        "today_net_cost_cents": summary.today_net_cost_cents,
                        "week_net_cost_cents": summary.week_net_cost_cents,
                    }
                    if summary.cycle:
                        data["accounting"]["cycle"] = {
                            "net_cost_cents": summary.cycle.net_cost_cents,
                            "import_cost_cents": summary.cycle.total_import_cost_cents,
                            "export_revenue_cents": summary.cycle.total_export_revenue_cents,
                            "days_elapsed": summary.cycle.days_elapsed,
                            "days_remaining": summary.cycle.days_remaining,
                        }

                # Include mode + override status if control loop is available
                control_loop = getattr(request.app.state, "control_loop", None)
                manual_override = getattr(request.app.state, "manual_override", None)

                if control_loop:
                    from power_master.hardware.base import OperatingMode as _OM

                    state = control_loop.state
                    override_active = manual_override.is_active if manual_override else False
                    if override_active:
                        source = "manual"
                    elif state.current_plan:
                        source = "plan"
                    else:
                        source = "default"

                    # Determine the optimiser's recommended mode from the plan
                    optimiser_mode = None
                    optimiser_mode_name = None
                    if state.current_plan:
                        slot = state.current_plan.get_current_slot()
                        if slot:
                            try:
                                opt_mode = _OM(int(slot.mode))
                                optimiser_mode = opt_mode.value
                                optimiser_mode_name = opt_mode.name
                            except (ValueError, TypeError):
                                pass

                    # Determine user manual mode if active
                    user_mode = None
                    user_mode_name = None
                    if override_active and manual_override:
                        cmd = manual_override.get_command()
                        if cmd:
                            user_mode = cmd.mode.value
                            user_mode_name = cmd.mode.name

                    # Never expose AUTO to the UI — it's an internal default
                    display_mode = state.current_mode
                    if display_mode == _OM.AUTO:
                        display_mode = _OM.SELF_USE

                    data["mode"] = {
                        "current": display_mode.value,
                        "name": display_mode.name,
                        "override_active": override_active,
                        "override_remaining_s": (
                            manual_override.remaining_seconds if manual_override else 0
                        ),
                        "source": source,
                        "optimiser_mode": optimiser_mode,
                        "optimiser_mode_name": optimiser_mode_name,
                        "user_mode": user_mode,
                        "user_mode_name": user_mode_name,
                        "auto_active": not override_active,
                    }

                # Include update status if updater is available
                updater = getattr(request.app.state, "updater", None)
                if updater:
                    data["update"] = {
                        "available": updater.update_available,
                        "latest_version": updater.latest_version,
                        "state": updater.state.state,
                    }

                # Include live device statuses if load manager is available
                load_manager = getattr(request.app.state, "load_manager", None)
                if load_manager:
                    try:
                        runtimes = load_manager.get_all_runtime_minutes()
                        device_list = []
                        for lid, ctrl in load_manager.controllers.items():
                            last_state = load_manager._last_known_state.get(lid)
                            device_list.append({
                                "name": ctrl.name,
                                "state": last_state.value.upper() if last_state else "UNKNOWN",
                                "runtime_min": round(runtimes.get(lid, 0.0)),
                            })
                        data["devices"] = device_list
                    except Exception:
                        pass

                yield f"data: {json.dumps(data)}\n\n"
            except Exception as e:
                logger.error("SSE error: %s", e)
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

            await asyncio.sleep(interval)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
