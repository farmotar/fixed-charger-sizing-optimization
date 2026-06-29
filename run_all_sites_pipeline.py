"""
run_all_sites_pipeline.py
==========================
End-to-end sizing pipeline for all 4 Caltrans sites:
    Northgate Â. Fresno Â. Glendale Â. San Diego (Kearney Mesa)

Phases:
  1. Build site-filtered Z2Z caches (reads 3.3 GB CSV once for new sites)
  2. Screen + rank days by demand severity  --  save ranked_days CSV per site
  3. Extract MILP event CSVs for top-N days per site
  4. Run XOS SOC simulation + Kempower MILP for each event CSV
  5. Compile 4-site summary table â†' CSV + printed table

Usage:
    python run_all_sites_pipeline.py              # all phases, top-5 days per site
    python run_all_sites_pipeline.py --top 3      # top 3 days per site
    python run_all_sites_pipeline.py --phase 1    # only phase 1 (build caches)
    python run_all_sites_pipeline.py --phase 4    # only phase 4 (run analysis)

Outputs per site (in BASE_DIR/site_outputs/<site>/):
    <site>_ranked_days.csv
    z2z_milp_events_<site>_<date>.csv  (one per top-N day)
    xos_results/                        (XOS simulation outputs)
    kempower_results/                   (Kempower MILP outputs)
    <site>_sizing_summary.csv
"""

from __future__ import annotations

import argparse
import importlib
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# ------------------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------------------

BASE_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
sys.path.insert(0, str(BASE_DIR))

Z2Z_CSV = (
    BASE_DIR
    / "Geotab_Zone_to_Zone_Dataset"
    / "Geotab_Zone_to_Zone_Dataset"
    / "01_Final_Dataset"
    / "final_zone_to_zone_all_rows.csv"
)
EV_CATEGORIES_XLSX = Path(r"D:\Geotab_EV_Parameters\final_categories.xlsx")
CHARGE_RATE_XLSX   = BASE_DIR / "ev_equivalent_max_charge_power_mapping_filled.xlsx"
OUT_DIR            = BASE_DIR / "site_outputs"

SITES: dict[str, dict] = {
    "northgate": {
        "zone":  "23143 Northgate",
        "cache": BASE_DIR / "_northgate_z2z_cache.csv",
        "label": "Northgate",
    },
    "fresno": {
        "zone":  "26101 Shop 26 Fresno",
        "cache": BASE_DIR / "_fresno_z2z_cache.csv",
        "label": "Fresno",
    },
    "glendale": {
        "zone":  "07 GLENDALE HMS",
        "cache": BASE_DIR / "_glendale_z2z_cache.csv",
        "label": "Glendale",
    },
    "san_diego": {
        "zone":  "31101 Shop 31 Kearney Mesa",
        "cache": BASE_DIR / "_san_diego_z2z_cache.csv",
        "label": "San Diego",
    },
}

# Days-screening parameters (from screen_candidate_days.py)
TZ_LOCAL     = "America/Los_Angeles"
MIN_DWELL_H  = 1.0
MIN_ENERGY   = 0.10
SOC_FALLBACK = 50.0
ETA          = 0.90
PEAK_START_H = 16.0
PEAK_END_H   = 21.0
WEIGHTS = {
    "peak_tight_sim":   0.40,
    "peak_sim_heavy":   0.25,
    "peak_sim_medium":  0.20,
    "total_energy_kwh": 0.10,
    "n_events":         0.05,
}
HEAVY_DC_KW  = 200.0

NEEDED_COLS = [
    "vehicle_name", "make", "model", "year",
    "to_zone", "to_entry_time", "to_exit_time", "to_dwell_minutes",
    "trip_first_distance_miles_between", "use_for_optimization_bool",
]


# ------------------------------------------------------------------------------
# PHASE 1  --  Build site caches
# ------------------------------------------------------------------------------

