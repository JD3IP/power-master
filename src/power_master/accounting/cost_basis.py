"""Weighted Average Cost Basis (WACB) tracking.

Tracks the average cost per kWh stored in the battery from:
- Grid charging: cost = import rate
- PV charging: cost = feed-in rate (opportunity cost)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class WACBState:
    """Current WACB tracking state."""

    wacb_cents: float = 0.0    # Weighted average cost basis in c/kWh
    stored_wh: float = 0.0     # Current energy stored (based on SOC * capacity)
    total_charged_wh: float = 0.0
    total_cost_cents: float = 0.0


class CostBasisTracker:
    """Tracks the weighted average cost basis of energy in the battery."""

    def __init__(self, capacity_wh: int, initial_soc: float = 0.5, initial_wacb: float = 10.0) -> None:
        self._capacity_wh = capacity_wh
        self._state = WACBState(
            wacb_cents=initial_wacb,
            stored_wh=initial_soc * capacity_wh,
        )

    @property
    def state(self) -> WACBState:
        return self._state

    @property
    def wacb_cents(self) -> float:
        return self._state.wacb_cents

    @property
    def stored_value_cents(self) -> float:
        """Total value of energy currently stored in battery."""
        return (self._state.stored_wh / 1000) * self._state.wacb_cents

    def record_charge(self, energy_wh: float, rate_cents: float) -> None:
        """Record a charge event and update WACB.

        Args:
            energy_wh: Energy charged in Wh.
            rate_cents: Cost rate in c/kWh (import rate for grid, feed-in rate for PV).
        """
        if energy_wh <= 0:
            return

        energy_kwh = energy_wh / 1000
        cost = energy_kwh * rate_cents

        prev_stored_kwh = self._state.stored_wh / 1000
        prev_cost = prev_stored_kwh * self._state.wacb_cents

        new_stored_kwh = prev_stored_kwh + energy_kwh
        new_total_cost = prev_cost + cost

        if new_stored_kwh > 0:
            self._state.wacb_cents = new_total_cost / new_stored_kwh
        self._state.stored_wh = new_stored_kwh * 1000
        self._state.total_charged_wh += energy_wh
        self._state.total_cost_cents += cost

        logger.debug(
            "WACB update: charged %.0fWh at %.1fc/kWh → WACB=%.1fc/kWh stored=%.0fWh",
            energy_wh, rate_cents, self._state.wacb_cents, self._state.stored_wh,
        )

    def record_discharge(self, energy_wh: float) -> float:
        """Record a discharge event. Returns the cost basis of discharged energy.

        Args:
            energy_wh: Energy discharged in Wh.

        Returns:
            Cost basis of the discharged energy in cents.
        """
        if energy_wh <= 0:
            return 0.0

        energy_kwh = energy_wh / 1000
        cost_basis = energy_kwh * self._state.wacb_cents

        self._state.stored_wh = max(0, self._state.stored_wh - energy_wh)

        # WACB doesn't change on discharge — it represents avg cost of remaining energy

        return cost_basis

    def sync_soc(self, soc: float) -> None:
        """Sync stored Wh from actual SOC reading.

        Called periodically to correct drift between tracked and actual energy.
        """
        self._state.stored_wh = soc * self._capacity_wh
