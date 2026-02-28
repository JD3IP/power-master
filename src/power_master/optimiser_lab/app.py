"""Standalone local app for optimiser backtesting/tuning."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from power_master import __version__
from power_master.config.manager import ConfigManager
from power_master.config.schema import AppConfig
from power_master.db.repository import Repository

TEMPLATES_DIR = Path(__file__).parent.parent / "dashboard" / "templates"
STATIC_DIR = Path(__file__).parent.parent / "dashboard" / "static"


def create_lab_app(
    config: AppConfig,
    repo: Repository,
    config_manager: ConfigManager | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Power Master Optimiser Lab",
        description="Local optimiser backtest/tuning sandbox",
        version=__version__,
    )
    app.state.config = config
    app.state.repo = repo
    app.state.config_manager = config_manager

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from power_master.dashboard.routes.optimiser_lab import router as optimiser_lab_router

    app.include_router(optimiser_lab_router)
    return app

