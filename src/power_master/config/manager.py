"""Configuration loading, saving, versioning, and validation."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from power_master.config.schema import AppConfig

logger = logging.getLogger(__name__)


class ConfigManager:
    """Loads config from YAML files, validates, and manages versioning in SQLite."""

    def __init__(
        self,
        defaults_path: Path | None = None,
        user_path: Path | None = None,
    ) -> None:
        self._defaults_path = defaults_path or Path("config.defaults.yaml")
        self._user_path = user_path or Path("config.yaml")
        self._config: AppConfig | None = None
        self._raw: dict[str, Any] = {}

    @property
    def config(self) -> AppConfig:
        if self._config is None:
            raise RuntimeError("Config not loaded. Call load() first.")
        return self._config

    def load(self) -> AppConfig:
        """Load configuration from defaults + user overrides."""
        defaults = self._load_yaml(self._defaults_path)
        overrides = self._load_yaml(self._user_path) if self._user_path.exists() else {}
        merged = self._deep_merge(defaults, overrides)
        self._raw = merged
        self._config = AppConfig.model_validate(merged)
        logger.info("Configuration loaded successfully")
        return self._config

    def get_raw(self) -> dict[str, Any]:
        return dict(self._raw)

    def to_json(self) -> str:
        return self.config.model_dump_json(indent=2)

    def save_user_config(self, updates: dict[str, Any]) -> AppConfig:
        """Apply updates to user config file and reload."""
        current = self._load_yaml(self._user_path) if self._user_path.exists() else {}
        merged = self._deep_merge(current, updates)
        with open(self._user_path, "w") as f:
            yaml.dump(merged, f, default_flow_style=False, sort_keys=False)
        return self.load()

    async def save_version(self, db: Any, changed_keys: list[str] | None = None) -> int:
        """Save current config as a versioned snapshot in the database."""
        config_json = self.to_json()
        now = datetime.now(timezone.utc).isoformat()
        changed = json.dumps(changed_keys) if changed_keys else None

        async with db.execute(
            """INSERT INTO config_versions (config_json, changed_keys, created_at, source)
               VALUES (?, ?, ?, 'user')""",
            (config_json, changed, now),
        ) as cursor:
            version_id = cursor.lastrowid
        await db.commit()
        logger.info("Config version %d saved", version_id)
        return version_id  # type: ignore[return-value]

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with open(path) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """Recursively merge override into base, returning a new dict."""
        result = dict(base)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = ConfigManager._deep_merge(result[key], value)
            else:
                result[key] = value
        return result
