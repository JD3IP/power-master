"""Pure-function narrators: build structured Action blocks from plan + context.

Narrators are deliberately free of mutable state.  Each takes a frozen
NarratorContext snapshot and the current OptimisationPlan and returns an
Action.  This keeps them trivially unit-testable and race-free — the caller
is responsible for taking the snapshot at the right moment (after a
rebuild completes, not while detection fires).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from power_master.notifications.bus import Action
from power_master.optimisation.plan import OptimisationPlan, SlotMode


@dataclass(frozen=True)
class NarratorContext:
    """Frozen snapshot of system state at emission time."""
    now: datetime
    current_soc: float | None = None
    storm_active: bool = False
    storm_reserve_soc: float = 0.0
    storm_window_start: datetime | None = None
    storm_window_end: datetime | None = None
    spike_price_cents: float | None = None
    spike_window_end: datetime | None = None
    force_charge_threshold_cents: float = 0.0
    force_charge_price_cents: float | None = None
    inverter_offline_since: datetime | None = None
    deferred_load_names: list[str] = ()
    evening_target_soc: float = 0.0
    evening_target_hour: int = 16


def _fmt_pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.0f}%"


def _fmt_time(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%H:%M")


def _slot_mode_summary(plan: OptimisationPlan, now: datetime) -> dict[str, Any]:
    """Summarise upcoming slots: first force-charge window, peak SOC, etc."""
    info: dict[str, Any] = {
        "next_force_charge": None,
        "next_force_charge_end": None,
        "next_force_discharge": None,
        "peak_soc": 0.0,
        "evening_soc": None,
    }
    if plan is None or not plan.slots:
        return info
    future = [s for s in plan.slots if s.end > now]
    if not future:
        return info
    info["peak_soc"] = max(s.expected_soc for s in future)
    # Find contiguous force_charge block
    in_block = False
    for slot in future:
        if slot.mode == SlotMode.FORCE_CHARGE:
            if not in_block:
                info["next_force_charge"] = slot.start
                in_block = True
            info["next_force_charge_end"] = slot.end
        elif in_block:
            break
    for slot in future:
        if slot.mode == SlotMode.FORCE_DISCHARGE:
            info["next_force_discharge"] = slot.start
            break
    return info


def narrate_storm_plan_active(
    plan: OptimisationPlan | None, ctx: NarratorContext,
) -> Action:
    """Storm forecast activated → describe reserve strategy + charging plan."""
    window = ""
    if ctx.storm_window_start and ctx.storm_window_end:
        window = f"{_fmt_time(ctx.storm_window_start)}–{_fmt_time(ctx.storm_window_end)}"
    reason = f"Storm forecast active ({window})" if window else "Storm forecast active"

    taken: list[str] = []
    taken.append(f"Holding battery reserve at {_fmt_pct(ctx.storm_reserve_soc)}")
    summary = _slot_mode_summary(plan, ctx.now)
    if summary["next_force_charge"]:
        taken.append(
            f"Grid-charging {_fmt_time(summary['next_force_charge'])}"
            f"–{_fmt_time(summary['next_force_charge_end'])} to build the reserve"
        )
    if ctx.current_soc is not None:
        gap = ctx.storm_reserve_soc - ctx.current_soc
        if gap > 0.02:
            taken.append(f"Currently {_fmt_pct(ctx.current_soc)} SOC ({_fmt_pct(gap)} short of target)")
    return Action(
        taken=taken,
        reason=reason,
        expires_at=ctx.storm_window_end,
    )


def narrate_storm_resolved(
    plan: OptimisationPlan | None, ctx: NarratorContext,
) -> Action:
    """Storm window cleared → describe actual outcome."""
    taken = ["Released storm reserve, resuming normal optimisation"]
    if ctx.current_soc is not None:
        taken.insert(0, f"Battery held at {_fmt_pct(ctx.current_soc)} through the window")
    return Action(taken=taken, reason="Storm window cleared")


def narrate_price_spike(
    plan: OptimisationPlan | None, ctx: NarratorContext,
) -> Action:
    """Price spike detected → describe discharge + load shedding."""
    price = ctx.spike_price_cents or 0.0
    reason = f"Buy price spiked to {price:.0f}c/kWh"
    taken = []
    summary = _slot_mode_summary(plan, ctx.now)
    if summary["next_force_discharge"]:
        taken.append(f"Forcing discharge from {_fmt_time(summary['next_force_discharge'])}")
    else:
        taken.append("Discharging battery to serve load and export for arbitrage")
    if ctx.deferred_load_names:
        taken.append(f"Deferred loads: {', '.join(ctx.deferred_load_names)}")
    if ctx.current_soc is not None:
        taken.append(f"Starting SOC {_fmt_pct(ctx.current_soc)}")
    return Action(
        taken=taken,
        reason=reason,
        expires_at=ctx.spike_window_end,
    )


def narrate_price_spike_resolved(
    plan: OptimisationPlan | None, ctx: NarratorContext,
) -> Action:
    """Price returned to normal after a spike."""
    taken = ["Prices normalised, resuming optimiser-driven dispatch"]
    if ctx.deferred_load_names:
        taken.append(f"Restoring deferred loads: {', '.join(ctx.deferred_load_names)}")
    if ctx.current_soc is not None:
        taken.append(f"Ending SOC {_fmt_pct(ctx.current_soc)}")
    return Action(taken=taken, reason="Price spike ended")


def narrate_grid_outage(
    plan: OptimisationPlan | None, ctx: NarratorContext,
) -> Action:
    """Inverter reported grid unavailable — observation, no action."""
    since = _fmt_time(ctx.inverter_offline_since) if ctx.inverter_offline_since else "—"
    soc = _fmt_pct(ctx.current_soc)
    return Action(
        taken=[],
        observation=(
            f"Grid unreachable since {since}. Battery at {soc}. "
            f"No automated remediation possible — manual check needed."
        ),
        reason="Inverter reports grid unavailable",
    )


def narrate_grid_resolved(
    plan: OptimisationPlan | None, ctx: NarratorContext,
) -> Action:
    soc = _fmt_pct(ctx.current_soc)
    return Action(
        taken=["Grid restored, resuming normal operation"],
        observation=f"Battery at {soc}",
        reason="Grid connectivity restored",
    )


def narrate_force_charge_triggered(
    plan: OptimisationPlan | None, ctx: NarratorContext,
) -> Action:
    """Force-grid-charge override fired due to cheap price."""
    price = ctx.force_charge_price_cents
    threshold = ctx.force_charge_threshold_cents
    price_str = f"{price:.1f}c/kWh" if price is not None else "—"
    reason = f"Buy price {price_str} is at or below your {threshold:.1f}c/kWh threshold"
    taken = ["Force-charging battery from grid while price stays below threshold"]
    if ctx.current_soc is not None and ctx.evening_target_soc > 0:
        remaining = max(0.0, ctx.evening_target_soc - ctx.current_soc)
        if remaining > 0.01:
            taken.append(
                f"Need {_fmt_pct(remaining)} more to reach evening target "
                f"({_fmt_pct(ctx.evening_target_soc)} by {ctx.evening_target_hour:02d}:00)"
            )
    return Action(taken=taken, reason=reason)


# Registry keyed by event name
NARRATORS: dict[str, Any] = {
    "storm_plan_active": narrate_storm_plan_active,
    "storm_resolved": narrate_storm_resolved,
    "price_spike": narrate_price_spike,
    "price_spike_end": narrate_price_spike_resolved,
    "grid_outage": narrate_grid_outage,
    "grid_outage_resolved": narrate_grid_resolved,
    "force_charge_triggered": narrate_force_charge_triggered,
}


def narrate(event_name: str, plan: OptimisationPlan | None, ctx: NarratorContext) -> Action | None:
    fn = NARRATORS.get(event_name)
    if fn is None:
        return None
    return fn(plan, ctx)


def render_plain(title: str, action: Action | None, message: str) -> str:
    """Default plain-text renderer for all channels."""
    if action is None:
        return message
    lines = [title, ""]
    if action.reason:
        lines.append(action.reason)
        lines.append("")
    if action.taken:
        lines.append("Response:")
        for item in action.taken:
            lines.append(f"  • {item}")
    if action.observation:
        lines.append("")
        lines.append(action.observation)
    return "\n".join(lines).strip()


def generate_daily_briefing(
    plan: OptimisationPlan | None, ctx: NarratorContext,
) -> Action:
    """Generate the optional daily briefing message.

    Summarises the next 24h: expected SOC trajectory, any force-charge or
    force-discharge windows, active storm/spike flags.  Intentionally short.
    """
    summary = _slot_mode_summary(plan, ctx.now)
    taken: list[str] = []
    if ctx.current_soc is not None:
        taken.append(f"Starting SOC {_fmt_pct(ctx.current_soc)}")
    if summary["peak_soc"]:
        taken.append(f"Expected peak SOC {_fmt_pct(summary['peak_soc'])}")
    if summary["next_force_charge"]:
        taken.append(
            f"Grid-charging {_fmt_time(summary['next_force_charge'])}"
            f"–{_fmt_time(summary['next_force_charge_end'])}"
        )
    if summary["next_force_discharge"]:
        taken.append(f"Planned discharge from {_fmt_time(summary['next_force_discharge'])}")
    reason_parts = []
    if ctx.storm_active:
        reason_parts.append("storm reserve active")
    if ctx.spike_price_cents:
        reason_parts.append(f"active spike {ctx.spike_price_cents:.0f}c")
    reason = ("Today: " + ", ".join(reason_parts)) if reason_parts else "Today's plan"
    return Action(taken=taken, reason=reason)
