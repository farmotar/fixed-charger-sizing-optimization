"""
fixed_charger_analysis_all_sites.py
=====================================
Fixed DCFC charger optimization and worst-day analysis for all 4 Caltrans sites.

Cost assumptions (finalized):
  Level 2 AC   (19.2 kW)  purchase=$11k  install=$14k  life=10yr  O&M=$550/yr   => $8.36/day
  Low-power DC  (50 kW)   purchase=$50k  install=$50k  life=10yr  O&M=$1,750/yr => $32.19/day
  Medium-power DC(150 kW) purchase=$90k  install=$110k life=10yr  O&M=$3,000/yr => $63.01/day
  High-power DC (350 kW)  purchase=$160k install=$225k life=10yr  O&M=$4,500/yr => $117.80/day

Workflow
--------
Phase 1 -- Full-year simulation
  For every day x every charger configuration x every site run a greedy simulation
  and collect service metrics + daily cost.

Phase 2 -- Site-level optimal config selection
  For each site choose the configuration that achieves the best service rate at
  minimum daily cost (ties broken by energy served %).

Phase 3 -- Worst-day ranking
  Using the site's selected configuration, rank all days by total daily cost.
  Select the 10 highest-cost days per site.

Phase 4 -- Worst-day configuration comparison
  For each of the 10 worst days, report results for ALL configurations so the
  reader can see how each option would have performed on a hard day.

Phase 5 -- Output
  Per-site CSV tables, worst-day detail tables, and a cross-site summary report.
"""

from __future__ import annotations

import glob
import re
import sys
from pathlib import Path
from datetime import datetime

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# -- Paths ----------------------------------------------------------------------
BASE_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
OUT_DIR  = BASE_DIR / "fixed_charger_outputs"

# -- Simulation constants -------------------------------------------------------
DT_MINUTES     = 5
DT_HOURS       = DT_MINUTES / 60.0
ETA            = 0.90          # grid-to-battery charging efficiency
ENERGY_TOL     = 0.05          # kWh -- below this counts as "unserved"
SMUD_TZ        = "America/Los_Angeles"
DAYS_PER_MONTH = 30.42

# -- SMUD C&I Secondary 21-299 kW TOD energy rates ($/kWh) ---------------------
C_ENERGY_SUMMER_PEAK        = 0.2341   # weekday 4-9 pm, Jun-Sep
C_ENERGY_SUMMER_OFFPEAK     = 0.1215
C_ENERGY_NONSUMMER_PEAK     = 0.1477   # weekday 4-9 pm, Oct-May
C_ENERGY_NONSUMMER_OFFSAVER = 0.0888   # 9 am-4 pm, Oct-May
C_ENERGY_NONSUMMER_OFFPEAK  = 0.1264

# -- Demand charges -------------------------------------------------------------
C_DEMAND_GLOBAL   = 6.454    # $/kW -- global peak
C_DEMAND_PEAK_WIN = 9.960    # $/kW -- peak during 4-9 pm
PEAK_WIN_START_H  = 16.0
PEAK_WIN_END_H    = 21.0

# -- Finalized charger specifications ------------------------------------------
CHARGER_SPECS: dict[str, dict] = {
    "L2_19p2kW": {
        "ac_dc":        "AC",
        "power_kw":      19.2,
        "purchase_cost": 11_000,
        "install_cost":  14_000,
        "annual_maint":     550,
        "life_years":        10,
        "label":        "Level 2 AC (19.2 kW)",
        "short":        "L2 19.2kW",
    },
    "DC_50kW": {
        "ac_dc":        "DC",
        "power_kw":      50.0,
        "purchase_cost": 50_000,
        "install_cost":  50_000,
        "annual_maint":   1_750,
        "life_years":        10,
        "label":        "Low-power DCFC (50 kW)",
        "short":        "DC 50kW",
    },
    "DC_150kW": {
        "ac_dc":        "DC",
        "power_kw":      150.0,
        "purchase_cost": 90_000,
        "install_cost": 110_000,
        "annual_maint":   3_000,
        "life_years":        10,
        "label":        "Medium-power DCFC (150 kW)",
        "short":        "DC 150kW",
    },
    "DC_350kW": {
        "ac_dc":        "DC",
        "power_kw":      350.0,
        "purchase_cost": 160_000,
        "install_cost":  225_000,
        "annual_maint":    4_500,
        "life_years":         10,
        "label":        "High-power DCFC (350 kW)",
        "short":        "DC 350kW",
    },
}

