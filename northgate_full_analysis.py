"""
northgate_full_analysis.py
==========================
Comprehensive daily analysis of XOS Hub MC02 scenarios (A1 + A2 with
proactive recharge) for all Northgate days.

Outputs (written to scenario_outputs/northgate_analysis/):
  northgate_summary.csv          — one row per (day × scenario)
  northgate_cost_detail.csv      — cost component breakdown
  northgate_sanity_log.csv       — per-day sanity check results
  northgate_analysis_report.txt  — human-readable summary

Usage:
    python northgate_full_analysis.py
"""
from __future__ import annotations

import io, sys, glob, re, math, contextlib
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import scenario_runner as sr
from charger_costs_xos_hub import XOS_HUB_SPECS, electrical_infra_cost

# ── Constants ──────────────────────────────────────────────────────────────────
SITE_DIR    = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
OUT_DIR     = SITE_DIR / "scenario_outputs" / "northgate_analysis"
CSV_STEM    = "z2z_milp_events_northgate"
MAX_K       = sr._XOS["MAX_UNITS"]
NP          = sr._XOS["N_PORTS"]
PP          = sr._XOS["P_PORT"]
ED          = sr._XOS["ETA_D"]
DT          = sr._XOS["DT_H"]
SMIN        = sr._XOS["SOC_MIN"]
SMAX        = sr._XOS["SOC_MAX"]
PG          = sr._XOS["P_GRID"]
DAYS_PER_YEAR = 365.25

S           = XOS_HUB_SPECS
PURCHASE    = S["purchase_cost"]       # $/unit
ANNUAL_MAINT    = S["annual_maint"]    # $/unit/yr
ANNUAL_WARRANTY = S["annual_warranty"] # $/unit/yr
LIFE_YEARS  = S["life_years"]          # 10


# ── Sanity checker ─────────────────────────────────────────────────────────────

