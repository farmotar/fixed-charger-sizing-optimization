import sys, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")

# Use the NEW fixed-scheduler outputs
DISP_CSV = r'scenario_outputs\northgate_2025_07_17\xos_a2_fixed\scenario_A2_dispatch_2025-07-17.csv'
disp = pd.read_csv(DISP_CSV)

TZ = 'America/Los_Angeles'
disp['time_pac'] = pd.to_datetime(disp['time_utc'], utc=True).dt.tz_convert(TZ)

print('=== Hub 1 (unit=0) entire activity — FIXED SCHEDULER ===')
h1 = disp[disp['unit']==0].sort_values('time_pac')
for _, r in h1.iterrows():
    print(f"  {r['time_pac'].strftime('%H:%M')}  port={int(r['port'])}  ev={r['event_id']}"
          f"  soc {r['soc_before']:.3f}->{r['soc_after']:.3f}  e={r['energy_to_vehicle_kwh']:.2f}kWh")

print()
print('=== All hub-vehicle assignments (sorted by hub then time) — FIXED ===')
summary = (disp.groupby(['unit','event_id'])
             .agg(first=('time_pac','min'), last=('time_pac','max'),
                  total_e=('energy_to_vehicle_kwh','sum'))
             .reset_index()
             .sort_values(['unit','first']))
for _, r in summary.iterrows():
    print(f"  Hub{int(r['unit'])+1:2d}  {r['event_id']:<25}  "
          f"{r['first'].strftime('%H:%M')}-{r['last'].strftime('%H:%M')}  "
          f"{r['total_e']:.1f}kWh")

print()
print('=== Vehicles served per hub ===')
per_hub = summary.groupby('unit')['event_id'].count().sort_index()
for hub, n in per_hub.items():
    print(f"  Hub{int(hub)+1:2d}: {n} vehicle(s)")
