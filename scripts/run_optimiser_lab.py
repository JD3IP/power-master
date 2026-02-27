"""Run standalone optimiser lab server locally."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import uvicorn

from power_master.config.manager import ConfigManager
from power_master.db.engine import init_db
from power_master.db.repository import Repository
from power_master.optimiser_lab.app import create_lab_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--defaults", default="config.defaults.yaml")
    parser.add_argument("--db-path", default="")
    return parser.parse_args()


async def _build_app(args: argparse.Namespace):
    manager = ConfigManager(defaults_path=Path(args.defaults), user_path=Path(args.config))
    config = manager.load()
    if args.db_path:
        config.db.path = args.db_path
    db = await init_db(config.db.path)
    repo = Repository(db)
    app = create_lab_app(config=config, repo=repo, config_manager=manager)

    @app.on_event("shutdown")
    async def _close_db() -> None:
        await db.close()

    return app


def main() -> None:
    args = parse_args()
    app = asyncio.run(_build_app(args))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
