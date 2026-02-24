"""Price spike detection and event management."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from power_master.tariff.base import TariffSchedule, TariffSlot

logger = logging.getLogger(__name__)


@dataclass
class SpikeEvent:
    """Represents a detected price spike event."""

    started_at: datetime
    ended_at: datetime | None = None
    peak_price_cents: float = 0.0
    slots_affected: int = 0
    energy_discharged_wh: float = 0.0
    revenue_cents: float = 0.0
    costs_avoided_cents: float = 0.0
    active: bool = True

    @property
    def financial_impact_cents(self) -> float:
        return self.revenue_cents + self.costs_avoided_cents


@dataclass
class SpikeDetector:
    """Detects and tracks price spike events."""

    spike_threshold_cents: int = 100
    _current_spike: SpikeEvent | None = field(default=None, init=False)
    _history: list[SpikeEvent] = field(default_factory=list, init=False)

    @property
    def is_spike_active(self) -> bool:
        return self._current_spike is not None and self._current_spike.active

    @property
    def current_spike(self) -> SpikeEvent | None:
        return self._current_spike

    def evaluate(self, schedule: TariffSchedule) -> bool:
        """Check for spike in current slot. Returns True if spike state changed."""
        now = datetime.now(timezone.utc)
        current_slot = schedule.get_slot_at(now)
        if current_slot is None:
            return self._end_spike_if_active(now)

        is_spike_price = current_slot.import_price_cents >= self.spike_threshold_cents

        if is_spike_price and not self.is_spike_active:
            return self._start_spike(current_slot, now)
        elif not is_spike_price and self.is_spike_active:
            return self._end_spike_if_active(now)

        # Update peak if spike is ongoing
        if self.is_spike_active and self._current_spike:
            self._current_spike.peak_price_cents = max(
                self._current_spike.peak_price_cents,
                current_slot.import_price_cents,
            )
            self._current_spike.slots_affected += 1

        return False

    def get_upcoming_spikes(self, schedule: TariffSchedule) -> list[TariffSlot]:
        """Find upcoming slots that exceed the spike threshold."""
        now = datetime.now(timezone.utc)
        return [
            s
            for s in schedule.slots
            if s.start > now and s.import_price_cents >= self.spike_threshold_cents
        ]

    def _start_spike(self, slot: TariffSlot, now: datetime) -> bool:
        self._current_spike = SpikeEvent(
            started_at=now,
            peak_price_cents=slot.import_price_cents,
            slots_affected=1,
        )
        logger.warning(
            "Price spike detected: %.1fc/kWh (threshold: %dc)",
            slot.import_price_cents,
            self.spike_threshold_cents,
        )
        return True

    def _end_spike_if_active(self, now: datetime) -> bool:
        if self._current_spike and self._current_spike.active:
            self._current_spike.ended_at = now
            self._current_spike.active = False
            self._history.append(self._current_spike)
            logger.info(
                "Price spike ended. Duration: %s, Peak: %.1fc, Impact: %.1fc",
                now - self._current_spike.started_at,
                self._current_spike.peak_price_cents,
                self._current_spike.financial_impact_cents,
            )
            self._current_spike = None
            return True
        return False
