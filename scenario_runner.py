"""
scenario_runner.py
==================
Modular runner for Caltrans ZEV DCFC charger scenarios.

  Scenario B  : Kempower-only               (Step 2 — implemented)
  Scenario A1 : XOS Hub always grid-connected  (Step 3 — stub only)
  Scenario A2 : XOS Hub not always grid-connected  (Step 4 — stub only)

Usage:
    python scenario_runner.py          # Northgate 2025-07-17, Kempower-only
    python scenario_runner.py <date>   # Northgate <date>, Kempower-only

Cost inputs (all confirmed):
  Kempower purchase   : DGS Contract 1-23-61-15A  (provided by Farhang)
  Kempower install    : DGS Contract               (provided by Farhang)
  Kempower maint      : $1,573/yr  DGS ChargerHelp rate
  Kempower warranty   : $2,000/yr  provided by Farhang
  XOS purchase        : $245,437.50  provided by Farhang
  XOS install         : tiered infra model (see charger_costs_xos_hub.py)
  XOS maint           : $6,000/yr  confirmed by Farhang
  XOS warranty        : $10,000/yr  provided by Farhang

Daily CapEx formula (all charger types):
  C = [(purchase + install) / (life*12) + (maint + warranty) / 12] / 30.42
"""

from __future__ import annotations

import importlib
import math
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
OUT_DIR  = BASE_DIR / "scenario_outputs"
sys.path.insert(0, str(BASE_DIR))

SMUD_TZ            = "America/Los_Angeles"
ENERGY_TOL         = 0.10    # kWh — vehicle "fully served" if unmet < this
MULTIDAY_THRESHOLD = 24.0    # hours — events with dwell > this are multi-day

# ── Kempower MILP upper bounds (keep search manageable) ───────────────────────

KEMPOWER_UB = {
    "Kempower_50kW":  20,
    "Kempower_150kW": 15,
    "Kempower_250kW": 10,
}

# ── Kempower building-side electrical infra estimate (NOT in MILP objective) ──
# Per-unit cost: circuit breaker + conduit + wire + outlet + labour.
# Shared one-time: panel / switchboard upgrade, engineering, permit.

KEMPOWER_ELEC_PER_UNIT = {
    "Kempower_50kW":  {"low": 8_000,  "mid": 13_000, "high": 20_000},
    "Kempower_150kW": {"low": 10_000, "mid": 16_000, "high": 25_000},
    "Kempower_250kW": {"low": 13_000, "mid": 20_000, "high": 32_000},
}
KEMPOWER_ELEC_SHARED = {"low": 15_000, "mid": 25_000, "high": 50_000}
KEMPOWER_ELEC_TIER   = {"low": 10_000, "mid": 18_000, "high": 30_000}  # per 4-unit tier


def _kempower_elec_cost(n_units: int, mix_df: pd.DataFrame,
                        estimate: str = "mid") -> dict:
    """
    Estimate building-side electrical infra for a given Kempower mix.
    Returns a breakdown dict.
    """
    shared = KEMPOWER_ELEC_SHARED[estimate]
    tier_cost = KEMPOWER_ELEC_TIER[estimate]
    n_tiers = max(0, math.ceil(n_units / 4) - 1)

    circuit_total = 0
    for _, r in mix_df.iterrows():
        n   = int(r["count"])
        ct  = r["charger_type"]
        per = KEMPOWER_ELEC_PER_UNIT.get(ct, {"low": 10_000, "mid": 15_000, "high": 25_000})
        circuit_total += n * per[estimate]

    tier_total  = n_tiers * tier_cost
    grand_total = shared + circuit_total + tier_total
    return {
        "n_units": n_units, "estimate": estimate,
        "shared_infra": shared,
        "circuit_cost": circuit_total,
        "n_tier_upgrades": n_tiers,
        "tier_upgrades": tier_total,
        "total": grand_total,
        "per_unit_avg": grand_total / max(n_units, 1),
    }


# ── Spec table ────────────────────────────────────────────────────────────────

def generate_spec_table() -> pd.DataFrame:
    """Print and return the charger/hub specification table."""
    from charger_costs_kempower_dgs import build_charger_specs_kempower_dgs
    from charger_costs_xos_hub import XOS_HUB_SPECS

    kmp = build_charger_specs_kempower_dgs()
    rows = []
    for ctype, spec in kmp.items():
        rows.append({
            "Charger / Hub":              ctype,
            "Scenario":                   "B (Kempower-only)",
            "Power/port (kW)":            spec["power_kw"],
            "Max simultaneous ports":     1,
            "Grid input (kW)":            spec["power_kw"],
            "Internal battery":           "No",
            "Always grid-connected":      "Yes",
            "Min SOC":                    "N/A",
            "Max SOC":                    "N/A",
            "Inactive while recharging":  "No",
            "Connector":                  "CCS1",
        })
    s = XOS_HUB_SPECS
    rows.append({
        "Charger / Hub":              "XOS Hub MC02",
        "Scenario":                   "A1 / A2 (XOS-only)",
        "Power/port (kW)":            s["power_per_port_kw"],
        "Max simultaneous ports":     s["n_ports"],
        "Grid input (kW)":            s["power_grid_input_kw"],
        "Internal battery":           f"Yes ({s['capacity_kwh']} kWh nominal, 225.6 kWh usable)",
        "Always grid-connected":      "A1: Yes  |  A2: No",
        "Min SOC":                    f"{int(s['soc_min']*100)}%",
        "Max SOC":                    f"{int(s['soc_max']*100)}%",
        "Inactive while recharging":  "A1: No  |  A2: Yes",
        "Connector":                  "CCS1",
    })
    df = pd.DataFrame(rows)
    print("\n" + "=" * 100)
    print("CHARGER / HUB SPECIFICATION TABLE")
    print("=" * 100)
    print(df.to_string(index=False))
    print("=" * 100 + "\n")
    return df


# ── Cost table ────────────────────────────────────────────────────────────────

def generate_cost_table() -> pd.DataFrame:
    """Print and return the full cost input table with source labels."""
    from charger_costs_kempower_dgs import build_charger_specs_kempower_dgs
    from charger_costs_xos_hub import XOS_HUB_SPECS, electrical_infra_cost

    DAYS = 30.42
    kmp  = build_charger_specs_kempower_dgs()

    def _daily(spec, install_override=None):
        install = install_override if install_override is not None else spec["install_cost"]
        mc = (spec["purchase_cost"] + install) / (spec["life_years"] * 12)
        mr = (spec["annual_maint"] + spec.get("annual_warranty", 0)) / 12
        return (mc + mr) / DAYS

    rows = []
    for ctype, spec in kmp.items():
        elec = KEMPOWER_ELEC_PER_UNIT[ctype]
        rows.append({
            "Charger/Hub":         ctype,
            "Purchase ($)":        spec["purchase_cost"],
            "DGS install ($)":     spec["install_cost"],
            "Elec infra/unit mid ($)": elec["mid"],
            "Annual maint ($)":    spec["annual_maint"],
            "Annual warranty ($)": spec.get("annual_warranty", 0),
            "Life (yr)":           spec["life_years"],
            "Daily CapEx — MILP ($/unit/day)": round(_daily(spec), 4),
            "Purchase source":     "Farhang (DGS contract)",
            "Install source":      "DGS contract",
            "Elec source":         "Assumed — building-side estimate",
            "Maint source":        "DGS ChargerHelp! rate",
            "Warranty source":     "Provided by Farhang",
        })

    # XOS Hub (reference fleet = 6 units, mid electrical estimate)
    s        = XOS_HUB_SPECS
    n_ref    = 6
    infra    = electrical_infra_cost(n_ref, "mid")
    inst_mid = infra["per_unit_avg"]
    rows.append({
        "Charger/Hub":         "XOS Hub MC02",
        "Purchase ($)":        s["purchase_cost"],
        "DGS install ($)":     f"Tiered model — ${inst_mid:,.0f}/unit ({n_ref}-unit mid)",
        "Elec infra/unit mid ($)": inst_mid,
        "Annual maint ($)":    s["annual_maint"],
        "Annual warranty ($)": s.get("annual_warranty", 0),
        "Life (yr)":           s["life_years"],
        "Daily CapEx — MILP ($/unit/day)": round(_daily(s, inst_mid), 4),
        "Purchase source":     "Farhang (Caltrans quote)",
        "Install source":      "Tiered infra model (Jun 23 2026)",
        "Elec source":         "Tiered infra model",
        "Maint source":        "Confirmed by Farhang",
        "Warranty source":     "Provided by Farhang",
    })

    df = pd.DataFrame(rows)
    print("\n" + "=" * 110)
    print("COST INPUT TABLE")
    print("=" * 110)
    cost_cols = [
        "Charger/Hub", "Purchase ($)", "DGS install ($)", "Elec infra/unit mid ($)",
        "Annual maint ($)", "Annual warranty ($)", "Life (yr)",
        "Daily CapEx — MILP ($/unit/day)",
    ]
    print(df[cost_cols].to_string(index=False))
    print()
    src_cols = ["Charger/Hub", "Purchase source", "Install source",
                "Maint source", "Warranty source"]
    print("Source / status:")
    print(df[src_cols].to_string(index=False))
    print("=" * 110)
    print("NOTE: Daily CapEx (MILP) = [(purchase + DGS_install) / (life*12)")
    print("      + (maint + warranty) / 12] / 30.42")
    print("      Building-side electrical infra is NOT in the MILP objective.")
    print("      It is reported separately in the cost breakdown.\n")
    return df


# ── Data loading ──────────────────────────────────────────────────────────────

def load_site_day_data(csv_path: Path) -> pd.DataFrame:
    """Load and clean one events CSV. Returns cleaned DataFrame."""
    milp = importlib.import_module("exact_northgate_charger_sizing_milp")
    raw  = milp.load_events_data(csv_path)
    return milp.clean_events_df(raw)


