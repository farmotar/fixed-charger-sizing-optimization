"""
run_fixed_charger_milp_pipeline.py
====================================
Exact MILP-based fixed charger sizing pipeline for all 4 Caltrans sites.

Methodology
-----------
Phase 1: For EVERY operating day at each site, run the exact MILP
         (exact_northgate_charger_sizing_milp.py) to simultaneously optimise:
           - N_c  (integer charger counts for L2/DC50/DC150/DC350)
           - Charging schedule u[v,t,c] for every vehicle
         Objective: minimise total daily cost =
           charger CapEx + energy (site TOU rates) + demand charges
           (SMUD 2-tier / PG&E BEV-2 subscription / SDG&E EV-HP subscription)

Phase 2: Sort ALL days by total operational cost (worst = highest).

Phase 3: Select 10 worst-cost days per site.

Phase 4: On those 10 days, identify the most frequent MILP-optimal config.
         That config = recommended permanent installation for the site.

Phase 5: Generate:
   - fixed_charger_milp_outputs/<site>_all_days.csv
   - fixed_charger_milp_outputs/<site>_worst10_schedule.csv
   - fixed_charger_milp_outputs/Fixed_Charger_MILP_Results.xlsx

Costs: charger_costs_caltrans.py  (purchase + install + O&M, 10-yr life)
Rates: utility_rates.py  (SMUD / PG&E BEV-2 / SDG&E EV-HP per site)
"""

from __future__ import annotations

import importlib
import math
import re
import sys
import time
import warnings
from pathlib import Path
from collections import Counter

import pandas as pd
import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
sys.path.insert(0, str(BASE_DIR))

# ── Load modules ───────────────────────────────────────────────────────────────
import utility_rates as ur
from charger_costs_caltrans import build_charger_specs_caltrans

milp = importlib.import_module("exact_northgate_charger_sizing_milp")

# ── Output directory ───────────────────────────────────────────────────────────
OUT_DIR  = BASE_DIR / "fixed_charger_milp_outputs"
SCRATCH  = BASE_DIR / "fixed_charger_milp_outputs" / "_scratch"
OUT_DIR.mkdir(parents=True, exist_ok=True)
SCRATCH.mkdir(parents=True, exist_ok=True)

# Redirect MILP module's OUTPUT_DIR to scratch (suppresses per-day LP/MPS files)
milp.OUTPUT_DIR = SCRATCH

# ── Gurobi solver parameters for batch runs ────────────────────────────────────
milp.GUROBI_TIME_LIMIT  = 60      # seconds per solve
milp.GUROBI_MIP_GAP     = 0.05   # 5 % optimality gap
milp.GUROBI_OUTPUT_FLAG = 0      # suppress per-solve console output
milp.GUROBI_THREADS     = 0      # use all cores
milp.GUROBI_MIP_FOCUS   = 1      # 1 = prioritise finding a feasible solution first

# ── MILP mode: disable quadratic smoothing term ────────────────────────────────
# LAMBDA_SMOOTH turns the model into an MIQP (quadratic objective), which is
# orders of magnitude harder for Gurobi's branch-and-bound than a pure MILP.
# Setting it to 0 removes the quadratic term; the planning costs (CapEx + energy +
# demand charges) are identical — we just lose cosmetic power-profile smoothing.
milp.LAMBDA_SMOOTH = 0.0

# ── Switch to 15-min time steps for tractable batch performance ────────────────
# 5-min (original) → 1400+ steps/day, 15k+ binary vars → MIQP → 60s/solve (no sol)
# 15-min (planning) → ~475 steps/day, ~5k binary vars  → MILP → <5s/solve → ~2hr total
milp.DT_MINUTES = 15
milp.DT_HOURS   = 15 / 60.0

# ── Charger specs (finalized Caltrans cost table, 10-yr life) ──────────────────
CHARGER_SPECS  = build_charger_specs_caltrans()
DAYS_PER_MONTH = 30.42

def _daily_capex_map(specs: dict) -> dict[str, float]:
    dc = {}
    for ctype, s in specs.items():
        monthly = (s["purchase_cost"] + s["install_cost"]) / (s["life_years"] * 12)
        recur   = s["annual_maint"] / 12
        dc[ctype] = (monthly + recur) / DAYS_PER_MONTH
    return dc

DAILY_CAPEX = _daily_capex_map(CHARGER_SPECS)

