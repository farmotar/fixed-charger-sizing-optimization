"""
run_milp_no_smooth.py
---------------------
Re-runs the Northgate charger-sizing MILP with lambda_smooth = 0
(pure MILP, no quadratic smoothing penalty) and writes results to
a separate output folder so the original (lambda=1.5) results are
preserved for comparison.

After solving, prints a side-by-side comparison table of both runs.
"""

import importlib
import sys
import math
from pathlib import Path

# ── 1. Import the original module and patch the two config globals ────────────
import exact_northgate_charger_sizing_milp as milp

# Override key configuration before any function is called
milp.LAMBDA_SMOOTH = 0.0
milp.OUTPUT_DIR    = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\exact_milp_outputs_no_smooth")
milp.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── 2. Run the full pipeline (mirrors main() in the original script) ──────────
print("=" * 70)
print("Northgate MILP -- NO SMOOTHING (lambda = 0)")
print("=" * 70)

raw_df     = milp.load_events_data()
events_df  = milp.clean_events_df(raw_df)

charger_specs = milp.build_charger_specs()
daily_capex   = milp.compute_daily_capex(charger_specs)

time_grid  = milp.build_time_grid(events_df)
P_eff      = milp.compute_effective_power(events_df, charger_specs)
feasible_keys, E, arrival_map, departure_map, available_times = milp.build_feasible_keys(
    events_df, time_grid, charger_specs, P_eff
)

try:
    import gurobipy  # noqa
    sol_ns = milp.solve_with_gurobi(
        events_df, time_grid, charger_specs, daily_capex,
        P_eff, feasible_keys, E, available_times,
        lambda_smooth=0.0,
    )
except ImportError:
    sol_ns = milp.solve_with_pyomo_highs(
        events_df, time_grid, charger_specs, daily_capex,
        P_eff, feasible_keys, E, available_times,
        lambda_smooth=0.0,
    )

if sol_ns.get("status") in ("infeasible", "no_solution"):
    print("No-smooth run FAILED.")
    sys.exit(1)

milp.validate_solution(sol_ns, events_df, time_grid, charger_specs, P_eff, E, feasible_keys)
milp.export_solution(sol_ns, events_df, time_grid, charger_specs, daily_capex, P_eff, E, feasible_keys)
milp.write_summary(sol_ns, events_df, charger_specs, E, daily_capex, lambda_smooth=0.0)

# ── 3. Load the original (with-smoothing, lambda=1.5) results from summary ───
ORIG_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\exact_milp_outputs")

# Read original summary for the with-smoothing run (already solved)
orig_summary = (ORIG_DIR / "exact_milp_summary.txt").read_text(encoding="utf-8")

def _parse_val(txt, key, cast=float):
    """Pull a value from the summary text file by keyword."""
    for line in txt.splitlines():
        if key in line:
            try:
                return cast(line.split(":")[-1].strip().lstrip("$").split()[0].replace(",", ""))
            except Exception:
                pass
    return None

# ── 4. Build comparison data ──────────────────────────────────────────────────
n_steps   = len(time_grid)
dt        = milp.DT_HOURS
eta       = milp.ETA
non_first = list(range(1, n_steps))

def compute_rmsd(sol):
    p = sol["P_total_vals"]
    diffs = [(p[t] - p[t-1])**2 for t in non_first]
    return math.sqrt(sum(diffs) / max(1, len(diffs))) if diffs else 0.0

def count_power_steps(sol, thr=5.0):
    """Count 15-min steps where |DeltaP| > thr kW."""
    p = sol["P_total_vals"]
    return sum(1 for t in non_first if abs(p[t] - p[t-1]) > thr)

# No-smooth solution values
ns = sol_ns
ns_N       = ns["N_vals"]
ns_capex   = ns["daily_capex_cost"]
ns_energy  = ns["energy_cost"]
ns_demand  = ns["global_demand_cost"]
ns_pk_win  = ns["peak_window_cost"]
ns_smooth  = ns["smoothing_cost"]    # should be ~0
ns_total   = ns["total_objective_cost"]
ns_pmax    = ns["P_max_val"]
ns_pkwin   = ns["P_peak_val"]
ns_rmsd    = compute_rmsd(ns)
ns_steps   = count_power_steps(ns)
ns_soltime = ns["solve_time"]
ns_gap     = ns["mip_gap"]

# With-smoothing values (from original run stored in memory via summary file)
# Re-read from saved CSV for accuracy
import pandas as pd
orig_cost_df = pd.read_csv(ORIG_DIR / "exact_milp_cost_breakdown.csv")
orig_mix_df  = pd.read_csv(ORIG_DIR / "exact_milp_selected_charger_mix.csv")

def get_cost(df, name):
    row = df[df["component"] == name]
    return float(row["value"].iloc[0]) if len(row) else 0.0

ws_capex  = get_cost(orig_cost_df, "daily_capex_cost")
ws_energy = get_cost(orig_cost_df, "energy_cost")
ws_demand = get_cost(orig_cost_df, "global_demand_cost")
ws_pk_win = get_cost(orig_cost_df, "peak_window_demand_cost")
ws_smooth = get_cost(orig_cost_df, "smoothing_cost")
ws_pmax   = get_cost(orig_cost_df, "P_max_kw")
ws_pkwin  = get_cost(orig_cost_df, "P_peak_window_kw")
ws_total  = ws_capex + ws_energy + ws_demand + ws_pk_win + ws_smooth

ws_N = {}
for _, row in orig_mix_df.iterrows():
    ws_N[row["charger_type"]] = int(row["count"])

