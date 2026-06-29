"""Run only first 5 Northgate days as preview — print serviceability diagnostics and summary."""
from __future__ import annotations
import sys, importlib
from pathlib import Path
from datetime import datetime
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
OUT_DIR  = BASE_DIR / "site_outputs" / "northgate"
OUT_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(BASE_DIR))
xos = importlib.import_module("xos_hub_soc_simulation")

ALL_CSVS = sorted(BASE_DIR.glob("z2z_milp_events_northgate_*.csv"))[:5]

# ──────────────────────────────────────────────────────────────────────────────
summary_rows = []

for csv_path in ALL_CSVS:
    date_tag  = csv_path.stem.split("_events_")[-1]   # e.g. "northgate_2025_05_08"
    date_str  = date_tag.replace("northgate_", "").replace("_", "-")
    xos_out   = OUT_DIR / f"xos_{date_tag}"
    xos_out.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*65}")
    print(f"  [{datetime.now().strftime('%H:%M:%S')}]  {date_str}")
    print(f"{'='*65}")

    events_df    = xos.load_events(csv_path)
    p_eff        = xos.compute_p_eff(events_df)
    n_units, res = xos.find_min_xos_units(events_df, p_eff)
    cost         = xos.compute_unit_cost_summary(n_units)
    xos.export_results(events_df, n_units, res, cost, xos_out, label=date_tag)

    # Read back the enriched event CSV (already has diagnostic columns)
    ev_csv = xos_out / f"xos_event_results_{date_tag}.csv"
    ev_df  = pd.read_csv(ev_csv)

    n_total   = len(ev_df)
    n_phys    = int(ev_df["physically_serviceable"].sum())
    n_served  = int(ev_df["served_by_simulation"].sum())
    n_tw_inf  = int((ev_df["reason_unserved"] == "time_window_infeasible").sum())
    n_sched   = int((ev_df["reason_unserved"] == "scheduler_or_energy_limited").sum())
    srv_pct   = 100 * n_served / max(n_total, 1)
    phys_pct  = 100 * n_phys  / max(n_total, 1)

    # Per-vehicle table
    print(f"\n  {'ID':<14} {'dwell_h':>8} {'E_need':>8} {'E_max':>8} "
          f"{'phys?':>6} {'served?':>8} {'reason'}")
    print(f"  {'-'*78}")
    for _, r in ev_df.sort_values("energy_needed_kwh", ascending=False).iterrows():
        p  = "YES" if r["physically_serviceable"]  else "NO"
        s  = "YES" if r["served_by_simulation"]    else "NO"
        print(f"  {str(r['charging_event_id'])[-14:]:<14} "
              f"{r['dwell_hours']:>8.2f} "
              f"{r['energy_needed_kwh']:>8.1f} "
              f"{r['max_deliverable_kwh_at_80kw']:>8.1f} "
              f"{p:>6} {s:>8}  {r['reason_unserved']}")

    summary_rows.append({
        "date":         date_str,
        "n_total":      n_total,
        "n_phys":       n_phys,
        "n_served":     n_served,
        "n_tw_inf":     n_tw_inf,
        "n_sched":      n_sched,
        "srv_pct":      round(srv_pct, 1),
        "phys_pct":     round(phys_pct, 1),
        "e_req":        round(res["total_energy_required_kwh"], 0),
        "e_del":        round(res["total_energy_delivered_kwh"], 0),
        "peak_kw":      res["peak_dispatch_kw"],
        "n_units":      n_units,
    })

# ──────────────────────────────────────────────────────────────────────────────
print(f"\n\n{'='*105}")
print("  NORTHGATE — 5-DAY SERVICEABILITY SUMMARY  (MAX_UNITS=20, 4-port @ 80 kW/port)")
print(f"{'='*105}")
hdr = (f"  {'Date':<12} {'Total':>6} {'Phys.OK':>8} {'Sim.Srvd':>9} "
       f"{'TW-Infeas':>10} {'Sched-Lim':>10} {'Srv%':>6} {'Phys%':>6}")
print(hdr)
print(f"  {'-'*101}")
for r in summary_rows:
    print(f"  {r['date']:<12} {r['n_total']:>6} {r['n_phys']:>8} {r['n_served']:>9} "
          f"{r['n_tw_inf']:>10} {r['n_sched']:>10} {r['srv_pct']:>5.1f}% {r['phys_pct']:>5.1f}%")

print(f"  {'-'*101}")
tot  = sum(r['n_total']  for r in summary_rows)
tph  = sum(r['n_phys']   for r in summary_rows)
tsv  = sum(r['n_served'] for r in summary_rows)
ttw  = sum(r['n_tw_inf'] for r in summary_rows)
tsc  = sum(r['n_sched']  for r in summary_rows)
print(f"  {'TOTAL':<12} {tot:>6} {tph:>8} {tsv:>9} "
      f"{ttw:>10} {tsc:>10} "
      f"{100*tsv/max(tot,1):>5.1f}% {100*tph/max(tot,1):>5.1f}%")
print(f"{'='*105}")
print()
print("  Column definitions:")
print("    Total       — charging events in the day")
print("    Phys.OK     — vehicles where E_need <= dwell_h * 80 kW * 0.95  (physically servable)")
print("    Sim.Srvd    — vehicles fully served by the XOS simulation")
print("    TW-Infeas   — unserved because dwell time too short for energy need (hard constraint)")
print("    Sched-Lim   — physically serviceable but NOT served (scheduler / energy routing gap)")
print("    Srv%        — simulation service rate")
print("    Phys%       — physical upper-bound service rate (= Phys.OK / Total)")
print(f"{'='*105}")
print(f"\nDone: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
