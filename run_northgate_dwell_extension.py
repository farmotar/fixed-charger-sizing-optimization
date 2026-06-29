"""
run_northgate_dwell_extension.py
---------------------------------
For every time_window_infeasible vehicle in the 5 Northgate preview days:
  1. Compute required dwell extension so the vehicle can receive full energy at
     one 80-kW XOS port:
       required_dwell_h = energy_needed_kwh / (80 * 0.95)
       extra_dwell_h    = max(0, required_dwell_h - current_dwell_h)
  2. Re-run the XOS simulation with extended departure times.
  3. Enrich the event-level CSV with the new columns.
  4. Print per-vehicle extension table and before/after daily summary.
"""
from __future__ import annotations

import sys, importlib
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
OUT_DIR  = BASE_DIR / "site_outputs" / "northgate"
OUT_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(BASE_DIR))
xos = importlib.import_module("xos_hub_soc_simulation")

P_PORT = xos.P_PORT_KW   # 80 kW
ETA_D  = xos.ETA_D       # 0.95
ETOL   = xos.ENERGY_TOL  # 0.10 kWh

ALL_CSVS = sorted(BASE_DIR.glob("z2z_milp_events_northgate_*.csv"))[:5]

# ─────────────────────────────────────────────────────────────────────────────
def compute_extensions(events_df: pd.DataFrame, result_orig: dict) -> pd.DataFrame:
    """
    Add dwell-extension columns to events_df.
    Returns a copy with the new columns and a modified departure_time for
    vehicles that need extension (for use in the re-run simulation).
    """
    rows = []
    for _, row in events_df.iterrows():
        v       = row["charging_event_id"]
        arr     = row["arrival_time"]
        dep     = row["departure_time"]
        e_need  = float(row["energy_needed_kwh_for_visit"])

        dwell_h    = (dep - arr).total_seconds() / 3600.0
        req_dwell  = e_need / (P_PORT * ETA_D)          # hours needed for full charge
        extra_h    = max(0.0, req_dwell - dwell_h)
        extended   = extra_h > 1e-6
        ext_dep    = dep + pd.Timedelta(hours=extra_h) if extended else dep

        rem_orig = result_orig["remaining"].get(v, e_need)
        served_orig = rem_orig <= ETOL

        rows.append({
            "charging_event_id":               v,
            "current_dwell_hours":             round(dwell_h,   4),
            "energy_needed_kwh":               round(e_need,    3),
            "required_dwell_hours_for_full_charge": round(req_dwell, 4),
            "extra_dwell_hours_needed":        round(extra_h,   4),
            "original_departure_time":         dep,
            "extended_departure_time":         ext_dep,
            "was_dwell_extended":              extended,
            "served_original":                 served_orig,
        })

    ext_df = pd.DataFrame(rows)

    # Merge into events_df copy, replacing departure_time for extended vehicles
    merged = events_df.copy()
    merged = merged.merge(ext_df, on="charging_event_id", how="left")
    merged["departure_time"] = merged.apply(
        lambda r: r["extended_departure_time"] if r["was_dwell_extended"] else r["departure_time"],
        axis=1,
    )
    return merged, ext_df


# ─────────────────────────────────────────────────────────────────────────────
summary_rows  = []
extension_log = []   # per-vehicle rows for the extension table

