"""Storm reserve SOC calculation."""

from __future__ import annotations

import logging

from power_master.config.schema import StormConfig

logger = logging.getLogger(__name__)


def calculate_reserve_soc(
    storm_probability: float,
    config: StormConfig,
    base_soc_min: float = 0.10,
) -> float:
    """Calculate the target reserve SOC based on storm probability.

    When storm probability exceeds the threshold, returns the configured
    reserve target. Below threshold, returns 0 (no reserve).

    A gradual ramp could be added but for simplicity we use a step function.

    Args:
        storm_probability: 0.0 to 1.0 probability of storm.
        config: Storm configuration.
        base_soc_min: Minimum SOC (normal operation).

    Returns:
        Target reserve SOC (0.0 if no reserve needed).
    """
    if not config.enabled:
        return 0.0

    if storm_probability >= config.probability_threshold:
        logger.info(
            "Storm reserve active: probability=%.0f%% threshold=%.0f%% target_soc=%.0f%%",
            storm_probability * 100,
            config.probability_threshold * 100,
            config.reserve_soc_target * 100,
        )
        return config.reserve_soc_target

    return 0.0


def estimate_hours_at_reserve(
    reserve_soc: float,
    current_soc: float,
    avg_load_w: float,
    capacity_wh: int,
) -> float:
    """Estimate how many hours the battery can sustain load from reserve.

    Args:
        reserve_soc: The reserve target SOC.
        current_soc: Current battery SOC.
        avg_load_w: Expected average load in watts.
        capacity_wh: Battery capacity in watt-hours.

    Returns:
        Estimated hours of autonomy. 0 if below reserve or no load.
    """
    if avg_load_w <= 0 or current_soc <= 0:
        return 0.0

    usable_wh = current_soc * capacity_wh
    return usable_wh / avg_load_w
