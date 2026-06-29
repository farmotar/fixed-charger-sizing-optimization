"""
audit_problematic_events.py
============================
Audits zero-dwell, near-zero-dwell, and individually-infeasible events
in the Northgate representative-day charging dataset.

DATA FLOW (confirmed from code inspection):
  zone_entry_time_utc  = vehicle ENTERS Northgate zone  (Geotab ExceptionEvent activeFrom)
  zone_exit_time_utc   = vehicle EXITS  Northgate zone  (Geotab ExceptionEvent activeTo)
  dwell_hrs            = (zone_exit - zone_entry) / 3600   [lines 1163-1165 in fetch script]
  northgate_fill_kwh   = energy needed for OUTBOUND trip AFTER zone_exit_time_utc
  Each row             = ONE departure event from Northgate

So the charging window = zone_entry_time_utc -> zone_exit_time_utc = dwell_hrs.
Zero-dwell rows arise when Geotab returns activeFrom == activeTo.

OUTPUTS:
  northgate_problematic_event_audit.csv
  northgate_problematic_event_audit_summary.csv
"""

from __future__ import annotations
from pathlib import Path
import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR   = Path("D:/Geotab_EV_Parameters")
OUTPUT_DIR = BASE_DIR / "charger_sizing_test"

EVENTS_FILE     = OUTPUT_DIR / "northgate_representative_day_method_c_visit_level_charging_events.csv"
FEASIBILITY_FILE= OUTPUT_DIR / "northgate_individual_feasibility_check.csv"
FALLBACK_FILE   = BASE_DIR / "northgate_departures_chained_aug_Dec_2025_with_inbound_soc_50pct_fallback.xlsx"
ARRIVALS_FILE   = BASE_DIR / "northgate_arrivals_aug_oct_2025.csv"
DAILY_FILE      = OUTPUT_DIR / "northgate_daily_method_c_energy_by_date.csv"

AUDIT_FILE   = OUTPUT_DIR / "northgate_problematic_event_audit.csv"
SUMMARY_FILE = OUTPUT_DIR / "northgate_problematic_event_audit_summary.csv"

ETA               = 0.90
BENCHMARK_DC_KW   = 350.0
MIN_DWELL_THRESH  = 0.25   # hours — threshold for "short dwell"
FULL_CHG_TOL      = 0.10   # kWh

# ---------------------------------------------------------------------------
# Step 1 — Load all relevant datasets
# ---------------------------------------------------------------------------
print("=" * 70)
print("  NORTHGATE PROBLEMATIC EVENT AUDIT")
print("=" * 70)

print("\n[1] Loading datasets ...")

# Charging events (representative day)
ev = pd.read_csv(EVENTS_FILE)
ev["arrival_time"]   = pd.to_datetime(ev["arrival_time"],   utc=True, errors="coerce")
ev["departure_time"] = pd.to_datetime(ev["departure_time"], utc=True, errors="coerce")
print(f"    Events file         : {len(ev)} rows")

# Feasibility results
feas = pd.read_csv(FEASIBILITY_FILE)
infeasible_ids = set(feas.loc[~feas["individually_feasible"], "charging_event_id"])
print(f"    Feasibility file    : {len(feas)} rows, {len(infeasible_ids)} infeasible")

# Source fallback file (ALL departures, Aug–Dec 2025)
print(f"    Loading source fallback Excel (may take a moment) ...")
src = pd.read_excel(FALLBACK_FILE, sheet_name="All Departures")
src["zone_entry_time_utc"] = pd.to_datetime(src["zone_entry_time_utc"], utc=True, errors="coerce")
src["zone_exit_time_utc"]  = pd.to_datetime(src["zone_exit_time_utc"],  utc=True, errors="coerce")
src["_visit_date"]         = src["zone_entry_time_utc"].dt.date
print(f"    Source fallback     : {len(src):,} rows  x  {src.shape[1]} columns")

# Arrivals file
try:
    arr = pd.read_csv(ARRIVALS_FILE)
    arr["zone_entry_time_utc"] = pd.to_datetime(arr["zone_entry_time_utc"], utc=True, errors="coerce")
    arr["zone_exit_time_utc"]  = pd.to_datetime(arr["zone_exit_time_utc"],  utc=True, errors="coerce")
    print(f"    Arrivals file       : {len(arr):,} rows | cols: {arr.columns.tolist()}")
    has_arrivals = True
