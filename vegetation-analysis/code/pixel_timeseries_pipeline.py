"""
Pixel-Level Time-Series Pipeline for PlanetScope Vegetation Analysis

Extracts per-pixel, per-date band values and vegetation indices from
PlanetScope 8-band GeoTIFFs. Supports single-scene and multi-scene
(season) workflows.

Pipeline:
  1. Load GeoTIFF scene(s) + UDM2 mask(s)
  2. Extract every valid pixel: row, col, x, y, date, B1-B8
  3. Compute per-pixel indices: NDVI, NDRE, EVI, SAVI, CI_RedEdge
  4. Per-pixel temporal analysis: smoothing, peak detection, growth stage
  5. Field-level aggregation (mean/std/percentiles per date)
  6. Export CSVs

Usage:
  # Single scene
  python pixel_timeseries_pipeline.py --scene composite.tif --udm2 composite_udm2.tif

  # Multi-scene directory
  python pixel_timeseries_pipeline.py --scenes-dir ./downloaded_scenes \
      --date-start 2024-03-01 --date-end 2024-08-15

  # With AOI clipping
  python pixel_timeseries_pipeline.py --scenes-dir ./scenes --aoi field.geojson
"""

import argparse
import csv
import glob
import os
import re
import sys
from datetime import datetime

import numpy as np
import rasterio
from rasterio.mask import mask as rio_mask

try:
    from scipy.signal import savgol_filter
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

EPS = 1e-10
SCALE_FACTOR = 10000.0


# ---------------------------------------------------------------------------
# 1. Scene discovery
# ---------------------------------------------------------------------------

def discover_scene_pairs(scenes_dir, date_start, date_end):
    """Find matching SR + UDM2 file pairs within a date range."""
    sr_files = sorted(
        glob.glob(os.path.join(scenes_dir, "*SR*8b*.tif"))
        + glob.glob(os.path.join(scenes_dir, "*AnalyticMS_SR*.tif"))
    )
    pairs = []
    date_pattern = re.compile(r"(\d{8})_")
    start_dt = datetime.strptime(date_start, "%Y-%m-%d")
    end_dt = datetime.strptime(date_end, "%Y-%m-%d")

    for sr_path in sr_files:
        match = date_pattern.search(os.path.basename(sr_path))
        if not match:
            continue
        scene_date = datetime.strptime(match.group(1), "%Y%m%d")
        if not (start_dt <= scene_date <= end_dt):
            continue
        scene_id = "_".join(os.path.basename(sr_path).split("_")[:4])
        udm2_candidates = glob.glob(
            os.path.join(scenes_dir, f"{scene_id}*udm2*.tif")
        )
        udm2_path = udm2_candidates[0] if udm2_candidates else None
        pairs.append(
            {
                "date": scene_date,
                "date_str": scene_date.strftime("%Y-%m-%d"),
                "sr_path": sr_path,
                "udm2_path": udm2_path,
            }
        )
    pairs.sort(key=lambda x: x["date"])
    return pairs


# ---------------------------------------------------------------------------
# 2. AOI helpers
# ---------------------------------------------------------------------------

def load_aoi(geojson_path):
    """Load AOI geometries from a GeoJSON file."""
    if geojson_path is None or not os.path.exists(geojson_path):
        return None
    import json
    from shapely.geometry import shape

    with open(geojson_path) as f:
        gj = json.load(f)
    if gj["type"] == "FeatureCollection":
        return [shape(feat["geometry"]) for feat in gj["features"]]
    elif gj["type"] == "Feature":
        return [shape(gj["geometry"])]
    return [shape(gj)]


# ---------------------------------------------------------------------------
# 3. Per-pixel extraction from a single GeoTIFF
# ---------------------------------------------------------------------------

def compute_indices_vectorized(b1, b2, b3, b4, b5, b6, b7, b8):
    """Compute all vegetation indices from raw reflectance arrays.

    Parameters are 1-D arrays (one value per valid pixel), already scaled
    to 0-1 reflectance.

    Returns dict of 1-D arrays keyed by index name.
    """
    nir = b8
    red = b6
    red_edge = b7
    blue = b2

    ndvi = (nir - red) / (nir + red + EPS)
    ndre = (nir - red_edge) / (nir + red_edge + EPS)
    evi = 2.5 * (nir - red) / (nir + 6.0 * red - 7.5 * blue + 1.0)
    savi = ((nir - red) / (nir + red + 0.5)) * 1.5
    ci_rededge = (nir / (red_edge + EPS)) - 1.0

    return {
        "ndvi": ndvi,
        "ndre": ndre,
        "evi": evi,
        "savi": savi,
        "ci_rededge": ci_rededge,
    }


