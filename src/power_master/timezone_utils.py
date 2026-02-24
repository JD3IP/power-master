"""Timezone resolution helpers with pragmatic fallbacks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Common deployment fallback when IANA tzdata is unavailable (Windows hosts).
_FIXED_FALLBACKS: dict[str, tzinfo] = {
    "Australia/Brisbane": timezone(timedelta(hours=10)),
}


def resolve_timezone(tz_name: str) -> tzinfo:
    """Resolve an IANA timezone name with safe fallbacks.

    Order:
    1. IANA database via ZoneInfo.
    2. Known fixed-offset fallback map.
    3. Host local timezone.
    4. UTC.
    """
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        pass

    if tz_name in _FIXED_FALLBACKS:
        return _FIXED_FALLBACKS[tz_name]

    local_tz = datetime.now().astimezone().tzinfo
    if local_tz is not None:
        return local_tz
    return timezone.utc
