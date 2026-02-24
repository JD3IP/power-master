"""MQTT topic constants."""

from __future__ import annotations


def build_topics(prefix: str = "power_master") -> dict[str, str]:
    """Build all MQTT topic strings from a configurable prefix."""
    return {
        "status": f"{prefix}/status",
        "battery_soc": f"{prefix}/battery/soc",
        "battery_power": f"{prefix}/battery/power",
        "battery_wacb": f"{prefix}/battery/wacb",
        "solar_power": f"{prefix}/solar/power",
        "grid_power": f"{prefix}/grid/power",
        "load_total": f"{prefix}/load/total",
        "tariff_import": f"{prefix}/tariff/import",
        "tariff_export": f"{prefix}/tariff/export",
        "mode_current": f"{prefix}/mode/current",
        "storm_active": f"{prefix}/storm/active",
        "accounting_today_net": f"{prefix}/accounting/today_net",
        "spike_active": f"{prefix}/spike/active",
    }


def load_command_topic(prefix: str, load_id: str) -> str:
    """Build command topic for a controllable load."""
    return f"{prefix}/load/{load_id}/command"


def load_state_topic(prefix: str, load_id: str) -> str:
    """Build state topic for a controllable load."""
    return f"{prefix}/load/{load_id}/state"
