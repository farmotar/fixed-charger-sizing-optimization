"""
_reprice_glendale_xos_pge.py
Re-applies PG&E BEV-2 rates to pre-computed Glendale XOS A1 simulation results.
The adaptive-K simulation output (K, grid_draw profiles) is unchanged — only costs
are recalculated. Updates glendale_cost_detail.csv and glendale_summary.csv in-place.
"""
import sys, math
from pathlib import Path
import pandas as pd
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))

from utility_rates import pge_bev2_energy_rate, pge_bev2_capacity_charge, pge_bev2_is_peak_win

BASE     = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
SITE_DIR = BASE / "scenario_outputs" / "glendale_analysis"
PER_DAY  = SITE_DIR / "per_day"

DAYS_PER_MO = 30.42

# XOS unit economics (unchanged)
XOS_PURCHASE = 245_437.50; XOS_LIFE = 10; XOS_MAINT = 6_000; XOS_WARRANTY = 10_000
INFRA_S = {"low":20_000,"mid":40_000,"high":80_000}
INFRA_U = {"low":6_000, "mid":8_500, "high":12_000}
INFRA_T = {"low":12_000,"mid":20_000,"high":35_000}
TIER_SZ = 4

def infra_total(K, e="mid"):
    nt = max(0, math.ceil(K/TIER_SZ) - 1)
    return INFRA_S[e] + K*INFRA_U[e] + nt*INFRA_T[e]

def xos_capex_daily(K):
    hw   = K * XOS_PURCHASE
    inf  = infra_total(K)
    return {
        "purchase_capex_daily": hw  / (XOS_LIFE * 12 * DAYS_PER_MO),
        "infra_capex_daily":    inf / (XOS_LIFE * 12 * DAYS_PER_MO),
        "infra_total_mid_$":    inf,
        "maint_daily":          K * XOS_MAINT   / (12 * DAYS_PER_MO),
        "warranty_daily":       K * XOS_WARRANTY / (12 * DAYS_PER_MO),
    }

# ── load existing cost_detail.csv (all scenarios) ──────────────────────
cost_path = SITE_DIR / "glendale_cost_detail.csv"
summ_path = SITE_DIR / "glendale_summary.csv"

cost_df = pd.read_csv(cost_path)
summ_df = pd.read_csv(summ_path)

a1_mask  = cost_df["scenario"] == "A1"
a1_dates = cost_df.loc[a1_mask, "date"].tolist()

print(f"Repricing {sum(a1_mask)} Glendale A1 days with PG&E BEV-2 rates …")

updated_rows = []
missing = []
for date_str in a1_dates:
    # find per-day grid_draw CSV
    candidates = sorted(PER_DAY.glob(f"{date_str}/A1_grid_draw_*.csv"))
    if not candidates:
        candidates = sorted(PER_DAY.glob(f"{date_str}/A*_grid_draw_*.csv"))
    if not candidates:
        print(f"  WARNING: no grid_draw file for {date_str}")
        missing.append(date_str)
        updated_rows.append(None)
        continue

    gd = pd.read_csv(candidates[0])
    # columns: timestamp (UTC string), grid_kw  (or similar)
    # find timestamp column
    ts_col = next((c for c in gd.columns if "time" in c.lower() or "ts" in c.lower()), gd.columns[0])
    pw_col = next((c for c in gd.columns if "kw" in c.lower() or "power" in c.lower() or "grid" in c.lower()), gd.columns[1])

    gd[ts_col] = pd.to_datetime(gd[ts_col], utc=True)
    gd["rate"]  = gd[ts_col].apply(pge_bev2_energy_rate)
    # energy per 15-min step = kw * 0.25 h * rate
    gd["energy_cost_step"] = gd[pw_col] * 0.25 * gd["rate"]

    energy_cost_daily = float(gd["energy_cost_step"].sum())
    total_grid_kwh    = float((gd[pw_col] * 0.25).sum())
    peak_grid_kw      = float(gd[pw_col].max())

    # PG&E capacity charge: subscription only (no peak-window demand)
    cap = pge_bev2_capacity_charge(peak_grid_kw)
    demand_global_monthly  = cap["subscription_monthly"]
    demand_peak_win_monthly = 0.0   # PG&E BEV-2 has no separate peak-window charge

    # get K from existing row
    row = cost_df.loc[(cost_df["date"]==date_str) & (cost_df["scenario"]=="A1")].iloc[0]
    K = int(row["K"]) if "K" in row else 0
    cap_costs = xos_capex_daily(K)

    total_daily_excl = (cap_costs["purchase_capex_daily"] + cap_costs["infra_capex_daily"] +
                        cap_costs["maint_daily"] + cap_costs["warranty_daily"] + energy_cost_daily)
    total_daily_incl = total_daily_excl + demand_global_monthly / DAYS_PER_MO

    updated_rows.append({
        "date":                     date_str,
        "energy_cost_daily":        round(energy_cost_daily, 4),
        "demand_global_monthly_$":  round(demand_global_monthly, 4),
        "demand_peak_win_monthly_$": round(demand_peak_win_monthly, 4),
        "total_daily_excl_demand":  round(total_daily_excl, 4),
        "total_daily_incl_demand":  round(total_daily_incl, 4),
        "total_grid_kwh":           round(total_grid_kwh, 4),
        "peak_grid_kw":             round(peak_grid_kw, 4),
        **{k: round(v,4) for k,v in cap_costs.items()},
    })

print(f"  {len(a1_dates)-len(missing)} days repriced | {len(missing)} missing grid_draw files")

# ── apply updates to cost_df ────────────────────────────────────────────
update_cols = ["energy_cost_daily","demand_global_monthly_$","demand_peak_win_monthly_$",
               "total_daily_excl_demand","total_daily_incl_demand","total_grid_kwh",
               "peak_grid_kw","purchase_capex_daily","infra_capex_daily",
               "maint_daily","warranty_daily","infra_total_mid_$"]

n_updated = 0
for i, (date_str, upd) in enumerate(zip(a1_dates, updated_rows)):
    if upd is None: continue
    mask = (cost_df["date"]==date_str) & (cost_df["scenario"]=="A1")
    for col in update_cols:
        if col in upd and col in cost_df.columns:
            cost_df.loc[mask, col] = upd[col]
    n_updated += 1

cost_df.to_csv(cost_path, index=False)
print(f"  Updated {n_updated} rows in {cost_path.name}")

# ── update summary.csv peak_grid_kw and total_grid_kwh ─────────────────
# summary.csv may have peak_grid_kw, total_grid_kwh columns
update_summ_cols = [c for c in ["peak_grid_kw","total_grid_kwh"] if c in summ_df.columns]
if update_summ_cols:
    upd_map = {r["date"]: r for r in updated_rows if r is not None}
    a1s_mask = summ_df["scenario"] == "A1"
    for col in update_summ_cols:
        for idx in summ_df[a1s_mask].index:
            d = summ_df.at[idx, "date"]
            if d in upd_map:
                summ_df.at[idx, col] = upd_map[d][col]
    summ_df.to_csv(summ_path, index=False)
    print(f"  Updated {update_summ_cols} in {summ_path.name}")

print("\nGlendale XOS repricing with PG&E BEV-2 complete.")
print("Next step: rerun Kempower Glendale MILP with updated rates.")
