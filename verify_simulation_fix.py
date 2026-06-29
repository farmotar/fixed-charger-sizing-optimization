"""
verify_simulation_fix.py
─────────────────────────
Verifies that the prorated-energy fix in xos_hub_soc_simulation.py works correctly.

The audit_dispatch_violations.py script checks dispatch_log["time_utc"] (step start times)
against arrival times. Step start times will ALWAYS precede arrival times by 0–14.9 min
because the simulation activates a vehicle in the step that CONTAINS its arrival —
this is a step-discretization fact that does not change with the energy fix.

The energy fix changes WHAT IS DELIVERED in boundary steps, not when steps occur.
This script verifies the fix by checking:

  1. First-step energy: for each vehicle, the first dispatch step should deliver
     P_PORT * overlap_fraction * ETA_D kWh, not the full DT_H=0.25h dose.
  2. Overall accuracy: total energy delivered == energy needed for all served vehicles.

Run on 2026-02-26 and 2025-09-25 as representative sample days.
"""
from __future__ import annotations

import sys, importlib, io, contextlib
from pathlib import Path
import pandas as pd
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
sys.path.insert(0, str(BASE_DIR))
xos = importlib.import_module("xos_hub_soc_simulation")

P_PORT = xos.P_PORT_KW    # 80 kW
ETA_D  = xos.ETA_D        # 0.95
DT_H   = xos.DT_H         # 0.25 h  (15 min)
DT15   = pd.Timedelta(minutes=15)
FULL_STEP_KWH = P_PORT * DT_H * ETA_D   # 19.0 kWh if full 15-min step

SAMPLE_CSVS = [
    BASE_DIR / "z2z_milp_events_northgate_2026_02_26.csv",
    BASE_DIR / "z2z_milp_events_northgate_2025_09_25.csv",
]


def compute_extensions(events_df: pd.DataFrame):
    rows = []
    for _, row in events_df.iterrows():
        v      = row["charging_event_id"]
        arr    = row["arrival_time"]
        dep    = row["departure_time"]
        e_need = float(row["energy_needed_kwh_for_visit"])
        dwell_h = (dep - arr).total_seconds() / 3600.0
        req_h   = e_need / (P_PORT * ETA_D)
        extra_h = max(0.0, req_h - dwell_h)
        ext_dep = dep + pd.Timedelta(hours=extra_h) if extra_h > 1e-6 else dep
        rows.append({"charging_event_id": v, "arr": arr, "dep": dep, "ext_dep": ext_dep})
    meta = pd.DataFrame(rows).set_index("charging_event_id")
    ext_ev = events_df.copy()
    for idx, row in ext_ev.iterrows():
        v = row["charging_event_id"]
        if meta.loc[v, "ext_dep"] != meta.loc[v, "dep"]:
            ext_ev.at[idx, "departure_time"] = meta.loc[v, "ext_dep"]
    return ext_ev, meta


def run_silent(events_df, p_eff):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        k, result = xos.find_min_xos_units(events_df, p_eff)
    return k, result


