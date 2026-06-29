"""
optimize_northgate_charger_mix.py
==================================
Cost-aware charger mix search for Northgate 2025-08-25 serviceable events.

Steps:
  1. Build serviceable dataset (dwell >= 0.25h, individually_feasible, energy > 0)
  2. Build excluded infeasible events CSV
  3. Report serviceable demand
  4. Bounded enumeration: L2(0-20) x DC_50(0-10) x DC_150(0-5) x DC_350(0-3)
     sorted by ascending cost, early-stop at first feasible cost level
  5. Re-simulate best mix for detailed vehicle-level and log outputs
  6. Save all outputs and print report
"""
from __future__ import annotations

from datetime import timedelta
from itertools import product
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
OUTPUT_DIR = Path("D:/Geotab_EV_Parameters/charger_sizing_test")

EVENTS_FILE = OUTPUT_DIR / "northgate_representative_day_method_c_visit_level_charging_events.csv"
FEAS_FILE   = OUTPUT_DIR / "northgate_individual_feasibility_check.csv"

SERVICEABLE_FILE = OUTPUT_DIR / "northgate_2025_08_25_serviceable_charging_events.csv"
EXCLUDED_FILE    = OUTPUT_DIR / "northgate_2025_08_25_excluded_infeasible_events.csv"
SEARCH_RESULTS   = OUTPUT_DIR / "charger_mix_search_results_serviceable.csv"
BEST_MIX_FILE    = OUTPUT_DIR / "best_min_cost_charger_mix_serviceable.csv"
BEST_VEH_FILE    = OUTPUT_DIR / "best_serviceable_vehicle_level_results.csv"
BEST_LOG_FILE    = OUTPUT_DIR / "best_serviceable_charging_log.csv"
SUMMARY_FILE     = OUTPUT_DIR / "serviceable_charger_sizing_summary.csv"

# ---------------------------------------------------------------------------
# Simulation parameters
# ---------------------------------------------------------------------------
ETA          = 0.90
TIME_STEP_H  = 0.25
FULL_CHG_TOL = 0.10
SIM_CAP_H    = 48.0

# ---------------------------------------------------------------------------
# Charger definitions
# ---------------------------------------------------------------------------
CTYPES = ["L2_19p2kW", "DC_50kW", "DC_150kW", "DC_350kW"]

