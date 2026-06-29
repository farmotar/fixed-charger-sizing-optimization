"""
batch_extract_northgate.py
──────────────────────────
Reads _northgate_z2z_cache.csv and generates one per-day MILP event CSV
for EVERY unique operating day (Pacific time), using the same extraction
logic as extract_z2z_events.py.

Existing z2z_milp_events_northgate_YYYY_MM_DD.csv files are SKIPPED
unless --overwrite is passed, so the 31 already-processed days are
preserved as-is.

Usage:
    python batch_extract_northgate.py              # skip existing
    python batch_extract_northgate.py --overwrite  # regenerate all
"""
from __future__ import annotations

import sys, re, difflib, argparse
from pathlib import Path

import openpyxl
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
CACHE_CSV = BASE_DIR / "_northgate_z2z_cache.csv"
EV_CATEGORIES_XLSX = Path(r"D:\Geotab_EV_Parameters\final_categories.xlsx")
CHARGE_RATE_XLSX   = BASE_DIR / "ev_equivalent_max_charge_power_mapping_filled.xlsx"

# ── Extraction parameters (same as extract_z2z_events.py) ──────────────────
MIN_DWELL_H    = 1.0
MIN_ENERGY_KWH = 0.10
TARGET_SOC     = 100.0
SOC_FALLBACK   = 50.0    # % arrival SOC when trip distance is 0
ETA            = 0.90    # charger efficiency for individual feasibility check

EV_SPEC_OVERRIDES: dict[str, tuple[float, float]] = {
    "Ford F-150 Lightning":             (131.0,  0.4814),
    "Freightliner eCascadia":           (438.0,  2.10),
    "Freightliner eM2":                 (315.0,  1.164),
    "Tesla Model 3":                    (82.0,   0.259),
    "Rivian R1T":                       (135.0,  0.427),
    "Rivian R1S":                       (135.0,  0.427),
    "GMC Hummer EV":                    (212.7,  0.640),
    "BYD 6F Cab-Forward Truck":         (183.0,  1.540),
    "Chevrolet Silverado EV WT":        (200.0,  0.350),
    "Chevrolet Bolt EV":                (65.0,   0.281),
    "Kia EV6":                          (77.5,   0.288),
    "Blue Arc EV":                      (158.0,  1.000),
    "Ram ProMaster EV (cargo)":         (110.0,  0.671),
    "Ford E-Transit":                   (89.0,   0.560),
    "Volkswagen ID.4":                  (77.0,   0.347),
    "Volkswagen ID. Buzz":              (91.0,   0.406),
    "Volkswagen ID. Buzz (passenger)":  (91.0,   0.406),
    "Volvo VNR 4X2 Electric":           (375.0,  1.630),
    "Global Electric Street Sweeper (M4E)": (210.0, 4.421),
}

EV_DIRECT_PATTERNS: list[tuple[str, str]] = [
    ("tesla model 3",          "Tesla Model 3"),
    ("silverado ev",           "Chevrolet Silverado EV WT"),
    ("f-150 lightning",        "Ford F-150 Lightning"),
    ("ecascadia",              "Freightliner eCascadia"),
    ("em2",                    "Freightliner eM2"),
    ("rivian r1t",             "Rivian R1T"),
    ("rivian r1s",             "Rivian R1S"),
    ("hummer ev",              "GMC Hummer EV"),
    ("bolt ev",                "Chevrolet Bolt EV"),
    ("kia ev6",                "Kia EV6"),
    ("promaster ev",           "Ram ProMaster EV (cargo)"),
    ("e-transit",              "Ford E-Transit"),
    ("volkswagen id.4",        "Volkswagen ID.4"),
    ("volkswagen id. buzz",    "Volkswagen ID. Buzz"),
    ("id.4",                   "Volkswagen ID.4"),
    ("id. buzz",               "Volkswagen ID. Buzz"),
    ("blue arc",               "Blue Arc EV"),
    ("volvo vnr",              "Volvo VNR 4X2 Electric"),
    ("global electric sweeper","Global Electric Street Sweeper (M4E)"),
]

