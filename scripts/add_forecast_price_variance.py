"""Create synthetic forecast price series from Amber historical actuals.

Includes mixed spike uncertainty:
- some spike-like intervals become "false spikes" (forecast ~5x worse),
- others remain "near actual" to simulate stabilising markets.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from collections import defaultdict
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default="tests/testdata/backtest_2025.db")
    p.add_argument("--start", default="2025-01-01T00:00:00+00:00")
    p.add_argument("--end", default="2025-12-31T23:59:59+00:00")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--import-std-pct", type=float, default=0.12)
    p.add_argument("--export-std-pct", type=float, default=0.10)
    p.add_argument("--absolute-jitter-cents", type=float, default=1.5)
    p.add_argument("--spike-threshold-export-cents", type=float, default=20.0)
    p.add_argument("--spike-threshold-import-cents", type=float, default=100.0)
    p.add_argument("--false-spike-ratio", type=float, default=0.55)
    p.add_argument("--false-spike-multiplier", type=float, default=5.0)
    p.add_argument("--near-actual-std-pct", type=float, default=0.03)
    p.add_argument("--near-actual-abs-jitter-cents", type=float, default=0.8)
    p.add_argument("--source-actual", default="amber_backfill_2025")
    p.add_argument("--source-forecast", default="amber_forecast_variant_uncertain")
    p.add_argument("--clear-existing", action="store_true")
    return p.parse_args()


def _forecast_value(actual: float, std_pct: float, abs_jitter: float) -> float:
    # Multiplicative + additive perturbation with non-negative clamp.
    mul = 1.0 + random.gauss(0.0, std_pct)
    add = random.uniform(-abs_jitter, abs_jitter)
    return max(0.0, actual * mul + add)


def _month_key(ts: str) -> str:
    return ts[:7]


def main() -> None:
    args = parse_args()
    random.seed(args.seed)

    db_path = Path(args.db_path)
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    try:
        if args.clear_existing:
            cur.execute("DELETE FROM historical_data WHERE data_type = 'forecast_import_price_cents'")
            cur.execute("DELETE FROM historical_data WHERE data_type = 'forecast_export_price_cents'")
            cur.execute("DROP TABLE IF EXISTS amber_price_enriched")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS amber_price_enriched (
                recorded_at TEXT PRIMARY KEY,
                import_price_cents REAL NOT NULL,
                export_price_cents REAL NOT NULL,
                forecast_import_price_cents REAL NOT NULL,
                forecast_export_price_cents REAL NOT NULL,
                source TEXT NOT NULL
            )
            """
        )
        cur.execute("PRAGMA table_info(amber_price_enriched)")
        cols = {r[1] for r in cur.fetchall()}
        if "forecast_import_variant_cents" not in cols:
            cur.execute("ALTER TABLE amber_price_enriched ADD COLUMN forecast_import_variant_cents REAL")
        if "forecast_export_variant_cents" not in cols:
            cur.execute("ALTER TABLE amber_price_enriched ADD COLUMN forecast_export_variant_cents REAL")
        if "forecast_variant_scenario" not in cols:
            cur.execute("ALTER TABLE amber_price_enriched ADD COLUMN forecast_variant_scenario TEXT")

        cur.execute(
            """
            SELECT recorded_at, value
            FROM historical_data
            WHERE data_type = 'import_price_cents'
              AND source = ?
              AND recorded_at >= ?
              AND recorded_at <= ?
            ORDER BY recorded_at
            """,
            (args.source_actual, args.start, args.end),
        )
        imports = dict(cur.fetchall())

        cur.execute(
            """
            SELECT recorded_at, value
            FROM historical_data
            WHERE data_type = 'export_price_cents'
              AND source = ?
              AND recorded_at >= ?
              AND recorded_at <= ?
            ORDER BY recorded_at
            """,
            (args.source_actual, args.start, args.end),
        )
        exports = dict(cur.fetchall())

        common_ts = sorted(set(imports.keys()) & set(exports.keys()))
        if not common_ts:
            raise SystemExit("No matching Amber import/export rows found for requested window.")

        hist_rows: list[tuple[str, str, float, str, str]] = []
        scenario_counts: dict[str, int] = defaultdict(int)
        scenario_months: dict[str, set[str]] = defaultdict(set)
        for ts in common_ts:
            imp = float(imports[ts])
            exp = float(exports[ts])

            f_imp = _forecast_value(imp, args.import_std_pct, args.absolute_jitter_cents)
            f_exp = _forecast_value(exp, args.export_std_pct, args.absolute_jitter_cents)
            scenario = "base_noise"

            is_spike_like = (exp >= args.spike_threshold_export_cents) or (imp >= args.spike_threshold_import_cents)
            if is_spike_like:
                if random.random() < args.false_spike_ratio:
                    scenario = "false_spike"
                    f_imp = _forecast_value(
                        imp * args.false_spike_multiplier,
                        args.import_std_pct,
                        args.absolute_jitter_cents,
                    )
                    f_exp = _forecast_value(
                        exp * args.false_spike_multiplier,
                        args.export_std_pct,
                        args.absolute_jitter_cents,
                    )
                else:
                    scenario = "near_actual"
                    f_imp = _forecast_value(
                        imp,
                        args.near_actual_std_pct,
                        args.near_actual_abs_jitter_cents,
                    )
                    f_exp = _forecast_value(
                        exp,
                        args.near_actual_std_pct,
                        args.near_actual_abs_jitter_cents,
                    )

            scenario_counts[scenario] += 1
            scenario_months[scenario].add(_month_key(ts))

            hist_rows.append(("forecast_import_price_cents", ts, f_imp, args.source_forecast, "30min"))
            hist_rows.append(("forecast_export_price_cents", ts, f_exp, args.source_forecast, "30min"))

            cur.execute(
                """
                INSERT INTO amber_price_enriched
                (recorded_at, import_price_cents, export_price_cents,
                 forecast_import_price_cents, forecast_export_price_cents, source,
                 forecast_import_variant_cents, forecast_export_variant_cents, forecast_variant_scenario)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recorded_at) DO UPDATE SET
                    import_price_cents=excluded.import_price_cents,
                    export_price_cents=excluded.export_price_cents,
                    forecast_import_price_cents=excluded.forecast_import_price_cents,
                    forecast_export_price_cents=excluded.forecast_export_price_cents,
                    source=excluded.source,
                    forecast_import_variant_cents=excluded.forecast_import_variant_cents,
                    forecast_export_variant_cents=excluded.forecast_export_variant_cents,
                    forecast_variant_scenario=excluded.forecast_variant_scenario
                """,
                (ts, imp, exp, f_imp, f_exp, args.source_forecast, f_imp, f_exp, scenario),
            )

        cur.executemany(
            """
            INSERT OR REPLACE INTO historical_data (data_type, recorded_at, value, source, resolution)
            VALUES (?, ?, ?, ?, ?)
            """,
            hist_rows,
        )

        con.commit()
        print(f"updated_rows={len(common_ts)}")
        print(f"historical_rows_written={len(hist_rows)}")
        print("table=amber_price_enriched")
        print(
            "scenario_counts="
            + ",".join(f"{k}:{scenario_counts[k]}" for k in sorted(scenario_counts.keys()))
        )
        for k in ("false_spike", "near_actual"):
            months = sorted(scenario_months.get(k, set()))
            print(f"{k}_months={','.join(months)}")
    finally:
        con.close()


if __name__ == "__main__":
    main()

