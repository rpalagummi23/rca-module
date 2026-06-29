#!/usr/bin/env python3
"""
Temporal Flowering Synchronization Score (FSS) for PlanetScope field-mean indices.

Scope / assumptions
--------------------
- Input is a FIELD-MEAN time series: ONE row per scene date, with already-computed
  vegetation indices (NDVI, NDRE, EVI, SAVI, CI_RedEdge). No raster / per-pixel work
  is done here - index computation is assumed already complete upstream.
- No GDD / weather / sowing-date inputs are available, so the FSRI "heading deviation"
  term is intentionally omitted. Heading timing is derived from the satellite curve.
- This score measures TEMPORAL flowering synchronization of the whole field:
  a sharp, narrow, smooth flowering peak  -> synchronized;
  a broad, flat, or jagged peak           -> staggered / desynchronized.

Methodology sources (companion docs)
------------------------------------
- field-analysis.html      : Savitzky-Golay smoothing, heading via 1st-derivative.
- rice-flowering-sync.html : FSRI components (Vigor Asymmetry, Stress Flag); "sharp
                             narrow peak = synchronized, broad flat peak = risk".
- Rice Synchronization docx: Method 1 (temporal slope variance / curve roughness).

FSS = w1*PeakBroadness + w2*CurveRoughness + w3*VigorAsymmetry + w4*StressFlag
      (0 = perfectly synchronized, higher = more desynchronized)

Run later against real data:
    python field_synchronization.py --input field_summary.csv --field-id MY_FIELD \
        --output field_synchronization.csv
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import pandas as pd
from scipy.signal import savgol_filter

EPS = 1e-10

# --------------------------------------------------------------------------- #
# Tunable constants (calibrate against ground-truth seed-set data when available)
# --------------------------------------------------------------------------- #

# Composite weights (must sum to 1.0).
W_PEAK_BROADNESS = 0.35
W_CURVE_ROUGHNESS = 0.25
W_VIGOR_ASYMMETRY = 0.20
W_STRESS_FLAG = 0.20

# Phenology / normalization parameters.
SMOOTH_WINDOW_DAYS = 11      # Savitzky-Golay window (days). Forced odd internally.
SMOOTH_POLYORDER = 3
PLATEAU_FRACTION = 0.95      # flowering = days with smoothed NDVI >= 0.95 * peak
IDEAL_FLOWERING_DAYS = 5.0   # concentrated synchronized burst (~5 days, per docs)
BROADNESS_SCALE_DAYS = 20.0  # extra days beyond ideal that map broadness -> 1.0
ROUGHNESS_THRESHOLD = 0.02   # Method 1: slope std-dev > 0.02 = desync signature
ROUGHNESS_SCALE = 0.04       # roughness value that maps to 1.0
STRESS_WINDOW_DAYS = 7       # +/- days around heading to scan for a sudden NDVI drop

# Classification bands on the final FSS (aligned to the docs' 4-tier scheme).
BAND_SYNCHRONIZED = 0.10     # < 0.10  -> Synchronized
BAND_MILD = 0.20             # 0.10-0.20 -> Mild Mismatch
BAND_MODERATE = 0.35         # 0.20-0.35 -> Moderate Mismatch
                             # > 0.35    -> Critical Mismatch

MIN_OBSERVATIONS = 8         # need enough scenes to build a meaningful curve


# --------------------------------------------------------------------------- #
# Column resolution (be tolerant of naming: NDVI vs NDVI_mean, Date vs date ...)
# --------------------------------------------------------------------------- #

_CANDIDATES = {
    "date": ["date", "Date", "DATE", "scene_date", "acquisition_date"],
    "ndvi": ["NDVI", "ndvi", "NDVI_mean", "ndvi_mean"],
    "ndre": ["NDRE", "ndre", "NDRE_mean", "ndre_mean"],
    "ci_re": ["CI_RedEdge", "ci_rededge", "CIRE", "CI_RE", "CI_RedEdge_mean",
              "ci_re", "CIred", "CI_red_edge"],
}


def _resolve_column(df: pd.DataFrame, key: str, required: bool = True) -> Optional[str]:
    for cand in _CANDIDATES[key]:
        if cand in df.columns:
            return cand
    if required:
        raise KeyError(
            f"Could not find a '{key}' column. Tried {_CANDIDATES[key]}. "
            f"Available columns: {list(df.columns)}"
        )
    return None


# --------------------------------------------------------------------------- #
# Core helpers
# --------------------------------------------------------------------------- #

def _odd(n: int) -> int:
    """Return the largest odd integer <= n (and >= 3)."""
    n = int(n)
    if n % 2 == 0:
        n -= 1
    return max(3, n)


def _to_daily_grid(days: np.ndarray, values: np.ndarray):
    """Linearly interpolate irregular observations onto a uniform daily grid.

    PlanetScope typically yields ~80 clear scenes out of ~110 days; resampling to a
    daily grid makes day-based smoothing and derivatives well defined.
    """
    grid = np.arange(int(days.min()), int(days.max()) + 1, dtype=float)
    interp = np.interp(grid, days, values)
    return grid, interp


def _smooth(values: np.ndarray, window_days: int, polyorder: int) -> np.ndarray:
    """Savitzky-Golay smoothing with a window that is valid for the series length."""
    n = len(values)
    win = _odd(min(window_days, n if n % 2 == 1 else n - 1))
    if win <= polyorder:
        win = _odd(polyorder + 2)
    if win > n:
        # series too short to smooth meaningfully; return as-is
        return values.astype(float)
    return savgol_filter(values, window_length=win, polyorder=polyorder)


# --------------------------------------------------------------------------- #
# Result container
# --------------------------------------------------------------------------- #

@dataclass
class SyncResult:
    field_id: str
    n_observations: int
    season_start_date: str
    booting_date: Optional[str]
    heading_date: str
    flowering_duration_days: float
    peak_ndvi: float
    ndvi_at_heading: float
    ndre_at_heading: Optional[float]
    ndre_max: Optional[float]
    # FSS components (each 0..1, higher = worse)
    peak_broadness: float
    curve_roughness: float
    vigor_asymmetry: float
    stress_flag: float
    # composite
    fss: float
    classification: str
    # provenance of weights used
    w_peak_broadness: float
    w_curve_roughness: float
    w_vigor_asymmetry: float
    w_stress_flag: float


# --------------------------------------------------------------------------- #
# Component computations
# --------------------------------------------------------------------------- #

def _classify(fss: float) -> str:
    if fss < BAND_SYNCHRONIZED:
        return "Synchronized"
    if fss < BAND_MILD:
        return "Mild Mismatch"
    if fss < BAND_MODERATE:
        return "Moderate Mismatch"
    return "Critical Mismatch"


def compute_synchronization(
    dates: pd.Series,
    ndvi: np.ndarray,
    ndre: Optional[np.ndarray] = None,
    ci_re: Optional[np.ndarray] = None,
    field_id: str = "FIELD",
    weights: Optional[tuple] = None,
) -> SyncResult:
    """Compute the Temporal Flowering Synchronization Score from field-mean curves.

    Parameters
    ----------
    dates : pd.Series of datetime-like, one per scene (will be sorted ascending).
    ndvi  : field-mean NDVI per scene (same order as dates before sorting).
    ndre  : field-mean NDRE per scene (optional; enables vigor asymmetry).
    ci_re : field-mean CI_RedEdge per scene (optional; enables booting-date marker).
    weights : optional (w1, w2, w3, w4); defaults to module constants. Renormalized
              to sum to 1 over the components that are actually computable.
    """
    # --- order by date and build day axis ---------------------------------- #
    dt = pd.to_datetime(pd.Series(dates).reset_index(drop=True))
    order = np.argsort(dt.values)
    dt = dt.iloc[order].reset_index(drop=True)
    ndvi = np.asarray(ndvi, dtype=float)[order]
    if ndre is not None:
        ndre = np.asarray(ndre, dtype=float)[order]
    if ci_re is not None:
        ci_re = np.asarray(ci_re, dtype=float)[order]

    n_obs = len(dt)
    if n_obs < MIN_OBSERVATIONS:
        raise ValueError(
            f"Only {n_obs} observations; need >= {MIN_OBSERVATIONS} to build a curve."
        )

    start_date = dt.iloc[0]
    days = (dt - start_date).dt.days.to_numpy().astype(float)

    # --- daily grid + smoothing -------------------------------------------- #
    grid, ndvi_grid = _to_daily_grid(days, ndvi)
    ndvi_s = _smooth(ndvi_grid, SMOOTH_WINDOW_DAYS, SMOOTH_POLYORDER)
    # Smoothed NDVI sampled at the actual observation days (for residual analysis).
    ndvi_s_at_obs = np.interp(days, grid, ndvi_s)
    ndvi_residual = ndvi - ndvi_s_at_obs  # raw minus trend; isolates noise/anomalies

    # --- heading date: peak of smoothed NDVI ------------------------------- #
    peak_idx = int(np.argmax(ndvi_s))
    peak_ndvi = float(ndvi_s[peak_idx])
    heading_day = float(grid[peak_idx])
    heading_date = (start_date + pd.Timedelta(days=heading_day))

    # --- booting date: peak of smoothed CI_RedEdge (if available) ---------- #
    booting_date = None
    if ci_re is not None and np.isfinite(ci_re).any():
        _, cire_grid = _to_daily_grid(days, ci_re)
        cire_s = _smooth(cire_grid, SMOOTH_WINDOW_DAYS, SMOOTH_POLYORDER)
        boot_idx = int(np.argmax(cire_s))
        booting_date = (start_date + pd.Timedelta(days=float(grid[boot_idx])))

    # --- flowering duration: plateau width at >= PLATEAU_FRACTION of peak --- #
    plateau_mask = ndvi_s >= (PLATEAU_FRACTION * peak_ndvi)
    flowering_duration = float(plateau_mask.sum())  # one grid step == 1 day

    # === Component 1: Peak Broadness ======================================= #
    # Wider flowering plateau than the ideal synchronized burst => worse.
    peak_broadness = float(
        np.clip((flowering_duration - IDEAL_FLOWERING_DAYS) / BROADNESS_SCALE_DAYS,
                0.0, 1.0)
    )

    # === Component 2: Curve Roughness (Method 1) =========================== #
    # Jaggedness = scatter of RAW about the smoothed trend (residual RMSE) over the
    # booting->heading run-up. Using residuals (not raw slope) means a legitimately
    # steep, clean rise scores ~0; only oscillation/noise raises roughness.
    win_lo = max(grid[0], heading_day - 20)
    run_up = (days >= win_lo) & (days <= heading_day)
    if run_up.sum() >= 3:
        roughness_rmse = float(np.sqrt(np.mean(ndvi_residual[run_up] ** 2)))
    else:
        roughness_rmse = 0.0
    curve_roughness = float(np.clip(roughness_rmse / (ROUGHNESS_SCALE + EPS), 0.0, 1.0))

    # === Component 3: Vigor Asymmetry (FSRI comp 2) ======================== #
    # NDRE at heading vs the season-max NDRE (proxy for "expected healthy" vigor).
    ndre_at_heading = None
    ndre_max = None
    if ndre is not None and np.isfinite(ndre).any():
        _, ndre_grid = _to_daily_grid(days, ndre)
        ndre_s = _smooth(ndre_grid, SMOOTH_WINDOW_DAYS, SMOOTH_POLYORDER)
        ndre_at_heading = float(ndre_s[peak_idx])
        ndre_max = float(np.max(ndre_s))
        vigor_asymmetry = float(
            np.clip(1.0 - (ndre_at_heading / (ndre_max + EPS)), 0.0, 1.0)
        )
    else:
        # No NDRE -> cannot assess vigor; treat as neutral (0) and renormalize weights.
        vigor_asymmetry = 0.0

    # === Component 4: Stress Flag (FSRI comp 3) ============================ #
    # Sudden anomalous NDVI drop near heading: the most negative RAW-vs-trend residual
    # within +/- STRESS_WINDOW_DAYS of heading (cold snap, heavy rain, missed cloud).
    # Using residuals makes this robust to the natural shoulders of a narrow peak.
    ndvi_at_heading = peak_ndvi
    near = (days >= heading_day - STRESS_WINDOW_DAYS) & (days <= heading_day + STRESS_WINDOW_DAYS)
    if near.any():
        worst_dip = float(max(0.0, -np.min(ndvi_residual[near])))  # depth below trend
        stress_flag = float(np.clip(worst_dip / (ndvi_at_heading + EPS), 0.0, 1.0))
    else:
        stress_flag = 0.0

    # === Composite FSS ===================================================== #
    if weights is None:
        w = [W_PEAK_BROADNESS, W_CURVE_ROUGHNESS, W_VIGOR_ASYMMETRY, W_STRESS_FLAG]
    else:
        w = list(weights)
    # If NDRE absent, drop the vigor weight and renormalize the rest.
    if ndre is None or not np.isfinite(np.array(ndre, dtype=float)).any():
        w[2] = 0.0
    wsum = sum(w) + EPS
    w = [x / wsum for x in w]

    fss = (w[0] * peak_broadness
           + w[1] * curve_roughness
           + w[2] * vigor_asymmetry
           + w[3] * stress_flag)
    fss = float(round(fss, 4))

    return SyncResult(
        field_id=field_id,
        n_observations=n_obs,
        season_start_date=start_date.strftime("%Y-%m-%d"),
        booting_date=booting_date.strftime("%Y-%m-%d") if booting_date is not None else None,
        heading_date=heading_date.strftime("%Y-%m-%d"),
        flowering_duration_days=round(flowering_duration, 1),
        peak_ndvi=round(peak_ndvi, 4),
        ndvi_at_heading=round(ndvi_at_heading, 4),
        ndre_at_heading=round(ndre_at_heading, 4) if ndre_at_heading is not None else None,
        ndre_max=round(ndre_max, 4) if ndre_max is not None else None,
        peak_broadness=round(peak_broadness, 4),
        curve_roughness=round(curve_roughness, 4),
        vigor_asymmetry=round(vigor_asymmetry, 4),
        stress_flag=round(stress_flag, 4),
        fss=fss,
        classification=_classify(fss),
        w_peak_broadness=round(w[0], 4),
        w_curve_roughness=round(w[1], 4),
        w_vigor_asymmetry=round(w[2], 4),
        w_stress_flag=round(w[3], 4),
    )


# --------------------------------------------------------------------------- #
# CSV I/O wrapper
# --------------------------------------------------------------------------- #

def run_from_csv(input_csv: str, field_id: str, output_csv: Optional[str]) -> SyncResult:
    df = pd.read_csv(input_csv)
    c_date = _resolve_column(df, "date", required=True)
    c_ndvi = _resolve_column(df, "ndvi", required=True)
    c_ndre = _resolve_column(df, "ndre", required=False)
    c_cire = _resolve_column(df, "ci_re", required=False)

    result = compute_synchronization(
        dates=df[c_date],
        ndvi=df[c_ndvi].to_numpy(),
        ndre=df[c_ndre].to_numpy() if c_ndre else None,
        ci_re=df[c_cire].to_numpy() if c_cire else None,
        field_id=field_id,
    )

    if output_csv:
        pd.DataFrame([asdict(result)]).to_csv(output_csv, index=False)
    return result


def _print_result(r: SyncResult) -> None:
    print("=" * 60)
    print(f"Field Synchronization Report : {r.field_id}")
    print("=" * 60)
    print(f"Observations            : {r.n_observations}")
    print(f"Season start            : {r.season_start_date}")
    print(f"Booting date (CI_RE max) : {r.booting_date}")
    print(f"Heading date (NDVI peak) : {r.heading_date}")
    print(f"Flowering duration (d)  : {r.flowering_duration_days}")
    print(f"Peak NDVI               : {r.peak_ndvi}")
    print(f"NDRE at heading / max   : {r.ndre_at_heading} / {r.ndre_max}")
    print("-" * 60)
    print("Components (0 = synchronized, 1 = worst):")
    print(f"  Peak broadness   : {r.peak_broadness}  (w={r.w_peak_broadness})")
    print(f"  Curve roughness  : {r.curve_roughness}  (w={r.w_curve_roughness})")
    print(f"  Vigor asymmetry  : {r.vigor_asymmetry}  (w={r.w_vigor_asymmetry})")
    print(f"  Stress flag      : {r.stress_flag}  (w={r.w_stress_flag})")
    print("-" * 60)
    print(f"FSS                     : {r.fss}")
    print(f"Classification          : {r.classification}")
    print("=" * 60)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Temporal Flowering Synchronization Score from field-mean indices."
    )
    p.add_argument("--input", required=True,
                   help="CSV with one row per date: date + NDVI[/NDRE/CI_RedEdge].")
    p.add_argument("--field-id", default="FIELD", help="Field identifier label.")
    p.add_argument("--output", default=None,
                   help="Optional output CSV path (one summary row).")
    args = p.parse_args(argv)

    try:
        result = run_from_csv(args.input, args.field_id, args.output)
    except (KeyError, ValueError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    _print_result(result)
    if args.output:
        print(f"\nWrote: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