# RMSD for with-smooth: read from power profile CSV
orig_prof_df = pd.read_csv(ORIG_DIR / "exact_milp_site_power_profile.csv")
orig_p = orig_prof_df["P_total_kw"].values
ws_rmsd   = math.sqrt(sum((orig_p[i] - orig_p[i-1])**2 for i in range(1, len(orig_p))) / max(1, len(orig_p)-1))
ws_steps  = sum(1 for i in range(1, len(orig_p)) if abs(orig_p[i] - orig_p[i-1]) > 5.0)

# parse solve time from summary
ws_soltime = _parse_val(orig_summary, "Solve time") or 0.4
ws_gap     = 0.0  # was 0.0% from earlier run

# ── 5. Print comparison table ─────────────────────────────────────────────────
SEP = "=" * 72
sep = "-" * 72

def pct_diff(a, b):
    """% change from b to a  (positive = a is larger)."""
    if abs(b) < 1e-9:
        return "  N/A"
    return f"{(a-b)/abs(b)*100:+.1f}%"

def charger_str(N):
    parts = [f"{v}x{c}" for c, v in N.items() if v > 0]
    return "  +  ".join(parts) if parts else "none"

print()
print(SEP)
print("  NORTHGATE MILP -- COMPARISON: With Smoothing (lambda=1.5) vs No Smoothing (lambda=0)")
print(SEP)
print(f"  {'Metric':<42}  {'lambda=1.5':>12}  {'lambda=0':>12}  {'Change':>8}")
print(sep)

rows_table = [
    ("CHARGER SELECTION", None, None, None),
    ("  Selected mix",       charger_str(ws_N),          charger_str(ns_N),           ""),
    ("  Total chargers",
        sum(ws_N.values()),  sum(ns_N.values()),          ""),
    ("",  None, None, None),
    ("COST BREAKDOWN ($/day)", None, None, None),
    ("  Charger CapEx",      f"${ws_capex:.2f}",         f"${ns_capex:.2f}",          pct_diff(ns_capex, ws_capex)),
    ("  Energy cost",        f"${ws_energy:.2f}",        f"${ns_energy:.2f}",         pct_diff(ns_energy, ws_energy)),
    ("  Global demand",      f"${ws_demand:.2f}",        f"${ns_demand:.2f}",         pct_diff(ns_demand, ws_demand)),
    ("  Peak-win demand",    f"${ws_pk_win:.2f}",        f"${ns_pk_win:.2f}",         pct_diff(ns_pk_win, ws_pk_win)),
    ("  Smoothing penalty",  f"${ws_smooth:.2f}",        f"${ns_smooth:.4f}",         ""),
    ("  TOTAL OBJECTIVE",    f"${ws_total:.4f}",         f"${ns_total:.4f}",          pct_diff(ns_total, ws_total)),
    ("",  None, None, None),
    ("POWER PROFILE", None, None, None),
    ("  Peak site power (kW)",   f"{ws_pmax:.2f}",       f"{ns_pmax:.2f}",            pct_diff(ns_pmax, ws_pmax)),
    ("  Peak-window power (kW)", f"{ws_pkwin:.2f}",      f"{ns_pkwin:.2f}",           ""),
    ("  RMSD of power steps (kW)", f"{ws_rmsd:.4f}",     f"{ns_rmsd:.4f}",            pct_diff(ns_rmsd, ws_rmsd)),
    ("  Steps with |DeltaP|>5kW", f"{ws_steps}",         f"{ns_steps}",               pct_diff(ns_steps, ws_steps)),
    ("",  None, None, None),
    ("SOLVER PERFORMANCE", None, None, None),
    ("  Solve time (s)",     f"{ws_soltime:.1f}",        f"{ns_soltime:.1f}",         ""),
    ("  MIP gap",            f"{ws_gap:.4f}",            f"{ns_gap:.4f}",             ""),
    ("  Model type",         "MIQP",                     "MILP",                      "(linear vs quadratic)"),
]

for row in rows_table:
    label, ws_val, ns_val, chg = row
    if ws_val is None:
        if label:
            print(f"\n  {label}")
            print(sep)
        else:
            print()
    else:
        print(f"  {label:<42}  {str(ws_val):>12}  {str(ns_val):>12}  {str(chg):>8}")

print(SEP)
print()

# ── 6. Plain-English interpretation ──────────────────────────────────────────
delta_total = ns_total - ws_total
delta_pmax  = ns_pmax  - ws_pmax
delta_rmsd  = ns_rmsd  - ws_rmsd
print("INTERPRETATION:")
print(f"  Removing smoothing changes the objective by  ${delta_total:+.2f}/day")
print(f"  Peak demand shift:                           {delta_pmax:+.2f} kW")
print(f"  RMSD shift (power roughness):                {delta_rmsd:+.4f} kW/step")
if ns_total < ws_total:
    print(f"  -> No-smooth solution is CHEAPER by ${abs(delta_total):.2f}/day in pure cost terms.")
    print(f"     The smoothing penalty (${ ws_smooth:.2f}) was 'buying' smoother power at a premium.")
else:
    print(f"  -> Smoothing shifts scheduling to reduce peak demand further,")
    print(f"     costing ${abs(delta_total):.2f}/day extra in exchange for {abs(delta_rmsd):.4f} kW/step lower RMSD.")
print()
print(f"  No-smooth outputs -> {milp.OUTPUT_DIR}")
print(f"  With-smooth outputs -> {ORIG_DIR}")