def extract_pixels_from_scene(sr_path, udm2_path=None, aoi_geoms=None,
                               date_str="unknown", scale_factor=SCALE_FACTOR):
    """Extract all valid pixels from one scene as a list of dicts.

    Returns:
        rows: list of dicts, one per valid pixel
        height, width: raster dimensions (for consistent pixel indexing)
    """
    with rasterio.open(sr_path) as src:
        if aoi_geoms is not None:
            data, transform = rio_mask(src, aoi_geoms, crop=True, nodata=0)
        else:
            data = src.read()
            transform = src.transform

        bands_count, height, width = data.shape

    # UDM2 clean mask
    if udm2_path and os.path.exists(udm2_path):
        with rasterio.open(udm2_path) as udm:
            if aoi_geoms is not None:
                udm_data, _ = rio_mask(udm, aoi_geoms, crop=True, nodata=0)
            else:
                udm_data = udm.read()
        clear = udm_data[0]
        shadow = udm_data[2]
        haze = udm_data[4]
        cloud = udm_data[5]
        clean_mask = (clear == 1) & (cloud == 0) & (shadow == 0) & (haze == 0)
    else:
        clean_mask = np.ones((height, width), dtype=bool)

    # Valid = not all-zero bands AND clean
    all_zero = np.all(data == 0, axis=0)
    valid_mask = ~all_zero & clean_mask

    pixel_rows, pixel_cols = np.where(valid_mask)
    n_valid = len(pixel_rows)

    if n_valid == 0:
        return [], height, width

    # Geographic coordinates of each valid pixel center
    xs, ys = rasterio.transform.xy(transform, pixel_rows, pixel_cols)
    xs = np.array(xs)
    ys = np.array(ys)

    # Extract band values and scale to reflectance
    band_vals = []
    for b in range(min(8, bands_count)):
        band_vals.append(data[b, pixel_rows, pixel_cols].astype(np.float64) / scale_factor)

    # Pad if fewer than 8 bands
    while len(band_vals) < 8:
        band_vals.append(np.full(n_valid, np.nan))

    # Compute indices
    indices = compute_indices_vectorized(*band_vals)

    # Build output rows (as columnar arrays for speed, convert later)
    result = {
        "pixel_row": pixel_rows,
        "pixel_col": pixel_cols,
        "x": xs,
        "y": ys,
        "date": np.full(n_valid, date_str, dtype=object),
        "B1_CoastalBlue": band_vals[0],
        "B2_Blue": band_vals[1],
        "B3_GreenI": band_vals[2],
        "B4_Green": band_vals[3],
        "B5_Yellow": band_vals[4],
        "B6_Red": band_vals[5],
        "B7_RedEdge": band_vals[6],
        "B8_NIR": band_vals[7],
        "NDVI": indices["ndvi"],
        "NDRE": indices["ndre"],
        "EVI": indices["evi"],
        "SAVI": indices["savi"],
        "CI_RedEdge": indices["ci_rededge"],
    }

    return result, height, width


# ---------------------------------------------------------------------------
# 4. Per-pixel temporal analysis
# ---------------------------------------------------------------------------

