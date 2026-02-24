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
        self._client = client or httpx.AsyncClient(timeout=5.0)
        self._owns_client = client is None
        self._base_url = f"http://{config.host}"

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
        """Query relay status via Shelly API (Gen2 RPC with Gen1 fallback)."""
        try:
            resp = await self._client.post(
                f"{self._base_url}{_SWITCH_GET_STATUS}",
                json={"id": self._config.relay_id},
            )
            resp.raise_for_status()
            data = resp.json()

            state = LoadState.ON if data.get("output", False) else LoadState.OFF
            power = int(data.get("apower", 0))

            return LoadStatus(
                load_id=self.load_id,
                name=self.name,
                state=state,
                power_w=power,
                is_available=True,
            )
        except httpx.HTTPError as rpc_error:
            logger.warning(
                "Gen2 status failed for Shelly '%s' (relay %d), trying Gen1: %s",
                self._config.name,
                self._config.relay_id,
                rpc_error,
            )
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
            except httpx.HTTPError as e:
                logger.error("Failed to get status of Shelly '%s': %s", self._config.name, e)
                return LoadStatus(
                    load_id=self.load_id,
                    name=self.name,
                    state=LoadState.ERROR,
                    is_available=False,
                    error=str(e),
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
