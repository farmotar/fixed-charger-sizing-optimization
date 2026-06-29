import sys, importlib
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(".")))
trip = importlib.import_module("xos_trip_simulation")

csv_path = Path("z2z_milp_events_northgate_2026_04_29.csv")
res = trip.simulate_xos(csv_path, n_units=10, verbose=False)
ev  = res["events"]

print()
print("=== ALL VEHICLES ===")
for v in ev["charging_event_id"].tolist():
    rem   = res["remaining"][v]
    deliv = res["delivered"][v]
    rows  = ev[ev["charging_event_id"] == v]
    need  = float(rows["energy_needed_kwh_for_visit"].iloc[0])
    arr   = rows["arrival_time"].iloc[0].tz_convert("America/Los_Angeles")
    dep   = rows["departure_time"].iloc[0].tz_convert("America/Los_Angeles")
    dwell = (dep - arr).total_seconds() / 3600
    min_h = need / (trip.P_PORT * trip.ETA_D)
    status = "OK  " if rem <= 0.1 else "MISS"
    print(f"  {status}  arr={arr.strftime('%H:%M')}  dep={dep.strftime('%H:%M')}  "
          f"dwell={dwell:.2f}h  need={need:.0f}kWh  min_charge_h={min_h:.2f}h  deliv={deliv:.0f}kWh")
