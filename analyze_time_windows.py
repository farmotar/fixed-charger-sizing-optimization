import pandas as pd, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
ETA_D, P_PORT = 0.95, 80.0
ENERGY_TOL = 0.10

days = sorted(BASE.glob("z2z_milp_events_northgate_2025_0[56]*.csv"))[:5]

print(f"\n{'='*70}")
print("  TIME-WINDOW SERVICEABILITY ANALYSIS — NORTHGATE (5 days)")
print(f"{'='*70}")
print("  A vehicle is 'physically servable' iff:")
print("  E_need  <=  dwell_h * 80 kW * 0.95   (at single port, one shot)")
print(f"{'='*70}")

grand_total = grand_servable = 0

for csv in days:
    df = pd.read_csv(csv)
    df["arrival_time"]   = pd.to_datetime(df["arrival_time"],   utc=True, errors="coerce")
    df["departure_time"] = pd.to_datetime(df["departure_time"], utc=True, errors="coerce")
    df["dwell_h"]  = (df["departure_time"] - df["arrival_time"]).dt.total_seconds() / 3600
    df["E_need"]   = pd.to_numeric(df["energy_needed_kwh_for_visit"], errors="coerce").fillna(0)
    df["E_max"]    = df["dwell_h"] * P_PORT * ETA_D
    df["servable"] = df["E_need"] <= df["E_max"] + ENERGY_TOL

    n_total    = len(df)
    n_servable = int(df["servable"].sum())
    tag = csv.stem.split("events_")[-1].replace("northgate_", "")
    grand_total    += n_total
    grand_servable += n_servable

    print(f"\n  {tag}  |  {n_total} vehicles  |  {n_servable} servable by time-window")
    print(f"  {'dwell_h':>8} {'E_need':>8} {'E_max@80kW':>12} {'servable':>10}")
    print(f"  {'-'*46}")
    for _, r in df.sort_values("E_need", ascending=False).iterrows():
        flag = "YES" if r["servable"] else "NO"
        print(f"  {r['dwell_h']:>8.2f} {r['E_need']:>8.1f} {r['E_max']:>12.1f} {flag:>10}")

print(f"\n{'='*70}")
print(f"  TOTAL: {grand_servable}/{grand_total} vehicles are physically servable by time window")
print(f"  (regardless of how many XOS units are deployed)")
print(f"{'='*70}")
