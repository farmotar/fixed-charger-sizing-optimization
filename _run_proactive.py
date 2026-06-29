"""
Re-run A1 and A2 for July 17 with proactive recharge enabled.
Uses separate output dirs so old results are preserved.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import scenario_runner as sr

CSV  = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\z2z_milp_events_northgate_2025_07_17.csv")
OUT_A2 = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\scenario_outputs\northgate_2025_07_17\xos_a2_proactive")
OUT_A1 = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\scenario_outputs\northgate_2025_07_17\xos_a1_proactive")

print("=" * 70)
print("  PROACTIVE RECHARGE — A2 (disconnect at 20% SOC)")
print("=" * 70)
sr.run_xos_not_always_grid_connected(CSV, OUT_A2, "2025-07-17", "Northgate")

print()
print("=" * 70)
print("  PROACTIVE RECHARGE — A1 (stay on port during recharge)")
print("=" * 70)
sr.run_xos_always_grid_connected(CSV, OUT_A1, "2025-07-17", "Northgate")

# Show proactive recharge events from debug log
print()
print("Proactive recharge events fired (A2):")
debug_file = list(OUT_A2.glob("*debug*.txt"))
if debug_file:
    lines = debug_file[0].read_text(encoding="utf-8").splitlines()
    pr_lines = [l for l in lines if "PROACTIVE" in l]
    print(f"  Total proactive triggers: {len(pr_lines)}")
    for l in pr_lines:
        print(f"  {l}")
else:
    print("  (no debug log found)")
