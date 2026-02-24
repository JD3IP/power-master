"""5-level control priority hierarchy.

Priority levels (lower number = higher priority):
1. Safety — Hard SOC limits, hardware protection
2. Storm reserve — Maintain SOC above storm threshold
3. Critical loads — Keep essential loads powered
4. Cost optimisation — Follow the MILP plan
5. Opportunistic — Arbitrage, load scheduling
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from power_master.control.command import ControlCommand
from power_master.hardware.base import OperatingMode

logger = logging.getLogger(__name__)


@dataclass
class HierarchyResult:
    """Result of hierarchy evaluation."""

    command: ControlCommand
    winning_level: int  # 1-5
    overridden: bool  # True if a higher priority overrode the plan


def evaluate_hierarchy(
    plan_command: ControlCommand,
    current_soc: float,
    soc_min_hard: float,
    soc_max_hard: float,
    storm_active: bool = False,
    storm_reserve_soc: float = 0.0,
    grid_available: bool = True,
) -> HierarchyResult:
    """Evaluate the 5-level priority hierarchy and return the winning command.

    Args:
        plan_command: Command from the optimiser (level 4-5).
        current_soc: Current battery state of charge (0-1).
        soc_min_hard: Hard minimum SOC.
        soc_max_hard: Hard maximum SOC.
        storm_active: Whether storm reserve is active.
        storm_reserve_soc: Target SOC for storm reserve.
        grid_available: Whether grid connection is active.
    """
    # Level 1: Safety
    safety = _check_safety(plan_command, current_soc, soc_min_hard, soc_max_hard, grid_available)
    if safety is not None:
        return HierarchyResult(command=safety, winning_level=1, overridden=True)

    # Level 2: Storm reserve
    if storm_active:
        storm = _check_storm_reserve(plan_command, current_soc, storm_reserve_soc)
        if storm is not None:
            return HierarchyResult(command=storm, winning_level=2, overridden=True)

    # Level 3: Critical loads (handled by load manager, not mode override)
    # Level 4-5: Cost optimisation / Opportunistic (the plan command)
    return HierarchyResult(command=plan_command, winning_level=4, overridden=False)


def _check_safety(
    command: ControlCommand,
    soc: float,
    soc_min: float,
    soc_max: float,
    grid_available: bool,
) -> ControlCommand | None:
    """Level 1: Safety overrides."""
    # SOC too low — stop discharging, force charge if grid available
    if soc <= soc_min:
        if command.mode in (OperatingMode.FORCE_DISCHARGE, OperatingMode.SELF_USE):
            mode = OperatingMode.FORCE_CHARGE if grid_available else OperatingMode.SELF_USE
            logger.warning("SAFETY: SOC %.1f%% at minimum, overriding to %s", soc * 100, mode.name)
            return ControlCommand(
                mode=mode,
                power_w=command.power_w if mode == OperatingMode.FORCE_CHARGE else 0,
                source="safety",
                reason=f"soc_at_minimum_{soc:.2f}",
                priority=1,
            )

    # SOC too high — stop charging
    if soc >= soc_max:
        if command.mode == OperatingMode.FORCE_CHARGE:
            logger.warning("SAFETY: SOC %.1f%% at maximum, overriding to self-use", soc * 100)
            return ControlCommand(
                mode=OperatingMode.SELF_USE,
                power_w=0,
                source="safety",
                reason=f"soc_at_maximum_{soc:.2f}",
                priority=1,
            )

    # Grid lost — go to self-use (battery supplies load)
    if not grid_available:
        if command.mode in (OperatingMode.FORCE_CHARGE, OperatingMode.FORCE_DISCHARGE):
            logger.warning("SAFETY: Grid unavailable, overriding to self-use")
            return ControlCommand(
                mode=OperatingMode.SELF_USE,
                power_w=0,
                source="safety",
                reason="grid_unavailable",
                priority=1,
            )

    return None


def _check_storm_reserve(
    command: ControlCommand,
    soc: float,
    reserve_soc: float,
) -> ControlCommand | None:
    """Level 2: Storm reserve — prevent discharge below reserve."""
    if soc <= reserve_soc and command.mode in (OperatingMode.FORCE_DISCHARGE,):
        logger.info(
            "STORM RESERVE: SOC %.1f%% at reserve target %.1f%%, blocking discharge",
            soc * 100, reserve_soc * 100,
        )
        return ControlCommand(
            mode=OperatingMode.SELF_USE,
            power_w=0,
            source="storm",
            reason=f"storm_reserve_soc_{soc:.2f}_below_{reserve_soc:.2f}",
            priority=2,
        )

    return None