EXTRA_ICE_OVERRIDES: dict[str, str] = {
    "international hv":       "Freightliner eCascadia",
    "international hx":       "Freightliner eCascadia",
    "international workstar": "Freightliner eCascadia",
    "international paystar":  "Freightliner eCascadia",
    "western star":           "Volvo VNR 4X2 Electric",
    "freightliner 114 sd":    "Freightliner eCascadia",
    "freightliner m2":        "Freightliner eM2",
    "international durastar": "Freightliner eM2",
    "ford f-250":             "Ford F-150 Lightning",
    "ford f-350":             "GMC Hummer EV",
    "ford f-450":             "GMC Hummer EV",
    "ford f-550":             "GMC Hummer EV",
    "ford f-650":             "BYD 6F Cab-Forward Truck",
    "chevrolet tahoe":        "Rivian R1S",
    "nissan frontier":        "Rivian R1T",
    "ram 3500":               "GMC Hummer EV",
    "ram promaster":          "Ram ProMaster EV (cargo)",
}

DC_FALLBACK: dict[str, float] = {"Global Electric Street Sweeper (M4E)": 60.0}
AC_FALLBACK: dict[str, float] = {"Global Electric Street Sweeper (M4E)": 0.0}


# ── EV matching helpers (copied from extract_z2z_events.py) ────────────────
def _load_ev_equivalencies(xlsx: Path) -> dict[str, str]:
    wb = openpyxl.load_workbook(str(xlsx), read_only=True, data_only=True)
    ws = wb["EV Equivalencies"]
    rows = list(ws.iter_rows(values_only=True))
    SKIP = {
        "ice example","iceexample","equivalent ev","category",
        "mpge city","mpge hwy","mpge comb","battery (kwh)","battery (kw)",
        "range (mi)","energy consumption at gvwr (kwh/mi)",
        "energy consumption (kwh/mi)","sweeping speed (mph)","sweeping time (h)",
    }
    out: dict[str, str] = {}
    for row in rows:
        c1 = row[1] if len(row) > 1 else None
        c2 = row[2] if len(row) > 2 else None
        if not isinstance(c1, str) or not isinstance(c2, str):
            continue
        k = c1.strip().lower()
        if k in SKIP or not k or not c2.strip():
            continue
        out[k] = c2.strip()
    wb.close()
    return out


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _match_ev(make: str, model: str, ice_to_ev: dict[str, str]) -> str | None:
    make  = (make  or "").strip()
    model = (model or "").strip()
    if not make and not model:
        return None
    combo   = _normalize(f"{make} {model}")
    model_n = _normalize(model)
    for pattern, ev_name in EV_DIRECT_PATTERNS:
        if pattern in combo or pattern in model_n:
            return ev_name
    for pattern, ev_name in EXTRA_ICE_OVERRIDES.items():
        if pattern in combo or pattern in model_n:
            return ev_name
    if combo in ice_to_ev:
        return ice_to_ev[combo]
    if model_n in ice_to_ev:
        return ice_to_ev[model_n]
    for key, ev_name in ice_to_ev.items():
        if combo.startswith(key) or key.startswith(combo):
            return ev_name
        if model_n.startswith(key) or key.startswith(model_n):
            return ev_name
    close = difflib.get_close_matches(combo, list(ice_to_ev), n=1, cutoff=0.60)
    if not close:
        close = difflib.get_close_matches(model_n, list(ice_to_ev), n=1, cutoff=0.60)
    return ice_to_ev[close[0]] if close else None