CHARGER_COSTS = {
    "L2_19p2kW": 10_000,
    "DC_50kW":   50_000,
    "DC_150kW":  150_000,
    "DC_350kW":  350_000,
}
CHARGER_SPECS = {
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
# Helpers
# ---------------------------------------------------------------------------

def _eff_power(charger_power: float, ac_dc: str, max_ac: float, max_dc: float) -> float:
    if ac_dc == "AC":
        return 0.0 if max_ac <= 0 else min(charger_power, max_ac)
    else:
        return 0.0 if max_dc <= 0 else min(charger_power, max_dc)


def build_pool(counts: dict) -> list[dict]:
    pool = []
    for ct in CTYPES:
        for i in range(counts[ct]):
            pool.append({
                "cid":      f"{ct}_{i+1:02d}",
                "ctype":    ct,
                "power_kw": CHARGER_SPECS[ct]["power_kw"],
                "ac_dc":    CHARGER_SPECS[ct]["ac_dc"],
            })
    return pool


def simulate(ev_data: dict, charger_pool: list, sim_start, n_steps: int,
             eta: float, dt_h: float, tol: float,
             detailed: bool = False):
    """
    Discrete-time greedy simulation.

    ev_data keys: charging_event_id
    ev_data values: dict(arr, dep, mac, mdc, energy, vehicle_id, ev_model)

    Returns (all_served, del_e) in fast mode.
    Returns (all_served, del_e, csteps, logs, peak_kw, n_steps) in detailed mode.
    """
    eids   = list(ev_data.keys())
    rem    = {eid: ev_data[eid]["energy"] for eid in eids}
    dld    = {eid: 0.0  for eid in eids}
    csteps = {c["cid"]: 0 for c in charger_pool}
    logs   = [] if detailed else None
    peak   = 0.0

    for si in range(n_steps):
        ts = sim_start + timedelta(hours=si * dt_h)
        te = ts + timedelta(hours=dt_h)

        active = [
            eid for eid in eids
            if ev_data[eid]["arr"] < te
            and ev_data[eid]["dep"] > ts
            and rem[eid] > tol
        ]
        if not active:
            continue

        def _urg(eid: str) -> float:
            rh = max((ev_data[eid]["dep"] - ts).total_seconds() / 3600, 0.01)
            return rem[eid] / rh

        active.sort(key=lambda eid: (ev_data[eid]["dep"], -_urg(eid)))
        avail   = {c["cid"] for c in charger_pool}
        step_kw = 0.0

        for eid in active:
            mac = ev_data[eid]["mac"]
            mdc = ev_data[eid]["mdc"]

            best_c, best_eff = None, 0.0
            for ch in charger_pool:
                if ch["cid"] not in avail:
                    continue
                eff = _eff_power(ch["power_kw"], ch["ac_dc"], mac, mdc)
                if eff > best_eff:
                    best_eff = eff
                    best_c   = ch

            if best_c is None or best_eff == 0:
                continue

            ov_s  = max(ev_data[eid]["arr"], ts)
            ov_e  = min(ev_data[eid]["dep"], te)
            ov_h  = (ov_e - ov_s).total_seconds() / 3600
            if ov_h <= 0:
                continue

            e_step = min(eta * best_eff * ov_h, rem[eid])
            avail.discard(best_c["cid"])
            rem[eid]   -= e_step
            dld[eid]   += e_step
            csteps[best_c["cid"]] += 1
            step_kw += best_eff

            if detailed:
                logs.append({
                    "time_step_start_utc":  ts.isoformat(),
                    "time_step_end_utc":    te.isoformat(),
                    "charging_event_id":    eid,
                    "vehicle_id":           ev_data[eid]["vehicle_id"],
                    "ev_equivalent_model":  ev_data[eid]["ev_model"],
                    "charger_id":           best_c["cid"],
                    "charger_type":         best_c["ctype"],
                    "charger_power_kw":     best_c["power_kw"],
                    "effective_power_kw":   round(best_eff, 3),
                    "overlap_hours":        round(ov_h, 4),
                    "energy_delivered_kwh": round(e_step, 4),
                    "remaining_energy_kwh": round(max(rem[eid], 0.0), 4),
                })

        peak = max(peak, step_kw)

    all_ok = all(rem[eid] <= tol for eid in eids)

    if detailed:
        return all_ok, dld, csteps, logs, peak, n_steps
    return all_ok, dld


# ===========================================================================
# MAIN
# ===========================================================================
print("=" * 70)
print("  NORTHGATE CHARGER MIX OPTIMIZATION — SERVICEABLE EVENTS ONLY")
print("=" * 70)

# ---------------------------------------------------------------------------
# Step 1 — Load and merge
# ---------------------------------------------------------------------------
print("\n[1] Loading datasets ...")

events_df = pd.read_csv(EVENTS_FILE)
events_df["arrival_time"]   = pd.to_datetime(events_df["arrival_time"],   utc=True, errors="coerce")
events_df["departure_time"] = pd.to_datetime(events_df["departure_time"], utc=True, errors="coerce")

# Impute missing departure_time from dwell_hours
can_imp = (
    events_df["departure_time"].isna()
    & events_df["dwell_hours"].notna()
    & events_df["arrival_time"].notna()
)
events_df.loc[can_imp, "departure_time"] = (
    events_df.loc[can_imp, "arrival_time"]
    + pd.to_timedelta(events_df.loc[can_imp, "dwell_hours"], unit="h")
)

feas_df = pd.read_csv(FEAS_FILE)

print(f"    Events file      : {len(events_df)} rows")
print(f"    Feasibility file : {len(feas_df)} rows")

merged = events_df.merge(
    feas_df[["charging_event_id", "individually_feasible",
             "max_possible_energy_kwh", "infeasibility_reason"]],
    on="charging_event_id",
    how="left",
)

# ---------------------------------------------------------------------------
# Step 1A — Serviceable dataset
# ---------------------------------------------------------------------------
svc_mask = (
    merged["dwell_hours"].notna()
    & (merged["dwell_hours"] >= 0.25)
    & merged["individually_feasible"].fillna(False).astype(bool)
    & merged["energy_needed_kwh_for_visit"].notna()
    & (merged["energy_needed_kwh_for_visit"] > 0)
)
svc_df = merged[svc_mask].copy().reset_index(drop=True)
svc_df.to_csv(SERVICEABLE_FILE, index=False, encoding="utf-8-sig")
print(f"\n    Serviceable events  : {len(svc_df)} -> {SERVICEABLE_FILE.name}")

# ---------------------------------------------------------------------------
# Step 1B — Excluded infeasible events
# ---------------------------------------------------------------------------
inf_mask  = merged["individually_feasible"].fillna(True).astype(bool) == False
infeas_df = merged[inf_mask].copy().reset_index(drop=True)

shortfall = (
    infeas_df["energy_needed_kwh_for_visit"] - infeas_df["max_possible_energy_kwh"]
).round(2)

excl_df = pd.DataFrame({
    "charging_event_id":           infeas_df["charging_event_id"].values,
    "vehicle_id":                  infeas_df["vehicle_id"].values,
    "ev_equivalent_model":         infeas_df["ev_equivalent_model"].values,
    "dwell_hours":                 infeas_df["dwell_hours"].values,
    "energy_needed_kwh_for_visit": infeas_df["energy_needed_kwh_for_visit"].values,
    "max_dc_charge_kw":            infeas_df["max_dc_charge_kw"].values,
    "max_possible_energy_kwh":     infeas_df["max_possible_energy_kwh"].values,
    "shortfall_kwh":               shortfall.values,
    "exclusion_reason":            (
        "Operationally infeasible dwell window: energy demand cannot be satisfied "
        "even with the strongest available DC charger."
    ),
})
excl_df.to_csv(EXCLUDED_FILE, index=False, encoding="utf-8-sig")
print(f"    Excluded infeasible : {len(excl_df)} -> {EXCLUDED_FILE.name}")

# ---------------------------------------------------------------------------
# Step 2 — Report serviceable demand
# ---------------------------------------------------------------------------
print("\n[2] Serviceable demand report:")

n_dc_only   = int((svc_df["max_ac_charge_kw"].fillna(0) == 0).sum())
n_ac_compat = int((svc_df["max_ac_charge_kw"].fillna(0) > 0).sum())
total_svc   = float(svc_df["energy_needed_kwh_for_visit"].sum())

print(f"    Events                : {len(svc_df)}")
print(f"    Unique vehicles       : {svc_df['vehicle_id'].nunique()}")
print(f"    Total serviceable kWh : {total_svc:.2f}")
dh = svc_df["dwell_hours"]
ek = svc_df["energy_needed_kwh_for_visit"]
print(f"    Dwell h   min/avg/max : {dh.min():.3f} / {dh.mean():.3f} / {dh.max():.3f}")
print(f"    Energy    min/avg/max : {ek.min():.2f} / {ek.mean():.2f} / {ek.max():.2f} kWh")
print(f"    DC-only events        : {n_dc_only}  (max_ac_charge_kw = 0)")
print(f"    AC-compatible events  : {n_ac_compat}")

vc    = svc_df.groupby("vehicle_id")["visit_sequence_for_vehicle_that_day"].max()
multi = vc[vc > 1]
print(f"    Multi-visit vehicles  : {len(multi)}")
for vid, nv in multi.items():
    rows = svc_df[svc_df["vehicle_id"] == vid].sort_values("arrival_time")
    print(f"      {vid}: {nv} visits")
    for _, r in rows.iterrows():
        arr = r["arrival_time"].strftime("%H:%M") if pd.notna(r["arrival_time"]) else "NaT"
        dep = r["departure_time"].strftime("%H:%M") if pd.notna(r["departure_time"]) else "NaT"
        print(f"        v{int(r['visit_sequence_for_vehicle_that_day'])}: "
              f"{arr}->{dep}  {r['dwell_hours']:.2f}h  {r['energy_needed_kwh_for_visit']:.1f} kWh")

# ---------------------------------------------------------------------------
# Step 3 — Build simulation inputs
# ---------------------------------------------------------------------------
print("\n[3] Building simulation inputs ...")

sim_start = svc_df["arrival_time"].min().floor("15min")
raw_end   = svc_df["departure_time"].max().ceil("15min")
sim_end   = min(raw_end, sim_start + timedelta(hours=SIM_CAP_H))
n_steps   = int((sim_end - sim_start).total_seconds() / (TIME_STEP_H * 3600))

print(f"    Simulation window : {sim_start}  ->  {sim_end}")
print(f"    Time steps        : {n_steps}  ({int(n_steps * TIME_STEP_H * 60)} min)")

ev_data: dict = {}
for _, row in svc_df.iterrows():
    eid = row["charging_event_id"]
    dep = row["departure_time"]
    ev_data[eid] = {
        "arr":        row["arrival_time"],
        "dep":        min(dep, sim_end),   # cap at 48h window
        "mac":        float(row["max_ac_charge_kw"]) if pd.notna(row["max_ac_charge_kw"]) else 0.0,
        "mdc":        float(row["max_dc_charge_kw"]) if pd.notna(row["max_dc_charge_kw"]) else 0.0,
        "energy":     float(row["energy_needed_kwh_for_visit"]),
        "vehicle_id": row["vehicle_id"],
        "ev_model":   row["ev_equivalent_model"],
    }

# Precompute per-event minimum required effective power for pruning
min_req: dict[str, float] = {}
for eid, d in ev_data.items():
    avail_h = (d["dep"] - d["arr"]).total_seconds() / 3600
    min_req[eid] = d["energy"] / (ETA * max(avail_h, 1e-6))

has_dc_only = any(d["mac"] <= 0 for d in ev_data.values())

# ---------------------------------------------------------------------------
# Step 4 — Bounded enumeration
# ---------------------------------------------------------------------------
print("\n[4] Generating search combinations ...")

all_combos: list[tuple[int, dict]] = []
for combo in product(*[SEARCH_BOUNDS[k] for k in CTYPES]):
    counts = dict(zip(CTYPES, combo))
    cost   = sum(CHARGER_COSTS[k] * v for k, v in counts.items())
    all_combos.append((cost, counts))

all_combos.sort(key=lambda x: x[0])
print(f"    Total combinations : {len(all_combos)}")
print(f"    Running enumeration (stops after first feasible cost level) ...")

search_rows: list[dict] = []
best_cost:   int | None  = None
best_counts: dict | None = None
n_tested = 0

for cost, counts in all_combos:
    # Early stop: once we pass the best cost, no improvement possible
    if best_cost is not None and cost > best_cost:
        break

    # Fast prune 1: no chargers at all
    total_chargers = sum(counts.values())
    if total_chargers == 0:
        search_rows.append({**{k: counts[k] for k in CTYPES},
                            "total_cost": cost, "all_served": False,
                            "total_delivered_kwh": 0.0, "skip_reason": "no_chargers"})
        continue

    # Fast prune 2: DC-only vehicles exist but zero DC chargers
    total_dc = counts["DC_50kW"] + counts["DC_150kW"] + counts["DC_350kW"]
    if has_dc_only and total_dc == 0:
        search_rows.append({**{k: counts[k] for k in CTYPES},
                            "total_cost": cost, "all_served": False,
                            "total_delivered_kwh": 0.0, "skip_reason": "no_dc_for_dc_only"})
        continue

    # Fast prune 3: no single charger in the pool can deliver min_req power for some event
    feasible_by_power = True
    for eid, req in min_req.items():
        d = ev_data[eid]
        max_eff = 0.0
        for ct in CTYPES:
            if counts[ct] == 0:
                continue
            eff = _eff_power(CHARGER_SPECS[ct]["power_kw"], CHARGER_SPECS[ct]["ac_dc"],
                             d["mac"], d["mdc"])
            if eff > max_eff:
                max_eff = eff
        if max_eff < req - 0.1:
            feasible_by_power = False
            break
    if not feasible_by_power:
        search_rows.append({**{k: counts[k] for k in CTYPES},
                            "total_cost": cost, "all_served": False,
                            "total_delivered_kwh": 0.0, "skip_reason": "power_too_low"})
        continue

    # Full simulation
    pool = build_pool(counts)
    all_served, dld = simulate(ev_data, pool, sim_start, n_steps,
                               ETA, TIME_STEP_H, FULL_CHG_TOL)
    n_tested += 1

    search_rows.append({
        **{k: counts[k] for k in CTYPES},
        "total_cost":          cost,
        "all_served":          all_served,
        "total_delivered_kwh": round(sum(dld.values()), 2),
        "skip_reason":         "",
    })

    if all_served and (best_cost is None or cost <= best_cost):
        best_cost   = cost
        best_counts = counts.copy()

search_df = pd.DataFrame(search_rows)
search_df.to_csv(SEARCH_RESULTS, index=False, encoding="utf-8-sig")

n_feasible_combos = int(search_df["all_served"].sum())
print(f"    Combinations evaluated (full sim): {n_tested}")
print(f"    Combinations pruned              : {len(search_rows) - n_tested}")
print(f"    Feasible combinations found      : {n_feasible_combos}")
print(f"    Best minimum cost                : ${best_cost:,}")
print(f"    Best mix                         : {best_counts}")
print(f"    Search results saved -> {SEARCH_RESULTS.name}")

if best_counts is None:
    print("\nERROR: No feasible mix found within search bounds. Expand bounds and retry.")
    raise SystemExit(1)

# ---------------------------------------------------------------------------
# Step 5 — Re-simulate best mix (detailed)
# ---------------------------------------------------------------------------
print("\n[5] Re-simulating best mix for detailed outputs ...")

best_pool = build_pool(best_counts)
all_served_best, dld_best, csteps_best, logs_best, peak_kw, _ = simulate(
    ev_data, best_pool, sim_start, n_steps,
    ETA, TIME_STEP_H, FULL_CHG_TOL, detailed=True,
)

# Vehicle-level results
veh_rows = []
for _, row in svc_df.iterrows():
    eid    = row["charging_event_id"]
    needed = float(row["energy_needed_kwh_for_visit"])
    deliv  = round(dld_best[eid], 3)
    unmet  = round(max(needed - deliv, 0.0), 3)
    full   = unmet <= FULL_CHG_TOL
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
    })

