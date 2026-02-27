from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from power_master.config.schema import AppConfig
from power_master.optimisation.backtest_lab import run_backtest


@pytest.mark.asyncio
async def test_run_backtest_with_historical_data(repo) -> None:
    start = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    for i in range(4):
        ts = (start + timedelta(minutes=30 * i)).isoformat()
        await repo.store_historical("load_w", 1200.0, "test", ts)
        await repo.store_historical("solar_w", 300.0 if i >= 2 else 0.0, "test", ts)
        await repo.store_historical("import_price_cents", 20.0 + i, "test", ts)
        await repo.store_historical("export_price_cents", 6.0, "test", ts)

    result = await run_backtest(
        repo=repo,
        config=AppConfig(),
        start=start,
        end=start + timedelta(minutes=30 * 3),
        initial_soc=0.5,
        initial_wacb_cents=10.0,
    )

    assert result.summary.slots == 4
    assert result.summary.import_kwh >= 0
    assert result.summary.export_kwh >= 0
    assert len(result.daily_rows) >= 1


@pytest.mark.asyncio
async def test_backtest_aligns_30min_slots_with_second_offset(repo) -> None:
    start = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    # Load/solar at :00:00, prices at :00:01 (Amber-like)
    await repo.store_historical("load_w", 1400.0, "test", start.isoformat())
    await repo.store_historical("solar_w", 300.0, "test", start.isoformat())
    await repo.store_historical("import_price_cents", 25.0, "test", start.replace(second=1).isoformat())
    await repo.store_historical("export_price_cents", 6.0, "test", start.replace(second=1).isoformat())

    result = await run_backtest(
        repo=repo,
        config=AppConfig(),
        start=start,
        end=start + timedelta(minutes=1),
        initial_soc=0.5,
        initial_wacb_cents=10.0,
    )

    assert result.summary.slots == 1
    assert len(result.slot_rows) == 1
    row = result.slot_rows[0]
    assert row["load_kw"] > 0
    assert row["solar_kw"] > 0