def sanity_check(sim: dict, events_ext: pd.DataFrame, date_str: str,
                 scenario: str) -> dict:
    """
    Run post-simulation sanity checks. Returns a dict with pass/fail and details.

    Checks:
      1. Per-step delivery ≤ PP × DT × ED (port power limit)
      2. Vehicle served only within its dwell window
      3. Per-hub energy per step ≤ PP × NP × DT (hub port capacity)
      4. Simultaneous vehicles per hub ≤ NP
      5. Delivered energy consistency (sim dict vs dispatch log)
      6. SOC stays within [SMIN, SMAX] at all times
      7. No vehicle energy ≤ 0 delivered when marked fully served
      8. Hub count is physically reasonable (K ≤ MAX_K)
      9. Peak charging power is physically plausible
    """
    issues: list[str] = []
    K = sim["n_units"]

    # (8) Hub count
    if K > MAX_K:
        issues.append(f"UNREASONABLE_K: K={K} > MAX_K={MAX_K}")
    if K == 0:
        issues.append("ZERO_K: no hubs deployed")

    df = pd.DataFrame(sim["dispatch_log"]) if sim["dispatch_log"] else pd.DataFrame()
    soc_df = pd.DataFrame(sim["soc_history"])
    soc_df["time_utc"] = pd.to_datetime(soc_df["time_utc"], utc=True)

    # (6) SOC bounds
    soc_cols = [c for c in soc_df.columns if c.startswith("soc_unit_")]
    for col in soc_cols:
        if (soc_df[col] < SMIN - 0.005).any():
            bad = soc_df[soc_df[col] < SMIN - 0.005][col].min()
            issues.append(f"SOC_BELOW_MIN: {col} min={bad:.4f} (floor={SMIN})")
        if (soc_df[col] > SMAX + 0.005).any():
            bad = soc_df[soc_df[col] > SMAX + 0.005][col].max()
            issues.append(f"SOC_ABOVE_MAX: {col} max={bad:.4f}")

    if df.empty:
        return {
            "date": date_str, "scenario": scenario, "pass": not issues,
            "n_checks": 6, "n_violations": len(issues), "details": issues
        }

    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)

    # Event dwell map
    ev_map: dict = {}
    for _, row in events_ext.iterrows():
        ev_map[row["charging_event_id"]] = {
            "arr": row["arrival_time"], "dep": row["departure_time"]
        }

    # (1) Per-step delivery ≤ PP × DT × ED per port
    max_per_step = PP * DT * ED * 1.02   # 2% tolerance for float rounding
    bad_power = df[df["energy_to_vehicle_kwh"] > max_per_step]
    if not bad_power.empty:
        worst = bad_power["energy_to_vehicle_kwh"].max()
        issues.append(
            f"PORT_POWER_EXCESS: {len(bad_power)} steps > {max_per_step:.2f} kWh "
            f"(max={worst:.3f}, limit={PP*DT*ED:.2f})")

    # (2) Vehicle served within dwell window
    window_violations = 0
    for _, row in df.iterrows():
        v = row["event_id"]
        if v not in ev_map:
            continue
        t = row["time_utc"]
        step_end = t + pd.Timedelta(hours=DT)
        # Step starts before arrival or ends after departure → violation
        if step_end <= ev_map[v]["arr"] or t >= ev_map[v]["dep"]:
            window_violations += 1
    if window_violations > 0:
        issues.append(
            f"WINDOW_VIOLATION: {window_violations} dispatch steps outside vehicle dwell")

    # (3) Per-hub energy per step ≤ PP × NP × DT
    hub_step = df.groupby(["step_idx", "unit"])["energy_to_vehicle_kwh"].sum()
    max_hub  = PP * NP * DT * 1.02
    bad_hub  = hub_step[hub_step > max_hub]
    if not bad_hub.empty:
        issues.append(
            f"HUB_POWER_EXCESS: {len(bad_hub)} hub-steps > {max_hub:.2f} kWh "
            f"(max={bad_hub.max():.3f})")

    # (4) Simultaneous vehicles per hub ≤ NP
    hub_vehicle_count = df.groupby(["step_idx", "unit"])["event_id"].nunique()
    over_capacity = hub_vehicle_count[hub_vehicle_count > NP]
    if not over_capacity.empty:
        issues.append(
            f"PORT_CAPACITY_EXCEEDED: {len(over_capacity)} hub-steps with >{NP} vehicles")

    # (5) Delivered energy consistency (sim dict vs log sum)
    log_delivered = df.groupby("event_id")["energy_to_vehicle_kwh"].sum()
    for v, del_v in sim["delivered"].items():
        log_del = log_delivered.get(v, 0.0)
        if abs(del_v - log_del) > 1.0:   # 1 kWh tolerance
            issues.append(
                f"ENERGY_MISMATCH: {v} dict={del_v:.2f} log={log_del:.2f} "
                f"delta={abs(del_v-log_del):.2f}")

    # (7) Fully served vehicles must have delivered > 0
    for v in sim["delivered"]:
        if sim["remaining"].get(v, 0) <= sr.ENERGY_TOL:
            if sim["delivered"][v] < sr.ENERGY_TOL:
                issues.append(f"SERVED_ZERO_ENERGY: {v} marked served but delivered=0")

    # (9) Peak power plausibility: max grid draw ≤ K × PG × 1.01
    if sim["grid_draw"]:
        peak = max(sim["grid_draw"])
        max_possible = K * PG * 1.01
        if peak > max_possible:
            issues.append(
                f"PEAK_POWER_IMPLAUSIBLE: {peak:.0f} kW > K×PG={max_possible:.0f} kW")

    n_checks = 9
    return {
        "date":         date_str,
        "scenario":     scenario,
        "pass":         len(issues) == 0,
        "n_checks":     n_checks,
        "n_violations": len(issues),
        "details":      issues,
    }


# ── Cost decomposer ────────────────────────────────────────────────────────────

