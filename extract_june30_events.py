"""
Extract 2025-06-30 serviceable charging events from 'Returned to Northgate' sheet
and save as MILP-compatible CSV.
"""

import pandas as pd
from pathlib import Path

EXCEL_PATH   = Path(r"D:\Geotab_EV_Parameters\northgate_departures_chained_aug_Dec_2025_with_inbound_soc_50pct_fallback.xlsx")
MAPPING_PATH = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\ev_equivalent_max_charge_power_mapping_filled.xlsx")
OUTPUT_PATH  = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\northgate_2025_06_30_serviceable_charging_events.csv")
TARGET_DATE  = "2025-06-30"
ETA          = 0.90
MIN_DWELL_H  = 0.25
MIN_ENERGY   = 0.10

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
# Excel stores some exit times as ISO strings (no microseconds), others as datetime objects.
# Parse without utc=True first to avoid coercion failures on mixed-type columns.
raw_exit = df["zone_exit_time_utc"]
parsed_exit = pd.to_datetime(raw_exit, errors="coerce")
if parsed_exit.dt.tz is None:
    parsed_exit = parsed_exit.dt.tz_localize("UTC")
else:
    parsed_exit = parsed_exit.dt.tz_convert("UTC")
df["zone_exit_time_utc"] = parsed_exit

# For rows where exit is still NaT, reconstruct from entry + dwell_hrs
nat_mask = df["zone_exit_time_utc"].isna()
if nat_mask.any():
    df.loc[nat_mask, "zone_exit_time_utc"] = (
        df.loc[nat_mask, "zone_entry_time_utc"] +
        pd.to_timedelta(df.loc[nat_mask, "dwell_hrs"], unit="h")
    )
    print(f"  Reconstructed {nat_mask.sum()} departure times from dwell_hrs.")

# Recompute dwell_hours from timestamps (authoritative)
df["dwell_hours"] = (
    (df["zone_exit_time_utc"] - df["zone_entry_time_utc"]).dt.total_seconds() / 3600
)

# ── 4. Level B: dwell >= 0.25 h ───────────────────────────────────────────────
df = df[df["dwell_hours"] >= MIN_DWELL_H].copy()
print(f"  After dwell >= {MIN_DWELL_H}h filter: {len(df)} rows")

# ── 5. Merge charger power limits ─────────────────────────────────────────────
print(f"Reading mapping file ...")
df_map = pd.read_excel(MAPPING_PATH)[["ev_equivalent_model", "max_ac_charge_kw", "max_dc_charge_kw"]]
df_map = df_map.drop_duplicates(subset=["ev_equivalent_model"])

df = df.merge(
    df_map.rename(columns={"ev_equivalent_model": "ev_equivalency"}),
    on="ev_equivalency",
    how="left",
)
df["max_ac_charge_kw"] = df["max_ac_charge_kw"].fillna(0.0)
df["max_dc_charge_kw"] = df["max_dc_charge_kw"].fillna(50.0)
print(f"  Models with missing DC power: {df['max_dc_charge_kw'].isna().sum()}")

# ── 6. Individually feasible + Level C filter ─────────────────────────────────
df["individually_feasible"] = (
    df["northgate_fill_kwh"] <=
    ETA * df["max_dc_charge_kw"].clip(upper=350) * df["dwell_hours"] + MIN_ENERGY
)
df_svc = df[
    df["individually_feasible"] &
    (df["northgate_fill_kwh"] >= MIN_ENERGY) &
    df["northgate_fill_kwh"].notna()
].copy()
print(f"  After serviceable filter: {len(df_svc)} events")

# ── 7. Build output columns ────────────────────────────────────────────────────
df_svc = df_svc.reset_index(drop=True)
df_svc["charging_event_id"] = [
    f"evt_{TARGET_DATE.replace('-', '')}_v{i+1:02d}" for i in range(len(df_svc))
]

# SOC columns: soc_arrival_northgate is 0-1 fraction -> convert to percent
df_svc["assumed_initial_soc_percent"] = (df_svc["soc_arrival_northgate"] * 100).round(2)
df_svc["target_soc_percent"]          = 100.0   # targeting full charge

# Timestamps as ISO UTC strings
df_svc["arrival_time_str"]   = df_svc["zone_entry_time_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
df_svc["departure_time_str"] = df_svc["zone_exit_time_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")

out_cols = {
    "charging_event_id":           "charging_event_id",
    "device_name":                 "vehicle_id",
    "arrival_time_str":            "arrival_time",
    "departure_time_str":          "departure_time",
    "dwell_hours":                 "dwell_hours",
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

# ── 8. Save ────────────────────────────────────────────────────────────────────
OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
df_out.to_csv(OUTPUT_PATH, index=False)
print(f"\nSaved {len(df_out)} serviceable events -> {OUTPUT_PATH}")
print("\nEvent summary:")
summary_cols = ["charging_event_id", "vehicle_id", "ev_equivalent_model",
                "dwell_hours", "energy_needed_kwh_for_visit",
                "max_ac_charge_kw", "max_dc_charge_kw",
                "battery_capacity_kwh", "assumed_initial_soc_percent"]
print(df_out[[c for c in summary_cols if c in df_out.columns]].to_string(index=False))
print(f"\nTotal energy needed: {df_out['energy_needed_kwh_for_visit'].sum():.2f} kWh")