def per_pixel_temporal_analysis(pixel_df):
    """Analyze each pixel's time-series across dates.

    Input: pandas DataFrame with columns pixel_row, pixel_col, date, NDVI, NDRE, etc.
    Output: pandas DataFrame with one row per pixel summarizing temporal behavior.
    """
    import pandas as pd

    pixel_df = pixel_df.copy()
    pixel_df["date"] = pd.to_datetime(pixel_df["date"])
    pixel_df = pixel_df.sort_values(["pixel_row", "pixel_col", "date"])

    grouped = pixel_df.groupby(["pixel_row", "pixel_col"])

    records = []
    for (pr, pc), group in grouped:
        group = group.sort_values("date")
        n_obs = len(group)

        ndvi_ts = group["NDVI"].values
        ndre_ts = group["NDRE"].values
        dates = group["date"].values

        # Savitzky-Golay smoothing (if enough points and scipy available)
        if HAS_SCIPY and n_obs >= 5:
            window = min(n_obs, 7)
            if window % 2 == 0:
                window -= 1
            window = max(window, 3)
            ndvi_smooth = savgol_filter(ndvi_ts, window, min(2, window - 1))
            ndre_smooth = savgol_filter(ndre_ts, window, min(2, window - 1))
        else:
            ndvi_smooth = ndvi_ts
            ndre_smooth = ndre_ts

        # Peak NDVI (heading date proxy)
        peak_idx = np.argmax(ndvi_smooth)
        peak_ndvi = ndvi_smooth[peak_idx]
        peak_date = dates[peak_idx]

        # Peak NDRE
        peak_ndre_idx = np.argmax(ndre_smooth)
        peak_ndre = ndre_smooth[peak_ndre_idx]

        # First derivative zero-crossing after peak (heading confirmation)
        if n_obs >= 3:
            deriv = np.gradient(ndvi_smooth)
            heading_date = peak_date
            for j in range(peak_idx, n_obs - 1):
                if deriv[j] > 0 and deriv[j + 1] <= 0:
                    heading_date = dates[j]
                    break
        else:
            heading_date = peak_date

        # NDVI drop during post-peak (stress indicator)
        if peak_idx < n_obs - 1:
            post_peak_ndvi = ndvi_smooth[peak_idx:]
            ndvi_drop = peak_ndvi - np.min(post_peak_ndvi)
        else:
            ndvi_drop = 0.0

        # Growth stage at last observation
        last_ndvi = ndvi_smooth[-1]
        if last_ndvi < 0.15:
            final_stage = "Bare/Harvested"
        elif peak_idx == n_obs - 1:
            final_stage = "Still Rising"
        elif last_ndvi >= 0.65:
            final_stage = "Peak Canopy"
        elif last_ndvi > 0.3:
            final_stage = "Reproductive"
        else:
            final_stage = "Senescence"

        records.append({
            "pixel_row": pr,
            "pixel_col": pc,
            "n_observations": n_obs,
            "ndvi_min": np.min(ndvi_ts),
            "ndvi_max": peak_ndvi,
            "ndvi_mean": np.mean(ndvi_ts),
            "ndre_max": peak_ndre,
            "peak_ndvi_date": str(peak_date)[:10],
            "heading_date": str(heading_date)[:10],
            "ndvi_drop_post_peak": ndvi_drop,
            "final_stage": final_stage,
        })

    return pd.DataFrame(records)


def field_level_phenology(field_df, phenology_df=None):
    """Derive a single-row field-level phenology summary.

    Combines smoothed field-mean NDVI time-series (from field_df) with
    aggregated per-pixel phenology (from phenology_df) to produce one
    row summarizing the field's entire season.

    Parameters:
        field_df: DataFrame from field_level_aggregation() — one row per date.
        phenology_df: DataFrame from per_pixel_temporal_analysis() — one row per pixel.
                      Optional; pixel-derived stats skipped if None.

    Returns:
        dict with field-level phenology metrics.
    """
    record = {}

    # --- From field_df: smooth field-mean NDVI and detect key dates ---
    field_df = field_df.sort_values("date").reset_index(drop=True)
    dates = field_df["date"].values
    ndvi_mean = field_df["NDVI_mean"].values
    n_dates = len(ndvi_mean)
    record["n_observations"] = n_dates

    # Smooth
    if HAS_SCIPY and n_dates >= 5:
        window = min(n_dates, 7)
        if window % 2 == 0:
            window -= 1
        window = max(window, 3)
        ndvi_smooth = savgol_filter(ndvi_mean, window, min(2, window - 1))
    else:
        ndvi_smooth = ndvi_mean.copy()

    # Peak NDVI
    peak_idx = np.argmax(ndvi_smooth)
    record["peak_ndvi"] = float(ndvi_smooth[peak_idx])
    record["peak_ndvi_date"] = str(dates[peak_idx])[:10]

    # Heading date (first-derivative zero-crossing after peak)
    heading_date = dates[peak_idx]
    if n_dates >= 3:
        deriv = np.gradient(ndvi_smooth)
        for j in range(peak_idx, n_dates - 1):
            if deriv[j] > 0 and deriv[j + 1] <= 0:
                heading_date = dates[j]
                break
    record["heading_date"] = str(heading_date)[:10]

    # Season start: first date NDVI crosses 0.2 (rising)
    season_start = str(dates[0])[:10]
    for i in range(n_dates - 1):
        if ndvi_smooth[i] < 0.2 and ndvi_smooth[i + 1] >= 0.2:
            season_start = str(dates[i + 1])[:10]
            break
    record["season_start"] = season_start

    # Season end: first date NDVI drops below 0.2 after peak
    season_end = str(dates[-1])[:10]
    for i in range(peak_idx, n_dates - 1):
        if ndvi_smooth[i] >= 0.2 and ndvi_smooth[i + 1] < 0.2:
            season_end = str(dates[i + 1])[:10]
            break
    record["season_end"] = season_end

    # Amplitude and cumulative
    record["ndvi_amplitude"] = float(ndvi_smooth[peak_idx] - np.min(ndvi_smooth))
    record["cumulative_ndvi"] = float(np.sum(ndvi_mean))

    # --- From phenology_df: pixel-aggregated stats ---
    if phenology_df is not None and len(phenology_df) > 0:
        record["n_pixels"] = len(phenology_df)

        # Heading date spread (std in days)
        import pandas as pd
        heading_dates = pd.to_datetime(phenology_df["heading_date"])
        heading_doy = heading_dates.dt.dayofyear
        record["heading_date_std_days"] = float(heading_doy.std())

        # Stress percentage
        stress_count = (phenology_df["ndvi_drop_post_peak"] > 0.15).sum()
        record["stress_pct"] = float(stress_count / len(phenology_df) * 100)

        # Spatial CV of peak NDVI
        peak_vals = phenology_df["ndvi_max"].values
        mean_peak = np.mean(peak_vals)
        if mean_peak > 0:
            record["spatial_cv"] = float(np.std(peak_vals) / mean_peak)
        else:
            record["spatial_cv"] = 0.0

        # Dominant final stage
        record["dominant_stage"] = phenology_df["final_stage"].mode().iloc[0]
    else:
        record["n_pixels"] = None
        record["heading_date_std_days"] = None
        record["stress_pct"] = None
        record["spatial_cv"] = None
        record["dominant_stage"] = None

    return record


