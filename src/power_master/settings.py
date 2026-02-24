"""Application settings loader."""

from __future__ import annotations

from pathlib import Path

from power_master.config.manager import ConfigManager
from power_master.config.schema import AppConfig

_config_manager: ConfigManager | None = None


def load_settings(
    defaults_path: Path | None = None,
    user_path: Path | None = None,
) -> AppConfig:
    """Load and return the application configuration."""
    global _config_manager
    _config_manager = ConfigManager(
        defaults_path=defaults_path,
        user_path=user_path,
    )
    return _config_manager.load()


def get_config_manager() -> ConfigManager:
    """Get the active config manager instance."""
    if _config_manager is None:
        raise RuntimeError("Settings not loaded. Call load_settings() first.")
    return _config_manager
