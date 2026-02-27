"""Second-pass load scheduler — assigns deferrable loads to optimal slots.

Runs after the main MILP solve to schedule loads into slots where
solar is available or prices are low.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime

from power_master.optimisation.plan import OptimisationPlan, SlotMode
from power_master.timezone_utils import resolve_timezone

logger = logging.getLogger(__name__)


@dataclass
class ScheduledLoad:
    """A load scheduled into specific plan slots."""

    load_id: str
    name: str
    power_w: int
    priority_class: int
    assigned_slots: list[int]  # Slot indices
    prefer_solar: bool = True


def schedule_loads(
    plan: OptimisationPlan,
    available_loads: list[dict],
    spike_active: bool = False,
    actual_runtime_minutes: dict[str, float] | None = None,
) -> list[ScheduledLoad]:
    """Assign deferrable loads to optimal plan slots.

    Strategy:
    - During spike: only essential loads (priority <= 2)
    - Prefer slots with excess solar (solar > load)
    - Then prefer cheapest import slots
    - Respect time windows (earliest_start, latest_end)
    - Respect runtime requirements (duration/min/max)
    - Schedule one run per eligible day in the plan horizon
    """
    scheduled = []

    # Sort loads by priority (lower = more important)
    sorted_loads = sorted(available_loads, key=lambda l: l.get("priority_class", 5))

    for load_config in sorted_loads:
        priority = load_config.get("priority_class", 5)

        # During spike, defer non-essential loads
        if spike_active and priority > 2:
            logger.info("Deferring load '%s' (priority %d) due to spike", load_config["name"], priority)
            continue

        if not load_config.get("enabled", True):
            continue

        slot_minutes = _slot_duration_minutes(plan)
        runtime_minutes = _effective_runtime_minutes(load_config)

        # Credit actual runtime already achieved today
        if actual_runtime_minutes:
            load_id = load_config.get("id", "")
            actual = actual_runtime_minutes.get(load_id, 0.0)
            if actual > 0:
                runtime_minutes = max(0, runtime_minutes - int(actual))
                if runtime_minutes <= 0:
                    logger.info(
                        "Load '%s' already satisfied minimum (%.0f min actual)",
                        load_config["name"], actual,
                    )
                    continue

        duration_slots = max(1, math.ceil(runtime_minutes / slot_minutes))
        power_w = load_config.get("power_w", 0)
        prefer_solar = load_config.get("prefer_solar", True)

        # Find eligible slots within the time window
        eligible = _find_eligible_slots(plan, load_config)

        if not eligible:
            continue

        # Score all eligible slots once
        score_by_index: dict[int, float] = {}
        for idx in eligible:
            score_by_index[idx] = _score_slot(plan.slots[idx], power_w, prefer_solar)

        # Schedule once per eligible local day
        assigned: list[int] = []
        for day_indices in _group_indices_by_local_day(plan, eligible, load_config):
            scored = [(idx, score_by_index[idx]) for idx in day_indices]
            scored.sort(key=lambda x: x[1])  # Lower score = better
            day_assigned = _assign_consecutive(scored, duration_slots)
            assigned.extend(day_assigned)

        if assigned:
            scheduled.append(ScheduledLoad(
                load_id=load_config.get("id", load_config["name"]),
                name=load_config["name"],
                power_w=power_w,
                priority_class=priority,
                assigned_slots=assigned,
                prefer_solar=prefer_solar,
            ))
            # Update plan slots with scheduled load info
            for idx in assigned:
                if plan.slots[idx].scheduled_loads is None:
                    plan.slots[idx].scheduled_loads = []
                plan.slots[idx].scheduled_loads.append(load_config["name"])

    logger.info("Scheduled %d loads across plan", len(scheduled))
    return scheduled


def _find_eligible_slots(plan: OptimisationPlan, load_config: dict) -> list[int]:
    """Find slot indices that fall within the load's time window."""
    earliest = load_config.get("earliest_start", "00:00")
    latest = load_config.get("latest_end", "23:59")

    try:
        earliest_h, earliest_m = map(int, earliest.split(":"))
        latest_h, latest_m = map(int, latest.split(":"))
    except (ValueError, AttributeError):
        return list(range(len(plan.slots)))

    day_filter = set(int(d) for d in load_config.get("days_of_week", [0, 1, 2, 3, 4, 5, 6]))
    tz = resolve_timezone(str(load_config.get("timezone", "UTC")))

    eligible = []
    for i, slot in enumerate(plan.slots):
        local_start = slot.start if slot.start.tzinfo is not None else slot.start.replace(tzinfo=resolve_timezone("UTC"))
        local_start = local_start.astimezone(tz)
        if int(local_start.weekday()) not in day_filter:
            continue

        h = local_start.hour
        m = local_start.minute
        slot_time = h * 60 + m
        start_time = earliest_h * 60 + earliest_m
        end_time = latest_h * 60 + latest_m

        if start_time <= end_time:
            # Normal daytime window, end exclusive.
            in_window = start_time <= slot_time < end_time
        else:
            # Overnight window (e.g. 22:00-06:00).
            in_window = slot_time >= start_time or slot_time < end_time

        if in_window:
            eligible.append(i)

    return eligible


