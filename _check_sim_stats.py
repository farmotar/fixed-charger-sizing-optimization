import io, sys, contextlib
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')
import scenario_runner as sr
from pathlib import Path

csv_path = Path('z2z_milp_events_northgate_2026_02_11.csv')
events = sr.load_site_day_data(csv_path)
events = sr.apply_multiday_rule(events, '2026-02-11', site_csv_dir=csv_path.parent,
                                 site_csv_stem='z2z_milp_events_northgate')
events_ext = sr._xos_extended_dwell(events)

with contextlib.redirect_stdout(io.StringIO()):
    sim = sr._simulate_xos(events_ext, 17, mode='a2')

ENERGY_TOL = 0.05
delivered  = sim['delivered']
remaining  = sim['remaining']
n_full     = sum(1 for v in delivered if remaining.get(v, 0) <= ENERGY_TOL)
n_partial  = sum(1 for v in delivered if delivered[v] > ENERGY_TOL and remaining.get(v, 0) > ENERGY_TOL)
n_unserved = sum(1 for v in delivered if delivered[v] <= ENERGY_TOL)
e_del      = sum(delivered.values())
e_unmet    = sum(r for r in remaining.values() if r > ENERGY_TOL)
e_dem      = e_del + e_unmet
n_veh      = sim['n_vehicles']
print(f"K=17, worst day 2026-02-11")
print(f"n_full={n_full}  n_partial={n_partial}  n_unserved={n_unserved}  n_veh={n_veh}")
print(f"e_dem={e_dem:.1f}  e_del={e_del:.1f}  e_unmet={e_unmet:.1f}")
print(f"svc_rate={100*n_full/n_veh:.1f}%")
print()

# Same with K=14 (rank-7 worst day K)
with contextlib.redirect_stdout(io.StringIO()):
    sim14 = sr._simulate_xos(events_ext, 14, mode='a2')
delivered14  = sim14['delivered']; remaining14 = sim14['remaining']
n_full14     = sum(1 for v in delivered14 if remaining14.get(v, 0) <= ENERGY_TOL)
n_partial14  = sum(1 for v in delivered14 if delivered14[v] > ENERGY_TOL and remaining14.get(v, 0) > ENERGY_TOL)
n_unserved14 = sum(1 for v in delivered14 if delivered14[v] <= ENERGY_TOL)
e_del14 = sum(delivered14.values())
e_unmet14 = sum(r for r in remaining14.values() if r > ENERGY_TOL)
print(f"K=14 on same worst day:")
print(f"n_full={n_full14}  n_partial={n_partial14}  n_unserved={n_unserved14}")
print(f"e_del={e_del14:.1f}  e_unmet={e_unmet14:.1f}  svc={100*n_full14/n_veh:.1f}%")