except Exception as exc:
    print(f"    Arrivals file       : could not load ({exc})")
    has_arrivals = False

# Daily energy file for context
daily = pd.read_csv(DAILY_FILE)
daily["date"] = pd.to_datetime(daily["date"]).dt.date
print(f"    Daily energy file   : {len(daily)} dates")

# ---------------------------------------------------------------------------
# Step 2 — Identify problematic events
# ---------------------------------------------------------------------------
print("\n[2] Identifying problematic events ...")

REP_DATE = str(ev["arrival_time"].dt.date.value_counts().idxmax())
print(f"    Representative date: {REP_DATE}")

# Criteria (applied to the original 44 events, including zero-dwell excluded ones)
# Re-apply imputation to get complete departure_time
orig_missing_dep = ev["departure_time"].isna()
can_imp = orig_missing_dep & ev["dwell_hours"].notna() & ev["arrival_time"].notna()
ev.loc[can_imp, "departure_time"] = (
    ev.loc[can_imp, "arrival_time"]
    + pd.to_timedelta(ev.loc[can_imp, "dwell_hours"], unit="h")
)

ev["_dwell_ok"]     = ev["dwell_hours"] > 0
ev["_dep_gt_arr"]   = ev["departure_time"] > ev["arrival_time"]
ev["_is_zero_dwell"]= ev["dwell_hours"].fillna(0) == 0
ev["_is_short_dwell"]= (ev["dwell_hours"].fillna(0) > 0) & (ev["dwell_hours"].fillna(0) < MIN_DWELL_THRESH)
ev["_is_infeasible"] = ev["charging_event_id"].isin(infeasible_ids)
ev["_high_energy_short_dwell"] = (
    (ev["energy_needed_kwh_for_visit"].fillna(0) > 50) &
    (ev["dwell_hours"].fillna(0) < MIN_DWELL_THRESH)
)

prob_mask = (
    ev["_is_zero_dwell"]
    | ev["_is_short_dwell"]
    | ev["_is_infeasible"]
    | ev["_high_energy_short_dwell"]
)
ev_prob = ev[prob_mask].copy().reset_index(drop=True)
print(f"    Total events (all 44)         : {len(ev)}")
print(f"    Zero-dwell (dwell = 0)        : {ev['_is_zero_dwell'].sum()}")
print(f"    Short-dwell (0 < dwell < 0.25h): {ev['_is_short_dwell'].sum()}")
print(f"    Infeasible (Step 2 check)     : {len(infeasible_ids)}")
print(f"    High-energy + short dwell     : {ev['_high_energy_short_dwell'].sum()}")
print(f"    Problematic (union)           : {len(ev_prob)}")

# ---------------------------------------------------------------------------
# Step 3 — Trace each problematic event back to source row
# ---------------------------------------------------------------------------
print("\n[3] Tracing back to source fallback file ...")

# Source rows for the rep date (all visits, not just filtered)
src_repdate = src[src["_visit_date"].astype(str) == REP_DATE].copy()
print(f"    Source rows on {REP_DATE}: {len(src_repdate)}")

# Show column list of source file
print(f"    Source columns: {src.columns.tolist()}")

# Build a lookup from source: device_name + zone_entry_time (rounded to nearest second)
def _ts_key(ts):
    if pd.isna(ts):
        return None
    return str(ts)[:19]   # "YYYY-MM-DD HH:MM:SS"

src_repdate["_key"] = (
    src_repdate["device_name"].astype(str) + "|"
    + src_repdate["zone_entry_time_utc"].apply(_ts_key)
)
src_lookup = src_repdate.set_index("_key")