def field_level_aggregation(pixel_df):
    """Aggregate per-pixel data to field-level stats per date.

    Returns DataFrame with one row per date (matches existing ts_df pattern).
    """
    import pandas as pd

    pixel_df = pixel_df.copy()
    pixel_df["date"] = pd.to_datetime(pixel_df["date"])

    index_cols = ["NDVI", "NDRE", "EVI", "SAVI", "CI_RedEdge"]
    records = []

    for date_val, group in pixel_df.groupby("date"):
        record = {"date": date_val, "n_pixels": len(group)}
        for col in index_cols:
            vals = group[col].dropna().values
            if len(vals) == 0:
                continue
            record[f"{col}_mean"] = np.mean(vals)
            record[f"{col}_std"] = np.std(vals)
            record[f"{col}_p10"] = np.percentile(vals, 10)
            record[f"{col}_p50"] = np.percentile(vals, 50)
            record[f"{col}_p90"] = np.percentile(vals, 90)
        records.append(record)

    return pd.DataFrame(records).sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 5. CSV export
# ---------------------------------------------------------------------------

def export_pixel_csv(result_dict, csv_path):
    """Write per-pixel columnar arrays to CSV."""
    keys = list(result_dict.keys())
    n_rows = len(result_dict[keys[0]])

    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(keys)
        for i in range(n_rows):
            row = []
            for k in keys:
                v = result_dict[k][i]
                if isinstance(v, (np.floating, float)):
                    row.append(f"{v:.6f}")
                elif isinstance(v, (np.integer, int)):
                    row.append(int(v))
                else:
                    row.append(v)
            writer.writerow(row)

    print(f"  Exported {n_rows:,} rows -> {csv_path} ({os.path.getsize(csv_path) / 1e6:.1f} MB)")


# ---------------------------------------------------------------------------
# 6. Main pipeline
# ---------------------------------------------------------------------------

def run_single_scene(sr_path, udm2_path=None, aoi_geoms=None,
                     date_str="unknown", output_dir="output"):
    """Process a single GeoTIFF and export per-pixel CSV."""
    print(f"\nProcessing: {os.path.basename(sr_path)}")
    print(f"  UDM2: {os.path.basename(udm2_path) if udm2_path else 'None'}")
    print(f"  Date: {date_str}")

    result, h, w = extract_pixels_from_scene(
        sr_path, udm2_path, aoi_geoms, date_str
    )

    if isinstance(result, list) and len(result) == 0:
        print("  No valid pixels found. Skipping.")
        return None

    n_pixels = len(result["pixel_row"])
    print(f"  Valid pixels: {n_pixels:,}")
    print(f"  Raster size: {w} x {h}")

    # Quick stats
    ndvi = result["NDVI"]
    print(f"  NDVI range: {np.min(ndvi):.4f} to {np.max(ndvi):.4f}, mean={np.mean(ndvi):.4f}")
    ndre = result["NDRE"]
    print(f"  NDRE range: {np.min(ndre):.4f} to {np.max(ndre):.4f}, mean={np.mean(ndre):.4f}")

    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, "pixel_timeseries.csv")
    export_pixel_csv(result, csv_path)

    return result