def phase1_build_caches(force: bool = False) -> None:
    """Read big Z2Z CSV once, write a filtered cache for each site that needs one."""
    need_build = [
        s for s, cfg in SITES.items()
        if not cfg["cache"].exists() or force
    ]
    if not need_build:
        print("[Phase 1] All site caches exist  --  skipping.")
        return

    print(f"[Phase 1] Need to build caches for: {need_build}")
    print(f"  Reading {Z2Z_CSV.name}  ({Z2Z_CSV.stat().st_size / 1e9:.1f} GB) ...")
    t0 = datetime.now()
    df = pd.read_csv(str(Z2Z_CSV), usecols=NEEDED_COLS, low_memory=False)
    df = df[df["use_for_optimization_bool"].fillna(False).astype(bool)].copy()
    print(f"  {len(df):,} rows after use_for_optimization filter  ({(datetime.now()-t0).seconds}s)")

    for site_name in need_build:
        cfg  = SITES[site_name]
        mask = df["to_zone"].str.contains(cfg["zone"], case=False, na=False)
        sdf  = df[mask].copy()
        sdf.to_csv(str(cfg["cache"]), index=False)
        print(f"  [{cfg['label']}] {len(sdf):,} rows â†' {cfg['cache'].name}")

    print("[Phase 1] Done.")


# ------------------------------------------------------------------------------
# PHASE 2  --  Screen and rank days
# ------------------------------------------------------------------------------

def _peak_sim(arrivals: pd.Series, departures: pd.Series,
              dc_kws: pd.Series | None = None,
              kw_min: float = 0, kw_max: float = 1e9,
              freq_min: int = 5) -> int:
    if arrivals.empty:
        return 0
    t_min = arrivals.min()
    t_max = min(departures.max(), t_min + pd.Timedelta(days=2))
    if pd.isna(t_min) or pd.isna(t_max):
        return 0
    times = pd.date_range(t_min, t_max, freq=f"{freq_min}min")
    peak  = 0
    for t in times:
        present = (arrivals <= t) & (departures > t)
        if dc_kws is not None:
            present = present & (dc_kws >= kw_min) & (dc_kws < kw_max)
        peak = max(peak, int(present.sum()))
    return peak


def _peak_window(arrivals: pd.Series, departures: pd.Series) -> int:
    count = 0
    for arr, dep in zip(arrivals, departures):
        if pd.isna(arr) or pd.isna(dep):
            continue
        a_h = arr.tz_convert(TZ_LOCAL).hour + arr.tz_convert(TZ_LOCAL).minute / 60
        d_h = dep.tz_convert(TZ_LOCAL).hour + dep.tz_convert(TZ_LOCAL).minute / 60
        if a_h < PEAK_END_H and d_h > PEAK_START_H:
            count += 1
    return count


def _compute_day_metrics(df: pd.DataFrame) -> pd.DataFrame:
    records = []
    for date, grp in df.groupby("_date_pacific"):
        arr  = grp["to_entry_time"]
        dep  = grp["to_exit_time_eff"]
        dc   = grp["max_dc_charge_kw"]

        dwell_h = (dep - arr).dt.total_seconds() / 3600.0
        tight_m = dwell_h <= 1.5

        records.append({
            "date":               date,
            "n_events":           len(grp),
            "total_energy_kwh":   round(grp["energy_kwh"].sum(), 1),
            "peak_sim_vehicles":  _peak_sim(arr, dep),
            "peak_tight_sim":     _peak_sim(arr[tight_m], dep[tight_m]),
            "peak_sim_heavy":     _peak_sim(arr, dep, dc, kw_min=HEAVY_DC_KW),
            "peak_sim_medium":    _peak_sim(arr, dep, dc, kw_min=100, kw_max=HEAVY_DC_KW),
            "peak_window_events": _peak_window(arr, dep),
        })
    return pd.DataFrame(records)


