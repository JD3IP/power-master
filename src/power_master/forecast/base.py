"""Abstract base classes for forecast providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class SolarForecastSlot:
    """Single time-slot of solar forecast data."""

    start: datetime
    end: datetime
    pv_estimate_w: float  # P50 estimate
    pv_estimate10_w: float  # P10 (pessimistic)
    pv_estimate90_w: float  # P90 (optimistic)

    @property
    def confidence(self) -> float:
        """Confidence score: 1 = narrow band, 0 = wide uncertainty."""
        p50 = max(self.pv_estimate_w, 0.1)
        spread = self.pv_estimate90_w - self.pv_estimate10_w
        return max(0.0, 1.0 - spread / p50)


@dataclass
class WeatherForecastSlot:
    """Single time-slot of weather forecast data."""

    time: datetime
    temperature_c: float
    cloud_cover_pct: float  # 0-100
    wind_speed_ms: float = 0.0
    precipitation_mm: float = 0.0
    humidity_pct: float = 0.0


@dataclass
class StormAlert:
    """Storm alert from weather provider."""

    location: str
    probability: float  # 0.0 - 1.0
    description: str
    valid_from: datetime
    valid_to: datetime
    severity: str = "moderate"  # low, moderate, severe


@dataclass
class SolarForecast:
    """Complete solar forecast response."""

    slots: list[SolarForecastSlot] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    provider: str = ""


@dataclass
class WeatherForecast:
    """Complete weather forecast response."""

    slots: list[WeatherForecastSlot] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    provider: str = ""


@dataclass
class StormForecast:
    """Storm forecast response."""

    alerts: list[StormAlert] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    provider: str = ""

    @property
    def max_probability(self) -> float:
        if not self.alerts:
            return 0.0
        return max(a.probability for a in self.alerts)


class SolarProvider(ABC):
    """Abstract base for solar forecast providers."""

    @abstractmethod
    async def fetch_forecast(self) -> SolarForecast:
        """Fetch solar power forecast."""
        ...

    @abstractmethod
    async def is_healthy(self) -> bool:
        """Check if the provider is reachable."""
        ...


class WeatherProvider(ABC):
    """Abstract base for weather forecast providers."""

    @abstractmethod
    async def fetch_forecast(self, hours: int = 48) -> WeatherForecast:
        """Fetch weather forecast."""
        ...

    @abstractmethod
    async def fetch_historical(
        self, start: datetime, end: datetime
    ) -> WeatherForecast:
        """Fetch historical weather data for backfill."""
        ...

    @abstractmethod
    async def is_healthy(self) -> bool:
        ...


class StormProvider(ABC):
    """Abstract base for storm alert providers."""

    @abstractmethod
    async def fetch_alerts(self) -> StormForecast:
        """Fetch current storm alerts."""
        ...

    @abstractmethod
    async def get_available_locations(self) -> list[dict[str, str]]:
        """Return list of selectable locations {aac, description}."""
        ...

    @abstractmethod
    async def is_healthy(self) -> bool:
        ...