# -- Configurations to evaluate  (label, charger_type_key, unit_count) ---------
CONFIGS: list[tuple[str, str, int]] = [
    ("1xL2 (19.2 kW)",  "L2_19p2kW", 1),
    ("1xDC 50 kW",      "DC_50kW",   1),
    ("2xDC 50 kW",      "DC_50kW",   2),
    ("1xDC 150 kW",     "DC_150kW",  1),
    ("2xDC 150 kW",     "DC_150kW",  2),
    ("1xDC 350 kW",     "DC_350kW",  1),
]

# -- Site registry --------------------------------------------------------------
SITES: dict[str, str] = {
    "northgate": "Northgate",
    "fresno":    "Fresno",
    "glendale":  "Glendale",
    "san_diego": "San Diego",
}


# ??????????????????????????????????????????????????????????????????????????????
# COST HELPERS
# ??????????????????????????????????????????????????????????????????????????????

def daily_capex(ctype: str, n_units: int) -> float:
    """Annualised daily CapEx for n_units of charger type ctype."""
    s  = CHARGER_SPECS[ctype]
    mc = (s["purchase_cost"] + s["install_cost"]) / (s["life_years"] * 12)
    mm = s["annual_maint"] / 12
    return n_units * (mc + mm) / DAYS_PER_MONTH


def smud_rate(t_utc: "pd.Timestamp") -> float:
    """Return $/kWh SMUD rate for a UTC timestamp."""
    t_loc     = t_utc.tz_convert(SMUD_TZ)
    hour      = t_loc.hour + t_loc.minute / 60.0
    is_summer = t_loc.month in (6, 7, 8, 9)
    is_wkday  = t_loc.weekday() < 5
    is_peak   = PEAK_WIN_START_H <= hour < PEAK_WIN_END_H
    if is_summer:
        return C_ENERGY_SUMMER_PEAK if (is_wkday and is_peak) else C_ENERGY_SUMMER_OFFPEAK
    if is_wkday and is_peak:
        return C_ENERGY_NONSUMMER_PEAK
    if 9.0 <= hour < 16.0:
        return C_ENERGY_NONSUMMER_OFFSAVER
    return C_ENERGY_NONSUMMER_OFFPEAK


def print_capex_table() -> None:
    print("\n" + "=" * 68)
    print("  FINALIZED CHARGER COST ASSUMPTIONS")
    print("=" * 68)
    fmt = "  {:<22} {:>8} kW  {:>10}  {:>10}  {:>5}yr  {:>10}/day"
    print(fmt.format("Type", "Power", "Purchase", "Install", "Life", "Daily CapEx"))
    print("  " + "-" * 66)
    for k, s in CHARGER_SPECS.items():
        dc = daily_capex(k, 1)
        print(fmt.format(
            s["label"], s["power_kw"],
            f"${s['purchase_cost']:,}", f"${s['install_cost']:,}",
            s["life_years"], f"${dc:,.2f}"))
    print("=" * 68 + "\n")


# ??????????????????????????????????????????????????????????????????????????????
# DATA LOADING
# ??????????????????????????????????????????????????????????????????????????????

def load_day_events(csv_path: Path) -> "pd.DataFrame | None":
    """Load and validate a z2z event CSV.  Returns None if unusable."""
    try:
        df = pd.read_csv(csv_path)
    except Exception:
        return None

    for col in ("arrival_time", "departure_time"):
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")

    if "energy_needed_kwh_for_visit" not in df.columns:
        return None

    df["energy_needed_kwh_for_visit"] = pd.to_numeric(
        df["energy_needed_kwh_for_visit"], errors="coerce")

    mask = (
        df["arrival_time"].notna()
        & df["departure_time"].notna()
        & df["energy_needed_kwh_for_visit"].notna()
        & (df["energy_needed_kwh_for_visit"] > 0)
        & (df["departure_time"] > df["arrival_time"])
    )
    df = df[mask].copy()

    for col in ("max_ac_charge_kw", "max_dc_charge_kw"):
        if col not in df.columns:
            df[col] = 0.0
        else:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    if "charging_event_id" not in df.columns:
        df["charging_event_id"] = [f"ev_{i}" for i in range(len(df))]

    return df if len(df) > 0 else None


