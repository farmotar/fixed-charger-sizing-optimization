"""
northgate_charger_sizing_final.py
==================================
Step 1  Finalize serviceable / excluded datasets for 2025-08-25.
Step 2  Build readable 1xDC_150kW charging schedule for 2025-08-25.
Step 3  Top-5 day robustness check with fixed 15-minute dwell rule.

Fixed rules:
  - dwell_hours >= 0.25 h  (15-min threshold, not subject to change)
  - individually_feasible: energy <= eta * min(350, max_dc_charge_kw) * dwell_hours
  - Infeasible events excluded from sizing and reported separately
  - eta = 0.90, time_step = 15 min

Charging-power logic:
  L2 / AC: effective_power = min(charger_kw, max_ac_charge_kw)
  DC:      effective_power = min(charger_kw, max_dc_charge_kw)
  max_ac_charge_kw = 0  ->  not compatible with L2
  max_dc_charge_kw = 0  ->  not compatible with DC
"""
from __future__ import annotations

from datetime import timedelta
from itertools import product
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DIR   = Path("D:/Geotab_EV_Parameters/charger_sizing_test")
BASE  = Path("D:/Geotab_EV_Parameters")

EVENTS_FILE   = DIR / "northgate_representative_day_method_c_visit_level_charging_events.csv"
MAPPING_FILE  = DIR / "ev_equivalent_max_charge_power_mapping_filled.xlsx"
DAILY_FILE    = DIR / "northgate_daily_method_c_energy_by_date.csv"
SOURCE_EXCEL  = BASE / "northgate_departures_chained_aug_Dec_2025_with_inbound_soc_50pct_fallback.xlsx"

# Outputs – Step 1
SVC_FILE   = DIR / "northgate_2025_08_25_serviceable_charging_events.csv"
EXCL_FILE  = DIR / "northgate_2025_08_25_excluded_infeasible_events.csv"
# Outputs – Step 2
SCHED_FILE = DIR / "best_1xDC150_readable_schedule.csv"
# Outputs – Step 3
TOP5_FILE  = DIR / "top5_day_charger_sizing_sensitivity_fixed_15min.csv"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ETA            = 0.90
TIME_STEP_H    = 0.25
FULL_CHG_TOL   = 0.10
SIM_CAP_H      = 48.0
BENCH_DC_KW    = 350.0
DWELL_MIN_H    = 0.25

CTYPES = ["L2_19p2kW", "DC_50kW", "DC_150kW", "DC_350kW"]
CHARGER_COSTS  = {"L2_19p2kW": 10_000, "DC_50kW": 50_000,
                  "DC_150kW": 150_000, "DC_350kW": 350_000}
CHARGER_SPECS  = {
    "L2_19p2kW": {"power_kw": 19.2,  "ac_dc": "AC"},
    "DC_50kW":   {"power_kw": 50.0,  "ac_dc": "DC"},
    "DC_150kW":  {"power_kw": 150.0, "ac_dc": "DC"},
    "DC_350kW":  {"power_kw": 350.0, "ac_dc": "DC"},
}
SEARCH_BOUNDS = {
    "L2_19p2kW": range(0, 21),
    "DC_50kW":   range(0, 11),
    "DC_150kW":  range(0, 6),
    "DC_350kW":  range(0, 4),
}

# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _eff(charger_kw: float, ac_dc: str, mac: float, mdc: float) -> float:
    if ac_dc == "AC":
        return 0.0 if mac <= 0 else min(charger_kw, mac)
    return 0.0 if mdc <= 0 else min(charger_kw, mdc)


def build_pool(counts: dict) -> list[dict]:
    pool = []
    for ct in CTYPES:
        for i in range(counts[ct]):
            pool.append({"cid": f"{ct}_{i+1:02d}", "ctype": ct,
                         "power_kw": CHARGER_SPECS[ct]["power_kw"],
                         "ac_dc":    CHARGER_SPECS[ct]["ac_dc"]})
    return pool


