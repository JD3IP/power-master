"""Solar forecast calibration.

Fits a small ridge regression mapping raw provider forecast + time-of-day
features to observed telemetry, so the planner sees a calibrated forecast
rather than taking the provider's output verbatim.

Features (4): [1, forecast/peak, sin(2π·h/24), cos(2π·h/24)] where h is
local solar hour.  Targets: actual/peak.  EWMA-weighted samples with a
5-day half-life by default.  Training pairs only the "0–3h ahead" horizon
band from stored forecast snapshots with the telemetry recorded in the
matching slot, to avoid blending nowcast bias with long-horizon weather
miss.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from power_master.timezone_utils import resolve_timezone

logger = logging.getLogger(__name__)

# Keep paired samples within this horizon band (snapshot.fetched_at to slot.start)
NEAR_TERM_HORIZON_HOURS = 3.0

# Ridge regression regularisation (prevents coefficient blow-up on collinear features)
RIDGE_LAMBDA = 1e-3

# Minimum samples before the model will engage; below this, apply_calibration passes through
MIN_TRAINING_SAMPLES = 50

N_FEATURES = 4  # intercept, forecast_norm, sin(h), cos(h)


@dataclass
class TrainingSample:
    slot_start_utc: datetime
    local_solar_hour: float  # 0.0–24.0
    forecast_w: float
    actual_w: float
    age_days: float  # slot_start relative to training time (for EWMA weighting)


@dataclass
class CalibrationModel:
    coefficients: list[float]  # length N_FEATURES
    n_samples: int
    trained_at: datetime
    system_peak_w: float
    raw_mae_w: float  # mean absolute error of raw forecast on training set
    calibrated_mae_w: float  # mean absolute error of calibrated forecast on training set
    tz_name: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "coefficients": self.coefficients,
            "n_samples": self.n_samples,
            "trained_at": self.trained_at.isoformat(),
            "system_peak_w": self.system_peak_w,
            "raw_mae_w": round(self.raw_mae_w, 1),
            "calibrated_mae_w": round(self.calibrated_mae_w, 1),
            "tz_name": self.tz_name,
            "lift_w": round(self.raw_mae_w - self.calibrated_mae_w, 1),
        }


def _features(forecast_w: float, local_solar_hour: float, system_peak_w: float) -> list[float]:
    f_norm = forecast_w / system_peak_w if system_peak_w > 0 else 0.0
    angle = 2.0 * math.pi * local_solar_hour / 24.0
    return [1.0, f_norm, math.sin(angle), math.cos(angle)]


def _predict_norm(features: list[float], coef: list[float]) -> float:
    return sum(f * c for f, c in zip(features, coef))


def _local_solar_hour(t_utc: datetime, tz_name: str) -> float:
    tz = resolve_timezone(tz_name)
    local = t_utc.astimezone(tz)
    return local.hour + local.minute / 60.0


# ── Linear algebra helpers (stdlib ridge solve for a 4×4 system) ─────

def _solve_ridge(
    x_rows: list[list[float]],
    y: list[float],
    weights: list[float],
    ridge_lambda: float,
) -> list[float]:
    """Weighted ridge regression via normal equations.

    Solves (X^T W X + λI) β = X^T W y using Gauss-Jordan elimination.
    k is small (4) so the stdlib implementation is plenty fast.
    """
    k = len(x_rows[0])
    # Build k×k matrix A = X^T W X + λI and k-vector b = X^T W y
    a = [[0.0] * k for _ in range(k)]
    b = [0.0] * k
    for row, target, w in zip(x_rows, y, weights):
        for i in range(k):
            b[i] += w * row[i] * target
            for j in range(k):
                a[i][j] += w * row[i] * row[j]
    for i in range(k):
        a[i][i] += ridge_lambda

    # Augment [A | b] and eliminate
    aug = [a[i] + [b[i]] for i in range(k)]
    for pivot in range(k):
        # Partial pivoting for numerical stability
        max_row = max(range(pivot, k), key=lambda r: abs(aug[r][pivot]))
        if abs(aug[max_row][pivot]) < 1e-12:
            # Singular — should not happen after ridge, but be defensive
            raise ValueError("Calibration: singular matrix after ridge regularisation")
        aug[pivot], aug[max_row] = aug[max_row], aug[pivot]
        pivot_val = aug[pivot][pivot]
        for j in range(pivot, k + 1):
            aug[pivot][j] /= pivot_val
        for r in range(k):
            if r != pivot and aug[r][pivot] != 0.0:
                factor = aug[r][pivot]
                for j in range(pivot, k + 1):
                    aug[r][j] -= factor * aug[pivot][j]
    return [row[k] for row in aug]


# ── Training set construction ────────────────────────────────────────

async def build_training_set(
    repo: Any,
    *,
    window_days: int,
    system_peak_w: float,
    tz_name: str,
    reference_time: datetime | None = None,
) -> list[TrainingSample]:
    """Pair persisted per-horizon solar forecasts with telemetry.

    Queries the forecast_samples table for solar pv_estimate_w rows in the
    near-term horizon band (0.5–3h ahead), joins with telemetry at each
    target_time, and returns weighted training samples.  Forecast values
    below max(100W, 5 % of system peak) are rejected so near-zero
    divisions don't pollute the regression.
    """
    if system_peak_w <= 0:
        return []

    now = reference_time or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    min_forecast_w = max(100.0, 0.05 * system_peak_w)

    rows = await repo.get_forecast_samples(
        "solar",
        metric="pv_estimate_w",
        target_time_start=cutoff.isoformat(),
        target_time_end=now.isoformat(),
        min_horizon=0.5,
        max_horizon=NEAR_TERM_HORIZON_HOURS,
    )
    if not rows:
        return []

    telemetry = await repo.get_telemetry_since(cutoff.isoformat())
    if not telemetry:
        return []

    # Bucket telemetry by 30-minute slot start for fast lookup
    telemetry_by_slot: dict[str, list[float]] = {}
    for row in telemetry:
        ts = _parse_iso(row["recorded_at"])
        if ts is None:
            continue
        slot_key = _slot_key(ts)
        telemetry_by_slot.setdefault(slot_key, []).append(float(row["solar_power_w"]))

    # Deduplicate by target_time — a single slot may have multiple fetches
    # covering it; take the most recent forecast per target.
    forecast_by_slot: dict[str, dict[str, Any]] = {}
    for r in rows:
        target = _parse_iso(r["target_time"])
        if target is None:
            continue
        forecast_w = float(r["predicted_value"])
        if forecast_w < min_forecast_w:
            continue
        key = _slot_key(target)
        existing = forecast_by_slot.get(key)
        if existing is None or r["fetched_at"] > existing["fetched_at"]:
            forecast_by_slot[key] = r

    samples: list[TrainingSample] = []
    for key, r in forecast_by_slot.items():
        target = _parse_iso(r["target_time"])
        if target is None:
            continue
        actuals = telemetry_by_slot.get(key)
        if not actuals:
            continue
        actual_w = sum(actuals) / len(actuals)
        age_days = (now - target).total_seconds() / 86400.0
        samples.append(TrainingSample(
            slot_start_utc=target,
            local_solar_hour=_local_solar_hour(target, tz_name),
            forecast_w=float(r["predicted_value"]),
            actual_w=max(0.0, actual_w),
            age_days=max(0.0, age_days),
        ))
    return samples


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _slot_key(t: datetime) -> str:
    """Round to the 30-minute slot start UTC, as a lookup key."""
    minute = 30 * (t.minute // 30)
    return t.replace(minute=minute, second=0, microsecond=0).isoformat()


# ── Fit ──────────────────────────────────────────────────────────────

def fit_calibration_model(
    samples: list[TrainingSample],
    *,
    system_peak_w: float,
    tz_name: str,
    ewma_half_life_days: float = 5.0,
    ridge_lambda: float = RIDGE_LAMBDA,
    trained_at: datetime | None = None,
) -> CalibrationModel | None:
    """Fit a ridge regression on weighted, normalised samples."""
    if len(samples) < MIN_TRAINING_SAMPLES or system_peak_w <= 0:
        return None

    decay = math.log(2.0) / max(ewma_half_life_days, 0.1)
    x_rows: list[list[float]] = []
    y: list[float] = []
    weights: list[float] = []
    for s in samples:
        x_rows.append(_features(s.forecast_w, s.local_solar_hour, system_peak_w))
        y.append(s.actual_w / system_peak_w)
        weights.append(math.exp(-decay * s.age_days))

    try:
        coef = _solve_ridge(x_rows, y, weights, ridge_lambda)
    except ValueError:
        return None

    # Compute training-set MAE for raw forecast and calibrated forecast (in watts)
    raw_err = 0.0
    cal_err = 0.0
    total_w = 0.0
    for row, target_norm, w, s in zip(x_rows, y, weights, samples):
        raw_err += w * abs(s.forecast_w - s.actual_w)
        cal_w = max(0.0, _predict_norm(row, coef)) * system_peak_w
        cal_w = min(cal_w, 1.2 * system_peak_w)
        cal_err += w * abs(cal_w - s.actual_w)
        total_w += w
    raw_mae = raw_err / total_w if total_w > 0 else 0.0
    cal_mae = cal_err / total_w if total_w > 0 else 0.0

    return CalibrationModel(
        coefficients=coef,
        n_samples=len(samples),
        trained_at=trained_at or datetime.now(timezone.utc),
        system_peak_w=system_peak_w,
        raw_mae_w=raw_mae,
        calibrated_mae_w=cal_mae,
        tz_name=tz_name,
    )


# ── Apply ────────────────────────────────────────────────────────────

def apply_calibration(
    forecast_w: list[float],
    slot_start_times: list[datetime],
    model: CalibrationModel | None,
) -> list[float]:
    """Return calibrated forecast per slot.  Pass-through if model is None."""
    if model is None:
        return list(forecast_w)
    peak = model.system_peak_w
    ceiling = 1.2 * peak
    out: list[float] = []
    for f_w, t in zip(forecast_w, slot_start_times):
        h = _local_solar_hour(t, model.tz_name)
        features = _features(f_w, h, peak)
        pred_norm = _predict_norm(features, model.coefficients)
        pred_w = max(0.0, pred_norm * peak)
        pred_w = min(pred_w, ceiling)
        # Never invent solar at night — if raw forecast is zero, leave it zero
        if f_w <= 0.0:
            pred_w = 0.0
        out.append(pred_w)
    return out
