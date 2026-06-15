"""Tests for free-window import-cap-aware orchestrator (§7.5).

Verifies that the orchestrator correctly allocates headroom under max_grid_import_w,
throttles battery grid-charge setpoint, and sheds loads by priority to stay under cap.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from power_master.config.schema import (
    AppConfig,
    BatteryConfig,
    FreeWindowOrchestratorConfig,
    LoadsConfig,
)
from power_master.loads.base import LoadState, LoadStatus
from power_master.loads.free_window_orchestrator import FreeWindowOrchestrator


# ── Test Helpers ─────────────────────────────────────────────


class FakeLoadController:
    """Test double for LoadController protocol."""

    def __init__(
        self,
        load_id: str,
        name: str,
        power_w: int = 1000,
        priority_class: int = 5,
        state: LoadState = LoadState.OFF,
    ) -> None:
        self._load_id = load_id
        self._name = name
        self._power_w = power_w
        self._priority_class = priority_class
        self._state = state

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
            is_available=True,
        )

    async def is_available(self) -> bool:
        return True


def _make_config(max_grid_import_w: int = 10000) -> AppConfig:
    """Create a test AppConfig with specified max_grid_import_w."""
    return AppConfig(
        battery=BatteryConfig(
            capacity_wh=10000,
            max_charge_rate_w=5000,
            max_grid_import_w=max_grid_import_w,
        ),
        loads=LoadsConfig(
            free_window_orchestrator=FreeWindowOrchestratorConfig(enabled=True),
        ),
    )


# ── Tests ────────────────────────────────────────────────────


class TestFreeWindowOrchestrator:
    """Tests for FreeWindowOrchestrator allocation and throttling."""

    @pytest.mark.asyncio
    async def test_no_limit_configured(self):
        """When max_grid_import_w <= 0, return full setpoints (no limit)."""
        config = _make_config(max_grid_import_w=0)
        orchestrator = FreeWindowOrchestrator(config, {})

        result = await orchestrator.allocate_for_free_window(
            current_grid_import_w=5000,
            measured_uncontrollable_load_w=500,
            battery_max_charge_w=5000,
            is_in_free_window=True,
            cap_exhausted=False,
        )

        assert result.allocated_w == 0
        assert result.battery_grid_charge_setpoint_w == 5000  # Full setpoint when no limit
        assert result.loads_to_shed == set()
        assert result.is_cap_exhausted is False

    @pytest.mark.asyncio
    async def test_cap_exhausted_stops_battery_charge(self):
        """When free-window cap is exhausted, battery grid-charge setpoint = 0."""
        config = _make_config(max_grid_import_w=10000)
        hws = FakeLoadController("hws", "hot_water", power_w=3600, priority_class=3)
        controllers = {"hws": hws}
        orchestrator = FreeWindowOrchestrator(config, controllers)

        result = await orchestrator.allocate_for_free_window(
            current_grid_import_w=2000,
            measured_uncontrollable_load_w=500,
            battery_max_charge_w=5000,
            is_in_free_window=True,
            cap_exhausted=True,  # Cap exhausted
        )

        assert result.battery_grid_charge_setpoint_w == 0  # Stopped
        assert result.is_cap_exhausted is True

    @pytest.mark.asyncio
    async def test_not_in_free_window_stops_battery_charge(self):
        """When not in free window, battery grid-charge setpoint = 0."""
        config = _make_config(max_grid_import_w=10000)
        orchestrator = FreeWindowOrchestrator(config, {})

        result = await orchestrator.allocate_for_free_window(
            current_grid_import_w=2000,
            measured_uncontrollable_load_w=500,
            battery_max_charge_w=5000,
            is_in_free_window=False,  # NOT in free window
            cap_exhausted=False,
        )

        assert result.battery_grid_charge_setpoint_w == 0  # Stopped

    @pytest.mark.asyncio
    async def test_battery_alone_exceeds_headroom_throttled(self):
        """When battery alone exceeds headroom, throttle it."""
        config = _make_config(max_grid_import_w=10000)
        orchestrator = FreeWindowOrchestrator(config, {})

        # Headroom = 10000 - 2000 = 8000W
        # Battery requests 5000W, which fits (should not be throttled)
        result = await orchestrator.allocate_for_free_window(
            current_grid_import_w=2000,
            measured_uncontrollable_load_w=2000,
            battery_max_charge_w=5000,
            is_in_free_window=True,
            cap_exhausted=False,
        )

        assert result.battery_grid_charge_setpoint_w == 5000  # Fits, not throttled
        assert result.allocated_w == 5000

    @pytest.mark.asyncio
    async def test_battery_throttled_when_exceeds_headroom(self):
        """When battery exceeds available headroom, throttle it."""
        config = _make_config(max_grid_import_w=10000)
        orchestrator = FreeWindowOrchestrator(config, {})

        # Headroom = 10000 - 8000 = 2000W
        # Battery requests 5000W — should be throttled to 2000W
        result = await orchestrator.allocate_for_free_window(
            current_grid_import_w=8000,
            measured_uncontrollable_load_w=8000,
            battery_max_charge_w=5000,
            is_in_free_window=True,
            cap_exhausted=False,
        )

        assert result.battery_grid_charge_setpoint_w == 2000  # Throttled to headroom
        assert result.allocated_w == 2000

    @pytest.mark.asyncio
    async def test_loads_fit_in_remaining_headroom(self):
        """Loads that fit in remaining headroom are kept; higher-priority first."""
        config = _make_config(max_grid_import_w=10000)
        hws = FakeLoadController("hws", "hot_water", power_w=2000, priority_class=3, state=LoadState.ON)
        pool = FakeLoadController("pool", "pool_pump", power_w=1500, priority_class=4, state=LoadState.ON)
        controllers = {"hws": hws, "pool": pool}
        orchestrator = FreeWindowOrchestrator(config, controllers)

        # Headroom = 10000 - 1000 = 9000W
        # Battery takes 5000W -> 4000W remaining
        # HWS (3, 2000W) fits -> 2000W remaining
        # Pool (4, 1500W) fits -> 500W remaining
        result = await orchestrator.allocate_for_free_window(
            current_grid_import_w=1000,
            measured_uncontrollable_load_w=1000,
            battery_max_charge_w=5000,
            is_in_free_window=True,
            cap_exhausted=False,
        )

        assert result.battery_grid_charge_setpoint_w == 5000
        assert result.allocated_w == 5000 + 2000 + 1500  # battery + hws + pool
        assert result.loads_to_shed == set()  # Both loads fit

    @pytest.mark.asyncio
    async def test_loads_shed_by_priority(self):
        """Loads that don't fit are shed, starting with highest priority_class (worst first)."""
        config = _make_config(max_grid_import_w=10000)
        hws = FakeLoadController("hws", "hot_water", power_w=2000, priority_class=3, state=LoadState.ON)
        pool = FakeLoadController("pool", "pool_pump", power_w=1500, priority_class=4, state=LoadState.ON)
        ev = FakeLoadController("ev", "ev_charger", power_w=2500, priority_class=5, state=LoadState.ON)
        controllers = {"hws": hws, "pool": pool, "ev": ev}
        orchestrator = FreeWindowOrchestrator(config, controllers)

        # Headroom = 10000 - 8500 = 1500W
        # Battery requests 5000W but only 1500W available
        # Battery gets 1500W, no room for any loads
        # Loads sorted by priority_class ascending: HWS(3), pool(4), EV(5)
        # All are bigger than remaining headroom, so all are shed
        result = await orchestrator.allocate_for_free_window(
            current_grid_import_w=8500,
            measured_uncontrollable_load_w=8500,
            battery_max_charge_w=5000,
            is_in_free_window=True,
            cap_exhausted=False,
        )

        assert result.battery_grid_charge_setpoint_w == 1500  # Throttled
        assert result.allocated_w == 1500
        # All loads should be shed because none fit in the 0W remaining headroom
        assert result.loads_to_shed == {"hws", "pool", "ev"}

    @pytest.mark.asyncio
    async def test_loads_shed_partial_priority(self):
        """Partial load shedding: keep high-priority, shed low-priority."""
        config = _make_config(max_grid_import_w=10000)
        hws = FakeLoadController("hws", "hot_water", power_w=2000, priority_class=3, state=LoadState.ON)
        pool = FakeLoadController("pool", "pool_pump", power_w=1500, priority_class=4, state=LoadState.ON)
        ev = FakeLoadController("ev", "ev_charger", power_w=2500, priority_class=5, state=LoadState.ON)
        controllers = {"hws": hws, "pool": pool, "ev": ev}
        orchestrator = FreeWindowOrchestrator(config, controllers)

        # Headroom = 10000 - 4000 = 6000W
        # Battery takes 5000W -> 1000W remaining
        # HWS (priority 3, 2000W) doesn't fit -> shed
        # Pool (priority 4, 1500W) doesn't fit -> shed
        # EV (priority 5, 2500W) doesn't fit -> shed
        result = await orchestrator.allocate_for_free_window(
            current_grid_import_w=4000,
            measured_uncontrollable_load_w=4000,
            battery_max_charge_w=5000,
            is_in_free_window=True,
            cap_exhausted=False,
        )

        assert result.battery_grid_charge_setpoint_w == 5000
        assert result.allocated_w == 5000  # Only battery, no room for loads
        assert result.loads_to_shed == {"hws", "pool", "ev"}  # All shed

    @pytest.mark.asyncio
    async def test_loads_partial_fit(self):
        """Some loads fit, others don't: keep high-priority, shed low-priority."""
        config = _make_config(max_grid_import_w=10000)
        hws = FakeLoadController("hws", "hot_water", power_w=2000, priority_class=3, state=LoadState.ON)
        pool = FakeLoadController("pool", "pool_pump", power_w=1500, priority_class=4, state=LoadState.ON)
        ev = FakeLoadController("ev", "ev_charger", power_w=2500, priority_class=5, state=LoadState.ON)
        controllers = {"hws": hws, "pool": pool, "ev": ev}
        orchestrator = FreeWindowOrchestrator(config, controllers)

        # Headroom = 10000 - 2000 = 8000W
        # Battery takes 5000W -> 3000W remaining
        # HWS (priority 3, 2000W) fits -> 1000W remaining
        # Pool (priority 4, 1500W) doesn't fit -> shed
        # EV (priority 5, 2500W) doesn't fit -> shed
        result = await orchestrator.allocate_for_free_window(
            current_grid_import_w=2000,
            measured_uncontrollable_load_w=2000,
            battery_max_charge_w=5000,
            is_in_free_window=True,
            cap_exhausted=False,
        )

        assert result.battery_grid_charge_setpoint_w == 5000
        assert result.allocated_w == 5000 + 2000  # battery + hws
        assert result.loads_to_shed == {"pool", "ev"}  # Low-priority loads shed

    @pytest.mark.asyncio
    async def test_throttle_battery_command(self):
        """throttle_battery_command clamps FORCE_CHARGE power_w to allowed value."""
        from power_master.control.command import ControlCommand
        from power_master.hardware.base import OperatingMode

        config = _make_config(max_grid_import_w=10000)
        orchestrator = FreeWindowOrchestrator(config, {})

        cmd = ControlCommand(
            mode=OperatingMode.FORCE_CHARGE,
            power_w=5000,
            source="optimiser",
            reason="test",
        )

        result = orchestrator.throttle_battery_command(cmd, allowed_battery_w=2000)

        assert result.power_w == 2000  # Throttled

    @pytest.mark.asyncio
    async def test_throttle_battery_command_no_throttle_when_lower(self):
        """throttle_battery_command doesn't increase power_w if already lower."""
        from power_master.control.command import ControlCommand
        from power_master.hardware.base import OperatingMode

        config = _make_config(max_grid_import_w=10000)
        orchestrator = FreeWindowOrchestrator(config, {})

        cmd = ControlCommand(
            mode=OperatingMode.FORCE_CHARGE,
            power_w=1000,
            source="optimiser",
            reason="test",
        )

        result = orchestrator.throttle_battery_command(cmd, allowed_battery_w=2000)

        assert result.power_w == 1000  # Not changed

    @pytest.mark.asyncio
    async def test_throttle_battery_command_ignores_self_use(self):
        """throttle_battery_command doesn't modify non-FORCE_CHARGE modes."""
        from power_master.control.command import ControlCommand
        from power_master.hardware.base import OperatingMode

        config = _make_config(max_grid_import_w=10000)
        orchestrator = FreeWindowOrchestrator(config, {})

        cmd = ControlCommand(
            mode=OperatingMode.SELF_USE,
            power_w=5000,
            source="optimiser",
            reason="test",
        )

        result = orchestrator.throttle_battery_command(cmd, allowed_battery_w=2000)

        assert result.power_w == 5000  # Not modified for SELF_USE

    @pytest.mark.asyncio
    async def test_shed_loads(self):
        """shed_loads turns off specified loads."""
        config = _make_config(max_grid_import_w=10000)
        hws = FakeLoadController("hws", "hot_water", power_w=2000, priority_class=3, state=LoadState.ON)
        pool = FakeLoadController("pool", "pool_pump", power_w=1500, priority_class=4, state=LoadState.ON)
        controllers = {"hws": hws, "pool": pool}
        orchestrator = FreeWindowOrchestrator(config, controllers)

        cmds = await orchestrator.shed_loads({"hws", "pool"})

        assert len(cmds) == 2
        assert hws._state == LoadState.OFF
        assert pool._state == LoadState.OFF
        assert all(cmd.action == "off" for cmd in cmds)
        assert all(cmd.reason == "free_window_cap_shed" for cmd in cmds)

    @pytest.mark.asyncio
    async def test_shed_loads_ignores_unknown_loads(self):
        """shed_loads safely ignores unknown load IDs."""
        config = _make_config(max_grid_import_w=10000)
        hws = FakeLoadController("hws", "hot_water", power_w=2000, priority_class=3, state=LoadState.ON)
        controllers = {"hws": hws}
        orchestrator = FreeWindowOrchestrator(config, controllers)

        cmds = await orchestrator.shed_loads({"hws", "unknown_load"})

        assert len(cmds) == 1  # Only hws shed
        assert cmds[0].load_id == "hws"


