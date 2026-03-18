#!/usr/bin/env python3
"""
SolarEdge API retrieval — all meters, 15-min resolution, full year.

Uses the energyDetails endpoint to get Production, Consumption,
SelfConsumption, FeedIn (grid export), and Purchased (grid import)
in a single API call per month chunk.

Usage:
    python solaredge_retrieve.py              # defaults to current year
    python solaredge_retrieve.py --year 2025
"""

import argparse
import os
import sys
import time
from datetime import datetime

import pandas as pd
import requests
from dotenv import load_dotenv
from tqdm import tqdm

# ── Configuration ────────────────────────────────────────────────────────────

load_dotenv()

API_KEY = os.getenv("SOLAREDGE_API_KEY")
SITE_ID = os.getenv("SOLAREDGE_SITE_ID")
BASE_URL = "https://monitoringapi.solaredge.com"

METERS = ["Production", "Consumption", "SelfConsumption", "FeedIn", "Purchased"]

# Canonical column order for output — always the same regardless of API order
COLUMN_ORDER = [
    "Production_kWh",
    "Consumption_kWh",
    "SelfConsumption_kWh",
    "FeedIn_kWh",
    "Purchased_kWh",
]

SITE_PEAK_POWER_KWP = 155  # kWp DC installed (east-west)


# ── API helpers ──────────────────────────────────────────────────────────────

def fetch_energy_details(site_id: str, api_key: str,
                         start: str, end: str,
                         max_retries: int = 3) -> dict:
    """
    Call energyDetails.json for one time window.
    Returns the raw JSON dict or raises on persistent failure.
    """
    url = f"{BASE_URL}/site/{site_id}/energyDetails.json"
    params = {
        "api_key": api_key,
        "timeUnit": "QUARTER_OF_AN_HOUR",
        "startTime": f"{start} 00:00:00",
        "endTime": f"{end} 23:59:59",
        "meters": ",".join(METERS),
    }

    for attempt in range(1, max_retries + 1):
        resp = requests.get(url, params=params, timeout=30)

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 429:
            wait = 2 ** attempt
            print(f"  Rate limited, waiting {wait}s (attempt {attempt}/{max_retries})")
            time.sleep(wait)
            continue

        if resp.status_code >= 500:
            wait = 2 ** attempt
            print(f"  Server error {resp.status_code}, waiting {wait}s (attempt {attempt}/{max_retries})")
            time.sleep(wait)
            continue

        # Client error — no point retrying
        resp.raise_for_status()

    raise RuntimeError(f"Failed after {max_retries} retries for {start} to {end}")


def parse_meter_response(json_data: dict) -> pd.DataFrame:
    """
    Parse the energyDetails JSON into a DataFrame with one column per meter.
    Missing values become NaN (not 0 — we distinguish "no data" from "zero production").
    """
    details = json_data.get("energyDetails", {})
    frames = {}

    for meter in details.get("meters", []):
        meter_type = meter["type"]
        col_name = f"{meter_type}_kWh"
        rows = {}
        for entry in meter["values"]:
            dt = pd.Timestamp(entry["date"])
            val = entry.get("value")  # None if key absent
            rows[dt] = round(val / 1000, 4) if val is not None else None
        frames[col_name] = pd.Series(rows, dtype="float64")

    if not frames:
        return pd.DataFrame()

    df = pd.DataFrame(frames)
    df.index.name = "datetime"
    return df


# ── Main retrieval ───────────────────────────────────────────────────────────

def retrieve_year(year: int) -> pd.DataFrame:
    """Retrieve all energy details for a full calendar year in monthly chunks."""

    start_date = datetime(year, 1, 1)
    end_date = datetime(year + 1, 1, 1)
    # Only generate months within the target year (Jan–Dec)
    months = pd.date_range(start=start_date, periods=12, freq="MS")

    chunks = []
    print(f"Retrieving {year} data in {len(months)} monthly chunks...")

    for month_start in tqdm(months, desc="Months"):
        month_end = (month_start + pd.DateOffset(months=1) - pd.DateOffset(days=1))
        from_str = month_start.strftime("%Y-%m-%d")
        to_str = month_end.strftime("%Y-%m-%d")

        data = fetch_energy_details(SITE_ID, API_KEY, from_str, to_str)
        df_chunk = parse_meter_response(data)

        if not df_chunk.empty:
            chunks.append(df_chunk)

    if not chunks:
        print("No data retrieved!")
        sys.exit(1)

    df = pd.concat(chunks).sort_index()

    # Remove duplicate timestamps (month boundaries can overlap)
    df = df[~df.index.duplicated(keep="first")]

    # Enforce canonical column order (add missing columns as NaN)
    for col in COLUMN_ORDER:
        if col not in df.columns:
            df[col] = float("nan")
    df = df[COLUMN_ORDER]

    return df


# ── Post-processing ──────────────────────────────────────────────────────────