def simulate(ev_data: dict, pool: list, sim_start, n_steps: int,
             eta: float, dt_h: float, tol: float, detailed: bool = False):
    """
    Greedy discrete-time simulation.
    ev_data[eid] = dict(arr, dep, mac, mdc, energy, vehicle_id, ev_model)
    Returns (all_ok, dld) or (all_ok, dld, csteps, logs, peak, n_steps) if detailed.
    """
    eids   = list(ev_data)
    rem    = {e: ev_data[e]["energy"] for e in eids}
    dld    = {e: 0.0 for e in eids}
    csteps = {c["cid"]: 0 for c in pool}
    logs   = [] if detailed else None
    peak   = 0.0

    for si in range(n_steps):
        ts = sim_start + timedelta(hours=si * dt_h)
        te = ts + timedelta(hours=dt_h)

        active = [e for e in eids
                  if ev_data[e]["arr"] < te and ev_data[e]["dep"] > ts and rem[e] > tol]
        if not active:
            continue

        def _urg(e):
            rh = max((ev_data[e]["dep"] - ts).total_seconds() / 3600, 0.01)
            return rem[e] / rh

        active.sort(key=lambda e: (ev_data[e]["dep"], -_urg(e)))
        avail = {c["cid"] for c in pool}
        kw    = 0.0

        for e in active:
            mac, mdc = ev_data[e]["mac"], ev_data[e]["mdc"]
            bc, beff = None, 0.0
            for ch in pool:
                if ch["cid"] not in avail:
                    continue
                eff = _eff(ch["power_kw"], ch["ac_dc"], mac, mdc)
                if eff > beff:
                    beff, bc = eff, ch
            if bc is None or beff == 0:
                continue

            ov_s = max(ev_data[e]["arr"], ts)
            ov_e = min(ev_data[e]["dep"], te)
            ov_h = (ov_e - ov_s).total_seconds() / 3600
            if ov_h <= 0:
                continue

            e_st = min(eta * beff * ov_h, rem[e])
            avail.discard(bc["cid"])
            rem[e]   -= e_st
            dld[e]   += e_st
            csteps[bc["cid"]] += 1
            kw += beff

            if detailed:
                logs.append({
                    "time_step_start_utc":  ts.isoformat(),
                    "time_step_end_utc":    te.isoformat(),
                    "charging_event_id":    e,
                    "vehicle_id":           ev_data[e]["vehicle_id"],
                    "ev_equivalent_model":  ev_data[e]["ev_model"],
                    "charger_id":           bc["cid"],
                    "charger_type":         bc["ctype"],
                    "charger_power_kw":     bc["power_kw"],
                    "effective_power_kw":   round(beff, 3),
                    "overlap_hours":        round(ov_h, 4),
                    "energy_delivered_kwh": round(e_st, 4),
                    "remaining_energy_kwh": round(max(rem[e], 0.0), 4),
                })

        peak = max(peak, kw)

    all_ok = all(rem[e] <= tol for e in eids)
    if detailed:
        return all_ok, dld, csteps, logs, peak, n_steps
    return all_ok, dld


def run_search(ev_data: dict, sim_start, n_steps: int):
    """
    Bounded enumeration. Returns (best_counts, best_cost, search_rows).
    Sorted by cost ascending; stops after first feasible cost level.
    """
    has_dc_only = any(ev_data[e]["mac"] <= 0 for e in ev_data)
    min_req = {}
    for e, d in ev_data.items():
        avail_h = (d["dep"] - d["arr"]).total_seconds() / 3600
        min_req[e] = d["energy"] / (ETA * max(avail_h, 1e-9))

    all_combos = sorted(
        ((sum(CHARGER_COSTS[k] * v for k, v in dict(zip(CTYPES, c)).items()),
          dict(zip(CTYPES, c)))
         for c in product(*[SEARCH_BOUNDS[k] for k in CTYPES])),
        key=lambda x: x[0]
    )

    rows, best_cost, best_counts = [], None, None

    for cost, counts in all_combos:
        if best_cost is not None and cost > best_cost:
            break

        total_dc = counts["DC_50kW"] + counts["DC_150kW"] + counts["DC_350kW"]

        # Prune: no chargers
        if sum(counts.values()) == 0:
            rows.append({**{k: counts[k] for k in CTYPES}, "total_cost": cost,
                         "all_served": False, "total_delivered_kwh": 0.0,
                         "skip_reason": "no_chargers"})
            continue

        # Prune: DC-only vehicles but no DC
        if has_dc_only and total_dc == 0:
            rows.append({**{k: counts[k] for k in CTYPES}, "total_cost": cost,
                         "all_served": False, "total_delivered_kwh": 0.0,
                         "skip_reason": "no_dc_for_dc_only"})
            continue

        # Prune: no single charger can meet min-power requirement for some event
        ok = True
        for e, req in min_req.items():
            d = ev_data[e]
            best_eff = max(
                (_eff(CHARGER_SPECS[ct]["power_kw"], CHARGER_SPECS[ct]["ac_dc"],
                      d["mac"], d["mdc"]) for ct in CTYPES if counts[ct] > 0),
                default=0.0
            )
            if best_eff < req - 0.1:
                ok = False
                break
        if not ok:
            rows.append({**{k: counts[k] for k in CTYPES}, "total_cost": cost,
                         "all_served": False, "total_delivered_kwh": 0.0,
                         "skip_reason": "power_too_low"})
            continue

        pool = build_pool(counts)
        served, dld = simulate(ev_data, pool, sim_start, n_steps,
                               ETA, TIME_STEP_H, FULL_CHG_TOL)
        rows.append({**{k: counts[k] for k in CTYPES}, "total_cost": cost,
                     "all_served": served,
                     "total_delivered_kwh": round(sum(dld.values()), 2),
                     "skip_reason": ""})

        if served and (best_cost is None or cost <= best_cost):
            best_cost, best_counts = cost, counts.copy()

    return best_counts, best_cost, rows


