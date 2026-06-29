"""
run_two_sample_days.py
Regenerate schedule txt + Gantt png for 2026-02-26 and 2025-09-25
using the corrected simulation (prorated partial steps) and corrected
Gantt plotting (green bar clipped to vehicle availability window).
"""
from __future__ import annotations
import sys, importlib
from pathlib import Path
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

# Force reimport so the fixed xos_hub_soc_simulation is loaded fresh
BASE_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
sys.path.insert(0, str(BASE_DIR))
if "xos_hub_soc_simulation" in sys.modules:
    del sys.modules["xos_hub_soc_simulation"]
if "run_northgate_extended_full" in sys.modules:
    del sys.modules["run_northgate_extended_full"]

main = importlib.import_module("run_northgate_extended_full")

SAMPLE_DATES = {"northgate_2026_02_26", "northgate_2025_09_25"}

csvs = [p for p in main.ALL_CSVS
        if any(d in p.stem for d in SAMPLE_DATES)]

print(f"\nRegenerating {len(csvs)} sample days with FIXED simulation + FIXED Gantt:")
for p in csvs:
    print(f"  {p.name}")

print()
for csv_path in csvs:
    row = main.process_day(csv_path, verbose=True)
    if row:
        print(f"\n  Summary: {row}")

print("\nDone.")