def apply_multiday_rule(events_df: pd.DataFrame,
                        analysis_date_str: str,
                        site_csv_dir: Path = BASE_DIR,
                        site_csv_stem: str = "z2z_milp_events_northgate",
                        max_lookback_days: int = 14) -> pd.DataFrame:
    """
    Apply the multi-day dwell rule (Farhang, Jun 24 2026):

    Rule:
      - Vehicles with dwell > 24 h are charged on the day they DEPART,
        not on any day they are simply parked.
      - On departure day their charging window = midnight(departure_day) → departure_time.
      - On all other days: excluded from the analysis entirely.

    Implementation:
      1. Remove events from today's arrivals that have dwell > 24 h
         (they belong to a future day's analysis).
      2. Scan the previous max_lookback_days events CSVs for multi-day
         vehicles whose departure falls on analysis_date.
         Add those events with arrival_time clamped to midnight(analysis_date)
         so the full available day window is used for charging.
    """
    tz = SMUD_TZ
    analysis_ts  = pd.Timestamp(analysis_date_str).tz_localize(tz)
    day_start    = analysis_ts.tz_convert("UTC")
    day_end      = (analysis_ts + pd.Timedelta(days=1)).tz_convert("UTC")

    df = events_df.copy()
    df["arrival_time"]   = pd.to_datetime(df["arrival_time"],   utc=True, errors="coerce")
    df["departure_time"] = pd.to_datetime(df["departure_time"], utc=True, errors="coerce")
    df["_dwell_h"]       = (df["departure_time"] - df["arrival_time"]).dt.total_seconds() / 3600

    # 1. Split today's arrivals: same-day vs. multi-day
    same_day  = df[df["_dwell_h"] <= MULTIDAY_THRESHOLD].drop(columns=["_dwell_h"])
    multi_day = df[df["_dwell_h"] >  MULTIDAY_THRESHOLD]

    n_excl = len(multi_day)
    if n_excl:
        print(f"  [multi-day] Excluded {n_excl} arrival event(s) with dwell > {MULTIDAY_THRESHOLD:.0f}h "
              f"(assigned to departure day):")
        for _, r in multi_day.iterrows():
            dep_pac = r["departure_time"].tz_convert(tz)
            print(f"    {r['charging_event_id']}  dwell={r['_dwell_h']:.1f}h  "
                  f"departs {dep_pac.strftime('%Y-%m-%d %H:%M')} Pacific")

    # 2. Scan lookback CSVs for multi-day departures happening today
    deferred: list[pd.DataFrame] = []
    for n in range(1, max_lookback_days + 1):
        lb_ts  = analysis_ts - pd.Timedelta(days=n)
        lb_tag = lb_ts.strftime("%Y_%m_%d")
        lb_csv = site_csv_dir / f"{site_csv_stem}_{lb_tag}.csv"
        if not lb_csv.exists():
            continue

        try:
            lb = pd.read_csv(lb_csv)
            lb["arrival_time"]   = pd.to_datetime(lb["arrival_time"],   utc=True, errors="coerce")
            lb["departure_time"] = pd.to_datetime(lb["departure_time"], utc=True, errors="coerce")
            lb = lb.dropna(subset=["arrival_time", "departure_time"])
            lb["_dwell_h"] = (lb["departure_time"] - lb["arrival_time"]).dt.total_seconds() / 3600

            # Multi-day events from that prior day that depart on analysis_date
            candidates = lb[
                (lb["_dwell_h"] >  MULTIDAY_THRESHOLD) &
                (lb["departure_time"] >= day_start) &
                (lb["departure_time"] <  day_end)
            ].copy().drop(columns=["_dwell_h"])

            if len(candidates):
                # Clamp arrival to start of today so full-day window is usable
                candidates["arrival_time"] = candidates["arrival_time"].apply(
                    lambda t: max(t, day_start)
                )
                candidates["dwell_hours"] = (
                    (candidates["departure_time"] - candidates["arrival_time"])
                    .dt.total_seconds() / 3600
                )
                deferred.append(candidates)
                for _, r in candidates.iterrows():
                    dep_pac = r["departure_time"].tz_convert(tz)
                    arr_pac = r["arrival_time"].tz_convert(tz)
                    print(f"  [multi-day] Added {r['charging_event_id']} "
                          f"(from {lb_tag}, departs {dep_pac.strftime('%H:%M')} today, "
                          f"charging window {arr_pac.strftime('%H:%M')}–{dep_pac.strftime('%H:%M')} "
                          f"{r['dwell_hours']:.1f}h)")
        except Exception as e:
            print(f"  [multi-day] Warning: could not read {lb_csv.name}: {e}")

    frames = [same_day] + deferred
    result = pd.concat(frames, ignore_index=True).sort_values("arrival_time").reset_index(drop=True)

    print(f"  [multi-day] Events: {len(df)} raw → {n_excl} deferred out "
          f"+ {sum(len(d) for d in deferred)} deferred in → {len(result)} final")
    return result


# ── Vehicle results table ─────────────────────────────────────────────────────

def _build_vehicle_table(event_df: pd.DataFrame) -> pd.DataFrame:
    """Classify each vehicle as served / partially served / unserved."""
    rows = []
    for _, r in event_df.iterrows():
        req  = float(r["required_energy_kwh"])
        dlv  = float(r["delivered_energy_kwh"])
        unmt = float(r["unmet_energy_kwh"])
        if unmt < ENERGY_TOL:
            status = "Fully served"
        elif dlv > ENERGY_TOL:
            status = "Partially served"
        else:
            status = "Unserved"
        rows.append({
            "Event":     r["charging_event_id"],
            "Vehicle":   r.get("vehicle_id", ""),
            "Model":     r.get("ev_equivalent_model", ""),
            "Arrival":   pd.to_datetime(r["arrival_time"]).tz_convert(SMUD_TZ).strftime("%H:%M"),
            "Departure": pd.to_datetime(r["departure_time"]).tz_convert(SMUD_TZ).strftime("%H:%M"),
            "Required (kWh)":  round(req, 2),
            "Delivered (kWh)": round(dlv, 2),
            "Unmet (kWh)":     round(unmt, 2),
            "Status":          status,
        })
    return pd.DataFrame(rows)


def _print_vehicle_table(vt: pd.DataFrame, site_label: str, date_str: str) -> None:
    n_full    = (vt["Status"] == "Fully served").sum()
    n_partial = (vt["Status"] == "Partially served").sum()
    n_unserv  = (vt["Status"] == "Unserved").sum()
    n_total   = len(vt)
    print(f"\n--- Vehicle Results: {site_label} / {date_str} ---")
    print(f"  Fully served    : {n_full}/{n_total}")
    print(f"  Partially served: {n_partial}/{n_total}")
    print(f"  Unserved        : {n_unserv}/{n_total}")
    print()
    pd.set_option("display.max_rows", 200)
    pd.set_option("display.width", 120)
    print(vt.to_string(index=False))


# ── Charger utilization table ─────────────────────────────────────────────────

def _build_utilization_table(sched_df: pd.DataFrame, mix_df: pd.DataFrame,
                              events_df: pd.DataFrame, dt_h: float) -> pd.DataFrame:
    """Compute charger utilization by type."""
    arr = pd.to_datetime(events_df["arrival_time"], utc=True)
    dep = pd.to_datetime(events_df["departure_time"], utc=True)
    day_h = (dep.max() - arr.min()).total_seconds() / 3600

    rows = []
    for _, r in mix_df.iterrows():
        ct  = r["charger_type"]
        n   = int(r["count"])
        if n == 0:
            rows.append({
                "Charger type": ct, "Count": 0, "Day window (h)": round(day_h, 2),
                "Avail port-h": 0.0, "Used port-h": 0.0, "Utilization": "N/A",
            })
            continue
        sub   = sched_df[sched_df["charger_type"] == ct]
        used_h = len(sub) * dt_h          # each row = one time step = dt_h hours
        avail_h = n * day_h
        util   = used_h / avail_h * 100 if avail_h > 0 else 0.0
        rows.append({
            "Charger type":   ct,
            "Count":          n,
            "Day window (h)": round(day_h, 2),
            "Avail port-h":   round(avail_h, 2),
            "Used port-h":    round(used_h, 2),
            "Utilization":    f"{util:.1f}%",
        })
    return pd.DataFrame(rows)


def _print_utilization_table(ut: pd.DataFrame) -> None:
    print("\n--- Charger Utilization ---")
    print(ut.to_string(index=False))


# ── Cost breakdown printout ───────────────────────────────────────────────────

def _print_cost_breakdown(cost_df: pd.DataFrame, mix_df: pd.DataFrame,
                           kempower_specs: dict, date_str: str, site_label: str) -> None:
    def _v(name):
        row = cost_df[cost_df["component"] == name]
        return float(row["value"].iloc[0]) if len(row) else 0.0

    daily_capex_cost = _v("daily_capex_cost")
    energy_cost      = _v("energy_cost")
    global_dem       = _v("global_demand_cost")
    peak_dem         = _v("peak_window_demand_cost")
    total_obj        = _v("total_objective_cost")
    p_max            = _v("P_max_kw")
    p_peak           = _v("P_peak_window_kw")
    grid_kwh         = _v("total_grid_energy_kwh")
    veh_kwh          = _v("total_vehicle_energy_kwh")

    # Electrical infra (mid estimate, building-side only, separate from MILP)
    n_total  = int(mix_df["count"].sum())
    elec_mid = _kempower_elec_cost(n_total, mix_df, "mid")
    elec_low = _kempower_elec_cost(n_total, mix_df, "low")
    elec_hi  = _kempower_elec_cost(n_total, mix_df, "high")

    print(f"\n{'='*65}")
    print(f"  COST BREAKDOWN — {site_label} / {date_str} — Kempower-only")
    print(f"{'='*65}")
    print(f"  Charger mix (selected):")
    for _, r in mix_df.iterrows():
        n = int(r["count"])
        if n > 0:
            print(f"    {r['charger_type']:<20}  {n:>2} units  "
                  f"${r['daily_capex_per_unit']:.2f}/unit/day  "
                  f"-> ${r['total_daily_capex']:.2f}/day")
    print(f"  {'─'*55}")
    print(f"  {'Daily CapEx (charger ownership)':<38}  ${daily_capex_cost:>9.2f}")
    print(f"  {'Energy cost (SMUD TOU)':<38}  ${energy_cost:>9.2f}")
    print(f"  {'Global demand charge (proxy)':<38}  ${global_dem:>9.2f}")
    print(f"  {'Peak-window demand charge (proxy)':<38}  ${peak_dem:>9.2f}")
    print(f"  {'─'*55}")
    print(f"  {'TOTAL (MILP objective)':<38}  ${total_obj:>9.2f}")
    print()
    print(f"  Grid peak power    : {p_max:.1f} kW")
    print(f"  Peak-window power  : {p_peak:.1f} kW")
    print(f"  Total grid energy  : {grid_kwh:.1f} kWh")
    print(f"  Total veh energy   : {veh_kwh:.1f} kWh (at vehicle battery)")
    print()
    print(f"  Building-side electrical infra (separate from MILP):")
    print(f"    {n_total} Kempower units — low/mid/high estimate:")
    print(f"    ${elec_low['total']:>8,} / ${elec_mid['total']:>8,} / ${elec_hi['total']:>8,}")
    print(f"    (Shared panel: ${elec_mid['shared_infra']:,}  "
          f"+ circuits: ${elec_mid['circuit_cost']:,}  "
          f"+ tier upgrades: ${elec_mid['tier_upgrades']:,})")
    print(f"    NOTE: Excludes utility transformer and service entrance upgrades.")
    print(f"{'='*65}\n")


# ── Validation report ─────────────────────────────────────────────────────────

