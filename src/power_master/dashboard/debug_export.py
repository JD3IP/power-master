"""Build a debug bundle: redacted config, current plan, last 24h data and logs."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

from power_master.config.schema import AppConfig

# Field names whose values are stripped from the exported config.
_SECRET_KEYS = frozenset({
    "api_key",
    "api_token",
    "bot_token",
    "password",
    "password_hash",
    "session_secret",
    "smtp_password",
    "token",
    "user_key",
})


def redact_config(config_dict: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of the config with any secret-looking keys masked.

    A value is masked to "***REDACTED***" when non-empty, or left as "" so an
    empty-vs-set distinction is still visible.  Webhook headers are replaced
    wholesale since header names/values can both leak auth.
    """
    def _scrub(value: Any, parent_key: str | None = None) -> Any:
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if k in _SECRET_KEYS:
                    out[k] = "***REDACTED***" if v not in ("", None) else v
                elif k == "headers" and parent_key == "webhook":
                    out[k] = {h: "***REDACTED***" for h in v} if v else v
                else:
                    out[k] = _scrub(v, k)
            return out
        if isinstance(value, list):
            return [_scrub(item, parent_key) for item in value]
        return value

    return _scrub(config_dict)


async def build_debug_bundle(
    config: AppConfig,
    repo: Any,
    *,
    hours: int = 24,
    log_limit: int = 10000,
    in_memory_logs: list[dict[str, Any]] | None = None,
    solar_calibration: dict[str, Any] | None = None,
) -> bytes:
    """Return a .zip archive of config, plan, telemetry, prices and logs."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    cutoff_iso = cutoff.isoformat()
    end_iso = now.isoformat()

    redacted = redact_config(config.model_dump(mode="python"))

    plan = await repo.get_active_plan()
    plan_slots = await repo.get_plan_slots(plan["id"]) if plan else []

    telemetry = await repo.get_telemetry_since(cutoff_iso)
    import_prices = await repo.get_historical("import_price_cents", cutoff_iso, end_iso)
    export_prices = await repo.get_historical("export_price_cents", cutoff_iso, end_iso)
    db_logs = await repo.get_logs_since(cutoff_iso, limit=log_limit)

    meta = {
        "generated_at": end_iso,
        "hours": hours,
        "record_counts": {
            "plan_slots": len(plan_slots),
            "telemetry": len(telemetry),
            "import_prices": len(import_prices),
            "export_prices": len(export_prices),
            "db_logs": len(db_logs),
            "in_memory_logs": len(in_memory_logs) if in_memory_logs else 0,
        },
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("meta.json", _json(meta))
        zf.writestr("config.json", _json(redacted))
        zf.writestr("plan.json", _json({"plan": plan, "slots": plan_slots}))
        zf.writestr("telemetry.json", _json(telemetry))
        zf.writestr("prices.json", _json({
            "import_price_cents": import_prices,
            "export_price_cents": export_prices,
        }))
        zf.writestr("logs_db.json", _json(db_logs))
        if in_memory_logs is not None:
            zf.writestr("logs_memory.json", _json(in_memory_logs))
        if solar_calibration is not None:
            zf.writestr("solar_calibration.json", _json(solar_calibration))
    return buf.getvalue()


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str, sort_keys=True)
