"""
screen_candidate_days.py
------------------------
Fast screening of all Northgate days in the Z2Z dataset to identify
WORST-CASE candidates for full MILP optimization.

Computes five metrics per day WITHOUT running any optimization:

  n_events              - serviceable charging events (EV-matched, min-1h dwell)
  total_energy_kwh      - sum of energy needed across all events
  peak_concurrent_kw    - max simultaneous sum of max-DC-charge-kW for
                          vehicles present at the same time (upper bound on
                          charger capacity required if all charge in parallel)
  heavy_count           - events needing DC 350 kW chargers
                          (Freightliner eCascadia, BYD 6F, eM2, Hummer, Silverado EV)
  peak_window_events    - events present during 4–9 p.m. local (SMUD peak window)

A composite "worst-case score" is computed as a weighted sum of
z-scored metrics.  Days are ranked descending.  Top-N are printed and
saved to a CSV for use as inputs to run_multi_day.py.

Usage:
    python screen_candidate_days.py [--top N] [--out ranked_days.csv]
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ── Config ────────────────────────────────────────────────────────────────────
CHARGER_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
CACHE_CSV   = CHARGER_DIR / "_northgate_z2z_cache.csv"
Z2Z_CSV     = (
    CHARGER_DIR
    / "Geotab_Zone_to_Zone_Dataset"
    / "Geotab_Zone_to_Zone_Dataset"
    / "01_Final_Dataset"
    / "final_zone_to_zone_all_rows.csv"
)
EV_CATEGORIES_XLSX = Path(r"D:\Geotab_EV_Parameters\final_categories.xlsx")
CHARGE_RATE_XLSX   = CHARGER_DIR / "ev_equivalent_max_charge_power_mapping_filled.xlsx"

TARGET_ZONE  = "Northgate"
TZ_LOCAL     = "America/Los_Angeles"
MIN_DWELL_H  = 1.0        # same as extract_z2z_events.py
MIN_ENERGY   = 0.10       # kWh — discard events below this
SOC_FALLBACK = 50.0       # % arrival SOC when distance is missing
ETA          = 0.90
PEAK_START_H = 16.0       # 4 p.m. local
PEAK_END_H   = 21.0       # 9 p.m. local

# DC charge rates ≥ this are considered "heavy" (drives DC 350 kW charger selection)
HEAVY_DC_KW_THRESHOLD = 200.0

# Composite score weights
# peak_tight_vehicles = simultaneous vehicles that monopolize a charger (dwell ~1h, can't share)
# This is the strongest predictor of required charger COUNT
WEIGHTS = {
    "peak_tight_sim":  0.40,   # peak simultaneous TIGHT vehicles (≤1.5h dwell) → charger count
    "peak_sim_heavy":  0.25,   # peak simultaneous DC≥350kW → DC 350kW count
    "peak_sim_medium": 0.20,   # peak simultaneous DC 100-350kW → DC 150kW count
    "total_energy_kwh": 0.10,
    "n_events":         0.05,
}


# ── Build / load Northgate cache ──────────────────────────────────────────────

def _ensure_cache() -> pd.DataFrame:
    """Return Northgate-filtered Z2Z dataframe (build cache if missing)."""
    if CACHE_CSV.exists():
        print(f"[cache] Loading {CACHE_CSV.name} …")
        df = pd.read_csv(CACHE_CSV, low_memory=False)
        print(f"[cache] {len(df):,} rows")
        return df

    print("[cache] Cache not found — reading full Z2Z CSV (may take ~60 s) …")
    needed = [
        "vehicle_name", "make", "model", "year",
        "to_zone", "to_entry_time", "to_exit_time", "to_dwell_minutes",
        "trip_first_distance_miles_between", "use_for_optimization_bool",
    ]
    df = pd.read_csv(str(Z2Z_CSV), usecols=needed, low_memory=False)
    df = df[df["use_for_optimization_bool"].fillna(False).astype(bool)].copy()
    df = df[df["to_zone"].str.contains(TARGET_ZONE, case=False, na=False)].copy()
    df.to_csv(CACHE_CSV, index=False)
    print(f"[cache] Saved {len(df):,} Northgate rows to {CACHE_CSV.name}")
    return df


# ── EV model matching + charge-rate join ──────────────────────────────────────

def _enrich(df: pd.DataFrame, ez) -> pd.DataFrame:
    """Add ev_model, max_dc_charge_kw, battery_kwh, eff_kwh_mi columns."""
    ice_to_ev  = ez._load_ev_equivalencies(EV_CATEGORIES_XLSX)
    charge_df  = ez._load_charge_rates(CHARGE_RATE_XLSX)
    charge_map = charge_df.set_index("ev_equivalent_model").to_dict("index")

    # Map each unique vehicle to EV model
    unique = (
        df[["vehicle_name", "make", "model", "year"]]
        .drop_duplicates("vehicle_name")
        .fillna({"make": "", "model": "", "year": ""})
    )
    ev_map = {
        row["vehicle_name"]: ez._match_ev(str(row["make"]), str(row["model"]), ice_to_ev)
        for _, row in unique.iterrows()
    }
    df = df.copy()
    df["ev_model"] = df["vehicle_name"].map(ev_map)
    df = df[df["ev_model"].notna()].copy()

    # Join charge rates
    df["max_dc_charge_kw"] = df["ev_model"].map(
        lambda m: charge_map.get(m, {}).get("max_dc_charge_kw", 0.0)
    )

    # EV battery + efficiency from EV_SPEC_OVERRIDES in extract script
    spec = ez.EV_SPEC_OVERRIDES
    df["battery_kwh"]   = df["ev_model"].map(lambda m: spec.get(m, (None, None))[0])
    df["eff_kwh_mi"]    = df["ev_model"].map(lambda m: spec.get(m, (None, None))[1])

    return df


# ── Per-event energy estimate ─────────────────────────────────────────────────

def _compute_energy(df: pd.DataFrame) -> pd.DataFrame:
    """Estimate energy_needed_kwh for each event (mirrors extract_z2z_events logic)."""
    df = df.copy()
    dist = pd.to_numeric(df["trip_first_distance_miles_between"], errors="coerce")
    batt = df["battery_kwh"]
    eff  = df["eff_kwh_mi"]

    # Energy from trip distance
    trip_energy = (dist * eff / ETA).where(dist > 0)

    # Fallback: SOC_FALLBACK% of battery when distance is missing
    fallback_energy = (1.0 - SOC_FALLBACK / 100.0) * batt

    energy = trip_energy.where(trip_energy.notna(), fallback_energy)

    # Cap at battery room (100% SOC)
    energy = energy.clip(upper=batt)
    df["energy_kwh"] = energy
    return df[df["energy_kwh"] >= MIN_ENERGY].copy()


# ── Per-day metrics ───────────────────────────────────────────────────────────

def _peak_simultaneous(arrivals, departures, dc_kws=None,
                        kw_min: float = 0, kw_max: float = 1e9,
                        freq_min: int = 5) -> int:
    """
    Max simultaneous vehicle count (optionally filtered by DC rate band).
    kw_min / kw_max filter on dc_kws (inclusive/exclusive).
    Returns integer count.
    """
    if len(arrivals) == 0:
        return 0
    t_min = arrivals.min()
    t_max = departures.max()
    if pd.isna(t_min) or pd.isna(t_max):
        return 0
    # Cap t_max to t_min + 2 days to ignore bad dwell outliers
    t_max = min(t_max, t_min + pd.Timedelta(days=2))
    times = pd.date_range(t_min, t_max, freq=f"{freq_min}min")
    peak = 0
    for t in times:
        present = (arrivals <= t) & (departures > t)
        if dc_kws is not None:
            present = present & (dc_kws >= kw_min) & (dc_kws < kw_max)
        count = int(present.sum())
        if count > peak:
            peak = count
    return peak


def _peak_window_count(arrivals, departures) -> int:
    """Number of events present at any point during the 4–9 p.m. local window."""
    count = 0
    for arr, dep in zip(arrivals, departures):
        if pd.isna(arr) or pd.isna(dep):
            continue
        arr_loc = arr.tz_convert(TZ_LOCAL)
        dep_loc = dep.tz_convert(TZ_LOCAL)
        arr_h   = arr_loc.hour + arr_loc.minute / 60
        dep_h   = dep_loc.hour + dep_loc.minute / 60
        # overlap with [PEAK_START_H, PEAK_END_H)
        if arr_h < PEAK_END_H and dep_h > PEAK_START_H:
            count += 1
    return count


def compute_day_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """
    df must have: _date_pacific, to_entry_time (UTC tz-aware), to_exit_time,
                  energy_kwh, max_dc_charge_kw, dwell_h_eff.
    Returns one row per date with all screening metrics.
    """
    records = []
    for date, grp in df.groupby("_date_pacific"):
        arr = grp["to_entry_time"]
        dep = grp["to_exit_time_eff"]
        dc  = grp["max_dc_charge_kw"]

        n_ev       = len(grp)
        e_tot      = grp["energy_kwh"].sum()
        dwell_h    = (dep - arr).dt.total_seconds() / 3600.0

        # Tight vehicles: dwell ≤ 1.5 h → essentially monopolize one charger slot
        tight_mask = dwell_h <= 1.5
        arr_tight  = arr[tight_mask].reset_index(drop=True)
        dep_tight  = dep[tight_mask].reset_index(drop=True)
        dc_tight   = dc[tight_mask].reset_index(drop=True)

        # Peak simultaneous TIGHT vehicles — best predictor of charger count
        peak_tight  = _peak_simultaneous(arr_tight, dep_tight)
        # Peak simultaneous heavy vehicles (DC ≥ 350 kW → drives DC 350kW count)
        peak_heavy  = _peak_simultaneous(arr, dep, dc, kw_min=350)
        # Peak simultaneous medium vehicles (DC 100–350 kW → drives DC 150kW count)
        peak_med    = _peak_simultaneous(arr, dep, dc, kw_min=100, kw_max=350)
        peak_sim    = _peak_simultaneous(arr, dep)
        pw_ev       = _peak_window_count(arr, dep)

        records.append({
            "date":              date,
            "n_events":          n_ev,
            "total_energy_kwh":  round(e_tot, 1),
            "peak_sim_vehicles": peak_sim,
            "peak_tight_sim":    peak_tight,
            "peak_sim_heavy":    peak_heavy,
            "peak_sim_medium":   peak_med,
            "peak_window_events": pw_ev,
        })

    return pd.DataFrame(records)


# ── Composite ranking ─────────────────────────────────────────────────────────

def rank_days(metrics: pd.DataFrame) -> pd.DataFrame:
    """Add z-score composite and rank descending."""
    scored = metrics.copy()
    z_cols = []
    for col, w in WEIGHTS.items():
        z = (scored[col] - scored[col].mean()) / (scored[col].std() + 1e-9)
        scored[f"_z_{col}"] = z * w
        z_cols.append(f"_z_{col}")
    scored["score"] = scored[z_cols].sum(axis=1)
    scored = scored.drop(columns=z_cols).sort_values("score", ascending=False)
    scored.insert(0, "rank", range(1, len(scored) + 1))
    return scored.reset_index(drop=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main(top_n: int = 20, out_csv: Path | None = None) -> pd.DataFrame:
    sys.path.insert(0, str(CHARGER_DIR))
    ez = importlib.import_module("extract_z2z_events")

    # 1. Load / build Northgate cache
    raw = _ensure_cache()

    # 2. Parse timestamps
    print("[enrich] Parsing timestamps …")
    raw["to_entry_time"] = pd.to_datetime(raw["to_entry_time"], utc=True, errors="coerce")
    raw["to_exit_time"]  = pd.to_datetime(raw["to_exit_time"],  utc=True, errors="coerce")
    raw["_date_pacific"] = (
        raw["to_entry_time"].dt.tz_convert(TZ_LOCAL).dt.strftime("%Y-%m-%d")
    )

    # 3. EV model matching + charge rates
    print("[enrich] Matching EV models …")
    df = _enrich(raw, ez)

    # 4. Dwell time (apply min-1h extension)
    dwell_raw = pd.to_numeric(df["to_dwell_minutes"], errors="coerce").fillna(60.0) / 60.0
    dwell_eff = dwell_raw.clip(lower=MIN_DWELL_H)
    df["to_exit_time_eff"] = df["to_entry_time"] + pd.to_timedelta(dwell_eff, unit="h")

    # 5. Energy estimate
    print("[enrich] Computing energy needs …")
    df = _compute_energy(df)

    # 6. Per-day metrics
    print(f"[screen] Computing metrics across {df['_date_pacific'].nunique()} dates …")
    metrics = compute_day_metrics(df)

    # 7. Rank
    ranked = rank_days(metrics)

    # 8. Output
    print(f"\n{'='*75}")
    print(f"TOP {top_n} WORST-CASE NORTHGATE DAYS  (composite score, descending)")
    print(f"{'='*75}")
    print(f"{'Rank':>4}  {'Date':>12}  {'Evts':>5}  {'PkTight':>8}  "
          f"{'PkHvy':>6}  {'PkMed':>6}  {'PkAll':>6}  {'Score':>7}")
    print(f"{'':->4}  {'':->12}  {'':->5}  {'':->8}  {'':->6}  {'':->6}  {'':->6}  {'':->7}")
    for _, r in ranked.head(top_n).iterrows():
        print(
            f"{int(r['rank']):>4}  {r['date']:>12}  {int(r['n_events']):>5}  "
            f"{int(r['peak_tight_sim']):>8}  {int(r['peak_sim_heavy']):>6}  "
            f"{int(r['peak_sim_medium']):>6}  {int(r['peak_sim_vehicles']):>6}  "
            f"{r['score']:>7.3f}"
        )

    out = out_csv or (CHARGER_DIR / "northgate_ranked_days.csv")
    ranked.to_csv(out, index=False)
    print(f"\nFull ranked table ({len(ranked)} days) saved -> {out}")

    return ranked


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Screen worst-case Northgate days")
    parser.add_argument("--top",  type=int, default=20,  help="Number of top days to print")
    parser.add_argument("--out",  type=str, default=None, help="Output CSV path")
    args = parser.parse_args()
    main(top_n=args.top, out_csv=Path(args.out) if args.out else None)
