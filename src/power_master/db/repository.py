"""Data access layer for all database operations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    """Centralised data access for all tables."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db

    async def check_integrity(self) -> None:
        """Verify SQLite database integrity. Raises RuntimeError if corrupt."""
        async with self.db.execute("PRAGMA integrity_check") as cursor:
            row = await cursor.fetchone()
            if row and row[0] != "ok":
                raise RuntimeError(f"Database integrity check failed: {row[0]}")
        logger.info("Database integrity check passed")

    # ── Telemetry ───────────────────────────────────────────

    async def store_telemetry(
        self,
        soc: float,
        battery_power_w: int,
        solar_power_w: int,
        grid_power_w: int,
        load_power_w: int,
        battery_voltage: float | None = None,
        battery_temp_c: float | None = None,
        inverter_mode: str | None = None,
        grid_available: bool = True,
        raw_data: dict[str, Any] | None = None,
    ) -> int:
        now = _now()
        async with self.db.execute(
            """INSERT INTO telemetry
               (recorded_at, soc, battery_power_w, solar_power_w, grid_power_w,
                load_power_w, battery_voltage, battery_temp_c, inverter_mode,
                grid_available, raw_data_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now, soc, battery_power_w, solar_power_w, grid_power_w,
                load_power_w, battery_voltage, battery_temp_c, inverter_mode,
                1 if grid_available else 0,
                json.dumps(raw_data) if raw_data else None,
            ),
        ) as cursor:
            row_id = cursor.lastrowid
        await self.db.commit()
        return row_id  # type: ignore[return-value]

    async def get_latest_telemetry(self) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT * FROM telemetry ORDER BY recorded_at DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_telemetry_since(self, cutoff_iso: str) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM telemetry WHERE recorded_at >= ? ORDER BY recorded_at",
            (cutoff_iso,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def store_telemetry_batch(self, records: list[dict[str, Any]]) -> None:
        """Bulk-insert telemetry records without individual commits.

        Each record should have keys: soc, battery_power_w, solar_power_w,
        grid_power_w, load_power_w, and optionally: battery_voltage,
        battery_temp_c, inverter_mode, grid_available, raw_data.
        """
        if not records:
            return

        rows = [
            (
                r.get("recorded_at") or _now(),
                r.get("soc", 0.5),
                r.get("battery_power_w", 0),
                r.get("solar_power_w", 0),
                r.get("grid_power_w", 0),
                r.get("load_power_w", 0),
                r.get("battery_voltage"),
                r.get("battery_temp_c"),
                r.get("inverter_mode"),
                1 if r.get("grid_available", True) else 0,
                json.dumps(r.get("raw_data")) if r.get("raw_data") else None,
            )
            for r in records
        ]
        await self.db.executemany(
            """INSERT INTO telemetry
               (recorded_at, soc, battery_power_w, solar_power_w, grid_power_w,
                load_power_w, battery_voltage, battery_temp_c, inverter_mode,
                grid_available, raw_data_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        await self.db.commit()

    # ── Forecast Snapshots ──────────────────────────────────

    async def store_forecast(
        self,
        provider_type: str,
        provider_name: str,
        horizon_start: str,
        horizon_end: str,
        data: dict[str, Any],
        solar_estimates: list[dict[str, Any]] | None = None,
        confidence_score: float | None = None,
        storm_probability: float | None = None,
        storm_window_start: str | None = None,
        storm_window_end: str | None = None,
        status: str = "ok",
    ) -> int:
        now = _now()
        async with self.db.execute(
            """INSERT INTO forecast_snapshots
               (provider_type, provider_name, fetched_at, horizon_start, horizon_end,
                data_json, solar_estimate_json, confidence_score, storm_probability,
                storm_window_start, storm_window_end, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                provider_type, provider_name, now, horizon_start, horizon_end,
                json.dumps(data),
                json.dumps(solar_estimates) if solar_estimates else None,
                confidence_score, storm_probability,
                storm_window_start, storm_window_end, status,
            ),
        ) as cursor:
            row_id = cursor.lastrowid
        await self.db.commit()
        return row_id  # type: ignore[return-value]

    async def get_latest_forecast(self, provider_type: str) -> dict[str, Any] | None:
        async with self.db.execute(
            """SELECT * FROM forecast_snapshots
               WHERE provider_type = ? ORDER BY fetched_at DESC LIMIT 1""",
            (provider_type,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_forecasts_between(
        self, provider_type: str, start_iso: str, end_iso: str,
    ) -> list[dict[str, Any]]:
        """Return forecast snapshots fetched in the given window."""
        async with self.db.execute(
            """SELECT * FROM forecast_snapshots
               WHERE provider_type = ? AND fetched_at >= ? AND fetched_at <= ?
               ORDER BY fetched_at""",
            (provider_type, start_iso, end_iso),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ── Forecast Samples (per-horizon forecast persistence) ────────────

    async def store_forecast_samples(
        self, samples: list[dict[str, Any]],
    ) -> int:
        """Bulk-insert forecast samples.  INSERT OR IGNORE on the unique index.

        Each sample dict: provider_type, metric, fetched_at, horizon_hours,
        target_time, predicted_value.
        """
        if not samples:
            return 0
        rows = [
            (
                s["provider_type"], s["metric"], s["fetched_at"],
                float(s["horizon_hours"]), s["target_time"],
                float(s["predicted_value"]),
            )
            for s in samples
        ]
        await self.db.executemany(
            """INSERT OR IGNORE INTO forecast_samples
               (provider_type, metric, fetched_at, horizon_hours,
                target_time, predicted_value)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )
        await self.db.commit()
        return len(rows)

    async def get_forecast_samples(
        self,
        provider_type: str,
        metric: str | None = None,
        *,
        target_time_start: str | None = None,
        target_time_end: str | None = None,
        min_horizon: float | None = None,
        max_horizon: float | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query persisted forecast samples.

        All filter arguments are optional.  Results ordered by target_time asc.
        """
        query = ["SELECT * FROM forecast_samples WHERE provider_type = ?"]
        params: list[Any] = [provider_type]
        if metric is not None:
            query.append(" AND metric = ?")
            params.append(metric)
        if target_time_start is not None:
            query.append(" AND target_time >= ?")
            params.append(target_time_start)
        if target_time_end is not None:
            query.append(" AND target_time <= ?")
            params.append(target_time_end)
        if min_horizon is not None:
            query.append(" AND horizon_hours >= ?")
            params.append(float(min_horizon))
        if max_horizon is not None:
            query.append(" AND horizon_hours <= ?")
            params.append(float(max_horizon))
        query.append(" ORDER BY target_time")
        if limit is not None:
            query.append(" LIMIT ?")
            params.append(int(limit))
        async with self.db.execute("".join(query), params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def prune_forecast_samples(self, cutoff_iso: str) -> int:
        """Delete forecast samples whose target_time is older than cutoff."""
        async with self.db.execute(
            "DELETE FROM forecast_samples WHERE target_time < ?",
            (cutoff_iso,),
        ) as cursor:
            count = cursor.rowcount or 0
        await self.db.commit()
        return count

    # ── Notification Log ───────────────────────────────────────

    async def store_notification(
        self,
        *,
        emitted_at: str,
        event_name: str,
        severity: str,
        tier: str,
        title: str,
        message: str,
        action_json: str | None = None,
        incident_id: str | None = None,
        correlation_id: str | None = None,
        channels_sent: str = "",
    ) -> int:
        async with self.db.execute(
            """INSERT INTO notification_log
               (emitted_at, event_name, severity, tier, title, message,
                action_json, incident_id, correlation_id, channels_sent)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                emitted_at, event_name, severity, tier, title, message,
                action_json, incident_id, correlation_id, channels_sent,
            ),
        ) as cursor:
            row_id = cursor.lastrowid
        await self.db.commit()
        return row_id  # type: ignore[return-value]

    async def get_notifications_since(
        self, cutoff_iso: str, limit: int = 500,
    ) -> list[dict[str, Any]]:
        async with self.db.execute(
            """SELECT * FROM notification_log
               WHERE emitted_at >= ?
               ORDER BY emitted_at DESC
               LIMIT ?""",
            (cutoff_iso, int(limit)),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def prune_notifications(self, cutoff_iso: str) -> int:
        async with self.db.execute(
            "DELETE FROM notification_log WHERE emitted_at < ?",
            (cutoff_iso,),
        ) as cursor:
            count = cursor.rowcount or 0
        await self.db.commit()
        return count

    # ── Tariff Schedules ────────────────────────────────────

    async def store_tariff(
        self,
        provider_name: str,
        effective_from: str,
        schedule: list[dict[str, Any]],
        effective_until: str | None = None,
    ) -> int:
        now = _now()
        async with self.db.execute(
            """INSERT INTO tariff_schedules
               (provider_name, effective_from, effective_until, schedule_json, fetched_at)
               VALUES (?, ?, ?, ?, ?)""",
            (provider_name, effective_from, effective_until, json.dumps(schedule), now),
        ) as cursor:
            row_id = cursor.lastrowid
        await self.db.commit()
        return row_id  # type: ignore[return-value]

    async def get_latest_tariff(self) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT * FROM tariff_schedules ORDER BY fetched_at DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_tariff_since(self, cutoff_iso: str) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM tariff_schedules WHERE fetched_at >= ? ORDER BY fetched_at",
            (cutoff_iso,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ── Optimisation Plans ──────────────────────────────────

    async def get_next_plan_version(self) -> int:
        async with self.db.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM optimisation_plans"
        ) as cursor:
            row = await cursor.fetchone()
            return row[0]  # type: ignore[index,return-value]

    async def store_plan(
        self,
        version: int,
        trigger_reason: str,
        horizon_start: str,
        horizon_end: str,
        objective_score: float,
        solver_time_ms: int,
        metrics: dict[str, Any],
        active_constraints: list[str],
        reserve_state: dict[str, Any] | None = None,
        forecast_snapshot_id: int | None = None,
        tariff_schedule_id: int | None = None,
        config_version_id: int | None = None,
    ) -> int:
        now = _now()
        # Mark previous active plans as superseded
        await self.db.execute(
            "UPDATE optimisation_plans SET status = 'superseded' WHERE status = 'active'"
        )
        async with self.db.execute(
            """INSERT INTO optimisation_plans
               (version, created_at, trigger_reason, horizon_start, horizon_end,
                objective_score, solver_time_ms, status, metrics_json,
                forecast_snapshot_id, tariff_schedule_id, config_version_id,
                active_constraints_json, reserve_state_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)""",
            (
                version, now, trigger_reason, horizon_start, horizon_end,
                objective_score, solver_time_ms, json.dumps(metrics),
                forecast_snapshot_id, tariff_schedule_id, config_version_id,
                json.dumps(active_constraints),
                json.dumps(reserve_state) if reserve_state else None,
            ),
        ) as cursor:
            plan_id = cursor.lastrowid
        await self.db.commit()
        return plan_id  # type: ignore[return-value]

    async def store_plan_slots(self, plan_id: int, slots: list[dict[str, Any]]) -> None:
        for slot in slots:
            await self.db.execute(
                """INSERT INTO plan_slots
                   (plan_id, slot_index, slot_start, slot_end, operating_mode,
                    target_power_w, expected_soc, import_rate_cents, export_rate_cents,
                    solar_forecast_w, load_forecast_w, scheduled_loads_json, constraint_flags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan_id, slot["slot_index"], slot["slot_start"], slot["slot_end"],
                    slot["operating_mode"], slot["target_power_w"], slot["expected_soc"],
                    slot["import_rate_cents"], slot["export_rate_cents"],
                    slot["solar_forecast_w"], slot["load_forecast_w"],
                    json.dumps(slot.get("scheduled_loads")) if slot.get("scheduled_loads") else None,
                    json.dumps(slot.get("constraint_flags")) if slot.get("constraint_flags") else None,
                ),
            )
        await self.db.commit()

    async def get_active_plan(self) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT * FROM optimisation_plans WHERE status = 'active' LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_plan_slots(self, plan_id: int) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM plan_slots WHERE plan_id = ? ORDER BY slot_index",
            (plan_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_plan_history(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM optimisation_plans ORDER BY version DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ── Inverter Commands ───────────────────────────────────

    async def log_command(
        self,
        command_type: str,
        parameters: dict[str, Any],
        source_reason: str,
        source_plan_id: int | None = None,
        result: str = "pending",
        response: dict[str, Any] | None = None,
        latency_ms: int | None = None,
    ) -> int:
        now = _now()
        async with self.db.execute(
            """INSERT INTO inverter_commands
               (issued_at, command_type, parameters_json, source_plan_id,
                source_reason, result, response_json, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now, command_type, json.dumps(parameters), source_plan_id,
                source_reason, result,
                json.dumps(response) if response else None,
                latency_ms,
            ),
        ) as cursor:
            row_id = cursor.lastrowid
        await self.db.commit()
        return row_id  # type: ignore[return-value]

    async def get_recent_commands(self, seconds: int = 900) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc).isoformat()
        async with self.db.execute(
            """SELECT * FROM inverter_commands
               WHERE issued_at >= datetime(?, '-' || ? || ' seconds')
               ORDER BY issued_at DESC""",
            (cutoff, seconds),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def log_command_audit(
        self,
        mode: str,
        power_w: int,
        source: str,
        source_type: str,
        reason: str,
        priority: int,
        result: str = "pending",
        latency_ms: int | None = None,
    ) -> int:
        """Log a control command to the audit trail."""
        now = _now()
        async with self.db.execute(
            """INSERT INTO command_audit_log
               (issued_at, mode, power_w, source, source_type, reason, priority,
                result, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now, mode, power_w, source, source_type, reason, priority,
                result, latency_ms,
            ),
        ) as cursor:
            row_id = cursor.lastrowid
        await self.db.commit()
        return row_id  # type: ignore[return-value]

    async def get_audit_trail(self, hours: int = 24, source_type: str | None = None) -> list[dict[str, Any]]:
        """Get recent command audit entries."""
        cutoff = datetime.now(timezone.utc).isoformat()
        if source_type:
            query = """SELECT * FROM command_audit_log
                       WHERE issued_at >= datetime(?, '-' || ? || ' hours')
                       AND source_type = ?
                       ORDER BY issued_at DESC"""
            args = (cutoff, hours, source_type)
        else:
            query = """SELECT * FROM command_audit_log
                       WHERE issued_at >= datetime(?, '-' || ? || ' hours')
                       ORDER BY issued_at DESC"""
            args = (cutoff, hours)
        async with self.db.execute(query, args) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ── Billing Cycles ──────────────────────────────────────

    async def get_active_billing_cycle(self) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT * FROM billing_cycles WHERE status = 'active' LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def create_billing_cycle(
        self, cycle_start: str, cycle_end: str
    ) -> int:
        async with self.db.execute(
            """INSERT INTO billing_cycles (cycle_start, cycle_end, status)
               VALUES (?, ?, 'active')""",
            (cycle_start, cycle_end),
        ) as cursor:
            row_id = cursor.lastrowid
        await self.db.commit()
        return row_id  # type: ignore[return-value]

    async def close_billing_cycle(self, cycle_id: int) -> None:
        await self.db.execute(
            "UPDATE billing_cycles SET status = 'closed' WHERE id = ?",
            (cycle_id,),
        )
        await self.db.commit()

    async def get_billing_history(self, limit: int = 24) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM billing_cycles ORDER BY cycle_start DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ── Accounting Events ───────────────────────────────────

    async def store_accounting_event(
        self,
        event_type: str,
        energy_wh: int,
        cost_cents: int | None = None,
        rate_cents: int | None = None,
        cost_basis_cents: int | None = None,
        profit_loss_cents: int | None = None,
        billing_cycle_id: int | None = None,
        plan_id: int | None = None,
        notes: str | None = None,
    ) -> int:
        now = _now()
        async with self.db.execute(
            """INSERT INTO accounting_events
               (event_type, started_at, energy_wh, cost_cents, rate_cents,
                cost_basis_cents, profit_loss_cents, billing_cycle_id, plan_id, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_type, now, energy_wh, cost_cents, rate_cents,
                cost_basis_cents, profit_loss_cents, billing_cycle_id, plan_id, notes,
            ),
        ) as cursor:
            row_id = cursor.lastrowid
        await self.db.commit()
        return row_id  # type: ignore[return-value]

    async def get_accounting_events_since(self, cutoff_iso: str) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM accounting_events WHERE started_at >= ? ORDER BY started_at DESC",
            (cutoff_iso,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_daily_accounting(self, cutoff_iso: str) -> list[dict[str, Any]]:
        async with self.db.execute(
            """SELECT date(started_at) as day,
                      SUM(CASE WHEN event_type = 'import' THEN cost_cents ELSE 0 END) as import_cents,
                      SUM(CASE WHEN event_type = 'export' THEN cost_cents ELSE 0 END) as export_cents,
                      SUM(CASE WHEN event_type = 'self_consumption' THEN cost_cents ELSE 0 END) as self_consumption_cents,
                      SUM(profit_loss_cents) as arbitrage_cents,
                      COUNT(*) as event_count
               FROM accounting_events
               WHERE started_at >= ?
               GROUP BY date(started_at)
               ORDER BY day""",
            (cutoff_iso,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_forecast_age_seconds(self, provider_type: str) -> float | None:
        """Get seconds since last forecast fetch for a provider type."""
        row = await self.get_latest_forecast(provider_type)
        if not row or not row.get("fetched_at"):
            return None
        from datetime import datetime, timezone
        fetched = datetime.fromisoformat(row["fetched_at"])
        age = (datetime.now(timezone.utc) - fetched).total_seconds()
        return age

    # ── System Events ───────────────────────────────────────

    async def log_system_event(
        self,
        event_type: str,
        source_module: str,
        details: dict[str, Any],
        operating_mode: str,
        severity: str = "info",
    ) -> int:
        now = _now()
        async with self.db.execute(
            """INSERT INTO system_events
               (occurred_at, event_type, severity, source_module, details_json, operating_mode)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (now, event_type, severity, source_module, json.dumps(details), operating_mode),
        ) as cursor:
            row_id = cursor.lastrowid
        await self.db.commit()
        return row_id  # type: ignore[return-value]

    # ── Optimisation Cycle Log ──────────────────────────────

    async def log_optimisation_cycle(
        self,
        trigger_reason: str,
        rebuild_performed: bool,
        soc_at_evaluation: float,
        active_constraints: list[str],
        reserve_state: dict[str, Any],
        forecast_delta: dict[str, Any],
        outcome: str,
        plan_version: int | None = None,
        objective_score: float | None = None,
        solver_time_ms: int | None = None,
    ) -> int:
        now = _now()
        async with self.db.execute(
            """INSERT INTO optimisation_cycle_log
               (cycle_at, plan_version, trigger_reason, rebuild_performed,
                objective_score, active_constraints, reserve_state_json,
                forecast_delta_json, soc_at_evaluation, solver_time_ms, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now, plan_version, trigger_reason,
                1 if rebuild_performed else 0,
                objective_score, json.dumps(active_constraints),
                json.dumps(reserve_state), json.dumps(forecast_delta),
                soc_at_evaluation, solver_time_ms, outcome,
            ),
        ) as cursor:
            row_id = cursor.lastrowid
        await self.db.commit()
        return row_id  # type: ignore[return-value]

    # ── Historical Data ─────────────────────────────────────

    async def store_historical(
        self,
        data_type: str,
        value: float,
        source: str,
        recorded_at: str | None = None,
        resolution: str = "30min",
    ) -> None:
        ts = recorded_at or _now()
        await self.db.execute(
            """INSERT OR REPLACE INTO historical_data (data_type, recorded_at, value, source, resolution)
               VALUES (?, ?, ?, ?, ?)""",
            (data_type, ts, value, source, resolution),
        )
        await self.db.commit()

    async def get_historical(
        self,
        data_type: str,
        start: str,
        end: str,
        resolution: str | None = None,
    ) -> list[dict[str, Any]]:
        query = """SELECT * FROM historical_data
                   WHERE data_type = ? AND recorded_at >= ? AND recorded_at <= ?"""
        params: list[Any] = [data_type, start, end]
        if resolution:
            query += " AND resolution = ?"
            params.append(resolution)
        query += " ORDER BY recorded_at"
        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_latest_historical_value(self, data_type: str) -> float | None:
        """Get the most recent value for a historical data series."""
        async with self.db.execute(
            """SELECT value
               FROM historical_data
               WHERE data_type = ?
               ORDER BY recorded_at DESC
               LIMIT 1""",
            (data_type,),
        ) as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row else None

    # ── Spike Events ────────────────────────────────────────

    async def store_spike_event(
        self,
        peak_price_cents: int,
        trigger_price_cents: int,
        response_mode: str,
    ) -> int:
        now = _now()
        async with self.db.execute(
            """INSERT INTO spike_events
               (started_at, peak_price_cents, trigger_price_cents, response_mode)
               VALUES (?, ?, ?, ?)""",
            (now, peak_price_cents, trigger_price_cents, response_mode),
        ) as cursor:
            row_id = cursor.lastrowid
        await self.db.commit()
        return row_id  # type: ignore[return-value]

    async def get_active_spike(self) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT * FROM spike_events WHERE status = 'active' ORDER BY started_at DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    # ── BOM Locations ───────────────────────────────────────

    async def store_bom_locations(
        self, state_code: str, locations: list[dict[str, str]]
    ) -> None:
        await self.db.execute(
            "DELETE FROM bom_locations WHERE state_code = ?", (state_code,)
        )
        for loc in locations:
            await self.db.execute(
                """INSERT INTO bom_locations (state_code, aac, description, selected)
                   VALUES (?, ?, ?, 0)""",
                (state_code, loc["aac"], loc["description"]),
            )
        await self.db.commit()

    async def get_bom_locations(self, state_code: str) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM bom_locations WHERE state_code = ? ORDER BY description",
            (state_code,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def select_bom_location(self, state_code: str, aac: str) -> None:
        await self.db.execute(
            "UPDATE bom_locations SET selected = 0 WHERE state_code = ?",
            (state_code,),
        )
        await self.db.execute(
            "UPDATE bom_locations SET selected = 1 WHERE state_code = ? AND aac = ?",
            (state_code, aac),
        )
        await self.db.commit()

    # ── Key-Value Store ──────────────────────────────────

    # ── Application Logs ────────────────────────────────

    async def store_log(
        self,
        recorded_at: str,
        level: str,
        logger_name: str,
        message: str,
    ) -> None:
        await self.db.execute(
            """INSERT INTO application_logs (recorded_at, level, logger_name, message)
               VALUES (?, ?, ?, ?)""",
            (recorded_at, level, logger_name, message),
        )

    async def get_logs_since(
        self,
        cutoff_iso: str,
        level: str | None = None,
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        if level:
            query = """SELECT * FROM application_logs
                       WHERE recorded_at >= ? AND level = ?
                       ORDER BY recorded_at DESC LIMIT ?"""
            params: list[Any] = [cutoff_iso, level.upper(), limit]
        else:
            query = """SELECT * FROM application_logs
                       WHERE recorded_at >= ?
                       ORDER BY recorded_at DESC LIMIT ?"""
            params = [cutoff_iso, limit]
        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def prune_logs(self, cutoff_iso: str) -> int:
        cursor = await self.db.execute(
            "DELETE FROM application_logs WHERE recorded_at < ?",
            (cutoff_iso,),
        )
        count = cursor.rowcount
        await self.db.commit()
        return count

    # ── Key-Value Store ──────────────────────────────────

    async def kv_get(self, key: str) -> Any | None:
        """Get a value from the key-value store. Returns None if not found."""
        async with self.db.execute(
            "SELECT value_json FROM kv_store WHERE key = ?", (key,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return json.loads(row[0])

    async def kv_set(self, key: str, value: Any) -> None:
        """Set a value in the key-value store (upsert)."""
        await self.db.execute(
            """INSERT INTO kv_store (key, value_json, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,
                                              updated_at = excluded.updated_at""",
            (key, json.dumps(value), _now()),
        )
        await self.db.commit()
