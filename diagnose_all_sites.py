"""
diagnose_all_sites.py
──────────────────────
Comprehensive sanity checks across all 4 sites after full-year XOS simulation.

Checks:
  1. All-sites summary table (baseline → extended service, units needed)
  2. Energy balance accuracy (delivered vs required per site)
  3. Days that hit MAX_UNITS cap without 100% coverage
  4. Extreme dwell-extension outliers (>6h added → likely bad Z2Z records)
  5. Vehicle count distribution per site (min/median/max per day)
  6. Per-event energy sanity from raw CSVs (E_need vs battery_capacity)
  7. Duplicate vehicle events on the same day
  8. Extended departure anomalies (ext_dep > original dep + 24h)
"""
from __future__ import annotations

import sys, re
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
SITES = {
    "northgate": "Northgate",
    "fresno":    "Fresno",
    "glendale":  "Glendale",
    "san_diego": "San Diego",
}
MAX_UNITS = 20
W = 100

# ─── Load all summary CSVs ────────────────────────────────────────────────────
summaries: dict[str, pd.DataFrame] = {}
for slug, label in SITES.items():
    p = BASE_DIR / "site_outputs" / slug / f"{slug}_extended_dwell_all_days_summary.csv"
    if p.exists():
        df = pd.read_csv(p)
        df["site"] = label
        summaries[slug] = df
    else:
        print(f"  [MISSING] {p}")

print(f"\n{'='*W}")
print("  CHECK 1: ALL-SITES SUMMARY TABLE")
print(f"{'='*W}")
print(f"  {'Site':<12} {'Days':>5} {'Vehicles':>10} {'Baseline%':>10} {'Extended%':>10} "
      f"{'100% days':>10} {'P50':>5} {'P80':>5} {'P90':>5} {'P99':>5} {'MaxU':>5}")
print(f"  {'-'*W}")

northgate_summary = summaries.get("northgate")
for slug, label in SITES.items():
    if slug not in summaries:
        continue
    df = summaries[slug]
    n_days   = len(df)
    n_veh    = df["total_vehicles"].sum()
    bpct     = 100 * df["served_before_extension"].sum() / max(df["total_vehicles"].sum(), 1)
    epct     = 100 * df["served_after_extension"].sum()  / max(df["total_vehicles"].sum(), 1)
    n100     = df["all_served_after_ext"].sum()
    units    = df["min_xos_units_after_ext"].values
    p50      = int(np.percentile(units, 50))
    p80      = int(np.percentile(units, 80))
    p90      = int(np.percentile(units, 90))
    p99      = int(np.percentile(units, 99))
    maxu     = int(units.max())
    print(f"  {label:<12} {n_days:>5} {n_veh:>10,} {bpct:>9.1f}% {epct:>9.1f}% "
          f"{n100:>9}/{n_days} {p50:>5} {p80:>5} {p90:>5} {p99:>5} {maxu:>5}")

# ─── CHECK 2: Energy balance ──────────────────────────────────────────────────
print(f"\n{'='*W}")
print("  CHECK 2: ENERGY BALANCE (delivered vs required)")
print(f"{'='*W}")
print(f"  {'Site':<12} {'Req kWh':>12} {'Del kWh':>12} {'Gap kWh':>10} {'Gap%':>7} {'Status'}")
print(f"  {'-'*65}")
for slug, label in SITES.items():
    if slug not in summaries:
        continue
    df   = summaries[slug]
    req  = df["total_energy_required_kwh"].sum()
    del_ = df["total_energy_delivered_kwh"].sum()
    gap  = req - del_
    gp   = 100 * gap / max(req, 1)
    flag = "OK" if abs(gp) < 1.0 else "WARN" if abs(gp) < 5.0 else "FAIL"
    print(f"  {label:<12} {req:>12,.0f} {del_:>12,.0f} {gap:>10,.0f} {gp:>7.3f}%  {flag}")