def cost_breakdown(sim: dict, date_str: str) -> dict:
    """
    Detailed cost decomposition for a single XOS simulation result.

    Returns a dict with every cost component split out clearly.
    Demand charges are reported as monthly proxies (not per-day) because
    they depend on the peak day of the billing month, not each individual day.
    """
    K     = sim["n_units"]
    costs = sr._xos_a1_cost(sim, date_str)   # existing cost function

    # Infrastructure (mid estimate) — amortized over equipment life
    infra       = electrical_infra_cost(K, "mid")
    infra_total = infra["total"]
    infra_daily = infra_total / (LIFE_YEARS * DAYS_PER_YEAR)

    # Per-unit CapEx components (daily)
    purchase_daily_pu = PURCHASE / (LIFE_YEARS * DAYS_PER_YEAR)
    maint_daily_pu    = ANNUAL_MAINT / DAYS_PER_YEAR
    warranty_daily_pu = ANNUAL_WARRANTY / DAYS_PER_YEAR

    # Fleet totals
    purchase_capex = purchase_daily_pu * K
    maint_total    = maint_daily_pu    * K
    warranty_total = warranty_daily_pu * K

    energy_cost = costs["energy_cost"]
    total_grid_kwh  = costs["total_grid_kwh"]
    vehicle_kwh     = costs["vehicle_kwh"]
    p_max_kw        = costs["p_max_kw"]
    p_peak_win_kw   = costs["p_peak_win_kw"]

    # Demand charge proxies (monthly — NOT per day)
    demand_global_monthly   = costs["demand_global"]     # = p_max × $6.454
    demand_peak_win_monthly = costs["demand_peak_win"]   # = p_peak_win × $9.960

    # Total variable daily cost (excludes demand — demand is a monthly billing concept)
    total_daily_var = purchase_capex + infra_daily + maint_total + warranty_total + energy_cost

    # Amortized daily cost including demand (dividing monthly proxy by 30)
    total_daily_incl_demand = total_daily_var + demand_global_monthly / 30

    return {
        # Identification
        "date":                     date_str,
        "K":                        K,
        # CapEx components ($/day)
        "purchase_capex_daily":     round(purchase_capex, 2),
        "infra_capex_daily":        round(infra_daily, 2),
        "maint_daily":              round(maint_total, 2),
        "warranty_daily":           round(warranty_total, 2),
        "energy_cost_daily":        round(energy_cost, 2),
        # Demand (monthly proxies, clearly labeled)
        "demand_global_monthly_$":  round(demand_global_monthly, 2),
        "demand_peak_win_monthly_$":round(demand_peak_win_monthly, 2),
        # Totals
        "total_daily_excl_demand":  round(total_daily_var, 2),
        "total_daily_incl_demand":  round(total_daily_incl_demand, 2),
        # Energy
        "total_grid_kwh":           round(total_grid_kwh, 2),
        "vehicle_kwh_delivered":    round(vehicle_kwh, 2),
        # Power
        "peak_grid_kw":             round(p_max_kw, 1),
        "peak_win_kw":              round(p_peak_win_kw, 1),
        # Infrastructure detail
        "infra_total_mid_$":        round(infra_total, 0),
        "infra_per_unit_mid_$":     round(infra["per_unit_avg"], 0),
    }


# ── Utilization metrics ────────────────────────────────────────────────────────

def utilization_metrics(sim: dict) -> dict:
    """Hub and port utilization from dispatch log and state history."""
    K           = sim["n_units"]
    total_steps = sim["n_steps"]
    df          = pd.DataFrame(sim["dispatch_log"]) if sim["dispatch_log"] else pd.DataFrame()

    if df.empty or total_steps == 0:
        return {
            "hub_utilization_pct":  0.0,
            "port_utilization_pct": 0.0,
            "avg_ports_active_per_hub": 0.0,
            "recharge_steps_total": 0,
            "serving_steps_total":  0,
            "idle_steps_total":     0,
        }

    # Port utilization: (port-steps with energy > 0) / (K × NP × total_steps)
    active_port_steps = len(df[df["energy_to_vehicle_kwh"] > sr.ENERGY_TOL])
    total_port_steps  = K * NP * total_steps
    port_util = 100 * active_port_steps / max(total_port_steps, 1)

    # Hub utilization: fraction of hub-steps where ≥1 vehicle is served
    hub_step_active = df[df["energy_to_vehicle_kwh"] > sr.ENERGY_TOL].groupby(
        ["step_idx", "unit"]).size().reset_index()["unit"].value_counts()
    hub_active_steps = hub_step_active.sum() if not hub_step_active.empty else 0
    hub_util = 100 * hub_active_steps / max(K * total_steps, 1)

    avg_ports = active_port_steps / max(K * total_steps, 1)

    # State breakdown from SOC history
    state_df = pd.DataFrame(sim["soc_history"])
    state_cols = [c for c in state_df.columns if c.startswith("state_unit_")]
    all_states = state_df[state_cols].values.flatten()
    n_recharge = int((all_states == "recharging").sum())
    n_serving  = int((all_states == "serving").sum())
    n_idle     = int((all_states == "idle").sum())

    return {
        "hub_utilization_pct":      round(hub_util, 1),
        "port_utilization_pct":     round(port_util, 1),
        "avg_ports_active_per_hub": round(avg_ports, 2),
        "recharge_steps_total":     n_recharge,
        "serving_steps_total":      n_serving,
        "idle_steps_total":         n_idle,
    }


# ── Per-day simulation runner ──────────────────────────────────────────────────

