"""
audit_dispatch_violations.py
─────────────────────────────
Runs the CURRENT (unfixed) extended-dwell simulation for all 31 Northgate days
and audits every dispatch interval for timing violations:

  Type A  charge_start < arrival_time        (charging before vehicle arrives)
  Type B  charge_end   > extended_departure  (charging after vehicle leaves)

Both arise from 15-min time-step discretization: a vehicle with arr=10:07 is
marked active in step t=10:00 (because arr < t_next=10:15), and the simulation
currently delivers a FULL 15-min energy dose (19 kWh) even though the vehicle
was physically present for only 8 of those 15 minutes.
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

P_PORT = xos.P_PORT_KW
ETA_D  = xos.ETA_D
ALL_CSVS = sorted(BASE_DIR.glob("z2z_milp_events_northgate_*.csv"))


# ── helpers ──────────────────────────────────────────────────────────────────
def compute_extensions(events_df: pd.DataFrame):
    rows = []
    for _, row in events_df.iterrows():
        v      = row["charging_event_id"]
        arr    = row["arrival_time"]
        dep    = row["departure_time"]
        e_need = float(row["energy_needed_kwh_for_visit"])
        dwell_h  = (dep - arr).total_seconds() / 3600.0
        req_h    = e_need / (P_PORT * ETA_D)
        extra_h  = max(0.0, req_h - dwell_h)
        extended = extra_h > 1e-6
        ext_dep  = dep + pd.Timedelta(hours=extra_h) if extended else dep
        rows.append({
            "charging_event_id":       v,
            "arrival_time":            arr,
            "original_departure_time": dep,
            "extended_departure_time": ext_dep,
            "extra_dwell_hours_needed": round(extra_h, 4),
            "was_dwell_extended":      extended,
        })
    ext_meta = pd.DataFrame(rows).set_index("charging_event_id")
    ext_events = events_df.copy()
    for idx, row in ext_events.iterrows():
        v = row["charging_event_id"]
        if ext_meta.loc[v, "was_dwell_extended"]:
            ext_events.at[idx, "departure_time"] = ext_meta.loc[v, "extended_departure_time"]
    return ext_events, ext_meta


def run_silent(events_df, p_eff):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        k, result = xos.find_min_xos_units(events_df, p_eff)
    return k, result


def dispatch_intervals(dispatch_log):
    """event_id → (charge_start_step, charge_end_step+15min)"""
    ivs: dict = {}
    for entry in dispatch_log:
        v = entry["event_id"]
        t = pd.Timestamp(entry["time_utc"])
        if v not in ivs:
            ivs[v] = [t, t]
        else:
            if t < ivs[v][0]: ivs[v][0] = t
            if t > ivs[v][1]: ivs[v][1] = t
    DT15 = pd.Timedelta(minutes=15)
    return {v: (d[0], d[1] + DT15) for v, d in ivs.items()}


# ── main audit loop ───────────────────────────────────────────────────────────
all_violations   = []
n_veh_total      = 0
n_type_A         = 0
n_type_B         = 0
before_arr_mins  = []
after_dep_mins   = []

print("\nRunning audit (suppressing per-day sim output)...", flush=True)

for csv_path in ALL_CSVS:
    date_tag = csv_path.stem.split("_events_")[-1]
    date_str = date_tag.replace("northgate_", "").replace("_", "-")

    try:
        events_df = xos.load_events(csv_path)
    except Exception:
        continue
    if len(events_df) == 0:
        continue

    p_eff     = xos.compute_p_eff(events_df)
    ext_ev, ext_meta = compute_extensions(events_df)
    p_eff_ext = xos.compute_p_eff(ext_ev)

    _, res_ext = run_silent(ext_ev, p_eff_ext)
    ivs = dispatch_intervals(res_ext["dispatch_log"])

    for v, (cs, ce) in ivs.items():
        n_veh_total += 1
        arr  = ext_meta.loc[v, "arrival_time"]
        odep = ext_meta.loc[v, "original_departure_time"]
        edep = ext_meta.loc[v, "extended_departure_time"]

        before_min = max(0.0, (arr  - cs).total_seconds() / 60.0)   # green bar before arr
        after_min  = max(0.0, (ce   - edep).total_seconds() / 60.0) # green bar after ext_dep

        if before_min > 0.001:
            n_type_A += 1
            before_arr_mins.append(before_min)
        if after_min > 0.001:
            n_type_B += 1
            after_dep_mins.append(after_min)

        if before_min > 0.001 or after_min > 0.001:
            vtypes = []
            if before_min > 0.001: vtypes.append("A:before_arrival")
            if after_min  > 0.001: vtypes.append("B:after_ext_dep")
            all_violations.append({
                "date":           date_str,
                "event_id":       str(v).rsplit("_", 1)[-1],
                "arrival":        str(arr)[:16].replace("T", " "),
                "orig_dep":       str(odep)[:16].replace("T", " "),
                "ext_dep":        str(edep)[:16].replace("T", " "),
                "charge_start":   str(cs)[:16].replace("T", " "),
                "charge_end":     str(ce)[:16].replace("T", " "),
                "violation_type": " | ".join(vtypes),
                "before_arr_min": round(before_min, 1),
                "after_dep_min":  round(after_min, 1),
            })

    sys.stdout.write(f"  {date_str} done  ({len(ivs)} vehicles)\n")
    sys.stdout.flush()


# ── REPORT ────────────────────────────────────────────────────────────────────
W = 100
print(f"\n\n{'='*W}")
print("  DISPATCH TIMING VIOLATION AUDIT — ALL 31 NORTHGATE DAYS")
print(f"  Simulation: CURRENT (unfixed) code — using full DT_H=0.25h for partial steps")
print(f"{'='*W}")
print(f"\n  Total dispatched vehicles across 31 days : {n_veh_total}")
print(f"  Type A — charge_start < arrival_time     : {n_type_A} / {n_veh_total}"
      f"  ({100*n_type_A/max(n_veh_total,1):.0f}%)")
print(f"  Type B — charge_end > ext_departure      : {n_type_B} / {n_veh_total}"
      f"  ({100*n_type_B/max(n_veh_total,1):.0f}%)")
print(f"  Either type                              : {len(all_violations)} / {n_veh_total}"
      f"  ({100*len(all_violations)/max(n_veh_total,1):.0f}%)")

if before_arr_mins:
    print(f"\n  Type A — minutes green bar appears before arrival_time:")
    print(f"    Min    : {min(before_arr_mins):.1f} min")
    print(f"    Mean   : {np.mean(before_arr_mins):.1f} min")
    print(f"    Median : {np.median(before_arr_mins):.1f} min")
    print(f"    Max    : {max(before_arr_mins):.1f} min")
    print(f"    (bounded by DT_H=15 min — pure step-alignment artifact)")

if after_dep_mins:
    print(f"\n  Type B — minutes green bar extends past extended_departure_time:")
    print(f"    Min    : {min(after_dep_mins):.1f} min")
    print(f"    Mean   : {np.mean(after_dep_mins):.1f} min")
    print(f"    Median : {np.median(after_dep_mins):.1f} min")
    print(f"    Max    : {max(after_dep_mins):.1f} min")

# Show 20 worst Type-A violations
print(f"\n\n  TOP 20 WORST VIOLATIONS (by minutes before arrival):")
print(f"  {'Date':<12} {'ID':<6} {'Arrival':>16} {'Ext dep':>16} "
      f"{'Charge start':>16} {'Charge end':>16} {'BeforeArr':>10} {'AfterDep':>9}")
print(f"  {'-'*96}")
worst = sorted(all_violations, key=lambda x: x["before_arr_min"], reverse=True)[:20]
for r in worst:
    print(f"  {r['date']:<12} {r['event_id']:<6} "
          f"{r['arrival']:>16} {r['ext_dep']:>16} "
          f"{r['charge_start']:>16} {r['charge_end']:>16} "
          f"{r['before_arr_min']:>9.1f}m {r['after_dep_min']:>8.1f}m")

# Simulation impact
print(f"""
{'='*W}
  SIMULATION IMPACT ANALYSIS
{'='*W}
  The violations above are caused by the same root issue at TWO levels:

  1. SIMULATION (energy overcounting):
     When a vehicle arrives at 10:07, the simulation activates it in step
     t=10:00 (because arr=10:07 < t_next=10:15) and delivers a FULL
     dose: e_del = pv * DT_H * ETA_D = 80 * 0.25 * 0.95 = 19.0 kWh.
     The vehicle was physically present for only 8 of those 15 minutes,
     so the physically correct energy is: 80 * (8/60) * 0.95 = 10.1 kWh.
     The simulation OVERCOUNTS by ~8.9 kWh in the first partial step,
     and similarly overcounts in the last partial step at departure.
     Combined, a 1.00-hour dwell vehicle can be credited up to ~95 kWh
     instead of the correct ~76 kWh, inflating 'served' counts.

  2. PLOTTING (visual artifact):
     dispatch_log records 'time_utc = t' (step start = 10:00) even when
     the vehicle arrived at 10:07, so the green 'charging period' bar in
     the Gantt starts at 10:00 instead of 10:07 — appearing before the
     blue 'original dwell' bar.

  FIX REQUIRED: Both layers need correction.
    Simulation: prorate e_del by actual step overlap (not full DT_H).
    Plotting:   clip green bar to [max(charge_start, arrival), min(charge_end, ext_dep)].
{'='*W}
""")