veh_df = pd.DataFrame(veh_rows)
veh_df.to_csv(BEST_VEH_FILE, index=False, encoding="utf-8-sig")
print(f"    Vehicle results  -> {BEST_VEH_FILE.name}")

log_df = pd.DataFrame(logs_best)
log_df.to_csv(BEST_LOG_FILE, index=False, encoding="utf-8-sig")
print(f"    Charging log     -> {BEST_LOG_FILE.name}  ({len(log_df)} entries)")

# Best mix table
best_mix_rows = []
for ct in CTYPES:
    n = best_counts[ct]
    best_mix_rows.append({
        "charger_type":   ct,
        "count":          n,
        "power_kw":       CHARGER_SPECS[ct]["power_kw"],
        "ac_dc":          CHARGER_SPECS[ct]["ac_dc"],
        "unit_cost_usd":  CHARGER_COSTS[ct],
        "total_cost_usd": CHARGER_COSTS[ct] * n,
        "selected":       n > 0,
    })
best_mix_df = pd.DataFrame(best_mix_rows)
best_mix_df.to_csv(BEST_MIX_FILE, index=False, encoding="utf-8-sig")
print(f"    Best mix file    -> {BEST_MIX_FILE.name}")

# Charger utilization
def util_pct(ct: str) -> float | str:
    n = best_counts[ct]
    if n == 0:
        return float("nan")
    used = sum(csteps_best.get(f"{ct}_{i+1:02d}", 0) for i in range(n))
    return round(used / (n * n_steps) * 100, 1)

