"""Optimisation plan model and versioning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum


class SlotMode(IntEnum):
    """Operating mode for each plan slot (mirrors OperatingMode)."""

    SELF_USE = 1
    SELF_USE_ZERO_EXPORT = 2
    FORCE_CHARGE = 3
    FORCE_DISCHARGE = 4


@dataclass
class PlanSlot:
    """A single 30-minute slot in the optimisation plan."""

    index: int
    start: datetime
    end: datetime
    mode: SlotMode
    target_power_w: int = 0  # Absolute power for charge/discharge
    expected_soc: float = 0.0  # Expected SOC at end of slot
    import_rate_cents: float = 0.0
    export_rate_cents: float = 0.0
    solar_forecast_w: float = 0.0
    load_forecast_w: float = 0.0
    scheduled_loads: list[str] | None = None
    constraint_flags: list[str] | None = None


@dataclass
class OptimisationPlan:
    """Complete optimisation plan covering the planning horizon."""

    version: int
    created_at: datetime
    trigger_reason: str  # periodic, tariff_change, forecast_delta, storm, soc_deviation, price_spike
    horizon_start: datetime
    horizon_end: datetime
    slots: list[PlanSlot]
    objective_score: float
    solver_time_ms: int
    active_constraints: list[str] = field(default_factory=list)
    reserve_state: dict | None = None
    metrics: dict = field(default_factory=dict)

    @property
    def total_slots(self) -> int:
        return len(self.slots)

    def get_current_slot(self) -> PlanSlot | None:
        """Get the slot covering the current time."""
        now = datetime.now(timezone.utc)
        for slot in self.slots:
            if slot.start <= now < slot.end:
                return slot
        return None

    def get_slot_at(self, dt: datetime) -> PlanSlot | None:
        for slot in self.slots:
            if slot.start <= dt < slot.end:
                return slot
        return None

    def to_db_dict(self) -> dict:
        """Serialise for repository storage."""
        return {
            "version": self.version,
            "trigger_reason": self.trigger_reason,
            "horizon_start": self.horizon_start.isoformat(),
            "horizon_end": self.horizon_end.isoformat(),
            "objective_score": self.objective_score,
            "solver_time_ms": self.solver_time_ms,
            "metrics": self.metrics,
            "active_constraints": self.active_constraints,
            "reserve_state": self.reserve_state,
        }

    def slots_to_db_dicts(self) -> list[dict]:
        return [
            {
                "slot_index": s.index,
                "slot_start": s.start.isoformat(),
                "slot_end": s.end.isoformat(),
                "operating_mode": int(s.mode),
                "target_power_w": s.target_power_w,
                "expected_soc": s.expected_soc,
                "import_rate_cents": s.import_rate_cents,
                "export_rate_cents": s.export_rate_cents,
                "solar_forecast_w": s.solar_forecast_w,
                "load_forecast_w": s.load_forecast_w,
                "scheduled_loads": s.scheduled_loads,
                "constraint_flags": s.constraint_flags,
            }
            for s in self.slots
        ]
