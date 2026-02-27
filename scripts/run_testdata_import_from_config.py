from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import yaml

from build_test_dataset import main_async


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--db-path", default="tests/testdata/backtest_2025.db")
    p.add_argument("--clear-existing", action="store_true")
    p.add_argument("--chunk-days", type=int, default=7)
    p.add_argument("--delay-seconds", type=float, default=6.2)
    p.add_argument("--max-chunks", type=int, default=1)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text()) or {}
    tariff = ((cfg.get("providers") or {}).get("tariff") or {})
    api_key = tariff.get("api_key", "")
    site_id = tariff.get("site_id", "")
    if not api_key or not site_id:
        raise SystemExit("Missing providers.tariff.api_key/site_id in config")

    ns = argparse.Namespace(
        db_path=args.db_path,
        usage_csv="tests/testdata/Home_usage.csv",
        solar_csv="tests/testdata/solar yield.csv",
        timezone="Australia/Brisbane",
        start_iso="2025-01-01T00:00:00+00:00",
        end_iso="2025-12-31T23:30:00+00:00",
        clear_existing=args.clear_existing,
        include_csv=False,
        fetch_amber=True,
        amber_api_key=api_key,
        amber_site_id=site_id,
        chunk_days=args.chunk_days,
        delay_seconds=args.delay_seconds,
        max_chunks=args.max_chunks,
    )
    asyncio.run(main_async(ns))


if __name__ == "__main__":
    main()