total_deliv = sum(dld_best.values())
total_unmet = sum(
    max(float(r["energy_needed_kwh_for_visit"]) - dld_best[r["charging_event_id"]], 0.0)
    for _, r in svc_df.iterrows()
)
n_fully = int(veh_df["fully_charged"].sum())
n_total = len(veh_df)

# Summary
excl_total_kwh = float(excl_df["energy_needed_kwh_for_visit"].sum())
orig_day_kwh   = 473.77

sum_row = {
    "representative_date":          "2025-08-25",
    "dwell_filter_applied":         "dwell_hours >= 0.25h",
    "serviceable_events":           n_total,
    "excluded_infeasible_events":   len(excl_df),
    # Best mix
    "n_L2_19p2kW":  best_counts["L2_19p2kW"],
    "n_DC_50kW":    best_counts["DC_50kW"],
    "n_DC_150kW":   best_counts["DC_150kW"],
    "n_DC_350kW":   best_counts["DC_350kW"],
    "total_charger_cost_usd": best_cost,
    # Energy
    "total_serviceable_kwh":  round(total_svc, 2),
    "total_delivered_kwh":    round(total_deliv, 2),
    "total_unmet_kwh":        round(total_unmet, 2),
    "pct_fully_charged":      round(n_fully / n_total * 100, 1),
    # Power
    "peak_simultaneous_kw":   round(peak_kw, 1),
    "eta":                    ETA,
    "time_step_min":          int(TIME_STEP_H * 60),
    # Utilization
    "util_L2_19p2kW_pct":  util_pct("L2_19p2kW"),
    "util_DC_50kW_pct":    util_pct("DC_50kW"),
    "util_DC_150kW_pct":   util_pct("DC_150kW"),
    "util_DC_350kW_pct":   util_pct("DC_350kW"),
    # Excluded
    "excluded_event_ids":           "; ".join(excl_df["charging_event_id"].tolist()),
    "excluded_total_energy_kwh":    round(excl_total_kwh, 2),
    # Context
    "original_day_total_kwh":       orig_day_kwh,
    "note": (
        "Selected mix satisfies only the serviceable subset. "
        "Excluded events require operational changes (longer dwell) to be served."
    ),
}
sum_df = pd.DataFrame([sum_row])
sum_df.to_csv(SUMMARY_FILE, index=False, encoding="utf-8-sig")
print(f"    Summary          -> {SUMMARY_FILE.name}")