for csv_path in ALL_CSVS:
    date_tag = csv_path.stem.split("_events_")[-1]   # "northgate_2025_05_08"
    date_str = date_tag.replace("northgate_", "").replace("_", "-")
    xos_out  = OUT_DIR / f"xos_{date_tag}"
    xos_out.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*70}")
    print(f"  {date_str}")
    print(f"{'='*70}")

    # ── 1. Original simulation ──────────────────────────────────────────────
    events_df = xos.load_events(csv_path)
    p_eff     = xos.compute_p_eff(events_df)

    print("  [Original] ", end="", flush=True)
    n_orig, res_orig = xos.find_min_xos_units(events_df, p_eff)
    n_served_orig = res_orig["n_served"]
    n_total       = res_orig["n_total"]
    print(f"  -> {n_orig} units | {n_served_orig}/{n_total} served")

    # ── 2. Compute extensions ───────────────────────────────────────────────
    ext_events_df, ext_meta_df = compute_extensions(events_df, res_orig)

    n_extended = int(ext_meta_df["was_dwell_extended"].sum())
    avg_extra  = ext_meta_df.loc[ext_meta_df["was_dwell_extended"], "extra_dwell_hours_needed"].mean()
    max_extra  = ext_meta_df["extra_dwell_hours_needed"].max()

    # ── 3. Extended simulation ──────────────────────────────────────────────
    p_eff_ext = xos.compute_p_eff(ext_events_df)

    print("  [Extended] ", end="", flush=True)
    n_ext, res_ext = xos.find_min_xos_units(ext_events_df, p_eff_ext)
    n_served_ext = res_ext["n_served"]
    print(f"  -> {n_ext} units | {n_served_ext}/{n_total} served")

    # ── 4. Enrich and save event CSV ────────────────────────────────────────
    # Build combined event table with original + extension + extended-sim columns
    ev_rows = []
    for _, row in events_df.iterrows():
        v       = row["charging_event_id"]
        arr     = row["arrival_time"]
        dep     = row["departure_time"]
        e_need  = float(row["energy_needed_kwh_for_visit"])

        dwell_h   = (dep - arr).total_seconds() / 3600.0
        req_dwell = e_need / (P_PORT * ETA_D)
        extra_h   = max(0.0, req_dwell - dwell_h)
        extended  = extra_h > 1e-6
        ext_dep   = dep + pd.Timedelta(hours=extra_h) if extended else dep

        max_del   = dwell_h * P_PORT * ETA_D
        phys_ok   = e_need <= max_del + ETOL

        rem_orig   = res_orig["remaining"].get(v, e_need)
        rem_ext    = res_ext["remaining"].get(v, e_need)
        srv_orig   = rem_orig <= ETOL
        srv_ext    = rem_ext  <= ETOL

        if srv_orig:
            reason = "served"
        elif not phys_ok:
            reason = "time_window_infeasible"
        else:
            reason = "scheduler_or_energy_limited"

        ev_rows.append({
            "charging_event_id":                    v,
            "vehicle_id":                           row.get("vehicle_id", ""),
            "ev_equivalent_model":                  row.get("ev_equivalent_model", ""),
            "arrival_time":                         arr,
            "original_departure_time":              dep,
            "current_dwell_hours":                  round(dwell_h,   4),
            "energy_needed_kwh":                    round(e_need,    3),
            "max_deliverable_kwh_at_80kw":          round(max_del,   3),
            "physically_serviceable":               phys_ok,
            "required_dwell_hours_for_full_charge": round(req_dwell, 4),
            "extra_dwell_hours_needed":             round(extra_h,   4),
            "extended_departure_time":              ext_dep,
            "was_dwell_extended":                   extended,
            "served_by_simulation_original":        srv_orig,
            "served_by_simulation_extended":        srv_ext,
            "energy_delivered_original_kwh":        round(res_orig["delivered"].get(v, 0.0), 3),
            "energy_delivered_extended_kwh":        round(res_ext["delivered"].get(v, 0.0),  3),
            "reason_unserved_original":             reason,
        })

    ev_df = pd.DataFrame(ev_rows)
    out_csv = xos_out / f"xos_event_results_with_extension_{date_tag}.csv"
    ev_df.to_csv(out_csv, index=False)
    print(f"  Saved: {out_csv.name}")

    # ── 5. Collect per-vehicle extension rows ──────────────────────────────
    for _, r in ev_df[ev_df["was_dwell_extended"]].iterrows():
        extension_log.append({
            "date":               date_str,
            "event_id":           str(r["charging_event_id"])[-16:],
            "orig_dwell_h":       r["current_dwell_hours"],
            "req_dwell_h":        r["required_dwell_hours_for_full_charge"],
            "extra_h":            r["extra_dwell_hours_needed"],
            "orig_departure":     str(r["original_departure_time"])[:16],
            "ext_departure":      str(r["extended_departure_time"])[:16],
            "energy_needed_kwh":  r["energy_needed_kwh"],
        })

    summary_rows.append({
        "date":           date_str,
        "n_total":        n_total,
        "n_extended":     n_extended,
        "avg_extra_h":    round(avg_extra, 2) if n_extended else 0.0,
        "max_extra_h":    round(max_extra, 2),
        "served_before":  n_served_orig,
        "served_after":   n_served_ext,
        "srv_pct_before": round(100 * n_served_orig / max(n_total, 1), 1),
        "srv_pct_after":  round(100 * n_served_ext  / max(n_total, 1), 1),
    })