def generate_validation_report(
    event_df:  pd.DataFrame,
    sched_df:  pd.DataFrame,
    mix_df:    pd.DataFrame,
    specs:     dict,
    label:     str = "Kempower-only",
    output_dir: Path | None = None,
) -> dict:
    """
    Run post-solve validation checks. Prints warnings for any violation.
    Returns a dict with pass/fail counts.
    """
    failures = []
    warnings_list = []

    # 1. Energy satisfaction
    delivered: dict[str, float] = {}
    for _, r in sched_df.iterrows():
        eid = r["charging_event_id"]
        delivered[eid] = delivered.get(eid, 0.0) + float(r["energy_delivered_kwh"])
    for _, r in event_df.iterrows():
        eid  = r["charging_event_id"]
        req  = float(r["required_energy_kwh"])
        dlv  = delivered.get(eid, 0.0)
        unmt = max(0.0, req - dlv)
        if unmt > ENERGY_TOL:
            if dlv < ENERGY_TOL:
                failures.append(f"UNSERVED: {eid} need={req:.1f} kWh delivered={dlv:.1f} kWh")
            else:
                warnings_list.append(
                    f"PARTIAL: {eid} need={req:.1f} kWh delivered={dlv:.1f} kWh unmet={unmt:.1f} kWh")

    # 2. Single charger type per vehicle
    if not sched_df.empty:
        multi_type = (
            sched_df.groupby("charging_event_id")["charger_type"].nunique()
        )
        for eid, n_types in multi_type.items():
            if n_types > 1:
                failures.append(f"MULTI-TYPE: {eid} uses {n_types} charger types simultaneously")

    # 3. Charger capacity: simultaneous vehicles per type ≤ N_c
    n_units = {r["charger_type"]: int(r["count"]) for _, r in mix_df.iterrows()}
    if not sched_df.empty:
        sched_df2 = sched_df.copy()
        sched_df2["time_step_start"] = pd.to_datetime(sched_df2["time_step_start"], utc=True)
        for t_start, grp in sched_df2.groupby("time_step_start"):
            for ctype, sub in grp.groupby("charger_type"):
                n_veh = len(sub)
                cap   = n_units.get(ctype, 0)
                if n_veh > cap:
                    failures.append(
                        f"CAPACITY: {ctype} serving {n_veh} vehicles at {t_start} but only {cap} installed")

    # 4. No battery-related checks for Kempower (it has no battery)
    for ctype in specs:
        s = specs[ctype]
        if s.get("ac_dc") == "DC" and "capacity_kwh" not in s:
            pass  # Kempower: no battery, no SOC constraint needed

    # 5. Dwell window check: no charging outside vehicle dwell
    if not sched_df.empty:
        arr_map = {}
        dep_map = {}
        for _, r in event_df.iterrows():
            eid = r["charging_event_id"]
            arr_map[eid] = pd.to_datetime(r["arrival_time"], utc=True)
            dep_map[eid] = pd.to_datetime(r["departure_time"], utc=True)
        sched_df2 = sched_df.copy()
        sched_df2["time_step_start"] = pd.to_datetime(sched_df2["time_step_start"], utc=True)
        sched_df2["time_step_end"]   = pd.to_datetime(sched_df2["time_step_end"], utc=True)
        for _, r in sched_df2.iterrows():
            eid = r["charging_event_id"]
            if eid not in arr_map:
                continue
            if r["time_step_start"] < arr_map[eid] - pd.Timedelta(minutes=1):
                failures.append(f"DWELL: {eid} charging BEFORE arrival at {r['time_step_start']}")
            if r["time_step_end"] > dep_map[eid] + pd.Timedelta(minutes=1):
                failures.append(f"DWELL: {eid} charging AFTER departure at {r['time_step_end']}")

    n_events = len(event_df)
    n_served = int((event_df["served_binary"] == 1).sum()) if "served_binary" in event_df.columns else 0

    print(f"\n--- Validation Report ({label}) ---")
    if failures:
        print(f"  FAILURES ({len(failures)}):")
        for f in failures:
            print(f"    [FAIL] {f}")
    else:
        print("  No hard failures detected.")
    if warnings_list:
        print(f"  Warnings ({len(warnings_list)}):")
        for w in warnings_list:
            print(f"    [WARN] {w}")
    else:
        print("  No warnings.")
    print(f"  Events: {n_events}   Served: {n_served}   "
          f"Partially/unserved: {n_events - n_served}")
    print(f"  Kempower-specific checks:")
    print(f"    No battery / no SOC                   : PASS (no battery in model)")
    print(f"    Always grid-connected                  : PASS (MILP assumption)")
    print(f"    Charger available after session ends   : PASS (MILP constraint D)")
    cap_fail = sum(1 for f in failures if "CAPACITY" in f)
    dwell_fail = sum(1 for f in failures if "DWELL" in f)
    print(f"    Capacity constraint                    : {'FAIL' if cap_fail else 'PASS'}")
    print(f"    Dwell window constraint                : {'FAIL' if dwell_fail else 'PASS'}")
    print(f"    Single charger type per vehicle        : {'FAIL' if any('MULTI-TYPE' in f for f in failures) else 'PASS'}")

    result = {
        "n_failures": len(failures),
        "n_warnings": len(warnings_list),
        "failures":   failures,
        "warnings":   warnings_list,
        "pass":       len(failures) == 0,
    }

    if output_dir is not None:
        lines = [f"Validation Report — {label}",
                 f"Failures: {len(failures)}   Warnings: {len(warnings_list)}", ""]
        lines += [f"[FAIL] {f}" for f in failures]
        lines += [f"[WARN] {w}" for w in warnings_list]
        (output_dir / "validation_report.txt").write_text("\n".join(lines), encoding="utf-8")

    return result


# ── Session builder for Gantt ─────────────────────────────────────────────────

def _build_sessions(sched_df: pd.DataFrame) -> pd.DataFrame:
    """Collapse per-step schedule into one row per (vehicle, charger_type) session."""
    if sched_df.empty:
        return pd.DataFrame()
    sd = sched_df.copy()
    sd["time_step_start"] = pd.to_datetime(sd["time_step_start"], utc=True)
    sd["time_step_end"]   = pd.to_datetime(sd["time_step_end"],   utc=True)
    rows = []
    for (eid, ctype), grp in sd.groupby(["charging_event_id", "charger_type"]):
        rows.append({
            "charging_event_id": eid,
            "charger_type":      ctype,
            "session_start":     grp["time_step_start"].min(),
            "session_end":       grp["time_step_end"].max(),
            "energy_kwh":        grp["energy_delivered_kwh"].sum(),
        })
    return pd.DataFrame(rows)


def _assign_lanes(sessions: pd.DataFrame, mix_df: pd.DataFrame) -> pd.DataFrame:
    """
    Greedily assign sessions to specific charger instances (lanes).
    Returns sessions with added 'lane' column (0-indexed globally).
    """
    if sessions.empty:
        return sessions

    # Build lane pool: {lane_idx: (charger_type, free_at_time)}
    lanes = {}
    lane_pool: dict[str, list[int]] = {}
    idx = 0
    for _, r in mix_df.sort_values("charger_type").iterrows():
        ct = r["charger_type"]
        n  = int(r["count"])
        if n > 0:
            lane_pool[ct] = list(range(idx, idx + n))
            for i in range(idx, idx + n):
                lanes[i] = {"free_at": pd.Timestamp.min.tz_localize("UTC")}
            idx += n

    assigned = []
    for _, sess in sessions.sort_values("session_start").iterrows():
        ct   = sess["charger_type"]
        pool = lane_pool.get(ct, [])
        if not pool:
            assigned.append({**sess, "lane": 0})
            continue
        free = [(i, lanes[i]["free_at"]) for i in pool
                if lanes[i]["free_at"] <= sess["session_start"]]
        chosen = (min(free, key=lambda x: x[1])[0]
                  if free else min(pool, key=lambda i: lanes[i]["free_at"]))
        lanes[chosen]["free_at"] = sess["session_end"]
        assigned.append({**sess, "lane": chosen})
    return pd.DataFrame(assigned)


# ── Gantt plot ────────────────────────────────────────────────────────────────

def plot_charger_assignments(
    sched_df:  pd.DataFrame,
    events_df: pd.DataFrame,
    mix_df:    pd.DataFrame,
    output_dir: Path,
    date_str:  str,
    site_label: str = "Northgate",
) -> Path:
    """
    Gantt chart: X = time, Y = charger lane. Colour = vehicle.
    Light bar = vehicle dwell window. Solid bar = active charging.
    """
    sessions = _build_sessions(sched_df)
    if sessions.empty:
        print("  [plot] No sessions to plot — skipping Gantt.")
        return output_dir / "no_sessions.txt"

    assigned = _assign_lanes(sessions, mix_df)

    ev_df = events_df.copy()
    ev_df["arrival_time"]   = pd.to_datetime(ev_df["arrival_time"],   utc=True)
    ev_df["departure_time"] = pd.to_datetime(ev_df["departure_time"], utc=True)

    t0 = ev_df["arrival_time"].min()
    def _h(ts):
        return (pd.to_datetime(ts, utc=True) - t0).total_seconds() / 3600

    all_eids = ev_df["charging_event_id"].tolist()
    cmap     = plt.cm.get_cmap("tab20", max(len(all_eids), 20))
    eid_col  = {e: cmap(i) for i, e in enumerate(all_eids)}

    n_lanes = int(mix_df["count"].sum())
    fig_h   = max(5, n_lanes * 0.65 + 3)
    fig, ax = plt.subplots(figsize=(18, fig_h))

    # Draw dwell windows (light)
    for _, r in ev_df.iterrows():
        eid   = r["charging_event_id"]
        col   = eid_col[eid]
        sess  = assigned[assigned["charging_event_id"] == eid]
        if sess.empty:
            continue
        lane  = int(sess["lane"].iloc[0])
        a_h   = _h(r["arrival_time"])
        d_h   = _h(r["departure_time"])
        ax.barh(lane, max(d_h - a_h, 0.05), left=a_h, height=0.6,
                color=col, alpha=0.20, edgecolor=col, linewidth=0.5, zorder=1)

    # Draw charging sessions (solid)
    for _, s in assigned.iterrows():
        col  = eid_col.get(s["charging_event_id"], "gray")
        lane = int(s["lane"])
        a_h  = _h(s["session_start"])
        d_h  = _h(s["session_end"])
        ax.barh(lane, max(d_h - a_h, 0.05), left=a_h, height=0.6,
                color=col, alpha=0.90, edgecolor="white", linewidth=0.3, zorder=3)
        ax.text(a_h + 0.05, lane,
                f"{s['charging_event_id'][-4:]}  {s['energy_kwh']:.0f}kWh",
                va="center", ha="left", fontsize=6.5, color="white",
                fontweight="bold", clip_on=True, zorder=4)

    # Y axis: lane labels
    lane_labels = {}
    for _, r in mix_df.iterrows():
        ct = r["charger_type"]
        for li in range(int(r["count"])):
            gl = sum(int(m["count"]) for _, m in mix_df.iterrows()
                     if m["charger_type"] < ct) + li
            lane_labels[gl] = f"{ct}\n#{li+1}"

    ax.set_yticks(list(range(n_lanes)))
    ax.set_yticklabels([lane_labels.get(i, str(i)) for i in range(n_lanes)], fontsize=7)
    ax.invert_yaxis()

    # X axis: hours since first arrival (with HH:MM labels)
    t_end   = ev_df["departure_time"].max()
    span_h  = (t_end - t0).total_seconds() / 3600
    ticks_h = np.arange(0, math.ceil(span_h) + 1, 1)
    ax.set_xticks(ticks_h)
    ax.set_xticklabels(
        [(t0 + pd.Timedelta(hours=h)).tz_convert(SMUD_TZ).strftime("%H:%M")
         for h in ticks_h],
        rotation=45, fontsize=7,
    )
    ax.set_xlabel("Time (Pacific)", fontsize=9)
    ax.set_xlim(0, span_h + 0.1)

    ax.grid(axis="x", linestyle=":", alpha=0.35, color="gray")
    ax.set_title(
        f"{site_label}  |  {date_str}  |  Scenario B: Kempower-only\n"
        f"Charger assignment (light = dwell window, solid = charging)  "
        f"Total: {n_lanes} charger(s)",
        fontsize=11, fontweight="bold",
    )

    # Legend: charger type colour bands
    ctype_patches = []
    start = 0
    for _, r in mix_df.iterrows():
        n = int(r["count"])
        if n > 0:
            for li in range(n):
                g = start + li
            start += n

    out = output_dir / f"scenario_B_charger_assignment_{date_str.replace('-','_')}.png"
    plt.tight_layout()
    plt.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out}")
    return out


