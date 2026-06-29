"""Resume San Diego: generate missing day-view figures, then run Phase 3."""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")
from pathlib import Path
from run_site_pipeline import (phase2_figures, phase3_worst_days,
                                _load_events_for_day, plot_one_day)

SITE       = "san_diego"
SITE_LABEL = "San Diego"
CSV_STEM   = "z2z_milp_events_san_diego"
BASE_DIR   = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
OUT_DIR    = BASE_DIR / "scenario_outputs" / "san_diego_analysis"
PER_DAY    = OUT_DIR / "per_day"

# Phase 2: only generate figures for days that don't already have one
day_dirs = sorted(PER_DAY.iterdir()) if PER_DAY.exists() else []
missing  = [d for d in day_dirs if d.is_dir() and not (d / f"day_view_{d.name}.png").exists()]
print(f"Phase 2 — San Diego: {len(missing)} figures missing (of {len(day_dirs)} days)")

ok = skip = fail = 0
for i, day_dir in enumerate(missing, 1):
    date_str = day_dir.name
    print(f"  [{i:3d}/{len(missing)}] {date_str}", end="  ", flush=True)
    events_ext = _load_events_for_day(date_str, CSV_STEM)
    if events_ext is None or events_ext.empty:
        print("skipped"); skip += 1; continue
    try:
        out = plot_one_day(date_str, day_dir, events_ext, SITE_LABEL)
        if out: print("saved"); ok += 1
        else:   print("skipped"); skip += 1
    except Exception as e:
        print(f"ERROR: {e}"); fail += 1

print(f"Phase 2 done. Saved={ok}  Skipped={skip}  Errors={fail}")

# Phase 3
phase3_worst_days(SITE, SITE_LABEL, OUT_DIR, CSV_STEM)
print("\nSan Diego complete.")