def make_ev_data(svc_df: pd.DataFrame, sim_start, sim_end) -> dict:
    ev = {}
    for _, r in svc_df.iterrows():
        e = r["charging_event_id"]
        dep = min(r["departure_time"], sim_end)
        ev[e] = {
            "arr":        r["arrival_time"],
            "dep":        dep,
            "mac":        float(r["max_ac_charge_kw"]) if pd.notna(r.get("max_ac_charge_kw")) else 0.0,
            "mdc":        float(r["max_dc_charge_kw"]) if pd.notna(r.get("max_dc_charge_kw")) else 0.0,
            "energy":     float(r["energy_needed_kwh_for_visit"]),
            "vehicle_id": r["vehicle_id"],
            "ev_model":   r["ev_equivalent_model"],
        }
    return ev


def sim_window(svc_df: pd.DataFrame):
    start = svc_df["arrival_time"].min().floor("15min")
    raw   = svc_df["departure_time"].max().ceil("15min")
    end   = min(raw, start + timedelta(hours=SIM_CAP_H))
    steps = int((end - start).total_seconds() / (TIME_STEP_H * 3600))
    return start, end, steps


def feasibility_check(df: pd.DataFrame, energy_col: str, dwell_col: str,
                      mdc_col: str) -> pd.Series:
    eff = df[mdc_col].fillna(0.0).clip(upper=BENCH_DC_KW)
    max_poss = ETA * eff * df[dwell_col]
    return df[energy_col] <= max_poss + FULL_CHG_TOL


# ---------------------------------------------------------------------------
# Source-data builder for arbitrary dates
# ---------------------------------------------------------------------------

def build_day_from_source(df_src: pd.DataFrame, date, mapping_df: pd.DataFrame):
    """
    Build serviceable + excluded event DataFrames for a given date from
    the full source fallback Excel DataFrame.
    Returns (svc_df, excl_df, all_df).
    all_df has dwell >= 0.25 and fill_kwh > 0 (pre-feasibility).
    """
    mask = (
        (df_src["_visit_date"] == date)
        & df_src["northgate_fill_kwh"].notna()
        & (df_src["northgate_fill_kwh"] > 0)
        & df_src["zone_entry_time_utc"].notna()
        & df_src["dwell_hrs"].notna()
        & (df_src["dwell_hrs"] >= DWELL_MIN_H)
    )
    day = df_src[mask].copy()
    day = day.sort_values(["device_name", "zone_entry_time_utc"]).reset_index(drop=True)

    if len(day) == 0:
        empty = pd.DataFrame()
        return empty, empty, empty

    # Visit sequence
    day["visit_seq"] = day.groupby("device_id").cumcount() + 1

    # Charging event ID
    ds = str(date).replace("-", "")
    day["charging_event_id"] = (
        day["device_name"].astype(str) + "_" + ds
        + "_visit_" + day["visit_seq"].astype(str)
    )

    # Rename to standard columns
    day = day.rename(columns={
        "device_name":       "vehicle_id",
        "ev_equivalency":    "ev_equivalent_model",
        "dwell_hrs":         "dwell_hours",
        "northgate_fill_kwh":"energy_needed_kwh_for_visit",
        "zone_entry_time_utc": "arrival_time",
        "zone_exit_time_utc":  "departure_time",
    })

    # Impute missing departure
    no_dep = day["departure_time"].isna() & day["dwell_hours"].notna()
    day.loc[no_dep, "departure_time"] = (
        day.loc[no_dep, "arrival_time"]
        + pd.to_timedelta(day.loc[no_dep, "dwell_hours"], unit="h")
    )

    # Drop rows where departure <= arrival (bad data even after imputation)
    day = day[day["departure_time"] > day["arrival_time"]].copy()

    # Merge max charge rates
    day = day.merge(
        mapping_df[["ev_equivalent_model", "max_ac_charge_kw", "max_dc_charge_kw"]],
        on="ev_equivalent_model",
        how="left",
    )
    day["max_ac_charge_kw"] = day["max_ac_charge_kw"].fillna(0.0)
    day["max_dc_charge_kw"] = day["max_dc_charge_kw"].fillna(0.0)

    # Feasibility
    eff350 = day["max_dc_charge_kw"].clip(upper=BENCH_DC_KW)
    day["effective_power_with_350kw_dc"] = eff350
    day["max_possible_energy_kwh"]       = (ETA * eff350 * day["dwell_hours"]).round(3)
    day["individually_feasible"]         = (
        day["energy_needed_kwh_for_visit"]
        <= day["max_possible_energy_kwh"] + FULL_CHG_TOL
    )

    svc_mask = day["individually_feasible"]
    svc_df   = day[svc_mask].copy().reset_index(drop=True)
    excl_df  = day[~svc_mask].copy().reset_index(drop=True)

    return svc_df, excl_df, day


