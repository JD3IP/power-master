"""Power Master application entry point and lifecycle orchestrator.

Startup sequence:
  config → SQLite → Fox-ESS connect → providers → aggregator →
  resilience → storm → accounting → loads → MQTT → HA discovery →
  initial forecast → initial plan → control loop → dashboard
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from power_master.config.manager import ConfigManager
from power_master.config.schema import AppConfig
from power_master.control.constants import (
    CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    DB_STORE_INTERVAL_SECONDS,
    HISTORY_FLUSH_INTERVAL_SECONDS,
    LOAD_POLL_INTERVAL_SECONDS,
    RECONNECT_INTERVAL_SECONDS,
    TELEMETRY_BUFFER_FLUSH_SECONDS,
    TELEMETRY_BUFFER_MAX_RECORDS,
)
from power_master.db.engine import close_db, init_db
from power_master.db.repository import Repository
from power_master.logging.structured import setup_logging
from power_master.timezone_utils import resolve_timezone

logger = logging.getLogger(__name__)

from power_master import __version__ as VERSION


# ── Module-level helpers for executor tasks ──────────────────────────

def _fit_solar_model_sync(
    samples: list, system_peak_w: float, tz_name: str, trained_at: datetime | None = None,
):
    """Non-async wrapper for solar calibration fitting (runs in thread pool)."""
    from power_master.forecast.solar_calibration import fit_calibration_model
    return fit_calibration_model(
        samples,
        system_peak_w=system_peak_w,
        tz_name=tz_name,
        trained_at=trained_at,
    )


class Application:
    """Main application lifecycle manager.

    Wires all modules together and manages startup/shutdown ordering.
    """

    def __init__(self, config: AppConfig, config_manager: ConfigManager) -> None:
        self.config: AppConfig = config
        self.config_manager: ConfigManager = config_manager
        self._running: bool = False
        self._tasks: list[asyncio.Task] = []
        self._stop_event: asyncio.Event = asyncio.Event()

        # References held for cleanup
        self._adapter: Any = None
        self._mqtt_client: Any = None
        self._control_loop: Any = None
        self._providers: list[Any] = []  # providers with .close() methods
        self._server: Any = None
        self._load_manager: Any = None
        self._repo: Any = None
        self._db_log_handler: Any = None

        # Solar calibration model (lazy-fitted, refreshed on stale)
        self._solar_calibration_model: Any = None
        self._solar_calibration_last_fit: datetime | None = None

        # Telemetry batching
        self._telemetry_buffer: list[dict] = []
        self._telemetry_buffer_lock: asyncio.Lock = asyncio.Lock()
        self._telemetry_buffer_flush_time: float = 0.0

        # Notification correlation state for open-state incidents
        self._spike_correlation_id: str | None = None
        self._spike_deferred_loads: list[str] = []
        self._grid_outage_correlation_id: str | None = None
        self._grid_outage_since: datetime | None = None
        self._storm_correlation_id: str | None = None
        self._storm_was_active: bool = False
        self._force_charge_last_emitted_hour: str | None = None

        # Concurrency locks for shared mutable state between forecast and control loops
        self._plan_lock: asyncio.Lock = asyncio.Lock()  # guards self._control_loop.state.current_plan
        self._storm_lock: asyncio.Lock = asyncio.Lock()  # guards _storm_was_active, _storm_correlation_id, _storm_detected_at
        self._model_lock: asyncio.Lock = asyncio.Lock()  # guards _solar_calibration_model, _solar_calibration_last_fit

    async def start(self) -> None:
        """Start all application components in dependency order."""
        logger.info("Starting Power Master v%s", VERSION)
        self._running = True
        self._stop_event.clear()

        # ── 0. Ensure auth session secret exists ──────────────
        auth_cfg = self.config.dashboard.auth
        if auth_cfg.users and not auth_cfg.session_secret:
            import secrets as _secrets

            generated = _secrets.token_hex(32)
            self.config_manager.save_user_config(
                {"dashboard": {"auth": {"session_secret": generated}}}
            )
            self.config = self.config_manager.load()
            logger.info("Generated and persisted session secret for dashboard auth")

        # ── 1. Database ──────────────────────────────────────
        db = await init_db(self.config.db.path)
        repo = Repository(db)
        await repo.check_integrity()
        await self.config_manager.save_version(db)
        logger.info("Database initialised")

        # ── 1b. DB log handler ─────────────────────────────────
        # Ensure application_logs table exists (handles existing DBs pre-migration)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS application_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                level       TEXT NOT NULL,
                logger_name TEXT NOT NULL,
                message     TEXT NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_app_logs_time ON application_logs(recorded_at)")
        await db.commit()


        # ── 2. Hardware adapter ──────────────────────────────
        adapter = await self._create_adapter()
        self._adapter = adapter

        # ── 3. Providers ─────────────────────────────────────
        solar_provider, weather_provider, storm_provider, tariff_provider = (
            self._create_providers()
        )

        # ── 4. Forecast aggregator ───────────────────────────
        from power_master.forecast.aggregator import ForecastAggregator
        from power_master.tariff.spike import SpikeDetector

        spike_detector = SpikeDetector(
            spike_threshold_cents=self.config.arbitrage.spike_threshold_cents,
        )
        aggregator = ForecastAggregator(
            solar_provider=solar_provider,
            weather_provider=weather_provider,
            storm_provider=storm_provider,
            tariff_provider=tariff_provider,
            spike_detector=spike_detector,
        )

        # ── 5. Health checker + resilience ───────────────────
        from power_master.resilience.health_check import HealthChecker
        from power_master.resilience.manager import ResilienceManager

        health_checker = HealthChecker(
            max_consecutive_failures=self.config.resilience.max_consecutive_failures,
        )
        health_checker.register("inverter")
        health_checker.register("solar_forecast")
        health_checker.register("weather_forecast")
        health_checker.register("tariff")
        if self.config.storm.enabled:
            health_checker.register("storm")

        resilience_mgr = ResilienceManager(self.config, health_checker)

        # ── 6. Storm monitor ─────────────────────────────────
        from power_master.storm.monitor import StormMonitor

        storm_monitor = StormMonitor(self.config.storm)

        # ── 7. Accounting engine ─────────────────────────────
        from power_master.accounting.engine import AccountingEngine

        accounting = AccountingEngine(self.config)
        await accounting.init_persistence(repo)

        # ── 7b. Notification event bus + manager ───────────────
        from power_master.notifications.bus import EventBus
        from power_master.notifications.manager import NotificationManager

        event_bus = EventBus()
        notification_manager = NotificationManager(self.config.notifications, event_bus, repo=repo)

        # Wire log forwarding if notifications enabled
        if self.config.notifications.enabled:
            from power_master.notifications.manager import NotificationLogHandler
            log_handler = NotificationLogHandler(
                event_bus, min_level=self.config.notifications.log_min_level,
            )
            logging.getLogger().addHandler(log_handler)

        # ── 8. Rebuild evaluator ─────────────────────────────
        from power_master.optimisation.rebuild_evaluator import RebuildEvaluator

        rebuild_evaluator = RebuildEvaluator(self.config)

        # ── 9. Load manager ──────────────────────────────────
        from power_master.loads.manager import LoadManager

        load_manager = LoadManager(self.config)
        self._register_loads(load_manager)
        self._load_manager = load_manager
        self._repo = repo
        await load_manager.restore_daily_runtime(repo)

        # ── 10. MQTT ─────────────────────────────────────────
        mqtt_client = None
        mqtt_publisher = None
        if self.config.mqtt.enabled:
            mqtt_client, mqtt_publisher = await self._setup_mqtt(load_manager)
            self._mqtt_client = mqtt_client

        # ── 11. History collector ────────────────────────────
        from power_master.history.collector import HistoryCollector

        history = HistoryCollector(repo)

        # ── 11b. Initial history backfill ─────────────────
        from power_master.history.loader import HistoryLoader

        try:
            history_loader = HistoryLoader(repo)
            backfill_results = await history_loader.backfill_all(
                tariff_provider=tariff_provider,
                weather_provider=weather_provider,
            )
            if backfill_results:
                logger.info("History backfill complete: %s", backfill_results)
        except Exception:
            logger.warning("History backfill failed (non-critical)", exc_info=True)

        # ── 12. Control loop ─────────────────────────────────
        from power_master.control.anti_oscillation import AntiOscillationGuard
        from power_master.control.loop import ControlLoop
        from power_master.control.manual_override import ManualOverride

        # Try to restore persisted override (if < 60 min old)
        override_path = Path(self.config.db.path).parent / "manual_override.json"
        manual_override = ManualOverride.load(override_path)
        if manual_override is None:
            manual_override = ManualOverride()
        anti_oscillation = AntiOscillationGuard(self.config.anti_oscillation)

        control_loop = ControlLoop(
            config=self.config,
            adapter=adapter,
            manual_override=manual_override,
            anti_oscillation=anti_oscillation,
            repo=self._repo,
        )
        self._control_loop = control_loop

        # Wrap manual override set/clear to persist state
        original_set = manual_override.set
        original_clear = manual_override.clear

        def _set_and_persist(
            mode, power_w=0, timeout_seconds=None, source="user",
        ) -> None:
            original_set(mode, power_w=power_w, timeout_seconds=timeout_seconds, source=source)
            manual_override.save(override_path)

        def _clear_and_persist(reason: str = "user") -> None:
            original_clear(reason)
            try:
                override_path.unlink(missing_ok=True)
            except Exception:
                logger.debug("Failed to delete persisted override file", exc_info=True)

        manual_override.set = _set_and_persist
        manual_override.clear = _clear_and_persist

        # Register telemetry callback: buffer for DB + history + MQTT
        async def on_telemetry(telemetry):
            # Buffer telemetry for batch commit (10 records or 30s)
            async with self._telemetry_buffer_lock:
                self._telemetry_buffer.append({
                    "soc": telemetry.soc,
                    "battery_power_w": telemetry.battery_power_w,
                    "solar_power_w": telemetry.solar_power_w,
                    "grid_power_w": telemetry.grid_power_w,
                    "load_power_w": telemetry.load_power_w,
                    "battery_voltage": telemetry.battery_voltage,
                    "battery_temp_c": telemetry.battery_temp_c,
                    "inverter_mode": telemetry.inverter_mode,
                    "grid_available": telemetry.grid_available,
                    "raw_data": telemetry.raw_data,
                })
                # Flush if buffer hits max records threshold
                if len(self._telemetry_buffer) >= TELEMETRY_BUFFER_MAX_RECORDS:
                    try:
                        await repo.store_telemetry_batch(self._telemetry_buffer)
                        self._telemetry_buffer.clear()
                        self._telemetry_buffer_flush_time = time.monotonic()
                    except Exception:
                        logger.exception("Failed to flush telemetry batch")

            # Buffer for history aggregation
            try:
                history.record_telemetry(telemetry)
            except Exception:
                logger.exception("Failed to record telemetry to history")

            # Record energy flows for accounting (power × interval → Wh)
            # NOTE: sync_soc is called AFTER energy recording so that
            # record_charge sees the previous tick's stored_wh, not the
            # already-updated SOC (which would double-count charged energy
            # and anchor the WACB at its initial value).
            tick_interval_s = self.config.planning.evaluation_interval_seconds
            tick_hours = tick_interval_s / 3600.0
            tariff = getattr(aggregator.state, "tariff", None)
            import_rate = 15.0  # default cents/kWh
            export_rate = 5.0
            if tariff:
                ip = tariff.get_current_import_price()
                ep = tariff.get_current_export_price()
                if ip is not None:
                    import_rate = ip
                if ep is not None:
                    export_rate = ep

            grid_w = telemetry.grid_power_w
            solar_w = telemetry.solar_power_w
            battery_w = telemetry.battery_power_w  # positive=charging
            load_w = telemetry.load_power_w

            if grid_w > 0:
                # Grid import
                import_wh = round(grid_w * tick_hours)
                if import_wh > 0:
                    await accounting.record_grid_import(import_wh, import_rate)
                # If battery is also charging from grid
                if battery_w > 0:
                    grid_charge_w = min(battery_w, grid_w)
                    grid_charge_wh = round(grid_charge_w * tick_hours)
                    if grid_charge_wh > 0:
                        accounting.record_grid_charge(grid_charge_wh, import_rate)
            elif grid_w < 0:
                # Grid export
                export_wh = round(abs(grid_w) * tick_hours)
                if export_wh > 0:
                    await accounting.record_grid_export(export_wh, export_rate)

            # Solar charging battery
            if battery_w > 0 and solar_w > 0 and grid_w <= 0:
                solar_charge_w = min(battery_w, solar_w)
                solar_charge_wh = round(solar_charge_w * tick_hours)
                if solar_charge_wh > 0:
                    accounting.record_solar_charge(solar_charge_wh, export_rate)

            # Self-consumption: load served by solar/battery (not grid)
            if load_w > 0 and grid_w <= 0:
                self_consumption_w = min(load_w, solar_w + max(0, -battery_w))
                self_consumption_wh = round(self_consumption_w * tick_hours)
                if self_consumption_wh > 0:
                    await accounting.record_self_consumption(self_consumption_wh, import_rate)

            # Sync accounting SOC after energy recording (see note above)
            try:
                accounting.sync_soc(telemetry.soc)
            except Exception:
                logger.exception("Failed to sync accounting SOC")

            # Notification: battery SOC thresholds
            if self.config.notifications.enabled:
                from power_master.notifications.bus import Event as _NEvent
                soc = telemetry.soc
                if soc <= self.config.notifications.battery_low_threshold:
                    await event_bus.publish(_NEvent(
                        name="battery_low",
                        severity="warning",
                        title="Battery Low",
                        message=f"Battery SOC is {soc*100:.0f}%",
                        data={"soc": soc},
                    ))
                elif soc >= self.config.notifications.battery_full_threshold:
                    await event_bus.publish(_NEvent(
                        name="battery_full",
                        severity="info",
                        title="Battery Full",
                        message=f"Battery SOC is {soc*100:.0f}%",
                        data={"soc": soc},
                    ))

            # Health check: inverter success
            health_checker.record_success("inverter")
            # Publish to MQTT
            if mqtt_publisher:
                await mqtt_publisher.publish_telemetry(telemetry)
            # Load shedding: shed loads if grid import exceeds configured max
            max_grid_w = self.config.battery.max_grid_import_w
            if max_grid_w > 0 and telemetry.grid_power_w > max_grid_w:
                await load_manager.shed_for_overload(
                    telemetry.grid_power_w, max_grid_w,
                )

        control_loop._on_telemetry.append(on_telemetry)

        # SOC deviation fast-check: flag the forecast loop when SOC drifts
        self._soc_rebuild_needed = asyncio.Event()

        async def on_telemetry_soc_check(telemetry):
            """Check SOC deviation on every control tick for faster response."""
            plan = control_loop.state.current_plan
            if plan is None:
                return
            result = rebuild_evaluator.evaluate(plan, telemetry.soc, aggregator)
            if result.should_rebuild and result.trigger == "soc_deviation":
                logger.info("SOC deviation detected on tick: %s", result.reason)
                self._soc_rebuild_needed.set()

        control_loop._on_telemetry.append(on_telemetry_soc_check)

        # ── 13. Load cached forecasts + initial fetch ─────────
        logger.info("Loading cached forecasts from DB...")
        await aggregator.load_from_db(repo)

        logger.info("Fetching initial forecasts (skipping fresh data)...")
        await aggregator.update_all(
            config=self.config.providers, respect_validity=True,
        )

        # Fetch one telemetry reading so the initial plan uses real SOC
        # (without this, _run_solver defaults to 50% because the control
        # loop / poll tasks haven't started yet).
        try:
            init_telemetry = await adapter.get_telemetry()
            control_loop.update_live_telemetry(init_telemetry)
            logger.info(
                "Initial telemetry: SOC=%.1f%%, PV=%dW, Grid=%dW, Load=%dW",
                init_telemetry.soc * 100,
                init_telemetry.solar_power_w,
                init_telemetry.grid_power_w,
                init_telemetry.load_power_w,
            )
        except Exception:
            logger.warning(
                "Could not fetch telemetry before initial plan — "
                "solver will use SOC=50%% default",
                exc_info=True,
            )

        # Always build an initial plan so startup does not reuse stale plan data.
        # If tariff data is missing, _run_solver falls back to default rates.
        await self._run_solver(
            aggregator, storm_monitor, accounting,
            rebuild_evaluator, control_loop, repo,
            trigger="initial", load_manager=load_manager,
        )

        logger.info(
            "System initialised: battery=%dWh, adapter=%s, horizon=%dh",
            self.config.battery.capacity_wh,
            self.config.hardware.adapter,
            self.config.planning.horizon_hours,
        )

        # ── 14. Start background tasks ───────────────────────

        # Control loop
        self._tasks.append(asyncio.create_task(
            control_loop.run(), name="control_loop",
        ))

        # Fast telemetry polling for live dashboard updates
        self._tasks.append(asyncio.create_task(
            self._telemetry_poll_loop(adapter, control_loop, repo=repo, load_manager=load_manager, event_bus=event_bus),
            name="telemetry_poller",
        ))

        # Periodic forecast updates
        self._tasks.append(asyncio.create_task(
            self._forecast_update_loop(
                aggregator, storm_monitor, health_checker,
                history, accounting, rebuild_evaluator,
                control_loop, repo, load_manager, mqtt_publisher,
                event_bus=event_bus,
            ),
            name="forecast_updater",
        ))

        # History flush (every 30 min)
        self._tasks.append(asyncio.create_task(
            self._history_flush_loop(history),
            name="history_flusher",
        ))

        # Telemetry buffer flush (every 30s)
        self._tasks.append(asyncio.create_task(
            self._telemetry_flush_loop(repo),
            name="telemetry_flusher",
        ))

        # Forecast samples retention prune (once per hour)
        self._tasks.append(asyncio.create_task(
            self._forecast_prune_loop(repo),
            name="forecast_prune",
        ))

        # Daily briefing + notification log retention
        self._tasks.append(asyncio.create_task(
            self._notification_maintenance_loop(repo, event_bus, control_loop, aggregator, storm_monitor),
            name="notification_maintenance",
        ))

        # Resilience evaluator
        self._tasks.append(asyncio.create_task(
            self._resilience_loop(resilience_mgr, event_bus=event_bus),
            name="resilience_evaluator",
        ))

        # MQTT listener (if enabled)
        if mqtt_client:
            self._tasks.append(asyncio.create_task(
                mqtt_client.listen(), name="mqtt_listener",
            ))

        # Publish initial online status
        if mqtt_publisher:
            await mqtt_publisher.publish_status(online=True)

        # ── 15. Self-update manager ───────────────────────────
        from power_master.updater import UpdateManager

        updater = UpdateManager(event_bus=event_bus)
        self._tasks.append(asyncio.create_task(
            updater.run(), name="update_checker",
        ))

        # ── 16. Dashboard server ─────────────────────────────
        from power_master.dashboard.app import create_app

        app = create_app(self.config, repo, config_manager=self.config_manager)
        # Store live references in app.state for dashboard access
        app.state.application = self
        app.state.control_loop = control_loop
        app.state.aggregator = aggregator
        app.state.accounting = accounting
        app.state.storm_monitor = storm_monitor
        app.state.load_manager = load_manager
        app.state.resilience_mgr = resilience_mgr
        app.state.manual_override = manual_override
        app.state.updater = updater
        app.state.event_bus = event_bus
        app.state.notification_manager = notification_manager
        app.state.adapter = adapter

        import uvicorn

        uvi_config = uvicorn.Config(
            app,
            host=self.config.dashboard.host,
            port=self.config.dashboard.port,
            log_level="warning",
        )
        server = uvicorn.Server(uvi_config)
        # Keep process signal handling in main() so Ctrl+C behaviour is predictable.
        server.install_signal_handlers = lambda: None
        self._server = server

        logger.info(
            "Dashboard available at http://%s:%d",
            self.config.dashboard.host,
            self.config.dashboard.port,
        )

        # Server.serve() blocks until shutdown
        await server.serve()

    async def stop(self) -> None:
        """Gracefully stop all components in reverse order."""
        if not self._running:
            return

        logger.info("Shutting down Power Master")
        self._running = False
        self._stop_event.set()

        # Tell uvicorn to exit its serve() loop.
        if self._server is not None:
            self._server.should_exit = True

        # Stop control loop
        if self._control_loop:
            self._control_loop.stop()

        # Flush telemetry buffer before shutdown
        if self._repo:
            async with self._telemetry_buffer_lock:
                if self._telemetry_buffer:
                    try:
                        await self._repo.store_telemetry_batch(self._telemetry_buffer)
                        self._telemetry_buffer.clear()
                    except Exception:
                        logger.exception("Failed to flush telemetry on shutdown")

        # Cancel background tasks
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # Publish offline status
        if self._mqtt_client:
            try:
                from power_master.mqtt.publisher import MQTTPublisher
                from power_master.mqtt.topics import build_topics

                topics = build_topics(self.config.mqtt.topic_prefix)
                await self._mqtt_client.publish(
                    topics["status"], "offline", retain=True,
                )
            except Exception:
                pass
            await self._mqtt_client.disconnect()

        # Disconnect hardware
        if self._adapter:
            try:
                await self._adapter.disconnect()
            except Exception:
                logger.exception("Error disconnecting adapter")

        # Close providers
        for provider in self._providers:
            if hasattr(provider, "close"):
                try:
                    await provider.close()
                except Exception:
                    pass

        # Persist load runtime before closing DB
        if self._load_manager and self._repo:
            try:
                await self._load_manager.persist_daily_runtime(self._repo)
            except Exception:
                logger.debug("Failed to persist load runtime on shutdown", exc_info=True)

        await close_db()
        self._server = None
        logger.info("Shutdown complete")

    # ── Hot-reload ────────────────────────────────────────────

    async def reload_config(self, updates: dict, app) -> None:
        """Apply config changes without restart.

        Saves updates to YAML, rebuilds the merged config, and
        hot-swaps any components whose config sections changed.
        """
        from power_master.config.schema import AppConfig

        # 1. Save to disk and rebuild config
        new_config = self.config_manager.save_user_config(updates)
        old_config = self.config
        self.config = new_config

        # 2. Update app.state so routes see new values immediately
        app.state.config = new_config

        # 3. Version the change in DB
        try:
            repo = app.state.repo
            await self.config_manager.save_version(repo._db, list(updates.keys()))
        except Exception:
            logger.warning("Failed to version config change (non-critical)")

        changed_sections = set(updates.keys())

        # 4. Hot-swap providers if their config changed
        if "providers" in changed_sections:
            await self._reload_providers(old_config, new_config, app)

        # 5. Hot-swap hardware adapter if config changed
        if "hardware" in changed_sections:
            await self._reload_adapter(app)

        # 6. Reconnect MQTT if broker settings changed
        if "mqtt" in changed_sections:
            await self._reload_mqtt(app)

        # 7. Rebuild notification channels if config changed
        if "notifications" in changed_sections:
            nm = getattr(app.state, "notification_manager", None)
            if nm is not None:
                nm.reload(new_config.notifications)

        logger.info("Config reloaded: changed=%s", list(changed_sections))

    async def _reload_providers(self, old_config, new_config, app) -> None:
        """Close old providers and create new ones."""
        # Close existing providers
        for provider in self._providers:
            if hasattr(provider, "close"):
                try:
                    await provider.close()
                except Exception:
                    pass
        self._providers.clear()

        # Create new providers with updated config
        solar, weather, storm, tariff = self._create_providers()

        # Update aggregator with new provider instances
        aggregator = getattr(app.state, "aggregator", None)
        if aggregator:
            aggregator.update_providers(
                solar_provider=solar,
                weather_provider=weather,
                storm_provider=storm,
                tariff_provider=tariff,
            )
        logger.info("Providers reloaded")

    async def _reload_adapter(self, app) -> None:
        """Disconnect old adapter and create new one."""
        if self._adapter:
            try:
                await self._adapter.disconnect()
            except Exception:
                pass

        adapter = await self._create_adapter()
        self._adapter = adapter

        control_loop = getattr(app.state, "control_loop", None)
        if control_loop:
            control_loop.update_adapter(adapter)
        logger.info("Hardware adapter reloaded")

    async def _reload_mqtt(self, app) -> None:
        """Reconnect MQTT with new settings."""
        if self._mqtt_client:
            try:
                await self._mqtt_client.disconnect()
            except Exception:
                pass
            self._mqtt_client = None

        if self.config.mqtt.enabled:
            load_manager = getattr(app.state, "load_manager", None)
            if load_manager:
                client, publisher = await self._setup_mqtt(load_manager)
                self._mqtt_client = client
                if publisher:
                    await publisher.publish_status(online=True)
        logger.info("MQTT reloaded")

    # ── Factory methods ──────────────────────────────────────

    async def _create_adapter(self):
        """Create and connect the hardware adapter."""
        from power_master.hardware.adapters.foxess import FoxESSAdapter

        adapter = FoxESSAdapter(self.config.hardware.foxess, max_export_w=self.config.battery.max_discharge_rate_w)
        if (
            self.config.hardware.foxess.watchdog_timeout_seconds
            <= self.config.planning.evaluation_interval_seconds
        ):
            logger.warning(
                "FoxESS watchdog_timeout_seconds (%d) is <= control tick interval (%d). "
                "Remote commands may expire before refresh.",
                self.config.hardware.foxess.watchdog_timeout_seconds,
                self.config.planning.evaluation_interval_seconds,
            )
        try:
            await adapter.connect()
            await adapter.read_firmware()
        except Exception:
            logger.warning(
                "Fox-ESS connection failed — running in degraded mode "
                "(no inverter telemetry or control). "
                "Poll loop will auto-reconnect every 30s.",
                exc_info=True,
            )
        return adapter

    def _create_providers(self):
        """Create forecast and tariff provider instances."""
        solar_provider = None
        weather_provider = None
        storm_provider = None
        tariff_provider = None

        # Solar (Forecast.Solar)
        from power_master.forecast.providers.forecast_solar import (
            ForecastSolarProvider,
        )

        solar_provider = ForecastSolarProvider(self.config.providers.solar)
        self._providers.append(solar_provider)
        logger.info("Solar provider: Forecast.Solar")

        # Weather (Open-Meteo)
        from power_master.forecast.providers.openmeteo import OpenMeteoProvider

        weather_provider = OpenMeteoProvider(self.config.providers.weather)
        self._providers.append(weather_provider)
        logger.info("Weather provider: Open-Meteo")

        # Storm (BOM)
        if self.config.storm.enabled and self.config.providers.storm.location_aac:
            from power_master.forecast.providers.bom_storm import BOMStormProvider

            storm_provider = BOMStormProvider(self.config.providers.storm)
            self._providers.append(storm_provider)
            logger.info("Storm provider: BOM (%s)", self.config.providers.storm.state_code)

        # Tariff (Amber)
        if self.config.providers.tariff.api_key:
            from power_master.tariff.providers.amber import AmberProvider

            tariff_provider = AmberProvider(self.config.providers.tariff)
            self._providers.append(tariff_provider)
            logger.info("Tariff provider: Amber Electric")

        return solar_provider, weather_provider, storm_provider, tariff_provider

    def _register_loads(self, load_manager):
        """Register configured Shelly and MQTT load controllers."""
        from power_master.loads.adapters.shelly import ShellyAdapter

        for device in self.config.loads.shelly_devices:
            if not device.enabled:
                continue
            adapter = ShellyAdapter(config=device)
            load_manager.register(adapter)

        # MQTT loads will be registered after MQTT client is ready
        logger.info(
            "Registered %d load controllers",
            len(load_manager.controllers),
        )

    async def _setup_mqtt(self, load_manager):
        """Set up MQTT client, publisher, discovery, and subscriber."""
        from power_master.mqtt.client import MQTTClient
        from power_master.mqtt.discovery import publish_discovery
        from power_master.mqtt.publisher import MQTTPublisher
        from power_master.mqtt.subscriber import LoadCommandSubscriber

        client = MQTTClient(self.config.mqtt)
        await client.connect()

        publisher = None
        if client.is_connected:
            publisher = MQTTPublisher(
                publish_fn=client.publish,
                topic_prefix=self.config.mqtt.topic_prefix,
            )

            # HA auto-discovery
            if self.config.mqtt.ha_discovery_enabled:
                count = await publish_discovery(
                    publish_fn=client.publish,
                    topic_prefix=self.config.mqtt.topic_prefix,
                    ha_prefix=self.config.mqtt.ha_discovery_prefix,
                )
                logger.info("Published %d HA discovery entities", count)

            # Subscribe to load commands
            subscriber = LoadCommandSubscriber(self.config.mqtt.topic_prefix)
            for load_id in load_manager.controllers:
                topic = subscriber.register_load(
                    load_id,
                    lambda payload, lid=load_id: logger.info(
                        "MQTT load command: %s → %s", lid, payload,
                    ),
                )
                client.subscribe(topic, subscriber.handle_message)

            # Register MQTT load adapters
            from power_master.loads.adapters.mqtt_load import MQTTLoadAdapter

            for endpoint in self.config.loads.mqtt_load_endpoints:
                if not endpoint.enabled:
                    continue
                mqtt_adapter = MQTTLoadAdapter(
                    config=endpoint,
                    publish_fn=client.publish,
                )
                load_manager.register(mqtt_adapter)
                # Subscribe for state updates
                client.subscribe(
                    endpoint.state_topic,
                    lambda t, p, ma=mqtt_adapter: ma.handle_state_update(p),
                )
        else:
            logger.warning("MQTT not connected — publisher disabled")

        return client, publisher

    # ── Background task loops ────────────────────────────────

    async def _forecast_update_loop(
        self, aggregator, storm_monitor, health_checker,
        history, accounting, rebuild_evaluator,
        control_loop, repo, load_manager, mqtt_publisher,
        event_bus=None,
    ):
        """Periodically update forecasts and trigger plan rebuilds."""
        tariff_interval = self.config.providers.tariff.update_interval_seconds
        solar_interval = self.config.providers.solar.update_interval_seconds
        weather_interval = self.config.providers.weather.update_interval_seconds
        storm_interval = self.config.providers.storm.update_interval_seconds

        # Track last update times
        last_tariff = 0.0
        last_solar = 0.0
        last_weather = 0.0
        last_storm = 0.0

        # Circuit breaker: track consecutive failures per provider
        provider_failures: dict[str, int] = {
            "tariff": 0,
            "solar": 0,
            "weather": 0,
            "storm": 0,
        }

        import time

        spike_was_active = False

        while not self._stop_event.is_set():
            try:
                now = time.monotonic()

                persist_enabled = self.config.providers.forecast_persistence_enabled
                horizons = self.config.providers.forecast_horizons_hours

                # Collect provider update tasks with timeouts and circuit breaker
                update_tasks = []
                update_keys = []

                # Tariff update
                if now - last_tariff >= tariff_interval:
                    if provider_failures["tariff"] < CIRCUIT_BREAKER_FAILURE_THRESHOLD:
                        async def _update_tariff():
                            try:
                                return await asyncio.wait_for(
                                    aggregator.update_tariff(), timeout=10.0
                                )
                            except asyncio.TimeoutError:
                                logger.warning("Tariff provider timeout")
                                return None
                        update_tasks.append(_update_tariff())
                        update_keys.append("tariff")
                    else:
                        logger.warning(
                            "Tariff provider disabled (5+ consecutive failures)"
                        )

                # Solar update
                if now - last_solar >= solar_interval:
                    if provider_failures["solar"] < CIRCUIT_BREAKER_FAILURE_THRESHOLD:
                        async def _update_solar():
                            try:
                                return await asyncio.wait_for(
                                    aggregator.update_solar(), timeout=10.0
                                )
                            except asyncio.TimeoutError:
                                logger.warning("Solar provider timeout")
                                return None
                        update_tasks.append(_update_solar())
                        update_keys.append("solar")
                    else:
                        logger.warning(
                            "Solar provider disabled (5+ consecutive failures)"
                        )

                # Weather update
                if now - last_weather >= weather_interval:
                    if provider_failures["weather"] < CIRCUIT_BREAKER_FAILURE_THRESHOLD:
                        async def _update_weather():
                            try:
                                return await asyncio.wait_for(
                                    aggregator.update_weather(), timeout=10.0
                                )
                            except asyncio.TimeoutError:
                                logger.warning("Weather provider timeout")
                                return None
                        update_tasks.append(_update_weather())
                        update_keys.append("weather")
                    else:
                        logger.warning(
                            "Weather provider disabled (5+ consecutive failures)"
                        )

                # Storm update
                if self.config.storm.enabled and now - last_storm >= storm_interval:
                    if provider_failures["storm"] < CIRCUIT_BREAKER_FAILURE_THRESHOLD:
                        async def _update_storm():
                            try:
                                return await asyncio.wait_for(
                                    aggregator.update_storm(), timeout=10.0
                                )
                            except asyncio.TimeoutError:
                                logger.warning("Storm provider timeout")
                                return None
                        update_tasks.append(_update_storm())
                        update_keys.append("storm")
                    else:
                        logger.warning(
                            "Storm provider disabled (5+ consecutive failures)"
                        )

                # Run all enabled provider updates in parallel
                if update_tasks:
                    results = await asyncio.gather(*update_tasks, return_exceptions=True)
                    for key, result in zip(update_keys, results):
                        if isinstance(result, Exception):
                            logger.exception("Provider update %s raised exception", key)
                            provider_failures[key] += 1
                        elif result is None:
                            provider_failures[key] += 1
                        else:
                            provider_failures[key] = 0  # Reset on success

                # Process tariff result (if task ran)
                if "tariff" in update_keys:
                    idx = update_keys.index("tariff")
                    result = results[idx]
                    last_tariff = now
                    if result and not isinstance(result, Exception):
                        health_checker.record_success("tariff")
                        try:
                            await history.record_price(result)
                        except Exception:
                            logger.exception("Failed to record tariff to history")
                        if persist_enabled:
                            try:
                                from power_master.forecast.persistence import persist_tariff_forecast
                                await persist_tariff_forecast(repo, result, horizons)
                            except Exception:
                                logger.exception("Tariff forecast persistence failed")
                    else:
                        health_checker.record_failure("tariff", "fetch failed")

                # Process solar result (if task ran)
                if "solar" in update_keys:
                    idx = update_keys.index("solar")
                    result = results[idx]
                    last_solar = now
                    if result and not isinstance(result, Exception):
                        health_checker.record_success("solar_forecast")
                        if persist_enabled:
                            try:
                                from power_master.forecast.persistence import persist_solar_forecast
                                n = await persist_solar_forecast(repo, result)
                                if n:
                                    logger.debug("Persisted %d solar forecast samples", n)
                            except Exception:
                                logger.exception("Solar forecast persistence failed")
                    else:
                        health_checker.record_failure("solar_forecast", "fetch failed")

                # Process weather result (if task ran)
                if "weather" in update_keys:
                    idx = update_keys.index("weather")
                    result = results[idx]
                    last_weather = now
                    if result and not isinstance(result, Exception):
                        health_checker.record_success("weather_forecast")
                        try:
                            await history.record_weather(result)
                        except Exception:
                            logger.exception("Failed to record weather to history")
                        if persist_enabled:
                            try:
                                from power_master.forecast.persistence import persist_weather_forecast
                                await persist_weather_forecast(repo, result, horizons)
                            except Exception:
                                logger.exception("Weather forecast persistence failed")
                    else:
                        health_checker.record_failure("weather_forecast", "fetch failed")

                # Process storm result (if task ran)
                if "storm" in update_keys:
                    idx = update_keys.index("storm")
                    result = results[idx]
                    last_storm = now
                    if result and not isinstance(result, Exception):
                        health_checker.record_success("storm")
                        storm_monitor.update(result.max_probability)
                        if persist_enabled:
                            try:
                                from power_master.forecast.persistence import persist_storm_forecast
                                await persist_storm_forecast(repo, result, horizons)
                            except Exception:
                                logger.exception("Storm forecast persistence failed")
                    elif health_checker.is_healthy("storm"):
                        health_checker.record_failure("storm", "fetch failed")

                # Check for stale forecasts (older than 1 hour) used by solver
                now_utc = datetime.now(timezone.utc)
                stale_threshold = now_utc - timedelta(hours=1)
                plan = control_loop.state.current_plan
                if plan is not None and plan.slots:
                    forecasts_used = {
                        "solar": aggregator.state.last_solar_update,
                        "weather": aggregator.state.last_weather_update,
                        "tariff": aggregator.state.last_tariff_update,
                        "storm": aggregator.state.last_storm_update,
                    }
                    for name, last_update in forecasts_used.items():
                        if last_update and last_update < stale_threshold:
                            logger.warning(
                                "Forecast staleness: %s is %.0f hours old "
                                "(older than 1h threshold)",
                                name,
                                (now_utc - last_update).total_seconds() / 3600,
                            )

                # Publish storm/spike status via MQTT
                if mqtt_publisher:
                    await mqtt_publisher.publish_storm(storm_monitor.is_active)
                    await mqtt_publisher.publish_spike(
                        aggregator.spike_detector.is_spike_active,
                    )
                    summary = accounting.get_summary()
                    await mqtt_publisher.publish_wacb(summary.wacb_cents)

                # Update storm state on control loop for hierarchy evaluation
                async with self._storm_lock:
                    control_loop.update_storm_state(
                        active=storm_monitor.is_active,
                        reserve_soc=storm_monitor.reserve_soc,
                    )

                # Check if plan rebuild needed
                telemetry = control_loop.state.last_telemetry
                current_soc = telemetry.soc if telemetry else 0.5
                manual_active = control_loop.manual_override.is_active
                rebuild_result = rebuild_evaluator.evaluate(
                    control_loop.state.current_plan, current_soc, aggregator,
                    manual_override_active=manual_active,
                    actual_solar_w=telemetry.solar_power_w if telemetry else None,
                    actual_load_w=telemetry.load_power_w if telemetry else None,
                )

                if rebuild_result.should_rebuild:
                    logger.info(
                        "Plan rebuild triggered: %s — %s (SOC=%.1f%%)",
                        rebuild_result.trigger, rebuild_result.reason,
                        current_soc * 100,
                    )
                    await self._run_solver(
                        aggregator, storm_monitor, accounting,
                        rebuild_evaluator, control_loop, repo,
                        trigger=rebuild_result.trigger, load_manager=load_manager,
                    )

                    # Handle spike load shedding + notifications
                    if rebuild_result.trigger == "price_spike":
                        spike_was_active = True
                        deferred = await load_manager.shed_for_spike()
                        if event_bus and self.config.notifications.enabled:
                            from power_master.notifications.bus import Tier
                            from power_master.notifications.emitter import (
                                emit_narrated, new_correlation_id, spike_incident_id,
                            )
                            from power_master.notifications.narrators import NarratorContext
                            spike = aggregator.spike_detector.current_spike
                            telemetry = control_loop.state.last_telemetry
                            # Correlation id is persisted so the "end" event can match
                            corr_id = new_correlation_id()
                            self._spike_correlation_id = corr_id
                            self._spike_deferred_loads = (
                                [c.load_id for c in deferred] if deferred else []
                            )
                            ctx = NarratorContext(
                                now=datetime.now(timezone.utc),
                                current_soc=telemetry.soc if telemetry else None,
                                spike_price_cents=spike.price_cents if spike else None,
                                spike_window_end=None,
                                deferred_load_names=self._spike_deferred_loads,
                            )
                            await emit_narrated(
                                event_bus,
                                event_name="price_spike",
                                title="Price spike detected",
                                severity="critical",
                                tier=Tier.ATTENTION,
                                plan=control_loop.state.current_plan,
                                ctx=ctx,
                                incident_id=spike_incident_id(spike.started_at if spike else None),
                                correlation_id=corr_id,
                                fallback_message="Electricity price spiked — discharging and shedding loads.",
                                data={"price_cents": spike.price_cents if spike else 0},
                            )
                    elif spike_was_active and not aggregator.spike_detector.is_spike_active:
                        spike_was_active = False
                        await load_manager.restore_after_spike()
                        if event_bus and self.config.notifications.enabled:
                            from power_master.notifications.bus import Tier
                            from power_master.notifications.emitter import emit_narrated
                            from power_master.notifications.narrators import NarratorContext
                            telemetry = control_loop.state.last_telemetry
                            ctx = NarratorContext(
                                now=datetime.now(timezone.utc),
                                current_soc=telemetry.soc if telemetry else None,
                                deferred_load_names=getattr(self, "_spike_deferred_loads", []),
                            )
                            await emit_narrated(
                                event_bus,
                                event_name="price_spike_end",
                                title="Price spike ended",
                                severity="info",
                                tier=Tier.INFORMATIONAL,
                                plan=control_loop.state.current_plan,
                                ctx=ctx,
                                correlation_id=getattr(self, "_spike_correlation_id", None),
                                fallback_message="Prices normalised. Restoring loads.",
                            )
                            self._spike_correlation_id = None
                            self._spike_deferred_loads = []

                # Check if control loop flagged urgent SOC deviation
                if self._soc_rebuild_needed.is_set():
                    self._soc_rebuild_needed.clear()
                    telemetry = control_loop.state.last_telemetry
                    current_soc = telemetry.soc if telemetry else 0.5
                    soc_result = rebuild_evaluator.evaluate(
                        control_loop.state.current_plan, current_soc, aggregator,
                        manual_override_active=control_loop.manual_override.is_active,
                    )
                    if soc_result.should_rebuild:
                        await self._run_solver(
                            aggregator, storm_monitor, accounting,
                            rebuild_evaluator, control_loop, repo,
                            trigger=soc_result.trigger, load_manager=load_manager,
                        )

                # Storm state transitions — emitted post-rebuild so the plan
                # reflects the reserve strategy we narrate.
                if event_bus and self.config.notifications.enabled:
                    await self._maybe_emit_storm_events(
                        event_bus, storm_monitor, control_loop, aggregator,
                    )
                    # Force-grid-charge override just became active in the current slot?
                    await self._maybe_emit_force_charge_event(
                        event_bus, control_loop, aggregator,
                    )

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in forecast update loop iteration")

            # Sleep before next check
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=min(tariff_interval, 60),
                )
                break
            except asyncio.TimeoutError:
                pass

    async def _history_flush_loop(self, history):
        """Flush history buffer and checkpoint WAL periodically."""
        from power_master.db.engine import checkpoint_wal

        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=HISTORY_FLUSH_INTERVAL_SECONDS,
                )
                break
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                pass
            await history.flush_telemetry()
            await checkpoint_wal()

    async def _telemetry_flush_loop(self, repo):
        """Flush telemetry buffer periodically."""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=TELEMETRY_BUFFER_FLUSH_SECONDS,
                )
                break
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                pass

            # Flush pending telemetry records
            async with self._telemetry_buffer_lock:
                if self._telemetry_buffer:
                    try:
                        await repo.store_telemetry_batch(self._telemetry_buffer)
                        self._telemetry_buffer.clear()
                        self._telemetry_buffer_flush_time = time.monotonic()
                    except Exception:
                        logger.exception("Telemetry batch flush failed")

    async def _resilience_loop(self, resilience_mgr, event_bus=None):
        """Periodically evaluate resilience level."""
        interval = self.config.resilience.health_check_interval_seconds
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval,
                )
                break
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                pass
            old_level = resilience_mgr.level
            changed = resilience_mgr.evaluate()
            if changed:
                logger.warning(
                    "Resilience level: %s (unhealthy: %s)",
                    resilience_mgr.level.value,
                    resilience_mgr.state.unhealthy_providers,
                )
                if event_bus and self.config.notifications.enabled:
                    from power_master.notifications.bus import Event as _NEvent
                    from power_master.resilience.manager import ResilienceLevel
                    new_level = resilience_mgr.level
                    if new_level.value > old_level.value:
                        await event_bus.publish(_NEvent(
                            name="resilience_degraded",
                            severity="warning",
                            title=f"System Degraded: {new_level.value}",
                            message=f"Unhealthy providers: {', '.join(resilience_mgr.state.unhealthy_providers)}",
                            data={"level": new_level.value},
                        ))
                    elif new_level == ResilienceLevel.NORMAL:
                        await event_bus.publish(_NEvent(
                            name="resilience_recovered",
                            severity="info",
                            title="System Recovered",
                            message="All providers are healthy. Resilience level: NORMAL.",
                        ))

    async def _telemetry_poll_loop(self, adapter, control_loop, repo=None, load_manager=None, event_bus=None) -> None:
        """Poll inverter telemetry frequently for responsive dashboard updates.

        Also stores to DB every 60s so historical charts have continuous
        data even if the control loop tick is delayed or skipped.

        Uses self._adapter (not the initial parameter) so hot-reloaded
        adapters are picked up automatically.
        """
        import time as _time

        interval = max(1, int(self.config.hardware.foxess.poll_interval_seconds))
        db_store_interval: int = DB_STORE_INTERVAL_SECONDS
        load_poll_interval: int = LOAD_POLL_INTERVAL_SECONDS
        reconnect_interval: int = RECONNECT_INTERVAL_SECONDS
        last_db_store: float = 0.0
        last_load_poll: float = 0.0
        last_reconnect_attempt: float = 0.0
        _was_connected: bool = True  # assume connected at startup
        logger.info("Telemetry poll loop starting (interval: %ds)", interval)
        while not self._stop_event.is_set():
            # Always use the latest adapter (may be swapped by config reload)
            current_adapter = self._adapter
            if current_adapter is None:
                await asyncio.sleep(interval)
                continue

            # Attempt reconnect if disconnected
            if not await current_adapter.is_connected():
                if _was_connected and event_bus and self.config.notifications.enabled:
                    from power_master.notifications.bus import Tier
                    from power_master.notifications.emitter import (
                        emit_narrated, grid_outage_incident_id, new_correlation_id,
                    )
                    from power_master.notifications.narrators import NarratorContext
                    self._grid_outage_since = datetime.now(timezone.utc)
                    self._grid_outage_correlation_id = new_correlation_id()
                    telemetry = control_loop.state.last_telemetry
                    ctx = NarratorContext(
                        now=datetime.now(timezone.utc),
                        current_soc=telemetry.soc if telemetry else None,
                        inverter_offline_since=self._grid_outage_since,
                    )
                    await emit_narrated(
                        event_bus,
                        event_name="inverter_offline",
                        title="Inverter unreachable",
                        severity="critical",
                        tier=Tier.ATTENTION,
                        plan=control_loop.state.current_plan,
                        ctx=ctx,
                        incident_id=grid_outage_incident_id(self._grid_outage_since),
                        correlation_id=self._grid_outage_correlation_id,
                        fallback_message="Lost connection to the inverter — retrying every 30s.",
                    )
                _was_connected = False
                now_mono = _time.monotonic()
                if (now_mono - last_reconnect_attempt) >= reconnect_interval:
                    last_reconnect_attempt = now_mono
                    try:
                        await current_adapter.connect()
                        logger.info("Reconnected to inverter")
                        _was_connected = True
                        if event_bus and self.config.notifications.enabled:
                            from power_master.notifications.bus import Tier
                            from power_master.notifications.emitter import emit_narrated
                            from power_master.notifications.narrators import NarratorContext
                            telemetry = control_loop.state.last_telemetry
                            ctx = NarratorContext(
                                now=datetime.now(timezone.utc),
                                current_soc=telemetry.soc if telemetry else None,
                            )
                            await emit_narrated(
                                event_bus,
                                event_name="inverter_online",
                                title="Inverter reconnected",
                                severity="info",
                                tier=Tier.INFORMATIONAL,
                                plan=control_loop.state.current_plan,
                                ctx=ctx,
                                correlation_id=self._grid_outage_correlation_id,
                                fallback_message="Reconnected to the inverter.",
                            )
                            self._grid_outage_correlation_id = None
                            self._grid_outage_since = None
                    except Exception:
                        logger.warning("Inverter reconnect failed, will retry in %ds", reconnect_interval)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                    break
                except asyncio.TimeoutError:
                    pass
                continue

            try:
                telemetry = await current_adapter.get_telemetry()
                control_loop.update_live_telemetry(telemetry)

                # Persist to DB at a throttled rate for chart continuity
                now_mono = _time.monotonic()
                if repo is not None and (now_mono - last_db_store) >= db_store_interval:
                    try:
                        await repo.store_telemetry(
                            soc=telemetry.soc,
                            battery_power_w=telemetry.battery_power_w,
                            solar_power_w=telemetry.solar_power_w,
                            grid_power_w=telemetry.grid_power_w,
                            load_power_w=telemetry.load_power_w,
                            battery_voltage=telemetry.battery_voltage,
                            battery_temp_c=telemetry.battery_temp_c,
                            inverter_mode=telemetry.inverter_mode,
                            grid_available=telemetry.grid_available,
                            raw_data=telemetry.raw_data,
                        )
                        last_db_store = now_mono
                    except Exception:
                        logger.debug("Poll loop DB store failed", exc_info=True)

                # Poll load device statuses for runtime tracking + schedule execution
                if load_manager and (now_mono - last_load_poll) >= load_poll_interval:
                    try:
                        statuses = await load_manager.get_all_statuses()
                        await load_manager.update_runtime_tracking(statuses, repo=repo)

                        # Execute scheduled load commands based on current plan slot
                        plan = control_loop.state.current_plan
                        if plan is not None:
                            load_cmds = await load_manager.execute_current_slot(plan, repo=repo)
                            if load_cmds:
                                logger.info(
                                    "Load schedule: %d commands (%s)",
                                    len(load_cmds),
                                    ", ".join(f"{c.load_id}={c.action}" for c in load_cmds),
                                )

                        last_load_poll = now_mono
                    except Exception:
                        logger.debug("Load status poll failed", exc_info=True)

            except Exception:
                logger.warning("Telemetry poll read failed", exc_info=True)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                break
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                pass

    async def _run_solver(
        self, aggregator, storm_monitor, accounting,
        rebuild_evaluator, control_loop, repo,
        load_manager=None,
        trigger: str = "periodic",
    ):
        """Build solver inputs and run the MILP optimisation."""
        from power_master.optimisation.solver import SolverInputs, solve

        state = aggregator.state
        n_slots = (self.config.planning.horizon_hours * 60) // self.config.planning.slot_duration_minutes
        slot_minutes = self.config.planning.slot_duration_minutes

        # Build slot arrays from forecasts
        # Floor to nearest slot boundary for better tariff alignment
        now = datetime.now(timezone.utc)
        floor_min = (now.minute // slot_minutes) * slot_minutes
        now_floored = now.replace(minute=floor_min, second=0, microsecond=0)
        slot_start_times = [
            now_floored + timedelta(minutes=i * slot_minutes) for i in range(n_slots)
        ]

        # Solar forecast — fill from aggregator, fallback to bell curve, or default to 0
        solar_forecast_w = [0.0] * n_slots
        solar_source = state.solar
        system_size_kw = self.config.providers.solar.system_size_kw

        # If Solcast is stale/missing and system size is configured, use bell curve fallback
        if (not state.has_solar or not state.solar) and system_size_kw > 0:
            from power_master.forecast.solar_estimate import build_fallback_forecast, merge_solar_forecasts

            # Build cloud cover map from weather forecast if available
            cloud_cover_by_hour: dict[int, float] = {}
            if state.has_weather and state.weather:
                for ws in state.weather.slots:
                    cloud_cover_by_hour[ws.time.hour] = ws.cloud_cover_pct

            # 50% of system size as conservative peak capacity
            peak_w = system_size_kw * 1000 * 0.5
            fallback = build_fallback_forecast(cloud_cover_by_hour, peak_w, now)
            solar_source = merge_solar_forecasts(state.solar, fallback)
            logger.info(
                "Solar fallback active: system_size=%.1fkW peak=%.0fW provider=%s",
                system_size_kw, peak_w,
                solar_source.provider if solar_source else "none",
            )

        if solar_source and solar_source.slots:
            matched_solar = 0
            for i, t in enumerate(slot_start_times):
                for slot in solar_source.slots:
                    if slot.start <= t < slot.end:
                        solar_forecast_w[i] = slot.pv_estimate_w
                        matched_solar += 1
                        break
            nonzero_solar = sum(1 for v in solar_forecast_w if v > 0)
            logger.info(
                "Solar matching: %d/%d plan slots matched, %d non-zero. "
                "Forecast range: %s to %s, Plan range: %s to %s",
                matched_solar, n_slots, nonzero_solar,
                solar_source.slots[0].start.isoformat(),
                solar_source.slots[-1].end.isoformat(),
                slot_start_times[0].isoformat(),
                slot_start_times[-1].isoformat(),
            )
        else:
            logger.warning(
                "No solar forecast data available for plan. "
                "has_solar=%s, solar_source=%s",
                state.has_solar,
                solar_source.provider if solar_source else "None",
            )

        # Calibrate the solar forecast against recent telemetry.  Model is
        # refit lazily (see _refresh_solar_calibration).  When disabled or
        # there's insufficient data, apply_calibration returns the raw
        # forecast unchanged.
        # Don't hold lock while awaiting solver — this is just getting the model
        async with self._model_lock:
            solar_model = self._solar_calibration_model
        if solar_model is not None:
            from power_master.forecast.solar_calibration import apply_calibration
            calibrated = apply_calibration(solar_forecast_w, slot_start_times, solar_model)
            raw_sum = sum(solar_forecast_w)
            cal_sum = sum(calibrated)
            logger.info(
                "Solar calibration applied: raw_total=%.0fWh cal_total=%.0fWh "
                "n_samples=%d raw_mae=%.0fW cal_mae=%.0fW",
                raw_sum * 0.5, cal_sum * 0.5,
                solar_model.n_samples,
                solar_model.raw_mae_w, solar_model.calibrated_mae_w,
            )
            solar_forecast_w = calibrated

        # Load forecast — use historic patterns, fall back to configured profile
        load_forecast_w = await self._build_load_forecast(
            repo, slot_start_times, n_slots,
        )

        # Tariff rates
        import_rate_cents = [15.0] * n_slots  # Default rates
        export_rate_cents = [5.0] * n_slots
        is_spike = [False] * n_slots

        if state.has_tariff and state.tariff:
            matched_count = 0
            unmatched = []
            for i, t in enumerate(slot_start_times):
                tariff_slot = state.tariff.get_slot_at(t)
                if tariff_slot:
                    import_rate_cents[i] = tariff_slot.import_price_cents
                    export_rate_cents[i] = tariff_slot.export_price_cents
                    is_spike[i] = tariff_slot.import_price_cents >= self.config.arbitrage.spike_threshold_cents
                    matched_count += 1
                else:
                    unmatched.append(i)

            # Gap-fill: use nearest matched neighbor for unmatched slots
            unmatched_set = set(unmatched)
            for i in unmatched:
                nearest = None
                for offset in range(1, n_slots):
                    if i - offset >= 0 and (i - offset) not in unmatched_set:
                        nearest = i - offset
                        break
                    if i + offset < n_slots and (i + offset) not in unmatched_set:
                        nearest = i + offset
                        break
                if nearest is not None:
                    import_rate_cents[i] = import_rate_cents[nearest]
                    export_rate_cents[i] = export_rate_cents[nearest]
                    is_spike[i] = is_spike[nearest]

            tariff_slots = state.tariff.slots
            gap_filled = len(unmatched)
            logger.info(
                "Tariff matching: %d/%d plan slots matched, %d gap-filled "
                "(tariff has %d slots, range %s to %s)",
                matched_count, n_slots, gap_filled, len(tariff_slots),
                tariff_slots[0].start.isoformat() if tariff_slots else "N/A",
                tariff_slots[-1].end.isoformat() if tariff_slots else "N/A",
            )
            if matched_count < n_slots // 2:
                logger.warning(
                    "Low tariff coverage: only %d/%d slots matched. "
                    "Plan range: %s to %s",
                    matched_count, n_slots,
                    slot_start_times[0].isoformat(),
                    slot_start_times[-1].isoformat(),
                )

        # Get current SOC from telemetry or default
        telemetry = control_loop.state.last_telemetry
        current_soc = telemetry.soc if telemetry else 0.5
        if telemetry is None:
            logger.warning(
                "No telemetry available for solver — using default SOC=50%%"
            )
        else:
            logger.info("Solver starting SOC=%.1f%% (from telemetry)", current_soc * 100)

        inputs = SolverInputs(
            solar_forecast_w=solar_forecast_w,
            load_forecast_w=load_forecast_w,
            import_rate_cents=import_rate_cents,
            export_rate_cents=export_rate_cents,
            is_spike=is_spike,
            current_soc=current_soc,
            wacb_cents=accounting.wacb_cents,
            storm_active=storm_monitor.is_active,
            storm_reserve_soc=storm_monitor.reserve_soc,
            slot_start_times=slot_start_times,
        )

        # Run solver in thread pool to avoid blocking
        version = await repo.get_next_plan_version()
        loop = asyncio.get_running_loop()
        plan = await loop.run_in_executor(
            None, solve, self.config, inputs, trigger, version,
        )

        # Validate solver status before storing/activating plan
        if plan.solver_status not in ("Optimal", "Feasible"):
            logger.error(
                "Solver returned non-solution status: %s. Retaining previous plan.",
                plan.solver_status,
            )
            return control_loop.state.current_plan

        # Second pass: schedule controllable loads into plan slots.
        if load_manager is not None:
            try:
                from power_master.optimisation.load_scheduler import schedule_loads

                scheduled = schedule_loads(
                    plan,
                    available_loads=load_manager.get_load_configs(),
                    spike_active=aggregator.spike_detector.is_spike_active,
                    actual_runtime_minutes=load_manager.get_all_runtime_minutes(),
                    manual_override_load_ids=load_manager.get_active_override_load_ids(),
                )
                logger.info("Load scheduler assigned %d devices across plan slots", len(scheduled))
            except Exception:
                logger.warning("Load scheduling failed; continuing with base plan", exc_info=True)

        # Store plan and slots in DB
        plan_id = await repo.store_plan(
            version=plan.version,
            trigger_reason=plan.trigger_reason,
            horizon_start=plan.horizon_start.isoformat(),
            horizon_end=plan.horizon_end.isoformat(),
            objective_score=plan.objective_score,
            solver_time_ms=plan.solver_time_ms,
            metrics=plan.metrics,
            active_constraints=plan.active_constraints,
        )
        if plan.slots:
            await repo.store_plan_slots(plan_id, plan.slots_to_db_dicts())

        # Update control loop with new plan (under lock to prevent control loop conflicts)
        async with self._plan_lock:
            control_loop.set_plan(plan)
        rebuild_evaluator.mark_rebuilt(trigger=trigger)

        logger.info(
            "Plan v%d built: trigger=%s objective=%.2f solver=%dms",
            plan.version, trigger, plan.objective_score, plan.solver_time_ms,
        )

        return plan

    async def _forecast_prune_loop(self, repo):
        """Delete forecast samples older than providers.forecast_retention_days.

        Runs once per hour, a trivial DELETE on an indexed column.
        """
        # Let the app warm up before first prune
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=300)
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop_event.is_set():
            try:
                retention_days = self.config.providers.forecast_retention_days
                cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
                pruned = await repo.prune_forecast_samples(cutoff)
                if pruned:
                    logger.info(
                        "Pruned %d forecast samples older than %d days",
                        pruned, retention_days,
                    )
            except Exception:
                logger.exception("Forecast sample prune failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=3600)
                return
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                continue

    async def _notification_maintenance_loop(
        self, repo, event_bus, control_loop, aggregator, storm_monitor,
    ) -> None:
        """Daily-briefing fire + notification log retention prune.

        Checks every 60s whether we've crossed the configured local-hour
        boundary for today's briefing, using the configured timezone.  The
        TZ lookup is re-done each iteration so DST transitions are handled.
        """
        from power_master.notifications.bus import Event as _NEvent
        from power_master.notifications.bus import Tier
        from power_master.notifications.narrators import NarratorContext, generate_daily_briefing
        from power_master.notifications.emitter import emit_narrated
        _KV_KEY = "notifications.last_briefing_date"
        try:
            last_briefing_date: str | None = await repo.kv_get(_KV_KEY)
        except Exception:
            last_briefing_date = None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=60)
                return
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                pass
            try:
                cfg = self.config.notifications
                # Notification log prune (once per iteration is cheap)
                cutoff = (
                    datetime.now(timezone.utc)
                    - timedelta(days=cfg.notification_retention_days)
                ).isoformat()
                try:
                    await repo.prune_notifications(cutoff)
                except Exception:
                    logger.debug("Notification prune failed", exc_info=True)

                # Daily briefing
                if not cfg.daily_briefing_enabled:
                    continue
                tz_name = self.config.load_profile.timezone
                tz = resolve_timezone(tz_name)
                now_local = datetime.now(tz)
                target_hour = cfg.daily_briefing_hour_local
                today_key = now_local.date().isoformat()
                if now_local.hour < target_hour or last_briefing_date == today_key:
                    continue
                telemetry = control_loop.state.last_telemetry
                spike = aggregator.spike_detector.current_spike
                ctx = NarratorContext(
                    now=datetime.now(timezone.utc),
                    current_soc=telemetry.soc if telemetry else None,
                    storm_active=storm_monitor.is_active,
                    spike_price_cents=spike.price_cents if spike else None,
                    evening_target_soc=self.config.battery_targets.evening_soc_target,
                    evening_target_hour=self.config.battery_targets.evening_target_hour,
                )
                action = generate_daily_briefing(control_loop.state.current_plan, ctx)
                event = _NEvent(
                    name="daily_briefing",
                    severity="info",
                    title=f"Daily briefing — {now_local.strftime('%a %d %b')}",
                    message="",
                    tier=Tier.INFORMATIONAL,
                    action=action,
                )
                await event_bus.publish(event)
                last_briefing_date = today_key
                try:
                    await repo.kv_set(_KV_KEY, today_key)
                except Exception:
                    logger.debug("Failed to persist last briefing date", exc_info=True)
            except Exception:
                logger.exception("Notification maintenance loop error")

    async def _maybe_emit_storm_events(
        self, event_bus, storm_monitor, control_loop, aggregator,
    ) -> None:
        """Emit storm_plan_active / storm_resolved on state transitions.

        Runs after a plan rebuild so the narrated Action reflects the
        reserve strategy now committed to the plan (not the pre-rebuild state).
        """
        from power_master.notifications.bus import Tier
        from power_master.notifications.emitter import (
            emit_narrated, new_correlation_id, storm_incident_id,
        )
        from power_master.notifications.narrators import NarratorContext

        currently_active = storm_monitor.is_active
        if currently_active and not self._storm_was_active:
            # Pull the alert window from the aggregator's last StormForecast
            window_start = None
            window_end = None
            storm_forecast = aggregator.state.storm
            if storm_forecast and storm_forecast.alerts:
                active_alerts = [
                    a for a in storm_forecast.alerts
                    if a.probability >= self.config.storm.probability_threshold
                ]
                if active_alerts:
                    window_start = min(a.valid_from for a in active_alerts)
                    window_end = max(a.valid_to for a in active_alerts)
            async with self._storm_lock:
                self._storm_correlation_id = new_correlation_id()
            telemetry = control_loop.state.last_telemetry
            ctx = NarratorContext(
                now=datetime.now(timezone.utc),
                current_soc=telemetry.soc if telemetry else None,
                storm_active=True,
                storm_reserve_soc=storm_monitor.reserve_soc,
                storm_window_start=window_start,
                storm_window_end=window_end,
            )
            await emit_narrated(
                event_bus,
                event_name="storm_plan_active",
                title="Storm plan active",
                severity="warning",
                tier=Tier.ATTENTION,
                plan=control_loop.state.current_plan,
                ctx=ctx,
                incident_id=storm_incident_id(
                    window_start,
                    activated_at=getattr(storm_monitor, "activated_at", None),
                ),
                correlation_id=self._storm_correlation_id,
                fallback_message="Storm forecast active — reserving battery.",
            )
        elif not currently_active and self._storm_was_active:
            telemetry = control_loop.state.last_telemetry
            ctx = NarratorContext(
                now=datetime.now(timezone.utc),
                current_soc=telemetry.soc if telemetry else None,
            )
            await emit_narrated(
                event_bus,
                event_name="storm_resolved",
                title="Storm window cleared",
                severity="info",
                tier=Tier.INFORMATIONAL,
                plan=control_loop.state.current_plan,
                ctx=ctx,
                correlation_id=self._storm_correlation_id,
                fallback_message="Storm window cleared.",
            )
            async with self._storm_lock:
                self._storm_correlation_id = None

        async with self._storm_lock:
            self._storm_was_active = currently_active

    async def _maybe_emit_force_charge_event(
        self, event_bus, control_loop, aggregator,
    ) -> None:
        """Emit once per hour when a force-charge override slot is currently active."""
        plan = control_loop.state.current_plan
        if plan is None:
            return
        threshold = self.config.battery_targets.force_charge_below_price_cents
        if threshold <= 0:
            return
        slot = plan.get_current_slot()
        if slot is None:
            return
        from power_master.optimisation.plan import SlotMode
        if slot.mode != SlotMode.FORCE_CHARGE:
            return
        if slot.import_rate_cents > threshold:
            # The force-charge is coming from the solver, not the price override
            return

        from power_master.notifications.bus import Tier
        from power_master.notifications.emitter import (
            emit_narrated, force_charge_incident_id,
        )
        from power_master.notifications.narrators import NarratorContext

        now = datetime.now(timezone.utc)
        hour_key = now.strftime("%Y-%m-%dT%H")
        if self._force_charge_last_emitted_hour == hour_key:
            return  # already emitted this hour

        telemetry = control_loop.state.last_telemetry
        ctx = NarratorContext(
            now=now,
            current_soc=telemetry.soc if telemetry else None,
            force_charge_threshold_cents=threshold,
            force_charge_price_cents=slot.import_rate_cents,
            evening_target_soc=self.config.battery_targets.evening_soc_target,
            evening_target_hour=self.config.battery_targets.evening_target_hour,
        )
        await emit_narrated(
            event_bus,
            event_name="force_charge_triggered",
            title="Cheap-price grid charge active",
            severity="info",
            tier=Tier.INFORMATIONAL,
            plan=plan,
            ctx=ctx,
            incident_id=force_charge_incident_id(now),
            fallback_message=(
                f"Buy price {slot.import_rate_cents:.1f}c/kWh — forcing grid charge."
            ),
        )
        self._force_charge_last_emitted_hour = hour_key

    async def _refresh_solar_calibration(self, repo):
        """Fit (or refit) the solar calibration model.  Returns None when disabled
        or when insufficient training data is available.
        """
        solar_cfg = self.config.providers.solar
        if not solar_cfg.calibration_enabled:
            return None

        peak_w = solar_cfg.system_size_kw * 1000.0 if solar_cfg.system_size_kw > 0 else solar_cfg.kwp * 1000.0
        if peak_w <= 0:
            return None

        now = datetime.now(timezone.utc)
        async with self._model_lock:
            last_fit = self._solar_calibration_last_fit
            model = self._solar_calibration_model
        refit_interval = timedelta(seconds=solar_cfg.calibration_refit_interval_seconds)
        if model is not None and last_fit is not None:
            if now - last_fit < refit_interval:
                return model

        from power_master.forecast.solar_calibration import (
            build_training_set,
        )

        try:
            samples = await build_training_set(
                repo,
                window_days=solar_cfg.calibration_window_days,
                system_peak_w=peak_w,
                tz_name=solar_cfg.timezone,
                reference_time=now,
            )
            # Run model fitting in thread pool to avoid blocking event loop
            loop = asyncio.get_event_loop()
            fitted_model = await loop.run_in_executor(
                None, _fit_solar_model_sync, samples, peak_w, solar_cfg.timezone, now,
            )
        except Exception:
            logger.exception("Solar calibration fit failed — using raw forecast")
            async with self._model_lock:
                self._solar_calibration_last_fit = now
            async with self._model_lock:
                prev_model = self._solar_calibration_model
            return prev_model  # keep previous (possibly None)

        async with self._model_lock:
            self._solar_calibration_last_fit = now
        if fitted_model is None:
            logger.info(
                "Solar calibration: insufficient data (%d samples, need >=50)",
                len(samples),
            )
            async with self._model_lock:
                self._solar_calibration_model = None
            return None

        async with self._model_lock:
            self._solar_calibration_model = fitted_model
        logger.info(
            "Solar calibration refit: n=%d raw_mae=%.0fW cal_mae=%.0fW lift=%.0fW",
            fitted_model.n_samples, fitted_model.raw_mae_w, fitted_model.calibrated_mae_w,
            fitted_model.raw_mae_w - fitted_model.calibrated_mae_w,
        )
        return fitted_model

    async def _build_load_forecast(
        self,
        repo,
        slot_start_times: list[datetime],
        n_slots: int,
    ) -> list[float]:
        """Build load forecast from historic data, falling back to config profile.

        Priority:
        1. Historical day-of-week + hour-of-day patterns (if enough data)
        2. Configured 4-hour block averages from load_profile config
        """
        from power_master.history.prediction import LoadPredictor

        profile_cfg = self.config.load_profile
        load_tz = resolve_timezone(profile_cfg.timezone)

        # Try historic prediction first
        predictor = LoadPredictor(repo, timezone_name=profile_cfg.timezone)
        await predictor.rebuild_profile(lookback_days=28)

        if predictor._profile is not None:
            # Historic data available — use it with config fallback per-slot
            forecast = []
            for t in slot_start_times:
                local_hour = t.astimezone(load_tz).hour
                predicted = predictor.predict(
                    t, default_w=profile_cfg.get_for_hour(local_hour),
                )
                forecast.append(max(0.0, predicted))
            logger.info(
                "Load forecast: historic prediction (%d slots)", n_slots,
            )
            return forecast

        # Fall back to configured 4-hour block profile
        forecast = [
            profile_cfg.get_for_hour(t.astimezone(load_tz).hour)
            for t in slot_start_times
        ]
        logger.info(
            "Load forecast: using configured profile (no historic data)",
        )
        return forecast


def main() -> None:
    """Entry point for the application."""
    defaults_path = Path("config.defaults.yaml")
    user_path = Path("config.yaml")

    config_manager = ConfigManager(defaults_path, user_path)
    config = config_manager.load()

    setup_logging(
        level=config.logging.level,
        fmt=config.logging.format,
        log_file=config.logging.file,
    )

    app = Application(config, config_manager)
    stop_requested = False
    signal_count = 0

    async def _run() -> None:
        nonlocal stop_requested
        try:
            await app.start()
        finally:
            if app._running:
                with contextlib.suppress(Exception):
                    await app.stop()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _request_stop() -> None:
        nonlocal stop_requested, signal_count
        signal_count += 1
        if signal_count >= 2:
            os._exit(130)
        if stop_requested or loop.is_closed():
            return
        stop_requested = True
        loop.call_soon_threadsafe(lambda: asyncio.create_task(app.stop()))

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _request_stop)
    else:
        signal.signal(signal.SIGINT, lambda *_: _request_stop())
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, lambda *_: _request_stop())
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, lambda *_: _request_stop())

    try:
        loop.run_until_complete(_run())
    except KeyboardInterrupt:
        _request_stop()
        with contextlib.suppress(Exception):
            loop.run_until_complete(app.stop())
    finally:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            with contextlib.suppress(Exception):
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


if __name__ == "__main__":
    main()