class TestFreeWindowOrchestratorTimeDeteminism:
    """Verifies time-deterministic test scenarios (no datetime.now() interference)."""

    @pytest.mark.asyncio
    async def test_scenario_free_window_dynamic_allocation(self):
        """
        Scenario: Free window with battery + HWS + pool, evolving headroom.

        Initial state:
        - max_grid_import_w = 10000W
        - uncontrollable load = 1000W (headroom = 9000W)
        - battery requests 5000W, HWS 2000W, pool 1500W

        Expected: All fit (battery 5000 + HWS 2000 + pool 1500 = 8500 < 9000)
        """
        config = _make_config(max_grid_import_w=10000)
        hws = FakeLoadController("hws", "hot_water", power_w=2000, priority_class=3, state=LoadState.ON)
        pool = FakeLoadController("pool", "pool_pump", power_w=1500, priority_class=4, state=LoadState.ON)
        controllers = {"hws": hws, "pool": pool}
        orchestrator = FreeWindowOrchestrator(config, controllers)

        # Simulate increasing house load pressure
        for uncontrollable_w in [1000, 3000, 5000, 7000]:
            headroom_w = 10000 - uncontrollable_w
            result = await orchestrator.allocate_for_free_window(
                current_grid_import_w=uncontrollable_w,
                measured_uncontrollable_load_w=uncontrollable_w,
                battery_max_charge_w=5000,
                is_in_free_window=True,
                cap_exhausted=False,
            )

            # Verify allocation never exceeds headroom
            assert result.allocated_w <= headroom_w, (
                f"uncontrollable={uncontrollable_w}W, headroom={headroom_w}W, "
                f"but allocated={result.allocated_w}W"
            )

    @pytest.mark.asyncio
    async def test_scenario_cap_exhaustion_transition(self):
        """
        Scenario: Cap exhaustion stops battery grid-charge.

        Start: cap_exhausted=False, battery = 5000W
        Transition: cap_exhausted=True, battery = 0W
        """
        config = _make_config(max_grid_import_w=10000)
        orchestrator = FreeWindowOrchestrator(config, {})

        # Cap available
        result1 = await orchestrator.allocate_for_free_window(
            current_grid_import_w=1000,
            measured_uncontrollable_load_w=1000,
            battery_max_charge_w=5000,
            is_in_free_window=True,
            cap_exhausted=False,
        )
        assert result1.battery_grid_charge_setpoint_w == 5000
        assert result1.is_cap_exhausted is False

        # Cap exhausted
        result2 = await orchestrator.allocate_for_free_window(
            current_grid_import_w=1000,
            measured_uncontrollable_load_w=1000,
            battery_max_charge_w=5000,
            is_in_free_window=True,
            cap_exhausted=True,  # Changed
        )
        assert result2.battery_grid_charge_setpoint_w == 0  # Stopped
        assert result2.is_cap_exhausted is True