def extract_day(day_df: pd.DataFrame, date_str: str, charge_map: dict,
                ice_to_ev: dict) -> pd.DataFrame | None:
    """Extract and enrich one day's events. Returns output DataFrame or None."""

    # EV classification (per unique vehicle)
    unique_v = (day_df[["vehicle_name","make","model","year"]]
                .drop_duplicates("vehicle_name")
                .fillna({"make":"","model":"","year":""}))
    matches = []
    for _, r in unique_v.iterrows():
        ev = _match_ev(str(r["make"]), str(r["model"]), ice_to_ev)
        matches.append({"vehicle_name": r["vehicle_name"], "ev_equivalent_model": ev})
    match_df = pd.DataFrame(matches)
    ev_df = match_df[match_df["ev_equivalent_model"].notna()]
    df = day_df.merge(ev_df[["vehicle_name","ev_equivalent_model"]],
                      on="vehicle_name", how="inner")
    if df.empty:
        return None

    # Dwell — apply min-1h floor
    df = df.copy()
    df["dwell_hours_actual"] = df["to_dwell_minutes"].fillna(0) / 60.0
    df["dwell_hours"] = df["dwell_hours_actual"].clip(lower=MIN_DWELL_H)
    short = df["dwell_hours_actual"] < MIN_DWELL_H
    df.loc[short, "to_exit_time"] = (
        df.loc[short, "to_entry_time"] + pd.to_timedelta(MIN_DWELL_H, unit="h")
    )

    # EV specs
    def get_spec(ev, idx):  # idx 0=battery, 1=efficiency
        s = EV_SPEC_OVERRIDES.get(ev)
        return s[idx] if s else float("nan")

    def get_ac(ev):
        cr = charge_map.get(ev)
        return float(cr["max_ac_charge_kw"]) if cr else AC_FALLBACK.get(ev, 0.0)

    def get_dc(ev):
        cr = charge_map.get(ev)
        return float(cr["max_dc_charge_kw"]) if cr else DC_FALLBACK.get(ev, 50.0)

    df["battery_capacity_kwh"]    = df["ev_equivalent_model"].map(lambda e: get_spec(e, 0))
    df["efficiency_kwh_per_mile"] = df["ev_equivalent_model"].map(lambda e: get_spec(e, 1))
    df["max_ac_charge_kw"]        = df["ev_equivalent_model"].map(get_ac)
    df["max_dc_charge_kw"]        = df["ev_equivalent_model"].map(get_dc)
    df = df[df["battery_capacity_kwh"].notna()].copy()
    if df.empty:
        return None

    # Energy needed
    dist = df["trip_first_distance_miles_between"].fillna(0).clip(lower=0)
    energy_used = dist * df["efficiency_kwh_per_mile"]
    arrival_soc = (SOC_FALLBACK - (energy_used / df["battery_capacity_kwh"] * 100.0)
                   ).clip(lower=0.0, upper=100.0)
    df["assumed_initial_soc_percent"] = arrival_soc.round(2)
    df["target_soc_percent"]          = TARGET_SOC
    df["energy_needed_kwh_for_visit"] = (
        (TARGET_SOC - arrival_soc) / 100.0 * df["battery_capacity_kwh"]
    ).clip(lower=0.0).round(3)

    # Feasibility
    max_del = ETA * df["max_dc_charge_kw"].clip(upper=350) * df["dwell_hours"]
    df["individually_feasible"] = (
        df["energy_needed_kwh_for_visit"] <= max_del + MIN_ENERGY_KWH
    )

    # Keep serviceable events
    svc = df[
        df["individually_feasible"] &
        (df["energy_needed_kwh_for_visit"] >= MIN_ENERGY_KWH) &
        df["energy_needed_kwh_for_visit"].notna()
    ].copy().reset_index(drop=True)
    if svc.empty:
        return None

    # Sort and assign IDs
    svc = svc.sort_values("to_entry_time").reset_index(drop=True)
    date_tag = date_str.replace("-", "")
    svc["charging_event_id"] = [f"z2z_{date_tag}_v{i+1:02d}" for i in range(len(svc))]

    # Fix missing exit times
    t_full_h = (svc["energy_needed_kwh_for_visit"] /
                (ETA * svc["max_dc_charge_kw"].clip(upper=350))).clip(lower=0)
    derived_dwell = svc[["dwell_hours_actual"]].assign(t=t_full_h).max(axis=1)
    missing_exit = svc["to_exit_time"].isna()
    if missing_exit.any():
        svc.loc[missing_exit, "to_exit_time"] = (
            svc.loc[missing_exit, "to_entry_time"] +
            pd.to_timedelta(derived_dwell[missing_exit], unit="h")
        )
        svc.loc[missing_exit, "dwell_hours"]        = derived_dwell[missing_exit]
        svc.loc[missing_exit, "dwell_hours_actual"] = derived_dwell[missing_exit]

    svc["arrival_time"]   = svc["to_entry_time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    svc["departure_time"] = svc["to_exit_time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    out_cols = [
        "charging_event_id", "vehicle_name", "arrival_time", "departure_time",
        "dwell_hours", "dwell_hours_actual", "energy_needed_kwh_for_visit",
        "max_ac_charge_kw", "max_dc_charge_kw", "ev_equivalent_model",
        "individually_feasible", "battery_capacity_kwh",
        "assumed_initial_soc_percent", "target_soc_percent",
    ]
    return svc[out_cols].rename(columns={"vehicle_name": "vehicle_id"})


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--overwrite", action="store_true",
                        help="Regenerate even if CSV already exists")
    args = parser.parse_args()

    print(f"\n{'='*70}")
    print("  BATCH Z2Z EXTRACTION — Northgate, all days")
    print(f"{'='*70}")

    # Load support tables once
    print("\nLoading EV equivalencies …")
    ice_to_ev = _load_ev_equivalencies(EV_CATEGORIES_XLSX)
    print(f"  {len(ice_to_ev)} ICE→EV pairs")

    print("Loading charge rates …")
    cr_df = pd.read_excel(str(CHARGE_RATE_XLSX),
                          usecols=["ev_equivalent_model","max_ac_charge_kw","max_dc_charge_kw"])
    charge_map = (cr_df.drop_duplicates("ev_equivalent_model")
                  .set_index("ev_equivalent_model").to_dict("index"))
    print(f"  {len(charge_map)} EV models with charge rates")

    # Load cache
    print(f"\nLoading {CACHE_CSV.name} …")
    z2z = pd.read_csv(str(CACHE_CSV), low_memory=False)
    z2z = z2z[z2z["use_for_optimization_bool"].fillna(False).astype(bool)].copy()
    z2z["to_entry_time"] = pd.to_datetime(z2z["to_entry_time"], utc=True, errors="coerce")
    z2z["to_exit_time"]  = pd.to_datetime(z2z["to_exit_time"],  utc=True, errors="coerce")
    z2z = z2z.dropna(subset=["to_entry_time"])
    z2z["_date_pacific"] = (z2z["to_entry_time"]
                            .dt.tz_convert("America/Los_Angeles")
                            .dt.strftime("%Y-%m-%d"))

    all_dates = sorted(z2z["_date_pacific"].unique())
    print(f"  {len(z2z):,} events across {len(all_dates)} unique Pacific dates")
    print(f"  Range: {all_dates[0]} → {all_dates[-1]}")

    # Count existing CSVs
    existing = set(p.stem for p in BASE_DIR.glob("z2z_milp_events_northgate_*.csv"))
    print(f"\n  Existing per-day CSVs : {len(existing)}")
    print(f"  Mode                  : {'overwrite all' if args.overwrite else 'skip existing'}")

    # Process each day
    skipped = generated = failed = 0
    print(f"\n{'─'*70}")
    for date_str in all_dates:
        date_tag   = date_str.replace("-", "_")
        stem       = f"z2z_milp_events_northgate_{date_tag}"
        out_path   = BASE_DIR / f"{stem}.csv"

        if out_path.exists() and not args.overwrite:
            skipped += 1
            continue

        day_df = z2z[z2z["_date_pacific"] == date_str].copy()
        out_df = extract_day(day_df, date_str, charge_map, ice_to_ev)

        if out_df is None or out_df.empty:
            print(f"  {date_str}  — 0 serviceable events, skipping")
            failed += 1
            continue

        out_df.to_csv(str(out_path), index=False)
        print(f"  {date_str}  — {len(out_df):>3} events  → {out_path.name}")
        generated += 1

    print(f"\n{'='*70}")
    print(f"  Done.")
    print(f"  Generated : {generated} new CSVs")
    print(f"  Skipped   : {skipped} already existed")
    print(f"  Empty/fail: {failed} days with no serviceable events")
    total = len([p for p in BASE_DIR.glob("z2z_milp_events_northgate_*.csv")])
    print(f"  Total CSVs now on disk: {total}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
