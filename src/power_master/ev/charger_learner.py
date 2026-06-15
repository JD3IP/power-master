"""Learn-the-charger seed (Phase 3.6, O2).

Typed scaffold for observing EV charger real draw from telemetry over 7-14 days
and provisioning off learned behaviour (robust to drifting dumb timer).

This module defines the intended interface and data types but does NOT implement
the actual observation/inference algorithm. Phase 4+ will fill in the learning logic.

Design rationale:
- The dumb timer may drift or be misconfigured; observing real behaviour is more robust.
- Telemetry is already recorded (db/repository.py: telemetry table stores battery_power_w,
  grid_power_w, load_power_w, soc). The learner consumes this window of 7-14 days of data.
- Once learned, the charger profile (charge_window + expected_kwh + confidence + sample_days)
  can override/augment the config-provided values in _build_ev_forecast (seam marked).
- Default OFF (learn_from_telemetry: False in EVConfig); no behaviour change this milestone.

Status: FUTURE (Phase 4+). Inert this milestone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class LearnedChargeProfile:
    """Result of EV charger learning from telemetry.

    Fields:
    - charge_window_start, charge_window_end: Observed window boundaries (HH:MM format, local time).
    - expected_kwh: Observed mean nightly energy draw (kWh).
    - confidence: Confidence score [0.0, 1.0]. 0 = unreliable, 1.0 = high confidence.
      Typically (n_days - 2) / (ideal_days - 2) where n_days is observed days and
      ideal_days is the target (7-14).
    - sample_days: Number of observation days (7-14).
    - observed_at: When the profile was learned (ISO datetime).
    """
    charge_window_start: str
    charge_window_end: str
    expected_kwh: float
    confidence: float
    sample_days: int
    observed_at: datetime


class ChargerLearner:
    """Stub interface for learning charger behaviour from telemetry.

    Phase 4+ will implement:
    1. Observe (7-14 days of telemetry) the charger's real draw during its active window.
    2. Detect charge_window boundaries (when does the charger actually run?).
    3. Estimate expected_kwh (mean nightly energy).
    4. Assign confidence (higher with more stable/consistent data).
    5. Return LearnedChargeProfile for use in _build_ev_forecast seam.

    This milestone: stub only. All methods raise NotImplementedError or return None.
    """

    def __init__(self) -> None:
        """Initialize the learner. No-op this milestone."""
        pass

    def observe(
        self,
        telemetry_window_days: list[dict],
    ) -> LearnedChargeProfile | None:
        """Observe telemetry over a time window and learn charger profile.

        Args:
            telemetry_window_days: List of telemetry records (dicts) from the past 7-14 days.
                Each record should have keys: recorded_at (ISO datetime), battery_power_w,
                grid_power_w, load_power_w, soc.

        Returns:
            LearnedChargeProfile if observation succeeds and confidence >= threshold.
            None if insufficient data or observation fails.

        Raises:
            NotImplementedError: Phase 4+ algorithm not implemented this milestone.

        Algorithm outline (Phase 4+):
        1. Filter telemetry to the configured charge_window (local time).
        2. For each day, sum grid_power_w during the window to infer EV draw
           (since grid_power_w = load + battery discharge + EV, and we know load + battery).
        3. Detect if charge_window timing is consistent or drifting.
        4. Estimate expected_kwh = mean of daily totals.
        5. Confidence = f(stability, sample_days, spread).
        6. Return LearnedChargeProfile.
        """
        raise NotImplementedError(
            "ChargerLearner.observe() not implemented. "
            "Phase 4+ will observe charger draw from 7-14 days of telemetry and return LearnedChargeProfile. "
            "This milestone: seed only, no learning algorithm."
        )

    @staticmethod
    def infer_charger_draw_from_telemetry(
        telemetry_record: dict,
        load_power_w: int,
        battery_power_w: int,
    ) -> float:
        """Estimate charger draw from a single telemetry snapshot.

        Placeholder for Phase 4+ inference. Currently a stub.

        Args:
            telemetry_record: Single telemetry dict (recorded_at, battery_power_w, grid_power_w, etc.).
            load_power_w: Actual house load (W).
            battery_power_w: Battery power (W, positive = charging).

        Returns:
            Estimated charger draw (W). 0 if not active or undetectable.

        Formula (Phase 4+):
            charger_w = grid_power_w - load_power_w - battery_import_equivalent
            (i.e., "other" power not accounted for by house load + battery).
            Requires careful de-mixing since grid_power_w = load + battery_charge - solar.

        Note: This is a stub. Real inference is more complex (requires load/battery
        deconvolution, noise filtering, anomaly detection).
        """
        # Stub: return 0 (no inference this milestone)
        return 0.0
