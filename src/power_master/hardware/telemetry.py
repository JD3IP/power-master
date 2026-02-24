"""Telemetry data model for inverter readings."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Telemetry:
    """Snapshot of inverter telemetry data."""

    soc: float  # 0.0 to 1.0
    battery_power_w: int  # Positive = charging, negative = discharging
    solar_power_w: int  # PV generation watts
    grid_power_w: int  # Positive = importing, negative = exporting
    load_power_w: int  # Total load consumption watts
    battery_voltage: float | None = None
    battery_temp_c: float | None = None
    inverter_mode: str | None = None
    grid_available: bool = True
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_data: dict | None = None

    @property
    def soc_pct(self) -> float:
        """SOC as percentage (0-100)."""
        return self.soc * 100

    @property
    def is_charging(self) -> bool:
        return self.battery_power_w > 0

    @property
    def is_discharging(self) -> bool:
        return self.battery_power_w < 0

    @property
    def is_exporting(self) -> bool:
        return self.grid_power_w < 0

    @property
    def is_importing(self) -> bool:
        return self.grid_power_w > 0