# ── Power profile plot ────────────────────────────────────────────────────────

def plot_grid_power(
    power_df:  pd.DataFrame,
    output_dir: Path,
    date_str:  str,
    site_label: str = "Northgate",
) -> Path:
    """
    Site power profile over time. Highlights SMUD peak window in orange.
    """
    if power_df.empty:
        print("  [plot] Empty power profile — skipping.")
        return output_dir / "no_power.txt"

    pf = power_df.copy()
    pf["time_step_start"] = pd.to_datetime(pf["time_step_start"], utc=True)
    t0   = pf["time_step_start"].min()
    x_h  = [(t - t0).total_seconds() / 3600 for t in pf["time_step_start"]]
    p_kw = pf["P_total_kw"].tolist()

    fig, ax = plt.subplots(figsize=(16, 5))

    # Peak window shading
    for i, (x, row) in enumerate(zip(x_h, pf.itertuples())):
        if getattr(row, "is_smud_peak_window", False):
            next_x = x_h[i + 1] if i + 1 < len(x_h) else x + (x_h[1] - x_h[0])
            ax.axvspan(x, next_x, color="orange", alpha=0.12, linewidth=0)

    ax.fill_between(x_h, p_kw, step="post", alpha=0.22, color="steelblue")
    ax.step(x_h, p_kw, where="post", color="steelblue", linewidth=1.8, label="Grid power (kW)")
    ax.axhline(max(p_kw) if p_kw else 0, color="red", linewidth=0.9,
               linestyle="--", alpha=0.65, label=f"Peak {max(p_kw):.0f} kW")

    # X ticks
    t_end  = pf["time_step_start"].max()
    span_h = (t_end - t0).total_seconds() / 3600
    ticks_h = np.arange(0, math.ceil(span_h) + 1, 1)
    ax.set_xticks(ticks_h)
    ax.set_xticklabels(
        [(t0 + pd.Timedelta(hours=h)).tz_convert(SMUD_TZ).strftime("%H:%M")
         for h in ticks_h],
        rotation=45, fontsize=8,
    )
    ax.set_xlabel("Time (Pacific)", fontsize=9)
    ax.set_ylabel("Power (kW)", fontsize=9)
    ax.grid(axis="x", linestyle=":", alpha=0.35)
    ax.grid(axis="y", linestyle=":", alpha=0.30)

    orange_patch = mpatches.Patch(color="orange", alpha=0.4, label="SMUD peak window (4–9 PM)")
    ax.legend(handles=[ax.lines[0], ax.lines[1], orange_patch], fontsize=8)

    total_kwh = pf["energy_from_grid_kwh"].sum()
    ax.set_title(
        f"{site_label}  |  {date_str}  |  Scenario B: Kempower-only\n"
        f"Site grid power profile   Total grid energy: {total_kwh:.1f} kWh",
        fontsize=11, fontweight="bold",
    )
    ax.set_xlim(0, span_h + 0.1)

    out = output_dir / f"scenario_B_power_profile_{date_str.replace('-','_')}.png"
    plt.tight_layout()
    plt.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out}")
    return out


# ── Kempower-only scenario runner ─────────────────────────────────────────────

def run_kempower_only(
    csv_path:   Path,
    output_dir: Path,
    date_str:   str,
    site_label: str = "Northgate",
) -> dict:
    """
    Run Scenario B: Kempower-only MILP for one site-day.

    Steps:
      1. Patch MILP module globals (output dir, charger bounds, input path)
      2. Call milp.main(charger_specs_override=kempower_specs)
         -> writes 5 CSVs to output_dir
      3. Read back output CSVs
      4. Generate and print all tables
      5. Validate
      6. Generate plots
      7. Return result dict

    Returns dict with keys:
      events_df, mix_df, event_df, schedule_df, power_df, cost_df,
      vehicle_df, util_df, validation
    """
    from charger_costs_kempower_dgs import build_charger_specs_kempower_dgs

    output_dir.mkdir(parents=True, exist_ok=True)
    milp = importlib.import_module("exact_northgate_charger_sizing_milp")

    kempower_specs = build_charger_specs_kempower_dgs()

    # Patch MILP module-level globals before calling main()
    milp.INPUT_PATH_PRIMARY   = csv_path
    milp.INPUT_PATH_FALLBACK  = csv_path
    milp.OUTPUT_DIR           = output_dir
    milp.CHARGER_UPPER_BOUNDS = KEMPOWER_UB

    print(f"\n{'='*70}")
    print(f"  SCENARIO B: Kempower-only")
    print(f"  Site     : {site_label}")
    print(f"  Day      : {date_str}")
    print(f"  Input    : {csv_path.name}")
    print(f"  Output   : {output_dir}")
    print(f"{'='*70}")

    # Load and apply multi-day rule before passing to MILP
    events_df = load_site_day_data(csv_path)
    # Infer site CSV stem from csv_path name (strip date tag)
    stem_parts = csv_path.stem.rsplit("_", 3)          # z2z_..._YYYY_MM_DD
    site_csv_stem = "_".join(stem_parts[:-3]) if len(stem_parts) > 3 else csv_path.stem
    events_df = apply_multiday_rule(events_df, date_str,
                                    site_csv_dir=csv_path.parent,
                                    site_csv_stem=site_csv_stem)

    if events_df.empty:
        print("[WARNING] No events remain after multi-day rule — skipping MILP.")
        return {}

    # Run MILP (Gurobi or HiGHS fallback), passing pre-filtered events
    milp.main(charger_specs_override=kempower_specs,
              events_df_override=events_df)

    # Read back output CSVs
    mix_df   = pd.read_csv(output_dir / "exact_milp_selected_charger_mix.csv")
    event_df = pd.read_csv(output_dir / "exact_milp_event_results.csv")
    sched_df = pd.read_csv(output_dir / "exact_milp_charging_schedule.csv")
    power_df = pd.read_csv(output_dir / "exact_milp_site_power_profile.csv")
    cost_df  = pd.read_csv(output_dir / "exact_milp_cost_breakdown.csv")

    # Determine dt from schedule (infer from first row)
    dt_h = 5 / 60  # default 5-min steps (MILP default)
    if not sched_df.empty:
        r0  = sched_df.iloc[0]
        ts  = pd.to_datetime(r0["time_step_start"], utc=True)
        te  = pd.to_datetime(r0["time_step_end"],   utc=True)
        dt_h = (te - ts).total_seconds() / 3600

    # Selected charger mix
    print(f"\n--- Selected Kempower Mix ---")
    for _, r in mix_df.iterrows():
        n = int(r["count"])
        if n > 0:
            print(f"  {r['charger_type']:<20}  x{n}  "
                  f"${r['daily_capex_per_unit']:.2f}/unit/day  "
                  f"-> ${r['total_daily_capex']:.2f}/day total")
    print(f"  Total chargers: {int(mix_df['count'].sum())}")

    # Vehicle results
    vehicle_df = _build_vehicle_table(event_df)
    _print_vehicle_table(vehicle_df, site_label, date_str)
    vehicle_df.to_csv(output_dir / "scenario_B_vehicle_results.csv", index=False)

    # Charger utilization
    util_df = _build_utilization_table(sched_df, mix_df, events_df, dt_h)
    _print_utilization_table(util_df)
    util_df.to_csv(output_dir / "scenario_B_charger_utilization.csv", index=False)

    # Cost breakdown
    _print_cost_breakdown(cost_df, mix_df, kempower_specs, date_str, site_label)

    # Validation
    val = generate_validation_report(
        event_df, sched_df, mix_df, kempower_specs,
        label="Scenario B Kempower-only",
        output_dir=output_dir,
    )

    # Plots
    plot_charger_assignments(sched_df, events_df, mix_df, output_dir, date_str, site_label)
    plot_grid_power(power_df, output_dir, date_str, site_label)

    return {
        "events_df":   events_df,
        "mix_df":      mix_df,
        "event_df":    event_df,
        "schedule_df": sched_df,
        "power_df":    power_df,
        "cost_df":     cost_df,
        "vehicle_df":  vehicle_df,
        "util_df":     util_df,
        "validation":  val,
    }


# ── XOS A1 physics constants ───────────────────────────────────────────────────
# Sourced from xos_hub_soc_simulation.py (TAI spec, 280 kWh battery)
_XOS = dict(
    B_KWH     = 280.0,
    SOC_MIN   = 0.20,
    SOC_MAX   = 1.00,
    P_GRID    = 83.0,    # kW  grid-to-battery
    P_PORT    = 80.0,    # kW  per CCS1 port (battery-to-vehicle)
    ETA_C     = 0.95,    # grid→battery efficiency
    ETA_D     = 0.95,    # battery→vehicle efficiency
    DT_H      = 0.25,    # time step (15 min)
    N_PORTS   = 4,       # CCS1 ports per unit
    MAX_UNITS = 20,
)
_XOS["USABLE"]        = (_XOS["SOC_MAX"] - _XOS["SOC_MIN"]) * _XOS["B_KWH"]
_XOS["RECH_STEPS"]    = int(np.ceil(_XOS["USABLE"] / (_XOS["P_GRID"] * _XOS["ETA_C"] * _XOS["DT_H"])))

# SMUD demand charge rates (same as Kempower scenario, planning proxy)
_SMUD_DEMAND_GLOBAL   = 6.454   # $/kW  (monthly, used as proxy)
_SMUD_DEMAND_PEAK_WIN = 9.960   # $/kW  (monthly, used as proxy)


