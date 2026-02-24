"""Forecast.Solar solar forecast provider.

Docs: https://doc.forecast.solar/api:estimate
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx

from power_master.config.schema import SolarProviderConfig
from power_master.forecast.base import SolarForecast, SolarForecastSlot, SolarProvider
from power_master.timezone_utils import resolve_timezone

logger = logging.getLogger(__name__)

BASE_URL = "https://api.forecast.solar"


class ForecastSolarProvider(SolarProvider):
    """Forecast.Solar API provider."""

    def __init__(self, config: SolarProviderConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=30.0)

    def _build_path(self) -> str:
        lat = self._config.latitude
        lon = self._config.longitude
        dec = self._config.declination
        az = self._config.azimuth if self._config.azimuth is not None else 0.0
        kwp = self._config.kwp

        return f"/estimate/{lat}/{lon}/{dec}/{az}/{kwp}"

    async def fetch_forecast(self) -> SolarForecast:
        """Fetch solar forecast from Forecast.Solar estimate endpoint."""
        resp = await self._client.get(self._build_path())
        resp.raise_for_status()
        data = resp.json()

        watts: dict[str, float] = (
            data.get("result", {}).get("watts", {}) if isinstance(data, dict) else {}
        )
        message = data.get("message", {}) if isinstance(data, dict) else {}
        info = message.get("info", {}) if isinstance(message, dict) else {}
        tz_name = (
            info.get("timezone")
            if isinstance(info, dict) and info.get("timezone")
            else self._config.timezone
        )
        local_tz = resolve_timezone(tz_name)

        if not watts:
            logger.warning("Forecast.Solar returned no watts data")
            return SolarForecast(
                slots=[],
                fetched_at=datetime.now(UTC),
                provider="forecast_solar",
            )

        entries: list[tuple[datetime, float]] = []
        for key, value in watts.items():
            try:
                local_dt = datetime.strptime(key, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=local_tz
                )
                entries.append((local_dt.astimezone(UTC), float(value)))
            except Exception:
                logger.debug("Skipping unparsable Forecast.Solar timestamp: %r", key)

        entries.sort(key=lambda x: x[0])
        if not entries:
            return SolarForecast(
                slots=[],
                fetched_at=datetime.now(UTC),
                provider="forecast_solar",
            )

        default_period = timedelta(hours=1)
        if len(entries) >= 2:
            default_period = max(timedelta(minutes=5), entries[1][0] - entries[0][0])

        slots: list[SolarForecastSlot] = []
        prev_end: datetime | None = None
        for end, watts_value in entries:
            start = prev_end if prev_end is not None else (end - default_period)
            prev_end = end
            slots.append(
                SolarForecastSlot(
                    start=start,
                    end=end,
                    pv_estimate_w=watts_value,
                    pv_estimate10_w=watts_value,
                    pv_estimate90_w=watts_value,
                )
            )

        logger.info("Forecast.Solar forecast fetched: %d slots", len(slots))
        return SolarForecast(
            slots=slots,
            fetched_at=datetime.now(UTC),
            provider="forecast_solar",
        )

    async def is_healthy(self) -> bool:
        try:
            resp = await self._client.head("/")
            return resp.status_code < 500
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()
