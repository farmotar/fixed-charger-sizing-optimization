"""
extract_z2z_events.py
---------------------
Build MILP charging-event input from the Geotab Zone-to-Zone dataset.

Replaces the hand-crafted northgate_2025_06_30_min1h_events.csv by:
  1. Filtering Z2Z trips where the DESTINATION is the target depot zone on the target date.
  2. Mapping each vehicle's ICE make/model to an EV equivalent via the EV Equivalencies
     sheet in final_categories.xlsx (same logic as fleet-electrification planning).
     Vehicles that are already EVs match the "Equivalent EV" column directly.
  3. Estimating energy needed from trip distance × efficiency (kWh/mi).
  4. Applying the same min-1h dwell extension and feasibility logic as extract_june30_min1h.py.
  5. Writing a 14-column CSV compatible with exact_northgate_charger_sizing_milp.py.

Configure TARGET_ZONE, TARGET_DATE, OUTPUT_CSV at the top of this file.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

import openpyxl
import pandas as pd

# ── Configuration ──────────────────────────────────────────────────────────────
TARGET_ZONE    = "Northgate"          # substring match on to_zone (case-insensitive)
TARGET_DATE    = "2025-06-30"         # Pacific calendar date to filter (YYYY-MM-DD)
MIN_DWELL_H    = 1.0                  # minimum charging window (hours); dwell is extended to this
MIN_ENERGY_KWH = 0.10                 # discard events needing < this kWh
TARGET_SOC     = 100.0                # target SOC % at departure
SOC_FALLBACK   = 50.0                 # % arrival SOC when trip distance is 0 or missing
ETA            = 0.90                 # charger efficiency

Z2Z_CSV = Path(
    r"D:\Geotab_EV_Parameters\charger_sizing_test"
    r"\Geotab_Zone_to_Zone_Dataset\Geotab_Zone_to_Zone_Dataset"
    r"\01_Final_Dataset\final_zone_to_zone_all_rows.csv"
)
# Set to True to restrict to rows flagged use_for_optimization_bool.
# False = use all rows in the dataset (needed when reading final_zone_to_zone_all_rows.csv).
FILTER_USE_FOR_OPT = True
EV_CATEGORIES_XLSX = Path(r"D:\Geotab_EV_Parameters\final_categories.xlsx")
CHARGE_RATE_XLSX   = Path(
    r"D:\Geotab_EV_Parameters\charger_sizing_test"
    r"\ev_equivalent_max_charge_power_mapping_filled.xlsx"
)
OUTPUT_CSV = Path(
    r"D:\Geotab_EV_Parameters\charger_sizing_test"
    r"\z2z_milp_events_northgate_2025_06_30.csv"
)

# ── EV specs: battery (kWh) and efficiency (kWh/mile) ──────────────────────────
# Values validated against existing northgate_2025_06_30_min1h_events.csv.
# Override or extend here for new EV models; ac/dc charge rates come from
# ev_equivalent_max_charge_power_mapping_filled.xlsx.
EV_SPEC_OVERRIDES: dict[str, tuple[float, float]] = {
    # model_canonical                   battery_kwh  efficiency_kwh_per_mile
    "Ford F-150 Lightning":             (131.0,      0.4814),
    "Freightliner eCascadia":           (438.0,      2.10),
    "Freightliner eM2":                 (315.0,      1.164),
    "Tesla Model 3":                    (82.0,       0.259),
    "Rivian R1T":                       (135.0,      0.427),
    "Rivian R1S":                       (135.0,      0.427),
    "GMC Hummer EV":                    (212.7,      0.640),
    "BYD 6F Cab-Forward Truck":         (183.0,      1.540),
    "Chevrolet Silverado EV WT":        (200.0,      0.350),
    "Chevrolet Bolt EV":                (65.0,       0.281),
    "Kia EV6":                          (77.5,       0.288),
    "Blue Arc EV":                      (158.0,      1.000),
    "Ram ProMaster EV (cargo)":         (110.0,      0.671),
    "Ford E-Transit":                   (89.0,       0.560),
    "Volkswagen ID.4":                  (77.0,       0.347),
    "Volkswagen ID. Buzz":              (91.0,       0.406),
    "Volkswagen ID. Buzz (passenger)":  (91.0,       0.406),
    "Volvo VNR 4X2 Electric":           (375.0,      1.630),
    "Global Electric Street Sweeper (M4E)": (210.0, 4.421),
}

# Extra patterns that map substring of Z2Z model/make -> canonical EV model name.
# Used BEFORE the EV Equivalencies sheet lookup.
# Tier 1: vehicles already deployed as EVs in the fleet.
EV_DIRECT_PATTERNS: list[tuple[str, str]] = [
    # (pattern_lower, canonical_ev_name)  — checked against "make model" lower
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
    ("global electric sweeper", "Global Electric Street Sweeper (M4E)"),
]

# Tier 2 extra overrides for ICE models that need special handling beyond
# what the EV Equivalencies sheet provides (e.g., vehicle classes not in sheet).
EXTRA_ICE_OVERRIDES: dict[str, str] = {
    # pattern (substring of "make model" lower) -> canonical EV model name
    "international hv":        "Freightliner eCascadia",
    "international hx":        "Freightliner eCascadia",
    "international workstar":  "Freightliner eCascadia",
    "international paystar":   "Freightliner eCascadia",
    "western star":            "Volvo VNR 4X2 Electric",
    "freightliner 114 sd":     "Freightliner eCascadia",
    "freightliner m2":         "Freightliner eM2",
    "international durastar":  "Freightliner eM2",
    "ford f-250":              "Ford F-150 Lightning",
    "ford f-350":              "GMC Hummer EV",
    "ford f-450":              "GMC Hummer EV",
    "ford f-550":              "GMC Hummer EV",
    "ford f-650":              "BYD 6F Cab-Forward Truck",
    "chevrolet tahoe":         "Rivian R1S",
    "nissan frontier":         "Rivian R1T",
    "ram 3500":                "GMC Hummer EV",
    "ram promaster":           "Ram ProMaster EV (cargo)",
}


# ── Step 1: Load EV Equivalencies sheet ───────────────────────────────────────

def _load_ev_equivalencies(xlsx: Path) -> dict[str, str]:
    """Return {ice_model_lower: canonical_ev_name} from 'EV Equivalencies' sheet."""
    wb = openpyxl.load_workbook(str(xlsx), read_only=True, data_only=True)
    ws = wb["EV Equivalencies"]
    rows = list(ws.iter_rows(values_only=True))

    SKIP_STRINGS = {
        "ice example", "iceexample", "equivalent ev", "category",
        "mpge city", "mpge hwy", "mpge comb", "battery (kwh)", "battery (kw)",
        "range (mi)", "energy consumption at gvwr (kwh/mi)",
        "energy consumption (kwh/mi)", "energy consumption at gvwr (kwh/mi)",
        "sweeping speed (mph)", "sweeping time (h)",
        "mpge city", "mpge hwy", "mpge comb", "range (mi)",
    }

    ice_to_ev: dict[str, str] = {}
    for row in rows:
        col1 = row[1] if len(row) > 1 else None
        col2 = row[2] if len(row) > 2 else None
        if not isinstance(col1, str) or not isinstance(col2, str):
            continue
        c1 = col1.strip().lower()
        c2 = col2.strip()
        if c1 in SKIP_STRINGS or not c1 or not c2:
            continue
        ice_to_ev[c1] = c2

    wb.close()
    return ice_to_ev


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _match_ev(make: str, model: str, ice_to_ev: dict[str, str]) -> str | None:
    """
    Return canonical EV model name for a Z2Z vehicle, or None if no match.

    Matching order:
      1. EV_DIRECT_PATTERNS    — vehicle is already an EV
      2. EXTRA_ICE_OVERRIDES   — hardcoded ICE -> EV overrides
      3. EV Equivalencies sheet lookup (exact then startswith)
      4. difflib fuzzy match against sheet ICE keys (threshold 0.60)
    """
    make  = (make  or "").strip()
    model = (model or "").strip()
    # Cannot classify a vehicle with no make and no model
    if not make and not model:
        return None

    combo = _normalize(f"{make} {model}")
    model_n = _normalize(model)

    # Tier 1: already an EV
    for pattern, ev_name in EV_DIRECT_PATTERNS:
        if pattern in combo or pattern in model_n:
            return ev_name

    # Tier 2: extra ICE overrides
    for pattern, ev_name in EXTRA_ICE_OVERRIDES.items():
        if pattern in combo or pattern in model_n:
            return ev_name

    # Tier 3: EV Equivalencies sheet (exact)
    if combo in ice_to_ev:
        return ice_to_ev[combo]
    if model_n in ice_to_ev:
        return ice_to_ev[model_n]

    # startswith or endswith match
    for key, ev_name in ice_to_ev.items():
        if combo.startswith(key) or key.startswith(combo):
            return ev_name
        if model_n.startswith(key) or key.startswith(model_n):
            return ev_name

    # Tier 4: fuzzy match against sheet ICE keys
    candidates = list(ice_to_ev.keys())
    close = difflib.get_close_matches(combo, candidates, n=1, cutoff=0.60)
    if not close:
        close = difflib.get_close_matches(model_n, candidates, n=1, cutoff=0.60)
    if close:
        return ice_to_ev[close[0]]

    return None


# ── Step 2: Load charge rates ──────────────────────────────────────────────────

def _load_charge_rates(xlsx: Path) -> pd.DataFrame:
    df = pd.read_excel(str(xlsx), usecols=["ev_equivalent_model", "max_ac_charge_kw", "max_dc_charge_kw"])
    return df.drop_duplicates(subset=["ev_equivalent_model"]).reset_index(drop=True)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 70)
    print(f"extract_z2z_events.py  |  zone={TARGET_ZONE!r}  date={TARGET_DATE}")
    print("=" * 70)

    # 1. EV Equivalencies sheet
    print(f"\n[1] Loading EV Equivalencies from {EV_CATEGORIES_XLSX.name} …")
    ice_to_ev = _load_ev_equivalencies(EV_CATEGORIES_XLSX)
    print(f"    {len(ice_to_ev)} ICE -> EV pairs loaded")

    # 2. Charge rates
    print(f"[2] Loading charge rates from {CHARGE_RATE_XLSX.name} …")
    charge_df = _load_charge_rates(CHARGE_RATE_XLSX)
    charge_map = charge_df.set_index("ev_equivalent_model").to_dict("index")

    # 3. Z2Z dataset
    print(f"[3] Loading Z2Z dataset …")
    z2z = pd.read_csv(
        str(Z2Z_CSV),
        usecols=[
            "vehicle_name", "make", "model", "year",
            "to_zone", "to_entry_time", "to_exit_time", "to_dwell_minutes",
            "trip_first_distance_miles_between",
            "use_for_optimization_bool",
        ],
        low_memory=False,
    )
    if FILTER_USE_FOR_OPT:
        z2z = z2z[z2z["use_for_optimization_bool"].fillna(False).astype(bool)].copy()
        print(f"    {len(z2z):,} rows after use_for_optimization_bool filter")
    else:
        print(f"    {len(z2z):,} total rows loaded (no use_for_optimization_bool filter)")

    # 4. Filter by depot zone
    zone_mask = z2z["to_zone"].str.contains(TARGET_ZONE, case=False, na=False)
    z2z = z2z[zone_mask].copy()
    print(f"    {len(z2z):,} rows with to_zone containing {TARGET_ZONE!r}")

    # 5. Parse timestamps and filter by date (Pacific)
    z2z["to_entry_time"] = pd.to_datetime(z2z["to_entry_time"], utc=True, errors="coerce")
    z2z["to_exit_time"]  = pd.to_datetime(z2z["to_exit_time"],  utc=True, errors="coerce")
    z2z["_date_pacific"] = (
        z2z["to_entry_time"]
        .dt.tz_convert("America/Los_Angeles")
        .dt.strftime("%Y-%m-%d")
    )
    z2z = z2z[z2z["_date_pacific"] == TARGET_DATE].copy()
    print(f"    {len(z2z):,} rows on Pacific date {TARGET_DATE}")

    if z2z.empty:
        print("  !! No rows found for target zone/date. Exiting.")
        return

    # 6. Classify vehicles -> EV model
    print("\n[4] Classifying vehicles …")
    unique_vehicles = (
        z2z[["vehicle_name", "make", "model", "year"]]
        .drop_duplicates(subset=["vehicle_name"])
        .fillna({"make": "", "model": "", "year": ""})
    )

    match_results: list[dict] = []
    for _, row in unique_vehicles.iterrows():
        ev_name = _match_ev(str(row["make"]), str(row["model"]), ice_to_ev)
        match_results.append(
            {
                "vehicle_name": row["vehicle_name"],
                "make": row["make"],
                "model": row["model"],
                "year": row["year"],
                "ev_equivalent_model": ev_name,
            }
        )

    match_df = pd.DataFrame(match_results)

    print("\n    Vehicle -> EV match table:")
    print(f"    {'vehicle_name':<14} {'make':<18} {'model':<30} {'ev_equivalent_model'}")
    print(f"    {'-'*14} {'-'*18} {'-'*30} {'-'*35}")
    for _, r in match_df.iterrows():
        flag = "" if r["ev_equivalent_model"] else "  <-- NO MATCH (excluded)"
        print(
            f"    {str(r['vehicle_name']):<14} {str(r['make']):<18} "
            f"{str(r['model']):<30} {str(r['ev_equivalent_model'])}{flag}"
        )

    # Filter to matched EVs only
    ev_vehicles = match_df[match_df["ev_equivalent_model"].notna()].copy()
    n_excluded  = len(match_df) - len(ev_vehicles)
    print(f"\n    Matched: {len(ev_vehicles)} vehicles | Excluded (no EV match): {n_excluded}")

    # Merge EV classification back into Z2Z rows
    z2z = z2z.merge(
        ev_vehicles[["vehicle_name", "ev_equivalent_model"]],
        on="vehicle_name",
        how="inner",
    )
    print(f"    Z2Z rows after EV filter: {len(z2z)}")

    # 7. Compute dwell and apply min-1h extension
    print("\n[5] Computing dwell and applying min-1h extension …")
    z2z["dwell_hours_actual"] = z2z["to_dwell_minutes"].fillna(0) / 60.0
    z2z["dwell_hours"] = z2z["dwell_hours_actual"].clip(lower=MIN_DWELL_H)

    # Extend exit time for short-dwell visits
    short_mask = z2z["dwell_hours_actual"] < MIN_DWELL_H
    z2z.loc[short_mask, "to_exit_time"] = (
        z2z.loc[short_mask, "to_entry_time"]
        + pd.to_timedelta(MIN_DWELL_H, unit="h")
    )
    print(f"    {short_mask.sum()} visits extended from <{MIN_DWELL_H}h to {MIN_DWELL_H}h")

    # 8. Join EV specs (battery, efficiency, charge rates)
    print("\n[6] Joining EV specs …")

    def get_battery(ev_model: str) -> float:
        spec = EV_SPEC_OVERRIDES.get(ev_model)
        return spec[0] if spec else float("nan")

    def get_efficiency(ev_model: str) -> float:
        spec = EV_SPEC_OVERRIDES.get(ev_model)
        return spec[1] if spec else float("nan")

    def get_ac(ev_model: str) -> float:
        cr = charge_map.get(ev_model)
        if cr:
            return float(cr["max_ac_charge_kw"])
        return AC_FALLBACK.get(ev_model, 0.0)

    # Fallback charge-rate values for models not in ev_equivalent_max_charge_power_mapping_filled.xlsx
    DC_FALLBACK: dict[str, float] = {
        "Global Electric Street Sweeper (M4E)": 60.0,
    }
    AC_FALLBACK: dict[str, float] = {
        "Global Electric Street Sweeper (M4E)": 0.0,
    }

    def get_dc(ev_model: str) -> float:
        cr = charge_map.get(ev_model)
        if cr:
            return float(cr["max_dc_charge_kw"])
        return DC_FALLBACK.get(ev_model, 50.0)  # conservative fallback if truly unknown

    z2z["battery_capacity_kwh"]   = z2z["ev_equivalent_model"].map(get_battery)
    z2z["efficiency_kwh_per_mile"] = z2z["ev_equivalent_model"].map(get_efficiency)
    z2z["max_ac_charge_kw"]        = z2z["ev_equivalent_model"].map(get_ac)
    z2z["max_dc_charge_kw"]        = z2z["ev_equivalent_model"].map(get_dc)

    missing_spec = z2z["battery_capacity_kwh"].isna().sum()
    if missing_spec:
        missing_models = z2z[z2z["battery_capacity_kwh"].isna()]["ev_equivalent_model"].unique()
        print(f"  !! {missing_spec} rows missing battery/efficiency spec for: {missing_models}")
        print("     Add these models to EV_SPEC_OVERRIDES at top of script.")
        z2z = z2z[z2z["battery_capacity_kwh"].notna()].copy()

    # Print spec table
    spec_summary = (
        z2z.groupby("ev_equivalent_model", sort=False)
        .agg(
            battery_kwh=("battery_capacity_kwh", "first"),
            eff_kwh_mi=("efficiency_kwh_per_mile", "first"),
            max_ac_kw=("max_ac_charge_kw", "first"),
            max_dc_kw=("max_dc_charge_kw", "first"),
            n_vehicles=("vehicle_name", "nunique"),
        )
        .reset_index()
    )
    print("\n    EV specs in use:")
    print(f"    {'model':<36} {'batt kWh':>9} {'eff kWh/mi':>11} {'AC kW':>6} {'DC kW':>6} {'#veh':>5}")
    print(f"    {'-'*36} {'-'*9} {'-'*11} {'-'*6} {'-'*6} {'-'*5}")
    for _, r in spec_summary.iterrows():
        print(
            f"    {r['ev_equivalent_model']:<36} {r['battery_kwh']:>9.1f} "
            f"{r['eff_kwh_mi']:>11.4f} {r['max_ac_kw']:>6.1f} {r['max_dc_kw']:>6.1f} {r['n_vehicles']:>5}"
        )

    # 9. Compute energy needed
    print("\n[7] Computing energy needed …")
    dist  = z2z["trip_first_distance_miles_between"].fillna(0).clip(lower=0)
    batt  = z2z["battery_capacity_kwh"]
    eff   = z2z["efficiency_kwh_per_mile"]

    # Departure SOC from previous zone is unknown -> assume SOC_FALLBACK (50%).
    # Subtract energy consumed on the trip to Northgate.
    # arrival_soc = 50% - (trip_distance × efficiency / battery × 100%)
    energy_used    = dist * eff
    arrival_soc_pct = (SOC_FALLBACK - (energy_used / batt * 100.0)).clip(lower=0.0, upper=100.0)

    zero_dist = (dist <= 0).sum()
    print(f"    {zero_dist} visits had zero/missing trip distance (arrival SOC = {SOC_FALLBACK}%)")

    energy_needed  = (TARGET_SOC - arrival_soc_pct) / 100.0 * batt
    energy_needed  = energy_needed.clip(lower=0.0)

    z2z["assumed_initial_soc_percent"]  = arrival_soc_pct.round(2)
    z2z["target_soc_percent"]           = TARGET_SOC
    z2z["energy_needed_kwh_for_visit"]  = energy_needed.round(3)

    # 10. Feasibility
    print("[8] Computing individually_feasible …")
    max_deliverable = ETA * z2z["max_dc_charge_kw"].clip(upper=350) * z2z["dwell_hours"]
    z2z["individually_feasible"] = z2z["energy_needed_kwh_for_visit"] <= max_deliverable + MIN_ENERGY_KWH

    # 11. Filter serviceable events
    svc = z2z[
        z2z["individually_feasible"]
        & (z2z["energy_needed_kwh_for_visit"] >= MIN_ENERGY_KWH)
        & z2z["energy_needed_kwh_for_visit"].notna()
    ].copy().reset_index(drop=True)

    infeasible = z2z[~z2z["individually_feasible"] & z2z["energy_needed_kwh_for_visit"].notna()]
    if len(infeasible):
        print(f"    {len(infeasible)} infeasible events (energy > deliverable at max DC rate × dwell):")
        for _, r in infeasible.iterrows():
            max_e = ETA * min(350, r["max_dc_charge_kw"]) * r["dwell_hours"]
            print(
                f"      {r['vehicle_name']}  {r['ev_equivalent_model']}  "
                f"need={r['energy_needed_kwh_for_visit']:.1f} kWh  "
                f"max_del={max_e:.1f} kWh  dwell={r['dwell_hours']:.2f}h"
            )

    print(f"    Serviceable events: {len(svc)}")

    if svc.empty:
        print("  !! No serviceable events. Check zone name and date. Exiting.")
        return

    # 12. Build output CSV
    print("\n[9] Building output …")
    svc = svc.sort_values("to_entry_time").reset_index(drop=True)
    date_tag = TARGET_DATE.replace("-", "")
    svc["charging_event_id"] = [f"z2z_{date_tag}_v{i+1:02d}" for i in range(len(svc))]

    # For vehicles with no recorded exit time (still on-site at end of horizon),
    # assume they stay until fully charged: departure = arrival + max(actual_dwell, t_needed).
    t_full_h = (
        svc["energy_needed_kwh_for_visit"] /
        (ETA * svc["max_dc_charge_kw"].clip(upper=350))
    ).clip(lower=0)
    derived_dwell = svc[["dwell_hours_actual"]].assign(t_full=t_full_h).max(axis=1)
    missing_exit = svc["to_exit_time"].isna()
    if missing_exit.any():
        n_fix = missing_exit.sum()
        svc.loc[missing_exit, "to_exit_time"] = (
            svc.loc[missing_exit, "to_entry_time"]
            + pd.to_timedelta(derived_dwell[missing_exit], unit="h")
        )
        svc.loc[missing_exit, "dwell_hours"]        = derived_dwell[missing_exit]
        svc.loc[missing_exit, "dwell_hours_actual"] = derived_dwell[missing_exit]
        print(
            f"    {n_fix} event(s) with no exit time: departure derived from "
            f"max(actual_dwell, t_full_charge) -> vehicle assumed fully charged"
        )

    svc["arrival_time"]   = svc["to_entry_time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    svc["departure_time"] = svc["to_exit_time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    out_cols = [
        "charging_event_id",
        "vehicle_name",           # -> vehicle_id
        "arrival_time",
        "departure_time",
        "dwell_hours",
        "dwell_hours_actual",
        "energy_needed_kwh_for_visit",
        "max_ac_charge_kw",
        "max_dc_charge_kw",
        "ev_equivalent_model",
        "individually_feasible",
        "battery_capacity_kwh",
        "assumed_initial_soc_percent",
        "target_soc_percent",
    ]
    df_out = svc[out_cols].rename(columns={"vehicle_name": "vehicle_id"})

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df_out.to_csv(str(OUTPUT_CSV), index=False)

    # 13. Summary
    print(f"\n{'='*70}")
    print(f"Output: {OUTPUT_CSV}  ({len(df_out)} events)")
    print(f"\nEvent summary:")
    print(
        f"  {'evt_id':<22} {'vehicle':<10} {'ev_model':<35} "
        f"{'soc_arr':>7} {'need_kWh':>9} {'dwell_h':>7} {'ext?':>5}"
    )
    print(f"  {'-'*22} {'-'*10} {'-'*35} {'-'*7} {'-'*9} {'-'*7} {'-'*5}")
    for _, r in df_out.iterrows():
        extended = "Y" if r["dwell_hours"] > r["dwell_hours_actual"] + 0.001 else ""
        print(
            f"  {r['charging_event_id']:<22} {str(r['vehicle_id']):<10} "
            f"{r['ev_equivalent_model']:<35} {r['assumed_initial_soc_percent']:>7.2f} "
            f"{r['energy_needed_kwh_for_visit']:>9.2f} {r['dwell_hours']:>7.2f} {extended:>5}"
        )

    print(f"\n  Total energy needed : {df_out['energy_needed_kwh_for_visit'].sum():.2f} kWh")
    print(f"  Events with ext dwell: {(df_out['dwell_hours'] > df_out['dwell_hours_actual']).sum()}")
    print(f"\nDone. Saved -> {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