# ===========================================================================
# Step 6 — Console report
# ===========================================================================
print()
print("=" * 70)
print("  RESULTS REPORT")
print("=" * 70)

print(f"\n  Representative date     : 2025-08-25")
print(f"  Dwell filter            : >= 0.25h")
print(f"  Serviceable events      : {n_total}  (of 28 total on this day)")
print(f"  Excluded infeasible     : {len(excl_df)}")

print(f"\n  1. BEST MINIMUM-COST CHARGER MIX:")
print(f"  {'Charger':<14}  {'Units':>5}  {'kW':>6}  {'Type':>4}  {'Unit cost':>10}  {'Total cost':>12}")
print(f"  {'-'*60}")
for ct in CTYPES:
    n   = best_counts[ct]
    sel = "<<" if n > 0 else "  "
    print(f"  {ct:<14}  {n:>5}  {CHARGER_SPECS[ct]['power_kw']:>6.1f}  "
          f"{CHARGER_SPECS[ct]['ac_dc']:>4}  "
          f"${CHARGER_COSTS[ct]:>9,}  "
          f"${CHARGER_COSTS[ct]*n:>11,}  {sel}")
print(f"  {'-'*60}")
print(f"  {'TOTAL':14}  {'':>5}  {'':>6}  {'':>4}  {'':>10}  ${best_cost:>11,}")