W = 110
for csv_path in SAMPLE_CSVS:
    date_str = csv_path.stem.split("northgate_")[-1].replace("_", "-")
    print(f"\n{'='*W}")
    print(f"  VERIFICATION — {date_str}")
    print(f"{'='*W}")

    events_df = xos.load_events(csv_path)
    p_eff     = xos.compute_p_eff(events_df)
    ext_ev, meta = compute_extensions(events_df)
    p_eff_ext = xos.compute_p_eff(ext_ev)

    k, res = run_silent(ext_ev, p_eff_ext)

    log = res["dispatch_log"]

    # Build per-vehicle first and last step info
    first_step: dict = {}
    last_step: dict  = {}
    for entry in log:
        v   = entry["event_id"]
        t   = pd.Timestamp(entry["time_utc"])
        e   = entry["energy_to_vehicle_kwh"]
        pv  = entry["power_kw"]
        if v not in first_step or t < first_step[v]["t"]:
            first_step[v] = {"t": t, "e": e, "pv": pv}
        if v not in last_step or t > last_step[v]["t"]:
            last_step[v]  = {"t": t, "e": e, "pv": pv}

    # ── FIRST STEP VERIFICATION ───────────────────────────────────────────────
    print(f"\n  FIRST-STEP ENERGY VERIFICATION (should be prorated, not full 19.0 kWh)")
    print(f"  {'Vehicle':<30} {'Arrival':>16} {'Step start':>16} "
          f"{'Overlap min':>12} {'Expected kWh':>13} {'Actual kWh':>12} {'Match':>6}")
    print(f"  {'-'*(W-2)}")

    ok_count = err_count = 0
    for v, fs in sorted(first_step.items(), key=lambda x: x[1]["t"]):
        arr    = meta.loc[v, "arr"]
        edep   = meta.loc[v, "ext_dep"]
        t      = fs["t"]
        t_next = t + DT15
        # Actual overlap for the first step
        overlap_start = max(t, arr)
        overlap_end   = min(t_next, edep)
        overlap_h     = max(0.0, (overlap_end - overlap_start).total_seconds() / 3600.0)
        expected_e    = round(fs["pv"] * overlap_h * ETA_D, 4)
        actual_e      = round(fs["e"], 4)
        tol           = 0.01   # kWh
        match         = abs(expected_e - actual_e) <= tol
        if match:
            ok_count  += 1
        else:
            err_count += 1
        overlap_min = overlap_h * 60
        marker      = "OK" if match else "FAIL"
        print(f"  {str(v)[-28:]:<30} "
              f"{str(arr)[:16]:>16} {str(t)[:16]:>16} "
              f"{overlap_min:>11.1f}m {expected_e:>13.4f} {actual_e:>12.4f} {marker:>6}")

    print(f"\n  First-step check: {ok_count} OK, {err_count} FAIL "
          f"(out of {ok_count+err_count} dispatched vehicles)")

    # ── LAST STEP VERIFICATION ────────────────────────────────────────────────
    print(f"\n  LAST-STEP ENERGY VERIFICATION (partial last steps should also be prorated)")
    print(f"  {'Vehicle':<30} {'Ext dep':>16} {'Step start':>16} "
          f"{'Overlap min':>12} {'Expected kWh':>13} {'Actual kWh':>12} {'Match':>6}")
    print(f"  {'-'*(W-2)}")

    ok2 = err2 = 0
    for v, ls in sorted(last_step.items(), key=lambda x: x[1]["t"]):
        arr    = meta.loc[v, "arr"]
        edep   = meta.loc[v, "ext_dep"]
        t      = ls["t"]
        t_next = t + DT15
        overlap_start = max(t, arr)
        overlap_end   = min(t_next, edep)
        overlap_h     = max(0.0, (overlap_end - overlap_start).total_seconds() / 3600.0)
        expected_e    = round(ls["pv"] * overlap_h * ETA_D, 4)
        actual_e      = round(ls["e"], 4)
        # For the last step: actual may be less than expected if vehicle was
        # fully charged before step_end — that's correct behaviour.
        # A FAIL is only if actual > expected (overshoot, impossible after fix)
        # or actual < 0 (nonsensical).
        overshot = actual_e > expected_e + 0.01
        ok2     += 0 if overshot else 1
        err2    += 1 if overshot else 0
        overlap_min = overlap_h * 60
        marker = "FAIL(overshoot)" if overshot else "OK"
        print(f"  {str(v)[-28:]:<30} "
              f"{str(edep)[:16]:>16} {str(t)[:16]:>16} "
              f"{overlap_min:>11.1f}m {expected_e:>13.4f} {actual_e:>12.4f} {marker:>6}")

    print(f"\n  Last-step check: {ok2} OK, {err2} FAIL (overshoot = simulation bug)")

    # ── SUMMARY ───────────────────────────────────────────────────────────────
    print(f"\n  ENERGY BALANCE SUMMARY:")
    total_needed   = sum(float(r["energy_needed_kwh_for_visit"]) for _, r in events_df.iterrows())
    total_del      = sum(res["delivered"].values())
    unmet          = {v: rem for v, rem in res["remaining"].items() if rem > xos.ENERGY_TOL}
    print(f"    Total energy required   : {total_needed:>10.1f} kWh")
    print(f"    Total energy delivered  : {total_del:>10.1f} kWh")
    print(f"    Vehicles fully served   : {k} units needed, {res['n_served']}/{res['n_total']} served")
    print(f"    Vehicles with unmet need: {len(unmet)}")
    print(f"    All first-steps prorated: {'YES — fix confirmed' if err_count==0 else f'NO — {err_count} failures'}")
    print(f"    No last-step overshoot  : {'YES — fix confirmed' if err2==0 else f'NO — {err2} overshoots'}")

print(f"\n{'='*W}")
print("  AUDIT NOTE: The original audit_dispatch_violations.py checks dispatch_log")
print("  step-start TIMESTAMPS against arrival times. Step starts will always precede")
print("  arrival by 0–14.9 min regardless of the energy fix — this is by design.")
print("  To verify the fix, check energy_to_vehicle_kwh for boundary steps (above).")
print(f"{'='*W}\n")
