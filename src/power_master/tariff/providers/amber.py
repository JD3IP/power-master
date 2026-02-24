"""Amber Electric tariff provider.

API docs: https://app.amber.com.au/developers/documentation/
Rate limit: 50 calls per 5 minutes. Bearer token auth.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from power_master.config.schema import TariffProviderConfig
from power_master.tariff.base import TariffProvider, TariffSchedule, TariffSlot

logger = logging.getLogger(__name__)

BASE_URL = "https://api.amber.com.au/v1"


class AmberProvider(TariffProvider):
    """Amber Electric API tariff provider."""

    def __init__(self, config: TariffProviderConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=30.0,
        )

    async def fetch_prices(self) -> TariffSchedule:
        """Fetch current and forecast prices from Amber.

        Returns both current and forecast prices for the next ~48 hours.
        """
        site_id = self._config.site_id

        # Fetch current + 48h forecast (96 x 30-min intervals)
        resp = await self._client.get(
            f"/sites/{site_id}/prices/current",
            params={"resolution": 30, "next": 144, "previous": 144},
        )
        resp.raise_for_status()
        data = resp.json()

        general_count = sum(1 for e in data if e.get("channelType", "general") == "general")
        feedin_count = sum(1 for e in data if e.get("channelType") == "feedIn")
        logger.info(
            "Amber API returned %d entries (%d general, %d feedIn)",
            len(data), general_count, feedin_count,
        )
        slots = self._parse_prices(data)
        logger.info("Amber prices fetched: %d slots", len(slots))

        return TariffSchedule(
            slots=slots,
            fetched_at=datetime.now(timezone.utc),
            provider="amber",
        )

    async def fetch_historical(
        self, start: datetime, end: datetime
    ) -> TariffSchedule:
        """Fetch historical prices from Amber for backfill.

        Amber supports up to 12 months of historical data.
        """
        site_id = self._config.site_id

        # Amber API accepts date range
        resp = await self._client.get(
            f"/sites/{site_id}/prices",
            params={
                "startDate": start.strftime("%Y-%m-%d"),
                "endDate": end.strftime("%Y-%m-%d"),
                "resolution": 30,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        slots = self._parse_prices(data)
        logger.info(
            "Amber historical fetched: %d slots (%s to %s)",
            len(slots),
            start.date(),
            end.date(),
        )

        return TariffSchedule(
            slots=slots,
            fetched_at=datetime.now(timezone.utc),
            provider="amber",
        )

    async def get_site_id(self) -> str | None:
        """Auto-discover the site ID from Amber account."""
        resp = await self._client.get("/sites")
        resp.raise_for_status()
        sites = resp.json()
        if sites:
            return sites[0]["id"]
        return None

    async def is_healthy(self) -> bool:
        try:
            resp = await self._client.get("/sites")
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _parse_prices(data: list[dict]) -> list[TariffSlot]:
        """Parse Amber API price response into TariffSlot list.

        Amber returns separate entries for GENERAL (import) and FEED_IN (export).
        We merge them into unified TariffSlot entries keyed by time.
        """
        # Group by period start time
        import_by_time: dict[str, dict] = {}
        export_by_time: dict[str, dict] = {}

        for entry in data:
            channel = entry.get("channelType", "general")
            start_str = entry.get("startTime", entry.get("nemTime", ""))

            if channel == "general":
                import_by_time[start_str] = entry
            elif channel == "feedIn":
                export_by_time[start_str] = entry

        slots = []
        for start_str, imp in sorted(import_by_time.items()):
            try:
                start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                # Normalize to UTC for consistent comparisons with plan slots
                if start.tzinfo is not None:
                    start = start.astimezone(timezone.utc)
                else:
                    start = start.replace(tzinfo=timezone.utc)
                    logger.warning("Amber returned naive datetime %s — assuming UTC", start_str)
            except (ValueError, AttributeError) as e:
                logger.warning("Failed to parse Amber startTime %r: %s", start_str, e)
                continue

            end = start + timedelta(minutes=30)

            # Amber prices are in c/kWh (including all fees)
            import_price = imp.get("perKwh", 0.0)
            # Descriptor from Amber
            descriptor = imp.get("descriptor", "").lower()

            # Match export price for same period.
            # Amber feedIn sign can be inverted depending on plan metadata.
            # Normalise to "positive cents = revenue for export".
            exp = export_by_time.get(start_str, {})
            raw_export_price = exp.get("perKwh", 0.0)
            export_price = abs(raw_export_price)

            slots.append(
                TariffSlot(
                    start=start,
                    end=end,
                    import_price_cents=import_price,
                    export_price_cents=export_price,
                    channel_type="general",
                    descriptor=descriptor,
                )
            )

        if slots:
            logger.debug(
                "Tariff time range: %s to %s (%d slots)",
                slots[0].start.isoformat(), slots[-1].end.isoformat(), len(slots),
            )
            # Detect coverage gaps
            gaps = []
            for i in range(len(slots) - 1):
                if slots[i + 1].start > slots[i].end + timedelta(minutes=1):
                    gaps.append((slots[i].end.isoformat(), slots[i + 1].start.isoformat()))
            if gaps:
                logger.warning(
                    "Amber price gaps: %d gaps (first: %s to %s)",
                    len(gaps), gaps[0][0], gaps[0][1],
                )

        return slots