print(f"\n  2. TOTAL CHARGER COST    : ${best_cost:,}")
print(f"\n  3. TOTAL SERVICEABLE ENERGY DEMAND : {total_svc:.2f} kWh")
print(f"  4. TOTAL DELIVERED ENERGY          : {round(total_deliv, 2):.2f} kWh")
print(f"  5. TOTAL UNMET ENERGY              : {round(total_unmet, 2):.2f} kWh  (should be 0 for serviceable events)")
print(f"     Events fully charged            : {n_fully} / {n_total}  ({n_fully/n_total*100:.1f}%)")

print(f"\n  6. PEAK SIMULTANEOUS CHARGING POWER: {peak_kw:.1f} kW")

print(f"\n  7. CHARGER UTILIZATION (over 48-h simulation window):")
for ct in CTYPES:
    n = best_counts[ct]
    u = util_pct(ct)
    if n > 0:
        print(f"     {ct:<14}: {n} unit(s)  {u:.1f}%")
    else:
        print(f"     {ct:<14}: not selected")

print(f"\n  8. CHARGER TYPE SELECTION RATIONALE:")
sel_types = [ct for ct in CTYPES if best_counts[ct] > 0]
for ct in sel_types:
    spec  = CHARGER_SPECS[ct]
    ac_dc = spec["ac_dc"]
    kw    = spec["power_kw"]
    n     = best_counts[ct]
    if ac_dc == "AC":
        print(f"     {ct}: {n} unit(s) — AC Level 2; serves AC-compatible vehicles")
    else:
        print(f"     {ct}: {n} unit(s) — DC {kw:.0f} kW; serves DC-only heavy trucks "
              f"and AC-compatible vehicles via DC port")

