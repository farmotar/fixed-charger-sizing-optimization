import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import scenario_runner as sr

CSV  = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\z2z_milp_events_northgate_2025_07_17.csv")
OUT  = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\scenario_outputs\northgate_2025_07_17\xos_a2_fixed")
sr.run_xos_not_always_grid_connected(CSV, OUT, "2025-07-17", "Northgate")
