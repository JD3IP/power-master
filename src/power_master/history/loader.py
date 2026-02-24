"""Initial data backfill from provider APIs.

On first run, populates historical data from:
- Amber Electric: up to 12 months of price history
- Open-Meteo: up to 2 years of weather history
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from power_master.db.repository import Repository
from power_master.forecast.base import WeatherProvider
from power_master.tariff.base import TariffProvider

logger = logging.getLogger(__name__)


class HistoryLoader:
    """Backfills historical data from provider APIs during initial setup."""

    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    async def needs_backfill(self, data_type: str, min_days: int = 7) -> bool:
        """Check if a data type needs backfill (less than min_days of data)."""
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=min_days)).isoformat()
        records = await self._repo.get_historical(data_type, start, now.isoformat())
        return len(records) < min_days * 2  # At least 2 records per day

    async def backfill_prices(
        self,
        provider: TariffProvider,
        months: int = 12,
    ) -> int:
        """Backfill price history from tariff provider."""
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=months * 30)

        logger.info("Backfilling %d months of price history...", months)
        schedule = await provider.fetch_historical(start, now)

        count = 0
        for slot in schedule.slots:
            ts = slot.start.isoformat()
            await self._repo.store_historical(
                "import_price_cents", slot.import_price_cents, "amber_backfill", ts
            )
            await self._repo.store_historical(
                "export_price_cents", slot.export_price_cents, "amber_backfill", ts
            )
            count += 1

        logger.info("Backfilled %d price slots", count)
        return count

    async def backfill_weather(
        self,
        provider: WeatherProvider,
        years: int = 2,
    ) -> int:
        """Backfill weather history from weather provider.

        Open-Meteo historical API allows up to 2 years of data.
        Fetches in 90-day chunks to avoid API limits.
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=years * 365)

        logger.info("Backfilling %d years of weather history...", years)
        count = 0

        # Fetch in 90-day chunks
        chunk_start = start
        while chunk_start < now:
            chunk_end = min(chunk_start + timedelta(days=90), now)

            try:
                forecast = await provider.fetch_historical(chunk_start, chunk_end)
                for slot in forecast.slots:
                    ts = slot.time.isoformat()
                    await self._repo.store_historical(
                        "temperature_c", slot.temperature_c,
                        "openmeteo_backfill", ts, "hourly",
                    )
                    await self._repo.store_historical(
                        "cloud_cover_pct", slot.cloud_cover_pct,
                        "openmeteo_backfill", ts, "hourly",
                    )
                    count += 1
            except Exception as e:
                logger.error(
                    "Weather backfill chunk failed (%s to %s): %s",
                    chunk_start.date(), chunk_end.date(), e,
                )

            chunk_start = chunk_end

        logger.info("Backfilled %d weather records", count)
        return count

    async def backfill_all(
        self,
        tariff_provider: TariffProvider | None = None,
        weather_provider: WeatherProvider | None = None,
    ) -> dict[str, int]:
        """Run all backfill operations that are needed."""
        results: dict[str, int] = {}

        if tariff_provider and await self.needs_backfill("import_price_cents"):
            results["prices"] = await self.backfill_prices(tariff_provider)

        if weather_provider and await self.needs_backfill("temperature_c"):
            results["weather"] = await self.backfill_weather(weather_provider)

        if not results:
            logger.info("No backfill needed — sufficient historical data exists")

        return results