# ===========================================================================
# LOAD SHARED DATA
# ===========================================================================
print("=" * 70)
print("  NORTHGATE CHARGER SIZING — FINAL ANALYSIS")
print("=" * 70)

print("\n[0] Loading shared inputs ...")

mapping_df = pd.read_excel(MAPPING_FILE)[
    ["ev_equivalent_model", "max_ac_charge_kw", "max_dc_charge_kw"]
].copy()
mapping_df["max_ac_charge_kw"] = pd.to_numeric(mapping_df["max_ac_charge_kw"], errors="coerce").fillna(0.0)
mapping_df["max_dc_charge_kw"] = pd.to_numeric(mapping_df["max_dc_charge_kw"], errors="coerce").fillna(0.0)
print(f"    Max-charge mapping   : {len(mapping_df)} models")

daily_df = pd.read_csv(DAILY_FILE)
top5_dates = [str(d) for d in daily_df.head(5)["date"].tolist()]
print(f"    Top-5 dates          : {top5_dates}")

print(f"    Loading source Excel (may take a moment) ...")
src_raw = pd.read_excel(SOURCE_EXCEL, sheet_name="All Departures")
src_raw["zone_entry_time_utc"] = pd.to_datetime(src_raw["zone_entry_time_utc"], utc=True, errors="coerce")
src_raw["zone_exit_time_utc"]  = pd.to_datetime(src_raw["zone_exit_time_utc"],  utc=True, errors="coerce")
src_raw["_visit_date"] = src_raw["zone_entry_time_utc"].dt.date.astype(str)
print(f"    Source rows          : {len(src_raw):,}")


# ===========================================================================
# STEP 1 — Finalize serviceable / excluded datasets for 2025-08-25
# ===========================================================================
print("\n" + "=" * 70)
print("  STEP 1: FINALIZE 2025-08-25 SERVICEABLE DATASET")
print("=" * 70)

events_df = pd.read_csv(EVENTS_FILE)
events_df["arrival_time"]   = pd.to_datetime(events_df["arrival_time"],   utc=True, errors="coerce")
events_df["departure_time"] = pd.to_datetime(events_df["departure_time"], utc=True, errors="coerce")

# Impute missing departure
can_imp = (
    events_df["departure_time"].isna()
    & events_df["dwell_hours"].notna()
    & events_df["arrival_time"].notna()
)
events_df.loc[can_imp, "departure_time"] = (
    events_df.loc[can_imp, "arrival_time"]
    + pd.to_timedelta(events_df.loc[can_imp, "dwell_hours"], unit="h")
)

# Drop where departure <= arrival
events_df = events_df[events_df["departure_time"] > events_df["arrival_time"]].copy()

# Apply dwell filter and feasibility formula
dwell_ok  = events_df["dwell_hours"] >= DWELL_MIN_H
energy_ok = events_df["energy_needed_kwh_for_visit"] > 0

