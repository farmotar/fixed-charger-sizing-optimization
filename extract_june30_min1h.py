"""
extract_june30_min1h.py
-----------------------
Extract 2025-06-30 charging events from 'Returned to Northgate' sheet.

Changes vs original extract_june30_events.py:
  1. NO minimum-dwell filter -- visits shorter than 15 min are kept.
  2. Any visit with dwell_hours < 1.0 is given a 1.0-hour effective dwell
     (departure_time is extended accordingly).  This reflects the assumption
     that a vehicle is held at least 1 hour for charging even if its GPS
     log shows a shorter stop.
  3. Feasibility check uses the clamped dwell (>= 1.0 h), so more events
     become individually feasible.
"""

import pandas as pd
from pathlib import Path

EXCEL_PATH   = Path(r"D:\Geotab_EV_Parameters\northgate_departures_chained_aug_Dec_2025_with_inbound_soc_50pct_fallback.xlsx")
MAPPING_PATH = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\ev_equivalent_max_charge_power_mapping_filled.xlsx")
OUTPUT_PATH  = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\northgate_2025_06_30_min1h_events.csv")
TARGET_DATE  = "2025-06-30"
ETA          = 0.90
MIN_DWELL_H  = 0.0    # no dwell exclusion
MIN_ENERGY   = 0.10
MIN_DWELL_ASSUMED = 1.0  # clamp all dwell times to at least this many hours

# ── 1. Load raw data ──────────────────────────────────────────────────────────
print(f"Reading '{EXCEL_PATH.name}' sheet='Returned to Northgate' ...")
df = pd.read_excel(EXCEL_PATH, sheet_name="Returned to Northgate")
print(f"  Loaded {len(df)} rows")

# ── 2. Filter to target date (Pacific time) ───────────────────────────────────
df["zone_entry_time_utc"] = pd.to_datetime(df["zone_entry_time_utc"], utc=True, errors="coerce")
df["date_local"] = df["zone_entry_time_utc"].dt.tz_convert("America/Los_Angeles").dt.date.astype(str)
df = df[df["date_local"] == TARGET_DATE].copy()
print(f"  Rows for {TARGET_DATE}: {len(df)}")

# ── 3. Parse departure time ────────────────────────────────────────────────────
raw_exit = df["zone_exit_time_utc"]
parsed_exit = pd.to_datetime(raw_exit, errors="coerce")
if parsed_exit.dt.tz is None:
    parsed_exit = parsed_exit.dt.tz_localize("UTC")
else:
    parsed_exit = parsed_exit.dt.tz_convert("UTC")
df["zone_exit_time_utc"] = parsed_exit

# Reconstruct from entry + dwell_hrs where still NaT
nat_mask = df["zone_exit_time_utc"].isna()
if nat_mask.any():
    df.loc[nat_mask, "zone_exit_time_utc"] = (
        df.loc[nat_mask, "zone_entry_time_utc"] +
        pd.to_timedelta(df.loc[nat_mask, "dwell_hrs"], unit="h")
    )
    print(f"  Reconstructed {nat_mask.sum()} departure times from dwell_hrs.")

# ── 4. Compute actual dwell from timestamps ────────────────────────────────────
df["dwell_hours_actual"] = (
    (df["zone_exit_time_utc"] - df["zone_entry_time_utc"]).dt.total_seconds() / 3600
)

# Report how many are below 1 hour
short = (df["dwell_hours_actual"] < MIN_DWELL_ASSUMED).sum()
print(f"  Visits with dwell < {MIN_DWELL_ASSUMED}h: {short} (will be extended to {MIN_DWELL_ASSUMED}h)")

# ── 5. Apply min-1h assumption ────────────────────────────────────────────────
# dwell_hours is the value used for all downstream calculations
df["dwell_hours"] = df["dwell_hours_actual"].clip(lower=MIN_DWELL_ASSUMED)

# Extend departure_time where dwell was below the minimum
short_mask = df["dwell_hours_actual"] < MIN_DWELL_ASSUMED
df.loc[short_mask, "zone_exit_time_utc"] = (
    df.loc[short_mask, "zone_entry_time_utc"] +
    pd.to_timedelta(MIN_DWELL_ASSUMED, unit="h")
)
print(f"  After min-{MIN_DWELL_ASSUMED}h dwell extension: all {len(df)} visits retained")