def _rank_days(metrics: pd.DataFrame) -> pd.DataFrame:
    scored = metrics.copy()
    z_cols = []
    for col, w in WEIGHTS.items():
        if col not in scored.columns:
            continue
        z = (scored[col] - scored[col].mean()) / (scored[col].std() + 1e-9)
        scored[f"_z_{col}"] = z * w
        z_cols.append(f"_z_{col}")
    scored["score"] = scored[z_cols].sum(axis=1)
    scored = scored.drop(columns=z_cols).sort_values("score", ascending=False)
    scored.insert(0, "rank", range(1, len(scored) + 1))
    return scored.reset_index(drop=True)


def phase2_screen_days(top_n: int = 5) -> dict[str, pd.DataFrame]:
    """
    For each site, load cache, enrich with EV data, compute metrics, rank.
    Returns {site_name: ranked_days_df}.
    """
    ez = importlib.import_module("extract_z2z_events")
    ice_to_ev  = ez._load_ev_equivalencies(EV_CATEGORIES_XLSX)
    charge_df  = ez._load_charge_rates(CHARGE_RATE_XLSX)
    charge_map = charge_df.set_index("ev_equivalent_model").to_dict("index")

    results: dict[str, pd.DataFrame] = {}

    for site_name, cfg in SITES.items():
        print(f"\n[Phase 2] {cfg['label']}  --  screening days ...")
        out_dir = OUT_DIR / site_name
        out_dir.mkdir(parents=True, exist_ok=True)
        ranked_csv = out_dir / f"{site_name}_ranked_days.csv"

        if ranked_csv.exists():
            print(f"  Found existing {ranked_csv.name}  --  loading")
            results[site_name] = pd.read_csv(ranked_csv)
            top = results[site_name].head(top_n)
            print(f"  Top {top_n}: {top['date'].tolist()}")
            continue

        if not cfg["cache"].exists():
            print(f"  !! Cache missing for {cfg['label']}  --  run phase 1 first")
            continue

        # Load cache
        raw = pd.read_csv(str(cfg["cache"]), low_memory=False)
        print(f"  {len(raw):,} cached rows")

        # Parse timestamps
        raw["to_entry_time"] = pd.to_datetime(raw["to_entry_time"], utc=True, errors="coerce")
        raw["to_exit_time"]  = pd.to_datetime(raw["to_exit_time"],  utc=True, errors="coerce")
        raw["_date_pacific"] = (
            raw["to_entry_time"].dt.tz_convert(TZ_LOCAL).dt.strftime("%Y-%m-%d")
        )

        # EV model matching
        unique = (
            raw[["vehicle_name", "make", "model", "year"]]
            .drop_duplicates("vehicle_name")
            .fillna({"make": "", "model": "", "year": ""})
        )
        ev_map = {
            row["vehicle_name"]: ez._match_ev(str(row["make"]), str(row["model"]), ice_to_ev)
            for _, row in unique.iterrows()
        }
        raw["ev_model"] = raw["vehicle_name"].map(ev_map)
        raw = raw[raw["ev_model"].notna()].copy()

        # Charge rates
        raw["max_dc_charge_kw"] = raw["ev_model"].map(
            lambda m: float(charge_map.get(m, {}).get("max_dc_charge_kw", 50.0))
        )

        # Energy estimate
        raw["battery_kwh"] = raw["ev_model"].map(
            lambda m: ez.EV_SPEC_OVERRIDES.get(m, (None, None))[0]
        )
        raw["eff_kwh_mi"] = raw["ev_model"].map(
            lambda m: ez.EV_SPEC_OVERRIDES.get(m, (None, None))[1]
        )
        raw = raw[raw["battery_kwh"].notna()].copy()

        dist = pd.to_numeric(raw["trip_first_distance_miles_between"], errors="coerce")
        trip_e   = (dist * raw["eff_kwh_mi"] / ETA).where(dist > 0)
        fallback = (1.0 - SOC_FALLBACK / 100.0) * raw["battery_kwh"]
        raw["energy_kwh"] = trip_e.where(trip_e.notna(), fallback).clip(upper=raw["battery_kwh"])
        raw = raw[raw["energy_kwh"] >= MIN_ENERGY].copy()

        # Min-dwell extension for effective exit time
        dwell_h_actual = raw["to_dwell_minutes"].fillna(0) / 60.0
        short_mask = dwell_h_actual < MIN_DWELL_H
        raw["to_exit_time_eff"] = raw["to_exit_time"].copy()
        raw.loc[short_mask, "to_exit_time_eff"] = (
            raw.loc[short_mask, "to_entry_time"]
            + pd.to_timedelta(MIN_DWELL_H, unit="h")
        )

        # Day metrics + ranking
        metrics = _compute_day_metrics(raw)
        if metrics.empty:
            print(f"  !! No valid days for {cfg['label']}")
            continue
        ranked = _rank_days(metrics)
        ranked.to_csv(ranked_csv, index=False)
        print(f"  Ranked {len(ranked)} days â†' {ranked_csv.name}")
        top = ranked.head(top_n)
        print(f"  Top {top_n}: {top['date'].tolist()}")
        results[site_name] = ranked

    return results