eff350    = events_df["max_dc_charge_kw"].fillna(0.0).clip(upper=BENCH_DC_KW)
max_poss  = (ETA * eff350 * events_df["dwell_hours"]).round(3)
feas      = events_df["energy_needed_kwh_for_visit"] <= max_poss + FULL_CHG_TOL

events_df["effective_power_with_350kw_dc"] = eff350
events_df["max_possible_energy_kwh"]       = max_poss
events_df["individually_feasible"]         = feas

# Serviceable
svc_mask = dwell_ok & energy_ok & feas
svc_df   = events_df[svc_mask].copy().reset_index(drop=True)
svc_df.to_csv(SVC_FILE, index=False, encoding="utf-8-sig")

# Excluded
excl_mask = ~feas
excl_raw  = events_df[excl_mask].copy().reset_index(drop=True)
shortfall  = (excl_raw["energy_needed_kwh_for_visit"] - excl_raw["max_possible_energy_kwh"]).round(2)

excl_out_cols = [
    "charging_event_id", "vehicle_id", "ev_equivalent_model",
    "arrival_time", "departure_time", "dwell_hours",
    "energy_needed_kwh_for_visit", "max_dc_charge_kw",
    "effective_power_with_350kw_dc", "max_possible_energy_kwh",
]
excl_df = excl_raw[excl_out_cols].copy()
excl_df["shortfall_kwh"] = shortfall.values
excl_df["exclusion_reason"] = (
    "Operationally infeasible dwell window: energy demand cannot be satisfied "
    "even with the strongest available DC charger."
)
excl_df.to_csv(EXCL_FILE, index=False, encoding="utf-8-sig")

print(f"\n  Total events in file  : {len(events_df)}")
print(f"  Serviceable           : {len(svc_df)}  -> {SVC_FILE.name}")
print(f"  Excluded infeasible   : {len(excl_df)}  -> {EXCL_FILE.name}")
print(f"\n  Serviceable summary:")
print(f"    Unique vehicles     : {svc_df['vehicle_id'].nunique()}")
print(f"    Total energy kWh    : {svc_df['energy_needed_kwh_for_visit'].sum():.2f}")
print(f"    Dwell h min/avg/max : "
      f"{svc_df['dwell_hours'].min():.3f} / "
      f"{svc_df['dwell_hours'].mean():.3f} / "
      f"{svc_df['dwell_hours'].max():.3f}")
print(f"    Energy min/avg/max  : "
      f"{svc_df['energy_needed_kwh_for_visit'].min():.2f} / "
      f"{svc_df['energy_needed_kwh_for_visit'].mean():.2f} / "
      f"{svc_df['energy_needed_kwh_for_visit'].max():.2f} kWh")
n_dc_only   = int((svc_df["max_ac_charge_kw"].fillna(0) == 0).sum())
n_ac_compat = int((svc_df["max_ac_charge_kw"].fillna(0) > 0).sum())
print(f"    DC-only events      : {n_dc_only}")
print(f"    AC-compatible events: {n_ac_compat}")
vc = svc_df.groupby("vehicle_id")["visit_sequence_for_vehicle_that_day"].max()
multi = vc[vc > 1]
print(f"    Multi-visit vehicles: {len(multi)}")
for vid, nv in multi.items():
    rows = svc_df[svc_df["vehicle_id"] == vid].sort_values("arrival_time")
    segs = "  |  ".join(
        f"v{int(r['visit_sequence_for_vehicle_that_day'])}: "
        f"{r['arrival_time'].strftime('%H:%M')}->{r['departure_time'].strftime('%H:%M')} "
        f"{r['dwell_hours']:.2f}h {r['energy_needed_kwh_for_visit']:.1f}kWh"
        for _, r in rows.iterrows()
    )
    print(f"      {vid}: {nv} visits  {segs}")

print(f"\n  Excluded events:")
for _, r in excl_df.iterrows():
    print(f"    {r['charging_event_id']}")
    print(f"      Model : {r['ev_equivalent_model']}")
    print(f"      Dwell : {r['dwell_hours']:.3f} h ({r['dwell_hours']*60:.0f} min)")
    print(f"      Need  : {r['energy_needed_kwh_for_visit']:.2f} kWh")
    print(f"      MaxPos: {r['max_possible_energy_kwh']:.2f} kWh")
    print(f"      Short : {r['shortfall_kwh']:.2f} kWh")


