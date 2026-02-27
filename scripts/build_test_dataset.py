"""Build a historical test dataset from local CSV files + Amber pricing."""

from __future__ import annotations

import argparse
import asyncio
import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from power_master.config.schema import TariffProviderConfig
from power_master.db.engine import close_db, init_db
from power_master.db.repository import Repository
from power_master.tariff.providers.amber import AmberProvider


@dataclass
class ImportStats:
    rows: int = 0
    inserted: int = 0


def _dt_iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _parse_usage_csv(
    path: Path,
    start: datetime,
    end: datetime,
) -> dict[datetime, float]:
    # Source amounts are kWh per 30-min period; aggregate duplicate timestamps.
    by_ts_kwh: dict[datetime, float] = defaultdict(float)
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = datetime.fromisoformat(row["From (date/time)"]).astimezone(UTC)
            if ts < start or ts > end:
                continue
            by_ts_kwh[ts] += float(row["Amount Used"])
    return by_ts_kwh


def _parse_solar_csv(
    path: Path,
    start: datetime,
    end: datetime,
    timezone_name: str,
) -> dict[datetime, float]:
    # Source values are hourly kW. Convert to 30-min watts (same value at :00 and :30).
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        # Fallback for Windows/Python installs without IANA tzdata package.
        # Brisbane has no DST, so fixed UTC+10 is safe for this dataset.
        if timezone_name == "Australia/Brisbane":
            tz = timezone(timedelta(hours=10))
        else:
            raise
    by_ts_w: dict[datetime, float] = {}
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            year = int(row["Year"])
            month = int(row["Month"])
            day = int(row["Day"])
            hour = int(row["Hour"])
            kw = float(row["North Array Output (kW)"])
            local_dt = datetime(year, month, day, hour, 0, tzinfo=tz)
            for minute in (0, 30):
                ts = local_dt.replace(minute=minute).astimezone(UTC)
                if ts < start or ts > end:
                    continue
                by_ts_w[ts] = kw * 1000.0
    return by_ts_w


async def _clear_existing(repo: Repository) -> None:
    for data_type in ("load_w", "solar_w", "import_price_cents", "export_price_cents"):
        await repo.db.execute("DELETE FROM historical_data WHERE data_type = ?", (data_type,))
    await repo.db.commit()


async def _import_usage(repo: Repository, by_ts_kwh: dict[datetime, float]) -> ImportStats:
    stats = ImportStats(rows=len(by_ts_kwh))
    rows = []
    for ts, kwh in sorted(by_ts_kwh.items()):
        load_w = (kwh / 0.5) * 1000.0
        rows.append(("load_w", _dt_iso_utc(ts), load_w, "testdata_usage_csv", "30min"))
    await repo.db.executemany(
        """INSERT OR REPLACE INTO historical_data (data_type, recorded_at, value, source, resolution)
           VALUES (?, ?, ?, ?, ?)""",
        rows,
    )
    await repo.db.commit()
    stats.inserted = len(rows)
    return stats


async def _import_solar(repo: Repository, by_ts_w: dict[datetime, float]) -> ImportStats:
    stats = ImportStats(rows=len(by_ts_w))
    rows = []
    for ts, watts in sorted(by_ts_w.items()):
        rows.append(("solar_w", _dt_iso_utc(ts), watts, "testdata_solar_csv", "30min"))
    await repo.db.executemany(
        """INSERT OR REPLACE INTO historical_data (data_type, recorded_at, value, source, resolution)
           VALUES (?, ?, ?, ?, ?)""",
        rows,
    )
    await repo.db.commit()
    stats.inserted = len(rows)
    return stats


