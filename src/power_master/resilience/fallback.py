"""Safe default mode when system enters degraded state."""

from __future__ import annotations

import logging

from power_master.config.schema import AppConfig
from power_master.control.command import ControlCommand
from power_master.hardware.base import OperatingMode
from power_master.resilience.modes import ResilienceLevel

logger = logging.getLogger(__name__)


def get_fallback_command(
    level: ResilienceLevel,
    current_soc: float,
    config: AppConfig,
) -> ControlCommand:
    """Return a safe fallback command for the given resilience level.

    Fallback strategy:
    - NORMAL: No fallback needed (should not be called).
    - DEGRADED_FORECAST: Self-use with conservative SOC management.
    - DEGRADED_TARIFF: Self-use only (no arbitrage without prices).
    - DEGRADED_HARDWARE: No command (can't communicate).
    - SAFE_MODE: Self-use zero export (preserve battery).
    """
    if level == ResilienceLevel.NORMAL:
        return ControlCommand(
            mode=OperatingMode.SELF_USE,
            source="fallback",
            reason="normal_fallback",
            priority=5,
        )

    if level == ResilienceLevel.DEGRADED_FORECAST:
        # Conservative self-use — don't try to optimise without forecast
        logger.info("Fallback: degraded forecast — self-use mode")
        return ControlCommand(
            mode=OperatingMode.SELF_USE,
            source="fallback",
            reason="degraded_forecast",
            priority=3,
        )

    if level == ResilienceLevel.DEGRADED_TARIFF:
        # No prices — self-use only, no arbitrage
        logger.info("Fallback: degraded tariff — self-use only")
        return ControlCommand(
            mode=OperatingMode.SELF_USE,
            source="fallback",
            reason="degraded_tariff",
            priority=3,
        )

    if level == ResilienceLevel.SAFE_MODE:
        # Multiple failures — preserve battery, no export
        logger.warning("Fallback: safe mode — zero export, preserve battery")
        return ControlCommand(
            mode=OperatingMode.SELF_USE_ZERO_EXPORT,
            source="fallback",
            reason="safe_mode",
            priority=2,
        )

    # DEGRADED_HARDWARE or OFFLINE — can't send commands
    logger.error("Fallback: hardware degraded/offline — no command possible")
    return ControlCommand(
        mode=OperatingMode.SELF_USE,
        source="fallback",
        reason="hardware_degraded_no_dispatch",
        priority=1,
    )