# For each problematic event, find its source row
audit_rows = []
for _, row in ev_prob.iterrows():
    key = str(row["vehicle_id"]) + "|" + _ts_key(row["arrival_time"])

    # Try exact match first, then nearest timestamp match
    src_match = src_lookup.loc[[key]] if key in src_lookup.index else pd.DataFrame()

    if src_match.empty:
        # Try matching by device_name only (closest zone_entry_time_utc)
        dev_rows = src_repdate[src_repdate["device_name"].astype(str) == str(row["vehicle_id"])]
        if not dev_rows.empty and pd.notna(row["arrival_time"]):
            diffs = (dev_rows["zone_entry_time_utc"] - row["arrival_time"]).abs()
            best_idx = diffs.idxmin()
            if diffs[best_idx].total_seconds() < 120:   # within 2 minutes
                src_match = dev_rows.loc[[best_idx]]

    # Build audit record
    if src_match.empty:
        src_row = {}
        match_status = "NOT FOUND IN SOURCE"
    else:
        src_row = src_match.iloc[0].to_dict()
        match_status = "MATCHED"

    # Determine classification
    dwell = float(row["dwell_hours"]) if pd.notna(row["dwell_hours"]) else 0.0
    energy = float(row["energy_needed_kwh_for_visit"]) if pd.notna(row["energy_needed_kwh_for_visit"]) else 0.0
    chain_end = str(src_row.get("chain_end_reason", "")) if src_row else ""
    chain_miles = float(src_row.get("total_chain_miles", 0) or 0) if src_row else 0.0
    inbound_miles = src_row.get("inbound_chain_miles_to_northgate", None) if src_row else None
    fill_source = str(src_row.get("fill_kwh_source", "")) if src_row else ""

    # Classification logic
    if dwell == 0:
        if chain_end in ("no_trips_remaining", "returned_to_origin", ""):
            classification = "A_drive_through_or_GPS_ping"
            classification_note = (
                "Zero dwell: Geotab returned activeFrom==activeTo. "
                f"chain_end_reason='{chain_end}'. Likely GPS zone-boundary oscillation "
                "or brief pass-through. NOT a real charging opportunity."
            )
        else:
            classification = "D_zero_dwell_with_outbound_chain"
            classification_note = (
                f"Zero dwell but chain exists (chain_end='{chain_end}', "
                f"chain_miles={chain_miles:.1f} mi). Vehicle exited immediately and started a trip. "
                "Charging window is 0 — energy cannot be delivered."
            )
    elif dwell < 0.05:
        classification = "A_drive_through_or_GPS_ping"
        classification_note = (
            f"Near-zero dwell ({dwell:.3f}h = {dwell*60:.1f} min). "
            f"chain_end='{chain_end}'. Effectively a pass-through; "
            "no practical charging opportunity."
        )
    elif dwell < MIN_DWELL_THRESH:
        # Has some dwell but too short; check if there's a later visit by same vehicle
        other_visits = ev[
            (ev["vehicle_id"] == row["vehicle_id"]) &
            (ev["charging_event_id"] != row["charging_event_id"])
        ]
        if len(other_visits) > 0:
            later_dwell = other_visits["dwell_hours"].max()
            classification = "F_short_dwell_vehicle_returned_later"
            classification_note = (
                f"Short dwell ({dwell:.3f}h = {dwell*60:.1f} min) but vehicle has "
                f"{len(other_visits)} other visit(s) on the same day "
                f"(max later dwell = {later_dwell:.2f}h). "
                "Energy may be servable during a longer return visit."
            )
        else:
            classification = "E_short_dwell_single_visit"
            classification_note = (
                f"Short dwell ({dwell:.3f}h = {dwell*60:.1f} min), only visit of the day. "
                "Energy demand cannot be met in this window."
            )
    else:
        classification = "CHECK_MANUALLY"
        classification_note = f"Dwell={dwell:.3f}h, energy={energy:.1f} kWh. Review needed."

    audit_rows.append({
        # Event identifiers
        "charging_event_id":            row["charging_event_id"],
        "vehicle_id":                   row["vehicle_id"],
        "ev_equivalent_model":          row["ev_equivalent_model"],
        "visit_sequence":               row.get("visit_sequence_for_vehicle_that_day", ""),
        # Problem flags
        "is_zero_dwell":                bool(row["_is_zero_dwell"]),
        "is_short_dwell":               bool(row["_is_short_dwell"]),
        "is_infeasible_benchmark":      bool(row["_is_infeasible"]),
        "is_high_energy_short_dwell":   bool(row["_high_energy_short_dwell"]),
        # Charging event timestamps (from charger-sizing dataset)
        "event_arrival_time_utc":       str(row["arrival_time"])[:19] if pd.notna(row["arrival_time"]) else "",
        "event_departure_time_utc":     str(row["departure_time"])[:19] if pd.notna(row["departure_time"]) else "",
        "event_dwell_hours":            round(dwell, 4),
        "event_dwell_minutes":          round(dwell * 60, 1),
        "energy_needed_kwh":            round(energy, 3),
        # Source file columns (original)
        "source_match_status":          match_status,
        "src_zone_entry_time_utc":      str(src_row.get("zone_entry_time_utc", ""))[:19] if src_row else "",
        "src_zone_exit_time_utc":       str(src_row.get("zone_exit_time_utc",  ""))[:19] if src_row else "",
        "src_dwell_hrs":                src_row.get("dwell_hrs", "") if src_row else "",
        "src_chain_end_reason":         chain_end,
        "src_total_chain_miles":        src_row.get("total_chain_miles", "") if src_row else "",
        "src_inbound_chain_miles":      inbound_miles if src_row else "",
        "src_outbound_energy_need_kwh": src_row.get("outbound_energy_need_kwh", "") if src_row else "",
        "src_northgate_fill_kwh":       src_row.get("northgate_fill_kwh", "") if src_row else "",
        "src_battery_capacity_kwh":     src_row.get("battery_capacity_kwh", "") if src_row else "",
        "src_fill_kwh_source":          fill_source,
        "src_prev_eligible_zone":       src_row.get("prev_eligible_zone_name", "") if src_row else "",
        "src_dest_zone_name":           src_row.get("dest_zone_name", "") if src_row else "",
        "src_dest_arrival_time_utc":    str(src_row.get("dest_arrival_time_utc", ""))[:19] if src_row else "",
        # Benchmark feasibility
        "max_dc_charge_kw":             row.get("max_dc_charge_kw", ""),
        "effective_power_350kw_dc":     min(BENCHMARK_DC_KW, float(row["max_dc_charge_kw"])) if pd.notna(row.get("max_dc_charge_kw")) else 0,
        "max_possible_kwh_350kw":       round(ETA * min(BENCHMARK_DC_KW, float(row["max_dc_charge_kw"])) * dwell, 2) if pd.notna(row.get("max_dc_charge_kw")) else 0,
        "individually_feasible":        row["charging_event_id"] not in infeasible_ids,
        # Classification
        "classification":               classification,
        "classification_note":          classification_note,
    })

    print(f"    {row['charging_event_id']:45s}  dwell={dwell:.3f}h  energy={energy:.1f}kWh  "
          f"class={classification.split('_')[0]}")