def _smud_rate(t_utc: pd.Timestamp) -> float:
    """SMUD C&I 21-299 kW TOD energy rate ($/kWh)."""
    t  = t_utc.tz_convert(SMUD_TZ)
    h  = t.hour + t.minute / 60.0
    su = t.month in (6, 7, 8, 9)
    wk = t.weekday() < 5
    pk = wk and 16 <= h < 21
    sv = (not su) and 9 <= h < 16
    if su:
        return 0.2341 if pk else 0.1215
    return 0.1477 if pk else (0.0888 if sv else 0.1264)


def _xos_extended_dwell(events_df: pd.DataFrame) -> pd.DataFrame:
    """Extend departure times so every vehicle has at least enough window for full charging."""
    df = events_df.copy()
    for idx, row in df.iterrows():
        e_need  = float(row["energy_needed_kwh_for_visit"])
        arr     = row["arrival_time"]
        dep     = row["departure_time"]
        dwell_h = (dep - arr).total_seconds() / 3600.0
        mdc     = float(row.get("max_dc_charge_kw", 0) or 0)
        p_eff   = min(_XOS["P_PORT"], mdc) if mdc > 0 else _XOS["P_PORT"]
        req_h   = e_need / (p_eff * _XOS["ETA_D"])
        extra_h = max(0.0, req_h - dwell_h)
        if extra_h > 1e-6:
            df.at[idx, "departure_time"] = dep + pd.Timedelta(hours=extra_h)
    return df


def _simulate_xos(events_df: pd.DataFrame, K: int,
                  mode: str = "a2",
                  debug_path: "Path | None" = None) -> dict:
    """
    Unified XOS simulation for both A1 and A2 scenarios.

    mode="a1": vehicles STAY on their ports when hub hits SOC_MIN.
               Hub recharges while vehicles wait. Vehicles only released
               when fully charged OR departure time expires.
    mode="a2": vehicles DISCONNECTED when hub hits SOC_MIN.
               Released vehicles re-enter the waiting pool immediately and
               are assigned to the next available hub with a free port.

    Scheduler (multi-port, fill-first):
      Each hub has NP=4 CCS1 ports; vehicles charge in parallel on the same hub.
      For each candidate hub the scheduler computes:
        available = (soc-SOC_MIN)*B*eta_d  -  sum(rem[v] for v already on ports)
      Priority (ascending = wins):
        1. is_idle=0 (hub already serving ≥1 vehicle) before is_idle=1 (idle hub)
           → fill existing hub ports before activating new hubs
        2. hub_last_assigned step (FIFO among hubs at same is_idle level)
      Tier 1: available >= rem_v  → hub can fully charge the waiting vehicle
      Tier 2: available < rem_v   → best-effort (largest available), last resort

    debug_path: if given, write a per-step diagnostic log to that file.
    """
    x = _XOS
    B = x["B_KWH"]; SMIN = x["SOC_MIN"]; SMAX = x["SOC_MAX"]
    PG = x["P_GRID"]; PP = x["P_PORT"]
    EC = x["ETA_C"];  ED = x["ETA_D"]
    DT = x["DT_H"];   NP = x["N_PORTS"]
    SOC_PROACTIVE = 0.95  # proactive recharge below this SOC when hub is empty
    MIN_SERVE_KWH = 5.0   # min kWh deliverable to count a waiting client as serviceable

    ev_ids  = events_df["charging_event_id"].tolist()
    remaining: dict[str, float] = {}
    delivered: dict[str, float] = {}
    ev_info:   dict[str, dict]  = {}
    for _, row in events_df.iterrows():
        v = row["charging_event_id"]
        remaining[v] = float(row["energy_needed_kwh_for_visit"])
        delivered[v] = 0.0
        ev_info[v]   = {"arr": row["arrival_time"], "dep": row["departure_time"]}

    soc         = [SMAX] * K
    unit_state  = ["serving"] * K
    port_assign: list[list[str | None]] = [[None] * NP for _ in range(K)]

    # FIFO fairness: track step when each hub last received a vehicle assignment.
    # Hub idle longest (smallest value) gets priority. Initialized to list(range(K))
    # so Hub 0 gets first pick, Hub 1 second, etc. on the opening round.
    # This replaces the old -soc[i] tiebreaker that starved partially-used hubs.
    hub_last_assigned: list[int] = list(range(K))

    t_start    = events_df["arrival_time"].min().floor("15min")
    t_end      = events_df["departure_time"].max().ceil("15min") + pd.Timedelta(hours=4)
    time_steps = pd.date_range(t_start, t_end, freq="15min", tz="UTC")
    n_steps    = len(time_steps)

    soc_history:  list[dict] = []
    dispatch_log: list[dict] = []
    grid_draw:    list[float] = []

    debug_lines: list[str] = []
    do_debug = debug_path is not None

    for ti, t in enumerate(time_steps):
        t_next = t + pd.Timedelta(hours=DT)
        t_pac  = t.tz_convert(SMUD_TZ).strftime("%H:%M") if do_debug else ""

        # A. Recharging units: receive grid power, return to serving when full
        for k in range(K):
            if unit_state[k] == "recharging":
                room   = (SMAX - soc[k]) * B
                add_b  = min(PG * EC * DT, room)
                soc[k] = min(soc[k] + add_b / B, SMAX)
                if soc[k] >= SMAX - 1e-4:
                    soc[k]        = SMAX
                    unit_state[k] = "serving"
                    if do_debug:
                        debug_lines.append(
                            f"[{t_pac}] Hub{k+1:2d} RECHARGE COMPLETE -> serving (SOC=100%)")

        # B. Release ports: vehicle fully charged OR dwell expired (any state)
        for k in range(K):
            for p in range(NP):
                v = port_assign[k][p]
                if v and (remaining[v] <= ENERGY_TOL or ev_info[v]["dep"] <= t):
                    port_assign[k][p] = None
                    if do_debug and remaining[v] <= ENERGY_TOL:
                        debug_lines.append(
                            f"[{t_pac}] Hub{k+1:2d} port{p} DONE: {v} "
                            f"(SOC={soc[k]*100:.1f}%)")

        # C. When serving unit hits SOC_MIN -> recharge
        #    A1: vehicles STAY on ports (wait for hub to recharge)
        #    A2: vehicles DISCONNECTED and re-enter the waiting pool
        for k in range(K):
            if unit_state[k] == "serving" and soc[k] <= SMIN + 1e-6:
                n_on_port = sum(1 for p in range(NP) if port_assign[k][p] is not None)
                if mode == "a2":
                    for p in range(NP):
                        port_assign[k][p] = None
                unit_state[k] = "recharging"
                if do_debug:
                    action = "RELEASED" if mode == "a2" else "WAITING on ports"
                    debug_lines.append(
                        f"[{t_pac}] Hub{k+1:2d} SOC_MIN -> RECHARGING "
                        f"({n_on_port} veh {action})")

        # D. Assign waiting vehicles to SERVING units with open ports
        already = {v for k in range(K) for v in port_assign[k] if v is not None}
        waiting: list[tuple[float, str]] = []
        for v in ev_ids:
            if (v not in already
                    and remaining[v] > ENERGY_TOL
                    and ev_info[v]["arr"] < t_next
                    and ev_info[v]["dep"] > t):
                tl_h = max((ev_info[v]["dep"] - t).total_seconds() / 3600, DT)
                waiting.append((remaining[v] / tl_h, v))
        waiting.sort(reverse=True)

        for _, v in waiting:
            rem_v  = remaining[v]
            placed = False
            # Multi-port capacity-aware scheduler:
            #   Key principle: FILL EXISTING HUB PORTS FIRST before activating new hubs.
            #   Each hub has NP=4 CCS1 ports; multiple vehicles charge simultaneously.
            #
            #   For each hub, compute energy AVAILABLE to a new vehicle:
            #     available = usable_total - committed_to_current_port_vehicles
            #   Use `available` (not raw usable) for the feasibility check.
            #
            #   Sort priority (ascending = wins):
            #     is_idle  — 0 if hub already serving ≥1 vehicle (fill first), 1 if idle
            #     hub_last_assigned — FIFO among hubs at the same is_idle level
            #
            #   Tier 1 (can_fully): available >= rem_v  → hub can fully charge this vehicle
            #   Tier 2 (best_effort): available < rem_v → partial charge (last resort)
            can_fully:   list[tuple] = []  # (is_idle, hub_last_assigned, k)
            best_effort: list[tuple] = []  # (is_idle, -available, hub_last_assigned, k)
            for ki in range(K):
                if unit_state[ki] != "serving" or soc[ki] <= SMIN + 1e-6:
                    continue
                if not any(port_assign[ki][p] is None for p in range(NP)):
                    continue
                usable    = (soc[ki] - SMIN) * B * ED
                n_act     = sum(1 for p in range(NP) if port_assign[ki][p] is not None)
                committed = sum(remaining.get(port_assign[ki][p], 0.0)
                                for p in range(NP)
                                if port_assign[ki][p] is not None)
                available = max(0.0, usable - committed)
                if available >= rem_v:
                    # Fill existing hub ports before activating a new idle hub
                    is_idle = 1 if n_act == 0 else 0  # 0=serving wins
                    can_fully.append((is_idle, hub_last_assigned[ki], ki))
                else:
                    # Best-effort: prefer idle hubs so partial-service vehicles
                    # don't accelerate battery drain on already-loaded hubs and
                    # cause early A2 disconnect for the vehicles already on those ports.
                    is_idle = 0 if n_act == 0 else 1  # 0=idle wins
                    best_effort.append((is_idle, -available, hub_last_assigned[ki], ki))

            chosen = None
            if can_fully:
                can_fully.sort()
                chosen = can_fully[0][2]
            elif best_effort:
                best_effort.sort()
                chosen = best_effort[0][3]

            if chosen is not None:
                k = chosen
                for p in range(NP):
                    if port_assign[k][p] is None:
                        port_assign[k][p] = v
                        hub_last_assigned[k] = ti
                        placed = True
                        if do_debug:
                            usable_d    = (soc[k] - SMIN) * B * ED
                            committed_d = sum(remaining.get(port_assign[k][pp], 0.0)
                                              for pp in range(NP)
                                              if port_assign[k][pp] is not None
                                              and port_assign[k][pp] != v)
                            available_d = max(0.0, usable_d - committed_d)
                            n_act_d     = sum(1 for pp in range(NP) if port_assign[k][pp])
                            tier        = "FULL" if available_d >= rem_v else "BEST-EFFORT"
                            debug_lines.append(
                                f"[{t_pac}]   ASSIGN {v} -> Hub{k+1:2d} port{p} "
                                f"[{tier}] (SOC={soc[k]*100:.1f}%, avail={available_d:.0f}"
                                f"kWh committed={committed_d:.0f}kWh need={rem_v:.0f}kWh"
                                f", {n_act_d}/{NP} ports)")
                        break

            if not placed and do_debug:
                reasons: list[str] = []
                for ki in range(K):
                    n_act_r   = sum(1 for p in range(NP) if port_assign[ki][p] is not None)
                    committed_r = sum(remaining.get(port_assign[ki][p], 0.0)
                                      for p in range(NP) if port_assign[ki][p] is not None)
                    usable_r  = (soc[ki] - SMIN) * B * ED
                    avail_r   = max(0.0, usable_r - committed_r)
                    if unit_state[ki] == "recharging":
                        reasons.append(f"Hub{ki+1}:recharging(SOC={soc[ki]*100:.0f}%)")
                    elif soc[ki] <= SMIN + 1e-6:
                        reasons.append(f"Hub{ki+1}:SOC<=20%")
                    elif n_act_r >= NP:
                        reasons.append(f"Hub{ki+1}:full({n_act_r}/{NP})")
                    elif avail_r < rem_v:
                        reasons.append(
                            f"Hub{ki+1}:insuff({avail_r:.0f}kWh avail,"
                            f"{committed_r:.0f}kWh committed,need={rem_v:.0f}kWh)")
                debug_lines.append(
                    f"[{t_pac}]   UNASSIGNED {v} (rem={rem_v:.0f}kWh): "
                    f"{'; '.join(reasons[:8])}")

        # Hourly hub-state summary line in debug log
        if do_debug and ti % 4 == 0:
            hub_status = "  ".join(
                f"H{k+1}:{unit_state[k][:3].upper()}"
                f"({soc[k]*100:.0f}%,"
                f"{sum(1 for p in range(NP) if port_assign[k][p])}/4)"
                for k in range(K)
            )
            debug_lines.append(f"\n[{t_pac}] STATUS: {hub_status}")

        # D.5 Proactive recharge: if a hub is empty AND below SOC_PROACTIVE AND
        # no currently-waiting client can receive meaningful energy from it given
        # its remaining SOC and the client's remaining dwell window, start
        # recharging now instead of sitting idle at partial SOC.
        already_pr = {v for k in range(K) for v in port_assign[k] if v is not None}
        waiting_pr = [
            v for v in ev_ids
            if (v not in already_pr
                and remaining[v] > ENERGY_TOL
                and ev_info[v]["arr"] < t_next
                and ev_info[v]["dep"] > t)
        ]
        for ki in range(K):
            if unit_state[ki] != "serving":
                continue
            # Hub must have all ports empty
            if any(port_assign[ki][p] is not None for p in range(NP)):
                continue
            if soc[ki] >= SOC_PROACTIVE:
                continue
            # Check if any unserved client can get meaningful energy from this hub
            # before its dwell window closes, given current hub SOC.
            can_serve_client = False
            for wv in waiting_pr:
                dwell_rem_h = max(0.0, (ev_info[wv]["dep"] - t_next).total_seconds() / 3600)
                if dwell_rem_h <= 0:
                    continue
                hub_avail   = max(0.0, (soc[ki] - SMIN) * B * ED)
                deliverable = min(PP * ED * dwell_rem_h, hub_avail)
                if deliverable >= MIN_SERVE_KWH:
                    can_serve_client = True
                    break
            if not can_serve_client:
                unit_state[ki] = "recharging"
                if do_debug:
                    debug_lines.append(
                        f"[{t_pac}] Hub{ki+1:2d} PROACTIVE RECHARGE "
                        f"(SOC={soc[ki]*100:.1f}%, no pending client to serve)")

        # E. Serve vehicles on SERVING units; tally grid draw for RECHARGING units
        gp_t = sum(PG for k in range(K) if unit_state[k] == "recharging")
        for k in range(K):
            if unit_state[k] != "serving":
                continue
            for p in range(NP):
                v = port_assign[k][p]
                if v is None:
                    continue
                usable = (soc[k] - SMIN) * B * ED
                if usable < ENERGY_TOL:
                    port_assign[k][p] = None
                    continue
                eff_h = (min(t_next, ev_info[v]["dep"])
                         - max(t, ev_info[v]["arr"])).total_seconds() / 3600.0
                e_del = min(PP * eff_h * ED, remaining[v], usable)
                if e_del < ENERGY_TOL:
                    continue
                soc_b   = soc[k]
                soc[k]  = max(soc[k] - e_del / (ED * B), SMIN)
                delivered[v] += e_del
                remaining[v]  = max(remaining[v] - e_del, 0.0)
                dispatch_log.append({
                    "step_idx": ti, "time_utc": t.isoformat(),
                    "unit": k, "port": p, "event_id": v,
                    "soc_before": round(soc_b, 4), "soc_after": round(soc[k], 4),
                    "energy_to_vehicle_kwh": round(e_del, 4),
                })

        grid_draw.append(gp_t)
        row_s: dict = {"step_idx": ti, "time_utc": t.isoformat()}
        for k in range(K):
            # "idle" = in serving state but no vehicles on any port
            n_active = sum(1 for p in range(NP) if port_assign[k][p] is not None)
            eff_state = ("idle" if unit_state[k] == "serving" and n_active == 0
                         else unit_state[k])
            row_s[f"soc_unit_{k}"]   = round(soc[k], 4)
            row_s[f"state_unit_{k}"] = eff_state
        soc_history.append(row_s)

    if do_debug and debug_lines:
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text("\n".join(debug_lines), encoding="utf-8")
        print(f"  [debug] Per-step log: {debug_path}")

    n_served = sum(1 for v in ev_ids if remaining[v] <= ENERGY_TOL)
    return {
        "n_units":      K,
        "n_vehicles":   len(ev_ids),
        "n_served":     n_served,
        "events":       events_df,
        "delivered":    delivered,
        "remaining":    remaining,
        "soc_history":  soc_history,
        "dispatch_log": dispatch_log,
        "time_grid":    pd.DatetimeIndex([pd.Timestamp(r["time_utc"]) for r in soc_history]),
        "n_steps":      n_steps,
        "grid_draw":    grid_draw,
    }


