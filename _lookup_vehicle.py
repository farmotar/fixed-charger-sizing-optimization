import pandas as pd

ALL_ROWS = (
    r"D:\Geotab_EV_Parameters\charger_sizing_test"
    r"\Geotab_Zone_to_Zone_Dataset\Geotab_Zone_to_Zone_Dataset"
    r"\01_Final_Dataset\final_zone_to_zone_all_rows.csv"
)
print("Loading …")
z2z = pd.read_csv(ALL_ROWS, low_memory=False)

for col in ["to_entry_time", "to_exit_time", "from_entry_time", "from_exit_time"]:
    z2z[col] = pd.to_datetime(z2z[col], utc=True, errors="coerce")

z2z["_date_pac"] = (
    z2z["to_entry_time"]
    .dt.tz_convert("America/Los_Angeles")
    .dt.strftime("%Y-%m-%d")
)

veh = z2z[
    (z2z["vehicle_name"].astype(str) == "7009504")
    & (z2z["_date_pac"] == "2025-06-30")
].copy()

pac = "America/Los_Angeles"

def fmt(ts):
    if pd.isna(ts):
        return "—"
    return ts.tz_convert(pac).strftime("%H:%M:%S")

print(f"\nAll Z2Z rows for vehicle 7009504 on 2025-06-30  ({len(veh)} rows)")
print("=" * 110)
for idx, (_, row) in enumerate(veh.iterrows(), 1):
    print(f"\n── Trip {idx} ──────────────────────────────────────────────────────────────────")
    print(f"  FROM zone        : {row['from_zone']}")
    print(f"  From entry (Pac) : {fmt(row['from_entry_time'])}")
    print(f"  From exit  (Pac) : {fmt(row['from_exit_time'])}")
    print(f"  From dwell (min) : {row['from_dwell_minutes']:.1f}")
    print(f"  ── travelled ──")
    print(f"  Trip dist (miles): {row['trip_first_distance_miles_between']}")
    print(f"  # trips between  : {row['number_of_trips_between']}")
    print(f"  ── TO zone ──")
    print(f"  TO zone          : {row['to_zone']}")
    print(f"  To entry   (Pac) : {fmt(row['to_entry_time'])}")
    print(f"  To exit    (Pac) : {fmt(row['to_exit_time'])}")
    print(f"  To dwell   (min) : {row['to_dwell_minutes']:.1f}")
    print(f"  use_for_opt      : {row['use_for_optimization']}  ({row['optimization_filter_reason']})")
print()

# Also show original event row for comparison
orig = pd.read_csv(
    r"D:\Geotab_EV_Parameters\charger_sizing_test\northgate_2025_06_30_min1h_events.csv"
)
orig_row = orig[orig["vehicle_id"] == 7009504]
print("Original events CSV row(s) for 7009504:")
print(orig_row[["vehicle_id","ev_equivalent_model","arrival_time","departure_time",
                "dwell_hours_actual","energy_needed_kwh_for_visit",
                "assumed_initial_soc_percent","battery_capacity_kwh"]].to_string())