# ===========================================================================
# STEP 2 — Readable 1×DC_150kW schedule for 2025-08-25
# ===========================================================================
print("\n" + "=" * 70)
print("  STEP 2: READABLE 1×DC_150kW SCHEDULE — 2025-08-25")
print("=" * 70)

ss, se, ns = sim_window(svc_df)
ev_data_08 = make_ev_data(svc_df, ss, se)

pool_1dc150 = build_pool({"L2_19p2kW": 0, "DC_50kW": 0, "DC_150kW": 1, "DC_350kW": 0})
ok, dld, csteps, logs, peak, _ = simulate(
    ev_data_08, pool_1dc150, ss, ns, ETA, TIME_STEP_H, FULL_CHG_TOL, detailed=True
)

log_df = pd.DataFrame(logs)
total_deliv_s2 = sum(dld.values())
total_unmet_s2 = sum(max(ev_data_08[e]["energy"] - dld[e], 0.0) for e in ev_data_08)
n_steps_s2     = ns
util_150        = round(csteps.get("DC_150kW_01", 0) / n_steps_s2 * 100, 1)

# Build per-event schedule from log
sched_rows = []
for _, row in svc_df.iterrows():
    eid    = row["charging_event_id"]
    energy = float(row["energy_needed_kwh_for_visit"])
    deliv  = round(dld[eid], 3)
    unmet  = round(max(energy - deliv, 0.0), 3)

    ev_logs = log_df[log_df["charging_event_id"] == eid] if len(log_df) > 0 else pd.DataFrame()

    if len(ev_logs) > 0:
        ev_logs = ev_logs.copy()
        ev_logs["_ts"] = pd.to_datetime(ev_logs["time_step_start_utc"], utc=True)
        ev_logs["_te"] = pd.to_datetime(ev_logs["time_step_end_utc"],   utc=True)
        charge_start   = ev_logs["_ts"].min()
        charge_end     = ev_logs["_te"].max()
        total_chg_h    = round(float(ev_logs["overlap_hours"].sum()), 4)
        eff_pw         = round(float(ev_logs["effective_power_kw"].iloc[0]), 3)
        ctype          = ev_logs["charger_type"].iloc[0]
    else:
        charge_start  = None
        charge_end    = None
        total_chg_h   = 0.0
        eff_pw        = 0.0
        ctype         = "none"

    dep = row["departure_time"]
    if charge_end is not None and pd.notna(dep):
        slack_min = round((dep - charge_end).total_seconds() / 60, 1)
    else:
        slack_min = None

    sched_rows.append({
        "charging_event_id":              eid,
        "vehicle_id":                     row["vehicle_id"],
        "ev_equivalent_model":            row["ev_equivalent_model"],
        "arrival_time":                   row["arrival_time"],
        "departure_time":                 dep,
        "dwell_hours":                    round(float(row["dwell_hours"]), 3),
        "energy_needed_kwh_for_visit":    round(energy, 3),
        "charge_start_time":              charge_start,
        "charge_end_time":                charge_end,
        "total_charging_duration_hours":  total_chg_h,
        "delivered_energy_kwh":           deliv,
        "remaining_unmet_kwh":            unmet,
        "charger_type":                   ctype,
        "effective_power_kw":             eff_pw,
        "timing_slack_minutes_after_charging": slack_min,
    })

sched_df = pd.DataFrame(sched_rows)
sched_df.to_csv(SCHED_FILE, index=False, encoding="utf-8-sig")
print(f"\n  Schedule saved -> {SCHED_FILE.name}  ({len(sched_df)} events)")

print(f"\n  --- Schedule summary ---")
print(f"  Total serviceable demand : {sum(ev_data_08[e]['energy'] for e in ev_data_08):.2f} kWh")
print(f"  Total delivered          : {total_deliv_s2:.2f} kWh")
print(f"  Total unmet              : {total_unmet_s2:.2f} kWh")
print(f"  Peak simultaneous power  : {peak:.1f} kW")
print(f"  DC_150kW utilization     : {util_150:.1f}%  (over {ns} steps / {ns*15} min window)")
print(f"  All events served        : {ok}")

# Events near deadline (slack < 10 min)
tight = sched_df[sched_df["timing_slack_minutes_after_charging"].notna()
                 & (sched_df["timing_slack_minutes_after_charging"] < 10)]