def _xos_a1_cost(sim: dict, date_str: str) -> dict:
    """Compute daily costs for an XOS A1 simulation result."""
    from charger_costs_xos_hub import daily_capex as xos_daily_capex, electrical_infra_cost

    K         = sim["n_units"]
    time_grid = sim["time_grid"]
    grid_draw = np.array(sim["grid_draw"])   # kW per step

    # Daily CapEx (amortized purchase + infra install + maint + warranty)
    infra          = electrical_infra_cost(K, "mid")["per_unit_avg"]
    capex_per_unit = xos_daily_capex(install_cost_override=infra)
    total_capex    = K * capex_per_unit

    # Energy cost: grid_draw[ti] × DT_H → kWh → × rate
    total_energy_cost = sum(
        grid_draw[ti] * _XOS["DT_H"] * _smud_rate(time_grid[ti])
        for ti in range(len(time_grid))
    )
    total_grid_kwh = float(np.sum(grid_draw) * _XOS["DT_H"])

    # Peak grid draw: max of simultaneous recharging units × P_GRID
    p_max       = float(np.max(grid_draw)) if len(grid_draw) else 0.0
    demand_glob = p_max * _SMUD_DEMAND_GLOBAL

    # Peak-window grid draw (16–21 Pacific weekdays)
    p_peak_win = 0.0
    for ti, t in enumerate(time_grid):
        tl = t.tz_convert(SMUD_TZ)
        h  = tl.hour + tl.minute / 60.0
        if tl.weekday() < 5 and 16 <= h < 21:
            p_peak_win = max(p_peak_win, grid_draw[ti])
    demand_peak = p_peak_win * _SMUD_DEMAND_PEAK_WIN

    total_vehicle_kwh = sum(sim["delivered"].values())

    return {
        "K":                K,
        "capex_per_unit":   capex_per_unit,
        "total_capex":      total_capex,
        "energy_cost":      total_energy_cost,
        "demand_global":    demand_glob,
        "demand_peak_win":  demand_peak,
        "p_max_kw":         p_max,
        "p_peak_win_kw":    p_peak_win,
        "total_grid_kwh":   total_grid_kwh,
        "vehicle_kwh":      total_vehicle_kwh,
        "infra_per_unit":   infra,
    }


def _print_xos_a1_results(sim: dict, costs: dict, events_df: pd.DataFrame,
                           date_str: str, site_label: str) -> None:
    """Print vehicle results and cost breakdown for Scenario A1."""
    K = sim["n_units"]
    print(f"\n{'='*70}")
    print(f"  SCENARIO A1 RESULTS — {site_label} / {date_str} — XOS Hub (always grid-connected)")
    print(f"{'='*70}")
    print(f"  Hub units deployed : {K}")
    print(f"  Vehicles           : {sim['n_vehicles']}")
    print(f"  Fully served       : {sim['n_served']}/{sim['n_vehicles']}")

    print()
    print(f"  {'Event ID':<25} {'Model':<26} {'Arr':>5} {'Dep':>5} "
          f"{'Need':>8} {'Del':>8} {'Unmet':>7}  Status")
    print("  " + "-" * 100)
    tz = SMUD_TZ
    for _, row in events_df.sort_values("arrival_time").iterrows():
        v      = row["charging_event_id"]
        arr    = row["arrival_time"].tz_convert(tz).strftime("%H:%M")
        dep    = row["departure_time"].tz_convert(tz).strftime("%H:%M")
        need   = float(row["energy_needed_kwh_for_visit"])
        deliv  = sim["delivered"].get(v, 0.0)
        unmet  = max(need - deliv, 0.0)
        status = "Fully served" if unmet <= ENERGY_TOL else "Partially served"
        model  = str(row.get("ev_equivalent_model", "") or "")[:26]
        print(f"  {v:<25} {model:<26} {arr:>5}  {dep:>5} "
              f"{need:>8.2f}  {deliv:>8.2f}  {unmet:>7.2f}   {status}")

    print(f"\n  {'COST BREAKDOWN':}")
    print(f"  {'Daily CapEx (all '+str(K)+' units)':<35}: ${costs['total_capex']:>10.2f}")
    print(f"    per-unit daily CapEx               : ${costs['capex_per_unit']:>10.2f}")
    print(f"    infra install (mid, amortised/unit) : ${costs['infra_per_unit']:>10,.2f}")
    print(f"  {'Grid energy cost':<35}: ${costs['energy_cost']:>10.2f}  ({costs['total_grid_kwh']:.1f} kWh)")
    print(f"  {'Global demand charge (proxy)':<35}: ${costs['demand_global']:>10.2f}  (peak {costs['p_max_kw']:.0f} kW)")
    print(f"  {'Peak-window demand charge (proxy)':<35}: ${costs['demand_peak_win']:>10.2f}  (peak {costs['p_peak_win_kw']:.0f} kW)")
    print(f"  {'Vehicle energy delivered':<35}: {costs['vehicle_kwh']:.1f} kWh")
    print(f"  {'Grid draw: max simultaneous':<35}: {costs['p_max_kw']:.0f} kW  ({K} units × 83 kW)")