async def _import_amber_prices(
    repo: Repository,
    api_key: str,
    site_id: str,
    start: datetime,
    end: datetime,
    chunk_days: int,
    delay_seconds: float,
    max_chunks: int,
) -> ImportStats:
    provider = AmberProvider(
        TariffProviderConfig(
            api_key=api_key,
            site_id=site_id,
        )
    )
    try:
        stats = ImportStats()
        cursor = start
        chunk_no = 0
        while cursor <= end:
            if chunk_no >= max_chunks:
                print(f"Amber fetch stopped after {chunk_no} chunk(s) by --max-chunks limit.", flush=True)
                break
            chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
            print(
                f"Amber chunk {chunk_no + 1}: {cursor.date()} -> {chunk_end.date()}",
                flush=True,
            )
            schedule = await provider.fetch_historical(cursor, chunk_end)
            if schedule.slots:
                first = schedule.slots[0]
                last = schedule.slots[-1]
                print(
                    "  slots=%d first=%s (imp=%.2f exp=%.2f) last=%s (imp=%.2f exp=%.2f)"
                    % (
                        len(schedule.slots),
                        first.start.isoformat(),
                        first.import_price_cents,
                        first.export_price_cents,
                        last.start.isoformat(),
                        last.import_price_cents,
                        last.export_price_cents,
                    ),
                    flush=True,
                )
            else:
                print("  slots=0", flush=True)
            for slot in schedule.slots:
                if slot.start < start or slot.start > end + timedelta(minutes=30):
                    continue
                ts = _dt_iso_utc(slot.start)
                await repo.store_historical("import_price_cents", slot.import_price_cents, "amber_backfill_2025", ts)
                await repo.store_historical("export_price_cents", slot.export_price_cents, "amber_backfill_2025", ts)
                stats.inserted += 1
            stats.rows += len(schedule.slots)
            chunk_no += 1
            if chunk_end < end:
                await asyncio.sleep(delay_seconds)
            cursor = chunk_end + timedelta(days=1)
        return stats
    finally:
        await provider.close()


async def main_async(args: argparse.Namespace) -> None:
    start = datetime.fromisoformat(args.start_iso).astimezone(UTC)
    end = datetime.fromisoformat(args.end_iso).astimezone(UTC)
    if end <= start:
        raise ValueError("end must be after start")

    print(f"Opening DB: {args.db_path}", flush=True)
    db = await init_db(args.db_path)
    repo = Repository(db)
    try:
        if args.clear_existing:
            print("Clearing existing historical series...", flush=True)
            await _clear_existing(repo)
            print("Cleared.", flush=True)

        if args.include_csv:
            print(f"Parsing usage CSV: {args.usage_csv}", flush=True)
            usage_kwh = _parse_usage_csv(Path(args.usage_csv), start, end)
            print(f"Parsed usage slots: {len(usage_kwh)}", flush=True)
            print(f"Parsing solar CSV: {args.solar_csv}", flush=True)
            solar_w = _parse_solar_csv(Path(args.solar_csv), start, end, args.timezone)
            print(f"Parsed solar slots: {len(solar_w)}", flush=True)

            print("Importing usage...", flush=True)
            usage_stats = await _import_usage(repo, usage_kwh)
            print("Importing solar...", flush=True)
            solar_stats = await _import_solar(repo, solar_w)

            print(f"Usage import: {usage_stats.inserted} slots")
            print(f"Solar import: {solar_stats.inserted} slots")
        else:
            print("CSV import skipped (--include-csv not set).", flush=True)

        if args.fetch_amber:
            print("Fetching Amber historical pricing...", flush=True)
            if not args.amber_api_key:
                raise ValueError("--amber-api-key is required when --fetch-amber is set")
            site_id = args.amber_site_id
            if not site_id:
                discover = AmberProvider(TariffProviderConfig(api_key=args.amber_api_key, site_id=""))
                try:
                    site_id = await discover.get_site_id()
                finally:
                    await discover.close()
                if not site_id:
                    raise ValueError("Could not auto-discover Amber site ID; provide --amber-site-id")
            amber_stats = await _import_amber_prices(
                repo=repo,
                api_key=args.amber_api_key,
                site_id=site_id,
                start=start,
                end=end,
                chunk_days=args.chunk_days,
                delay_seconds=args.delay_seconds,
                max_chunks=args.max_chunks,
            )
            print(f"Amber import: {amber_stats.inserted} price slots ({amber_stats.rows} raw rows)")
    finally:
        await close_db(db)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="tests/testdata/backtest_2025.db")
    parser.add_argument("--usage-csv", default="tests/testdata/Home_usage.csv")
    parser.add_argument("--solar-csv", default="tests/testdata/solar yield.csv")
    parser.add_argument("--timezone", default="Australia/Brisbane")
    parser.add_argument("--start-iso", default="2025-01-01T00:00:00+00:00")
    parser.add_argument("--end-iso", default="2025-12-31T23:30:00+00:00")
    parser.add_argument("--clear-existing", action="store_true")
    parser.add_argument("--include-csv", action="store_true")
    parser.add_argument("--fetch-amber", action="store_true")
    parser.add_argument("--amber-api-key", default="")
    parser.add_argument("--amber-site-id", default="")
    parser.add_argument("--chunk-days", type=int, default=7)
    parser.add_argument("--delay-seconds", type=float, default=6.2)
    parser.add_argument("--max-chunks", type=int, default=1)
    return parser.parse_args()


def main() -> None:
    asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    main()