# ??????????????????????????????????????????????????????????????????????????????
# GREEDY SIMULATION
# ??????????????????????????????????????????????????????????????????????????????

def greedy_simulate(events_df: "pd.DataFrame", ctype: str, n_chargers: int,
                    config_label: str) -> dict:
    """
    Greedy first-come-first-served simulation for a fixed charger configuration.

    Strategy: at each 5-min time step, assign up to n_chargers available slots
    to vehicles sorted by highest remaining energy need (maximises total energy
    delivered per slot).

    Returns a dict with service metrics and cost breakdown.
    """
    spec      = CHARGER_SPECS[ctype]
    P_rated   = spec["power_kw"]
    is_ac     = spec["ac_dc"] == "AC"

    # Effective charging power per vehicle (limited by vehicle's on-board charger)
    eff_power: dict[str, float] = {}
    for _, row in events_df.iterrows():
        v = row["charging_event_id"]
        if is_ac:
            mac = float(row["max_ac_charge_kw"])
            peff = min(P_rated, mac) if mac > 0 else 0.0
        else:
            mdc = float(row["max_dc_charge_kw"])
            peff = min(P_rated, mdc) if mdc > 0 else 0.0
        eff_power[v] = peff

    # Build 5-min UTC time grid spanning all events
    t_start = events_df["arrival_time"].min().floor(f"{DT_MINUTES}min")
    t_end   = events_df["departure_time"].max().ceil(f"{DT_MINUTES}min")
    time_grid = pd.date_range(t_start, t_end, freq=f"{DT_MINUTES}min", tz="UTC")
    if len(time_grid) < 2:
        return _empty_result(events_df, ctype, n_chargers, config_label)

    # Vehicle state tracking
    energy_needed: dict[str, float] = {}
    arrivals:      dict[str, "pd.Timestamp"] = {}
    departures:    dict[str, "pd.Timestamp"] = {}
    for _, row in events_df.iterrows():
        v = row["charging_event_id"]
        energy_needed[v] = float(row["energy_needed_kwh_for_visit"])
        arrivals[v]      = row["arrival_time"]
        departures[v]    = row["departure_time"]

    remaining  = dict(energy_needed)
    delivered  = {v: 0.0 for v in energy_needed}

    dt_td         = pd.Timedelta(minutes=DT_MINUTES)
    power_profile = []
    rates_list    = []
    peak_win_pwr  = []

    for t in time_grid[:-1]:
        t_next = t + dt_td
        rate   = smud_rate(t)
        t_loc  = t.tz_convert(SMUD_TZ)
        hour   = t_loc.hour + t_loc.minute / 60.0
        in_peak = PEAK_WIN_START_H <= hour < PEAK_WIN_END_H

        # Vehicles present this slot, needing charge, and compatible
        active = [
            v for v in energy_needed
            if arrivals[v] <= t
            and departures[v] >= t_next
            and remaining[v] > ENERGY_TOL
            and eff_power[v] > 0
        ]
        # Prioritise vehicles with most remaining need (fills the most energy per slot)
        active.sort(key=lambda v: -remaining[v])

        step_kw = 0.0
        for i, v in enumerate(active):
            if i >= n_chargers:
                break
            peff            = eff_power[v]
            energy_possible = peff * DT_HOURS * ETA
            charge          = min(remaining[v], energy_possible)
            delivered[v]   += charge
            remaining[v]   -= charge
            step_kw        += peff

        power_profile.append(step_kw)
        rates_list.append(rate)
        if in_peak:
            peak_win_pwr.append(step_kw)

    # -- Cost breakdown ---------------------------------------------------------
    cap_cost    = daily_capex(ctype, n_chargers)
    energy_cost = sum(p * DT_HOURS * r for p, r in zip(power_profile, rates_list))
    p_max       = max(power_profile) if power_profile else 0.0
    p_peak_win  = max(peak_win_pwr)  if peak_win_pwr  else 0.0
    dem_global  = p_max      * C_DEMAND_GLOBAL
    dem_peak    = p_peak_win * C_DEMAND_PEAK_WIN
    total_cost  = cap_cost + energy_cost + dem_global + dem_peak

    # -- Service metrics --------------------------------------------------------
    n_vehicles       = len(energy_needed)
    n_fully_served   = sum(1 for v in remaining if remaining[v] <= ENERGY_TOL)
    n_partial        = sum(1 for v in delivered
                           if delivered[v] > ENERGY_TOL and remaining.get(v, 0) > ENERGY_TOL)
    n_veh_served     = n_fully_served + n_partial
    e_demanded       = sum(energy_needed.values())
    e_served         = sum(delivered.values())

    return {
        "config":              config_label,
        "charger_type":        ctype,
        "n_chargers":          n_chargers,
        "daily_capex":         round(cap_cost,    2),
        "energy_cost":         round(energy_cost, 2),
        "demand_global":       round(dem_global,  2),
        "demand_peak_win":     round(dem_peak,    2),
        "total_cost":          round(total_cost,  2),
        "peak_kw":             round(p_max,       1),
        "peak_win_kw":         round(p_peak_win,  1),
        "energy_demanded_kwh": round(e_demanded,  1),
        "energy_served_kwh":   round(e_served,    1),
        "demand_served_pct":   round(100 * e_served / max(e_demanded, 0.001), 1),
        "n_vehicles":          n_vehicles,
        "n_fully_served":      n_fully_served,
        "n_partially_served":  n_partial,
        "n_vehicles_served":   n_veh_served,
        "vehicles_served_pct": round(100 * n_veh_served / max(n_vehicles, 1), 1),
        "n_unserved":          n_vehicles - n_veh_served,
    }


