"""
_run_glendale_smud.py
Runs XOS repricing + Kempower MILP for Glendale under SMUD rates, saving results
to separate files so PG&E results are not overwritten.

Outputs:
  glendale_analysis/glendale_cost_detail_smud.csv     (XOS repriced)
  glendale_analysis/glendale_kempower_summary_smud.csv (Kempower MILP)
  per_day/{date}/kempower_smud/                        (Kempower per-day files)
"""
import sys, math
from pathlib import Path
import pandas as pd
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))

import utility_rates as ur

BASE     = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
SITE_DIR = BASE / "scenario_outputs" / "glendale_analysis"
PER_DAY  = SITE_DIR / "per_day"
DAYS_PER_MO = 30.42

# ── Part 1: XOS SMUD repricing ────────────────────────────────────────────────
print("=" * 60)
print("  Part 1: XOS Glendale — SMUD repricing")
print("=" * 60)

XOS_PURCHASE = 245_437.50; XOS_LIFE = 10; XOS_MAINT = 6_000; XOS_WARRANTY = 10_000
INFRA_S = {"low":20_000,"mid":40_000,"high":80_000}
INFRA_U = {"low":6_000, "mid":8_500, "high":12_000}
INFRA_T = {"low":12_000,"mid":20_000,"high":35_000}
TIER_SZ = 4

def infra_total(K, e="mid"):
    nt = max(0, math.ceil(K / TIER_SZ) - 1)
    return INFRA_S[e] + K * INFRA_U[e] + nt * INFRA_T[e]

def xos_capex_daily(K):
    hw  = K * XOS_PURCHASE
    inf = infra_total(K)
    return {
        "purchase_capex_daily": hw  / (XOS_LIFE * 12 * DAYS_PER_MO),
        "infra_capex_daily":    inf / (XOS_LIFE * 12 * DAYS_PER_MO),
        "infra_total_mid_$":    inf,
        "maint_daily":          K * XOS_MAINT   / (12 * DAYS_PER_MO),
        "warranty_daily":       K * XOS_WARRANTY / (12 * DAYS_PER_MO),
    }

cost_df = pd.read_csv(SITE_DIR / "glendale_cost_detail.csv")
a1_mask  = cost_df["scenario"] == "A1"
a1_dates = cost_df.loc[a1_mask, "date"].tolist()

print(f"Repricing {sum(a1_mask)} A1 days with SMUD rates …")

smud_rows = []
missing   = []
for date_str in a1_dates:
    candidates = sorted(PER_DAY.glob(f"{date_str}/A1_grid_draw_*.csv"))
    if not candidates:
        candidates = sorted(PER_DAY.glob(f"{date_str}/A*_grid_draw_*.csv"))
    if not candidates:
        missing.append(date_str); smud_rows.append(None); continue

    gd = pd.read_csv(candidates[0])
    ts_col = next((c for c in gd.columns if "time" in c.lower()), gd.columns[0])
    pw_col = next((c for c in gd.columns if "kw" in c.lower() or "grid" in c.lower()), gd.columns[1])
    gd[ts_col] = pd.to_datetime(gd[ts_col], utc=True)
    gd["rate"]  = gd[ts_col].apply(ur.smud_energy_rate)
    gd["energy_cost_step"] = gd[pw_col] * 0.25 * gd["rate"]

    # peak window (SMUD: 16-21 weekdays)
    gd["is_peak_win"] = gd[ts_col].apply(ur.smud_is_peak_win)
    peak_win_kw = float(gd.loc[gd["is_peak_win"], pw_col].max()) if gd["is_peak_win"].any() else 0.0

    energy_cost_daily  = float(gd["energy_cost_step"].sum())
    total_grid_kwh     = float((gd[pw_col] * 0.25).sum())
    peak_grid_kw       = float(gd[pw_col].max())

    cap = ur.smud_capacity_charge(peak_grid_kw, peak_win_kw)
    demand_global_monthly   = cap["demand_global_monthly"]
    demand_peak_win_monthly = cap["demand_peak_win_monthly"]

    row = cost_df.loc[(cost_df["date"]==date_str) & (cost_df["scenario"]=="A1")].iloc[0]
    K = int(row["K"]) if "K" in row else 0
    cap_costs = xos_capex_daily(K)

    total_excl = (cap_costs["purchase_capex_daily"] + cap_costs["infra_capex_daily"] +
                  cap_costs["maint_daily"] + cap_costs["warranty_daily"] + energy_cost_daily)
    total_incl = (total_excl + demand_global_monthly / DAYS_PER_MO
                  + demand_peak_win_monthly / DAYS_PER_MO)

    smud_rows.append({
        "date": date_str, "scenario": "A1", "K": K,
        "energy_cost_daily":          round(energy_cost_daily, 4),
        "demand_global_monthly_$":    round(demand_global_monthly, 4),
        "demand_peak_win_monthly_$":  round(demand_peak_win_monthly, 4),
        "total_daily_excl_demand":    round(total_excl, 4),
        "total_daily_incl_demand":    round(total_incl, 4),
        "total_grid_kwh":             round(total_grid_kwh, 4),
        "peak_grid_kw":               round(peak_grid_kw, 4),
        **{k: round(v, 4) for k, v in cap_costs.items()},
    })

smud_cost_df = pd.DataFrame([r for r in smud_rows if r is not None])
out_path = SITE_DIR / "glendale_cost_detail_smud.csv"
smud_cost_df.to_csv(out_path, index=False)
print(f"  Saved {len(smud_cost_df)} rows → {out_path.name}")

# quick p90 check
smud_cost_df["total_allin"] = (smud_cost_df["total_daily_excl_demand"]
    + smud_cost_df["demand_global_monthly_$"] / DAYS_PER_MO
    + smud_cost_df["demand_peak_win_monthly_$"] / DAYS_PER_MO)
p90 = float(np.percentile(smud_cost_df["total_allin"], 90))
r90 = smud_cost_df.loc[(smud_cost_df["total_allin"] - p90).abs().idxmin()]
print(f"  XOS SMUD p90 = ${p90:.0f}/day  K={int(r90['K'])}")

# ── Part 2: Kempower SMUD MILP rerun ──────────────────────────────────────────
print("\n" + "=" * 60)
print("  Part 2: Kempower Glendale — SMUD MILP rerun")
print("=" * 60)

# Temporarily switch Glendale to SMUD rates
original_site_utility = ur.SITE_UTILITY.get("glendale")
ur.SITE_UTILITY["glendale"] = "smud"
print(f"  Patched SITE_UTILITY['glendale'] = 'smud' (was '{original_site_utility}')")

try:
    import run_kempower_pipeline as rkp
    rkp.run_kempower_site(
        "glendale", "Glendale", "z2z_milp_events_glendale",
        force=True,
        per_day_subdir="kempower_smud",
        summary_suffix="_smud",
    )
finally:
    # Always restore
    ur.SITE_UTILITY["glendale"] = original_site_utility
    print(f"\n  Restored SITE_UTILITY['glendale'] = '{original_site_utility}'")

print("\nGlendale SMUD run complete.")
print("  XOS: glendale_cost_detail_smud.csv")
print("  KMP: glendale_kempower_summary_smud.csv")
