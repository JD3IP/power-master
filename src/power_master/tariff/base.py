"""Abstract base classes for tariff providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class TariffSlot:
    """Single time-slot of tariff data."""

    start: datetime
    end: datetime
    import_price_cents: float  # c/kWh (incl. all fees from Amber)
    export_price_cents: float  # c/kWh (feed-in rate)
    channel_type: str = "general"  # general, controlled_load, feed_in
    descriptor: str = ""  # e.g. "peak", "off-peak", "spike"

    @property
    def is_spike(self) -> bool:
        return self.descriptor == "spike"


@dataclass
class TariffSchedule:
    """Complete tariff schedule (current + forecast)."""

    slots: list[TariffSlot] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    provider: str = ""

    def get_slot_at(self, dt: datetime) -> TariffSlot | None:
        """Find the tariff slot covering the given time."""
        # Normalize query time to UTC for consistent comparison
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        else:
            dt = dt.replace(tzinfo=timezone.utc)

        for slot in self.slots:
            start = slot.start.astimezone(timezone.utc) if slot.start.tzinfo else slot.start.replace(tzinfo=timezone.utc)
            end = slot.end.astimezone(timezone.utc) if slot.end.tzinfo else slot.end.replace(tzinfo=timezone.utc)
            if start <= dt < end:
                return slot
        return None

    def get_current_import_price(self) -> float | None:
        """Get the current import price in cents/kWh."""
        slot = self.get_slot_at(datetime.now(timezone.utc))
        return slot.import_price_cents if slot else None

    def get_current_export_price(self) -> float | None:
        """Get the current export price in cents/kWh."""
        slot = self.get_slot_at(datetime.now(timezone.utc))
        return slot.export_price_cents if slot else None


class TariffProvider(ABC):
    """Abstract base for tariff/pricing providers."""

    @abstractmethod
    async def fetch_prices(self) -> TariffSchedule:
        """Fetch current and forecast prices."""
        ...

    @abstractmethod
    async def fetch_historical(
        self, start: datetime, end: datetime
    ) -> TariffSchedule:
        """Fetch historical price data for backfill."""
        ...

    @abstractmethod
    async def is_healthy(self) -> bool:
        ...
