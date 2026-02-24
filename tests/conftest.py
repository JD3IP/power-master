"""Shared test fixtures for Power Master."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncGenerator

import aiosqlite
import pytest
import pytest_asyncio

from power_master.config.manager import ConfigManager
from power_master.config.schema import AppConfig
from power_master.db.engine import init_db
from power_master.db.repository import Repository


@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for all tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def config() -> AppConfig:
    """Provide a default test configuration."""
    return AppConfig()


@pytest.fixture
def config_manager(tmp_path: Path) -> ConfigManager:
    """Provide a config manager with test paths."""
    defaults = tmp_path / "config.defaults.yaml"
    defaults.write_text("db:\n  path: ':memory:'\n")
    user = tmp_path / "config.yaml"
    mgr = ConfigManager(defaults_path=defaults, user_path=user)
    mgr.load()
    return mgr


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Provide a fresh in-memory database for each test."""
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def repo(db: aiosqlite.Connection) -> Repository:
    """Provide a repository with a fresh database."""
    return Repository(db)
