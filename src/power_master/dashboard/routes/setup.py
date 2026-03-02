"""Initial setup wizard routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request) -> HTMLResponse:
    """Render the initial setup wizard."""
    templates = request.app.state.templates
    config = request.app.state.config
    return templates.TemplateResponse(
        "setup.html",
        {"request": request, "config": config},
    )


@router.post("/api/setup/save")
async def save_setup(request: Request) -> JSONResponse:
    """Save initial setup configuration."""
    config_manager = getattr(request.app.state, "config_manager", None)
    if config_manager is None:
        return JSONResponse({"error": "Config manager not available"})

    body = await request.json()

    # Handle password hashing for the admin user
    auth_users = (body.get("dashboard") or {}).get("auth", {}).get("users", [])
    if auth_users:
        from power_master.dashboard.auth import hash_password
        for user in auth_users:
            if "password_hash" in user and not user["password_hash"].startswith("$"):
                user["password_hash"] = hash_password(user["password_hash"])

    # Use Application.reload_config() for hot-reload if available
    application = getattr(request.app.state, "application", None)
    if application is not None:
        try:
            await application.reload_config(body, request.app)
        except Exception as e:
            logger.exception("Setup save failed")
            return JSONResponse({"error": str(e)})
    else:
        try:
            new_config = config_manager.save_user_config(body)
            request.app.state.config = new_config
        except Exception as e:
            logger.exception("Setup save failed")
            return JSONResponse({"error": str(e)})

    logger.info("Initial setup completed")
    return JSONResponse({"ok": True})
