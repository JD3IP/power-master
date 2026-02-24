"""Tariff schedule utilities."""

from __future__ import annotations

from datetime import datetime

from power_master.tariff.base import TariffSchedule, TariffSlot


def classify_slot(slot: TariffSlot, spike_threshold_cents: int = 100) -> str:
    """Classify a tariff slot as peak/off-peak/spike/negative.

    Returns a descriptor string used for logging and UI display.
    """
    if slot.import_price_cents >= spike_threshold_cents:
        return "spike"
    if slot.import_price_cents < 0:
        return "negative"
    if slot.import_price_cents < 10:
        return "off-peak"
    if slot.import_price_cents < 30:
        return "shoulder"
    return "peak"


def get_cheapest_slots(
    schedule: TariffSchedule,
    after: datetime | None = None,
    count: int = 6,
) -> list[TariffSlot]:
    """Return the cheapest upcoming import slots sorted by price.

    Useful for identifying optimal grid-charge windows.
    """
    slots = schedule.slots
    if after:
        slots = [s for s in slots if s.start >= after]
    return sorted(slots, key=lambda s: s.import_price_cents)[:count]


def get_most_profitable_export_slots(
    schedule: TariffSchedule,
    after: datetime | None = None,
    count: int = 6,
) -> list[TariffSlot]:
    """Return the highest-paying export slots sorted by price descending."""
    slots = schedule.slots
    if after:
        slots = [s for s in slots if s.start >= after]
    return sorted(slots, key=lambda s: s.export_price_cents, reverse=True)[:count]