def _empty_result(events_df, ctype, n_chargers, config_label):
    n_veh = len(events_df)
    return {
        "config": config_label, "charger_type": ctype, "n_chargers": n_chargers,
        "daily_capex": round(daily_capex(ctype, n_chargers), 2),
        "energy_cost": 0.0, "demand_global": 0.0, "demand_peak_win": 0.0,
        "total_cost": round(daily_capex(ctype, n_chargers), 2),
        "peak_kw": 0.0, "peak_win_kw": 0.0,
        "energy_demanded_kwh": 0.0, "energy_served_kwh": 0.0, "demand_served_pct": 0.0,
        "n_vehicles": n_veh, "n_fully_served": 0, "n_partially_served": 0,
        "n_vehicles_served": 0, "vehicles_served_pct": 0.0, "n_unserved": n_veh,
    }


# ??????????????????????????????????????????????????????????????????????????????
# OPTIMAL CONFIG SELECTION
# ??????????????????????????????????????????????????????????????????????????????

def select_optimal_config(day_rows: list[dict]) -> dict:
    """
    Choose the best single-charger config for a given day.

    Priority:
      1. Highest vehicles_served_pct
      2. Lowest total_cost (tie-break on service)
      3. Highest demand_served_pct (secondary energy metric)
    """
    return max(
        day_rows,
        key=lambda r: (
            r["vehicles_served_pct"],
            -r["total_cost"],
            r["demand_served_pct"],
        ),
    )


# ??????????????????????????????????????????????????????????????????????????????
# PER-SITE ANALYSIS
# ??????????????????????????????????????????????????????????????????????????????

