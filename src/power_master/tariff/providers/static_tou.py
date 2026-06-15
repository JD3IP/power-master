"""Static TOU tariff provider.

Generates deterministic TariffSchedule from a declarative TOU tariff DSL config.
No network calls; supports arbitrary TOU plans with time windows, volume tiers,
and caps. Handles timezone/DST/midnight-crossing correctly.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from power_master.config.schema import TariffProviderConfig
from power_master.tariff.base import TariffProvider, TariffSchedule, TariffSlot

logger = logging.getLogger(__name__)


class StaticTariffProvider(TariffProvider):
    """Static TOU tariff provider from DSL config.

    Generates slots for a rolling 48-hour horizon at 30-minute granularity,
    with full support for:
    - Time-windowed import/export bands
    - Volume-tiered export rates
    - Free windows with daily caps
    - Midnight-crossing windows
    - Timezone-aware DST handling
    """

    def __init__(self, config: TariffProviderConfig) -> None:
        """Initialize provider with TOU config.

        Args:
            config: TariffProviderConfig with type='tou', timezone, and plan
        """
        if config.type != "tou":
            raise ValueError(f"StaticTariffProvider requires type='tou', got '{config.type}'")
        if not config.timezone:
            raise ValueError("StaticTariffProvider requires timezone to be set")
        if not config.plan:
            raise ValueError("StaticTariffProvider requires plan to be set")

        self._config = config
        self._tz = ZoneInfo(config.timezone)
        self._plan = config.plan

        # Optional cap tracker and event emitter (wired in externally)
        self._cap_tracker = None
        self._event_emitter = None

        logger.info(
            "Initialized StaticTariffProvider: %d versions, timezone=%s, grid_charge_policy=%s",
            len(self._plan.versions),
            config.timezone,
            config.grid_charge_policy,
        )

    async def fetch_prices(self) -> TariffSchedule:
        """Generate slots for a rolling 48-hour horizon.

        Returns:
            TariffSchedule with ~96 slots (30-min granularity).
        """
        now_utc = datetime.now(timezone.utc)
        slots = self._generate_slots(now_utc, now_utc + timedelta(hours=48))

        logger.info(
            "StaticTariffProvider generated %d slots (%s to %s)",
            len(slots),
            slots[0].start.isoformat() if slots else "N/A",
            slots[-1].end.isoformat() if slots else "N/A",
        )

        return TariffSchedule(
            slots=slots,
            fetched_at=now_utc,
            provider="static_tou",
        )

    async def fetch_historical(
        self, start: datetime, end: datetime
    ) -> TariffSchedule:
        """Generate slots deterministically for a historical range.

        Args:
            start: Start of range (UTC or aware)
            end: End of range (UTC or aware)

        Returns:
            TariffSchedule with slots for [start, end).
        """
        # Normalize to UTC
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        else:
            start = start.astimezone(timezone.utc)

        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        else:
            end = end.astimezone(timezone.utc)

        slots = self._generate_slots(start, end)

        logger.info(
            "StaticTariffProvider historical: %d slots (%s to %s)",
            len(slots),
            start.date(),
            end.date(),
        )

        return TariffSchedule(
            slots=slots,
            fetched_at=datetime.now(timezone.utc),
            provider="static_tou",
        )

    async def is_healthy(self) -> bool:
        """Check if the provider is healthy.

        Returns True only if the active version can produce a valid schedule.
        Reflects config validity, not network status.
        """
        try:
            # Try to generate a single slot with today's date
            now_utc = datetime.now(timezone.utc)
            now_local = now_utc.astimezone(self._tz)
            today = now_local.date()

            # Find the version active on today
            active_version = self._get_active_version(today)
            if not active_version:
                logger.warning("is_healthy: no active version for today (%s)", today)
                return False

            # Try to generate one slot to verify the config is valid
            slot = self._generate_slot_for_time(now_utc, today, active_version)
            if slot is None:
                logger.warning("is_healthy: failed to generate slot for now")
                return False

            return True
        except Exception as e:
            logger.warning("is_healthy: exception during check: %s", e)
            return False

    def wire_cap_tracker(self, cap_tracker) -> None:
        """Wire in a free-window cap tracker for cap-aware pricing.

        Args:
            cap_tracker: FreeWindowCapTracker instance
        """
        self._cap_tracker = cap_tracker
        logger.info("StaticTariffProvider: wired cap tracker")

    def wire_event_emitter(self, event_emitter) -> None:
        """Wire in a tariff event emitter for logging.

        Args:
            event_emitter: TariffEventEmitter instance
        """
        self._event_emitter = event_emitter
        logger.info("StaticTariffProvider: wired event emitter")

    def _generate_slots(self, start_utc: datetime, end_utc: datetime) -> list[TariffSlot]:
        """Generate TariffSlot list for a UTC time range.

        Args:
            start_utc: Start of range (must be UTC-aware)
            end_utc: End of range (must be UTC-aware)

        Returns:
            List of TariffSlot at 30-min granularity.
        """
        slots = []
        current = start_utc

        while current < end_utc:
            # Determine the local calendar date for this UTC time
            current_local = current.astimezone(self._tz)
            local_date = current_local.date()

            # Find the version active on this date
            active_version = self._get_active_version(local_date)
            if not active_version:
                logger.warning("No active version for date %s; skipping", local_date)
                current += timedelta(minutes=30)
                continue

            # Generate the slot for this UTC time
            slot = self._generate_slot_for_time(current, local_date, active_version)
            if slot:
                slots.append(slot)

            current += timedelta(minutes=30)

        return slots

    def _generate_slot_for_time(
        self, utc_time: datetime, local_date: date, version
    ) -> TariffSlot | None:
        """Generate a single TariffSlot for a given UTC time.

        Args:
            utc_time: The UTC time (start of the 30-min slot)
            local_date: The local calendar date (for band + cap lookups)
            version: The active TariffVersion

        Returns:
            TariffSlot or None if generation fails.
        """
        try:
            slot_end_utc = utc_time + timedelta(minutes=30)
            local_time = utc_time.astimezone(self._tz)

            # Determine import price
            import_price, descriptor = self._get_import_price(
                local_time, local_date, version
            )

            # Determine export price (phase 1: flat tier 1 rate, or fallback)
            export_price = self._get_export_price(local_time, version)

            return TariffSlot(
                start=utc_time,
                end=slot_end_utc,
                import_price_cents=import_price,
                export_price_cents=export_price,
                channel_type="general",
                descriptor=descriptor,
            )
        except Exception as e:
            logger.warning("Failed to generate slot for %s: %s", utc_time, e)
            return None

    def _get_import_price(
        self, local_time: datetime, local_date: date, version
    ) -> tuple[float, str]:
        """Get import price and descriptor for a local time.

        Checks free windows first, respecting the daily cap via the cap tracker.
        Then checks windowed import bands.
        Falls back to the default (no-windows) band.

        Args:
            local_time: Local timezone-aware datetime
            local_date: Local calendar date
            version: TariffVersion

        Returns:
            (price_cents, descriptor)
        """
        hm = (local_time.hour, local_time.minute)

        # Check free windows first
        for fw in version.free_windows:
            if self._time_in_windows(hm, fw.windows):
                # If cap tracker is wired, check remaining cap
                if self._cap_tracker:
                    remaining_cap = self._cap_tracker.get_remaining_cap()
                    if remaining_cap > 0:
                        # Still have cap; price at free rate
                        return (fw.rate_c_per_kwh, fw.name)
                    else:
                        # Cap exhausted; fall back to over_cap band
                        fallback_band = next(
                            (b for b in version.import_bands
                             if b.descriptor == fw.over_cap_falls_back_to),
                            None,
                        )
                        if fallback_band:
                            return (fallback_band.rate_c_per_kwh, fallback_band.descriptor)
                        else:
                            # Fallback band not found (config error); log and use free rate
                            logger.warning(
                                "Free window %s: fallback band '%s' not found; using free rate",
                                fw.name,
                                fw.over_cap_falls_back_to,
                            )
                            return (fw.rate_c_per_kwh, fw.name)
                else:
                    # No cap tracker wired; always price at free rate
                    return (fw.rate_c_per_kwh, fw.name)

        # Check windowed import bands
        for band in version.import_bands:
            if band.windows and self._time_in_windows(hm, band.windows):
                return (band.rate_c_per_kwh, band.descriptor)

        # Fall back to the default band (no windows)
        default_band = next((b for b in version.import_bands if not b.windows), None)
        if default_band:
            return (default_band.rate_c_per_kwh, default_band.descriptor)

        # Should never reach here if the config is valid, but be safe
        logger.warning("No default import band found; using 0c")
        return (0.0, "unknown")

    def _get_export_price(self, local_time: datetime, version) -> float:
        """Get export price (FiT) for a local time.

        Phase 1: uses the first tier's rate if in-window, else the fallback.
        Volume-tiering (tracking per-day exports) is Phase 2.

        Args:
            local_time: Local timezone-aware datetime
            version: TariffVersion

        Returns:
            Export price in cents/kWh
        """
        hm = (local_time.hour, local_time.minute)

        # Check each feed-in band in order
        for band in version.feed_in_bands:
            if not band.windows or self._time_in_windows(hm, band.windows):
                # In-window: use the first tier (phase 1 assumes tier 1 only)
                if band.tiers:
                    # Phase 1: return the rate of the first tier.
                    # SEAM: When volume-tiering lands, this will track daily exports
                    # and return the appropriate tier rate based on cumulative kWh.
                    return band.tiers[0].rate_c_per_kwh
                elif band.rate_c_per_kwh is not None:
                    return band.rate_c_per_kwh

        # No in-window band matched; use the default fallback (no windows)
        for band in version.feed_in_bands:
            if not band.windows:
                if band.tiers:
                    return band.tiers[0].rate_c_per_kwh
                elif band.rate_c_per_kwh is not None:
                    return band.rate_c_per_kwh

        # No default fallback found; return 0c
        logger.warning("No default feed-in band found; using 0c")
        return 0.0

    def _get_active_version(self, local_date: date):
        """Find the TariffVersion active on a given local date.

        Boundary semantics (INCLUSIVE on both ends):
        - A version is active on local_date if:
          valid_from <= local_date <= valid_until (if valid_until is set)
          OR valid_from <= local_date (if valid_until is None, open-ended)
        - When multiple versions cover a date, prefer the latest valid_from (most recent).

        Args:
            local_date: Local calendar date (YYYY-MM-DD)

        Returns:
            TariffVersion or None if none match.
        """
        active = None
        for version in self._plan.versions:
            # Check if version is active on this date (INCLUSIVE on both bounds)
            if version.valid_from <= local_date:
                if version.valid_until is None or version.valid_until >= local_date:
                    # This version covers the date; prefer the latest valid_from
                    if active is None or version.valid_from > active.valid_from:
                        active = version

        return active

    def _time_in_windows(
        self, local_hm: tuple[int, int], windows: list[str]
    ) -> bool:
        """Check if a (hour, minute) local time falls in any window.

        Supports midnight-crossing windows (e.g., "22:00-07:00").

        Args:
            local_hm: (hour, minute) tuple in local time
            windows: List of "HH:MM-HH:MM" window strings

        Returns:
            True if the time is in any window.
        """
        h, m = local_hm

        for window_str in windows:
            start_str, end_str = window_str.split("-")
            sh, sm = map(int, start_str.split(":"))
            eh, em = map(int, end_str.split(":"))

            start_hm = (sh, sm)
            end_hm = (eh, em)

            # Check if start > end (midnight-crossing)
            if start_hm > end_hm:
                # Midnight-crossing: [start, 24:00) or [00:00, end)
                if (h, m) >= start_hm or (h, m) < end_hm:
                    return True
            else:
                # Normal window: [start, end)
                if start_hm <= (h, m) < end_hm:
                    return True

        return False

    def get_export_tier_structure(self, local_time: datetime, local_date: date):
        """Get the export tier structure for a given local time (Phase 2).

        Returns a tuple (in_tiered_window, tiers) where:
        - in_tiered_window: True if this time is in a feed-in band with tiers.
        - tiers: List of (up_to_kwh_per_day, rate_c_per_kwh) if in-window, else None.

        For flat-FiT plans (no tiers), returns (False, None).

        Args:
            local_time: Local timezone-aware datetime
            local_date: Local calendar date

        Returns:
            (in_tiered_window: bool, tiers: list[tuple] | None)
        """
        # Find the active version for this date
        active_version = self._get_active_version(local_date)
        if not active_version:
            return (False, None)

        hm = (local_time.hour, local_time.minute)

        # Check each feed-in band in order
        for band in active_version.feed_in_bands:
            if band.windows and not self._time_in_windows(hm, band.windows):
                continue
            # This band matches (either no windows or in-window)
            if band.tiers:
                # Tiered band: return the tier structure
                tiers = [
                    (tier.up_to_kwh_per_day, tier.rate_c_per_kwh)
                    for tier in band.tiers
                ]
                return (True, tiers)
            else:
                # Flat-rate band: no tiers
                return (False, None)

        # No band matched; return no tiers (flat FiT, likely 0c default)
        return (False, None)

    def get_credit_windows(self, local_date: date) -> list[dict]:
        """Get all low-import credit windows for a given local date (Phase 2).

        Returns a list of credit window dicts with:
        - name: Credit name (e.g., 'zerohero-evening')
        - windows: List of HH:MM-HH:MM windows (e.g., ['18:00-20:59'])
        - max_import_kwh_per_hour: Max hourly import to earn credit
        - reward_dollars_per_day: Daily reward if earned
        - enforcement: 'soft' (penalty) or 'hard' (constraint+slack)
        - credit_priority_weight: [0, 1] scaling vs export revenue

        Returns:
            List of credit configs (empty if no credits or plan not active)
        """
        active_version = self._get_active_version(local_date)
        if not active_version or not active_version.credits:
            return []

        result = []
        for credit in active_version.credits:
            result.append({
                "name": credit.name,
                "type": credit.type,
                "windows": credit.windows,
                "max_import_kwh_per_hour": credit.max_import_kwh_per_hour,
                "reward_dollars_per_day": credit.reward_dollars_per_day,
                "enforcement": credit.enforcement,
                "credit_priority_weight": credit.credit_priority_weight,
            })

        return result