# ------------------------------------------------------------------------------
# PHASE 3  --  Extract event CSVs for top-N days per site
# ------------------------------------------------------------------------------

def phase3_extract_events(ranked: dict[str, pd.DataFrame], top_n: int = 5) -> dict[str, list[Path]]:
    """
    For each site, extract MILP event CSVs for the top-N days.
    Northgate: also scan existing z2z_milp_events_northgate_*.csv files.
    Returns {site_name: [event_csv_path, ...]}.
    """
    ez = importlib.import_module("extract_z2z_events")
    results: dict[str, list[Path]] = {}

    for site_name, cfg in SITES.items():
        out_dir = OUT_DIR / site_name
        out_dir.mkdir(parents=True, exist_ok=True)

        # Northgate: use already-generated event CSVs from BASE_DIR (top-N only)
        if site_name == "northgate":
            existing = sorted(BASE_DIR.glob("z2z_milp_events_northgate_*.csv"))
            if site_name in ranked and not ranked[site_name].empty:
                top_days = ranked[site_name].head(top_n)["date"].tolist()
                top_tags = {d.replace("-", "_") for d in top_days}
                existing = [p for p in existing if any(t in p.stem for t in top_tags)]
            results["northgate"] = existing
            print(f"[Phase 3] northgate: {len(existing)} event CSVs (top-{top_n})")
            continue

        if site_name not in ranked:
            print(f"[Phase 3] {cfg['label']}: no ranked days  --  skipping")
            results[site_name] = []
            continue

        top_days = ranked[site_name].head(top_n)["date"].tolist()
        event_csvs: list[Path] = []

        # Load shared EV data once
        ice_to_ev  = ez._load_ev_equivalencies(EV_CATEGORIES_XLSX)
        charge_df  = ez._load_charge_rates(CHARGE_RATE_XLSX)
        charge_map = charge_df.set_index("ev_equivalent_model").to_dict("index")

        cache_df = pd.read_csv(str(cfg["cache"]), low_memory=False)
        cache_df["to_entry_time"] = pd.to_datetime(cache_df["to_entry_time"], utc=True, errors="coerce")
        cache_df["to_exit_time"]  = pd.to_datetime(cache_df["to_exit_time"],  utc=True, errors="coerce")
        cache_df["_date_pacific"] = (
            cache_df["to_entry_time"].dt.tz_convert(TZ_LOCAL).dt.strftime("%Y-%m-%d")
        )

        for date_str in top_days:
            label    = site_name.replace("_", "")
            date_tag = date_str.replace("-", "_")
            csv_path = out_dir / f"z2z_milp_events_{label}_{date_tag}.csv"

            if csv_path.exists():
                print(f"[Phase 3] {cfg['label']} {date_str}: event CSV exists  --  skipping")
                event_csvs.append(csv_path)
                continue

            day_df = cache_df[cache_df["_date_pacific"] == date_str].copy()
            if day_df.empty:
                print(f"[Phase 3] {cfg['label']} {date_str}: no rows in cache")
                continue

            # EV matching
            unique = (
                day_df[["vehicle_name", "make", "model", "year"]]
                .drop_duplicates("vehicle_name")
                .fillna({"make": "", "model": "", "year": ""})
            )
            ev_map = {
                row["vehicle_name"]: ez._match_ev(str(row["make"]), str(row["model"]), ice_to_ev)
                for _, row in unique.iterrows()
            }
            day_df["ev_equivalent_model"] = day_df["vehicle_name"].map(ev_map)
            day_df = day_df[day_df["ev_equivalent_model"].notna()].copy()

            # Charge rates + specs
            DC_FALLBACK = {"Global Electric Street Sweeper (M4E)": 60.0}
            AC_FALLBACK = {"Global Electric Street Sweeper (M4E)": 0.0}

            day_df["max_ac_charge_kw"]       = day_df["ev_equivalent_model"].map(
                lambda m: float(charge_map.get(m, {}).get("max_ac_charge_kw",
                                AC_FALLBACK.get(m, 7.2)))
            )
            day_df["max_dc_charge_kw"]       = day_df["ev_equivalent_model"].map(
                lambda m: float(charge_map.get(m, {}).get("max_dc_charge_kw",
                                DC_FALLBACK.get(m, 50.0)))
            )
            day_df["battery_capacity_kwh"]   = day_df["ev_equivalent_model"].map(
                lambda m: ez.EV_SPEC_OVERRIDES.get(m, (None, None))[0]
            )
            day_df["efficiency_kwh_per_mile"] = day_df["ev_equivalent_model"].map(
                lambda m: ez.EV_SPEC_OVERRIDES.get(m, (None, None))[1]
            )
            day_df = day_df[day_df["battery_capacity_kwh"].notna()].copy()

            # Energy
            dist   = pd.to_numeric(day_df["trip_first_distance_miles_between"], errors="coerce")
            trip_e = (dist * day_df["efficiency_kwh_per_mile"] / ETA).where(dist > 0)
            fback  = (1.0 - SOC_FALLBACK / 100.0) * day_df["battery_capacity_kwh"]
            day_df["energy_needed_kwh_for_visit"] = (
                trip_e.where(trip_e.notna(), fback)
                .clip(upper=day_df["battery_capacity_kwh"])
            )
            day_df = day_df[day_df["energy_needed_kwh_for_visit"] >= MIN_ENERGY].copy()

            # Dwell extension
            dwell_h = day_df["to_dwell_minutes"].fillna(0) / 60.0
            short_m = dwell_h < MIN_DWELL_H
            day_df["to_exit_time_eff"] = day_df["to_exit_time"].copy()
            day_df.loc[short_m, "to_exit_time_eff"] = (
                day_df.loc[short_m, "to_entry_time"]
                + pd.to_timedelta(MIN_DWELL_H, unit="h")
            )

            # Initial / target SOC
            day_df["assumed_initial_soc_percent"] = (
                (1.0 - day_df["energy_needed_kwh_for_visit"] / day_df["battery_capacity_kwh"])
                .clip(0.0, 1.0) * 100.0
            ).round(1)
            day_df["target_soc_percent"] = 100.0

            # Charging event ID
            day_df = day_df.reset_index(drop=True)
            day_df["charging_event_id"] = [
                f"{label.upper()}_{date_tag}_{i+1:03d}" for i in range(len(day_df))
            ]

            # Format times as ISO strings
            day_df["arrival_time"]   = day_df["to_entry_time"].dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            day_df["departure_time"] = day_df["to_exit_time_eff"].dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")

            # Write 14-column MILP events CSV
            out_cols = [
                "charging_event_id", "vehicle_name", "ev_equivalent_model",
                "arrival_time", "departure_time",
                "energy_needed_kwh_for_visit",
                "max_ac_charge_kw", "max_dc_charge_kw",
                "battery_capacity_kwh",
                "assumed_initial_soc_percent", "target_soc_percent",
            ]
            out_df = day_df[out_cols].copy()
            out_df.to_csv(str(csv_path), index=False)
            print(f"[Phase 3] {cfg['label']} {date_str}: {len(out_df)} events â†' {csv_path.name}")
            event_csvs.append(csv_path)

        results[site_name] = event_csvs

    return results


