"""SQL table definitions for all 17 tables."""

SCHEMA_VERSION = 1

TABLES = [
    # ── Config ──────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS config_versions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        config_json     TEXT NOT NULL,
        changed_keys    TEXT,
        created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        source          TEXT NOT NULL DEFAULT 'user'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_config_versions_created ON config_versions(created_at)",

    # ── Forecasts ───────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS forecast_snapshots (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_type       TEXT NOT NULL,
        provider_name       TEXT NOT NULL,
        fetched_at          TEXT NOT NULL,
        horizon_start       TEXT NOT NULL,
        horizon_end         TEXT NOT NULL,
        data_json           TEXT NOT NULL,
        solar_estimate_json TEXT,
        confidence_score    REAL,
        storm_probability   REAL,
        storm_window_start  TEXT,
        storm_window_end    TEXT,
        status              TEXT NOT NULL DEFAULT 'ok'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_forecast_fetched ON forecast_snapshots(fetched_at)",
    "CREATE INDEX IF NOT EXISTS idx_forecast_type ON forecast_snapshots(provider_type, fetched_at)",

    # ── Tariffs ─────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS tariff_schedules (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_name   TEXT NOT NULL,
        effective_from  TEXT NOT NULL,
        effective_until TEXT,
        schedule_json   TEXT NOT NULL,
        fetched_at      TEXT NOT NULL,
        version         INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tariff_effective ON tariff_schedules(effective_from)",

    # ── Optimisation Plans ──────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS optimisation_plans (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        version                 INTEGER NOT NULL,
        created_at              TEXT NOT NULL,
        trigger_reason          TEXT NOT NULL,
        horizon_start           TEXT NOT NULL,
        horizon_end             TEXT NOT NULL,
        objective_score         REAL NOT NULL,
        solver_time_ms          INTEGER NOT NULL,
        status                  TEXT NOT NULL DEFAULT 'active',
        metrics_json            TEXT NOT NULL,
        forecast_snapshot_id    INTEGER,
        tariff_schedule_id      INTEGER,
        config_version_id       INTEGER,
        active_constraints_json TEXT NOT NULL,
        reserve_state_json      TEXT,
        FOREIGN KEY (forecast_snapshot_id) REFERENCES forecast_snapshots(id),
        FOREIGN KEY (tariff_schedule_id) REFERENCES tariff_schedules(id),
        FOREIGN KEY (config_version_id) REFERENCES config_versions(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_plans_version ON optimisation_plans(version)",
    "CREATE INDEX IF NOT EXISTS idx_plans_status ON optimisation_plans(status)",
    "CREATE INDEX IF NOT EXISTS idx_plans_created ON optimisation_plans(created_at)",

    # ── Plan Slots ──────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS plan_slots (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id             INTEGER NOT NULL,
        slot_index          INTEGER NOT NULL,
        slot_start          TEXT NOT NULL,
        slot_end            TEXT NOT NULL,
        operating_mode      INTEGER NOT NULL,
        target_power_w      INTEGER NOT NULL,
        expected_soc        REAL NOT NULL,
        import_rate_cents   INTEGER NOT NULL,
        export_rate_cents   INTEGER NOT NULL,
        solar_forecast_w    INTEGER NOT NULL,
        load_forecast_w     INTEGER NOT NULL,
        scheduled_loads_json TEXT,
        constraint_flags    TEXT,
        FOREIGN KEY (plan_id) REFERENCES optimisation_plans(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_slots_plan ON plan_slots(plan_id, slot_index)",

    # ── Inverter Commands ───────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS inverter_commands (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        issued_at       TEXT NOT NULL,
        command_type    TEXT NOT NULL,
        parameters_json TEXT NOT NULL,
        source_plan_id  INTEGER,
        source_reason   TEXT NOT NULL,
        result          TEXT NOT NULL DEFAULT 'pending',
        response_json   TEXT,
        latency_ms      INTEGER,
        FOREIGN KEY (source_plan_id) REFERENCES optimisation_plans(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_commands_issued ON inverter_commands(issued_at)",

    # ── Telemetry ───────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS telemetry (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at     TEXT NOT NULL,
        soc             REAL NOT NULL,
        battery_power_w INTEGER NOT NULL,
        solar_power_w   INTEGER NOT NULL,
        grid_power_w    INTEGER NOT NULL,
        load_power_w    INTEGER NOT NULL,
        battery_voltage REAL,
        battery_temp_c  REAL,
        inverter_mode   TEXT,
        grid_available  INTEGER NOT NULL DEFAULT 1,
        raw_data_json   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_telemetry_time ON telemetry(recorded_at)",

    # ── Accounting Events ───────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS accounting_events (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type          TEXT NOT NULL,
        started_at          TEXT NOT NULL,
        ended_at            TEXT,
        energy_wh           INTEGER NOT NULL,
        cost_cents          INTEGER,
        rate_cents           INTEGER,
        cost_basis_cents    INTEGER,
        profit_loss_cents   INTEGER,
        billing_cycle_id    INTEGER,
        plan_id             INTEGER,
        notes               TEXT,
        FOREIGN KEY (billing_cycle_id) REFERENCES billing_cycles(id),
        FOREIGN KEY (plan_id) REFERENCES optimisation_plans(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_billing ON accounting_events(billing_cycle_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON accounting_events(event_type, started_at)",

    # ── Billing Cycles ──────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS billing_cycles (
        id                                  INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_start                         TEXT NOT NULL,
        cycle_end                           TEXT NOT NULL,
        status                              TEXT NOT NULL DEFAULT 'active',
        total_import_cost_cents             INTEGER NOT NULL DEFAULT 0,
        total_export_revenue_cents          INTEGER NOT NULL DEFAULT 0,
        total_arbitrage_profit_cents        INTEGER NOT NULL DEFAULT 0,
        total_self_consumption_value_cents  INTEGER NOT NULL DEFAULT 0,
        total_storm_opportunity_cost_cents  INTEGER NOT NULL DEFAULT 0,
        total_fixed_costs_cents             INTEGER NOT NULL DEFAULT 0,
        net_cost_cents                      INTEGER NOT NULL DEFAULT 0,
        projected_outcome_cents             INTEGER,
        summary_json                        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_billing_status ON billing_cycles(status)",

    # ── Scheduled Loads ─────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS scheduled_loads (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        name                TEXT NOT NULL,
        adapter_type        TEXT NOT NULL,
        adapter_config_json TEXT NOT NULL,
        power_w             INTEGER NOT NULL,
        duration_minutes    INTEGER NOT NULL,
        priority_class      INTEGER NOT NULL DEFAULT 5,
        enabled             INTEGER NOT NULL DEFAULT 1,
        earliest_start      TEXT,
        latest_end          TEXT,
        days_of_week        TEXT,
        prefer_solar        INTEGER NOT NULL DEFAULT 1,
        config_json         TEXT
    )
    """,

    # ── Load Execution Log ──────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS load_execution_log (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        load_id             INTEGER NOT NULL,
        plan_id             INTEGER,
        scheduled_start     TEXT NOT NULL,
        scheduled_end       TEXT NOT NULL,
        actual_start        TEXT,
        actual_end          TEXT,
        status              TEXT NOT NULL DEFAULT 'scheduled',
        assigned_slots_json TEXT,
        energy_consumed_wh  INTEGER,
        cost_cents          INTEGER,
        reason              TEXT,
        FOREIGN KEY (load_id) REFERENCES scheduled_loads(id),
        FOREIGN KEY (plan_id) REFERENCES optimisation_plans(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_load_exec ON load_execution_log(load_id, scheduled_start)",

    # ── System Events ───────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS system_events (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        occurred_at     TEXT NOT NULL,
        event_type      TEXT NOT NULL,
        severity        TEXT NOT NULL DEFAULT 'info',
        source_module   TEXT NOT NULL,
        details_json    TEXT NOT NULL,
        operating_mode  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sysevents_time ON system_events(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_sysevents_type ON system_events(event_type)",

    # ── Optimisation Cycle Log ──────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS optimisation_cycle_log (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_at            TEXT NOT NULL,
        plan_version        INTEGER,
        trigger_reason      TEXT NOT NULL,
        rebuild_performed   INTEGER NOT NULL,
        objective_score     REAL,
        active_constraints  TEXT NOT NULL,
        reserve_state_json  TEXT NOT NULL,
        forecast_delta_json TEXT NOT NULL,
        soc_at_evaluation   REAL NOT NULL,
        solver_time_ms      INTEGER,
        outcome             TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cycle_log_time ON optimisation_cycle_log(cycle_at)",

    # ── Historical Data ─────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS historical_data (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        data_type   TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        value       REAL NOT NULL,
        source      TEXT NOT NULL,
        resolution  TEXT NOT NULL DEFAULT '30min'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hist_type_time ON historical_data(data_type, recorded_at)",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_hist_dedup
       ON historical_data(data_type, recorded_at, source)""",

    # ── Load Profile Estimates ──────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS load_profile_estimates (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        day_of_week     INTEGER NOT NULL,
        slot_index      INTEGER NOT NULL,
        avg_load_w      INTEGER NOT NULL,
        source          TEXT NOT NULL,
        sample_count    INTEGER NOT NULL DEFAULT 0,
        updated_at      TEXT NOT NULL
    )
    """,
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_load_profile_dow_slot
       ON load_profile_estimates(day_of_week, slot_index)""",

    # ── BOM Locations ───────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS bom_locations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        state_code  TEXT NOT NULL,
        aac         TEXT NOT NULL,
        description TEXT NOT NULL,
        selected    INTEGER NOT NULL DEFAULT 0
    )
    """,
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_bom_loc_aac
       ON bom_locations(state_code, aac)""",

    # ── Spike Events ────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS spike_events (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at          TEXT NOT NULL,
        ended_at            TEXT,
        peak_price_cents    INTEGER NOT NULL,
        trigger_price_cents INTEGER NOT NULL,
        response_mode       TEXT NOT NULL,
        energy_discharged_wh INTEGER NOT NULL DEFAULT 0,
        revenue_cents       INTEGER NOT NULL DEFAULT 0,
        loads_deferred      TEXT,
        status              TEXT NOT NULL DEFAULT 'active'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_spike_time ON spike_events(started_at)",

    # ── Schema version tracking ─────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        id      INTEGER PRIMARY KEY CHECK (id = 1),
        version INTEGER NOT NULL
    )
    """,
]