audit_df = pd.DataFrame(audit_rows)

# ---------------------------------------------------------------------------
# Step 4 — Cross-reference with arrivals file
# ---------------------------------------------------------------------------
if has_arrivals:
    print("\n[4] Cross-referencing with arrivals file ...")
    # Show arrivals file columns
    print(f"    Arrivals columns: {arr.columns.tolist()}")

    arr_repdate = arr[arr["zone_entry_time_utc"].dt.date.astype(str) == REP_DATE].copy()
    print(f"    Arrivals on {REP_DATE}: {len(arr_repdate)}")

    # For each problematic vehicle, check if arrivals file has a different dwell record
    arr_repdate["_key"] = arr_repdate["device_name"].astype(str)
    arr_lookup = arr_repdate.groupby("_key")

    arr_notes = []
    for _, row in audit_df.iterrows():
        vid = str(row["vehicle_id"])
        if vid in arr_lookup.groups:
            dev_arrs = arr_lookup.get_group(vid).sort_values("zone_entry_time_utc")
            n_arr = len(dev_arrs)
            dwell_values = dev_arrs["dwell_hrs"].tolist() if "dwell_hrs" in dev_arrs.columns else []
            arr_notes.append(
                f"Found {n_arr} arrival record(s); "
                f"dwell_hrs={[round(d,3) for d in dwell_values if pd.notna(d)]}"
            )
        else:
            arr_notes.append("Not found in arrivals file on this date")

    audit_df["arrivals_file_note"] = arr_notes
    print(f"    Arrivals cross-reference complete.")
else:
    audit_df["arrivals_file_note"] = "arrivals file not loaded"

# ---------------------------------------------------------------------------
# Step 5 — Check top-5 alternative days for zero/short dwell prevalence
# ---------------------------------------------------------------------------
print("\n[5] Checking zero/short-dwell prevalence across top 5 days ...")

