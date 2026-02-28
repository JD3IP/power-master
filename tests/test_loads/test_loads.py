"""Tests for load controllers, manager, and scheduler."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from power_master.config.schema import (
    AppConfig,
    MQTTLoadEndpointConfig,
    ShellyDeviceConfig,
)
from power_master.loads.base import LoadController, LoadState, LoadStatus
from power_master.loads.adapters.mqtt_load import MQTTLoadAdapter
from power_master.loads.adapters.shelly import ShellyAdapter
from power_master.loads.manager import LoadManager
from power_master.optimisation.load_scheduler import ScheduledLoad, schedule_loads
from power_master.optimisation.plan import OptimisationPlan, PlanSlot, SlotMode


# ── Helpers ──────────────────────────────────────────────────


def _make_shelly_config(**kwargs) -> ShellyDeviceConfig:
    defaults = {
        "name": "pool_pump",
        "host": "192.168.1.50",
        "relay_id": 0,
        "power_w": 1200,
        "priority_class": 4,
    }
    defaults.update(kwargs)
    return ShellyDeviceConfig(**defaults)


def _make_mqtt_config(**kwargs) -> MQTTLoadEndpointConfig:
    defaults = {
        "name": "hot_water",
        "command_topic": "power_master/load/hot_water/command",
        "state_topic": "power_master/load/hot_water/state",
        "power_w": 3600,
        "priority_class": 3,
    }
    defaults.update(kwargs)
    return MQTTLoadEndpointConfig(**defaults)


def _make_plan(n_slots: int = 8, solar_w: float = 0.0, load_w: float = 500.0) -> OptimisationPlan:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    slots = []
    for i in range(n_slots):
        start = now + timedelta(minutes=30 * i)
        end = start + timedelta(minutes=30)
        slots.append(PlanSlot(
            index=i,
            start=start,
            end=end,
            mode=SlotMode.SELF_USE,
            solar_forecast_w=solar_w,
            load_forecast_w=load_w,
            import_rate_cents=20.0 if i < 4 else 5.0,
        ))
    return OptimisationPlan(
        version=1,
        created_at=now,
        trigger_reason="periodic",
        horizon_start=now,
        horizon_end=now + timedelta(minutes=30 * n_slots),
        slots=slots,
        objective_score=0.0,
        solver_time_ms=10,
    )


class FakeLoadController:
    """Test double implementing LoadController protocol."""

    def __init__(self, load_id: str, name: str, power_w: int = 500, priority_class: int = 5) -> None:
        self._load_id = load_id
        self._name = name
        self._power_w = power_w
        self._priority_class = priority_class
        self._state = LoadState.OFF
        self._available = True

    @property
    def load_id(self) -> str:
        return self._load_id

    @property
    def name(self) -> str:
        return self._name

    @property
    def power_w(self) -> int:
        return self._power_w

    @property
    def priority_class(self) -> int:
        return self._priority_class

    async def turn_on(self) -> bool:
        self._state = LoadState.ON
        return True

    async def turn_off(self) -> bool:
        self._state = LoadState.OFF
        return True

    async def get_status(self) -> LoadStatus:
        return LoadStatus(
            load_id=self._load_id,
            name=self._name,
            state=self._state,
            power_w=self._power_w if self._state == LoadState.ON else 0,
            is_available=self._available,
        )

    async def is_available(self) -> bool:
        return self._available


# ── Protocol Tests ───────────────────────────────────────────


class TestLoadControllerProtocol:
    def test_fake_satisfies_protocol(self) -> None:
        controller = FakeLoadController("test_1", "Test Load")
        assert isinstance(controller, LoadController)

    def test_shelly_satisfies_protocol(self) -> None:
        config = _make_shelly_config()
        adapter = ShellyAdapter(config)
        assert isinstance(adapter, LoadController)

    def test_mqtt_satisfies_protocol(self) -> None:
        config = _make_mqtt_config()
        publish_fn = AsyncMock()
        adapter = MQTTLoadAdapter(config, publish_fn)
        assert isinstance(adapter, LoadController)


# ── Shelly Adapter Tests ──────────────────────────────────────


class TestShellyAdapter:
    def test_properties(self) -> None:
        config = _make_shelly_config(name="test_shelly", power_w=2000, priority_class=3)
        adapter = ShellyAdapter(config)
        assert adapter.load_id == "shelly_test_shelly"
        assert adapter.name == "test_shelly"
        assert adapter.power_w == 2000
        assert adapter.priority_class == 3

    @pytest.mark.asyncio
    async def test_turn_on_success(self) -> None:
        config = _make_shelly_config()
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None
        mock_client.post = AsyncMock(return_value=mock_response)

        adapter = ShellyAdapter(config, client=mock_client)
        result = await adapter.turn_on()

        assert result is True
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "/rpc/Switch.Set" in call_args[0][0]
        assert call_args[1]["json"]["on"] is True

    @pytest.mark.asyncio
    async def test_turn_off_success(self) -> None:
        config = _make_shelly_config()
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None
        mock_client.post = AsyncMock(return_value=mock_response)

        adapter = ShellyAdapter(config, client=mock_client)
        result = await adapter.turn_off()

        assert result is True
        call_args = mock_client.post.call_args
        assert call_args[1]["json"]["on"] is False

    @pytest.mark.asyncio
    async def test_get_status_success(self) -> None:
        config = _make_shelly_config()
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: {"output": True, "apower": 1150.5}
        mock_client.get = AsyncMock(return_value=mock_response)

        adapter = ShellyAdapter(config, client=mock_client)
        status = await adapter.get_status()

        assert status.state == LoadState.ON
        assert status.power_w == 1150
        assert status.is_available is True

    @pytest.mark.asyncio
    async def test_get_status_off(self) -> None:
        config = _make_shelly_config()
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: {"output": False, "apower": 0}
        mock_client.get = AsyncMock(return_value=mock_response)

        adapter = ShellyAdapter(config, client=mock_client)
        status = await adapter.get_status()

        assert status.state == LoadState.OFF
        assert status.power_w == 0

    @pytest.mark.asyncio
    async def test_get_status_gen1_fallback(self) -> None:
        config = _make_shelly_config()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.HTTPError("rpc failed"))
        gen1_response = AsyncMock()
        gen1_response.status_code = 200
        gen1_response.raise_for_status = lambda: None
        gen1_response.json = lambda: {"ison": True, "power": 987.4}
        # GET is tried twice: first Gen2 GET (fails), then Gen1 GET (succeeds)
        mock_client.get = AsyncMock(
            side_effect=[httpx.HTTPError("gen2 get failed"), gen1_response],
        )

        adapter = ShellyAdapter(config, client=mock_client)
        status = await adapter.get_status()

        assert status.state == LoadState.ON
        assert status.power_w == 987
        assert status.is_available is True

    @pytest.mark.asyncio
    async def test_turn_on_gen1_fallback(self) -> None:
        config = _make_shelly_config()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.HTTPError("rpc failed"))
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None
        mock_client.get = AsyncMock(return_value=mock_response)

        adapter = ShellyAdapter(config, client=mock_client)
        result = await adapter.turn_on()

        assert result is True
        mock_client.get.assert_called_once()


# ── MQTT Load Adapter Tests ──────────────────────────────────


class TestMQTTLoadAdapter:
    def test_properties(self) -> None:
        config = _make_mqtt_config(name="test_mqtt", power_w=1500, priority_class=4)
        adapter = MQTTLoadAdapter(config, AsyncMock())
        assert adapter.load_id == "mqtt_test_mqtt"
        assert adapter.name == "test_mqtt"
        assert adapter.power_w == 1500
        assert adapter.priority_class == 4
        assert adapter.command_topic == "power_master/load/hot_water/command"
        assert adapter.state_topic == "power_master/load/hot_water/state"

    @pytest.mark.asyncio
    async def test_turn_on(self) -> None:
        publish_fn = AsyncMock()
        config = _make_mqtt_config()
        adapter = MQTTLoadAdapter(config, publish_fn)

        result = await adapter.turn_on()

        assert result is True
        publish_fn.assert_called_once_with(config.command_topic, "ON", False)

    @pytest.mark.asyncio
    async def test_turn_off(self) -> None:
        publish_fn = AsyncMock()
        config = _make_mqtt_config()
        adapter = MQTTLoadAdapter(config, publish_fn)

        result = await adapter.turn_off()

        assert result is True
        publish_fn.assert_called_once_with(config.command_topic, "OFF", False)

    @pytest.mark.asyncio
    async def test_status_after_on(self) -> None:
        publish_fn = AsyncMock()
        config = _make_mqtt_config()
        adapter = MQTTLoadAdapter(config, publish_fn)

        await adapter.turn_on()
        status = await adapter.get_status()

        assert status.state == LoadState.ON
        assert status.power_w == config.power_w

    @pytest.mark.asyncio
    async def test_status_unknown_initially(self) -> None:
        publish_fn = AsyncMock()
        config = _make_mqtt_config()
        adapter = MQTTLoadAdapter(config, publish_fn)

        status = await adapter.get_status()

        assert status.state == LoadState.UNKNOWN
        assert status.power_w == 0

    def test_handle_state_update_on(self) -> None:
        config = _make_mqtt_config()
        adapter = MQTTLoadAdapter(config, AsyncMock())

        for payload in ["ON", "on", "1", "true", "TRUE"]:
            adapter.handle_state_update(payload)
            assert adapter._last_known_state == LoadState.ON

    def test_handle_state_update_off(self) -> None:
        config = _make_mqtt_config()
        adapter = MQTTLoadAdapter(config, AsyncMock())

        for payload in ["OFF", "off", "0", "false", "FALSE"]:
            adapter.handle_state_update(payload)
            assert adapter._last_known_state == LoadState.OFF

    def test_handle_state_update_unknown(self) -> None:
        config = _make_mqtt_config()
        adapter = MQTTLoadAdapter(config, AsyncMock())

        adapter.handle_state_update("garbage")
        assert adapter._last_known_state == LoadState.UNKNOWN

    @pytest.mark.asyncio
    async def test_publish_failure_marks_unavailable(self) -> None:
        publish_fn = AsyncMock(side_effect=ConnectionError("broker down"))
        config = _make_mqtt_config()
        adapter = MQTTLoadAdapter(config, publish_fn)

        result = await adapter.turn_on()

        assert result is False
        assert adapter._available is False


# ── Load Manager Tests ────────────────────────────────────────


class TestLoadManager:
    @pytest.mark.asyncio
    async def test_register_and_list(self) -> None:
        manager = LoadManager(AppConfig())
        ctrl = FakeLoadController("load_1", "Test Load 1")
        manager.register(ctrl)

        assert "load_1" in manager.controllers
        statuses = await manager.get_all_statuses()
        assert len(statuses) == 1
        assert statuses[0].load_id == "load_1"

    @pytest.mark.asyncio
    async def test_unregister(self) -> None:
        manager = LoadManager(AppConfig())
        ctrl = FakeLoadController("load_1", "Test Load 1")
        manager.register(ctrl)
        manager.unregister("load_1")

        assert "load_1" not in manager.controllers

    @pytest.mark.asyncio
    async def test_shed_for_spike(self) -> None:
        manager = LoadManager(AppConfig())
        essential = FakeLoadController("essential", "Essential", priority_class=1)
        essential._state = LoadState.ON
        deferrable = FakeLoadController("deferrable", "Deferrable", priority_class=4)
        deferrable._state = LoadState.ON

        manager.register(essential)
        manager.register(deferrable)

        commands = await manager.shed_for_spike(max_priority=2)

        # Only deferrable should be shed
        assert len(commands) == 1
        assert commands[0].load_id == "deferrable"
        assert commands[0].action == "off"

        # Essential should still be on
        essential_status = await essential.get_status()
        assert essential_status.state == LoadState.ON

    @pytest.mark.asyncio
    async def test_restore_after_spike(self) -> None:
        manager = LoadManager(AppConfig())
        ctrl = FakeLoadController("load_1", "Load 1", priority_class=4)
        ctrl._state = LoadState.ON
        manager.register(ctrl)

        await manager.shed_for_spike(max_priority=2)
        assert "load_1" in manager._shed_loads

        await manager.restore_after_spike()
        assert len(manager._shed_loads) == 0

    @pytest.mark.asyncio
    async def test_turn_all_off(self) -> None:
        manager = LoadManager(AppConfig())
        ctrl1 = FakeLoadController("load_1", "Load 1")
        ctrl1._state = LoadState.ON
        ctrl2 = FakeLoadController("load_2", "Load 2")
        ctrl2._state = LoadState.ON

        manager.register(ctrl1)
        manager.register(ctrl2)

        commands = await manager.turn_all_off(reason="test")

        assert len(commands) == 2
        assert all(c.action == "off" for c in commands)

    @pytest.mark.asyncio
    async def test_execute_schedule(self) -> None:
        manager = LoadManager(AppConfig())
        ctrl = FakeLoadController("load_1", "Load 1")
        manager.register(ctrl)

        scheduled = [
            ScheduledLoad(
                load_id="load_1",
                name="Load 1",
                power_w=500,
                priority_class=5,
                assigned_slots=[0, 1, 2],
            ),
        ]

        # Slot 0 — load should turn on
        commands = await manager.execute_schedule(scheduled, current_slot_index=0)
        assert len(commands) == 1
        assert commands[0].action == "on"

        # Check it's now on
        status = await ctrl.get_status()
        assert status.state == LoadState.ON

    def test_get_load_configs(self) -> None:
        manager = LoadManager(AppConfig())
        ctrl = FakeLoadController("load_1", "Load 1", power_w=1200, priority_class=3)
        manager.register(ctrl)

        configs = manager.get_load_configs()
        assert len(configs) == 1
        assert configs[0]["id"] == "load_1"
        assert configs[0]["power_w"] == 1200
        assert configs[0]["priority_class"] == 3

    @pytest.mark.asyncio
    async def test_set_load_override_turns_on(self) -> None:
        manager = LoadManager(AppConfig())
        ctrl = FakeLoadController("load_1", "Load 1")
        manager.register(ctrl)

        success = await manager.set_load_override("load_1", "on", timeout_seconds=3600)
        assert success is True
        status = await ctrl.get_status()
        assert status.state == LoadState.ON

    @pytest.mark.asyncio
    async def test_set_load_override_turns_off(self) -> None:
        manager = LoadManager(AppConfig())
        ctrl = FakeLoadController("load_1", "Load 1")
        ctrl._state = LoadState.ON
        manager.register(ctrl)

        success = await manager.set_load_override("load_1", "off", timeout_seconds=3600)
        assert success is True
        status = await ctrl.get_status()
        assert status.state == LoadState.OFF

    @pytest.mark.asyncio
    async def test_load_override_unknown_id_returns_false(self) -> None:
        manager = LoadManager(AppConfig())
        success = await manager.set_load_override("nonexistent", "on")
        assert success is False

    def test_get_load_override_returns_active(self) -> None:
        from power_master.loads.manager import LoadOverride
        manager = LoadManager(AppConfig())
        manager._load_overrides["load_1"] = LoadOverride(
            load_id="load_1", state="on", timeout_seconds=3600
        )
        override = manager.get_load_override("load_1")
        assert override is not None
        assert override.state == "on"

    def test_get_load_override_returns_none_for_unknown(self) -> None:
        manager = LoadManager(AppConfig())
        assert manager.get_load_override("unknown") is None

    def test_clear_load_override(self) -> None:
        from power_master.loads.manager import LoadOverride
        manager = LoadManager(AppConfig())
        manager._load_overrides["load_1"] = LoadOverride(
            load_id="load_1", state="on", timeout_seconds=3600
        )
        manager.clear_load_override("load_1")
        assert manager.get_load_override("load_1") is None

    @pytest.mark.asyncio
    async def test_execute_schedule_respects_override(self) -> None:
        """When a load is in manual override, execute_schedule should not change its state."""
        from power_master.loads.manager import LoadOverride
        manager = LoadManager(AppConfig())
        ctrl = FakeLoadController("load_1", "Load 1")
        manager.register(ctrl)

        # Set load to manual OFF override
        manager._load_overrides["load_1"] = LoadOverride(
            load_id="load_1", state="off", timeout_seconds=3600
        )

        # Schedule says load should be ON in slot 0
        scheduled = [
            ScheduledLoad(
                load_id="load_1",
                name="Load 1",
                power_w=500,
                priority_class=5,
                assigned_slots=[0],
            ),
        ]
        # Execute with the override active — load should stay OFF
        commands = await manager.execute_schedule(scheduled, current_slot_index=0)
        status = await ctrl.get_status()
        assert status.state == LoadState.OFF

    def test_get_command_history_for_load(self) -> None:
        import time
        from power_master.loads.manager import LoadCommand
        manager = LoadManager(AppConfig())
        cmd1 = LoadCommand(load_id="load_1", action="on", reason="scheduled")
        cmd2 = LoadCommand(load_id="load_2", action="off", reason="scheduled")
        cmd3 = LoadCommand(load_id="load_1", action="off", reason="manual")
        manager._command_history = [cmd1, cmd2, cmd3]

        history = manager.get_command_history_for_load("load_1")
        assert len(history) == 2
        assert history[0].action == "on"
        assert history[1].action == "off"

    def test_get_active_override_load_ids(self) -> None:
        from power_master.loads.manager import LoadOverride
        manager = LoadManager(AppConfig())
        manager._load_overrides["load_1"] = LoadOverride(
            load_id="load_1", state="on", timeout_seconds=3600
        )
        ids = manager.get_active_override_load_ids()
        assert "load_1" in ids


# ── Load Scheduler Tests ──────────────────────────────────────


class TestLoadSchedulerOverride:
    def test_manual_override_ids_skipped(self) -> None:
        plan = _make_plan(n_slots=4)
        loads = [
            {"id": "pump", "name": "Pump", "power_w": 500, "priority_class": 3, "min_runtime_minutes": 30},
        ]
        result = schedule_loads(plan, loads, manual_override_load_ids={"pump"})
        assert len(result) == 0

    def test_non_overridden_loads_still_scheduled(self) -> None:
        plan = _make_plan(n_slots=4)
        loads = [
            {"id": "pump", "name": "Pump", "power_w": 500, "priority_class": 3, "min_runtime_minutes": 30},
            {"id": "heater", "name": "Heater", "power_w": 1000, "priority_class": 4, "min_runtime_minutes": 30},
        ]
        result = schedule_loads(plan, loads, manual_override_load_ids={"pump"})
        ids = [s.load_id for s in result]
        assert "pump" not in ids
        assert "heater" in ids




class TestLoadScheduler:
    def test_min_runtime_overrides_duration(self) -> None:
        plan = _make_plan(n_slots=16)
        loads = [
            {
                "id": "pool_pump",
                "name": "Pool Pump",
                "power_w": 1200,
                "priority_class": 4,
                "min_runtime_minutes": 60,
                "min_runtime_minutes": 180,
                "prefer_solar": False,
                "timezone": "UTC",
                "days_of_week": [0, 1, 2, 3, 4, 5, 6],
                "earliest_start": "00:00",
                "latest_end": "23:59",
            },
        ]

        result = schedule_loads(plan, loads)
        assert len(result) == 1
        # 180min at 30min slots = 6 slots
        assert len(result[0].assigned_slots) == 6

    def test_respects_time_window_and_days_of_week(self) -> None:
        base = datetime(2026, 1, 8, 0, 0, tzinfo=timezone.utc)  # Thursday
        slots = []
        for i in range(48):  # 24h
            start = base + timedelta(minutes=30 * i)
            end = start + timedelta(minutes=30)
            slots.append(PlanSlot(
                index=i,
                start=start,
                end=end,
                mode=SlotMode.SELF_USE,
                solar_forecast_w=0.0,
                load_forecast_w=500.0,
                import_rate_cents=10.0,
            ))
        plan = OptimisationPlan(
            version=1,
            created_at=base,
            trigger_reason="periodic",
            horizon_start=base,
            horizon_end=base + timedelta(hours=24),
            slots=slots,
            objective_score=0.0,
            solver_time_ms=10,
        )

        loads = [
            {
                "id": "pool_pump",
                "name": "Pool Pump",
                "power_w": 1200,
                "priority_class": 4,
                "min_runtime_minutes": 180,
                "prefer_solar": False,
                "timezone": "UTC",
                "days_of_week": [3],  # Thursday
                "earliest_start": "08:00",
                "latest_end": "16:00",
            },
        ]

        result = schedule_loads(plan, loads)
        assert len(result) == 1
        assert len(result[0].assigned_slots) == 6
        for idx in result[0].assigned_slots:
            slot = plan.slots[idx]
            assert slot.start.weekday() == 3
            assert 8 <= slot.start.hour < 16

    def test_schedules_once_per_day_in_horizon(self) -> None:
        base = datetime(2026, 1, 8, 0, 0, tzinfo=timezone.utc)  # Thursday
        slots = []
        for i in range(96):  # 48h
            start = base + timedelta(minutes=30 * i)
            end = start + timedelta(minutes=30)
            slots.append(PlanSlot(
                index=i,
                start=start,
                end=end,
                mode=SlotMode.SELF_USE,
                solar_forecast_w=0.0,
                load_forecast_w=500.0,
                import_rate_cents=10.0,
            ))
        plan = OptimisationPlan(
            version=1,
            created_at=base,
            trigger_reason="periodic",
            horizon_start=base,
            horizon_end=base + timedelta(hours=48),
            slots=slots,
            objective_score=0.0,
            solver_time_ms=10,
        )

        loads = [
            {
                "id": "pool_pump",
                "name": "Pool Pump",
                "power_w": 1200,
                "priority_class": 4,
                "min_runtime_minutes": 60,  # 2 slots/day
                "prefer_solar": False,
                "timezone": "UTC",
                "days_of_week": [3, 4],  # Thu/Fri
                "earliest_start": "08:00",
                "latest_end": "16:00",
            },
        ]

        result = schedule_loads(plan, loads)
        assert len(result) == 1
        # 2 days in horizon * 2 slots/day
        assert len(result[0].assigned_slots) == 4
        days = {plan.slots[idx].start.date() for idx in result[0].assigned_slots}
        assert len(days) == 2

    def test_schedule_into_cheap_slots(self) -> None:
        plan = _make_plan(n_slots=8)
        loads = [
            {
                "id": "pool_pump",
                "name": "Pool Pump",
                "power_w": 1200,
                "priority_class": 4,
                "min_runtime_minutes": 60,
                "prefer_solar": False,
            },
        ]

        result = schedule_loads(plan, loads)

        assert len(result) == 1
        sched = result[0]
        assert sched.load_id == "pool_pump"
        assert len(sched.assigned_slots) == 2  # 60min / 30min = 2 slots
        # Should prefer cheaper slots (index 4-7 at 5c vs 0-3 at 20c)
        for idx in sched.assigned_slots:
            assert plan.slots[idx].import_rate_cents == 5.0

    def test_schedule_prefers_solar(self) -> None:
        plan = _make_plan(n_slots=8, solar_w=5000.0, load_w=500.0)
        loads = [
            {
                "id": "hot_water",
                "name": "Hot Water",
                "power_w": 3000,
                "priority_class": 3,
                "min_runtime_minutes": 30,
                "prefer_solar": True,
            },
        ]

        result = schedule_loads(plan, loads)

        assert len(result) == 1
        # With 5000W solar and 500W load, excess solar is 4500W > 3000W power_w
        # So any slot qualifies for the solar bonus — scheduler should use it

    def test_spike_defers_non_essential(self) -> None:
        plan = _make_plan(n_slots=4)
        loads = [
            {"id": "essential", "name": "Essential", "power_w": 500, "priority_class": 1, "min_runtime_minutes": 30},
            {"id": "deferrable", "name": "Deferrable", "power_w": 1200, "priority_class": 4, "min_runtime_minutes": 30},
        ]

        result = schedule_loads(plan, loads, spike_active=True)

        # Essential should be scheduled, deferrable deferred
        ids = [s.load_id for s in result]
        assert "essential" in ids
        assert "deferrable" not in ids

    def test_disabled_load_skipped(self) -> None:
        plan = _make_plan(n_slots=4)
        loads = [
            {"id": "disabled", "name": "Disabled", "power_w": 500, "priority_class": 3, "min_runtime_minutes": 30, "enabled": False},
        ]

        result = schedule_loads(plan, loads)
        assert len(result) == 0

    def test_updates_plan_slots(self) -> None:
        plan = _make_plan(n_slots=4)
        loads = [
            {"id": "pump", "name": "Pump", "power_w": 500, "priority_class": 5, "min_runtime_minutes": 30, "prefer_solar": False},
        ]

        result = schedule_loads(plan, loads)

        assert len(result) == 1
        # The assigned slot should have the load name in scheduled_loads
        idx = result[0].assigned_slots[0]
        assert plan.slots[idx].scheduled_loads is not None
        assert "Pump" in plan.slots[idx].scheduled_loads
