"""Open-Meteo weather forecast provider.

Free, no authentication required.
API docs: https://open-meteo.com/en/docs
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from power_master.config.schema import WeatherProviderConfig
from power_master.forecast.base import WeatherForecast, WeatherForecastSlot, WeatherProvider

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HISTORICAL_URL = "https://archive-api.open-meteo.com/v1/archive"


class OpenMeteoProvider(WeatherProvider):
    """Open-Meteo REST API weather provider."""

    def __init__(self, config: WeatherProviderConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(timeout=30.0)

    async def fetch_forecast(self, hours: int = 48) -> WeatherForecast:
        """Fetch weather forecast from Open-Meteo."""
        params = {
            "latitude": self._config.latitude,
            "longitude": self._config.longitude,
            "hourly": "temperature_2m,cloud_cover,wind_speed_10m,precipitation,relative_humidity_2m",
            "forecast_hours": hours,
            "timezone": "UTC",
        }
        resp = await self._client.get(FORECAST_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

        slots = self._parse_hourly(data)
        logger.info("Open-Meteo forecast fetched: %d slots", len(slots))
        return WeatherForecast(
            slots=slots,
            fetched_at=datetime.now(timezone.utc),
            provider="openmeteo",
        )

    async def fetch_historical(
        self, start: datetime, end: datetime
    ) -> WeatherForecast:
        """Fetch historical weather from Open-Meteo archive API."""
        params = {
            "latitude": self._config.latitude,
            "longitude": self._config.longitude,
            "hourly": "temperature_2m,cloud_cover,wind_speed_10m,precipitation,relative_humidity_2m",
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": end.strftime("%Y-%m-%d"),
            "timezone": "UTC",
        }
        resp = await self._client.get(HISTORICAL_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

        slots = self._parse_hourly(data)
        logger.info(
            "Open-Meteo historical fetched: %d slots (%s to %s)",
            len(slots),
            start.date(),
            end.date(),
        )
        return WeatherForecast(
            slots=slots,
            fetched_at=datetime.now(timezone.utc),
            provider="openmeteo",
        )

    async def is_healthy(self) -> bool:
        try:
            params = {
                "latitude": self._config.latitude,
                "longitude": self._config.longitude,
                "hourly": "temperature_2m",
                "forecast_hours": 1,
            }
            resp = await self._client.get(FORECAST_URL, params=params)
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _parse_hourly(data: dict) -> list[WeatherForecastSlot]:
        """Parse Open-Meteo hourly response into WeatherForecastSlot list."""
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        clouds = hourly.get("cloud_cover", [])
        winds = hourly.get("wind_speed_10m", [])
        precips = hourly.get("precipitation", [])
        humids = hourly.get("relative_humidity_2m", [])

        slots = []
        for i, time_str in enumerate(times):
            dt = datetime.fromisoformat(time_str).replace(tzinfo=timezone.utc)
            slots.append(
                WeatherForecastSlot(
                    time=dt,
                    temperature_c=temps[i] if i < len(temps) else 0.0,
                    cloud_cover_pct=clouds[i] if i < len(clouds) else 0.0,
                    wind_speed_ms=winds[i] if i < len(winds) else 0.0,
                    precipitation_mm=precips[i] if i < len(precips) else 0.0,
                    humidity_pct=humids[i] if i < len(humids) else 0.0,
                )
            )
        return slots