def run_multi_scene(scenes_dir, date_start, date_end, aoi_geoms=None,
                    output_dir="output", min_clean_pct=70.0):
    """Process multiple scenes and build per-pixel time-series."""
    import pandas as pd

    pairs = discover_scene_pairs(scenes_dir, date_start, date_end)
    print(f"\nFound {len(pairs)} scenes in {date_start} to {date_end}")

    if not pairs:
        print("No scenes found. Check --scenes-dir and date range.")
        return

    all_results = {k: [] for k in [
        "pixel_row", "pixel_col", "x", "y", "date",
        "B1_CoastalBlue", "B2_Blue", "B3_GreenI", "B4_Green",
        "B5_Yellow", "B6_Red", "B7_RedEdge", "B8_NIR",
        "NDVI", "NDRE", "EVI", "SAVI", "CI_RedEdge",
    ]}

    for pair in pairs:
        result, h, w = extract_pixels_from_scene(
            pair["sr_path"], pair["udm2_path"], aoi_geoms, pair["date_str"]
        )
        if isinstance(result, list) and len(result) == 0:
            print(f"  {pair['date_str']}  SKIPPED (no valid pixels)")
            continue

        n = len(result["pixel_row"])
        print(f"  {pair['date_str']}  OK  {n:,} pixels  NDVI={np.mean(result['NDVI']):.3f}")

        for k in all_results:
            all_results[k].append(result[k])

    # Concatenate all scenes
    for k in all_results:
        all_results[k] = np.concatenate(all_results[k])

    total = len(all_results["pixel_row"])
    print(f"\nTotal rows (all scenes): {total:,}")

    os.makedirs(output_dir, exist_ok=True)

    # Export full pixel-level CSV
    csv_path = os.path.join(output_dir, "pixel_timeseries.csv")
    export_pixel_csv(all_results, csv_path)

    # Build DataFrame for temporal analysis
    pixel_df = pd.DataFrame(all_results)

    # Per-pixel temporal analysis (only if multiple dates)
    n_dates = pixel_df["date"].nunique()
    phenology_df = None
    if n_dates > 1:
        print(f"\nRunning per-pixel temporal analysis across {n_dates} dates...")
        phenology_df = per_pixel_temporal_analysis(pixel_df)
        pheno_path = os.path.join(output_dir, "pixel_phenology.csv")
        phenology_df.to_csv(pheno_path, index=False)
        print(f"  Exported {len(phenology_df):,} pixel summaries -> {pheno_path}")
    else:
        print("\nOnly 1 date — skipping temporal analysis (need multiple scenes).")

    # Field-level aggregation (per-date stats)
    field_df = field_level_aggregation(pixel_df)
    field_path = os.path.join(output_dir, "field_summary.csv")
    field_df.to_csv(field_path, index=False, float_format="%.6f")
    print(f"  Exported {len(field_df)} date summaries -> {field_path}")

    # Field-level phenology (single-row season summary)
    if n_dates > 1:
        print("\nGenerating field-level phenology summary...")
        pheno_record = field_level_phenology(field_df, phenology_df)
        import pandas as pd
        pheno_summary_df = pd.DataFrame([pheno_record])
        pheno_summary_path = os.path.join(output_dir, "field_phenology.csv")
        pheno_summary_df.to_csv(pheno_summary_path, index=False, float_format="%.6f")
        print(f"  Exported 1 row -> {pheno_summary_path}")
        for k, v in pheno_record.items():
            print(f"    {k}: {v}")

    return pixel_df


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Pixel-level time-series extraction from PlanetScope GeoTIFFs"
    )
    parser.add_argument("--scene", help="Path to a single SR GeoTIFF")
    parser.add_argument("--udm2", help="Path to corresponding UDM2 GeoTIFF")
    parser.add_argument("--scenes-dir", help="Directory of multi-date scenes")
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2024-12-31")
    parser.add_argument("--date", default="unknown",
                        help="Date label for single-scene mode")
    parser.add_argument("--aoi", help="GeoJSON file for AOI clipping")
    parser.add_argument("--output-dir", default="output/pixel_timeseries",
                        help="Output directory for CSVs")
    args = parser.parse_args()

    aoi_geoms = load_aoi(args.aoi) if args.aoi else None

    if args.scene:
        run_single_scene(
            args.scene, args.udm2, aoi_geoms, args.date, args.output_dir
        )
    elif args.scenes_dir:
        run_multi_scene(
            args.scenes_dir, args.date_start, args.date_end,
            aoi_geoms, args.output_dir
        )
    else:
        parser.print_help()
        print("\nError: Provide --scene or --scenes-dir")
        sys.exit(1)


if __name__ == "__main__":
    main()