# ── 6. Merge charger power limits ─────────────────────────────────────────────
print("Reading mapping file ...")
df_map = pd.read_excel(MAPPING_PATH)[["ev_equivalent_model", "max_ac_charge_kw", "max_dc_charge_kw"]]
df_map = df_map.drop_duplicates(subset=["ev_equivalent_model"])

df = df.merge(
    df_map.rename(columns={"ev_equivalent_model": "ev_equivalency"}),
    on="ev_equivalency",
    how="left",
)
df["max_ac_charge_kw"] = df["max_ac_charge_kw"].fillna(0.0)
df["max_dc_charge_kw"] = df["max_dc_charge_kw"].fillna(50.0)

# ── 7. Feasibility filter ─────────────────────────────────────────────────────
# Uses extended dwell_hours so short-stop vehicles are now feasible
df["individually_feasible"] = (
    df["northgate_fill_kwh"] <=
    ETA * df["max_dc_charge_kw"].clip(upper=350) * df["dwell_hours"] + MIN_ENERGY
)

df_svc = df[
    df["individually_feasible"] &
    (df["northgate_fill_kwh"] >= MIN_ENERGY) &
    df["northgate_fill_kwh"].notna()
].copy()
print(f"  Serviceable events after min-1h dwell: {len(df_svc)}")

infeasible = df[~df["individually_feasible"] & df["northgate_fill_kwh"].notna()]
if len(infeasible) > 0:
    print(f"  Still infeasible ({len(infeasible)} events):")
    for _, r in infeasible.iterrows():
        max_e = ETA * min(350, r["max_dc_charge_kw"]) * r["dwell_hours"]
        print(f"    {r.get('device_name','?')}  need={r['northgate_fill_kwh']:.1f} kWh  "
              f"max_deliverable={max_e:.1f} kWh  dwell={r['dwell_hours']:.2f}h")

# ── 8. Build output columns ────────────────────────────────────────────────────
df_svc = df_svc.reset_index(drop=True)
df_svc["charging_event_id"] = [
    f"evt_{TARGET_DATE.replace('-', '')}_v{i+1:02d}" for i in range(len(df_svc))
]

df_svc["assumed_initial_soc_percent"] = (df_svc["soc_arrival_northgate"] * 100).round(2)
df_svc["target_soc_percent"]          = 100.0

df_svc["arrival_time_str"]   = df_svc["zone_entry_time_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
df_svc["departure_time_str"] = df_svc["zone_exit_time_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")

out_cols = {
    "charging_event_id":           "charging_event_id",
    "device_name":                 "vehicle_id",
    "arrival_time_str":            "arrival_time",
    "departure_time_str":          "departure_time",
    "dwell_hours":                 "dwell_hours",
    "dwell_hours_actual":          "dwell_hours_actual",
    "northgate_fill_kwh":          "energy_needed_kwh_for_visit",
    "max_ac_charge_kw":            "max_ac_charge_kw",
    "max_dc_charge_kw":            "max_dc_charge_kw",
    "ev_equivalency":              "ev_equivalent_model",
    "individually_feasible":       "individually_feasible",
    "battery_capacity_kwh":        "battery_capacity_kwh",
    "assumed_initial_soc_percent": "assumed_initial_soc_percent",
    "target_soc_percent":          "target_soc_percent",
}
df_out = df_svc[[c for c in out_cols if c in df_svc.columns]].rename(columns=out_cols)

# ── 9. Save ────────────────────────────────────────────────────────────────────
OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
df_out.to_csv(OUTPUT_PATH, index=False)
print(f"\nSaved {len(df_out)} events -> {OUTPUT_PATH}")

print("\nEvent summary (actual dwell vs assumed dwell):")
summary_cols = ["charging_event_id", "vehicle_id", "ev_equivalent_model",
                "dwell_hours_actual", "dwell_hours",
                "energy_needed_kwh_for_visit", "max_dc_charge_kw"]
print(df_out[[c for c in summary_cols if c in df_out.columns]].to_string(index=False))
print(f"\nTotal energy needed: {df_out['energy_needed_kwh_for_visit'].sum():.2f} kWh")
print(f"Events with extended dwell: {(df_out['dwell_hours'] > df_out['dwell_hours_actual']).sum()}")
