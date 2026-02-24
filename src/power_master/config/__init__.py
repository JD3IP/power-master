"""Configuration management for Power Master."""

from power_master.config.schema import AppConfig
from power_master.config.manager import ConfigManager

__all__ = ["AppConfig", "ConfigManager"]
