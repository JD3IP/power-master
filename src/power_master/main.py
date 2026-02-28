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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from power_master.config.manager import ConfigManager
from power_master.config.schema import AppConfig
from power_master.db.engine import close_db, init_db
from power_master.db.repository import Repository
from power_master.logging.structured import setup_logging
from power_master.timezone_utils import resolve_timezone

logger = logging.getLogger(__name__)

VERSION = "0.1.0"


class Application:
    """Main application lifecycle manager.

    Wires all modules together and manages startup/shutdown ordering.
    """

    def __init__(self, config: AppConfig, config_manager: ConfigManager) -> None:
        self.config = config
        self.config_manager = config_manager
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._stop_event = asyncio.Event()

        # References held for cleanup
        self._adapter = None
        self._mqtt_client = None
        self._control_loop = None
        self._providers: list = []  # providers with .close() methods
        self._server = None

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
        await self.config_manager.save_version(db)
        logger.info("Database initialised")

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

        # ── 8. Rebuild evaluator ─────────────────────────────
        from power_master.optimisation.rebuild_evaluator import RebuildEvaluator

        rebuild_evaluator = RebuildEvaluator(self.config)

        # ── 9. Load manager ──────────────────────────────────
        from power_master.loads.manager import LoadManager

        load_manager = LoadManager(self.config)
        self._register_loads(load_manager)

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

        manual_override = ManualOverride()
        anti_oscillation = AntiOscillationGuard(self.config.anti_oscillation)

        control_loop = ControlLoop(
            config=self.config,
            adapter=adapter,
            manual_override=manual_override,
            anti_oscillation=anti_oscillation,
        )
        self._control_loop = control_loop

        # Register telemetry callback: store in DB + history + MQTT
        async def on_telemetry(telemetry):
            # Store in database
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
            # Buffer for history aggregation
            history.record_telemetry(telemetry)
            # Sync accounting SOC
            accounting.sync_soc(telemetry.soc)

            # Record energy flows for accounting (power × interval → Wh)
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
                import_wh = int(grid_w * tick_hours)
                if import_wh > 0:
                    accounting.record_grid_import(import_wh, import_rate)
                # If battery is also charging from grid
                if battery_w > 0:
                    grid_charge_w = min(battery_w, grid_w)
                    grid_charge_wh = int(grid_charge_w * tick_hours)
                    if grid_charge_wh > 0:
                        accounting.record_grid_charge(grid_charge_wh, import_rate)
            elif grid_w < 0:
                # Grid export
                export_wh = int(abs(grid_w) * tick_hours)
                if export_wh > 0:
                    accounting.record_grid_export(export_wh, export_rate)

            # Solar charging battery
            if battery_w > 0 and solar_w > 0 and grid_w <= 0:
                solar_charge_w = min(battery_w, solar_w)
                solar_charge_wh = int(solar_charge_w * tick_hours)
                if solar_charge_wh > 0:
                    accounting.record_solar_charge(solar_charge_wh, export_rate)

            # Self-consumption: load served by solar/battery (not grid)
            if load_w > 0 and grid_w <= 0:
                self_consumption_w = min(load_w, solar_w + max(0, -battery_w))
                self_consumption_wh = int(self_consumption_w * tick_hours)
                if self_consumption_wh > 0:
                    accounting.record_self_consumption(self_consumption_wh, import_rate)

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
            self._telemetry_poll_loop(adapter, control_loop, repo=repo, load_manager=load_manager),
            name="telemetry_poller",
        ))

        # Periodic forecast updates
        self._tasks.append(asyncio.create_task(
            self._forecast_update_loop(
                aggregator, storm_monitor, health_checker,
                history, accounting, rebuild_evaluator,
                control_loop, repo, load_manager, mqtt_publisher,
            ),
            name="forecast_updater",
        ))

        # History flush (every 30 min)
        self._tasks.append(asyncio.create_task(
            self._history_flush_loop(history),
            name="history_flusher",
        ))

        # Resilience evaluator
        self._tasks.append(asyncio.create_task(
            self._resilience_loop(resilience_mgr),
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

        updater = UpdateManager()
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

        adapter = FoxESSAdapter(self.config.hardware.foxess)
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
        except Exception:
            logger.warning(
                "Fox-ESS connection failed — running in degraded mode "
                "(no inverter telemetry or control)"
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

        import time

        while not self._stop_event.is_set():
            try:
                now = time.monotonic()

                # Tariff update
                if now - last_tariff >= tariff_interval:
                    result = await aggregator.update_tariff()
                    if result:
                        health_checker.record_success("tariff")
                        await history.record_price(result)
                        last_tariff = now
                    else:
                        health_checker.record_failure("tariff", "fetch failed")

                # Solar update
                if now - last_solar >= solar_interval:
                    result = await aggregator.update_solar()
                    # Throttle retries by configured interval even on failures
                    # to avoid excessive provider polling when endpoint is degraded.
                    last_solar = now
                    if result:
                        health_checker.record_success("solar_forecast")
                    else:
                        health_checker.record_failure("solar_forecast", "fetch failed")

                # Weather update
                if now - last_weather >= weather_interval:
                    result = await aggregator.update_weather()
                    if result:
                        health_checker.record_success("weather_forecast")
                        await history.record_weather(result)
                        last_weather = now
                    else:
                        health_checker.record_failure("weather_forecast", "fetch failed")

                # Storm update
                if self.config.storm.enabled and now - last_storm >= storm_interval:
                    result = await aggregator.update_storm()
                    if result:
                        health_checker.record_success("storm")
                        storm_monitor.update(result.max_probability)
                        last_storm = now
                    elif health_checker.is_healthy("storm"):
                        health_checker.record_failure("storm", "fetch failed")

                # Publish storm/spike status via MQTT
                if mqtt_publisher:
                    await mqtt_publisher.publish_storm(storm_monitor.is_active)
                    await mqtt_publisher.publish_spike(
                        aggregator.spike_detector.is_spike_active,
                    )
                    summary = accounting.get_summary()
                    await mqtt_publisher.publish_wacb(summary.wacb_cents)

                # Update storm state on control loop for hierarchy evaluation
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

                    # Handle spike load shedding
                    if rebuild_result.trigger == "price_spike":
                        await load_manager.shed_for_spike()
                    elif not aggregator.spike_detector.is_spike_active:
                        await load_manager.restore_after_spike()

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
        """Flush history buffer and checkpoint WAL every 30 minutes."""
        from power_master.db.engine import checkpoint_wal

        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=1800,
                )
                break
            except asyncio.TimeoutError:
                pass
            await history.flush_telemetry()
            await checkpoint_wal()

    async def _resilience_loop(self, resilience_mgr):
        """Periodically evaluate resilience level."""
        interval = self.config.resilience.health_check_interval_seconds
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval,
                )
                break
            except asyncio.TimeoutError:
                pass
            changed = resilience_mgr.evaluate()
            if changed:
                logger.warning(
                    "Resilience level: %s (unhealthy: %s)",
                    resilience_mgr.level.value,
                    resilience_mgr.state.unhealthy_providers,
                )

    async def _telemetry_poll_loop(self, adapter, control_loop, repo=None, load_manager=None) -> None:
        """Poll inverter telemetry frequently for responsive dashboard updates.

        Also stores to DB every 60s so historical charts have continuous
        data even if the control loop tick is delayed or skipped.

        Uses self._adapter (not the initial parameter) so hot-reloaded
        adapters are picked up automatically.
        """
        import time as _time

        interval = max(1, int(self.config.hardware.foxess.poll_interval_seconds))
        db_store_interval = 60  # seconds between DB writes from poll loop
        load_poll_interval = 30  # seconds between load status polls
        reconnect_interval = 30  # seconds between reconnect attempts
        last_db_store = 0.0
        last_load_poll = 0.0
        last_reconnect_attempt = 0.0
        logger.info("Telemetry poll loop starting (interval: %ds)", interval)
        while not self._stop_event.is_set():
            # Always use the latest adapter (may be swapped by config reload)
            current_adapter = self._adapter
            if current_adapter is None:
                await asyncio.sleep(interval)
                continue

            # Attempt reconnect if disconnected
            if not await current_adapter.is_connected():
                now_mono = _time.monotonic()
                if (now_mono - last_reconnect_attempt) >= reconnect_interval:
                    last_reconnect_attempt = now_mono
                    try:
                        await current_adapter.connect()
                        logger.info("Reconnected to inverter")
                    except Exception:
                        logger.debug("Inverter reconnect failed, will retry in %ds", reconnect_interval)
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

                # Poll load device statuses for runtime tracking
                if load_manager and (now_mono - last_load_poll) >= load_poll_interval:
                    try:
                        statuses = await load_manager.get_all_statuses()
                        load_manager.update_runtime_tracking(statuses)
                        last_load_poll = now_mono
                    except Exception:
                        logger.debug("Load status poll failed", exc_info=True)

            except Exception:
                logger.debug("Telemetry poll read failed", exc_info=True)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                break
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

        # Update control loop with new plan
        control_loop.set_plan(plan)
        rebuild_evaluator.mark_rebuilt(trigger=trigger)

        logger.info(
            "Plan v%d built: trigger=%s objective=%.2f solver=%dms",
            plan.version, trigger, plan.objective_score, plan.solver_time_ms,
        )

        return plan

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
