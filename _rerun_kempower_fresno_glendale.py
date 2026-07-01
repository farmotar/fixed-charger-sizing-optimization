import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))
import run_kempower_pipeline as rkp

rkp.run_kempower_site("fresno", "Fresno", "z2z_milp_events_fresno", force=True)
rkp.run_kempower_site("glendale", "Glendale", "z2z_milp_events_glendale", force=True)
print("\nFresno + Glendale Kempower rerun complete.")