# ------------------------------------------------------------------------------
# PHASE 4  --  Run XOS simulation + Kempower MILP
# ------------------------------------------------------------------------------

def phase4_run_analysis(event_csvs: dict[str, list[Path]]) -> dict[str, list[dict]]:
    """
    For each site, run XOS and Kempower on each event CSV.
    Returns {site_name: [result_dict, ...]}.
    """
    xos = importlib.import_module("xos_hub_soc_simulation")
    kmp = importlib.import_module("kempower_milp_sizing")
    milp, kempower_specs = kmp._load_modules()

    all_results: dict[str, list[dict]] = {}

    for site_name, csvs in event_csvs.items():
        cfg = SITES[site_name]
        site_results: list[dict] = []
        print(f"\n[Phase 4] {cfg['label']}  --  {len(csvs)} event CSV(s)")

        for csv_path in csvs:
            date_tag = csv_path.stem.split("_events_")[-1]  # e.g. northgate_2025_06_30
            date_str = date_tag.replace(f"{site_name.replace('_','')}_", "").replace("_", "-")
            if len(date_str.split("-")) == 4:
                # handle northgate_ prefix: northgate_2025_06_30 -> 2025-06-30
                parts = date_tag.replace(f"{site_name.replace('_','')}_", "")
                date_str = parts.replace("_", "-")

            xos_out  = OUT_DIR / site_name / f"xos_{date_tag}"
            kmp_out  = OUT_DIR / site_name / f"kempower_{date_tag}"
            xos_out.mkdir(parents=True, exist_ok=True)
            kmp_out.mkdir(parents=True, exist_ok=True)

            # -- XOS simulation -----------------------------------------------
            xos_summary_txt = xos_out / f"xos_sim_summary_{date_tag}.txt"
            if xos_summary_txt.exists():
                print(f"  [XOS] {date_tag}: summary exists -- reading")
                xos_result = _read_xos_summary(xos_summary_txt, csv_path)
            else:
                print(f"  [XOS] {date_tag}: running simulation ...")
                try:
                    events_df = xos.load_events(csv_path)
                    p_eff     = xos.compute_p_eff(events_df)
                    n_units, result = xos.find_min_xos_units(events_df, p_eff)
                    cost_summary   = xos.compute_unit_cost_summary(n_units)
                    xos.export_results(events_df, n_units, result, cost_summary,
                                       xos_out, label=date_tag)
                    xos_result = {
                        "site":     cfg["label"],
                        "date":     date_tag,
                        "xos_units": n_units,
                        "xos_served_pct": result.get("pct_served", float("nan")),
                        "xos_lifecycle_low":  cost_summary["lifecycle_low"]  * n_units,
                        "xos_lifecycle_high": cost_summary["lifecycle_high"] * n_units,
                        "xos_daily_cost":     cost_summary["daily_cost_low"] * n_units,
                    }
                except Exception as exc:
                    print(f"    [XOS] ERROR: {exc}")
                    xos_result = {"site": cfg["label"], "date": date_tag,
                                  "xos_units": None, "xos_served_pct": None,
                                  "xos_lifecycle_low": None, "xos_lifecycle_high": None,
                                  "xos_daily_cost": None}

            # -- Kempower MILP ------------------------------------------------
            kmp_summary_csv = kmp_out / "kempower_lifecycle_costs.csv"
            if kmp_summary_csv.exists():
                print(f"  [KMP] {date_tag}: summary exists -- reading")
                kmp_result = _read_kmp_summary(kmp_summary_csv, cfg["label"], date_tag)
            else:
                print(f"  [KMP] {date_tag}: running MILP ...")
                try:
                    milp.GUROBI_MIP_GAP   = 0.06   # 6% tolerance avoids indefinite solves
                    milp.GUROBI_TIME_LIMIT = 480   # 8-min per day max
                    r = kmp._run_one(milp, kempower_specs, csv_path, kmp_out, label=date_tag)
                    kmp_result = _read_kmp_summary(kmp_summary_csv, cfg["label"], date_tag) if r else {
                        "site": cfg["label"], "date": date_tag,
                        "kmp_mix": "failed", "kmp_lifecycle_low": None,
                        "kmp_lifecycle_high": None, "kmp_daily_cost": None,
                    }
                except Exception as exc:
                    print(f"    [KMP] ERROR: {exc}")
                    kmp_result = {"site": cfg["label"], "date": date_tag,
                                  "kmp_mix": "error", "kmp_lifecycle_low": None,
                                  "kmp_lifecycle_high": None, "kmp_daily_cost": None}

            merged = {**xos_result, **{k: v for k, v in kmp_result.items()
                                        if k not in xos_result}}
            site_results.append(merged)

        all_results[site_name] = site_results

    return all_results