# ─── CHECK 3: Days at MAX_UNITS cap without 100% coverage ────────────────────
print(f"\n{'='*W}")
print(f"  CHECK 3: DAYS HITTING MAX_UNITS={MAX_UNITS} CAP (not all served)")
print(f"{'='*W}")
any_cap = False
for slug, label in SITES.items():
    if slug not in summaries:
        continue
    df   = summaries[slug]
    capped = df[(df["min_xos_units_after_ext"] >= MAX_UNITS) & (~df["all_served_after_ext"])]
    if capped.empty:
        print(f"  {label:<12}: no cap days — all vehicles served within {MAX_UNITS} units")
    else:
        any_cap = True
        print(f"\n  {label}: {len(capped)} days where {MAX_UNITS} units is INSUFFICIENT:")
        print(f"    {'Date':<12} {'Vehicles':>10} {'Served':>8} {'Svc%':>7} {'E req kWh':>12}")
        for _, r in capped.sort_values("total_vehicles", ascending=False).head(15).iterrows():
            svc_pct = 100 * r["served_after_extension"] / max(r["total_vehicles"], 1)
            print(f"    {r['date']:<12} {int(r['total_vehicles']):>10} "
                  f"{int(r['served_after_extension']):>8} {svc_pct:>6.1f}%"
                  f" {r['total_energy_required_kwh']:>12,.0f}")

# ─── CHECK 4: Extreme dwell extension outliers ────────────────────────────────
print(f"\n{'='*W}")
print("  CHECK 4: EXTREME DWELL EXTENSION OUTLIERS (added dwell > 6 h per day)")
print(f"{'='*W}")
print("  Note: max_added_dwell_h is the worst single vehicle on that day.")
THRESH_H = 6.0
for slug, label in SITES.items():
    if slug not in summaries:
        continue
    df = summaries[slug]
    outliers = df[df["max_added_dwell_h"] > THRESH_H].sort_values("max_added_dwell_h", ascending=False)
    if outliers.empty:
        print(f"  {label:<12}: no days with max_added_dwell > {THRESH_H:.0f}h  OK")
    else:
        print(f"\n  {label}: {len(outliers)} days with a vehicle needing >{THRESH_H:.0f}h extension:")
        print(f"    {'Date':<12} {'MaxAdded h':>12} {'AvgAdded h':>12} {'Vehicles':>10}")
        for _, r in outliers.head(10).iterrows():
            print(f"    {r['date']:<12} {r['max_added_dwell_h']:>12.2f} "
                  f"{r['avg_added_dwell_h']:>12.2f} {int(r['total_vehicles']):>10}")

# ─── CHECK 5: Vehicle count distribution per day ─────────────────────────────
print(f"\n{'='*W}")
print("  CHECK 5: DAILY VEHICLE COUNT DISTRIBUTION")
print(f"{'='*W}")
print(f"  {'Site':<12} {'Min':>5} {'P25':>5} {'P50':>5} {'P75':>5} {'P90':>5} "
      f"{'P95':>5} {'Max':>5}  Busiest day")
print(f"  {'-'*W}")
for slug, label in SITES.items():
    if slug not in summaries:
        continue
    df   = summaries[slug]
    v    = df["total_vehicles"].values
    idx  = df["total_vehicles"].idxmax()
    busy = f"{df.loc[idx,'date']}  ({int(df.loc[idx,'total_vehicles'])} vehicles)"
    print(f"  {label:<12} {int(np.percentile(v,0)):>5} {int(np.percentile(v,25)):>5} "
          f"{int(np.percentile(v,50)):>5} {int(np.percentile(v,75)):>5} "
          f"{int(np.percentile(v,90)):>5} {int(np.percentile(v,95)):>5} "
          f"{int(v.max()):>5}  {busy}")

# ─── CHECK 6: Per-event energy sanity (sample from each site) ─────────────────
print(f"\n{'='*W}")
print("  CHECK 6: PER-EVENT ENERGY SANITY (E_need vs battery capacity)")
print(f"{'='*W}")
for slug, label in SITES.items():
    all_csvs = sorted(BASE_DIR.glob(f"z2z_milp_events_{slug}_*.csv"))
    if not all_csvs:
        continue
    frames = []
    for p in all_csvs:
        try:
            frames.append(pd.read_csv(p))
        except Exception:
            pass
    if not frames:
        continue
    all_ev = pd.concat(frames, ignore_index=True)
    n_total = len(all_ev)
    e_need  = all_ev["energy_needed_kwh_for_visit"]
    bat     = all_ev["battery_capacity_kwh"]
    overcap = all_ev[e_need > bat * 1.01]   # >1% over battery capacity
    neg_e   = all_ev[e_need < 0]
    zero_e  = all_ev[e_need < 0.01]
    huge_e  = all_ev[e_need > 450]          # no EV we model has >450 kWh battery
    print(f"\n  {label} ({n_total:,} events):")
    print(f"    E_need range    : {e_need.min():.1f} – {e_need.max():.1f} kWh "
          f"  median={e_need.median():.1f} kWh")
    print(f"    Battery range   : {bat.min():.0f} – {bat.max():.0f} kWh")
    print(f"    E > battery cap : {len(overcap):,} events  "
          f"({'OK' if len(overcap)==0 else 'WARN — check EV spec'})")
    print(f"    E < 0.01 kWh    : {len(zero_e):,} events  "
          f"({'OK' if len(zero_e)==0 else 'trivial events'})")
    print(f"    E > 450 kWh     : {len(huge_e):,} events  "
          f"({'OK' if len(huge_e)==0 else 'WARN — check EV model'})")
    if len(overcap) > 0:
        print(f"    Over-cap sample :")
        oc = overcap[["vehicle_id","ev_equivalent_model","energy_needed_kwh_for_visit",
                       "battery_capacity_kwh"]].head(5)
        for _, r in oc.iterrows():
            print(f"      {r['vehicle_id']}  {r['ev_equivalent_model']}  "
                  f"need={r['energy_needed_kwh_for_visit']:.1f}  bat={r['battery_capacity_kwh']:.0f}")

