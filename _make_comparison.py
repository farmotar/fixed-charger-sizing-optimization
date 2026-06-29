"""Regenerate comparison CSVs from final_zone_to_zone_all_rows.csv."""
import importlib.util, pandas as pd
from pathlib import Path

# load helpers from extract_z2z_events
spec = importlib.util.spec_from_file_location(
    "ez",
    r"D:\Geotab_EV_Parameters\charger_sizing_test\extract_z2z_events.py",
)
ez = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ez)

ALL_ROWS = (
    r"D:\Geotab_EV_Parameters\charger_sizing_test"
    r"\Geotab_Zone_to_Zone_Dataset\Geotab_Zone_to_Zone_Dataset"
    r"\01_Final_Dataset\final_zone_to_zone_all_rows.csv"
)
print("Loading all_rows CSV …")
z2z = pd.read_csv(ALL_ROWS, low_memory=False)
print(f"  {len(z2z):,} rows loaded")

z2z["to_entry_time"] = pd.to_datetime(z2z["to_entry_time"], utc=True, errors="coerce")
z2z["to_exit_time"]  = pd.to_datetime(z2z["to_exit_time"],  utc=True, errors="coerce")
z2z["_date_pac"] = (
    z2z["to_entry_time"]
    .dt.tz_convert("America/Los_Angeles")
    .dt.strftime("%Y-%m-%d")
)

ng = z2z[
    z2z["to_zone"].str.contains("Northgate", case=False, na=False)
    & (z2z["_date_pac"] == "2025-06-30")
].copy()
print(f"  {len(ng)} Northgate rows on 2025-06-30 (no opt-flag filter)")

# EV lookup helpers
ice_to_ev  = ez._load_ev_equivalencies(ez.EV_CATEGORIES_XLSX)
charge_df  = ez._load_charge_rates(ez.CHARGE_RATE_XLSX)
charge_map = charge_df.set_index("ev_equivalent_model").to_dict("index")
DC_FALLBACK = {"Global Electric Street Sweeper (M4E)": 60.0}
AC_FALLBACK = {"Global Electric Street Sweeper (M4E)": 0.0}


def get_spec(ev, key):
    s = ez.EV_SPEC_OVERRIDES.get(ev)
    if s:
        return s[0] if key == "b" else s[1]
    return float("nan")


def get_cr(ev, key):
    cr = charge_map.get(ev)
    if cr:
        return float(cr[key])
    return DC_FALLBACK.get(ev, 50.0) if "dc" in key else AC_FALLBACK.get(ev, 0.0)


# ── Build Z2Z comparison rows ──────────────────────────────────────────────────
rows = []
for _, r in ng.iterrows():
    make  = str(r.get("make", "") or "")
    model = str(r.get("model", "") or "")
    ev    = ez._match_ev(make, model, ice_to_ev)

    dwell_actual = (r["to_dwell_minutes"] or 0) / 60.0
    dwell_eff    = max(dwell_actual, 1.0)
    dist         = float(r.get("trip_first_distance_miles_between") or 0)

    # use_for_optimization_bool flags GPS artifacts (same-zone micro-trips,
    # distance < 0.2 mi, travel time < 2 min). Only rows flagged True are
    # candidates for the MILP.
    use_for_opt = str(r.get("use_for_optimization_bool", "")).strip().lower() == "true"

    if ev:
        batt = get_spec(ev, "b")
        eff  = get_spec(ev, "e")
        ac   = get_cr(ev, "max_ac_charge_kw")
        dc   = get_cr(ev, "max_dc_charge_kw")
        # Assume 50% SOC at departure from previous zone; subtract trip energy.
        # arrival_soc = 50% − (distance × efficiency / battery × 100%)
        if not (pd.isna(batt) or pd.isna(eff)):
            arr_soc = max(0.0, 50.0 - dist * eff / batt * 100.0)
        else:
            arr_soc = 50.0
        need = max(0.0, (100.0 - arr_soc) / 100.0 * batt) if not pd.isna(batt) else float("nan")
        max_del  = 0.9 * min(350, dc) * dwell_eff
        feasible = (need <= max_del + 0.10) if not pd.isna(need) else False
        in_milp  = use_for_opt and feasible and (not pd.isna(need)) and need >= 0.10
        if not use_for_opt:
            excl = "gps_artifact"
        elif not feasible:
            excl = "infeasible"
        elif pd.isna(need) or need < 0.10:
            excl = "need<0.10"
        else:
            excl = ""
    else:
        batt = ac = dc = arr_soc = need = float("nan")
        feasible = False
        in_milp  = False
        excl     = "no_ev_match"

    t_in  = r["to_entry_time"].tz_convert("America/Los_Angeles")
    t_out = (
        r["to_exit_time"].tz_convert("America/Los_Angeles")
        if pd.notna(r["to_exit_time"])
        else None
    )

    rows.append(
        {
            "vehicle_id":               str(r["vehicle_name"]),
            "make":                     make,
            "model":                    model,
            "year":                     int(r["year"]) if pd.notna(r.get("year")) else "",
            "ev_equivalent_model":      ev or "",
            "previous_zone":            str(r.get("from_zone", "")),
            "northgate_entry_time_pac": t_in.strftime("%Y-%m-%d %H:%M") if pd.notna(t_in) else "",
            "northgate_exit_time_pac":  t_out.strftime("%Y-%m-%d %H:%M") if t_out and pd.notna(t_out) else "",
            "dwell_actual_h":           round(dwell_actual, 3),
            "dwell_effective_h":        round(dwell_eff, 3),
            "trip_dist_miles":          round(dist, 2),
            "estimated_arrival_soc_pct": round(arr_soc, 2) if not pd.isna(arr_soc) else "",
            "energy_needed_kwh":        round(need, 3) if not pd.isna(need) else "",
            "battery_kwh":              round(batt, 1) if not pd.isna(batt) else "",
            "max_ac_kw":                ac if not pd.isna(ac) else "",
            "max_dc_kw":                dc if not pd.isna(dc) else "",
            "individually_feasible":    feasible,
            "included_in_milp":         in_milp,
            "exclusion_reason":         excl,
            "use_for_optimization":     str(r.get("use_for_optimization", "")),
            "opt_filter_reason":        str(r.get("optimization_filter_reason", "") or ""),
        }
    )