def _save_day_outputs(sim: dict, events_ext: pd.DataFrame,
                      day_dir: Path, mode: str, date_str: str) -> None:
    """Save per-day dispatch log, SOC state history, and grid draw to day_dir."""
    day_dir.mkdir(parents=True, exist_ok=True)
    tag = mode.upper()

    # Dispatch log
    if sim["dispatch_log"]:
        df_disp = pd.DataFrame(sim["dispatch_log"])
        df_disp["time_pac"] = pd.to_datetime(
            df_disp["time_utc"], utc=True
        ).dt.tz_convert(sr.SMUD_TZ).dt.strftime("%H:%M")
        df_disp.to_csv(day_dir / f"{tag}_dispatch_{date_str}.csv", index=False)

    # SOC state history
    pd.DataFrame(sim["soc_history"]).to_csv(
        day_dir / f"{tag}_state_{date_str}.csv", index=False)

    # Grid draw timeseries
    time_utc = [r["time_utc"] for r in sim["soc_history"]]
    time_pac = [
        pd.Timestamp(t, tz="UTC").tz_convert(sr.SMUD_TZ).strftime("%H:%M")
        for t in time_utc
    ]
    pd.DataFrame({
        "time_utc": time_utc,
        "time_pac": time_pac,
        "grid_kw":  sim["grid_draw"],
    }).to_csv(day_dir / f"{tag}_grid_draw_{date_str}.csv", index=False)

    # Vehicle-level results summary
    rows = []
    for _, row in events_ext.sort_values("arrival_time").iterrows():
        v = row["charging_event_id"]
        needed    = float(row["energy_needed_kwh_for_visit"])
        delivered = sim["delivered"].get(v, 0.0)
        remaining = sim["remaining"].get(v, needed)
        status = (
            "fully_served"    if remaining <= sr.ENERGY_TOL else
            "partially_served" if delivered  > sr.ENERGY_TOL else
            "unserved"
        )
        rows.append({
            "event_id":    v,
            "model":       row.get("ev_equivalent_model", ""),
            "arrival_pac": pd.Timestamp(row["arrival_time"]).tz_convert(
                               sr.SMUD_TZ).strftime("%H:%M"),
            "departure_pac": pd.Timestamp(row["departure_time"]).tz_convert(
                               sr.SMUD_TZ).strftime("%H:%M"),
            "energy_needed_kwh":    round(needed,    2),
            "energy_delivered_kwh": round(delivered, 2),
            "energy_unmet_kwh":     round(remaining, 2),
            "status": status,
        })
    pd.DataFrame(rows).to_csv(
        day_dir / f"{tag}_vehicle_results_{date_str}.csv", index=False)


def run_one_day(csv_path: Path, date_str: str,
                per_day_root: Path | None = None) -> dict | None:
    """
    Run A1 + A2 scenarios for one day. Returns a dict with all metrics,
    or None if the events file is empty/invalid.

    If per_day_root is given, per-day CSVs (dispatch, state, grid draw,
    vehicle results) are written to per_day_root / date_str / .
    """
    stem_parts    = csv_path.stem.rsplit("_", 3)
    site_csv_stem = "_".join(stem_parts[:-3]) if len(stem_parts) > 3 else csv_path.stem

    # Load + preprocess
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            events_raw = sr.load_site_day_data(csv_path)
            events_raw = sr.apply_multiday_rule(
                events_raw, date_str,
                site_csv_dir=csv_path.parent,
                site_csv_stem=site_csv_stem,
            )
    except Exception as e:
        return {"error": str(e), "date": date_str}

    if events_raw.empty:
        return None

    events_ext = sr._xos_extended_dwell(events_raw)
    n_events   = len(events_ext)

    results = {}
    for mode in ("a1", "a2"):
        # Min-K search (silent)
        best_served = 0
        best_K      = 1
        sim_best    = None
        for K in range(1, MAX_K + 1):
            sim = sr._simulate_xos(events_ext, K, mode=mode)
            if sim["n_served"] > best_served:
                best_served = sim["n_served"]
                best_K      = K
                sim_best    = sim
            if sim["n_served"] >= sim["n_vehicles"]:
                break

        sim = sim_best

        # Save per-day files if a root directory is provided
        if per_day_root is not None:
            day_dir = per_day_root / date_str
            _save_day_outputs(sim, events_ext, day_dir, mode, date_str)

        # Sanity checks
        sc = sanity_check(sim, events_ext, date_str, mode.upper())

        # Service breakdown
        n_total   = sim["n_vehicles"]
        n_served  = sim["n_served"]
        n_partial = sum(
            1 for v, r in sim["remaining"].items()
            if r > sr.ENERGY_TOL and sim["delivered"].get(v, 0) > sr.ENERGY_TOL
        )
        n_unserved = sum(
            1 for v in sim["delivered"]
            if sim["delivered"][v] <= sr.ENERGY_TOL
        )
        energy_unmet = sum(sim["remaining"].values())
        energy_demanded = sum(
            float(row["energy_needed_kwh_for_visit"])
            for _, row in events_ext.iterrows()
        )

        # Model info per unserved / partial vehicle
        partial_vehicles = [
            v for v, r in sim["remaining"].items()
            if r > sr.ENERGY_TOL and sim["delivered"].get(v, 0) > sr.ENERGY_TOL
        ]
        unserved_vehicles = [
            v for v in sim["delivered"]
            if sim["delivered"][v] <= sr.ENERGY_TOL
        ]

        # Cost + utilization
        cost  = cost_breakdown(sim, date_str)
        util  = utilization_metrics(sim)

        results[mode] = {
            "date":           date_str,
            "scenario":       mode.upper(),
            "n_events":       n_events,
            "K":              sim["n_units"],
            "n_vehicles":     n_total,
            "n_fully_served": n_served,
            "n_partial":      n_partial,
            "n_unserved":     n_unserved,
            "energy_demanded_kwh":  round(energy_demanded, 1),
            "energy_delivered_kwh": round(sum(sim["delivered"].values()), 1),
            "energy_unmet_kwh":     round(energy_unmet, 1),
            "service_rate_pct":     round(100 * n_served / max(n_total, 1), 1),
            # Partial/unserved vehicle IDs (compact)
            "partial_vehicles":  ", ".join(partial_vehicles[:5]),
            "unserved_vehicles": ", ".join(unserved_vehicles[:5]),
            # Sanity
            "sanity_pass":      sc["pass"],
            "sanity_issues":    "; ".join(sc["details"]),
            **cost,
            **util,
        }

    return results


