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

        result = data.get("result", {}) if isinstance(data, dict) else {}
        watts: dict[str, float] = (
            result.get("watts", {}) if isinstance(result, dict) else {}
        )
        message = data.get("message", {}) if isinstance(data, dict) else {}
        info = message.get("info", {}) if isinstance(message, dict) else {}
        tz_name = (
            info.get("timezone")
            if isinstance(info, dict) and info.get("timezone")
            else self._config.timezone
        )
        local_tz = resolve_timezone(tz_name)

        # Fallback: if watts is empty, try watt_hours_period (energy per
        # interval) and convert to average power.
        if not watts and isinstance(result, dict):
            whp = result.get("watt_hours_period", {})
            if whp and isinstance(whp, dict):
                logger.info(
                    "Forecast.Solar: 'watts' empty, falling back to "
                    "'watt_hours_period' (%d entries)", len(whp),
                )
                sorted_keys = sorted(whp.keys())
                if len(sorted_keys) >= 2:
                    t0 = datetime.strptime(sorted_keys[0], "%Y-%m-%d %H:%M:%S")
                    t1 = datetime.strptime(sorted_keys[1], "%Y-%m-%d %H:%M:%S")
                    period_h = max((t1 - t0).total_seconds() / 3600.0, 0.25)
                else:
                    period_h = 1.0
                watts = {k: float(v) / period_h for k, v in whp.items()}

        # Second fallback: cumulative watt_hours (take derivative)
        if not watts and isinstance(result, dict):
            wh = result.get("watt_hours", {})
            if wh and isinstance(wh, dict):
                logger.info(
                    "Forecast.Solar: falling back to 'watt_hours' "
                    "derivative (%d entries)", len(wh),
                )
                sorted_items = sorted(wh.items())
                prev_val = None
                for k, v in sorted_items:
                    cur = float(v)
                    if prev_val is not None:
                        watts[k] = max(0.0, cur - prev_val)
                    prev_val = cur

        logger.info(
            "Forecast.Solar response: result_keys=%s, watts_entries=%d, "
            "tz=%s, path=%s",
            list(result.keys()) if isinstance(result, dict) else "N/A",
            len(watts), tz_name, self._build_path(),
        )

        if not watts:
            logger.warning(
                "Forecast.Solar returned no usable solar data "
                "(result keys: %s)",
                list(result.keys()) if isinstance(result, dict) else "N/A",
            )
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

        logger.info(
            "Forecast.Solar forecast fetched: %d slots, range %s to %s",
            len(slots),
            slots[0].start.isoformat() if slots else "N/A",
            slots[-1].end.isoformat() if slots else "N/A",
        )
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