def _slot_duration_minutes(plan: OptimisationPlan) -> int:
    if plan.slots:
        delta = plan.slots[0].end - plan.slots[0].start
        mins = int(delta.total_seconds() // 60)
        if mins > 0:
            return mins
    return 30


def _effective_runtime_minutes(load_config: dict) -> int:
    min_runtime = int(load_config.get("min_runtime_minutes", 0) or 0)
    ideal_runtime = int(load_config.get("ideal_runtime_minutes", 0) or 0)
    max_runtime = int(load_config.get("max_runtime_minutes", 0) or 0)
    # duration_minutes is intentionally ignored; runtime is now derived
    # entirely from min/ideal/max runtime settings.
    runtime = 60
    if ideal_runtime > 0:
        runtime = ideal_runtime
    elif min_runtime > 0:
        runtime = min_runtime
    elif max_runtime > 0:
        runtime = max_runtime

    runtime = max(runtime, min_runtime) if min_runtime > 0 else runtime
    runtime = min(runtime, max_runtime) if max_runtime > 0 else runtime
    return max(1, int(runtime))


def _group_indices_by_local_day(plan: OptimisationPlan, indices: list[int], load_config: dict) -> list[list[int]]:
    tz = resolve_timezone(str(load_config.get("timezone", "UTC")))
    by_day: dict[date, list[int]] = {}
    for idx in indices:
        local_start = plan.slots[idx].start
        if local_start.tzinfo is None:
            local_start = local_start.replace(tzinfo=resolve_timezone("UTC"))
        local_start = local_start.astimezone(tz)
        key = local_start.date()
        by_day.setdefault(key, []).append(idx)
    return [sorted(v) for _, v in sorted(by_day.items(), key=lambda kv: kv[0])]


def _score_slot(slot, power_w: int, prefer_solar: bool) -> float:
    """Score a slot for load scheduling. Lower is better."""
    score = slot.import_rate_cents  # Base: prefer cheap import slots

    if prefer_solar:
        excess_solar = slot.solar_forecast_w - slot.load_forecast_w
        if excess_solar > power_w:
            score -= 50  # Large bonus for running during excess solar

    # Penalty for spike slots
    if slot.constraint_flags and "spike" in slot.constraint_flags:
        score += 500

    return score


def _assign_consecutive(scored: list[tuple[int, float]], duration_slots: int) -> list[int]:
    """Find the best run of consecutive slots."""
    if len(scored) < duration_slots:
        return []

    indices = sorted([idx for idx, _ in scored])

    best_run = []
    best_score = float("inf")

    for start in range(len(indices) - duration_slots + 1):
        run = indices[start : start + duration_slots]
        # Check consecutiveness
        if all(run[i + 1] - run[i] == 1 for i in range(len(run) - 1)):
            # Score is sum of individual scores
            run_score = sum(s for idx, s in scored if idx in run)
            if run_score < best_score:
                best_score = run_score
                best_run = run

    return best_run
