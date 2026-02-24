"""Continuous data collection — stores telemetry and provider data as historical records."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from power_master.db.repository import Repository
from power_master.forecast.base import WeatherForecast
from power_master.hardware.telemetry import Telemetry
from power_master.tariff.base import TariffSchedule

logger = logging.getLogger(__name__)


class HistoryCollector:
    """Collects telemetry, weather, and price data into historical_data table.

    Designed to run every 30 minutes (matching slot duration), aggregating
    instantaneous readings into 30-min averages.
    """

    def __init__(self, repo: Repository) -> None:
        self._repo = repo
        self._telemetry_buffer: list[Telemetry] = []
        self._last_flush: datetime | None = None

    def record_telemetry(self, telemetry: Telemetry) -> None:
        """Buffer a telemetry reading for later aggregation."""
        self._telemetry_buffer.append(telemetry)

    async def flush_telemetry(self) -> None:
        """Aggregate buffered telemetry and store as historical 30-min records."""
        if not self._telemetry_buffer:
            return

        now = datetime.now(timezone.utc).isoformat()
        n = len(self._telemetry_buffer)

        avg_load = sum(t.load_power_w for t in self._telemetry_buffer) / n
        avg_solar = sum(t.solar_power_w for t in self._telemetry_buffer) / n
        avg_grid = sum(t.grid_power_w for t in self._telemetry_buffer) / n
        avg_battery = sum(t.battery_power_w for t in self._telemetry_buffer) / n
        avg_soc = sum(t.soc for t in self._telemetry_buffer) / n

        await self._repo.store_historical("load_w", avg_load, "telemetry", now)
        await self._repo.store_historical("solar_w", avg_solar, "telemetry", now)
        await self._repo.store_historical("grid_w", avg_grid, "telemetry", now)
        await self._repo.store_historical("battery_w", avg_battery, "telemetry", now)
        await self._repo.store_historical("soc", avg_soc, "telemetry", now)

        logger.debug(
            "Flushed %d telemetry readings: load=%.0fW solar=%.0fW grid=%.0fW",
            n, avg_load, avg_solar, avg_grid,
        )
        self._telemetry_buffer.clear()
        self._last_flush = datetime.now(timezone.utc)

    async def record_weather(self, weather: WeatherForecast) -> None:
        """Store current weather conditions as historical records."""
        if not weather.slots:
            return
        # Store the most recent/current slot
        slot = weather.slots[0]
        now = slot.time.isoformat()
        await self._repo.store_historical(
            "temperature_c", slot.temperature_c, "openmeteo", now, "hourly"
        )
        await self._repo.store_historical(
            "cloud_cover_pct", slot.cloud_cover_pct, "openmeteo", now, "hourly"
        )

    async def record_price(self, schedule: TariffSchedule) -> None:
        """Store all past and current tariff slots as historical price records.

        Stores every slot from the schedule whose start time <= now,
        enabling full historic price coverage from Amber's `previous` param.
        Future-only forecast slots are skipped (not yet historical).
        Deduplication handled by unique index + INSERT OR REPLACE.
        """
        if not schedule.slots:
            return

        now_dt = datetime.now(timezone.utc)
        count = 0
        for slot in schedule.slots:
            if slot.start > now_dt:
                continue
            ts = slot.start.isoformat()
            await self._repo.store_historical(
                "import_price_cents", slot.import_price_cents, "amber", ts
            )
            await self._repo.store_historical(
                "export_price_cents", slot.export_price_cents, "amber", ts
            )
            count += 1

        if count:
            logger.debug("Stored %d price slots to historical_data", count)