def analyze_site(site: str, site_label: str) -> dict:
    """
    Run all phases for one site.  Returns a dict with:
      all_rows       -- flat list of every (day x config) result row
      daily_optimal  -- list of per-day optimal-config results
      worst10        -- list of 10 worst days (by cost under optimal config)
      selected_config-- the configuration recommended for this site
    """
    csv_stem = f"z2z_milp_events_{site}"
    csv_files = sorted(BASE_DIR.glob(f"{csv_stem}_*.csv"))
    n_files = len(csv_files)

    print(f"\n{'='*68}")
    print(f"  {site_label.upper()} -- {n_files} operating days")
    print(f"{'='*68}")
    if n_files == 0:
        print("  No CSV files found -- skipping.")
        return {}

    all_rows: list[dict] = []
    daily_optimal: list[dict] = []

    for i, csv_path in enumerate(csv_files, 1):
        m = re.search(r"(\d{4}_\d{2}_\d{2})\.csv$", csv_path.name)
        if not m:
            continue
        date_str = m.group(1).replace("_", "-")

        events_df = load_day_events(csv_path)
        if events_df is None:
            continue

        day_results: list[dict] = []
        for cfg_label, ctype, n_units in CONFIGS:
            r = greedy_simulate(events_df, ctype, n_units, cfg_label)
            r["date"]       = date_str
            r["site"]       = site
            r["site_label"] = site_label
            all_rows.append(r)
            day_results.append(r)

        if day_results:
            opt = select_optimal_config(day_results)
            daily_optimal.append(opt)

        if i % 50 == 0 or i == n_files:
            pct = 100 * i / n_files
            print(f"  [{i:3d}/{n_files}]  {date_str}  ({pct:.0f}%)", flush=True)

    if not daily_optimal:
        print("  No valid days found.")
        return {}

    # -- Site-level recommended config -----------------------------------------
    # Most frequently selected optimal config; ties broken by average cost
    from collections import Counter
    cfg_counts = Counter(r["config"] for r in daily_optimal)
    top_cfg_label = cfg_counts.most_common(1)[0][0]

    # Confirm: of all rows for this config, compute aggregate service
    top_rows = [r for r in all_rows if r["config"] == top_cfg_label]
    total_veh = sum(r["n_vehicles"] for r in top_rows)
    total_svc = sum(r["n_vehicles_served"] for r in top_rows)
    avg_cost  = sum(r["total_cost"] for r in top_rows) / len(top_rows)

    print(f"\n  => Recommended config  : {top_cfg_label}")
    print(f"    Days selected as optimal: {cfg_counts[top_cfg_label]}/{len(daily_optimal)}")
    print(f"    Annual vehicle svc rate : {100*total_svc/max(total_veh,1):.1f}%")
    print(f"    Average daily cost      : ${avg_cost:,.2f}")

    # -- Worst-day ranking under recommended config -----------------------------
    site_optimal_rows = [r for r in all_rows if r["config"] == top_cfg_label]
    df_opt = pd.DataFrame(site_optimal_rows).sort_values("total_cost", ascending=False)
    worst10_dates = df_opt["date"].head(10).tolist()

    # Collect all-config results for those 10 days
    worst10_all_config: list[dict] = [
        r for r in all_rows if r["date"] in worst10_dates
    ]

    return {
        "all_rows":        all_rows,
        "daily_optimal":   daily_optimal,
        "worst10_dates":   worst10_dates,
        "worst10_rows":    worst10_all_config,
        "selected_config": top_cfg_label,
        "df_all_days_opt": df_opt,
    }


# ??????????????????????????????????????????????????????????????????????????????
# REPORT GENERATION
# ??????????????????????????????????????????????????????????????????????????????

