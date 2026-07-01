"""
recalc_site_costs.py
=====================
Recompute energy + capacity (demand/subscription) costs for all 4 Caltrans
sites using site-specific utility rates, instead of the SMUD-for-all-sites
shortcut baked into scenario_runner.py's _xos_a1_cost().

No re-simulation needed: per-day grid_kw time series are already saved at
    scenario_outputs/{site}_analysis/per_day/{date}/{scenario}_grid_draw_{date}.csv

For each site/date/scenario row in {site}_cost_detail.csv and {site}_summary.csv,
this script:
  1. Loads the matching grid_draw CSV (time_utc, grid_kw)
  2. Computes total_grid_kwh, energy_cost_daily (sum grid_kw * 0.25h * rate(t))
  3. Computes peak_grid_kw (max) and peak_win_kw (max within that utility's peak window)
  4. Computes the monthly capacity charge (SMUD: demand; PG&E/SDG&E: subscription)
  5. Recomputes total_daily_excl_demand / total_daily_incl_demand
     (capex/maint/warranty columns are untouched -- those aren't rate-dependent)

Northgate uses scenario A1 (always grid-connected) as its headline scenario;
Fresno / Glendale / San Diego use A2. Both A1 and A2 rows are recalculated
in every file (rates apply regardless of scenario) but only the headline
scenario should be used for reporting.

Glendale currently uses a SMUD-rate placeholder (see utility_rates.py
docstring) because GWP's actual tariff could not be retrieved.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))
import utility_rates as ur

BASE_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
OUT_DIR  = BASE_DIR / "scenario_outputs"
DT_H     = 0.25

SITES = ["northgate", "fresno", "glendale", "san_diego"]


def _recalc_row(site: str, date_str: str, scenario: str) -> dict | None:
    gd_path = OUT_DIR / f"{site}_analysis" / "per_day" / date_str / f"{scenario}_grid_draw_{date_str}.csv"
    if not gd_path.exists():
        return None
    gd = pd.read_csv(gd_path, parse_dates=["time_utc"])
    if gd.empty:
        return None
    t_utc = gd["time_utc"]
    if t_utc.dt.tz is None:
        t_utc = t_utc.dt.tz_localize("UTC")
    grid_kw = gd["grid_kw"].to_numpy(dtype=float)

    rate_fn = ur.energy_rate_fn(site)
    pkwin_fn = ur.peak_win_fn(site)

    rates = np.array([rate_fn(t) for t in t_utc])
    is_pk = np.array([pkwin_fn(t) for t in t_utc])

    energy_cost = float(np.sum(grid_kw * DT_H * rates))
    total_kwh   = float(np.sum(grid_kw) * DT_H)
    p_max       = float(np.max(grid_kw)) if len(grid_kw) else 0.0
    p_peak_win  = float(np.max(grid_kw[is_pk])) if is_pk.any() else 0.0

    cap = ur.capacity_charge(site, p_max, p_peak_win)
    cap_vals = list(cap.values())
    primary_monthly   = cap_vals[0]
    secondary_monthly = cap_vals[1] if len(cap_vals) > 1 else 0.0

    return {
        "energy_cost_daily":         round(energy_cost, 2),
        "demand_global_monthly_$":   round(primary_monthly, 2),
        "demand_peak_win_monthly_$": round(secondary_monthly, 2),
        "total_grid_kwh":            round(total_kwh, 2),
        "peak_grid_kw":              round(p_max, 1),
        "peak_win_kw":               round(p_peak_win, 1),
    }


def _update_file(df: pd.DataFrame, site: str) -> tuple[pd.DataFrame, int, int]:
    n_ok, n_missing = 0, 0
    for idx, row in df.iterrows():
        new_vals = _recalc_row(site, row["date"], row["scenario"])
        if new_vals is None:
            n_missing += 1
            continue
        for k, v in new_vals.items():
            df.at[idx, k] = v
        fixed = (
            row.get("purchase_capex_daily", 0)
            + row.get("infra_capex_daily", 0)
            + row.get("maint_daily", 0)
            + row.get("warranty_daily", 0)
        )
        total_var = fixed + new_vals["energy_cost_daily"]
        df.at[idx, "total_daily_excl_demand"] = round(total_var, 2)
        df.at[idx, "total_daily_incl_demand"] = round(
            total_var + new_vals["demand_global_monthly_$"] / 30.0, 2
        )
        n_ok += 1
    return df, n_ok, n_missing


def run_site(site: str) -> None:
    label = site.replace("_", " ").title()
    util  = ur.SITE_UTILITY[site]
    headline = ur.SITE_SCENARIO[site]
    print(f"\n{'='*70}\n{label}  (utility={util}, headline scenario={headline})\n{'='*70}")

    site_dir = OUT_DIR / f"{site}_analysis"
    for fname in (f"{site}_cost_detail.csv", f"{site}_summary.csv"):
        fpath = site_dir / fname
        if not fpath.exists():
            print(f"  [skip] {fname} not found")
            continue
        df = pd.read_csv(fpath)
        backup = fpath.with_suffix(".csv.bak")
        if not backup.exists():
            df.to_csv(backup, index=False)
        df, n_ok, n_missing = _update_file(df, site)
        df.to_csv(fpath, index=False)
        print(f"  {fname}: updated {n_ok} rows, {n_missing} missing grid_draw files")

    # quick headline summary
    cost_path = site_dir / f"{site}_cost_detail.csv"
    if cost_path.exists():
        dc = pd.read_csv(cost_path)
        hl = dc[dc.scenario == headline]
        if not hl.empty:
            print(f"  Headline ({headline}) avg energy_cost_daily: ${hl.energy_cost_daily.mean():.2f}")
            print(f"  Headline ({headline}) avg total_daily_excl_demand: ${hl.total_daily_excl_demand.mean():.2f}")
            print(f"  Headline ({headline}) avg total_daily_incl_demand: ${hl.total_daily_incl_demand.mean():.2f}")
            print(f"  Headline ({headline}) avg peak_grid_kw: {hl.peak_grid_kw.mean():.0f}")


if __name__ == "__main__":
    targets = sys.argv[1:] if len(sys.argv) > 1 else SITES
    for s in targets:
        run_site(s)
    print("\nDone.")
