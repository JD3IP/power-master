"""Conditional rebuild evaluator — decides when to trigger a plan rebuild.

Nine triggers:
1. initial - No active plan
2. price_spike - Price spike detected
3. storm - Storm probability changed
4. plan_expired - Current time past all plan slots
5. soc_deviation - Actual SOC deviated from plan
6. tariff_change - Import/export price changed significantly from plan assumptions
7. actuals_deviation - Live solar/load diverges from plan forecast
8. periodic - Regular interval (default 1 hour)
9. forecast_staleness - Forecast data is stale
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from power_master.config.schema import AppConfig
from power_master.forecast.aggregator import ForecastAggregator
from power_master.optimisation.plan import OptimisationPlan, PlanSlot

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
        self._last_soc_rebuild_time: float = 0.0
        self._last_actuals_rebuild_time: float = 0.0
        self._last_tariff_rebuild_time: float = 0.0
        self._last_storm_state: bool = False

    def evaluate(
        self,
        current_plan: OptimisationPlan | None,
        current_soc: float,
        aggregator: ForecastAggregator,
        manual_override_active: bool = False,
        actual_solar_w: float | None = None,
        actual_load_w: float | None = None,
    ) -> RebuildResult:
        """Check all rebuild triggers and return result.

        When manual_override_active is True, only safety-critical triggers
        (price spike, storm) are evaluated — SOC deviation and periodic
        rebuilds are suppressed because the user is intentionally overriding
        the optimizer.
        """
        now = time.monotonic()

        # 1. No plan exists
        if current_plan is None:
            return RebuildResult(True, "initial", "No active plan")

        # 2. Price spike state changed (always active, even during override)
        if aggregator.spike_detector.is_spike_active:
            # Check if we haven't already rebuilt for this spike
            if current_plan.trigger_reason != "price_spike":
                return RebuildResult(True, "price_spike", "Price spike detected")

        # 3. Storm state changed (always active, even during override)
        storm_active = aggregator.state.storm_probability >= self._config.storm.probability_threshold
        if storm_active != self._last_storm_state:
            self._last_storm_state = storm_active
            state_str = "activated" if storm_active else "cleared"
            return RebuildResult(True, "storm", f"Storm {state_str}")

        # During manual override, skip SOC deviation, periodic, actuals
        # deviation, and staleness triggers — they are not useful when the
        # user is intentionally controlling the system.
        if manual_override_active:
            return RebuildResult(False)

        # 4. Plan expired — current time is past all plan slots.
        #    This means the plan is completely stale and must be rebuilt.
        current_slot = current_plan.get_current_slot()
        if current_slot is None and current_plan.slots:
            last_slot_end = current_plan.slots[-1].end
            from datetime import datetime, timezone
            if datetime.now(timezone.utc) >= last_slot_end:
                return RebuildResult(
                    True, "plan_expired",
                    f"Plan expired (horizon ended {last_slot_end.isoformat()})",
                )

        # 5. SOC deviation — interpolate expected SOC within current slot
        if current_slot:
            expected_now = self._interpolated_expected_soc(current_plan, current_slot)
            deviation = abs(current_soc - expected_now)
            logger.info(
                "SOC check: actual=%.1f%% expected=%.1f%% deviation=%.1f%% tolerance=%.1f%%",
                current_soc * 100, expected_now * 100, deviation * 100,
                self._config.planning.soc_deviation_tolerance * 100,
            )
            if deviation > self._config.planning.soc_deviation_tolerance:
                cooldown = self._config.planning.soc_deviation_cooldown_seconds
                if (now - self._last_soc_rebuild_time) >= cooldown:
                    return RebuildResult(
                        True, "soc_deviation",
                        f"SOC deviation: {deviation:.1%} "
                        f"(expected {expected_now:.1%}, actual {current_soc:.1%})",
                    )

        # 6. Tariff change — current import price differs significantly
        #    from what the plan assumed for this slot.
        if current_slot:
            tariff = aggregator.state.tariff
            if tariff:
                live_import = tariff.get_current_import_price()
                if live_import is not None and current_slot.import_rate_cents > 0:
                    tariff_dev = abs(live_import - current_slot.import_rate_cents) / max(current_slot.import_rate_cents, 1)
                    threshold_pct = self._config.planning.forecast_delta_threshold_pct / 100.0
                    if tariff_dev > threshold_pct:
                        cooldown = self._config.planning.soc_deviation_cooldown_seconds
                        if (now - self._last_tariff_rebuild_time) >= cooldown:
                            return RebuildResult(
                                True, "tariff_change",
                                f"Import price changed: plan={current_slot.import_rate_cents:.1f}c "
                                f"live={live_import:.1f}c ({tariff_dev:.0%} deviation)",
                            )

        # 7. Actuals vs forecast deviation — trigger rebuild when real
        # solar or load diverges significantly from what the plan assumed.
        if current_slot and (actual_solar_w is not None or actual_load_w is not None):
            threshold_pct = self._config.planning.forecast_delta_threshold_pct / 100.0
            cooldown = self._config.planning.soc_deviation_cooldown_seconds

            if actual_solar_w is not None and current_slot.solar_forecast_w > 0:
                solar_dev = abs(actual_solar_w - current_slot.solar_forecast_w) / max(current_slot.solar_forecast_w, 1)
                if solar_dev > threshold_pct and (now - self._last_actuals_rebuild_time) >= cooldown:
                    return RebuildResult(
                        True, "actuals_deviation",
                        f"Solar actuals deviation: {solar_dev:.0%} "
                        f"(forecast {current_slot.solar_forecast_w:.0f}W, actual {actual_solar_w:.0f}W)",
                    )

            if actual_load_w is not None and current_slot.load_forecast_w > 0:
                load_dev = abs(actual_load_w - current_slot.load_forecast_w) / max(current_slot.load_forecast_w, 1)
                if load_dev > threshold_pct and (now - self._last_actuals_rebuild_time) >= cooldown:
                    return RebuildResult(
                        True, "actuals_deviation",
                        f"Load actuals deviation: {load_dev:.0%} "
                        f"(forecast {current_slot.load_forecast_w:.0f}W, actual {actual_load_w:.0f}W)",
                    )

        # 8. Periodic rebuild
        elapsed = now - self._last_rebuild_time
        if elapsed >= self._config.planning.periodic_rebuild_interval_seconds:
            return RebuildResult(True, "periodic", f"Periodic ({elapsed:.0f}s since last)")

        # 9. Forecast staleness
        if aggregator.is_stale(self._config.resilience.stale_forecast_max_age_seconds):
            return RebuildResult(True, "forecast_delta", "Forecast data is stale")

        return RebuildResult(False)

    def _interpolated_expected_soc(
        self, plan: OptimisationPlan, current_slot: PlanSlot,
    ) -> float:
        """Linearly interpolate expected SOC at current time within the slot.

        Uses previous slot's expected_soc as start, current slot's as end.
        For the first slot, uses plan's initial SOC (from metrics).
        """
        now = datetime.now(timezone.utc)
        slot_duration = (current_slot.end - current_slot.start).total_seconds()
        elapsed = (now - current_slot.start).total_seconds()
        progress = max(0.0, min(1.0, elapsed / slot_duration)) if slot_duration > 0 else 1.0

        # SOC at start of current slot = previous slot's expected_soc or initial
        if current_slot.index > 0 and current_slot.index <= len(plan.slots):
            prev_slot = plan.slots[current_slot.index - 1]
            soc_start = prev_slot.expected_soc
        else:
            # First slot — use the solver's initial SOC from plan metrics
            soc_start = plan.metrics.get("current_soc", current_slot.expected_soc)

        soc_end = current_slot.expected_soc
        return soc_start + (soc_end - soc_start) * progress

    def mark_rebuilt(self, trigger: str = "") -> None:
        """Record that a rebuild just occurred."""
        self._last_rebuild_time = time.monotonic()
        if trigger == "soc_deviation":
            self._last_soc_rebuild_time = time.monotonic()
        if trigger == "actuals_deviation":
            self._last_actuals_rebuild_time = time.monotonic()
        if trigger == "tariff_change":
            self._last_tariff_rebuild_time = time.monotonic()