def _worst10_table(site: str, site_label: str, worst10_dates: list[str],
                   worst10_rows: list[dict], selected_cfg: str) -> str:
    """Build an ASCII table for the 10 worst days x all configs."""
    cfg_labels = [c[0] for c in CONFIGS]
    rows_by_date: dict[str, dict[str, dict]] = {}
    for r in worst10_rows:
        rows_by_date.setdefault(r["date"], {})[r["config"]] = r

    col_w = 16
    header_line = (
        f"  {'Date':<12}  {'Config':<{col_w}}  {'Total Cost':>10}  "
        f"{'E Demand':>9}  {'E Served':>9}  {'Dmnd Svc%':>9}  "
        f"{'Vehicles':>8}  {'Veh Svc':>7}  {'Veh%':>6}"
    )
    sep = "  " + "-" * (len(header_line) - 2)

    lines = [
        "=" * 80,
        f"  {site_label.upper()} -- TOP 10 WORST DAYS: FULL CONFIG COMPARISON",
        "=" * 80,
        f"  Selected optimal config: {selected_cfg}",
        f"  Worst days ranked by total daily cost under selected config",
        "",
        header_line,
        sep,
    ]

    for rank, date in enumerate(worst10_dates, 1):
        day_data = rows_by_date.get(date, {})
        first = True
        for cfg_label in cfg_labels:
            r = day_data.get(cfg_label)
            if r is None:
                continue
            star = "*" if cfg_label == selected_cfg else " "
            date_col = f"#{rank} {date}" if first else ""
            lines.append(
                f"  {date_col:<12}  {star}{cfg_label:<{col_w-1}}  "
                f"${r['total_cost']:>9,.2f}  "
                f"{r['energy_demanded_kwh']:>8.1f}  "
                f"{r['energy_served_kwh']:>8.1f}  "
                f"{r['demand_served_pct']:>8.1f}%  "
                f"{r['n_vehicles']:>8d}  "
                f"{r['n_vehicles_served']:>7d}  "
                f"{r['vehicles_served_pct']:>5.1f}%"
            )
            first = False
        lines.append(sep)

    return "\n".join(lines)


