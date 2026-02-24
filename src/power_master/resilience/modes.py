"""Degraded operating mode definitions."""

from __future__ import annotations

from enum import Enum


class ResilienceLevel(str, Enum):
    """System resilience levels."""

    NORMAL = "normal"              # All systems operational
    DEGRADED_FORECAST = "degraded_forecast"  # Forecast provider(s) down
    DEGRADED_TARIFF = "degraded_tariff"      # Tariff provider down
    DEGRADED_HARDWARE = "degraded_hardware"  # Inverter communication issues
    SAFE_MODE = "safe_mode"        # Multiple failures, minimal operation
    OFFLINE = "offline"            # System shutdown/unrecoverable


# Which providers are required for each level
LEVEL_REQUIREMENTS: dict[ResilienceLevel, list[str]] = {
    ResilienceLevel.NORMAL: ["inverter", "tariff", "solar_forecast"],
    ResilienceLevel.DEGRADED_FORECAST: ["inverter", "tariff"],
    ResilienceLevel.DEGRADED_TARIFF: ["inverter"],
    ResilienceLevel.DEGRADED_HARDWARE: [],
    ResilienceLevel.SAFE_MODE: [],
    ResilienceLevel.OFFLINE: [],
}
