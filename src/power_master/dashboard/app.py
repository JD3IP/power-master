"""FastAPI application factory for the Power Master dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from power_master.config.manager import ConfigManager
from power_master.config.schema import AppConfig
from power_master.dashboard.auth import AuthMiddleware, auth_router
from power_master.db.repository import Repository

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    config: AppConfig,
    repo: Repository,
    config_manager: ConfigManager | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Power Master",
        description="Solar optimisation and control system",
        version="0.1.0",
    )

    # Store config and repo in app state for access in routes
    app.state.config = config
    app.state.repo = repo
    app.state.config_manager = config_manager

    auth_enabled = bool(config.dashboard.auth.password_hash)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["auth_enabled"] = auth_enabled
    app.state.templates = templates

    # Mount static files
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Register auth routes (login/logout)
    app.include_router(auth_router)

    # Register routes
    from power_master.dashboard.routes.accounting import router as accounting_router
    from power_master.dashboard.routes.api import router as api_router
    from power_master.dashboard.routes.graphs import router as graphs_router
    from power_master.dashboard.routes.overview import router as overview_router
    from power_master.dashboard.routes.plans import router as plans_router
    from power_master.dashboard.routes.settings import router as settings_router
    from power_master.dashboard.routes.sse import router as sse_router

    app.include_router(overview_router)
    app.include_router(plans_router)
    app.include_router(accounting_router)
    app.include_router(graphs_router)
    app.include_router(settings_router)
    app.include_router(api_router, prefix="/api")
    app.include_router(sse_router, prefix="/api")

    # Add auth middleware (only if a password is configured)
    if auth_enabled:
        app.add_middleware(AuthMiddleware, auth_config=config.dashboard.auth)

    return app
