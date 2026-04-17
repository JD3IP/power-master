"""High-level emission helpers that run narrators + attach action block.

Call sites in the rest of the app should prefer these over constructing
Event directly so the narrator output, tier, and correlation tracking are
applied consistently.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from power_master.notifications.bus import Action, Event, EventBus, Tier
from power_master.notifications.narrators import NarratorContext, narrate
from power_master.optimisation.plan import OptimisationPlan

logger = logging.getLogger(__name__)


async def emit_narrated(
    bus: EventBus,
    *,
    event_name: str,
    title: str,
    severity: str,
    tier: Tier,
    plan: OptimisationPlan | None,
    ctx: NarratorContext,
    incident_id: str | None = None,
    correlation_id: str | None = None,
    fallback_message: str = "",
    data: dict[str, Any] | None = None,
) -> Event:
    """Build and publish an Event with a narrated Action attached."""
    action: Action | None = narrate(event_name, plan, ctx)
    event = Event(
        name=event_name,
        severity=severity,
        title=title,
        message=fallback_message or title,
        tier=tier,
        action=action,
        incident_id=incident_id,
        correlation_id=correlation_id,
        data=data or {},
    )
    await bus.publish(event)
    return event


def new_correlation_id() -> str:
    """Generate a fresh correlation id for a new incident."""
    return uuid.uuid4().hex[:12]


def storm_incident_id(window_start: datetime | None) -> str:
    """Stable incident id for a storm window — changes if the window slides."""
    if window_start is None:
        return "storm"
    return f"storm:{window_start.date().isoformat()}"


def spike_incident_id(started_at: datetime | None) -> str:
    if started_at is None:
        return "spike"
    return f"spike:{started_at.isoformat()}"


def grid_outage_incident_id(since: datetime | None) -> str:
    if since is None:
        return "grid_outage"
    return f"grid_outage:{since.isoformat()}"


def force_charge_incident_id(started_at: datetime) -> str:
    # Round to hour so consecutive cheap-price slots in the same hour share an incident
    rounded = started_at.replace(minute=0, second=0, microsecond=0)
    return f"force_charge:{rounded.isoformat()}"
