"""FastAPI application factory for the Power Master dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
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

    @app.middleware("http")
    async def disable_browser_cache(request: Request, call_next):
        response = await call_next(request)
        if request.method in {"GET", "HEAD"}:
            # Force fresh fetches so UI updates propagate across all browsers.
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    # Store config and repo in app state for access in routes
    app.state.config = config
    app.state.repo = repo
    app.state.config_manager = config_manager

    auth_enabled = bool(config.dashboard.auth.users)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["auth_enabled"] = auth_enabled
    app.state.templates = templates

    # Middleware to inject user context into templates and request state
    @app.middleware("http")
    async def inject_user_context(request: Request, call_next):
        username = ""
        user_role = "viewer"
        if auth_enabled:
            from power_master.dashboard.auth import get_session
            session = get_session(request)
            if session:
                username = session.get("username", "")
                user_role = session.get("role", "viewer")
        request.state.username = username
        request.state.user_role = user_role
        # Make user info available in all templates
        templates.env.globals["current_username"] = username
        templates.env.globals["user_role"] = user_role
        return await call_next(request)

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

    # Add auth middleware (only if users are configured)
    if auth_enabled:
        app.add_middleware(AuthMiddleware, auth_config=config.dashboard.auth)

    return app
