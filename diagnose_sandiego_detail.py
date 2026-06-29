"""Deep-dive diagnostic on San Diego cap days and duplicate vehicles."""
import sys, pandas as pd, numpy as np
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")

df = pd.read_csv(BASE / "site_outputs/san_diego/san_diego_extended_dwell_all_days_summary.csv")
capped = df[(df["min_xos_units_after_ext"] >= 20) & (~df["all_served_after_ext"])].sort_values("total_vehicles", ascending=False)

print("=== SAN DIEGO: 10 CAP DAYS ===")
total_unserved = (capped["total_vehicles"] - capped["served_after_extension"]).sum()
pct_events = 100 * capped["total_vehicles"].sum() / df["total_vehicles"].sum()
print(f"  Total unserved events across all 10 cap days : {int(total_unserved)}")
print(f"  Cap-day vehicles as % of annual total        : {pct_events:.1f}%")
print()

# --- Extreme dwell anomalies: scan all site CSVs for dwell > 24h ---
print("=== EXTREME DWELL ANOMALIES (dwell_hours > 24h) ACROSS ALL SITES ===")
for slug in ["northgate", "fresno", "glendale", "san_diego"]:
    all_csvs = sorted(BASE.glob(f"z2z_milp_events_{slug}_*.csv"))
    frames = []
    for p in all_csvs:
        try:
            d = pd.read_csv(p)
            d["_date"] = p.stem
            frames.append(d)
        except Exception:
            pass
    if not frames:
        continue
    all_ev = pd.concat(frames, ignore_index=True)
    extreme = all_ev[all_ev["dwell_hours"] > 24].copy()
    if extreme.empty:
        print(f"  {slug:<12}: no events with dwell > 24h  OK")
    else:
        print(f"  {slug:<12}: {len(extreme)} events with dwell > 24h  "
              f"max={extreme['dwell_hours'].max():.1f}h  "
              f"({extreme['dwell_hours'].max()/24:.0f} days)")
        top = extreme.nlargest(5, "dwell_hours")[
            ["_date","vehicle_id","dwell_hours","energy_needed_kwh_for_visit","ev_equivalent_model"]]
        for _, r in top.iterrows():
            print(f"    {r['_date']}  {r['vehicle_id']}  "
                  f"dwell={r['dwell_hours']:.1f}h ({r['dwell_hours']/24:.0f}d)  "
                  f"E_need={r['energy_needed_kwh_for_visit']:.0f} kWh  "
                  f"{r['ev_equivalent_model']}")

print()
# --- Worst cap day deep dive ---
print("=== WORST CAP DAY: 2026-03-11 (93 events, 20 units insufficient) ===")
p = BASE / "z2z_milp_events_san_diego_2026_03_11.csv"
d = pd.read_csv(p)
print(f"  Unique vehicle_id : {d['vehicle_id'].nunique()}")
print(f"  Total events      : {len(d)}")
dwell_ok  = d[d["dwell_hours"] <= 24]
dwell_bad = d[d["dwell_hours"] > 24]
print(f"  Events dwell ≤24h : {len(dwell_ok)}")
print(f"  Events dwell >24h : {len(dwell_bad)}  (data anomalies — exclude from sizing)")
print(f"  Dwell h  min={d['dwell_hours'].min():.2f}  "
      f"median={dwell_ok['dwell_hours'].median():.2f}  "
      f"max={d['dwell_hours'].max():.1f}")
print(f"  EV models (top 5):")
for m, cnt in d["ev_equivalent_model"].value_counts().head(5).items():
    print(f"    {cnt:>3}x  {m}")
d["arr"] = pd.to_datetime(d["arrival_time"], utc=True)
d["dep"] = pd.to_datetime(d["departure_time"], utc=True)

# Peak concurrency using only sane dwell events
d_sane = d[d["dwell_hours"] <= 24].copy()
slots = []
for _, r in d_sane.iterrows():
    slots.append((r["arr"], 1)); slots.append((r["dep"], -1))
slots.sort(key=lambda x: x[0])
cur = peak = 0
for _, delta in slots:
    cur += delta; peak = max(peak, cur)
N_PORTS = 4
print(f"  Peak concurrent (sane events only): {peak}")
print(f"  XOS units needed (peak/ports): ceil({peak}/{N_PORTS}) = {int(np.ceil(peak/N_PORTS))}")

print()
# --- Unique fleet sizes ---
print("=== UNIQUE VEHICLE FLEET SIZE PER SITE ===")
for slug in ["northgate","fresno","glendale","san_diego"]:
    cache = BASE / f"_{slug}_z2z_cache.csv"
    if not cache.exists():
        continue
    z2z = pd.read_csv(cache, low_memory=False)
    z2z_f = z2z[z2z["use_for_optimization_bool"].fillna(False).astype(bool)]
    n_veh = z2z_f["vehicle_name"].nunique()
    print(f"  {slug:<12}: {n_veh:>4} unique vehicles in Z2Z cache")

print()
# --- Daily multi-visit breakdown for Northgate worst day ---
print("=== NORTHGATE 2025-10-14: MULTI-VISIT VEHICLE DETAIL ===")
worst = BASE / "z2z_milp_events_northgate_2025_10_14.csv"
d2 = pd.read_csv(worst)
print(f"  Total events: {len(d2)}  unique vehicles: {d2['vehicle_id'].nunique()}")
multi = d2.groupby("vehicle_id").filter(lambda x: len(x) > 1)
print(f"  Multi-visit vehicles: {multi['vehicle_id'].nunique()}")
count = 0
for vid, grp in multi.groupby("vehicle_id"):
    print(f"    {vid}: {len(grp)} visits  "
          f"dwells={list(grp['dwell_hours'].round(2))}  "
          f"E_needs={list(grp['energy_needed_kwh_for_visit'].round(0))}")
    count += 1
    if count >= 4:
        print(f"    ... (showing 4 of {multi['vehicle_id'].nunique()})")
        break