print(f"\n  Events with < 10 min slack after charging: {len(tight)}")
if len(tight) > 0:
    for _, r in tight.iterrows():
        print(f"    {r['charging_event_id']}"
              f"  slack={r['timing_slack_minutes_after_charging']:.1f} min"
              f"  dwell={r['dwell_hours']:.2f}h"
              f"  energy={r['energy_needed_kwh_for_visit']:.1f}kWh"
              f"  eff_power={r['effective_power_kw']:.0f}kW")

# Events needing highest effective power
print(f"\n  Events requiring highest effective charging power:")
by_pw = sched_df[sched_df["effective_power_kw"] > 0].sort_values("effective_power_kw", ascending=False).head(5)
for _, r in by_pw.iterrows():
    print(f"    {r['charging_event_id']}"
          f"  eff_power={r['effective_power_kw']:.0f}kW"
          f"  model={r['ev_equivalent_model']}"
          f"  energy={r['energy_needed_kwh_for_visit']:.1f}kWh"
          f"  dwell={r['dwell_hours']:.2f}h"
          f"  slack={r['timing_slack_minutes_after_charging']} min")

print(f"\n  Full schedule (all events, sorted by arrival):")
cols_show = ["charging_event_id", "dwell_hours", "energy_needed_kwh_for_visit",
             "effective_power_kw", "total_charging_duration_hours",
             "remaining_unmet_kwh", "timing_slack_minutes_after_charging"]
sched_sorted = sched_df.sort_values("arrival_time")
print(sched_sorted[cols_show].to_string(index=False))


# ===========================================================================
# STEP 3 — Top-5 day robustness check
# ===========================================================================
print("\n" + "=" * 70)
print("  STEP 3: TOP-5 DAY ROBUSTNESS CHECK (fixed dwell >= 15 min)")
print("=" * 70)

top5_rows = []

for rank, date_str in enumerate(top5_dates, 1):
    print(f"\n  [{rank}] {date_str}")

    svc_d, excl_d, all_d = build_day_from_source(src_raw, date_str, mapping_df)

    n_all   = len(all_d)
    n_svc   = len(svc_d)
    n_excl  = len(excl_d)
    svc_kwh = float(svc_d["energy_needed_kwh_for_visit"].sum()) if n_svc > 0 else 0.0
    excl_kwh= float(excl_d["energy_needed_kwh_for_visit"].sum()) if n_excl > 0 else 0.0
    tot_kwh = svc_kwh + excl_kwh  # clean day total (dwell >= 0.25h)

    print(f"    Events (dwell>=0.25h)  : {n_all}")
    print(f"    Serviceable (feasible) : {n_svc}  ({svc_kwh:.1f} kWh)")
    print(f"    Excluded (infeasible)  : {n_excl}  ({excl_kwh:.1f} kWh)")

    row = {
        "rank":                       rank,
        "date":                       date_str,
        "clean_total_kwh":            round(tot_kwh, 2),
        "n_events_dwell_filtered":    n_all,
        "n_serviceable_events":       n_svc,
        "serviceable_kwh":            round(svc_kwh, 2),
        "n_excluded_infeasible":      n_excl,
        "excluded_kwh":               round(excl_kwh, 2),
    }

    if n_svc == 0:
        print(f"    No serviceable events — skipping optimization.")
        row.update({
            "best_L2_19p2kW": None, "best_DC_50kW": None,
            "best_DC_150kW": None,  "best_DC_350kW": None,
            "best_total_cost": None,
            "delivered_kwh": 0.0, "unmet_kwh": 0.0,
            "peak_kw": 0.0,
            "util_L2_pct": None, "util_DC50_pct": None,
            "util_DC150_pct": None, "util_DC350_pct": None,
            "one_dc150_sufficient": None,
            "note": "No serviceable events",
        })
        top5_rows.append(row)
        continue

    ss_d, se_d, ns_d = sim_window(svc_d)
    ev_d = make_ev_data(svc_d, ss_d, se_d)

    # Check if 1×DC_150kW is sufficient
    pool_test = build_pool({"L2_19p2kW": 0, "DC_50kW": 0, "DC_150kW": 1, "DC_350kW": 0})
    ok_1dc150, _ = simulate(ev_d, pool_test, ss_d, ns_d, ETA, TIME_STEP_H, FULL_CHG_TOL)

    # Full optimization
    best_counts, best_cost, search_rows_d = run_search(ev_d, ss_d, ns_d)

    if best_counts is None:
        print(f"    WARNING: No feasible mix found in search bounds.")
        row.update({
            "best_L2_19p2kW": None, "best_DC_50kW": None,
            "best_DC_150kW": None,  "best_DC_350kW": None,
            "best_total_cost": None,
            "delivered_kwh": 0.0, "unmet_kwh": svc_kwh,
            "peak_kw": 0.0,
            "util_L2_pct": None, "util_DC50_pct": None,
            "util_DC150_pct": None, "util_DC350_pct": None,
            "one_dc150_sufficient": ok_1dc150,
            "note": "No feasible mix in bounds — expand search bounds",
        })
        top5_rows.append(row)
        continue

    # Detailed simulation of best mix
    best_pool_d = build_pool(best_counts)
    _, dld_d, csteps_d, _, peak_d, ns_used = simulate(
        ev_d, best_pool_d, ss_d, ns_d, ETA, TIME_STEP_H, FULL_CHG_TOL, detailed=True
    )

    deliv_d = sum(dld_d.values())
    unmet_d = sum(max(ev_d[e]["energy"] - dld_d[e], 0.0) for e in ev_d)

    def _util(ct, n, cs, ns_):
        if n == 0:
            return None
        used = sum(cs.get(f"{ct}_{i+1:02d}", 0) for i in range(n))
        return round(used / (n * ns_) * 100, 1)

    mix_str = " + ".join(
        f"{best_counts[ct]}x{ct}" for ct in CTYPES if best_counts[ct] > 0
    )
    print(f"    Best mix               : {mix_str}  ${best_cost:,}")
    print(f"    1xDC_150kW sufficient  : {ok_1dc150}")
    print(f"    Peak power             : {peak_d:.1f} kW")
    print(f"    Delivered / Unmet      : {deliv_d:.1f} / {unmet_d:.1f} kWh")

    row.update({
        "best_L2_19p2kW":     best_counts["L2_19p2kW"],
        "best_DC_50kW":       best_counts["DC_50kW"],
        "best_DC_150kW":      best_counts["DC_150kW"],
        "best_DC_350kW":      best_counts["DC_350kW"],
        "best_total_cost":    best_cost,
        "delivered_kwh":      round(deliv_d, 2),
        "unmet_kwh":          round(unmet_d, 2),
        "peak_kw":            round(peak_d, 1),
        "util_L2_pct":        _util("L2_19p2kW", best_counts["L2_19p2kW"], csteps_d, ns_used),
        "util_DC50_pct":      _util("DC_50kW",   best_counts["DC_50kW"],   csteps_d, ns_used),
        "util_DC150_pct":     _util("DC_150kW",  best_counts["DC_150kW"],  csteps_d, ns_used),
        "util_DC350_pct":     _util("DC_350kW",  best_counts["DC_350kW"],  csteps_d, ns_used),
        "one_dc150_sufficient": ok_1dc150,
        "note": "",
    })
    top5_rows.append(row)

