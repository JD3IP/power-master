"""Shelly local HTTP API adapter for load control."""

from __future__ import annotations

import logging

import httpx

from power_master.config.schema import ShellyDeviceConfig
from power_master.loads.base import LoadState, LoadStatus

logger = logging.getLogger(__name__)

# Shelly Gen2 RPC endpoints
_SWITCH_SET = "/rpc/Switch.Set"
_SWITCH_GET_STATUS = "/rpc/Switch.GetStatus"
_GEN1_RELAY = "/relay/{relay_id}"


class ShellyAdapter:
    """Controls a Shelly relay via local HTTP API (Gen2 RPC).

    Uses httpx async client for non-blocking requests.
    """

    def __init__(self, config: ShellyDeviceConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=2.0),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=0),
        )
        self._owns_client = client is None
        self._base_url = f"http://{config.host}"
        self._last_known_status: LoadStatus | None = None

    @property
    def load_id(self) -> str:
        return f"shelly_{self._config.name}"

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def power_w(self) -> int:
        return self._config.power_w

    @property
    def priority_class(self) -> int:
        return self._config.priority_class

    async def turn_on(self) -> bool:
        """Turn relay on via Shelly API (Gen2 RPC with Gen1 fallback)."""
        try:
            resp = await self._client.post(
                f"{self._base_url}{_SWITCH_SET}",
                json={"id": self._config.relay_id, "on": True},
            )
            resp.raise_for_status()
            logger.info("Shelly '%s' turned ON", self._config.name)
            return True
        except httpx.HTTPError as rpc_error:
            logger.warning(
                "Gen2 turn_on failed for Shelly '%s' (relay %d), trying Gen1: %s",
                self._config.name,
                self._config.relay_id,
                rpc_error,
            )
            try:
                resp = await self._client.get(
                    f"{self._base_url}{_GEN1_RELAY.format(relay_id=self._config.relay_id)}",
                    params={"turn": "on"},
                )
                resp.raise_for_status()
                logger.info("Shelly '%s' turned ON (Gen1 fallback)", self._config.name)
                return True
            except httpx.HTTPError as e:
                logger.error("Failed to turn on Shelly '%s': %s", self._config.name, e)
                return False

    async def turn_off(self) -> bool:
        """Turn relay off via Shelly API (Gen2 RPC with Gen1 fallback)."""
        try:
            resp = await self._client.post(
                f"{self._base_url}{_SWITCH_SET}",
                json={"id": self._config.relay_id, "on": False},
            )
            resp.raise_for_status()
            logger.info("Shelly '%s' turned OFF", self._config.name)
            return True
        except httpx.HTTPError as rpc_error:
            logger.warning(
                "Gen2 turn_off failed for Shelly '%s' (relay %d), trying Gen1: %s",
                self._config.name,
                self._config.relay_id,
                rpc_error,
            )
            try:
                resp = await self._client.get(
                    f"{self._base_url}{_GEN1_RELAY.format(relay_id=self._config.relay_id)}",
                    params={"turn": "off"},
                )
                resp.raise_for_status()
                logger.info("Shelly '%s' turned OFF (Gen1 fallback)", self._config.name)
                return True
            except httpx.HTTPError as e:
                logger.error("Failed to turn off Shelly '%s': %s", self._config.name, e)
                return False

    async def get_status(self) -> LoadStatus:
        """Query relay status via Shelly API (Gen2 GET, Gen2 POST, Gen1 fallback).

        Caches the last successful result so transient timeouts return
        the last-known state instead of UNKNOWN/ERROR.
        """
        try:
            status = await self._query_status()
        except Exception as e:
            logger.warning("Shelly '%s' unexpected error: %s", self._config.name, e)
            status = LoadStatus(
                load_id=self.load_id,
                name=self.name,
                state=LoadState.ERROR,
                is_available=False,
                error=str(e),
            )

        if status.is_available:
            self._last_known_status = status
            return status

        # Device unreachable — return cached last-known state if available
        if self._last_known_status is not None:
            logger.debug(
                "Shelly '%s' unreachable, returning cached state: %s",
                self._config.name, self._last_known_status.state.value,
            )
            return self._last_known_status
        return status

    async def _query_status(self) -> LoadStatus:
        """Try Gen2 GET, Gen2 POST, then Gen1 GET to read relay status."""
        # Gen2 GET (preferred — avoids CORS/POST issues on some firmware)
        try:
            resp = await self._client.get(
                f"{self._base_url}{_SWITCH_GET_STATUS}",
                params={"id": self._config.relay_id},
            )
            resp.raise_for_status()
            return self._parse_gen2_status(resp.json())
        except Exception:
            pass

        # Gen2 POST (standard RPC)
        try:
            resp = await self._client.post(
                f"{self._base_url}{_SWITCH_GET_STATUS}",
                json={"id": self._config.relay_id},
            )
            resp.raise_for_status()
            return self._parse_gen2_status(resp.json())
        except Exception:
            pass

        # Gen1 GET fallback
        try:
            resp = await self._client.get(
                f"{self._base_url}{_GEN1_RELAY.format(relay_id=self._config.relay_id)}",
            )
            resp.raise_for_status()
            data = resp.json()
            state = LoadState.ON if data.get("ison", False) else LoadState.OFF
            power = int(float(data.get("apower", data.get("power", 0)) or 0))
            return LoadStatus(
                load_id=self.load_id,
                name=self.name,
                state=state,
                power_w=power,
                is_available=True,
            )
        except Exception as e:
            logger.warning("Failed to get status of Shelly '%s': %s", self._config.name, e)
            return LoadStatus(
                load_id=self.load_id,
                name=self.name,
                state=LoadState.ERROR,
                is_available=False,
                error=str(e),
            )

    def _parse_gen2_status(self, data: dict) -> LoadStatus:
        """Parse a Gen2 Switch.GetStatus response."""
        state = LoadState.ON if data.get("output", False) else LoadState.OFF
        power = int(data.get("apower", 0))
        return LoadStatus(
            load_id=self.load_id,
            name=self.name,
            state=state,
            power_w=power,
            is_available=True,
        )

    async def is_available(self) -> bool:
        """Check if the Shelly device is reachable."""
        try:
            resp = await self._client.post(
                f"{self._base_url}{_SWITCH_GET_STATUS}",
                json={"id": self._config.relay_id},
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            try:
                resp = await self._client.get(
                    f"{self._base_url}{_GEN1_RELAY.format(relay_id=self._config.relay_id)}",
                )
                return resp.status_code == 200
            except httpx.HTTPError:
                return False

    async def close(self) -> None:
        """Close the HTTP client if we own it."""
        if self._owns_client:
            await self._client.aclose()