top5_dates = daily.head(5)["date"].tolist()
day_quality = []
for d in top5_dates:
    d_str = str(d)
    src_day = src[src["_visit_date"].astype(str) == d_str]
    n_total = len(src_day)
    n_zero  = int((src_day["dwell_hrs"].fillna(0) == 0).sum())
    n_short = int(((src_day["dwell_hrs"].fillna(0) > 0) & (src_day["dwell_hrs"].fillna(0) < 0.25)).sum())
    has_fill = src_day["northgate_fill_kwh"].notna() & (src_day["northgate_fill_kwh"] > 0)
    n_ev = int(has_fill.sum())
    zero_kwh = float(src_day.loc[src_day["dwell_hrs"].fillna(0) == 0, "northgate_fill_kwh"].fillna(0).sum())
    total_kwh = float(daily[daily["date"] == d]["total_fill_kwh"].values[0]) if d in daily["date"].values else 0.0
    day_quality.append({
        "date":              d_str,
        "total_fill_kwh":    round(total_kwh, 1),
        "ev_depart_events":  n_ev,
        "zero_dwell_events": n_zero,
        "short_dwell_events":n_short,
        "kwh_at_risk_zero_dwell": round(zero_kwh, 1),
        "pct_kwh_zero_dwell": round(zero_kwh / total_kwh * 100, 1) if total_kwh > 0 else 0,
    })
    print(f"    {d_str}: total={n_total}  ev={n_ev}  zero_dwell={n_zero}  "
          f"short_dwell={n_short}  zero_dwell_kwh={zero_kwh:.0f}  "
          f"pct={zero_kwh/total_kwh*100:.1f}%  total_kwh={total_kwh:.0f}")

dq_df = pd.DataFrame(day_quality)

# ---------------------------------------------------------------------------
# Step 6 — Check dwell_hours distribution for the representative day
# ---------------------------------------------------------------------------
print(f"\n[6] Dwell distribution for {REP_DATE} (all EV events in source file) ...")
src_repdate_ev = src_repdate[src_repdate["northgate_fill_kwh"].notna() & (src_repdate["northgate_fill_kwh"] > 0)]
bins = [0, 0.001, 0.05, 0.25, 0.5, 1.0, 2.0, 4.0, float("inf")]
labels = ["0h", "<3min", "3-15min", "15-30min", "0.5-1h", "1-2h", "2-4h", ">4h"]
src_repdate_ev = src_repdate_ev.copy()
src_repdate_ev["dwell_bin"] = pd.cut(src_repdate_ev["dwell_hrs"].fillna(0), bins=bins, labels=labels, right=False)
dwell_dist = src_repdate_ev.groupby("dwell_bin", observed=False).agg(
    events=("dwell_hrs", "count"),
    total_kwh=("northgate_fill_kwh", "sum")
).reset_index()
dwell_dist["total_kwh"] = dwell_dist["total_kwh"].round(1)
print(dwell_dist.to_string(index=False))

# ---------------------------------------------------------------------------
# Step 7 — Write outputs
# ---------------------------------------------------------------------------
print("\n[7] Writing audit outputs ...")
audit_df.to_csv(AUDIT_FILE, index=False, encoding="utf-8-sig")
print(f"    Audit file -> {AUDIT_FILE.name}  ({len(audit_df)} rows)")

# Summary
classifications = audit_df["classification"].value_counts().reset_index()
classifications.columns = ["classification", "event_count"]
kwh_by_class = audit_df.groupby("classification")["energy_needed_kwh"].sum().reset_index()
kwh_by_class.columns = ["classification", "total_energy_kwh_at_risk"]
kwh_by_class["total_energy_kwh_at_risk"] = kwh_by_class["total_energy_kwh_at_risk"].round(1)
class_summary = classifications.merge(kwh_by_class, on="classification")

total_prob_kwh = audit_df["energy_needed_kwh"].sum()
n_zero   = int(audit_df["is_zero_dwell"].sum())
n_short  = int(audit_df["is_short_dwell"].sum())
n_infeas = int(audit_df["is_infeasible_benchmark"].sum())
n_he_sd  = int(audit_df["is_high_energy_short_dwell"].sum())

