"""Provider health monitoring."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ProviderHealth:
    """Health state of a single provider."""

    name: str
    healthy: bool = True
    last_success: float = 0.0
    last_failure: float = 0.0
    consecutive_failures: int = 0
    total_failures: int = 0
    last_error: str = ""


class HealthChecker:
    """Tracks health of all external providers."""

    def __init__(self, max_consecutive_failures: int = 3) -> None:
        self._max_failures = max_consecutive_failures
        self._providers: dict[str, ProviderHealth] = {}

    def register(self, name: str) -> None:
        """Register a provider for health tracking."""
        self._providers[name] = ProviderHealth(name=name, last_success=time.monotonic())

    def record_success(self, name: str) -> None:
        """Record a successful operation for a provider."""
        if name not in self._providers:
            self.register(name)
        p = self._providers[name]
        p.healthy = True
        p.last_success = time.monotonic()
        p.consecutive_failures = 0

    def record_failure(self, name: str, error: str = "") -> None:
        """Record a failed operation for a provider."""
        if name not in self._providers:
            self.register(name)
        p = self._providers[name]
        p.last_failure = time.monotonic()
        p.consecutive_failures += 1
        p.total_failures += 1
        p.last_error = error

        if p.consecutive_failures >= self._max_failures:
            p.healthy = False
            logger.warning(
                "Provider '%s' marked unhealthy (%d consecutive failures): %s",
                name, p.consecutive_failures, error,
            )

    def is_healthy(self, name: str) -> bool:
        """Check if a specific provider is healthy."""
        p = self._providers.get(name)
        return p.healthy if p else True  # Unknown providers assumed healthy

    def get_unhealthy(self) -> list[str]:
        """Return list of unhealthy provider names."""
        return [name for name, p in self._providers.items() if not p.healthy]

    def all_healthy(self) -> bool:
        """Check if all registered providers are healthy."""
        return all(p.healthy for p in self._providers.values())

    def get_health(self, name: str) -> ProviderHealth | None:
        return self._providers.get(name)

    def get_all_health(self) -> dict[str, ProviderHealth]:
        return dict(self._providers)