# ─────────────────────────────────────────────────────────────────────────────
# PER-VEHICLE EXTENSION TABLE
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n\n{'='*110}")
print("  PER-VEHICLE DWELL EXTENSION TABLE  (time_window_infeasible vehicles only)")
print(f"{'='*110}")
print(f"  {'Date':<12} {'Event ID':<18} {'Curr dwell':>10} {'Req dwell':>10} "
      f"{'Extra h':>8} {'Orig depart':>18} {'Ext depart':>18} {'E_need kWh':>11}")
print(f"  {'-'*106}")

cur_date = None
for r in extension_log:
    if r["date"] != cur_date:
        if cur_date is not None:
            print()
        cur_date = r["date"]
    print(f"  {r['date']:<12} {r['event_id']:<18} "
          f"{r['orig_dwell_h']:>10.2f} {r['req_dwell_h']:>10.2f} "
          f"{r['extra_h']:>8.2f} {r['orig_departure']:>18} "
          f"{r['ext_departure']:>18} {r['energy_needed_kwh']:>11.1f}")

n_ext_total   = len(extension_log)
avg_ext_all   = np.mean([r["extra_h"] for r in extension_log]) if extension_log else 0
max_ext_all   = max((r["extra_h"] for r in extension_log), default=0)
print(f"\n  Total vehicles extended: {n_ext_total}")
print(f"  Average extra dwell needed: {avg_ext_all:.2f} h   Max: {max_ext_all:.2f} h")


# ─────────────────────────────────────────────────────────────────────────────
# DAILY SUMMARY TABLE
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n\n{'='*100}")
print("  DAILY SUMMARY — BEFORE vs AFTER DWELL EXTENSION  (MAX_UNITS=20, 4-port @ 80 kW)")
print(f"{'='*100}")
print(f"  {'Date':<12} {'Total':>6} {'Extended':>9} {'Avg+h':>7} {'Max+h':>7} "
      f"{'Srv(orig)':>10} {'Srv(ext)':>9} {'Srv%(orig)':>11} {'Srv%(ext)':>10}")
print(f"  {'-'*96}")
for r in summary_rows:
    delta = r["srv_pct_after"] - r["srv_pct_before"]
    sign  = "+" if delta >= 0 else ""
    print(f"  {r['date']:<12} {r['n_total']:>6} {r['n_extended']:>9} "
          f"{r['avg_extra_h']:>7.2f} {r['max_extra_h']:>7.2f} "
          f"{r['served_before']:>10} {r['served_after']:>9} "
          f"{r['srv_pct_before']:>10.1f}% {r['srv_pct_after']:>8.1f}%  "
          f"({sign}{delta:.1f}pp)")

print(f"  {'-'*96}")
tot   = sum(r["n_total"]    for r in summary_rows)
next_ = sum(r["n_extended"] for r in summary_rows)
sbf   = sum(r["served_before"] for r in summary_rows)
saft  = sum(r["served_after"]  for r in summary_rows)
aext  = np.mean([r["avg_extra_h"] for r in summary_rows if r["n_extended"] > 0])
mext  = max(r["max_extra_h"] for r in summary_rows)
print(f"  {'TOTAL':<12} {tot:>6} {next_:>9} {aext:>7.2f} {mext:>7.2f} "
      f"{sbf:>10} {saft:>9} "
      f"{100*sbf/max(tot,1):>10.1f}% {100*saft/max(tot,1):>8.1f}%  "
      f"(+{100*(saft-sbf)/max(tot,1):.1f}pp)")
print(f"{'='*100}")
print(f"\nDone: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