# ── Site definitions ───────────────────────────────────────────────────────────
SITES = [
    ("northgate",  "Northgate",  "SMUD"),
    ("fresno",     "Fresno",     "PG&E BEV-2"),
    ("glendale",   "Glendale",   "PG&E BEV-2"),
    ("san_diego",  "San Diego",  "SDG&E EV-HP"),
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def config_label(N_vals: dict[str, int]) -> str:
    """Human-readable config string, e.g. '0xL2+0xDC50+2xDC150+0xDC350'."""
    parts = []
    for ctype in ["L2_19p2kW", "DC_50kW", "DC_150kW", "DC_350kW"]:
        n = N_vals.get(ctype, 0)
        short = {"L2_19p2kW": "L2", "DC_50kW": "DC50",
                 "DC_150kW": "DC150", "DC_350kW": "DC350"}[ctype]
        parts.append(f"{n}×{short}")
    return " + ".join(p for p in parts if not p.startswith("0×"))  or "0 chargers"


def service_metrics(events_df: pd.DataFrame, E: dict, sol: dict,
                    dt_hours: float, eta: float) -> dict:
    """Extract vehicle service metrics from MILP solution."""
    x_vals   = sol.get("x_vals", {})
    delivered = {v: 0.0 for v in E}
    for (v, tidx, c), pwr in x_vals.items():
        delivered[v] = delivered.get(v, 0.0) + pwr * dt_hours * eta

    n_vehicles     = len(E)
    n_full         = sum(1 for v in E if delivered.get(v, 0) >= E[v] - milp.ENERGY_TOL)
    n_partial      = sum(1 for v in E
                         if delivered.get(v, 0) > milp.ENERGY_TOL
                         and delivered.get(v, 0) < E[v] - milp.ENERGY_TOL)
    n_served       = n_full + n_partial
    e_demanded     = sum(E.values())
    e_delivered    = sum(delivered.values())

    return {
        "n_vehicles":          n_vehicles,
        "n_fully_served":      n_full,
        "n_partially_served":  n_partial,
        "n_vehicles_served":   n_served,
        "n_unserved":          n_vehicles - n_served,
        "vehicles_served_pct": round(100 * n_served / max(n_vehicles, 1), 1),
        "energy_demanded_kwh": round(e_demanded, 1),
        "energy_delivered_kwh": round(e_delivered, 1),
        "energy_served_pct":   round(100 * e_delivered / max(e_demanded, 0.001), 1),
        "delivered_per_vehicle": delivered,
    }


def vehicle_schedule(events_df: pd.DataFrame, E: dict, sol: dict,
                     time_grid: list, dt_hours: float, eta: float) -> list[dict]:
    """Build per-vehicle schedule rows from MILP u_vals."""
    x_vals    = sol.get("x_vals", {})
    u_vals    = sol.get("u_vals", {})

    delivered  = {v: 0.0 for v in E}
    charge_start = {v: None for v in E}
    charge_end   = {v: None for v in E}
    charger_type = {v: "" for v in E}

    for (v, tidx, c), pwr in x_vals.items():
        delivered[v]  = delivered.get(v, 0.0) + pwr * dt_hours * eta
        t = time_grid[tidx]
        t_end = t + pd.Timedelta(hours=dt_hours)
        if charge_start[v] is None:
            charge_start[v] = t
        charge_end[v] = t_end
        charger_type[v] = c

    ev_lookup = events_df.set_index("charging_event_id")
    rows = []
    for v in E:
        row_ev  = ev_lookup.loc[v] if v in ev_lookup.index else {}
        needed  = E[v]
        deliv   = delivered.get(v, 0.0)
        if deliv >= needed - milp.ENERGY_TOL:
            status = "full"
        elif deliv > milp.ENERGY_TOL:
            status = "partial"
        else:
            status = "unserved"

        arr_utc = pd.Timestamp(row_ev["arrival_time"]) if "arrival_time" in row_ev else None
        dep_utc = pd.Timestamp(row_ev["departure_time"]) if "departure_time" in row_ev else None
        dwell = (dep_utc - arr_utc).total_seconds() / 3600.0 if (arr_utc and dep_utc) else 0.0
        cs = charge_start[v]; ce = charge_end[v]
        dur = (ce - cs).total_seconds() / 3600.0 if (cs and ce) else 0.0

        # Convert UTC → Pacific local for display
        tz_la = "America/Los_Angeles"
        arr_la = arr_utc.tz_convert(tz_la) if arr_utc is not None else None
        dep_la = dep_utc.tz_convert(tz_la) if dep_utc is not None else None
        cs_la  = cs.tz_convert(tz_la) if cs is not None else None
        ce_la  = ce.tz_convert(tz_la) if ce is not None else None

        soc_start = float(row_ev.get("assumed_initial_soc_percent", 0) or 0)
        bat_cap   = float(row_ev.get("battery_capacity_kwh", 0) or 0)
        soc_end   = min(100.0, soc_start + 100 * deliv / bat_cap) if bat_cap > 0 else None

        rows.append({
            "charging_event_id":   v,
            "vehicle_id":          str(row_ev.get("vehicle_id", "")),
            "ev_model":            str(row_ev.get("ev_equivalent_model", "")),
            "arrival_local":       arr_la.strftime("%H:%M") if arr_la else "",
            "departure_local":     dep_la.strftime("%H:%M") if dep_la else "",
            "dwell_h":             round(dwell, 2),
            "energy_needed_kwh":   round(needed, 1),
            "energy_delivered_kwh": round(deliv, 1),
            "energy_gap_kwh":      round(max(needed - deliv, 0), 1),
            "status":              status,
            "charger_type_used":   charger_type[v],
            "charge_start":        cs_la.strftime("%H:%M") if cs_la else "",
            "charge_end":          ce_la.strftime("%H:%M") if ce_la else "",
            "charge_duration_h":   round(dur, 2),
            "soc_start_pct":       round(soc_start, 1),
            "soc_end_pct":         round(soc_end, 1) if soc_end is not None else "",
        })

    rows.sort(key=lambda r: (r["arrival_local"] or "99:99"))
    return rows


def run_one_day(csv_path: Path, site: str) -> dict | None:
    """
    Run the exact MILP for one day at one site.
    Returns a result dict or None on failure.
    """
    m = re.search(r"(\d{4}_\d{2}_\d{2})\.csv$", csv_path.name)
    if not m:
        return None
    date_str = m.group(1).replace("_", "-")

    # Load and clean events
    try:
        raw_df = pd.read_csv(csv_path)
    except Exception as e:
        print(f"    [SKIP] {date_str}: CSV read error: {e}")
        return None

    events_df = milp.clean_events_df(raw_df)
    if events_df is None or len(events_df) == 0:
        return None

    # Patch MILP globals for this site
    milp.RATE_SITE = site

    # Build time grid and feasibility structures
    time_grid   = milp.build_time_grid(events_df, dt_hours=milp.DT_HOURS)
    if len(time_grid) < 2:
        return None

    P_eff = milp.compute_effective_power(events_df, CHARGER_SPECS)
    try:
        feasible_keys, E, arr_map, dep_map, avail_times = milp.build_feasible_keys(
            events_df, time_grid, CHARGER_SPECS, P_eff, dt_hours=milp.DT_HOURS
        )
    except Exception as e:
        print(f"    [SKIP] {date_str}: feasible key build failed: {e}")
        return None

    if not feasible_keys:
        return None

    # Get site-specific demand charge rates
    c_dem_global, c_dem_peak = milp.site_capacity_charge_rates(site)

    # Solve
    t0 = time.time()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            sol = milp.solve_with_gurobi(
                events_df      = events_df,
                time_grid      = time_grid,
                charger_specs  = CHARGER_SPECS,
                daily_capex    = DAILY_CAPEX,
                P_eff          = P_eff,
                feasible_keys  = feasible_keys,
                E              = E,
                available_times = avail_times,
                dt_hours       = milp.DT_HOURS,
                eta            = milp.ETA,
                c_demand_global   = c_dem_global,
                c_demand_peak_win = c_dem_peak,
                lambda_smooth       = milp.LAMBDA_SMOOTH,
                lambda_energy_error = milp.LAMBDA_ENERGY_ERROR,
            )
    except Exception as e:
        print(f"    [ERROR] {date_str}: solver exception: {e}")
        return None

    elapsed = time.time() - t0

    if sol.get("status") in ("infeasible", "no_solution", None):
        print(f"    [NO SOL] {date_str}: {sol.get('status','unknown')}  ({elapsed:.1f}s)")
        return None

    N_vals = sol["N_vals"]

    # Real operational cost (exclude solver-artifact terms smoothing/energy_error)
    op_cost = (sol["daily_capex_cost"] + sol["energy_cost"]
               + sol["global_demand_cost"] + sol["peak_window_cost"])

    svc = service_metrics(events_df, E, sol, milp.DT_HOURS, milp.ETA)
    sched = vehicle_schedule(events_df, E, sol, time_grid, milp.DT_HOURS, milp.ETA)

    return {
        "date":                date_str,
        "site":                site,
        # Charger mix
        "N_L2":                N_vals.get("L2_19p2kW", 0),
        "N_DC50":              N_vals.get("DC_50kW", 0),
        "N_DC150":             N_vals.get("DC_150kW", 0),
        "N_DC350":             N_vals.get("DC_350kW", 0),
        "config_label":        config_label(N_vals),
        # Cost components ($/day)
        "capex_daily":         round(sol["daily_capex_cost"],   2),
        "energy_cost":         round(sol["energy_cost"],        2),
        "demand_global":       round(sol["global_demand_cost"], 2),
        "demand_peak_win":     round(sol["peak_window_cost"],   2),
        "total_op_cost":       round(op_cost,                   2),
        # Demand peaks
        "peak_kw":             round(sol["P_max_val"],   1),
        "peak_win_kw":         round(sol["P_peak_val"],  1),
        # Service
        **{k: v for k, v in svc.items() if k != "delivered_per_vehicle"},
        # Solver metadata
        "solve_time_s":        round(elapsed, 1),
        "mip_gap":             round(sol.get("mip_gap", float("nan")), 4),
        "solver_status":       sol.get("status", ""),
        # Payloads for schedule extraction
        "_events_df":          events_df,
        "_E":                  E,
        "_sol":                sol,
        "_time_grid":          time_grid,
        "_sched":              sched,
    }


# ── Main pipeline ──────────────────────────────────────────────────────────────

print("=" * 72)
print("  FIXED CHARGER MILP PIPELINE  —  ALL 4 CALTRANS SITES")
print("=" * 72)
print(f"\nCharger specs (finalized Caltrans cost table):")
for ctype, s in CHARGER_SPECS.items():
    dc = DAILY_CAPEX[ctype]
    print(f"  {ctype:<14}  {s['power_kw']:>5.1f} kW  "
          f"purchase=${s['purchase_cost']:>8,}  install=${s['install_cost']:>8,}  "
          f"O&M=${s['annual_maint']:,}/yr  life={s['life_years']}yr  -> ${dc:.2f}/day")

all_site_summary = []

with pd.ExcelWriter(OUT_DIR / "Fixed_Charger_MILP_Results.xlsx", engine="openpyxl") as xl:

    for site, site_label, utility in SITES:
        csv_files = sorted(BASE_DIR.glob(f"z2z_milp_events_{site}_*.csv"))
        n_files   = len(csv_files)

        print(f"\n{'='*72}")
        print(f"  {site_label.upper()}  [{utility}]  —  {n_files} operating days")
        print(f"{'='*72}")
        if n_files == 0:
            print("  No CSV files found, skipping.")
            continue

        day_results: list[dict] = []
        t_site_start = time.time()

        for i, csv_path in enumerate(csv_files, 1):
            res = run_one_day(csv_path, site)
            if res is None:
                continue

            day_results.append(res)
            cfg  = res["config_label"]
            cost = res["total_op_cost"]
            svc  = res["vehicles_served_pct"]
            st   = res["solve_time_s"]

            if i % 20 == 0 or i == n_files or i == 1:
                elapsed_total = time.time() - t_site_start
                rate = i / elapsed_total if elapsed_total > 0 else 0
                eta_s = (n_files - i) / rate if rate > 0 else 0
                print(f"  [{i:3d}/{n_files}]  {res['date']}  "
                      f"cfg={cfg:<32}  cost=${cost:,.0f}  svc={svc:.0f}%  "
                      f"t={st:.0f}s  ETA={eta_s/60:.1f}min")

        if not day_results:
            print("  No valid days solved.")
            continue

        # ── Sort by total operational cost, worst first ─────────────────────
        day_results.sort(key=lambda r: r["total_op_cost"], reverse=True)
        for rank, r in enumerate(day_results, 1):
            r["cost_rank"] = rank

        # ── Worst 10 days ────────────────────────────────────────────────────
        worst10 = day_results[:10]
        worst10_dates = {r["date"] for r in worst10}

        # ── Recommended config: majority vote on worst 10 ────────────────────
        cfg_counts = Counter(r["config_label"] for r in worst10)
        rec_cfg    = cfg_counts.most_common(1)[0][0]
        rec_cfg_count = cfg_counts[rec_cfg]
        avg_svc_worst10 = sum(r["vehicles_served_pct"] for r in worst10) / len(worst10)

        print(f"\n  Recommended config: {rec_cfg}")
        print(f"    Appears on {rec_cfg_count}/10 worst days")
        print(f"    Avg vehicle svc on 10 worst days: {avg_svc_worst10:.1f}%")
        print(f"    Config vote breakdown: {dict(cfg_counts)}")

        # ── All-days CSV ─────────────────────────────────────────────────────
        export_cols = [
            "date", "cost_rank", "config_label",
            "N_L2", "N_DC50", "N_DC150", "N_DC350",
            "capex_daily", "energy_cost", "demand_global", "demand_peak_win", "total_op_cost",
            "peak_kw", "peak_win_kw",
            "n_vehicles", "n_fully_served", "n_partially_served",
            "n_vehicles_served", "n_unserved", "vehicles_served_pct",
            "energy_demanded_kwh", "energy_delivered_kwh", "energy_served_pct",
            "solve_time_s", "mip_gap", "solver_status",
        ]
        df_all = pd.DataFrame(day_results)[export_cols].copy()
        df_all["is_worst10"] = df_all["date"].isin(worst10_dates)
        csv_path_all = OUT_DIR / f"{site}_all_days_milp.csv"
        df_all.to_csv(csv_path_all, index=False)
        print(f"  Saved: {csv_path_all.name}")

        # ── Write Excel sheet: all days ──────────────────────────────────────
        sheet = site_label.replace(" ", "")
        df_all.to_excel(xl, sheet_name=f"{sheet}_AllDays", index=False)

        # ── Worst-10 schedule (all vehicles) ─────────────────────────────────
        sched_rows_all = []
        for r in worst10:
            sched = r.get("_sched", [])
            for row in sched:
                row["date"]           = r["date"]
                row["worst_day_rank"] = r["cost_rank"]
                row["total_op_cost"]  = r["total_op_cost"]
                row["config_label"]   = r["config_label"]
            sched_rows_all.extend(sched)

        if sched_rows_all:
            df_sched = pd.DataFrame(sched_rows_all)
            lead = ["date", "worst_day_rank", "total_op_cost", "config_label"]
            rest = [c for c in df_sched.columns if c not in lead]
            df_sched = df_sched[lead + rest]
            csv_path_sched = OUT_DIR / f"{site}_worst10_schedule.csv"
            df_sched.to_csv(csv_path_sched, index=False)
            df_sched.to_excel(xl, sheet_name=f"{sheet}_Worst10Sched", index=False)
            print(f"  Saved: {csv_path_sched.name}  ({len(df_sched)} vehicle-day rows)")

        # ── Site summary row ─────────────────────────────────────────────────
        all_rows_site = [r for r in day_results if r["config_label"] == rec_cfg]
        avg_cost_all  = sum(r["total_op_cost"] for r in day_results) / len(day_results)
        p90_cost      = float(np.percentile([r["total_op_cost"] for r in day_results], 90))
        max_cost      = max(r["total_op_cost"] for r in day_results)
        avg_svc_all   = sum(r["vehicles_served_pct"] for r in day_results) / len(day_results)

        all_site_summary.append({
            "site":             site_label,
            "utility":          utility,
            "n_days":           len(day_results),
            "recommended_config": rec_cfg,
            "cfg_vote_worst10": f"{rec_cfg_count}/10",
            "avg_svc_worst10%": round(avg_svc_worst10, 1),
            "avg_svc_all%":     round(avg_svc_all, 1),
            "avg_total_cost":   round(avg_cost_all, 2),
            "p90_total_cost":   round(p90_cost, 2),
            "max_total_cost":   round(max_cost, 2),
            "worst10_dates":    "; ".join(r["date"] for r in worst10),
        })

        site_elapsed = time.time() - t_site_start
        print(f"\n  Site complete: {len(day_results)} days solved in {site_elapsed/60:.1f} min")

    # ── Cross-site summary sheet ──────────────────────────────────────────────
    if all_site_summary:
        df_summary = pd.DataFrame(all_site_summary)
        df_summary.to_excel(xl, sheet_name="Summary", index=False)
        print(f"\n{'='*72}")
        print("  CROSS-SITE SUMMARY")
        print(f"{'='*72}")
        print(df_summary[["site","utility","n_days","recommended_config",
                           "avg_svc_worst10%","avg_total_cost",
                           "p90_total_cost"]].to_string(index=False))

print(f"\nSaved: {OUT_DIR / 'Fixed_Charger_MILP_Results.xlsx'}")
print("DONE.")
