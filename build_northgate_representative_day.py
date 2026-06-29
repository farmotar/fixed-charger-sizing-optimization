"""
build_northgate_representative_day.py
======================================
Northgate Charger-Sizing Workflow — Steps 1–5 (data preparation only).

SOURCE DATA: Method C — 50% SOC at previous eligible zone with unresolved-
inbound fallback (apply_unresolved_inbound_fallback.py output).

  fill_kwh_source = 'inbound_chain_resolved'    : inbound chain found in
      Geotab data; vehicle arrived at Northgate with SOC = max(0, 50% –
      inbound_miles / range_mi).
  fill_kwh_source = 'unresolved_fallback_50pct' : no inbound chain found;
      vehicle assumed to arrive at Northgate with SOC = 50% (conservative).

SOC_AT_ELIGIBLE_ZONE = 0.50  (explicit in fetch_northgate_departures_with
                               _inbound_soc_50pct.py, line 81)

ENERGY PER VISIT (already computed in source file):
  northgate_fill_kwh = min(outbound_energy_need_kwh, battery_room_kwh)
  where outbound_energy_need_kwh = total_chain_miles * e_value
        battery_room_kwh         = battery_capacity_kwh – arrival_energy_kwh
        arrival_energy_kwh       = soc_arrival_northgate * battery_capacity_kwh

TARGET SOC is NOT a fixed percentage.  It is determined per visit by how
much energy is needed to cover the outbound trip.  It is reported as:
  target_soc_percent = min((arrival_energy_kwh + northgate_fill_kwh)
                           / battery_capacity_kwh, 1.0) * 100

STEPS IN THIS SCRIPT:
  1. Load Method C source data (50pct fallback file, 'All Departures' sheet).
  2. Find representative day = date with highest sum(northgate_fill_kwh).
  3. Build visit-level charging-event dataset for that day (one row per visit).
  4. Create max_charge_kw mapping template (NEEDS_USER_INPUT — do not fill).
  5. Create validation summary and multi-visit-vehicles list.
  6. STOP — do NOT run charger sizing until max_charge_kw mapping is approved.

OUTPUTS:
  northgate_representative_day_method_c_visit_level_charging_events.csv
  ev_equivalent_max_charge_power_mapping.csv
  northgate_representative_day_method_c_validation_summary.csv
  northgate_representative_day_method_c_multi_visit_vehicles.csv
  northgate_daily_method_c_energy_by_date.csv   (all dates, for reference)
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR    = Path("D:/Geotab_EV_Parameters")
OUTPUT_DIR  = BASE_DIR / "charger_sizing_test"
OUTPUT_DIR.mkdir(exist_ok=True)

# Primary Method C source: 50% SOC inbound chain + unresolved fallback
FALLBACK_FILE = (
    BASE_DIR /
    "northgate_departures_chained_aug_Dec_2025_with_inbound_soc_50pct_fallback.xlsx"
)
SHEET_NAME = "All Departures"

# Vehicle master — provides derived_segment (vehicle_type_or_segment)
VEHICLE_MASTER = BASE_DIR / "northgate_vehicle_master.csv"

# Outputs
EVENTS_FILE       = OUTPUT_DIR / "northgate_representative_day_method_c_visit_level_charging_events.csv"
MAPPING_FILE      = OUTPUT_DIR / "ev_equivalent_max_charge_power_mapping.csv"
VALIDATION_FILE   = OUTPUT_DIR / "northgate_representative_day_method_c_validation_summary.csv"
MULTI_VISIT_FILE  = OUTPUT_DIR / "northgate_representative_day_method_c_multi_visit_vehicles.csv"
DAILY_ENERGY_FILE = OUTPUT_DIR / "northgate_daily_method_c_energy_by_date.csv"

# ---------------------------------------------------------------------------
# Column names in the source file
# ---------------------------------------------------------------------------
COL_DEVICE_ID   = "device_id"
COL_DEVICE_NAME = "device_name"
COL_VIN         = "vin"
COL_ARRIVAL     = "zone_entry_time_utc"   # vehicle ENTERS Northgate zone
COL_DEPARTURE   = "zone_exit_time_utc"    # vehicle EXITS Northgate zone (starts outbound)
COL_DWELL       = "dwell_hrs"
COL_PREV_ZONE   = "prev_eligible_zone_name"
COL_FILL_SOURCE = "fill_kwh_source"
COL_EV_EQUIV    = "ev_equivalency"
COL_BATT        = "battery_capacity_kwh"
COL_SOC_ARRIVAL = "soc_arrival_northgate"
COL_ARR_KWH     = "arrival_energy_kwh"
COL_OUT_KWH     = "outbound_energy_need_kwh"
COL_ROOM_KWH    = "battery_room_kwh"
COL_FILL_KWH    = "northgate_fill_kwh"
COL_E_VALUE     = "e_value"
COL_CHAIN_MILES = "total_chain_miles"

# ---------------------------------------------------------------------------
# Step 1 — Load data
# ---------------------------------------------------------------------------
print("=" * 70)
print("  NORTHGATE CHARGER SIZING — METHOD C REPRESENTATIVE DAY BUILD")
print("=" * 70)

print(f"\n[1] Loading source file ...")
print(f"    {FALLBACK_FILE.name}  [sheet: {SHEET_NAME}]")

df = pd.read_excel(FALLBACK_FILE, sheet_name=SHEET_NAME)
print(f"    {len(df):,} rows  x  {df.shape[1]} columns")
print(f"    Columns: {df.columns.tolist()}")

# Parse timestamps
df[COL_ARRIVAL]   = pd.to_datetime(df[COL_ARRIVAL],   utc=True, errors="coerce")
df[COL_DEPARTURE] = pd.to_datetime(df[COL_DEPARTURE], utc=True, errors="coerce")

# Date of each visit (UTC date — consistent with prior analyses)
# Keep NaT-derived dates as NaN (object dtype); filter them below
df["_visit_date"] = df[COL_ARRIVAL].dt.date  # returns datetime.date or NaT->None

# ---------------------------------------------------------------------------
# Step 2 — Load vehicle master for derived_segment
# ---------------------------------------------------------------------------
print(f"\n[1b] Loading vehicle master for segment info ...")
master = pd.read_csv(VEHICLE_MASTER, dtype=str)
vin_to_segment: dict[str, str] = dict(
    zip(
        master["vin"].str.strip().str.upper(),
        master["derived_segment"].fillna(""),
    )
)
print(f"     {len(vin_to_segment)} VINs with segment info")

# ---------------------------------------------------------------------------
# Step 3 — Filter to rows with Method C energy demand
# ---------------------------------------------------------------------------
has_fill      = df[COL_FILL_KWH].notna() & (df[COL_FILL_KWH] > 0)
has_arrival   = df[COL_ARRIVAL].notna()
has_min_dwell = df[COL_DWELL].notna() & (df[COL_DWELL] >= 0.25)   # exclude GPS artifacts / drive-throughs
df_ev = df[has_fill & has_arrival & has_min_dwell].copy()

n_resolved  = int((df_ev[COL_FILL_SOURCE] == "inbound_chain_resolved").sum())
n_fallback  = int((df_ev[COL_FILL_SOURCE] == "unresolved_fallback_50pct").sum())
n_dropped   = int((has_fill & has_arrival & ~has_min_dwell).sum())

print(f"\n[2] Rows with Method C energy demand (northgate_fill_kwh > 0): {len(df_ev):,}")
print(f"    inbound_chain_resolved     : {n_resolved:,}")
print(f"    unresolved_fallback_50pct  : {n_fallback:,}")
print(f"    Dropped (dwell < 0.25h)    : {n_dropped:,}  [GPS artifacts / drive-throughs excluded]")
print(f"    Date range: {df_ev['_visit_date'].min()}  to  {df_ev['_visit_date'].max()}")

# ---------------------------------------------------------------------------
# Step 4 — Daily aggregation — find representative day
# ---------------------------------------------------------------------------
daily = (
    df_ev
    .groupby("_visit_date")
    .agg(
        total_fill_kwh          = (COL_FILL_KWH,    "sum"),
        unique_vehicles         = (COL_DEVICE_ID,   "nunique"),
        visit_count             = (COL_DEVICE_ID,   "count"),
        resolved_visits         = (COL_FILL_SOURCE, lambda x: (x == "inbound_chain_resolved").sum()),
        fallback_visits         = (COL_FILL_SOURCE, lambda x: (x == "unresolved_fallback_50pct").sum()),
    )
    .reset_index()
    .rename(columns={"_visit_date": "date"})
    .sort_values("total_fill_kwh", ascending=False)
    .reset_index(drop=True)
)

# Save all-dates reference
daily.to_csv(DAILY_ENERGY_FILE, index=False, encoding="utf-8-sig")
print(f"\n[3] Daily Method C energy by date saved -> {DAILY_ENERGY_FILE.name}")

# Representative day = highest total energy
rep_row      = daily.iloc[0]
rep_date     = rep_row["date"]
rep_kwh      = rep_row["total_fill_kwh"]
rep_vehicles = int(rep_row["unique_vehicles"])
rep_visits   = int(rep_row["visit_count"])

print(f"\n    Representative day (highest total Method C energy):")
print(f"      Date          : {rep_date}")
print(f"      Total kWh     : {rep_kwh:.1f}")
print(f"      Unique vehicles: {rep_vehicles}")
print(f"      Northgate visits: {rep_visits}")
print(f"\n    Top 5 days by Method C energy:")
print(daily.head(5).to_string(index=False))

# ---------------------------------------------------------------------------
# Step 5 — Build visit-level dataset for representative day
# ---------------------------------------------------------------------------
print(f"\n[4] Building visit-level dataset for {rep_date} ...")

day_df = (
    df_ev[df_ev["_visit_date"] == rep_date]
    .copy()
    .sort_values([COL_DEVICE_NAME, COL_ARRIVAL])
    .reset_index(drop=True)
)

# Visit sequence per vehicle (within this day)
day_df["visit_seq"] = day_df.groupby(COL_DEVICE_ID).cumcount() + 1

# Vehicle type / segment from master
day_df["_vin_norm"]            = day_df[COL_VIN].astype(str).str.strip().str.upper()
day_df["vehicle_type_or_seg"]  = day_df["_vin_norm"].map(vin_to_segment).fillna("")

# Charging event ID
date_str = str(rep_date).replace("-", "")
day_df["charging_event_id"] = (
    day_df[COL_DEVICE_NAME].astype(str)
    + "_" + date_str
    + "_visit_" + day_df["visit_seq"].astype(str)
)

# assumed_initial_soc_percent:
#   - inbound_chain_resolved  → soc_arrival_northgate (may be < 0.50 if vehicle
#     drove far from the eligible zone)
#   - unresolved_fallback_50pct → always 0.50 (see apply_unresolved_inbound_fallback.py)
day_df["assumed_initial_soc_pct"] = (day_df[COL_SOC_ARRIVAL].fillna(0.5) * 100).round(1)

# target_soc_percent:
#   Charged to exactly enough to complete the outbound trip (capped at 100%).
#   NOT a fixed target — depends on trip length.
valid_batt = day_df[COL_BATT].notna() & (day_df[COL_BATT] > 0)
day_df["target_soc_pct"] = None
day_df.loc[valid_batt, "target_soc_pct"] = (
    (
        (day_df.loc[valid_batt, COL_ARR_KWH].fillna(0) + day_df.loc[valid_batt, COL_FILL_KWH])
        / day_df.loc[valid_batt, COL_BATT]
    )
    .clip(upper=1.0)
    * 100
).round(1)

# dwell_hours
day_df["dwell_hours"] = day_df[COL_DWELL].round(3)

# Notes per row
def _build_note(row: pd.Series) -> str:
    src = row.get(COL_FILL_SOURCE, "")
    if src == "inbound_chain_resolved":
        pz   = row.get(COL_PREV_ZONE, "")
        d_in = row.get("inbound_chain_miles_to_northgate", None)
        soc  = row.get(COL_SOC_ARRIVAL, None)
        parts = [f"Inbound from '{pz}' at 50% SOC"]
        if pd.notna(d_in):
            parts.append(f"inbound_dist={d_in:.1f}mi")
        if pd.notna(soc):
            parts.append(f"arrival_SOC={soc*100:.0f}%")
        return "; ".join(parts)
    elif src == "unresolved_fallback_50pct":
        return "Inbound chain unresolved; assumed arrival SOC=50% (Method C fallback)"
    return ""

day_df["notes"] = day_df.apply(_build_note, axis=1)

# max_charge_kw — NOT in source data, awaiting user mapping
day_df["max_charge_kw"]        = None
day_df["max_charge_kw_status"] = "missing_needs_user_input"

# Assemble output columns
events = pd.DataFrame({
    "charging_event_id":                    day_df["charging_event_id"],
    "vehicle_id":                           day_df[COL_DEVICE_NAME],
    "device_id_geotab":                     day_df[COL_DEVICE_ID],
    "vin":                                  day_df[COL_VIN],
    "visit_sequence_for_vehicle_that_day":  day_df["visit_seq"],
    "site_id":                              "Northgate",
    "arrival_time":                         day_df[COL_ARRIVAL],
    "departure_time":                       day_df[COL_DEPARTURE],
    "dwell_hours":                          day_df["dwell_hours"],
    "previous_eligible_zone":               day_df[COL_PREV_ZONE],
    "inbound_chain_status":                 day_df[COL_FILL_SOURCE],
    "ev_equivalent_model":                  day_df[COL_EV_EQUIV],
    "vehicle_type_or_segment":              day_df["vehicle_type_or_seg"],
    "battery_capacity_kwh":                 day_df[COL_BATT],
    "assumed_initial_soc_percent":          day_df["assumed_initial_soc_pct"],
    "target_soc_percent":                   day_df["target_soc_pct"],
    "energy_needed_kwh_for_visit":          day_df[COL_FILL_KWH].round(2),
    "energy_method":                        "Method_C_50pct_inbound_chain_with_fallback",
    "source_column":                        COL_FILL_KWH,   # preserves source column name
    "outbound_energy_need_kwh":             day_df[COL_OUT_KWH].round(2),
    "outbound_chain_miles":                 day_df[COL_CHAIN_MILES],
    "max_charge_kw":                        day_df["max_charge_kw"],
    "max_charge_kw_status":                 day_df["max_charge_kw_status"],
    "notes":                                day_df["notes"],
})

events.to_csv(EVENTS_FILE, index=False, encoding="utf-8-sig")
print(f"    Saved {len(events)} charging events -> {EVENTS_FILE.name}")

# ---------------------------------------------------------------------------
# Step 5b — Merge max_charge_kw from user-filled mapping
# ---------------------------------------------------------------------------
FILLED_MAPPING = OUTPUT_DIR / "ev_equivalent_max_charge_power_mapping_filled.xlsx"
print(f"\n[4b] Merging max_charge_kw from filled mapping ...")
print(f"     {FILLED_MAPPING.name}")

if FILLED_MAPPING.exists():
    mapping_filled = pd.read_excel(FILLED_MAPPING)[
        ["ev_equivalent_model", "max_ac_charge_kw", "max_dc_charge_kw",
         "selected_max_charge_kw_for_model", "source_or_assumption", "confidence"]
    ].copy()
    mapping_filled = mapping_filled.rename(columns={
        "selected_max_charge_kw_for_model": "max_charge_kw",
        "source_or_assumption":             "max_charge_kw_source",
        "confidence":                       "max_charge_kw_confidence",
    })

    events = events.drop(columns=["max_charge_kw", "max_charge_kw_status"], errors="ignore")
    events = events.merge(
        mapping_filled[["ev_equivalent_model", "max_ac_charge_kw", "max_dc_charge_kw",
                         "max_charge_kw", "max_charge_kw_source", "max_charge_kw_confidence"]],
        on="ev_equivalent_model",
        how="left",
    )
    events["max_charge_kw_status"] = events["max_charge_kw"].apply(
        lambda v: "provided_by_mapping" if pd.notna(v) else "missing_needs_user_input"
    )
    n_mapped  = int(events["max_charge_kw"].notna().sum())
    n_missing = int(events["max_charge_kw"].isna().sum())
    print(f"     Events with max_charge_kw mapped : {n_mapped}")
    print(f"     Events still missing             : {n_missing}")
    if n_missing > 0:
        missing_models = events.loc[events["max_charge_kw"].isna(), "ev_equivalent_model"].unique()
        for m in missing_models:
            print(f"       MISSING: {m}")
    events.to_csv(EVENTS_FILE, index=False, encoding="utf-8-sig")
    print(f"     Events file updated with max_charge_kw -> {EVENTS_FILE.name}")
else:
    print(f"     WARNING: filled mapping not found at {FILLED_MAPPING} — max_charge_kw left blank")

# ---------------------------------------------------------------------------
# Step 6 — Max-charge-power mapping template
# ---------------------------------------------------------------------------
print(f"\n[5] Building max_charge_kw mapping template ...")

# Unique (ev_equivalent_model, vehicle_type_or_segment) pairs from this day
unique_models = (
    events[["ev_equivalent_model", "vehicle_type_or_segment"]]
    .dropna(subset=["ev_equivalent_model"])
    .drop_duplicates(subset=["ev_equivalent_model"])
    .sort_values("ev_equivalent_model")
    .reset_index(drop=True)
)

mapping = pd.DataFrame({
    "ev_equivalent_model":              unique_models["ev_equivalent_model"],
    "vehicle_type_or_segment":          unique_models["vehicle_type_or_segment"],
    "max_ac_charge_kw":                 "",
    "max_dc_charge_kw":                 "",
    "selected_max_charge_kw_for_model": "NEEDS_USER_INPUT",
    "source_or_assumption":             "",
    "notes":                            "",
})

mapping.to_csv(MAPPING_FILE, index=False, encoding="utf-8-sig")
print(f"    Saved {len(mapping)} unique EV models -> {MAPPING_FILE.name}")
print(f"    Models requiring max_charge_kw input:")
for _, r in mapping.iterrows():
    print(f"      {r['ev_equivalent_model']:45s}  segment: {r['vehicle_type_or_segment']}")

# ---------------------------------------------------------------------------
# Step 7 — Validation summary
# ---------------------------------------------------------------------------
print(f"\n[6] Building validation summary ...")

# Multi-visit vehicles
visit_counts = (
    events.groupby("vehicle_id")["visit_sequence_for_vehicle_that_day"]
    .max()
    .reset_index()
    .rename(columns={"visit_sequence_for_vehicle_that_day": "max_visit_seq"})
)
multi_mask    = visit_counts["max_visit_seq"] > 1
n_multi_veh   = int(multi_mask.sum())
multi_vehicle_ids = visit_counts.loc[multi_mask, "vehicle_id"].tolist()

summary = pd.DataFrame([{
    "selected_date":                    str(rep_date),
    "source_file":                      FALLBACK_FILE.name,
    "source_sheet":                     SHEET_NAME,
    "energy_method":                    "Method_C_50pct_inbound_chain_with_fallback",
    "soc_at_eligible_zone_assumption":  "50%",
    "target_soc_note":                  "Dynamic per visit (enough to cover outbound trip); not a fixed %",
    "number_of_unique_vehicles":        int(events["vehicle_id"].nunique()),
    "number_of_charging_events":        len(events),
    "vehicles_with_multiple_visits":    n_multi_veh,
    "inbound_chain_resolved_visits":    int((events["inbound_chain_status"] == "inbound_chain_resolved").sum()),
    "unresolved_fallback_50pct_visits": int((events["inbound_chain_status"] == "unresolved_fallback_50pct").sum()),
    "total_method_c_energy_kwh":        round(float(events["energy_needed_kwh_for_visit"].sum()), 2),
    "min_dwell_hours":                  round(float(events["dwell_hours"].min()), 3),
    "avg_dwell_hours":                  round(float(events["dwell_hours"].mean()), 3),
    "max_dwell_hours":                  round(float(events["dwell_hours"].max()), 3),
    "min_energy_kwh":                   round(float(events["energy_needed_kwh_for_visit"].min()), 2),
    "avg_energy_kwh":                   round(float(events["energy_needed_kwh_for_visit"].mean()), 2),
    "max_energy_kwh":                   round(float(events["energy_needed_kwh_for_visit"].max()), 2),
    "missing_arrival_time":             int(events["arrival_time"].isna().sum()),
    "missing_departure_time":           int(events["departure_time"].isna().sum()),
    "missing_method_c_energy":          int(events["energy_needed_kwh_for_visit"].isna().sum()),
    "missing_ev_equivalent_model":      int(events["ev_equivalent_model"].isna().sum()),
    "missing_battery_capacity_kwh":     int(events["battery_capacity_kwh"].isna().sum()),
    "missing_max_charge_kw":            len(events),   # all missing — awaiting user mapping
}])

summary.to_csv(VALIDATION_FILE, index=False, encoding="utf-8-sig")
print(f"    Saved validation summary -> {VALIDATION_FILE.name}")

# Multi-visit vehicles detail
multi_df = (
    events[events["vehicle_id"].isin(multi_vehicle_ids)]
    .sort_values(["vehicle_id", "arrival_time"])
)
multi_df.to_csv(MULTI_VISIT_FILE, index=False, encoding="utf-8-sig")
print(f"    Saved multi-visit vehicles -> {MULTI_VISIT_FILE.name}  ({n_multi_veh} vehicles)")

# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------
print(f"\n{'=' * 70}")
print("  RESULTS SUMMARY — STEP 5 COMPLETE")
print(f"{'=' * 70}")
print(f"\n  Source file      : {FALLBACK_FILE.name}")
print(f"  Sheet            : {SHEET_NAME}")
print(f"  Energy method    : Method C — 50% SOC at previous eligible zone")
print(f"                     (unresolved inbounds: 50% SOC fallback)")
print(f"  SOC assumption   : SOC_AT_ELIGIBLE_ZONE = 0.50 (from script line 81)")
print(f"  Target SOC       : Dynamic per visit — NOT a fixed percentage")
print(f"                     = min((arrival_kwh + fill_kwh) / batt_kwh, 1.0) × 100")
print(f"\n  Representative date        : {rep_date}")
print(f"  Total Method C energy      : {rep_kwh:.1f} kWh")
print(f"  Unique vehicles            : {rep_vehicles}")
print(f"  Total charging events      : {len(events)}")
print(f"  Vehicles w/ multiple visits: {n_multi_veh}")
if multi_vehicle_ids:
    for vid in multi_vehicle_ids:
        vevents = events[events["vehicle_id"] == vid].sort_values("arrival_time")
        print(f"      {vid}: {len(vevents)} visits")
        for _, ve in vevents.iterrows():
            print(f"        visit {ve['visit_sequence_for_vehicle_that_day']}: "
                  f"arrive {ve['arrival_time']}  depart {ve['departure_time']}  "
                  f"energy {ve['energy_needed_kwh_for_visit']:.1f} kWh")

print(f"\n  EV models needing max_charge_kw from user ({len(mapping)}):")
for _, r in mapping.iterrows():
    print(f"    {r['ev_equivalent_model']}")

print(f"\n  Output files:")
print(f"    {EVENTS_FILE}")
print(f"    {MAPPING_FILE}")
print(f"    {VALIDATION_FILE}")
print(f"    {MULTI_VISIT_FILE}")
print(f"    {DAILY_ENERGY_FILE}")

print(f"\n{'=' * 70}")
print("  STEP 5 COMPLETE — STOPPED BEFORE CHARGER SIZING")
print("  Next: review and fill ev_equivalent_max_charge_power_mapping.csv")
print("  Then: re-run to merge max_charge_kw into the events file.")
print(f"{'=' * 70}")
