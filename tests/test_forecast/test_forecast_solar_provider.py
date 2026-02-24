from __future__ import annotations

from power_master.config.schema import SolarProviderConfig
from power_master.forecast.providers.forecast_solar import ForecastSolarProvider


class _DummyResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


async def test_forecast_solar_request_and_parsing() -> None:
    cfg = SolarProviderConfig(
        type="forecast_solar",
        api_key="should-be-ignored",
        latitude=-33.856784,
        longitude=151.215297,
        declination=20.0,
        azimuth=180.0,
        kwp=8.5,
        timezone="Australia/Sydney",
    )
    provider = ForecastSolarProvider(cfg)
    captured: dict = {}

    async def _fake_get(path: str):
        captured["path"] = path
        return _DummyResponse(
            {
                "message": {"info": {"timezone": "Australia/Sydney"}},
                "result": {
                    "watts": {
                        "2026-02-24 18:00:00": 1200.0,
                        "2026-02-24 19:00:00": 1400.0,
                    }
                },
            }
        )

    provider._client.get = _fake_get  # type: ignore[method-assign]
    forecast = await provider.fetch_forecast()

    assert (
        captured["path"]
        == "/estimate/-33.856784/151.215297/20.0/180.0/8.5"
    )
    assert forecast.provider == "forecast_solar"
    assert len(forecast.slots) == 2
    assert forecast.slots[0].pv_estimate_w == 1200.0
    assert forecast.slots[1].pv_estimate_w == 1400.0
    assert forecast.slots[0].pv_estimate10_w == 1200.0
    assert forecast.slots[1].pv_estimate90_w == 1400.0

    await provider.close()