# ── Main analysis loop ────────────────────────────────────────────────────────

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Find all Northgate event files, sorted by date
    pattern = str(SITE_DIR / f"{CSV_STEM}_*.csv")
    all_files = sorted(glob.glob(pattern))
    n_files   = len(all_files)
    print(f"Found {n_files} Northgate event files.")
    print(f"Output directory: {OUT_DIR}")
    print()

    rows_summary: list[dict] = []
    rows_cost:    list[dict] = []
    rows_sanity:  list[dict] = []

    for i, fpath in enumerate(all_files, 1):
        # Extract date from filename: z2z_milp_events_northgate_YYYY_MM_DD.csv
        m = re.search(r"(\d{4}_\d{2}_\d{2})\.csv$", fpath)
        if not m:
            continue
        date_str = m.group(1).replace("_", "-")   # YYYY-MM-DD

        pct = 100 * i / n_files
        print(f"  [{i:3d}/{n_files}] {date_str}  ({pct:.0f}%)", end="  ", flush=True)

        per_day_root = OUT_DIR / "per_day"
        result = run_one_day(Path(fpath), date_str, per_day_root=per_day_root)

        if result is None:
            print("skipped (no events)")
            continue
        if "error" in result:
            print(f"ERROR: {result['error']}")
            continue

        for mode in ("a1", "a2"):
            if mode not in result:
                continue
            r = result[mode]
            print(f"{mode.upper()} K={r['K']}  served={r['n_fully_served']}/{r['n_vehicles']}",
                  end="  ", flush=True)

            # Summary row
            rows_summary.append({k: v for k, v in r.items()
                                  if k not in ("partial_vehicles", "unserved_vehicles",
                                               "sanity_issues")})
            # Cost detail row
            rows_cost.append({
                "date":     r["date"],
                "scenario": r["scenario"],
                "K":        r["K"],
                "purchase_capex_daily":      r.get("purchase_capex_daily", 0),
                "infra_capex_daily":         r.get("infra_capex_daily", 0),
                "maint_daily":               r.get("maint_daily", 0),
                "warranty_daily":            r.get("warranty_daily", 0),
                "energy_cost_daily":         r.get("energy_cost_daily", 0),
                "demand_global_monthly_$":   r.get("demand_global_monthly_$", 0),
                "demand_peak_win_monthly_$": r.get("demand_peak_win_monthly_$", 0),
                "total_daily_excl_demand":   r.get("total_daily_excl_demand", 0),
                "total_daily_incl_demand":   r.get("total_daily_incl_demand", 0),
                "total_grid_kwh":            r.get("total_grid_kwh", 0),
                "vehicle_kwh_delivered":     r.get("vehicle_kwh_delivered", 0),
                "peak_grid_kw":              r.get("peak_grid_kw", 0),
                "peak_win_kw":               r.get("peak_win_kw", 0),
                "infra_total_mid_$":         r.get("infra_total_mid_$", 0),
            })
            # Sanity row
            rows_sanity.append({
                "date":           r["date"],
                "scenario":       r["scenario"],
                "pass":           r["sanity_pass"],
                "n_violations":   0 if r["sanity_pass"] else r["sanity_issues"].count(":"),
                "details":        r["sanity_issues"],
            })

        print()   # newline after A1/A2

    # ── Save CSVs ──────────────────────────────────────────────────────────────
    df_summary = pd.DataFrame(rows_summary)
    df_cost    = pd.DataFrame(rows_cost)
    df_sanity  = pd.DataFrame(rows_sanity)

    df_summary.to_csv(OUT_DIR / "northgate_summary.csv",      index=False)
    df_cost.to_csv(   OUT_DIR / "northgate_cost_detail.csv",  index=False)
    df_sanity.to_csv( OUT_DIR / "northgate_sanity_log.csv",   index=False)

    print(f"\nSaved summary CSVs to {OUT_DIR}")

    # ── Print comprehensive text report ────────────────────────────────────────
    report_lines = build_report(df_summary, df_cost, df_sanity)
    report_path  = OUT_DIR / "northgate_analysis_report.txt"
    report_text  = "\n".join(report_lines)
    report_path.write_text(report_text, encoding="utf-8")
    print(report_text)
    print(f"\nSaved: {report_path}")