def validate_completeness(df: pd.DataFrame, year: int) -> None:
    """Check data completeness and report gaps."""
    start = pd.Timestamp(f"{year}-01-01")
    end = pd.Timestamp(f"{year + 1}-01-01")
    expected = pd.date_range(start=start, end=end, freq="15min", inclusive="left")

    present = df.index
    missing = expected.difference(present)

    pct = len(present) / len(expected) * 100
    print(f"\nCompleteness: {len(present):,} / {len(expected):,} intervals ({pct:.1f}%)")

    if len(missing) > 0:
        print(f"Missing intervals: {len(missing):,}")
        # Show which months have gaps
        missing_months = missing.to_period("M").unique()
        print(f"Months with gaps: {', '.join(str(m) for m in sorted(missing_months))}")
    else:
        print("All 15-minute intervals present.")


def build_full_timeseries(df: pd.DataFrame, year: int) -> pd.DataFrame:
    """Reindex to complete 15-min grid, filling gaps with 0."""
    start = pd.Timestamp(f"{year}-01-01")
    end = pd.Timestamp(f"{year + 1}-01-01")
    full_index = pd.date_range(start=start, end=end, freq="15min",
                               inclusive="left", name="datetime")
    df = df.reindex(full_index)
    df = df.fillna(0.0)
    return df


def add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add power column derived from 15-min energy data."""
    df = df.copy()
    # Power in kW (energy per 15 min → multiply by 4)
    df["Production_kW"] = (df["Production_kWh"] * 4).round(3)
    return df


# ── Output ───────────────────────────────────────────────────────────────────

def save_outputs(df: pd.DataFrame, year: int) -> None:
    """Save compact (non-zero only) and full timeseries outputs."""
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(data_dir, exist_ok=True)

    base = f"solaredge_{year}_{SITE_PEAK_POWER_KWP}kWp"

    # CSV first (fast, no locking issues with OneDrive)
    # Use ; separator and , decimal for European Excel compatibility
    csv_path = os.path.join(data_dir, f"{base}_15min.csv")
    df.to_csv(csv_path, sep=";", decimal=",")
    print(f"Saved: {csv_path}")

    # Excel — can fail if OneDrive locks the file
    excel_path = os.path.join(data_dir, f"{base}_15min.xlsx")
    try:
        df.to_excel(excel_path)
        print(f"Saved: {excel_path}")
    except Exception as e:
        print(f"Warning: Excel save failed ({e}). CSV is available.")

    # Compact: PV production only, with a zero-row padded before/after each block
    production = df[["Production_kWh", "Production_kW"]]
    nonzero_mask = production["Production_kWh"] > 0

    # Timestamps one step before and after each production row
    pad_before = production.index[nonzero_mask] - pd.Timedelta(minutes=15)
    pad_after = production.index[nonzero_mask] + pd.Timedelta(minutes=15)

    # Combine: production rows + padding timestamps, keep unique, sort
    keep_idx = nonzero_mask[nonzero_mask].index.union(pad_before).union(pad_after)
    # Clip to year boundaries
    keep_idx = keep_idx[(keep_idx >= production.index.min()) &
                        (keep_idx <= production.index.max())]

    df_compact = production.reindex(keep_idx.sort_values(), fill_value=0.0)

    compact_csv = os.path.join(data_dir, f"{base}_compact.csv")
    df_compact.to_csv(compact_csv, sep=";", decimal=",")
    print(f"Saved: {compact_csv} ({len(df_compact):,} rows)")


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Retrieve SolarEdge energy data")
    parser.add_argument("--year", type=int, default=datetime.now().year,
                        help="Calendar year to retrieve (default: current year)")
    args = parser.parse_args()
    year = args.year

    if not API_KEY or not SITE_ID:
        print("Error: Set SOLAREDGE_API_KEY and SOLAREDGE_SITE_ID in .env")
        sys.exit(1)

    print(f"SolarEdge Energy Retrieval — Site {SITE_ID}, Year {year}")
    print(f"Meters: {', '.join(METERS)}")
    print()

    # 1. Retrieve
    df_raw = retrieve_year(year)
    print(f"\nRetrieved {len(df_raw):,} data points")

    # 2. Validate
    validate_completeness(df_raw, year)

    # 3. Build full timeseries
    df_full = build_full_timeseries(df_raw, year)

    # 4. Add derived columns
    df_full = add_derived_columns(df_full)

    # 5. Summary
    prod_kwh = df_full["Production_kWh"].sum()
    cons_kwh = df_full["Consumption_kWh"].sum()
    feedin_kwh = df_full["FeedIn_kWh"].sum()
    purchased_kwh = df_full["Purchased_kWh"].sum()
    selfcons_kwh = df_full["SelfConsumption_kWh"].sum()

    print(f"\n{'─' * 50}")
    print(f"Annual Summary {year}:")
    print(f"  PV Production:     {prod_kwh:>10,.0f} kWh")
    print(f"  Consumption:       {cons_kwh:>10,.0f} kWh")
    print(f"  Self-consumption:  {selfcons_kwh:>10,.0f} kWh")
    print(f"  Grid export:       {feedin_kwh:>10,.0f} kWh")
    print(f"  Grid import:       {purchased_kwh:>10,.0f} kWh")
    print(f"  Self-cons ratio:   {selfcons_kwh / prod_kwh * 100:>10.1f} %" if prod_kwh > 0 else "")
    print(f"{'─' * 50}")

    # 6. Save
    save_outputs(df_full, year)
    print("\nDone!")


if __name__ == "__main__":
    main()