# ─── CHECK 7: Duplicate vehicle events on same day ───────────────────────────
print(f"\n{'='*W}")
print("  CHECK 7: DUPLICATE VEHICLE EVENTS (same vehicle_id, same day CSV)")
print(f"{'='*W}")
for slug, label in SITES.items():
    all_csvs = sorted(BASE_DIR.glob(f"z2z_milp_events_{slug}_*.csv"))
    if not all_csvs:
        continue
    total_dups = 0
    worst_date, worst_n = "", 0
    for p in all_csvs:
        try:
            df = pd.read_csv(p)
        except Exception:
            continue
        dups = df[df.duplicated("vehicle_id", keep=False)]
        if len(dups) > worst_n:
            worst_n   = len(dups)
            worst_date = p.stem
        total_dups += len(dups)
    if total_dups == 0:
        print(f"  {label:<12}: 0 duplicates  OK")
    else:
        print(f"  {label:<12}: {total_dups} duplicate vehicle-day events  "
              f"worst day: {worst_date} ({worst_n} dupes)")

# ─── CHECK 8: Ext_dep anomalies (>24h extension, likely bad Z2Z record) ──────
print(f"\n{'='*W}")
print("  CHECK 8: EXTENDED DEPARTURE ANOMALIES (ext_dep > orig_dep + 24h in summary)")
print(f"{'='*W}")
print("  Note: avg_added_dwell is per-day average — checking days where avg > 5h "
      "(suggests outlier vehicles pulling average up).")
AVG_THRESH = 5.0
for slug, label in SITES.items():
    if slug not in summaries:
        continue
    df = summaries[slug]
    bad = df[df["avg_added_dwell_h"] > AVG_THRESH]
    if bad.empty:
        print(f"  {label:<12}: no days with avg_added_dwell > {AVG_THRESH:.0f}h  OK")
    else:
        print(f"  {label}: {len(bad)} days with avg_added_dwell > {AVG_THRESH:.0f}h:")
        for _, r in bad.head(5).iterrows():
            print(f"    {r['date']}  avg+{r['avg_added_dwell_h']:.2f}h  "
                  f"max+{r['max_added_dwell_h']:.2f}h  vehicles={int(r['vehicles_extended'])}")

# ─── FINAL VERDICT ────────────────────────────────────────────────────────────
print(f"\n{'='*W}")
print("  DIAGNOSTIC VERDICT")
print(f"{'='*W}")
for slug, label in SITES.items():
    if slug not in summaries:
        print(f"  {label:<12}: MISSING SUMMARY — rerun simulation")
        continue
    df = summaries[slug]
    issues = []
    req = df["total_energy_required_kwh"].sum()
    del_ = df["total_energy_delivered_kwh"].sum()
    gap_pct = 100 * abs(req - del_) / max(req, 1)
    if gap_pct > 1.0: issues.append(f"energy gap {gap_pct:.1f}%")
    capped = df[(df["min_xos_units_after_ext"] >= MAX_UNITS) & (~df["all_served_after_ext"])]
    if len(capped) > 0: issues.append(f"{len(capped)} days hit MAX_UNITS cap")
    outliers = df[df["max_added_dwell_h"] > THRESH_H]
    if len(outliers) > 0: issues.append(f"{len(outliers)} days with >6h single-vehicle extension")
    status = "PASS" if not issues else "WARN: " + "; ".join(issues)
    print(f"  {label:<12}: {status}")

print(f"\n  Diagnostic complete: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"{'='*W}\n")