# ── Report builder ─────────────────────────────────────────────────────────────

def build_report(df_sum: pd.DataFrame, df_cost: pd.DataFrame,
                 df_sanity: pd.DataFrame) -> list[str]:
    L = []
    SEP  = "=" * 78
    SEP2 = "-" * 78

    def hdr(title: str):
        L.append(""); L.append(SEP)
        L.append(f"  {title}")
        L.append(SEP)

    hdr(f"NORTHGATE XOS HUB MC02 — FULL ANALYSIS REPORT")
    L.append(f"  Generated : {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    L.append(f"  Days run  : {df_sum['date'].nunique()}")
    L.append(f"  Scenarios : A1 (always-grid) + A2 (disconnect at 20%) with proactive recharge")

    # ── Charger specs ──────────────────────────────────────────────────────────
    hdr("EQUIPMENT SPECIFICATIONS")
    L.append(f"  XOS Hub MC02 (Mobile DCFC Battery Trailer)")
    L.append(f"  {'Purchase price':<32}: ${PURCHASE:>12,.2f}  / unit")
    L.append(f"  {'Annual maintenance':<32}: ${ANNUAL_MAINT:>12,.0f}  / unit / yr")
    L.append(f"  {'Annual warranty':<32}: ${ANNUAL_WARRANTY:>12,.0f}  / unit / yr")
    L.append(f"  {'Equipment lifespan':<32}:  {LIFE_YEARS} years")
    L.append(f"  {'Battery capacity':<32}:  282 kWh nominal  (225.6 kWh usable @ 20% floor)")
    L.append(f"  {'Charge ports':<32}:  {NP} × CCS1 @ {PP:.0f} kW each")
    L.append(f"  {'Discharge efficiency':<32}:  {ED*100:.0f}%  (battery → vehicle)")
    L.append(f"  {'Grid recharge rate':<32}:  {PG:.0f} kW  (480V 3-phase 100A)")
    L.append(f"  {'Charge efficiency':<32}:  95%  (grid → battery)")
    L.append(f"  {'Deliverable per cycle':<32}: ~214 kWh  (225.6 × 0.95 η)")
    L.append(f"  {'Full recharge time':<32}:  2.86 h  (225.6 / 83×0.95)")

    # ── Energy cost structure ──────────────────────────────────────────────────
    hdr("ENERGY & DEMAND COST STRUCTURE (SMUD C&I 21-299 kW)")
    L.append(f"  Energy rates (Time-Of-Day):")
    L.append(f"    Summer (Jun-Sep)  on-peak  (M-F 16-21h) : $0.2341 / kWh")
    L.append(f"    Summer           off-peak               : $0.1215 / kWh")
    L.append(f"    Winter           on-peak  (M-F 16-21h) : $0.1932 / kWh")
    L.append(f"    Winter           shoulder (M-F 09-16h) : $0.1477 / kWh")
    L.append(f"    Winter           off-peak               : $0.0888 / kWh")
    L.append(f"  Demand charges (monthly proxies):")
    L.append(f"    Global demand                           : $6.454 / kW-month")
    L.append(f"    Peak-window demand (M-F 16-21h)         : $9.960 / kW-month")
    L.append(f"  NOTE: Demand charges are MONTHLY.  Values below are monthly proxies,")
    L.append(f"        not per-day costs. Divide by ~30 for a daily equivalent estimate.")

    # ── Infrastructure cost table ──────────────────────────────────────────────
    hdr("BUILDING-SIDE ELECTRICAL INFRASTRUCTURE (mid estimate)")
    L.append(f"  {'K':>3}  {'Shared':>10}  {'Circuits':>10}  {'Tier upgr':>10}  "
             f"{'Total':>10}  {'$/unit avg':>10}")
    L.append(f"  {SEP2[:72]}")
    for k in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 16, 20]:
        ic = electrical_infra_cost(k, "mid")
        L.append(f"  {k:>3}  ${ic['shared_infra']:>9,.0f}  ${ic['circuit_cost']:>9,.0f}"
                 f"  ${ic['tier_upgrades']:>9,.0f}  ${ic['total']:>9,.0f}"
                 f"  ${ic['per_unit_avg']:>9,.0f}")

    # ── Scenario-by-scenario summaries ─────────────────────────────────────────
    for scen in ("A1", "A2"):
        df_s = df_sum[df_sum["scenario"] == scen].copy()
        dc_s = df_cost[df_cost["scenario"] == scen].copy()
        if df_s.empty:
            continue

        label = ("always-grid-connected" if scen == "A1"
                 else "disconnect at 20% SOC")
        hdr(f"SCENARIO {scen} ({label}) — SUMMARY STATISTICS")

        n_days  = len(df_s)
        n_total = df_s["n_vehicles"].sum()
        n_served = df_s["n_fully_served"].sum()
        n_partial = df_s["n_partial"].sum()
        n_unserv = df_s["n_unserved"].sum()

        L.append(f"  Days analyzed        : {n_days}")
        L.append(f"  Total vehicle-visits : {n_total}")
        L.append(f"  Fully served         : {n_served}  ({100*n_served/max(n_total,1):.1f}%)")
        L.append(f"  Partially served     : {n_partial}  ({100*n_partial/max(n_total,1):.1f}%)")
        L.append(f"  Unserved             : {n_unserv}  ({100*n_unserv/max(n_total,1):.1f}%)")
        L.append("")
        L.append(f"  Fleet size (K hubs):")
        L.append(f"    Min / Avg / Max    : {df_s['K'].min()} / {df_s['K'].mean():.1f} / {df_s['K'].max()}")
        L.append(f"    Distribution:")
        k_counts = df_s["K"].value_counts().sort_index()
        for k, cnt in k_counts.items():
            bar = "█" * int(cnt / max(k_counts.max(), 1) * 20)
            L.append(f"      K={k:>2}: {cnt:>4} days  {bar}")
        L.append("")
        L.append(f"  Energy:")
        L.append(f"    Daily grid kWh     : min={dc_s['total_grid_kwh'].min():.0f} "
                 f"avg={dc_s['total_grid_kwh'].mean():.0f} "
                 f"max={dc_s['total_grid_kwh'].max():.0f}")
        L.append(f"    Daily veh. deliver : min={dc_s['vehicle_kwh_delivered'].min():.0f} "
                 f"avg={dc_s['vehicle_kwh_delivered'].mean():.0f} "
                 f"max={dc_s['vehicle_kwh_delivered'].max():.0f}")
        L.append(f"    Peak grid demand   : min={dc_s['peak_grid_kw'].min():.0f} "
                 f"avg={dc_s['peak_grid_kw'].mean():.0f} "
                 f"max={dc_s['peak_grid_kw'].max():.0f} kW")
        L.append("")
        L.append(f"  Daily cost breakdown (median day):")
        med = dc_s.median(numeric_only=True)
        L.append(f"    Purchase CapEx (amortized)  : ${med['purchase_capex_daily']:>8.2f}")
        L.append(f"    Infra CapEx (amortized)     : ${med['infra_capex_daily']:>8.2f}")
        L.append(f"    Maintenance                 : ${med['maint_daily']:>8.2f}")
        L.append(f"    Warranty                    : ${med['warranty_daily']:>8.2f}")
        L.append(f"    Energy cost                 : ${med['energy_cost_daily']:>8.2f}")
        L.append(f"    ─────────────────────────────────────────")
        L.append(f"    Total (excl. demand)        : ${med['total_daily_excl_demand']:>8.2f}")
        L.append(f"    Demand global (monthly→/30) : +${med['demand_global_monthly_$']/30:>7.2f}")
        L.append(f"    ─────────────────────────────────────────")
        L.append(f"    Total incl. demand equiv.   : ${med['total_daily_incl_demand']:>8.2f}")

        L.append("")
        L.append(f"  Charger utilization (median day):")
        L.append(f"    Hub utilization         : {df_s['hub_utilization_pct'].median():.1f}%")
        L.append(f"    Port utilization        : {df_s['port_utilization_pct'].median():.1f}%")
        L.append(f"    Avg ports active/hub    : {df_s['avg_ports_active_per_hub'].median():.2f} / {NP}")

        # Monthly cost annualized (rough)
        avg_cost_yr = dc_s["total_daily_excl_demand"].mean() * 365 + \
                      dc_s["demand_global_monthly_$"].mean() * 12
        L.append("")
        L.append(f"  Annualized cost estimate (avg-day × 365 + demand × 12):")
        L.append(f"    = ${avg_cost_yr:,.0f} / year")

        # Top 5 most expensive days
        top5 = dc_s.nlargest(5, "total_daily_excl_demand")[
            ["date", "K", "total_grid_kwh", "total_daily_excl_demand",
             "demand_global_monthly_$"]
        ]
        L.append("")
        L.append(f"  Top 5 highest-cost days:")
        L.append(f"  {'Date':>12}  {'K':>3}  {'Grid kWh':>9}  "
                 f"{'Daily$':>9}  {'Demand(mo)':>10}")
        L.append(f"  {'-'*50}")
        for _, row in top5.iterrows():
            L.append(f"  {row['date']:>12}  {int(row['K']):>3}  "
                     f"{row['total_grid_kwh']:>9.0f}  "
                     f"${row['total_daily_excl_demand']:>8.2f}  "
                     f"${row['demand_global_monthly_$']:>9.0f}")

    # ── Daily results table (truncated for readability) ────────────────────────
    hdr("DAILY RESULTS — A1 vs A2 COMPARISON (all days)")
    L.append(f"  {'Date':>12}  {'A1_K':>4}  {'A1_srv':>6}  {'A1_cost':>8}  "
             f"{'A2_K':>4}  {'A2_srv':>6}  {'A2_cost':>8}  "
             f"{'Nveh':>5}  {'SanOK':>5}")
    L.append(f"  {SEP2}")

    a1 = df_sum[df_sum["scenario"] == "A1"].set_index("date")
    a2 = df_sum[df_sum["scenario"] == "A2"].set_index("date")
    dates = sorted(set(a1.index) | set(a2.index))
    for d in dates:
        r1 = a1.loc[d] if d in a1.index else None
        r2 = a2.loc[d] if d in a2.index else None
        nveh = int(r1["n_vehicles"]) if r1 is not None else 0

        dc1 = df_cost[(df_cost["date"] == d) & (df_cost["scenario"] == "A1")]
        dc2 = df_cost[(df_cost["date"] == d) & (df_cost["scenario"] == "A2")]
        c1  = dc1["total_daily_excl_demand"].values[0] if not dc1.empty else 0
        c2  = dc2["total_daily_excl_demand"].values[0] if not dc2.empty else 0

        san = ""
        if r1 is not None and not r1["sanity_pass"]:
            san = "FAIL"
        elif r2 is not None and not r2["sanity_pass"]:
            san = "FAIL"
        else:
            san = "OK"

        k1  = int(r1["K"]) if r1 is not None else 0
        k2  = int(r2["K"]) if r2 is not None else 0
        s1  = f"{int(r1['n_fully_served'])}/{nveh}" if r1 is not None else "—"
        s2  = f"{int(r2['n_fully_served'])}/{nveh}" if r2 is not None else "—"
        L.append(f"  {d:>12}  {k1:>4}  {s1:>6}  ${c1:>7.2f}  "
                 f"{k2:>4}  {s2:>6}  ${c2:>7.2f}  {nveh:>5}  {san:>5}")

    # ── Sanity check summary ───────────────────────────────────────────────────
    hdr("SANITY CHECK SUMMARY")
    total_checks = len(df_sanity)
    n_pass       = df_sanity["pass"].sum()
    n_fail       = total_checks - n_pass
    L.append(f"  Total scenario-days checked : {total_checks}")
    L.append(f"  Passed                      : {n_pass}")
    L.append(f"  Failed                      : {n_fail}")
    if n_fail > 0:
        L.append("")
        L.append(f"  Failure details:")
        for _, row in df_sanity[~df_sanity["pass"]].iterrows():
            L.append(f"    {row['date']} {row['scenario']}: {row['details'][:120]}")
    else:
        L.append(f"  All sanity checks passed — no physical violations found.")

    hdr("END OF REPORT")
    return L


if __name__ == "__main__":
    main()