top5_df = pd.DataFrame(top5_rows)
top5_df.to_csv(TOP5_FILE, index=False, encoding="utf-8-sig")
print(f"\n  Top-5 results saved -> {TOP5_FILE.name}")

# ---------------------------------------------------------------------------
# Final summary table
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("  TOP-5 DAY SUMMARY")
print("=" * 70)

print(f"\n  {'Date':<12} {'ClnKWh':>8} {'SvcEvt':>7} {'SvcKWh':>8} "
      f"{'Excl':>5} {'BestMix':<30} {'Cost':>10} {'PkKW':>6} {'1DC150?':>8}")
print(f"  {'-'*97}")

for r in top5_rows:
    mix_parts = [
        f"{r.get('best_' + ct, 0) or 0}x{ct}"
        for ct in CTYPES
        if (r.get("best_" + ct) or 0) > 0
    ]
    mix      = " ".join(mix_parts) if mix_parts else "none"
    cost_str = f"${r['best_total_cost']:,}" if r.get("best_total_cost") else "N/A"
    peak_str = f"{r['peak_kw']:.0f}" if r.get("peak_kw") else "N/A"
    suf_str  = str(r.get("one_dc150_sufficient", "N/A"))
    print(f"  {r['date']:<12} {r['clean_total_kwh']:>8.1f} "
          f"{r['n_serviceable_events']:>7} {r['serviceable_kwh']:>8.1f} "
          f"{r['n_excluded_infeasible']:>5} {mix:<38} {cost_str:>10} "
          f"{peak_str:>6} {suf_str:>8}")

print()
print("=" * 70)
print("  ANALYSIS COMPLETE")
print("=" * 70)
