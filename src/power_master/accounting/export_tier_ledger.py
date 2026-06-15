"""Export tier ledger for per-day volume-tiered export tracking.

Tracks cumulative exported kWh attributed to the correct tier (e.g., ZEROHERO
first 15 kWh/day at 10c tier, then 2c flat) for correct REVENUE accounting.
Resets per local day; persists to SQLite kv_store so mid-day restarts don't
lose the count.

Mirrors the FreeWindowCapTracker pattern for consistency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# KV store key for persisting export tier ledger state
KV_EXPORT_TIER_LEDGER_KEY = "export_tier_ledger_state"


@dataclass
class ExportTierState:
    """Per-tier export tracking for a single day."""
    local_date_str: str  # YYYY-MM-DD in the configured timezone
    tier_ledger: dict[str, float] = field(default_factory=dict)
    # {tier_name: cumulative_kwh_for_this_tier}
    timestamp: str = ""  # ISO timestamp when last updated

    def to_dict(self) -> dict:
        """Convert to JSON-serialisable dict for kv_store."""
        return {
            "local_date_str": self.local_date_str,
            "tier_ledger": self.tier_ledger,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ExportTierState:
        """Restore from dict loaded from kv_store."""
        return cls(
            local_date_str=d.get("local_date_str", ""),
            tier_ledger=d.get("tier_ledger", {}),
            timestamp=d.get("timestamp", ""),
        )


class ExportTierLedger:
    """Tracks per-tier export volumes and revenue attribution.

    Per the tariff spec (§6), export tiers are per-day with cumulative
    kWh caps per tier (e.g., ZEROHERO 18:00-20:59: first 15 kWh @ 10c,
    remainder @ 2c). This ledger records measured grid export and attributes
    it correctly to the tiers, resets per local day, and persists state.
    """

    def __init__(
        self,
        timezone_name: str,
        tier_structure: dict[str, float],
        # {tier_name: cap_kwh_or_None, ...}
        # E.g. {"tier1": 15.0, "tier2": None}
        repo=None,
    ) -> None:
        """Initialize the export tier ledger.

        Args:
            timezone_name: IANA timezone name (e.g., 'Australia/Brisbane')
            tier_structure: Dict mapping tier name to cap in kWh (None = unlimited).
                           E.g. for ZEROHERO evening: {"premium-15kwh": 15.0, "flat-2c": None}
            repo: Optional Repository instance (aiosqlite); if provided,
                  ledger will load/save state from kv_store
        """
        self._tz = ZoneInfo(timezone_name)
        self._tier_structure = tier_structure  # Read-only reference to configured tiers
        self._repo = repo

        # In-memory state
        self._state: ExportTierState | None = None

    async def init_persistence(self, repo) -> None:
        """Load persisted export tier ledger state from kv_store.

        Call this after the repository is initialized.

        Args:
            repo: Repository instance
        """
        self._repo = repo
        if repo is None:
            logger.warning("ExportTierLedger: no repository provided; persistence disabled")
            return

        try:
            saved = await repo.kv_get(KV_EXPORT_TIER_LEDGER_KEY)
            if saved:
                self._state = ExportTierState.from_dict(saved)
                # Check if we're still in the same local day
                today_local_date = self._get_local_date_str(datetime.now(self._tz))
                if self._state.local_date_str != today_local_date:
                    logger.info(
                        "ExportTierLedger: day boundary crossed (%s -> %s); resetting ledger",
                        self._state.local_date_str,
                        today_local_date,
                    )
                    self._state = None
                else:
                    logger.info(
                        "ExportTierLedger: restored state for %s: %s",
                        self._state.local_date_str,
                        self._state.tier_ledger,
                    )
            else:
                logger.info("ExportTierLedger: no persisted state; starting fresh")
        except Exception as e:
            logger.warning("ExportTierLedger: failed to restore persisted state: %s", e)

    def get_tier_consumption(self, tier_name: str) -> float:
        """Return cumulative kWh exported into a specific tier today.

        Args:
            tier_name: Name of the tier (e.g., 'premium-15kwh')

        Returns:
            Cumulative kWh for this tier (0 if not yet consumed)
        """
        if self._state is None:
            return 0.0
        return self._state.tier_ledger.get(tier_name, 0.0)

    def get_all_tier_consumption(self) -> dict[str, float]:
        """Return all tier consumption for today.

        Returns:
            Dict of {tier_name: cumulative_kwh}
        """
        if self._state is None:
            return {}
        return dict(self._state.tier_ledger)

    async def record_export(self, energy_wh: int, rate_cents: float) -> dict:
        """Record a grid export and attribute it to the correct tier.

        Returns a dict with:
        - 'tier_name': which tier this export was attributed to
        - 'kwh_in_tier': cumulative kWh now in that tier
        - 'revenue_cents': revenue attributed to this export
        (based on rate_cents and energy_wh)

        Args:
            energy_wh: Energy exported in Wh
            rate_cents: Rate in cents/kWh this was exported at

        Returns:
            Dict with attribution details
        """
        if energy_wh <= 0:
            return {"tier_name": None, "kwh_in_tier": 0.0, "revenue_cents": 0}

        now = datetime.now(self._tz)
        today_local_date = self._get_local_date_str(now)

        # Initialize state if fresh day or first call
        if self._state is None or self._state.local_date_str != today_local_date:
            self._state = ExportTierState(
                local_date_str=today_local_date,
                tier_ledger={},
                timestamp=datetime.now(self._tz).isoformat(),
            )
            logger.info("ExportTierLedger: starting fresh for %s", today_local_date)

        energy_kwh = energy_wh / 1000.0
        revenue_cents = energy_wh * rate_cents / 1000.0

        # Find which tier to attribute this export to
        # Tiers are processed in order; each one fills up to its cap before moving to next
        attributed_tier = None
        kwh_remaining = energy_kwh

        for tier_name in self._tier_structure.keys():
            tier_cap = self._tier_structure[tier_name]
            current_in_tier = self._state.tier_ledger.get(tier_name, 0.0)

            if tier_cap is None:
                # Unlimited tier; all remaining goes here
                self._state.tier_ledger[tier_name] = current_in_tier + kwh_remaining
                attributed_tier = tier_name
                kwh_remaining = 0.0
                break
            else:
                # Capped tier; fill up to cap
                room_in_tier = max(0.0, tier_cap - current_in_tier)
                kwh_to_this_tier = min(kwh_remaining, room_in_tier)

                self._state.tier_ledger[tier_name] = current_in_tier + kwh_to_this_tier
                if kwh_to_this_tier > 0:
                    attributed_tier = tier_name
                kwh_remaining -= kwh_to_this_tier

                if kwh_remaining == 0.0:
                    break

        self._state.timestamp = datetime.now(self._tz).isoformat()

        # Persist to kv_store
        if self._repo:
            try:
                await self._repo.kv_set(KV_EXPORT_TIER_LEDGER_KEY, self._state.to_dict())
            except Exception as e:
                logger.warning("ExportTierLedger: failed to persist state: %s", e)

        return {
            "tier_name": attributed_tier,
            "kwh_in_tier": (
                self._state.tier_ledger.get(attributed_tier, 0.0)
                if attributed_tier
                else 0.0
            ),
            "revenue_cents": int(round(revenue_cents)),
        }

    @staticmethod
    def _get_local_date_str(dt: datetime) -> str:
        """Return YYYY-MM-DD string for a datetime in its local timezone."""
        return dt.date().isoformat()

    async def reset_for_testing(self) -> None:
        """Reset state (for testing only)."""
        self._state = None
        if self._repo:
            try:
                await self._repo.kv_set(KV_EXPORT_TIER_LEDGER_KEY, None)
            except Exception:
                pass