def _plot_xos_a1_soc(sim: dict, output_dir: Path, date_str: str, site_label: str) -> Path:
    """Save per-unit SOC + state timeline figure for Scenario A1."""
    K         = sim["n_units"]
    soc_hist  = sim["soc_history"]
    time_grid = sim["time_grid"]
    times_loc = pd.DatetimeIndex(time_grid).tz_convert(SMUD_TZ)
    n_steps   = sim["n_steps"]
    x         = np.arange(n_steps)

    hticks = [i for i in range(n_steps) if times_loc[i].minute == 0]
    hlbls  = [times_loc[i].strftime("%H:%M") for i in hticks]

    fig, axes = plt.subplots(K, 1, figsize=(16, 2.8 * K + 1.5), sharex=True)
    if K == 1:
        axes = [axes]

    for k, ax in enumerate(axes):
        soc_arr = np.array([r[f"soc_unit_{k}"] for r in soc_hist]) * 100
        states  = [r[f"state_unit_{k}"] for r in soc_hist]

        # Shade serving/recharging periods
        for state, col in [("serving", "#aed6f1"), ("recharging", "#a9dfbf")]:
            in_b = False
            for ti in range(n_steps + 1):
                s = states[ti] if ti < n_steps else None
                if not in_b and s == state:
                    in_b = True; b0 = ti
                elif in_b and (s != state or ti == n_steps):
                    ax.axvspan(b0, ti, color=col, alpha=0.40, linewidth=0)
                    in_b = False

        ax.plot(x, soc_arr, color="navy", linewidth=1.6)
        ax.axhline(20, color="red", linewidth=1.0, linestyle="--", alpha=0.7)
        ax.set_ylim(0, 110)
        ax.set_yticks([20, 60, 100])
        ax.set_yticklabels(["20%", "60%", "100%"])
        ax.set_ylabel(f"Unit {k+1}\nSOC (%)", fontsize=9)
        ax.grid(axis="x", linestyle=":", alpha=0.3)
        ax.grid(axis="y", linestyle=":", alpha=0.2)
        if k == 0:
            srv_p = mpatches.Patch(color="#aed6f1", alpha=0.6, label="Serving vehicles")
            rch_p = mpatches.Patch(color="#a9dfbf", alpha=0.6, label="Recharging (vehicles wait)")
            ax.legend(handles=[srv_p, rch_p], loc="upper right", fontsize=8)

    axes[-1].set_xticks(hticks)
    axes[-1].set_xticklabels(hlbls, rotation=45, fontsize=8)
    axes[-1].set_xlabel("Time (Pacific)", fontsize=9)
    axes[-1].set_xlim(0, n_steps)

    fig.suptitle(
        f"XOS Hub A1 — SOC per unit | {site_label} {date_str}\n"
        f"Served {sim['n_served']}/{sim['n_vehicles']} vehicles | {K} units | "
        f"Blue=serving, Green=recharging (vehicles wait on ports)",
        fontsize=11, fontweight="bold"
    )
    fig.tight_layout(rect=[0, 0, 1, 0.95])

    out = output_dir / f"scenario_A1_soc_{date_str}.png"
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out}")
    return out


# ── Scenario A1 ────────────────────────────────────────────────────────────────

def run_xos_always_grid_connected(
    csv_path: Path, output_dir: Path, date_str: str, site_label: str = "Northgate"
) -> dict:
    """
    Scenario A1 — XOS Hub MC02, always grid-connected.

    Rule: when a hub unit hits 20% SOC it immediately starts recharging from
    the SMUD grid (83 kW).  Vehicles STAY on their CCS1 ports and wait.
    Once the unit returns to 100% SOC it resumes serving the waiting vehicles.
    Vehicles are released only when fully charged or their dwell window expires.

    Extended dwell is applied: if a vehicle's original dwell is too short to
    receive its full energy need at 80 kW × 0.95 η, its departure_time is
    extended by the deficit.  This models the depot asking vehicles to stay
    longer — a common operating assumption for Caltrans overnight fleets.

    Finds minimum K hub units by trying K = 1, 2, … until all vehicles served.
    """
    from charger_costs_xos_hub import electrical_infra_cost

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*70}")
    print(f"  SCENARIO A1: XOS Hub — always grid-connected")
    print(f"  Site     : {site_label}")
    print(f"  Day      : {date_str}")
    print(f"  Input    : {csv_path.name}")
    print(f"  Output   : {output_dir}")
    print(f"{'='*70}")

    # Load events with multi-day rule
    stem_parts    = csv_path.stem.rsplit("_", 3)
    site_csv_stem = "_".join(stem_parts[:-3]) if len(stem_parts) > 3 else csv_path.stem
    events_df     = load_site_day_data(csv_path)
    events_df     = apply_multiday_rule(events_df, date_str,
                                        site_csv_dir=csv_path.parent,
                                        site_csv_stem=site_csv_stem)

    if events_df.empty:
        print("[WARNING] No events remain — skipping A1.")
        return {}

    # Apply extended dwell so vehicles have minimum window to get fully served
    events_ext = _xos_extended_dwell(events_df)
    n_ext = (events_ext["departure_time"] > events_df["departure_time"]).sum()
    print(f"  Extended dwell applied: {n_ext}/{len(events_df)} vehicles got extra time")

    # Find minimum K that achieves the maximum possible service rate.
    best_served = 0
    best_K      = 1
    for K in range(1, _XOS["MAX_UNITS"] + 1):
        s = _simulate_xos(events_ext, K, mode="a1")
        print(f"    K={K:2d}  served={s['n_served']}/{s['n_vehicles']}")
        if s["n_served"] >= s["n_vehicles"]:
            print(f"  → Minimum XOS units needed (A1): {K}")
            best_served = s["n_served"]
            best_K      = K
            break
        if s["n_served"] > best_served:
            best_served = s["n_served"]
            best_K      = K
    else:
        print(f"  [WARNING] Even {_XOS['MAX_UNITS']} units cannot serve all vehicles.")
        print(f"  → Best achievable: K={best_K} serves {best_served}/{s['n_vehicles']}")
    sim = _simulate_xos(events_ext, best_K, mode="a1")

    # Compute costs
    costs = _xos_a1_cost(sim, date_str)

    # Print results
    _print_xos_a1_results(sim, costs, events_ext, date_str, site_label)

    # SOC plot
    soc_fig = _plot_xos_a1_soc(sim, output_dir, date_str, site_label)

    # Save summary CSV
    n_veh   = sim["n_vehicles"]
    n_serv  = sim["n_served"]
    n_part  = n_veh - n_serv
    summary = pd.DataFrame([{
        "scenario":          "A1_xos_always_grid",
        "site":              site_label,
        "date":              date_str,
        "n_xos_units":       sim["n_units"],
        "n_vehicles":        n_veh,
        "n_fully_served":    n_serv,
        "n_partially_served": n_part,
        "total_daily_capex": costs["total_capex"],
        "energy_cost_usd":   costs["energy_cost"],
        "demand_global_usd": costs["demand_global"],
        "demand_peak_usd":   costs["demand_peak_win"],
        "total_grid_kwh":    costs["total_grid_kwh"],
        "vehicle_kwh":       costs["vehicle_kwh"],
        "p_max_kw":          costs["p_max_kw"],
    }])
    csv_out = output_dir / f"scenario_A1_summary_{date_str}.csv"
    summary.to_csv(csv_out, index=False)
    print(f"  Saved: {csv_out}")

    # Save dispatch log, grid draw, and state history for downstream plotting
    if sim["dispatch_log"]:
        dp = output_dir / f"scenario_A1_dispatch_{date_str}.csv"
        pd.DataFrame(sim["dispatch_log"]).to_csv(dp, index=False)
        print(f"  Saved: {dp}")
    gp = output_dir / f"scenario_A1_grid_draw_{date_str}.csv"
    pd.DataFrame({
        "time_utc": [r["time_utc"] for r in sim["soc_history"]],
        "grid_kw":  sim["grid_draw"],
    }).to_csv(gp, index=False)
    print(f"  Saved: {gp}")
    sp = output_dir / f"scenario_A1_state_{date_str}.csv"
    pd.DataFrame(sim["soc_history"]).to_csv(sp, index=False)
    print(f"  Saved: {sp}")

    return {
        "sim":        sim,
        "costs":      costs,
        "events_df":  events_df,
        "events_ext": events_ext,
        "soc_fig":    soc_fig,
        "summary":    summary,
        "validation": {
            "pass":     n_part == 0,
            "n_served": n_serv,
            "n_total":  n_veh,
        },
    }


