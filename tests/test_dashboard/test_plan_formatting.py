"""Tests for plan slot display formatting."""

from power_master.dashboard.routes.plans import _format_slot_for_display


def _make_slot(**overrides) -> dict:
    base = {
        "operating_mode": 1,
        "target_power_w": 0,
        "slot_start": "2026-02-24T00:00:00Z",
        "slot_end": "2026-02-24T00:30:00Z",
        "expected_soc": 0.72,
        "import_rate_cents": 25.678,
        "export_rate_cents": 8.123,
        "solar_forecast_w": 3000,
        "load_forecast_w": 1500,
        "scheduled_loads_json": None,
        "slot_index": 0,
    }
    base.update(overrides)
    return base


def test_mode_name_self_use() -> None:
    result = _format_slot_for_display(_make_slot(operating_mode=1))
    assert result["mode_name"] == "Self-Use"


def test_mode_name_zero_export() -> None:
    result = _format_slot_for_display(_make_slot(operating_mode=2))
    assert result["mode_name"] == "Zero Export"


def test_mode_name_charge() -> None:
    result = _format_slot_for_display(_make_slot(operating_mode=3))
    assert result["mode_name"] == "Charge"


def test_mode_name_discharge() -> None:
    result = _format_slot_for_display(_make_slot(operating_mode=4))
    assert result["mode_name"] == "Discharge"


def test_discharge_signed_power_negative() -> None:
    result = _format_slot_for_display(_make_slot(operating_mode=4, target_power_w=3000))
    assert result["signed_power_w"] == -3000


def test_charge_signed_power_positive() -> None:
    result = _format_slot_for_display(_make_slot(operating_mode=3, target_power_w=5000))
    assert result["signed_power_w"] == 5000


def test_timestamp_aest_conversion() -> None:
    # UTC midnight = 10:00 AEST
    result = _format_slot_for_display(_make_slot(slot_start="2026-02-24T00:00:00Z"))
    assert result["slot_start_local"] == "10:00"


def test_timestamp_aest_afternoon() -> None:
    # UTC 04:00 = 14:00 AEST
    result = _format_slot_for_display(_make_slot(slot_start="2026-02-24T04:00:00Z"))
    assert result["slot_start_local"] == "14:00"


def test_price_rounding() -> None:
    result = _format_slot_for_display(_make_slot(import_rate_cents=25.678, export_rate_cents=8.123))
    assert result["import_rate_cents_fmt"] == "25.7"
    assert result["export_rate_cents_fmt"] == "8.1"


def test_scheduled_loads_parsed() -> None:
    result = _format_slot_for_display(_make_slot(scheduled_loads_json='["Pool Pump", "Hot Water"]'))
    assert result["scheduled_loads"] == ["Pool Pump", "Hot Water"]


def test_scheduled_loads_empty() -> None:
    result = _format_slot_for_display(_make_slot(scheduled_loads_json=None))
    assert result["scheduled_loads"] == []