summary_rows = [{
    "representative_date":                REP_DATE,
    "total_events_in_dataset":            len(ev),
    "problematic_events_total":           len(audit_df),
    "zero_dwell_events":                  n_zero,
    "short_dwell_events_0_to_0p25h":      n_short,
    "individually_infeasible_events":     n_infeas,
    "high_energy_short_dwell_events":     n_he_sd,
    "total_energy_at_risk_kwh":           round(total_prob_kwh, 1),
    "total_day_method_c_energy_kwh":      round(daily[daily["date"].astype(str) == REP_DATE]["total_fill_kwh"].values[0], 1) if REP_DATE in daily["date"].astype(str).values else 0,
    "pct_day_energy_at_risk":             round(total_prob_kwh / daily[daily["date"].astype(str) == REP_DATE]["total_fill_kwh"].values[0] * 100, 1) if REP_DATE in daily["date"].astype(str).values else 0,
    # Timestamp semantics (confirmed)
    "zone_entry_time_utc_meaning":        "Vehicle ENTERS Northgate zone (Geotab activeFrom)",
    "zone_exit_time_utc_meaning":         "Vehicle EXITS Northgate zone (Geotab activeTo)",
    "dwell_hrs_formula":                  "(zone_exit - zone_entry) / 3600",
    "method_c_energy_is_for":             "Outbound trip AFTER zone_exit_time_utc",
    "arrival_time_mapping_correct":       "YES — arrival_time = zone_entry_time_utc",
    "departure_time_mapping_correct":     "YES — departure_time = zone_exit_time_utc (or imputed)",
    "charging_window_definition_correct": "YES — charging window = dwell_hrs between entry and exit",
    # Zero-dwell root cause
    "zero_dwell_root_cause":              "Geotab returned activeFrom==activeTo for zone visit (boundary ping/drive-through)",
    "zero_dwell_handled_in_fetch_script": "NO — zero-dwell rows pass through with no filtering",
    # Day quality comparison
    "day_quality_table":                  dq_df.to_json(orient="records"),
}]
sum_df = pd.DataFrame(summary_rows)

# Also append dwell distribution table and alternative day table to summary
alt_days_str = dq_df.to_string(index=False)
class_str    = class_summary.to_string(index=False)

# Create a multi-section summary CSV (two separate DataFrames written with separators)
with open(SUMMARY_FILE, "w", encoding="utf-8-sig", newline="") as f:
    f.write("=== AUDIT SUMMARY ===\n")
    sum_df.drop(columns=["day_quality_table"]).T.to_csv(f, header=False)
    f.write("\n=== CLASSIFICATION BREAKDOWN ===\n")
    class_summary.to_csv(f, index=False)
    f.write("\n=== DWELL DISTRIBUTION (rep day, EV events with fill_kwh > 0) ===\n")
    dwell_dist.to_csv(f, index=False)
    f.write("\n=== TOP-5 ALTERNATIVE DAYS: ZERO/SHORT DWELL PREVALENCE ===\n")
    dq_df.to_csv(f, index=False)

print(f"    Summary file -> {SUMMARY_FILE.name}")

# ---------------------------------------------------------------------------
# Step 8 — Console report
# ---------------------------------------------------------------------------
print()
print("=" * 70)
print("  AUDIT REPORT")
print("=" * 70)
print(f"""
TIMESTAMP SEMANTICS (confirmed from code):
  zone_entry_time_utc  = Vehicle ENTERS Northgate zone (Geotab activeFrom)
  zone_exit_time_utc   = Vehicle EXITS  Northgate zone (Geotab activeTo)
  dwell_hrs            = (exit - entry) / 3600   [no special handling for zero]
  Method C energy      = for OUTBOUND trip AFTER zone_exit_time_utc
  Charging window      = zone_entry -> zone_exit = dwell_hrs   [CORRECT MAPPING]

PROBLEMATIC EVENT SUMMARY:
  Zero-dwell (dwell = 0)           : {n_zero}
  Short-dwell (0 < dwell < 0.25h)  : {n_short}
  Infeasible (350 kW DC benchmark) : {n_infeas}
  High-energy + short dwell        : {n_he_sd}
  Total problematic (union)        : {len(audit_df)}
  Total energy at risk             : {round(total_prob_kwh, 1)} kWh
""")

print("  CLASSIFICATION BREAKDOWN:")
for _, cr in class_summary.iterrows():
    print(f"    {cr['classification']:45s}  {cr['event_count']} events  {cr['total_energy_kwh_at_risk']:.1f} kWh")

print(f"\n  DWELL DISTRIBUTION (rep day, EV events):")
print(dwell_dist.to_string(index=False))

print(f"\n  TOP-5 DAYS — ZERO/SHORT DWELL PREVALENCE:")
print(dq_df.to_string(index=False))
print()
print("  Output files:")
print(f"    {AUDIT_FILE}")
print(f"    {SUMMARY_FILE}")
print()
print("=" * 70)
print("  AUDIT COMPLETE — awaiting recommendation decision")
print("=" * 70)