def _read_xos_summary(txt_path: Path, csv_path: Path) -> dict:
    """Parse xos_sim_summary.txt into a result dict."""
    text = txt_path.read_text(encoding="utf-8", errors="replace")
    import re
    n_units  = int(re.search(r"Units deployed.*?(\d+)", text).group(1)) if re.search(r"Units deployed.*?(\d+)", text) else None
    date_tag = csv_path.stem.split("_events_")[-1]
    lc_low   = lc_high = daily = None
    m = re.search(r"Lifecycle.*?low.*?\$([0-9,]+)", text, re.IGNORECASE)
    if m:
        lc_low = float(m.group(1).replace(",", ""))
    m = re.search(r"Lifecycle.*?high.*?\$([0-9,]+)", text, re.IGNORECASE)
    if m:
        lc_high = float(m.group(1).replace(",", ""))
    m = re.search(r"Daily.*?\$([0-9,.]+)", text, re.IGNORECASE)
    if m:
        daily = float(m.group(1).replace(",", ""))
    return {"date": date_tag, "xos_units": n_units,
            "xos_served_pct": None,
            "xos_lifecycle_low": lc_low, "xos_lifecycle_high": lc_high,
            "xos_daily_cost": daily}


def _read_kmp_summary(csv_path: Path, site_label: str, date_tag: str) -> dict:
    """Read kempower_lifecycle_costs.csv and build result dict."""
    try:
        df  = pd.read_csv(str(csv_path))
        mix_parts = [
            f"{int(r['count'])}Ã—{r['charger_type'].replace('Kempower_','')}"
            for _, r in df.iterrows() if int(r.get("count", 0)) > 0
        ]
        mix       = " + ".join(mix_parts) if mix_parts else "0 chargers"
        lc_low    = df["lifecycle_fleet_low"].sum()
        lc_high   = df["lifecycle_fleet_high"].sum()
        life_yr   = df["life_years"].max() if "life_years" in df else 8
        daily     = (lc_low / life_yr) / 365
        return {"date": date_tag, "kmp_mix": mix,
                "kmp_lifecycle_low": lc_low, "kmp_lifecycle_high": lc_high,
                "kmp_daily_cost": daily}
    except Exception:
        return {"date": date_tag, "kmp_mix": "n/a",
                "kmp_lifecycle_low": None, "kmp_lifecycle_high": None,
                "kmp_daily_cost": None}