def generate_reports(results: dict[str, dict]) -> None:
    """Write all output files and print the final summary."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    summary_lines = [
        "=" * 80,
        "  FIXED CHARGER OPTIMIZATION -- ALL 4 CALTRANS SITES",
        f"  Generated: {timestamp}",
        "",
        "  FINALIZED COST ASSUMPTIONS",
        "  ---------------------------------------------------------------------",
    ]
    for k, s in CHARGER_SPECS.items():
        dc = daily_capex(k, 1)
        summary_lines.append(
            f"    {s['label']:<28}  purchase=${s['purchase_cost']:>8,}"
            f"  install=${s['install_cost']:>8,}"
            f"  life={s['life_years']}yr"
            f"  O&M=${s['annual_maint']:,}/yr"
            f"  => ${dc:.2f}/day"
        )
    summary_lines += ["", "=" * 80, ""]

    all_site_rows = []

    for site, site_label in SITES.items():
        res = results.get(site)
        if not res:
            continue

        # -- 1. Full-year all-config CSV ----------------------------------------
        df_all = pd.DataFrame(res["all_rows"])
        out_all = OUT_DIR / f"{site}_all_days_config_analysis.csv"
        df_all.to_csv(out_all, index=False)
        print(f"  Saved: {out_all.name}")

        # -- 2. Optimal-config per-day ranking CSV ------------------------------
        df_opt = res["df_all_days_opt"]
        out_opt = OUT_DIR / f"{site}_all_days_optimal_ranking.csv"
        df_opt.to_csv(out_opt, index=False)
        print(f"  Saved: {out_opt.name}")

        # -- 3. Worst-10 detail CSV (all configs) ------------------------------
        df_w10 = pd.DataFrame(res["worst10_rows"])
        out_w10 = OUT_DIR / f"{site}_worst10_all_configs.csv"
        df_w10.to_csv(out_w10, index=False)
        print(f"  Saved: {out_w10.name}")

        # -- 4. Worst-10 pivot table CSV (configs as columns) ------------------
        if not df_w10.empty:
            pivot = df_w10.pivot_table(
                index="date",
                columns="config",
                values=["total_cost", "energy_demanded_kwh", "energy_served_kwh",
                        "demand_served_pct", "n_vehicles", "n_vehicles_served",
                        "vehicles_served_pct"],
                aggfunc="first"
            )
            pivot.columns = ["_".join(c).strip() for c in pivot.columns]
            pivot = pivot.reset_index()
            # Re-order rows by worst-first
            date_order = {d: i for i, d in enumerate(res["worst10_dates"])}
            pivot["_rank"] = pivot["date"].map(date_order)
            pivot = pivot.sort_values("_rank").drop(columns=["_rank"])
            out_pivot = OUT_DIR / f"{site}_worst10_pivot.csv"
            pivot.to_csv(out_pivot, index=False)
            print(f"  Saved: {out_pivot.name}")

        # -- 5. Worst-10 ASCII report -------------------------------------------
        w10_txt = _worst10_table(
            site, site_label,
            res["worst10_dates"], res["worst10_rows"], res["selected_config"]
        )
        out_txt = OUT_DIR / f"{site}_worst10_report.txt"
        out_txt.write_text(w10_txt, encoding="utf-8")
        print(f"  Saved: {out_txt.name}")

        # -- Site summary for cross-site table ---------------------------------
        n_days = len(res["daily_optimal"])
        if n_days > 0:
            opt_rows_site = [r for r in res["all_rows"]
                             if r["config"] == res["selected_config"]]
            total_veh  = sum(r["n_vehicles"]        for r in opt_rows_site)
            total_svc  = sum(r["n_vehicles_served"] for r in opt_rows_site)
            total_e_d  = sum(r["energy_demanded_kwh"] for r in opt_rows_site)
            total_e_s  = sum(r["energy_served_kwh"]   for r in opt_rows_site)
            avg_cost   = sum(r["total_cost"]           for r in opt_rows_site) / len(opt_rows_site)
            max_cost   = max(r["total_cost"]           for r in opt_rows_site)
            all_site_rows.append({
                "site":             site_label,
                "selected_config":  res["selected_config"],
                "n_days":           n_days,
                "annual_veh_svc%":  round(100 * total_svc / max(total_veh, 1), 1),
                "annual_energy_svc%": round(100 * total_e_s / max(total_e_d, 0.001), 1),
                "avg_daily_cost":   round(avg_cost, 2),
                "max_daily_cost":   round(max_cost, 2),
                "worst10_dates":    "; ".join(res["worst10_dates"]),
            })

        # -- Print worst-10 table to terminal ----------------------------------
        print(w10_txt)

        # Add to summary
        summary_lines += [
            f"  {site_label.upper()} -- {n_days} operating days",
            f"  Selected config:       {res['selected_config']}",
        ]
        if opt_rows_site:
            summary_lines += [
                f"  Annual vehicle svc:    {100*total_svc/max(total_veh,1):.1f}%",
                f"  Annual energy svc:     {100*total_e_s/max(total_e_d,0.001):.1f}%",
                f"  Avg daily cost:        ${avg_cost:,.2f}",
                f"  Max daily cost:        ${max_cost:,.2f}",
                f"  10 worst days:         {', '.join(res['worst10_dates'])}",
            ]
        summary_lines.append("")

    # -- Cross-site summary CSV -------------------------------------------------
    if all_site_rows:
        df_cross = pd.DataFrame(all_site_rows)
        out_cross = OUT_DIR / "all_sites_summary.csv"
        df_cross.to_csv(out_cross, index=False)
        print(f"\n  Saved: {out_cross.name}")

    # -- Master summary text ----------------------------------------------------
    summary_lines += ["=" * 80]
    summary_txt = "\n".join(summary_lines)
    out_summary = OUT_DIR / "all_sites_summary_report.txt"
    out_summary.write_text(summary_txt, encoding="utf-8")
    print(f"  Saved: {out_summary.name}")
    print("\n" + summary_txt)


# ??????????????????????????????????????????????????????????????????????????????
# WORST-10 DETAILED TABLE (printed to console in structured format)
# ??????????????????????????????????????????????????????????????????????????????

def print_worst10_structured(results: dict[str, dict]) -> None:
    """Print a structured per-site summary of worst-10 days in table form."""
    cfg_labels = [c[0] for c in CONFIGS]

    for site, site_label in SITES.items():
        res = results.get(site)
        if not res:
            continue

        worst10_dates = res["worst10_dates"]
        worst10_rows  = res["worst10_rows"]
        selected_cfg  = res["selected_config"]

        rows_by_date: dict[str, dict[str, dict]] = {}
        for r in worst10_rows:
            rows_by_date.setdefault(r["date"], {})[r["config"]] = r

        print(f"\n{'='*100}")
        print(f"  {site_label.upper()} -- TOP 10 WORST DAYS CONFIGURATION ANALYSIS")
        print(f"  Selected optimal configuration: {selected_cfg}  (* = selected)")
        print(f"{'='*100}")
        print(f"  {'Rank':<5} {'Date':<12} {'Configuration':<20} {'Total Cost':>11}"
              f" {'E Demand':>10} {'E Served':>10} {'Dmnd%':>7}"
              f" {'Vehicles':>9} {'Veh Svc':>8} {'Veh%':>7}")
        print(f"  {'-'*98}")

        for rank, date in enumerate(worst10_dates, 1):
            day_data = rows_by_date.get(date, {})
            first    = True
            for cfg_label in cfg_labels:
                r = day_data.get(cfg_label)
                if r is None:
                    continue
                star    = "*" if cfg_label == selected_cfg else " "
                rk_col  = f"#{rank}" if first else ""
                dt_col  = date if first else ""
                print(
                    f"  {rk_col:<5} {dt_col:<12} {star}{cfg_label:<19}"
                    f" ${r['total_cost']:>10,.2f}"
                    f" {r['energy_demanded_kwh']:>9.1f}"
                    f" {r['energy_served_kwh']:>9.1f}"
                    f" {r['demand_served_pct']:>6.1f}%"
                    f" {r['n_vehicles']:>9d}"
                    f" {r['n_vehicles_served']:>8d}"
                    f" {r['vehicles_served_pct']:>6.1f}%"
                )
                first = False
            print(f"  {'-'*98}")


# ??????????????????????????????????????????????????????????????????????????????
# FINAL COMPARISON
# ??????????????????????????????????????????????????????????????????????????????

def print_final_comparison(results: dict[str, dict]) -> None:
    """Print cross-site comparison focusing on worst-day performance."""
    cfg_labels = [c[0] for c in CONFIGS]

    print("\n" + "=" * 100)
    print("  FINAL COMPARISON -- WORST-DAY ROBUSTNESS BY CONFIGURATION")
    print("  (Metrics averaged across the 10 worst days per site)")
    print("=" * 100)

    for site, site_label in SITES.items():
        res = results.get(site)
        if not res:
            continue

        worst10_rows = res["worst10_rows"]
        selected_cfg = res["selected_config"]

        print(f"\n  {site_label.upper()}  (selected: {selected_cfg})")
        print(f"  {'Configuration':<22} {'Avg Cost':>10} {'Avg Dmnd%':>10}"
              f" {'Avg Veh%':>9} {'Days 100% svc':>14} {'Min Veh%':>9}")
        print(f"  {'-'*80}")

        for cfg_label in cfg_labels:
            cfg_rows = [r for r in worst10_rows if r["config"] == cfg_label]
            if not cfg_rows:
                continue
            avg_cost  = sum(r["total_cost"]          for r in cfg_rows) / len(cfg_rows)
            avg_dmnd  = sum(r["demand_served_pct"]   for r in cfg_rows) / len(cfg_rows)
            avg_veh   = sum(r["vehicles_served_pct"] for r in cfg_rows) / len(cfg_rows)
            days_full = sum(1 for r in cfg_rows if r["vehicles_served_pct"] >= 99.9)
            min_veh   = min(r["vehicles_served_pct"] for r in cfg_rows)
            star      = "*" if cfg_label == selected_cfg else " "
            print(
                f"  {star}{cfg_label:<21} ${avg_cost:>9,.2f}"
                f" {avg_dmnd:>9.1f}%"
                f" {avg_veh:>8.1f}%"
                f" {days_full:>14d}/10"
                f" {min_veh:>8.1f}%"
            )

    print("\n  LEGEND")
    print("  * = site's selected optimal configuration")
    print("  'Days 100% svc' = worst days where all vehicles were served (out of 10)")
    print("  'Min Veh%' = worst single-day vehicle service rate across the 10 worst days")
    print("=" * 100)


# ??????????????????????????????????????????????????????????????????????????????
# MAIN
# ??????????????????????????????????????????????????????????????????????????????

def main() -> None:
    print_capex_table()

    results: dict[str, dict] = {}
    for site, site_label in SITES.items():
        results[site] = analyze_site(site, site_label)

    print("\n\n" + "=" * 68)
    print("  GENERATING OUTPUT FILES")
    print("=" * 68)
    generate_reports(results)

    print_worst10_structured(results)
    print_final_comparison(results)

    print(f"\n  All outputs saved to: {OUT_DIR}")


if __name__ == "__main__":
    main()
