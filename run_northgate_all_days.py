"""
run_northgate_all_days.py
--------------------------
Run XOS Hub MC02 simulation for ALL available Northgate event CSVs.
For each day:
  - Saves results to site_outputs/northgate/xos_northgate_YYYY_MM_DD/
  - Generates a human-readable schedule: xos_schedule_YYYY_MM_DD.txt
    showing per-15-min time slot: units active, vehicles served, power, SoC
"""
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

# All Northgate event CSVs, sorted by date
ALL_CSVS = sorted(BASE_DIR.glob("z2z_milp_events_northgate_*.csv"))

print(f"Found {len(ALL_CSVS)} Northgate event files.\n")

# ──────────────────────────────────────────────────────────────────────────────
def build_schedule_text(date_tag: str, events_df: pd.DataFrame,
                        n_units: int, result: dict) -> str:
    """
    Build a human-readable schedule for one day.
    Shows per-15-min slot: time, units active, vehicles served, power, avg SoC.
    """
    dispatch_log = result["dispatch_log"]
    soc_history  = result["soc_history"]

    # Index dispatch log by step
    step_to_assignments: dict[int, list[dict]] = {}
    for entry in dispatch_log:
        si = entry["step_idx"]
        step_to_assignments.setdefault(si, []).append(entry)

    lines = []
    lines.append("=" * 90)
    lines.append(f"  XOS HUB MC02 — DAILY SCHEDULE   {date_tag.replace('_','-')}   "
                 f"({n_units} units deployed, 4 ports/unit @ 80 kW/port)")
    lines.append("=" * 90)

    served_set = {v for v, rem in result["remaining"].items() if rem <= xos.ENERGY_TOL}
    n_total    = result["n_total"]

    lines.append(
        f"  Summary: {n_units} XOS units | "
        f"{result['n_served']}/{n_total} vehicles served "
        f"({100*result['n_served']//max(n_total,1)}%) | "
        f"Energy delivered: {result['total_energy_delivered_kwh']:.0f}/"
        f"{result['total_energy_required_kwh']:.0f} kWh"
    )
    lines.append("")

    # Header
    soc_cols = "  ".join(f"U{k:02d} SoC" for k in range(n_units))
    lines.append(
        f"{'Time':<7} {'Veh Served':>10} {'Pwr kW':>8} {'Ports':>6}   {soc_cols}"
    )
    lines.append("-" * 90)

    for row_soc in soc_history:
        si   = row_soc["step_idx"]
        t    = row_soc["time_utc"][:16].replace("T", " ")   # "YYYY-MM-DD HH:MM"
        t_hm = t[11:16]                                       # "HH:MM"

        assigns   = step_to_assignments.get(si, [])
        n_ports   = len(assigns)
        pwr_kw    = sum(a["power_kw"] for a in assigns)
        veh_ids   = [a["event_id"] for a in assigns]
        units_used = sorted(set(a["unit"] for a in assigns))

        # Short vehicle label list
        if veh_ids:
            veh_str = ",".join(str(v)[-4:] for v in veh_ids[:4])
            if len(veh_ids) > 4:
                veh_str += f"+{len(veh_ids)-4}"
        else:
            veh_str = "—  (charging)"

        soc_vals = "  ".join(f"{row_soc.get(f'soc_unit_{k}', 0)*100:6.1f}%" for k in range(n_units))

        lines.append(
            f"{t_hm:<7} {veh_str:>10} {pwr_kw:>8.0f} {n_ports:>6}   {soc_vals}"
        )

    lines.append("=" * 90)
    lines.append(f"  Vehicle legend (last 4 chars of event ID):")
    lines.append(f"  Total vehicles: {n_total}  |  Served: {result['n_served']}  |  "
                 f"Unserved: {n_total - result['n_served']}")
    lines.append("=" * 90)
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
summary_rows = []
errors       = []

for csv_path in ALL_CSVS:
    date_tag = csv_path.stem.split("_events_")[-1]   # e.g. "northgate_2025_06_09"
    xos_out  = OUT_DIR / f"xos_{date_tag}"
    summary  = xos_out / f"xos_sim_summary_{date_tag}.txt"

    if summary.exists():
        print(f"  [SKIP] {date_tag} — already done")
        # Still read result for summary table
        txt = summary.read_text(encoding="utf-8", errors="replace")
        import re
        m1 = re.search(r"Minimum XOS units\s*:\s*(\d+)", txt)
        m2 = re.search(r"Vehicles served\s*:\s*(\d+)\s*/\s*(\d+)", txt)
        if m1 and m2:
            summary_rows.append({
                "date": date_tag.replace("northgate_", "").replace("_", "-"),
                "n_units": int(m1.group(1)),
                "n_served": int(m2.group(1)),
                "n_total":  int(m2.group(2)),
            })
        continue

    xos_out.mkdir(parents=True, exist_ok=True)
    print(f"\n  [{datetime.now().strftime('%H:%M:%S')}] {date_tag} ...")

    try:
        events_df    = xos.load_events(csv_path)
        p_eff        = xos.compute_p_eff(events_df)
        n_units, res = xos.find_min_xos_units(events_df, p_eff)
        cost         = xos.compute_unit_cost_summary(n_units)
        xos.export_results(events_df, n_units, res, cost, xos_out, label=date_tag)

        # Write schedule
        sched_txt = build_schedule_text(date_tag, events_df, n_units, res)
        (xos_out / f"xos_schedule_{date_tag}.txt").write_text(
            sched_txt, encoding="utf-8"
        )
        print(f"    Saved schedule: xos_schedule_{date_tag}.txt")

        pct = 100 * res["n_served"] / max(res["n_total"], 1)
        print(f"    -> {n_units} units | {res['n_served']}/{res['n_total']} served ({pct:.0f}%)")

        summary_rows.append({
            "date":     date_tag.replace("northgate_", "").replace("_", "-"),
            "n_units":  n_units,
            "n_served": res["n_served"],
            "n_total":  res["n_total"],
        })

    except Exception as exc:
        print(f"    [ERROR] {exc}")
        errors.append((date_tag, str(exc)))

# ──────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("  NORTHGATE — ALL DAYS SUMMARY")
print("=" * 70)
print(f"  {'Date':<14} {'XOS Units':>10} {'Served':>8} {'Total':>7} {'Srv%':>6}")
print("-" * 70)
for r in sorted(summary_rows, key=lambda x: x["date"]):
    pct = 100 * r["n_served"] // max(r["n_total"], 1)
    flag = "*" if r["n_units"] >= xos.MAX_UNITS else " "
    print(f"  {r['date']:<14} {r['n_units']:>9}{flag} {r['n_served']:>8} {r['n_total']:>7} {pct:>5}%")
print("=" * 70)
print(f"  * = capped at MAX_UNITS={xos.MAX_UNITS}; not all vehicles served")
if errors:
    print(f"\n  Errors ({len(errors)}):")
    for tag, msg in errors:
        print(f"    {tag}: {msg}")
print(f"\nDone: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