# ------------------------------------------------------------------------------
# PHASE 5  --  Compile summary
# ------------------------------------------------------------------------------

def phase5_compile_summary(all_results: dict[str, list[dict]]) -> pd.DataFrame:
    """Compile all site results into one summary DataFrame and print."""
    rows = []
    for site_name, site_results in all_results.items():
        if not site_results:
            continue
        cfg = SITES[site_name]
        for r in site_results:
            rows.append({
                "Site":                cfg["label"],
                "Date":                r.get("date", ""),
                "XOS Units":           r.get("xos_units", ""),
                "XOS Lifecycle Low":   f"${r['xos_lifecycle_low']:,.0f}" if r.get("xos_lifecycle_low") else "n/a",
                "XOS Lifecycle High":  f"${r['xos_lifecycle_high']:,.0f}" if r.get("xos_lifecycle_high") else "n/a",
                "XOS Daily Cost":      f"${r['xos_daily_cost']:,.0f}" if r.get("xos_daily_cost") else "n/a",
                "Kempower Mix":        r.get("kmp_mix", ""),
                "KMP Lifecycle Low":   f"${r['kmp_lifecycle_low']:,.0f}" if r.get("kmp_lifecycle_low") else "n/a",
                "KMP Lifecycle High":  f"${r['kmp_lifecycle_high']:,.0f}" if r.get("kmp_lifecycle_high") else "n/a",
                "KMP Daily Cost":      f"${r['kmp_daily_cost']:,.0f}" if r.get("kmp_daily_cost") else "n/a",
            })

    summary_df = pd.DataFrame(rows)
    out_csv = OUT_DIR / "all_sites_sizing_summary.csv"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_df.to_csv(str(out_csv), index=False)

    print("\n" + "=" * 100)
    print("  MULTI-SITE SIZING SUMMARY")
    print("=" * 100)
    if not summary_df.empty:
        print(summary_df.to_string(index=False))
    print("=" * 100)
    print(f"\nSaved: {out_csv}")
    return summary_df


