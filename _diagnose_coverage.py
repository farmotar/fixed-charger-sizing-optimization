"""
Diagnose why K=14 and K=17 give identical coverage.
Finds partial days and checks if increasing K helps.
"""
import io, sys, contextlib
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')
import scenario_runner as sr
from pathlib import Path
import pandas as pd

ENERGY_TOL = 0.05

def run_k(events_ext, K, mode='a2'):
    with contextlib.redirect_stdout(io.StringIO()):
        sim = sr._simulate_xos(events_ext, K, mode=mode)
    delivered = sim['delivered']; remaining = sim['remaining']
    n_full    = sum(1 for v in delivered if remaining.get(v, 0) <= ENERGY_TOL)
    n_partial = sum(1 for v in delivered if delivered[v] > ENERGY_TOL and remaining.get(v, 0) > ENERGY_TOL)
    n_unserved= sum(1 for v in delivered if delivered[v] <= ENERGY_TOL)
    e_del = sum(delivered.values())
    e_unmet = sum(r for r in remaining.values() if r > ENERGY_TOL)
    return {'n_full': n_full, 'n_partial': n_partial, 'n_unserved': n_unserved,
            'e_del': e_del, 'e_unmet': e_unmet, 'n_veh': sim['n_vehicles']}

# Check how many days (from the original summary) had 100% service at their optimal K
df = pd.read_csv('scenario_outputs/northgate_analysis/northgate_summary.csv')
a2 = df[df['scenario']=='A2']
print(f"Original A2 analysis — days by service rate:")
print(f"  100% fully served:  {(a2['n_unserved']+a2['n_partial']==0).sum()} / {len(a2)}")
print(f"  >=95% service rate: {(a2['service_rate_pct']>=95).sum()} / {len(a2)}")
print(f"  <95%:               {(a2['service_rate_pct']<95).sum()} / {len(a2)}")
print()

# Pick 3 "partial" days from original analysis and test K=3,7,14,17
partial_days = a2[a2['n_partial'] > 0].sort_values('n_partial', ascending=False).head(3)
print("Testing K sweep on 3 high-partial days:")
for _, row in partial_days.iterrows():
    date_str = row['date']
    date_tag  = date_str.replace('-', '_')
    csv_path  = Path(f'z2z_milp_events_northgate_{date_tag}.csv')
    if not csv_path.exists():
        continue
    ev = sr.load_site_day_data(csv_path)
    ev = sr.apply_multiday_rule(ev, date_str, site_csv_dir=csv_path.parent,
                                 site_csv_stem='z2z_milp_events_northgate')
    ev = sr._xos_extended_dwell(ev)
    print(f"\n  {date_str}  (orig K={int(row['K'])}, orig n_partial={int(row['n_partial'])})")
    for K in [int(row['K']), 14, 17, 20, 25]:
        r = run_k(ev, K)
        print(f"    K={K:2d}:  full={r['n_full']:2d}  partial={r['n_partial']:2d}  unserved={r['n_unserved']:2d}  "
              f"e_unmet={r['e_unmet']:.1f} kWh  svc={100*r['n_full']/r['n_veh']:.0f}%")

# Now check a day that was 100% in original — does it stay 100% with K=14?
full_days = a2[a2['n_partial']+a2['n_unserved']==0].head(3)
print("\nTesting K sweep on 3 originally-100%-served days:")
for _, row in full_days.iterrows():
    date_str = row['date']
    date_tag  = date_str.replace('-', '_')
    csv_path  = Path(f'z2z_milp_events_northgate_{date_tag}.csv')
    if not csv_path.exists():
        continue
    ev = sr.load_site_day_data(csv_path)
    ev = sr.apply_multiday_rule(ev, date_str, site_csv_dir=csv_path.parent,
                                 site_csv_stem='z2z_milp_events_northgate')
    ev = sr._xos_extended_dwell(ev)
    print(f"\n  {date_str}  (orig K={int(row['K'])}, 100% served)")
    for K in [int(row['K']), 14, 17]:
        r = run_k(ev, K)
        print(f"    K={K:2d}:  full={r['n_full']:2d}  partial={r['n_partial']:2d}  "
              f"e_unmet={r['e_unmet']:.1f} kWh  svc={100*r['n_full']/r['n_veh']:.0f}%")