def run_xos_not_always_grid_connected(
    csv_path: Path, output_dir: Path, date_str: str, site_label: str = "Northgate"
) -> dict:
    """
    Scenario A2 — XOS Hub MC02, NOT always grid-connected.

    Rule: when a hub unit hits 20% SOC it releases ALL vehicles (disconnects).
    The unit recharges from the SMUD grid (83 kW).  Released vehicles re-enter
    the waiting pool and are immediately reassigned to any available serving unit
    with a free CCS1 port.

    Vehicles receive energy from whichever unit has capacity.  If all units are
    recharging simultaneously, vehicles wait until the first unit completes.

    Extended dwell is applied (same as A1): vehicles with insufficient original
    dwell have their departure extended so they can accumulate the full energy
    need across multiple hub serve/recharge cycles.

    Finds minimum K hub units by trying K = 1, 2, … until all vehicles served.
    """
    from charger_costs_xos_hub import electrical_infra_cost

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*70}")
    print(f"  SCENARIO A2: XOS Hub — disconnect at 20% SOC, reassign")
    print(f"  Site     : {site_label}")
    print(f"  Day      : {date_str}")
    print(f"  Input    : {csv_path.name}")
    print(f"  Output   : {output_dir}")
    print(f"{'='*70}")

    # Load events with multi-day rule
    stem_parts    = csv_path.stem.rsplit("_", 3)
    site_csv_stem = "_".join(stem_parts[:-3]) if len(stem_parts) > 3 else csv_path.stem
    events_df     = load_site_day_data(csv_path)
    events_df     = apply_multiday_rule(events_df, date_str,
                                        site_csv_dir=csv_path.parent,
                                        site_csv_stem=site_csv_stem)

    if events_df.empty:
        print("[WARNING] No events remain — skipping A2.")
        return {}

    events_ext = _xos_extended_dwell(events_df)
    n_ext = (events_ext["departure_time"] > events_df["departure_time"]).sum()
    print(f"  Extended dwell applied: {n_ext}/{len(events_df)} vehicles got extra time")

    # Find minimum K that achieves the maximum possible service rate.
    # Since Freightliners may be physically impossible to fully serve in a
    # single hub cycle, 100% service is not always achievable. Track the
    # first K that achieves each new best, and stop at 100% if reached.
    best_served = 0
    best_K      = 1
    for K in range(1, _XOS["MAX_UNITS"] + 1):
        s = _simulate_xos(events_ext, K, mode="a2")
        print(f"    K={K:2d}  served={s['n_served']}/{s['n_vehicles']}")
        if s["n_served"] >= s["n_vehicles"]:
            print(f"  → Minimum XOS units needed (A2): {K}")
            best_served = s["n_served"]
            best_K      = K
            break
        if s["n_served"] > best_served:
            best_served = s["n_served"]
            best_K      = K
    else:
        print(f"  [WARNING] Even {_XOS['MAX_UNITS']} units cannot serve all vehicles.")
        print(f"  → Best achievable: K={best_K} serves {best_served}/{s['n_vehicles']}")

    # Final simulation with the minimum K + debug log
    debug_log = output_dir / f"scenario_A2_debug_{date_str}.txt"
    sim = _simulate_xos(events_ext, best_K, mode="a2", debug_path=debug_log)

    costs = _xos_a1_cost(sim, date_str)   # same cost formula as A1

    # Print results (reuse A1 printer with label change)
    K = sim["n_units"]
    print(f"\n{'='*70}")
    print(f"  SCENARIO A2 RESULTS — {site_label} / {date_str} — XOS Hub (disconnect at 20%)")
    print(f"{'='*70}")
    print(f"  Hub units deployed : {K}")
    print(f"  Vehicles           : {sim['n_vehicles']}")
    print(f"  Fully served       : {sim['n_served']}/{sim['n_vehicles']}")

    print()
    print(f"  {'Event ID':<25} {'Model':<26} {'Arr':>5} {'Dep':>5} "
          f"{'Need':>8} {'Del':>8} {'Unmet':>7}  Status")
    print("  " + "-" * 100)
    tz = SMUD_TZ
    for _, row in events_ext.sort_values("arrival_time").iterrows():
        v      = row["charging_event_id"]
        arr    = row["arrival_time"].tz_convert(tz).strftime("%H:%M")
        dep    = row["departure_time"].tz_convert(tz).strftime("%H:%M")
        need   = float(row["energy_needed_kwh_for_visit"])
        deliv  = sim["delivered"].get(v, 0.0)
        unmet  = max(need - deliv, 0.0)
        status = "Fully served" if unmet <= ENERGY_TOL else "Partially served"
        model  = str(row.get("ev_equivalent_model", "") or "")[:26]
        print(f"  {v:<25} {model:<26} {arr:>5}  {dep:>5} "
              f"{need:>8.2f}  {deliv:>8.2f}  {unmet:>7.2f}   {status}")

    print(f"\n  COST BREAKDOWN")
    print(f"  {'Daily CapEx (all '+str(K)+' units)':<35}: ${costs['total_capex']:>10.2f}")
    print(f"    per-unit daily CapEx               : ${costs['capex_per_unit']:>10.2f}")
    print(f"    infra install (mid, amortised/unit) : ${costs['infra_per_unit']:>10,.2f}")
    print(f"  {'Grid energy cost':<35}: ${costs['energy_cost']:>10.2f}  ({costs['total_grid_kwh']:.1f} kWh)")
    print(f"  {'Global demand charge (proxy)':<35}: ${costs['demand_global']:>10.2f}  (peak {costs['p_max_kw']:.0f} kW)")
    print(f"  {'Peak-window demand charge (proxy)':<35}: ${costs['demand_peak_win']:>10.2f}  (peak {costs['p_peak_win_kw']:.0f} kW)")
    print(f"  {'Vehicle energy delivered':<35}: {costs['vehicle_kwh']:.1f} kWh")

    # SOC plot (reuse A1 plotter — same signature, different label embedded in title)
    soc_fig_path = output_dir / f"scenario_A2_soc_{date_str}.png"
    K_plot        = sim["n_units"]
    soc_hist      = sim["soc_history"]
    time_grid     = sim["time_grid"]
    times_loc     = pd.DatetimeIndex(time_grid).tz_convert(SMUD_TZ)
    n_steps_p     = sim["n_steps"]
    x_arr         = np.arange(n_steps_p)
    hticks = [i for i in range(n_steps_p) if times_loc[i].minute == 0]
    hlbls  = [times_loc[i].strftime("%H:%M") for i in hticks]

    fig, axes = plt.subplots(K_plot, 1,
                             figsize=(16, 2.8 * K_plot + 1.5), sharex=True)
    if K_plot == 1:
        axes = [axes]
    for k, ax in enumerate(axes):
        soc_arr = np.array([r[f"soc_unit_{k}"] for r in soc_hist]) * 100
        states  = [r[f"state_unit_{k}"] for r in soc_hist]
        for state, col in [("serving", "#aed6f1"), ("recharging", "#a9dfbf")]:
            in_b = False
            for ti in range(n_steps_p + 1):
                s = states[ti] if ti < n_steps_p else None
                if not in_b and s == state:
                    in_b = True; b0 = ti
                elif in_b and (s != state or ti == n_steps_p):
                    ax.axvspan(b0, ti, color=col, alpha=0.40, linewidth=0)
                    in_b = False
        ax.plot(x_arr, soc_arr, color="darkred", linewidth=1.6)
        ax.axhline(20, color="red", linewidth=1.0, linestyle="--", alpha=0.7)
        ax.set_ylim(0, 110); ax.set_yticks([20, 60, 100])
        ax.set_yticklabels(["20%", "60%", "100%"])
        ax.set_ylabel(f"Unit {k+1}\nSOC (%)", fontsize=9)
        ax.grid(axis="x", linestyle=":", alpha=0.3)
        ax.grid(axis="y", linestyle=":", alpha=0.2)
        if k == 0:
            srv_p = mpatches.Patch(color="#aed6f1", alpha=0.6, label="Serving vehicles")
            rch_p = mpatches.Patch(color="#a9dfbf", alpha=0.6, label="Recharging (vehicles released)")
            ax.legend(handles=[srv_p, rch_p], loc="upper right", fontsize=8)
    axes[-1].set_xticks(hticks); axes[-1].set_xticklabels(hlbls, rotation=45, fontsize=8)
    axes[-1].set_xlabel("Time (Pacific)", fontsize=9); axes[-1].set_xlim(0, n_steps_p)
    fig.suptitle(
        f"XOS Hub A2 — SOC per unit | {site_label} {date_str}\n"
        f"Served {sim['n_served']}/{sim['n_vehicles']} vehicles | {K_plot} units | "
        f"Blue=serving, Green=recharging (vehicles reassigned)",
        fontsize=11, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(soc_fig_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {soc_fig_path}")

    # Summary CSV
    n_veh  = sim["n_vehicles"]; n_serv = sim["n_served"]
    summary = pd.DataFrame([{
        "scenario":           "A2_xos_disconnect_20pct",
        "site":               site_label,
        "date":               date_str,
        "n_xos_units":        K,
        "n_vehicles":         n_veh,
        "n_fully_served":     n_serv,
        "n_partially_served": n_veh - n_serv,
        "total_daily_capex":  costs["total_capex"],
        "energy_cost_usd":    costs["energy_cost"],
        "demand_global_usd":  costs["demand_global"],
        "demand_peak_usd":    costs["demand_peak_win"],
        "total_grid_kwh":     costs["total_grid_kwh"],
        "vehicle_kwh":        costs["vehicle_kwh"],
        "p_max_kw":           costs["p_max_kw"],
    }])
    csv_out = output_dir / f"scenario_A2_summary_{date_str}.csv"
    summary.to_csv(csv_out, index=False)
    print(f"  Saved: {csv_out}")

    # Save dispatch log and grid draw for downstream plotting
    if sim["dispatch_log"]:
        disp_path = output_dir / f"scenario_A2_dispatch_{date_str}.csv"
        pd.DataFrame(sim["dispatch_log"]).to_csv(disp_path, index=False)
        print(f"  Saved: {disp_path}")
    grid_path = output_dir / f"scenario_A2_grid_draw_{date_str}.csv"
    pd.DataFrame({
        "time_utc": [r["time_utc"] for r in sim["soc_history"]],
        "grid_kw":  sim["grid_draw"],
    }).to_csv(grid_path, index=False)
    print(f"  Saved: {grid_path}")

    # Save full per-step state history for downstream plot (hub SOC + state per step)
    state_path = output_dir / f"scenario_A2_state_{date_str}.csv"
    pd.DataFrame(sim["soc_history"]).to_csv(state_path, index=False)
    print(f"  Saved: {state_path}")

    return {
        "sim":        sim,
        "costs":      costs,
        "events_df":  events_df,
        "events_ext": events_ext,
        "soc_fig":    soc_fig_path,
        "summary":    summary,
        "validation": {
            "pass":     (n_veh - n_serv) == 0,
            "n_served": n_serv,
            "n_total":  n_veh,
        },
    }


# ── Comparison stub (Step 5) ──────────────────────────────────────────────────

def compare_scenarios(results: dict, output_dir: Path, date_str: str,
                       site_label: str = "Northgate") -> None:
    """Cross-scenario comparison table and plots. (Step 5 — not yet implemented)"""
    raise NotImplementedError("Comparison table will be implemented in Step 5.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Caltrans ZEV Scenario Runner")
    parser.add_argument("date",    nargs="?", default="2025-07-17")
    parser.add_argument("--scenario", choices=["kempower", "a1", "a2", "both", "all"], default="both")
    args     = parser.parse_args()
    date_str = args.date
    date_tag = date_str.replace("-", "_")

    csv_path = BASE_DIR / f"z2z_milp_events_northgate_{date_tag}.csv"

    if not csv_path.exists():
        print(f"[ERROR] Events CSV not found: {csv_path}")
        sys.exit(1)

    generate_spec_table()
    generate_cost_table()

    if args.scenario in ("kempower", "both"):
        out_kmp = OUT_DIR / f"northgate_{date_tag}" / "kempower_only"
        res_kmp = run_kempower_only(csv_path, out_kmp, date_str, site_label="Northgate")
        print(f"\nStep 2 (Kempower) complete. Outputs: {out_kmp}")
        print(f"  Validation passed: {res_kmp['validation']['pass']}")

    if args.scenario in ("a1", "both", "all"):
        out_a1  = OUT_DIR / f"northgate_{date_tag}" / "xos_a1"
        res_a1  = run_xos_always_grid_connected(csv_path, out_a1, date_str, site_label="Northgate")
        print(f"\nStep 3 (XOS A1) complete. Outputs: {out_a1}")
        if res_a1:
            print(f"  Validation passed: {res_a1['validation']['pass']}")

    if args.scenario in ("a2", "all"):
        out_a2  = OUT_DIR / f"northgate_{date_tag}" / "xos_a2"
        res_a2  = run_xos_not_always_grid_connected(csv_path, out_a2, date_str, site_label="Northgate")
        print(f"\nStep 4 (XOS A2) complete. Outputs: {out_a2}")
        if res_a2:
            print(f"  Validation passed: {res_a2['validation']['pass']}")