df_z2z = pd.DataFrame(rows).sort_values("northgate_entry_time_pac").reset_index(drop=True)
out1 = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\comparison_z2z_northgate_20250630.csv")
df_z2z.to_csv(str(out1), index=False)
n_in   = df_z2z["included_in_milp"].sum()
n_excl = (~df_z2z["included_in_milp"]).sum()
print(f"Z2Z: {len(df_z2z)} rows  (in MILP: {n_in}, excluded: {n_excl})  -> {out1.name}")

# ── Original 16-event CSV enriched with Z2Z context ───────────────────────────
orig = pd.read_csv(
    r"D:\Geotab_EV_Parameters\charger_sizing_test\northgate_2025_06_30_min1h_events.csv"
)
orig["vehicle_id"] = orig["vehicle_id"].astype(str)
orig["northgate_entry_time_pac"] = (
    pd.to_datetime(orig["arrival_time"], utc=True)
    .dt.tz_convert("America/Los_Angeles")
    .dt.strftime("%Y-%m-%d %H:%M")
)
orig["northgate_exit_time_pac"] = (
    pd.to_datetime(orig["departure_time"], utc=True)
    .dt.tz_convert("America/Los_Angeles")
    .dt.strftime("%Y-%m-%d %H:%M")
)

z2z_ctx = (
    ng[["vehicle_name", "from_zone", "trip_first_distance_miles_between", "make", "model", "year"]]
    .copy()
    .assign(vehicle_name=lambda d: d["vehicle_name"].astype(str))
    .sort_values("vehicle_name")
    .drop_duplicates(subset=["vehicle_name"])
    .rename(
        columns={
            "vehicle_name": "vehicle_id",
            "from_zone": "previous_zone",
            "trip_first_distance_miles_between": "trip_dist_miles",
        }
    )
)
orig2 = orig.merge(z2z_ctx, on="vehicle_id", how="left")

df_orig = orig2[
    [
        "charging_event_id", "vehicle_id", "make", "model", "year",
        "ev_equivalent_model", "previous_zone",
        "northgate_entry_time_pac", "northgate_exit_time_pac",
        "dwell_hours_actual", "dwell_hours", "trip_dist_miles",
        "assumed_initial_soc_percent", "energy_needed_kwh_for_visit",
        "battery_capacity_kwh", "max_ac_charge_kw", "max_dc_charge_kw",
        "individually_feasible", "target_soc_percent",
    ]
].copy()

out2 = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\comparison_original_northgate_20250630.csv")
df_orig.to_csv(str(out2), index=False)
print(f"Original: {len(df_orig)} rows -> {out2.name}")
print("Done.")