# ------------------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-site DCFC sizing pipeline")
    parser.add_argument("--top",   type=int, default=5,  help="Top N days per site (default 5)")
    parser.add_argument("--phase", type=int, default=0,  help="Run only this phase (0=all)")
    parser.add_argument("--force-cache", action="store_true", help="Rebuild all site caches")
    parser.add_argument("--sites", nargs="+",
                        choices=["northgate","fresno","glendale","san_diego"],
                        default=None, help="Limit to specific sites (default: all)")
    args = parser.parse_args()

    run_all = (args.phase == 0)

    # Filter SITES dict if --sites specified
    active_sites = args.sites or list(SITES.keys())
    for k in list(SITES.keys()):
        if k not in active_sites:
            del SITES[k]

    print("=" * 70)
    print("  MULTI-SITE DCFC SIZING PIPELINE")
    print(f"  Sites: {', '.join(cfg['label'] for cfg in SITES.values())}")
    print(f"  Top-N: {args.top} days per site")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    if run_all or args.phase == 1:
        phase1_build_caches(force=args.force_cache)

    ranked: dict[str, pd.DataFrame] = {}
    if run_all or args.phase == 2:
        ranked = phase2_screen_days(top_n=args.top)
    else:
        # Load existing ranked day CSVs for downstream phases
        for site_name, cfg in SITES.items():
            ranked_csv = OUT_DIR / site_name / f"{site_name}_ranked_days.csv"
            if ranked_csv.exists():
                ranked[site_name] = pd.read_csv(ranked_csv)

    event_csvs: dict[str, list[Path]] = {}
    if run_all or args.phase == 3:
        event_csvs = phase3_extract_events(ranked, top_n=args.top)
    else:
        for site_name, cfg in SITES.items():
            if site_name == "northgate":
                event_csvs["northgate"] = sorted(
                    BASE_DIR.glob("z2z_milp_events_northgate_*.csv")
                )
            else:
                site_out = OUT_DIR / site_name
                event_csvs[site_name] = sorted(site_out.glob(f"z2z_milp_events_*_*.csv"))

    all_results: dict[str, list[dict]] = {}
    if run_all or args.phase == 4:
        all_results = phase4_run_analysis(event_csvs)

    if run_all or args.phase == 5:
        if not all_results:
            print("[Phase 5] No analysis results to compile  --  run phase 4 first")
        else:
            phase5_compile_summary(all_results)

    print(f"\nDone: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()

