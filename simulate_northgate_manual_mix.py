"""
simulate_northgate_manual_mix.py
=================================
Northgate Charger-Sizing — Steps 1 through 4.

Step 1  Validate the final input dataset.
Step 2  Individual feasibility check (benchmark: 350 kW DC, eta=0.90).
Step 3  Discrete-time simulation for one manual charger mix.
Step 4  Save all result files.

CHARGER MIX UNDER TEST:
  L2_19p2kW  : 10   (AC Level 2,  19.2 kW)
  DC_50kW    :  2   (DC fast,     50.0 kW)
  DC_150kW   :  1   (DC fast,    150.0 kW)
  DC_350kW   :  0

CHARGING POWER LOGIC:
  L2 charger  -> effective_power = min(charger_kw, vehicle.max_ac_charge_kw)
  DC charger  -> effective_power = min(charger_kw, vehicle.max_dc_charge_kw)
  If max_ac_charge_kw == 0 -> vehicle cannot use any L2 charger.
  If max_dc_charge_kw == 0 -> vehicle cannot use any DC charger.

SIMULATION PARAMETERS:
  eta        = 0.90
  time_step  = 15 min (0.25 h)
  sim_window = earliest arrival -> latest departure (covers multi-day visits)

PRIORITY RULE:
  For each time step, vehicles needing charge are sorted by:
    1. earliest departure_time (ascending)   — serve urgent vehicles first
    2. highest urgency (descending)           — urgency = remaining_kWh / remaining_h

  Chargers are then assigned greedily in priority order.
  When choosing among available compatible chargers, the most powerful is preferred.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
OUTPUT_DIR  = Path("D:/Geotab_EV_Parameters/charger_sizing_test")
EVENTS_FILE = OUTPUT_DIR / "northgate_representative_day_method_c_visit_level_charging_events.csv"

FEASIBILITY_FILE = OUTPUT_DIR / "northgate_individual_feasibility_check.csv"
VEHICLE_RESULTS  = OUTPUT_DIR / "manual_mix_vehicle_level_results.csv"
CHARGING_LOG     = OUTPUT_DIR / "manual_mix_charging_log.csv"
SUMMARY_FILE     = OUTPUT_DIR / "manual_mix_summary.csv"

# ---------------------------------------------------------------------------
# Simulation parameters
# ---------------------------------------------------------------------------
ETA          = 0.90
TIME_STEP_H  = 0.25        # 15 minutes in hours
FULL_CHG_TOL = 0.10        # kWh tolerance — event considered "fully charged" below this

# Benchmark for individual feasibility check
BENCHMARK_DC_KW = 350.0

# ---------------------------------------------------------------------------
# Charger mix definition
# ---------------------------------------------------------------------------
# key        -> label used in output files
# power_kw   -> nameplate charger power
# ac_dc      -> "AC" or "DC" — selects which vehicle max to use
# count      -> number of units at the site
CHARGER_MIX: dict[str, dict] = {
    "L2_19p2kW": {"power_kw": 19.2,  "ac_dc": "AC", "count": 10},
    "DC_50kW":   {"power_kw": 50.0,  "ac_dc": "DC", "count":  2},
    "DC_150kW":  {"power_kw": 150.0, "ac_dc": "DC", "count":  1},
    "DC_350kW":  {"power_kw": 350.0, "ac_dc": "DC", "count":  0},
}

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _eff_power(charger_power: float, ac_dc: str, max_ac: float, max_dc: float) -> float:
    """Effective charging power given vehicle limits and charger type."""
    if ac_dc == "AC":
        if max_ac <= 0:
            return 0.0
        return min(charger_power, max_ac)
    else:
        if max_dc <= 0:
            return 0.0
        return min(charger_power, max_dc)


# ===========================================================================
# STEP 1 — Validate input dataset
# ===========================================================================
print("=" * 70)
print("  STEP 1: INPUT DATASET VALIDATION")
print("=" * 70)

df = pd.read_csv(EVENTS_FILE)
df["arrival_time"]   = pd.to_datetime(df["arrival_time"],   utc=True, errors="coerce")
df["departure_time"] = pd.to_datetime(df["departure_time"], utc=True, errors="coerce")

# --- Impute missing departure_time from dwell_hours where possible ----------
orig_missing_depart = df["departure_time"].isna()
can_impute = orig_missing_depart & df["dwell_hours"].notna() & df["arrival_time"].notna()
df.loc[can_impute, "departure_time"] = (
    df.loc[can_impute, "arrival_time"]
    + pd.to_timedelta(df.loc[can_impute, "dwell_hours"], unit="h")
)
n_imputed = int(can_impute.sum())

rep_date = df["arrival_time"].dropna().dt.date.value_counts().idxmax()

print(f"\n  Events total                   : {len(df)}")
print(f"  Unique vehicles                : {df['vehicle_id'].nunique()}")
print(f"  Representative date (UTC)      : {rep_date}")
print(f"  Total Method C energy demand   : {df['energy_needed_kwh_for_visit'].sum():.2f} kWh")

dh = df["dwell_hours"].dropna()
print(f"\n  dwell_hours   min / avg / max  : {dh.min():.3f} / {dh.mean():.3f} / {dh.max():.3f} h")
ek = df["energy_needed_kwh_for_visit"].dropna()
print(f"  energy kWh    min / avg / max  : {ek.min():.2f} / {ek.mean():.2f} / {ek.max():.2f} kWh")

print(f"\n  Missing values:")
print(f"    arrival_time                 : {df['arrival_time'].isna().sum()}")
print(f"    departure_time (original)    : {orig_missing_depart.sum()}")
print(f"    departure_time (after impute): {df['departure_time'].isna().sum()}  ({n_imputed} imputed from dwell_hours)")
print(f"    dwell_hours                  : {df['dwell_hours'].isna().sum()}")
print(f"    energy_needed_kwh            : {df['energy_needed_kwh_for_visit'].isna().sum()}")
print(f"    max_ac_charge_kw             : {df['max_ac_charge_kw'].isna().sum()}")
print(f"    max_dc_charge_kw             : {df['max_dc_charge_kw'].isna().sum()}")
print(f"    max_ac_charge_kw == 0        : {(df['max_ac_charge_kw'] == 0).sum()} events (DC-only vehicles)")
print(f"    max_dc_charge_kw == 0        : {(df['max_dc_charge_kw'] == 0).sum()} events")

# Multi-visit vehicles
vc = df.groupby("vehicle_id")["visit_sequence_for_vehicle_that_day"].max()
multi = vc[vc > 1].sort_values(ascending=False)
print(f"\n  Vehicles with multiple visits  : {len(multi)}")
for vid, nv in multi.items():
    visits_df = df[df["vehicle_id"] == vid].sort_values("arrival_time")
    print(f"    {vid}: {nv} visits")
    for _, vr in visits_df.iterrows():
        arr = vr["arrival_time"].strftime("%H:%M") if pd.notna(vr["arrival_time"]) else "NaT"
        dep = vr["departure_time"].strftime("%H:%M") if pd.notna(vr["departure_time"]) else "NaT"
        print(f"      visit {int(vr['visit_sequence_for_vehicle_that_day'])}: "
              f"arrive {arr} UTC  depart {dep} UTC  "
              f"dwell {vr['dwell_hours']:.2f}h  "
              f"energy {vr['energy_needed_kwh_for_visit']:.1f} kWh")

# Consistency checks
ok_dep  = (df["departure_time"] > df["arrival_time"]).sum()
ok_dwl  = (df["dwell_hours"] > 0).sum()
ok_enrg = (df["energy_needed_kwh_for_visit"] > 0).sum()
print(f"\n  Consistency checks (pass / total):")
print(f"    departure > arrival          : {ok_dep} / {len(df)}")
print(f"    dwell_hours > 0              : {ok_dwl} / {len(df)}")
print(f"    energy > 0                   : {ok_enrg} / {len(df)}")

# Mark events that can be scheduled (have all required fields)
needs_skip = (
    df["arrival_time"].isna()
    | df["departure_time"].isna()
    | df["dwell_hours"].isna()
    | df["energy_needed_kwh_for_visit"].isna()
    | (df["departure_time"] <= df["arrival_time"])
)
n_skip = int(needs_skip.sum())
if n_skip:
    print(f"\n  WARNING: {n_skip} event(s) cannot be scheduled (missing/invalid times)")
    print(f"  These events will be EXCLUDED from simulation:")
    for _, r in df[needs_skip].iterrows():
        print(f"    {r['charging_event_id']}")

df_sim = df[~needs_skip].copy().reset_index(drop=True)
print(f"\n  Events in simulation           : {len(df_sim)} of {len(df)}")


# ===========================================================================
# STEP 2 — Individual feasibility check
# ===========================================================================
print()
print("=" * 70)
print(f"  STEP 2: INDIVIDUAL FEASIBILITY CHECK (benchmark: {BENCHMARK_DC_KW:.0f} kW DC, eta={ETA})")
print("=" * 70)

feas_rows = []
for _, row in df_sim.iterrows():
    mac_dc  = float(row["max_dc_charge_kw"]) if pd.notna(row["max_dc_charge_kw"]) else 0.0
    eff_dc  = min(BENCHMARK_DC_KW, mac_dc)
    max_e   = round(ETA * eff_dc * float(row["dwell_hours"]), 3)
    energy  = float(row["energy_needed_kwh_for_visit"])
    feasible = energy <= max_e + FULL_CHG_TOL

    if not feasible:
        if eff_dc == 0:
            reason = "No DC charging capability (max_dc_charge_kw=0 or missing)"
        else:
            reason = (f"max_possible={max_e:.1f} kWh < needed={energy:.1f} kWh "
                      f"(shortfall {energy - max_e:.1f} kWh)")
    else:
        reason = ""

    feas_rows.append({
        "charging_event_id":           row["charging_event_id"],
        "vehicle_id":                  row["vehicle_id"],
        "ev_equivalent_model":         row["ev_equivalent_model"],
        "arrival_time":                row["arrival_time"],
        "departure_time":              row["departure_time"],
        "dwell_hours":                 row["dwell_hours"],
        "energy_needed_kwh_for_visit": round(energy, 3),
        "max_ac_charge_kw":            row["max_ac_charge_kw"],
        "max_dc_charge_kw":            mac_dc,
        "effective_power_with_350kw_dc": eff_dc,
        "max_possible_energy_kwh":     max_e,
        "individually_feasible":       feasible,
        "infeasibility_reason":        reason,
    })

feas_df = pd.DataFrame(feas_rows)
feas_df.to_csv(FEASIBILITY_FILE, index=False, encoding="utf-8-sig")

n_feasible   = int(feas_df["individually_feasible"].sum())
n_infeasible = len(feas_df) - n_feasible
print(f"\n  Feasible events   : {n_feasible} / {len(feas_df)}")
print(f"  Infeasible events : {n_infeasible}")

if n_infeasible > 0:
    print("\n  INFEASIBLE EVENTS (cannot be satisfied even with 350 kW DC):")
    for _, r in feas_df[~feas_df["individually_feasible"]].iterrows():
        print(f"    {r['charging_event_id']}")
        print(f"      EV model   : {r['ev_equivalent_model']}")
        print(f"      dwell      : {r['dwell_hours']:.3f} h")
        print(f"      energy need: {r['energy_needed_kwh_for_visit']:.2f} kWh")
        print(f"      max_poss   : {r['max_possible_energy_kwh']:.2f} kWh")
        print(f"      reason     : {r['infeasibility_reason']}")
    print()
    print("  NOTE: These events will remain unmet regardless of charger mix.")
    print("  Proceeding with simulation (infeasible events included but will show unmet energy).")
else:
    print("  All events individually feasible with 350 kW DC benchmark.")

print(f"\n  Feasibility file -> {FEASIBILITY_FILE.name}")


# ===========================================================================
# STEP 3 — Discrete-time simulation
# ===========================================================================
print()
print("=" * 70)
print("  STEP 3: MANUAL CHARGER-MIX SIMULATION")
print("=" * 70)
print()
print("  Charger mix:")
for cname, cfg in CHARGER_MIX.items():
    if cfg["count"] > 0:
        print(f"    {cname:12s}  {cfg['count']} units  {cfg['power_kw']:.1f} kW  ({cfg['ac_dc']})")
print(f"\n  eta = {ETA}    time_step = {int(TIME_STEP_H * 60)} min")

# --- Build charger pool (one entry per individual charger unit) -------------
charger_pool: list[dict] = []
for cname, cfg in CHARGER_MIX.items():
    for i in range(cfg["count"]):
        charger_pool.append({
            "cid":      f"{cname}_{i+1:02d}",
            "ctype":    cname,
            "power_kw": cfg["power_kw"],
            "ac_dc":    cfg["ac_dc"],
        })

# --- Simulation time window -------------------------------------------------
from datetime import timedelta

sim_start = df_sim["arrival_time"].min().floor("15min")
raw_end   = df_sim["departure_time"].max().ceil("15min")
# Cap at 48 h: covers overnight dwell but excludes long-term parked vehicles
# that would inflate the utilization denominator without affecting results.
SIM_CAP_H = 48.0
sim_end   = min(raw_end, sim_start + timedelta(hours=SIM_CAP_H))
n_steps   = int((sim_end - sim_start).total_seconds() / (TIME_STEP_H * 3600))
print(f"\n  Simulation window: {sim_start}  ->  {sim_end}")
print(f"  Time steps       : {n_steps}  ({int(n_steps * TIME_STEP_H * 60)} min total)")
n_beyond_cap = int((df_sim["departure_time"] > sim_end).sum())
if n_beyond_cap:
    print(f"  NOTE: {n_beyond_cap} event(s) depart after the 48-h cap — they remain in simulation")
    print(f"        but are treated as departing at cap time for utilization accounting.")

# --- State vectors ----------------------------------------------------------
eids      = df_sim["charging_event_id"].tolist()
rem_e     = df_sim.set_index("charging_event_id")["energy_needed_kwh_for_visit"].astype(float).to_dict()
del_e     = {eid: 0.0 for eid in eids}
ctypes_used     = {eid: set() for eid in eids}        # charger types used per event
charger_steps   = {c["cid"]: 0 for c in charger_pool} # how many steps each charger was active

# Pre-extract per-event values for speed
ev_arr  = df_sim.set_index("charging_event_id")["arrival_time"].to_dict()
ev_dep  = df_sim.set_index("charging_event_id")["departure_time"].to_dict()
ev_mac  = df_sim.set_index("charging_event_id")["max_ac_charge_kw"].astype(float).to_dict()
ev_mdc  = df_sim.set_index("charging_event_id")["max_dc_charge_kw"].astype(float).to_dict()
ev_vid  = df_sim.set_index("charging_event_id")["vehicle_id"].to_dict()
ev_model= df_sim.set_index("charging_event_id")["ev_equivalent_model"].to_dict()

log_rows  = []
peak_kw   = 0.0
step_power_ts: list[float] = []  # total site power per step

# --- Main simulation loop ---------------------------------------------------
for step_i in range(n_steps):
    t_start = sim_start + timedelta(hours=step_i * TIME_STEP_H)
    t_end   = t_start   + timedelta(hours=TIME_STEP_H)

    # Vehicles present this step AND still needing charge
    active_eids = [
        eid for eid in eids
        if ev_arr[eid] < t_end
        and ev_dep[eid] > t_start
        and rem_e[eid] > FULL_CHG_TOL
    ]

    if not active_eids:
        step_power_ts.append(0.0)
        continue

    # Compute urgency for sorting
    def urgency(eid: str) -> float:
        rem_h = max((ev_dep[eid] - t_start).total_seconds() / 3600.0, 0.01)
        return rem_e[eid] / rem_h

    active_eids_sorted = sorted(
        active_eids,
        key=lambda eid: (ev_dep[eid], -urgency(eid))
    )

    # Assign chargers greedily in priority order
    avail_cids = {c["cid"] for c in charger_pool}   # available this step
    step_kw    = 0.0

    for eid in active_eids_sorted:
        mac = ev_mac.get(eid, 0.0)
        mdc = ev_mdc.get(eid, 0.0)

        # Find best compatible available charger (highest effective power)
        best_c       = None
        best_eff_kw  = 0.0

        for ch in charger_pool:
            if ch["cid"] not in avail_cids:
                continue
            eff = _eff_power(ch["power_kw"], ch["ac_dc"], mac, mdc)
            if eff > best_eff_kw:
                best_eff_kw = eff
                best_c      = ch

        if best_c is None or best_eff_kw == 0.0:
            continue   # no compatible charger available

        # Compute overlap (handles partial steps at boundaries)
        overlap_start = max(ev_arr[eid], t_start)
        overlap_end   = min(ev_dep[eid], t_end)
        overlap_h     = (overlap_end - overlap_start).total_seconds() / 3600.0
        if overlap_h <= 0:
            continue

        # Energy delivered this step
        e_step = ETA * best_eff_kw * overlap_h
        e_step = min(e_step, rem_e[eid])   # never exceed remaining need

        # Update state
        avail_cids.discard(best_c["cid"])
        rem_e[eid]   -= e_step
        del_e[eid]   += e_step
        charger_steps[best_c["cid"]] += 1
        ctypes_used[eid].add(best_c["ctype"])
        step_kw += best_eff_kw

        log_rows.append({
            "time_step_start_utc":   t_start.isoformat(),
            "time_step_end_utc":     t_end.isoformat(),
            "charging_event_id":     eid,
            "vehicle_id":            ev_vid[eid],
            "ev_equivalent_model":   ev_model[eid],
            "charger_id":            best_c["cid"],
            "charger_type":          best_c["ctype"],
            "charger_power_kw":      best_c["power_kw"],
            "effective_power_kw":    round(best_eff_kw, 3),
            "overlap_hours":         round(overlap_h, 4),
            "energy_delivered_kwh":  round(e_step, 4),
            "remaining_energy_kwh":  round(max(rem_e[eid], 0.0), 4),
        })

    step_power_ts.append(step_kw)
    peak_kw = max(peak_kw, step_kw)

print(f"\n  Simulation complete. {len(log_rows):,} charging log entries.")


# ===========================================================================
# STEP 4 — Build and save outputs
# ===========================================================================
print()
print("=" * 70)
print("  STEP 4: SAVING RESULTS")
print("=" * 70)

# --- Charging log -----------------------------------------------------------
log_df = pd.DataFrame(log_rows)
log_df.to_csv(CHARGING_LOG, index=False, encoding="utf-8-sig")
print(f"\n  Charging log        -> {CHARGING_LOG.name}  ({len(log_df):,} rows)")

# --- Vehicle-level results --------------------------------------------------
veh_rows = []
for _, row in df_sim.iterrows():
    eid    = row["charging_event_id"]
    needed = float(row["energy_needed_kwh_for_visit"])
    deliv  = round(del_e[eid], 3)
    unmet  = round(max(needed - deliv, 0.0), 3)
    full   = unmet <= FULL_CHG_TOL
    ctypes = ", ".join(sorted(ctypes_used[eid])) if ctypes_used[eid] else "none"

    if not full:
        if ctypes == "none":
            note = "No compatible charger assigned during dwell — check AC/DC compatibility and charger availability"
        else:
            note = f"Partial charge: {deliv:.1f}/{needed:.1f} kWh delivered; shortfall {unmet:.1f} kWh"
    else:
        note = "Fully charged"

    veh_rows.append({
        "charging_event_id":           eid,
        "vehicle_id":                  row["vehicle_id"],
        "ev_equivalent_model":         row["ev_equivalent_model"],
        "arrival_time":                row["arrival_time"],
        "departure_time":              row["departure_time"],
        "dwell_hours":                 row["dwell_hours"],
        "energy_needed_kwh_for_visit": round(needed, 3),
        "energy_delivered_kwh":        deliv,
        "energy_unmet_kwh":            unmet,
        "fully_charged":               full,
        "charger_types_used":          ctypes,
        "notes":                       note,
    })

veh_df = pd.DataFrame(veh_rows)
veh_df.to_csv(VEHICLE_RESULTS, index=False, encoding="utf-8-sig")
print(f"  Vehicle-level results -> {VEHICLE_RESULTS.name}  ({len(veh_df)} rows)")

# --- Charger utilization ----------------------------------------------------
total_steps = n_steps

def charger_utilization(ctype_key: str) -> float:
    n_units = CHARGER_MIX[ctype_key]["count"]
    if n_units == 0:
        return float("nan")
    used_steps = sum(
        charger_steps[c["cid"]]
        for c in charger_pool
        if c["ctype"] == ctype_key
    )
    return used_steps / (n_units * total_steps)

# --- Summary ----------------------------------------------------------------
n_fully = int(veh_df["fully_charged"].sum())
n_total = len(veh_df)

sum_rows = [{
    # Charger mix
    "L2_19p2kW_count":          CHARGER_MIX["L2_19p2kW"]["count"],
    "DC_50kW_count":            CHARGER_MIX["DC_50kW"]["count"],
    "DC_150kW_count":           CHARGER_MIX["DC_150kW"]["count"],
    "DC_350kW_count":           CHARGER_MIX["DC_350kW"]["count"],
    # Energy
    "total_requested_kwh":      round(df_sim["energy_needed_kwh_for_visit"].sum(), 2),
    "total_delivered_kwh":      round(sum(del_e.values()), 2),
    "total_unmet_kwh":          round(sum(max(float(r["energy_needed_kwh_for_visit"]) - del_e[r["charging_event_id"]], 0)
                                        for _, r in df_sim.iterrows()), 2),
    # Events
    "events_total":             n_total,
    "events_fully_charged":     n_fully,
    "events_partially_charged": n_total - n_fully,
    "pct_fully_charged":        round(n_fully / n_total * 100, 1),
    # Power
    "peak_simultaneous_kw":     round(peak_kw, 1),
    "eta":                      ETA,
    "time_step_min":            int(TIME_STEP_H * 60),
    # Utilization
    "L2_utilization_pct":       round(charger_utilization("L2_19p2kW") * 100, 1),
    "DC_50_utilization_pct":    round(charger_utilization("DC_50kW")   * 100, 1),
    "DC_150_utilization_pct":   round(charger_utilization("DC_150kW")  * 100, 1),
    "DC_350_utilization_pct":   "N/A (0 units)",
    # Failures
    "events_not_fully_charged": "; ".join(
        veh_df.loc[~veh_df["fully_charged"], "charging_event_id"].tolist()
    ) or "none",
}]

sum_df = pd.DataFrame(sum_rows)
sum_df.to_csv(SUMMARY_FILE, index=False, encoding="utf-8-sig")
print(f"  Summary             -> {SUMMARY_FILE.name}")


# ===========================================================================
# STEP 5 — Console report
# ===========================================================================
print()
print("=" * 70)
print("  STEP 5: RESULTS REPORT")
print("=" * 70)

sr = sum_rows[0]

print(f"""
  Charger mix tested:
    L2_19p2kW  : {sr['L2_19p2kW_count']} units
    DC_50kW    : {sr['DC_50kW_count']} units
    DC_150kW   : {sr['DC_150kW_count']} unit
    DC_350kW   : {sr['DC_350kW_count']} units

  Energy:
    Requested  : {sr['total_requested_kwh']:.2f} kWh
    Delivered  : {sr['total_delivered_kwh']:.2f} kWh
    Unmet      : {sr['total_unmet_kwh']:.2f} kWh

  Events:
    Total      : {sr['events_total']}
    Fully chgd : {sr['events_fully_charged']}  ({sr['pct_fully_charged']}%)
    Partial    : {sr['events_partially_charged']}

  Peak simultaneous charging power : {sr['peak_simultaneous_kw']:.1f} kW

  Charger utilization:
    L2_19p2kW  : {sr['L2_utilization_pct']:.1f}%
    DC_50kW    : {sr['DC_50_utilization_pct']:.1f}%
    DC_150kW   : {sr['DC_150_utilization_pct']:.1f}%
    DC_350kW   : N/A (0 units)
""")

if n_fully < n_total:
    not_full = veh_df[~veh_df["fully_charged"]].sort_values("energy_unmet_kwh", ascending=False)
    print("  Events NOT fully charged:")
    for _, r in not_full.iterrows():
        print(f"    {r['charging_event_id']}")
        print(f"      model   : {r['ev_equivalent_model']}")
        print(f"      dwell   : {r['dwell_hours']:.2f} h")
        print(f"      needed  : {r['energy_needed_kwh_for_visit']:.2f} kWh")
        print(f"      deliv   : {r['energy_delivered_kwh']:.2f} kWh")
        print(f"      unmet   : {r['energy_unmet_kwh']:.2f} kWh")
        print(f"      chargers: {r['charger_types_used']}")
        print(f"      note    : {r['notes']}")
else:
    print("  All events fully charged.")

print()
print("=" * 70)
print("  STOPPED — do not proceed with charger-cost optimization yet.")
print("=" * 70)
