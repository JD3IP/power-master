"""Forecast aggregator — merges multiple providers into unified forecast state."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from power_master.forecast.base import (
    SolarForecast,
    SolarProvider,
    StormForecast,
    StormProvider,
    WeatherForecast,
    WeatherProvider,
)
from power_master.tariff.base import TariffProvider, TariffSchedule
from power_master.tariff.spike import SpikeDetector

logger = logging.getLogger(__name__)


@dataclass
class AggregatedForecast:
    """Combined forecast state from all providers."""

    solar: SolarForecast | None = None
    weather: WeatherForecast | None = None
    storm: StormForecast | None = None
    tariff: TariffSchedule | None = None
    last_solar_update: datetime | None = None
    last_weather_update: datetime | None = None
    last_storm_update: datetime | None = None
    last_tariff_update: datetime | None = None

    @property
    def has_solar(self) -> bool:
        return self.solar is not None and len(self.solar.slots) > 0

    @property
    def has_weather(self) -> bool:
        return self.weather is not None and len(self.weather.slots) > 0

    @property
    def has_tariff(self) -> bool:
        return self.tariff is not None and len(self.tariff.slots) > 0

    @property
    def storm_probability(self) -> float:
        if self.storm is None:
            return 0.0
        return self.storm.max_probability


class ForecastAggregator:
    """Orchestrates fetching from all forecast/tariff providers."""

    def __init__(
        self,
        solar_provider: SolarProvider | None = None,
        weather_provider: WeatherProvider | None = None,
        storm_provider: StormProvider | None = None,
        tariff_provider: TariffProvider | None = None,
        spike_detector: SpikeDetector | None = None,
    ) -> None:
        self._solar = solar_provider
        self._weather = weather_provider
        self._storm = storm_provider
        self._tariff = tariff_provider
        self._spike = spike_detector or SpikeDetector()
        self._state = AggregatedForecast()

    @property
    def state(self) -> AggregatedForecast:
        return self._state

    @property
    def spike_detector(self) -> SpikeDetector:
        return self._spike

    async def update_solar(self) -> SolarForecast | None:
        """Fetch latest solar forecast."""
        if self._solar is None:
            return None
        try:
            forecast = await self._solar.fetch_forecast()
            self._state.solar = forecast
            self._state.last_solar_update = datetime.now(timezone.utc)
            return forecast
        except Exception as e:
            logger.error("Solar forecast update failed: %s", e)
            return None

    async def update_weather(self, hours: int = 48) -> WeatherForecast | None:
        """Fetch latest weather forecast."""
        if self._weather is None:
            return None
        try:
            forecast = await self._weather.fetch_forecast(hours)
            self._state.weather = forecast
            self._state.last_weather_update = datetime.now(timezone.utc)
            return forecast
        except Exception as e:
            logger.error("Weather forecast update failed: %s", e)
            return None

    async def update_storm(self) -> StormForecast | None:
        """Fetch latest storm alerts."""
        if self._storm is None:
            return None
        try:
            forecast = await self._storm.fetch_alerts()
            self._state.storm = forecast
            self._state.last_storm_update = datetime.now(timezone.utc)
            return forecast
        except Exception as e:
            logger.error("Storm alert update failed: %s", e)
            return None

    async def update_tariff(self) -> TariffSchedule | None:
        """Fetch latest tariff prices and check for spikes."""
        if self._tariff is None:
            return None
        try:
            schedule = await self._tariff.fetch_prices()
            self._state.tariff = schedule
            self._state.last_tariff_update = datetime.now(timezone.utc)
            # Run spike detection
            self._spike.evaluate(schedule)
            return schedule
        except Exception as e:
            logger.error("Tariff update failed: %s", e)
            return None

    async def update_all(
        self, config=None, respect_validity: bool = False,
    ) -> AggregatedForecast:
        """Update all providers. Errors in one don't block others.

        If respect_validity=True and config is provided, skip providers
        whose cached data is still within their validity_seconds window.
        This prevents unnecessary API calls on startup after load_from_db().
        """
        now = datetime.now(timezone.utc)

        def _is_fresh(
            last_update: datetime | None,
            validity_s: int,
            has_data: bool = True,
        ) -> bool:
            if not respect_validity or last_update is None or not has_data:
                return False
            age = (now - last_update).total_seconds()
            return age < validity_s

        # Solar — expensive (Solcast: 10 req/day free tier)
        solar_validity = getattr(config, "solar", None)
        solar_vs = solar_validity.validity_seconds if solar_validity else 21600
        if _is_fresh(
            self._state.last_solar_update,
            solar_vs,
            has_data=self._state.has_solar,
        ):
            logger.info("Solar forecast still fresh (skipping API call)")
        else:
            await self.update_solar()

        # Weather
        weather_validity = getattr(config, "weather", None)
        weather_vs = weather_validity.validity_seconds if weather_validity else 3600
        if _is_fresh(
            self._state.last_weather_update,
            weather_vs,
            has_data=self._state.has_weather,
        ):
            logger.info("Weather forecast still fresh (skipping API call)")
        else:
            await self.update_weather()

        # Storm
        storm_validity = getattr(config, "storm", None)
        storm_vs = storm_validity.validity_seconds if storm_validity else 21600
        if _is_fresh(
            self._state.last_storm_update,
            storm_vs,
            has_data=self._state.storm is not None,
        ):
            logger.info("Storm forecast still fresh (skipping API call)")
        else:
            await self.update_storm()

        # Tariff — frequent updates needed for real-time pricing
        tariff_validity = getattr(config, "tariff", None)
        tariff_vs = tariff_validity.validity_seconds if tariff_validity else 300
        if _is_fresh(
            self._state.last_tariff_update,
            tariff_vs,
            has_data=self._state.has_tariff,
        ):
            logger.info("Tariff data still fresh (skipping API call)")
        else:
            await self.update_tariff()

        return self._state

    def is_stale(self, max_age_seconds: int = 7200) -> bool:
        """Check if any critical forecast is stale."""
        now = datetime.now(timezone.utc)

        if self._state.last_tariff_update:
            age = (now - self._state.last_tariff_update).total_seconds()
            if age > max_age_seconds:
                return True

        if self._state.last_solar_update:
            age = (now - self._state.last_solar_update).total_seconds()
            if age > max_age_seconds:
                return True

        return False

    async def load_from_db(self, repo) -> None:
        """Restore forecast state from database on startup.

        Loads the latest forecast for each provider type and sets
        last-update timestamps so we only fetch from APIs when data
        is actually stale.
        """
        now = datetime.now(timezone.utc)

        # Load latest forecasts from DB
        for provider_type in ("solar", "weather", "storm", "tariff"):
            try:
                age_s = await repo.get_forecast_age_seconds(provider_type)
                if age_s is not None:
                    ts = now - timedelta(seconds=age_s)
                    setattr(self._state, f"last_{provider_type}_update", ts)
                    logger.info(
                        "Restored %s forecast timestamp (age: %ds)",
                        provider_type, int(age_s),
                    )
            except Exception as e:
                logger.debug("No stored %s forecast: %s", provider_type, e)

    def update_providers(
        self,
        solar_provider: SolarProvider | None = None,
        weather_provider: WeatherProvider | None = None,
        storm_provider: StormProvider | None = None,
        tariff_provider: TariffProvider | None = None,
    ) -> None:
        """Hot-swap provider instances (for config reload)."""
        if solar_provider is not None:
            self._solar = solar_provider
        if weather_provider is not None:
            self._weather = weather_provider
        if storm_provider is not None:
            self._storm = storm_provider
        if tariff_provider is not None:
            self._tariff = tariff_provider
