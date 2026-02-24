"""Conditional rebuild evaluator — decides when to trigger a plan rebuild.

Six triggers:
1. periodic - Regular interval (default 1 hour)
2. tariff_change - Significant price change detected
3. forecast_delta - Solar/weather forecast changed significantly
4. storm - Storm probability exceeded threshold
5. soc_deviation - Actual SOC deviated from plan
6. price_spike - Price spike detected/ended
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from power_master.config.schema import AppConfig
from power_master.forecast.aggregator import ForecastAggregator
from power_master.optimisation.plan import OptimisationPlan

logger = logging.getLogger(__name__)


@dataclass
class RebuildResult:
    """Result of rebuild evaluation."""

    should_rebuild: bool
    trigger: str = ""
    reason: str = ""


class RebuildEvaluator:
    """Evaluates whether the current plan needs rebuilding."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._last_rebuild_time: float = 0.0
        self._last_storm_state: bool = False

    def evaluate(
        self,
        current_plan: OptimisationPlan | None,
        current_soc: float,
        aggregator: ForecastAggregator,
    ) -> RebuildResult:
        """Check all rebuild triggers and return result."""
        now = time.monotonic()

        # 1. No plan exists
        if current_plan is None:
            return RebuildResult(True, "initial", "No active plan")

        # 2. Price spike state changed
        if aggregator.spike_detector.is_spike_active:
            # Check if we haven't already rebuilt for this spike
            if current_plan.trigger_reason != "price_spike":
                return RebuildResult(True, "price_spike", "Price spike detected")

        # 3. Storm state changed
        storm_active = aggregator.state.storm_probability >= self._config.storm.probability_threshold
        if storm_active != self._last_storm_state:
            self._last_storm_state = storm_active
            state_str = "activated" if storm_active else "cleared"
            return RebuildResult(True, "storm", f"Storm {state_str}")

        # 4. SOC deviation
        current_slot = current_plan.get_current_slot()
        if current_slot:
            deviation = abs(current_soc - current_slot.expected_soc)
            if deviation > self._config.planning.soc_deviation_tolerance:
                return RebuildResult(
                    True, "soc_deviation",
                    f"SOC deviation: {deviation:.1%} (expected {current_slot.expected_soc:.1%}, actual {current_soc:.1%})",
                )

        # 5. Periodic rebuild
        elapsed = now - self._last_rebuild_time
        if elapsed >= self._config.planning.periodic_rebuild_interval_seconds:
            return RebuildResult(True, "periodic", f"Periodic ({elapsed:.0f}s since last)")

        # 6. Forecast staleness
        if aggregator.is_stale(self._config.resilience.stale_forecast_max_age_seconds):
            return RebuildResult(True, "forecast_delta", "Forecast data is stale")

        return RebuildResult(False)

    def mark_rebuilt(self) -> None:
        """Record that a rebuild just occurred."""
        self._last_rebuild_time = time.monotonic()