print(f"\n  9. COMPARISON WITH MANUAL MIX (10xL2 + 2xDC_50 + 1xDC_150):")
manual_cost = 10*10_000 + 2*50_000 + 1*150_000
print(f"     Manual mix cost  : ${manual_cost:,}")
print(f"     Optimal mix cost : ${best_cost:,}")
print(f"     Savings          : ${manual_cost - best_cost:,}")
print(f"     Manual mix L2 utilization was 0% — L2 chargers unused for this fleet")

print(f"\n  10. EXCLUDED INFEASIBLE EVENTS (not used in sizing):")
for _, r in excl_df.iterrows():
    print(f"     {r['charging_event_id']}")
    print(f"       Model       : {r['ev_equivalent_model']}")
    print(f"       Dwell       : {r['dwell_hours']:.3f} h  ({r['dwell_hours']*60:.0f} min)")
    print(f"       Energy need : {r['energy_needed_kwh_for_visit']:.2f} kWh")
    print(f"       Max possible: {r['max_possible_energy_kwh']:.2f} kWh  (at 350 kW DC, eta=0.90)")
    print(f"       Shortfall   : {r['shortfall_kwh']:.2f} kWh")
    print(f"       Reason      : {r['exclusion_reason']}")

print(f"""
  NOTE ON TOTAL DAY DEMAND:
    Original day total (all 28 events)   : 473.77 kWh
    Serviceable subset                   : {total_svc:.2f} kWh
    Excluded (infeasible)                : {excl_total_kwh:.2f} kWh
    Sum check                            : {total_svc + excl_total_kwh:.2f} kWh

    The selected charger mix satisfies {total_svc:.2f} kWh of serviceable demand.
    The {excl_total_kwh:.2f} kWh from excluded events cannot be served without
    longer vehicle dwell times (operational constraint, not a charger sizing issue).
""")

print("=" * 70)
print("  OPTIMIZATION COMPLETE")
print("=" * 70)
