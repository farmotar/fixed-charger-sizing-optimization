"""
exact_northgate_charger_sizing_milp.py
=======================================
Exact MILP-based EV charging infrastructure optimizer for the
Caltrans Northgate Maintenance Station.

Replaces the previous greedy charger-sizing method with a global
Mixed-Integer Linear Program that simultaneously selects the number
of chargers of each type AND optimizes the charging schedule for
every vehicle event, minimizing total daily cost.

Primary solver  : Gurobi  (gurobipy)
Fallback solver : Pyomo + HiGHS

Mathematical model overview
---------------------------
Sets    : V (events), T (15-min time steps), C (charger types)
Variables:
  N_c        - integer, number of installed chargers of type c
  x[v,t,c]   - continuous, kW delivered to event v at step t by charger c
  u[v,t,c]   - binary,     1 if event v is plugged into charger c at step t
  P_total[t] - continuous, total site power at step t
  P_max      - continuous, global peak demand
  P_peak_win - continuous, peak demand during 17:00-20:00
  delta[t]   - continuous, |P_total[t] - P_total[t-1]| (linearised)

Objective: minimise
  sum_c N_c*C_daily_c  (annualised charger ownership)
  + P_max * 6.45      (SMUD site infrastructure demand charge)
  + P_peak_win * 9.96 (SMUD summer peak-window demand charge)
  + lambda_ * sum_t delta[t]  (power-smoothing penalty)
  + C_energy * sum_t P_total[t]*dt  (energy cost)

NOTE on demand charges: demand charges are normally billed monthly.
This script applies them to the representative-day peak as a planning
proxy.  Monthly billing may require selecting the worst-case day or
scaling appropriately.
"""

from __future__ import annotations

import math
import sys
import time
import warnings
from pathlib import Path
from typing import Dict, List, Tuple

# Caltrans Q3 FY2025/26 cost set (replaces build_charger_specs defaults)
sys.path.insert(0, str(Path(__file__).parent))
from charger_costs_caltrans import build_charger_specs_caltrans

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")          # non-interactive backend; safe on all platforms
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

# ============================================================
# TOP-LEVEL CONFIGURATION  (edit here before running)
# ============================================================

# --- Paths ---
INPUT_PATH_PRIMARY   = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\z2z_milp_events_northgate_2025_06_30.csv")
INPUT_PATH_FALLBACK  = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\northgate_representative_day_method_c_visit_level_charging_events.csv")
OUTPUT_DIR           = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\exact_milp_outputs_min1h_exact")

# --- Time discretisation ---
DT_MINUTES = 5              # slot length in minutes; 15 = coarse/fast, 5 = fine/practical, 1 = very fine/slow
DT_HOURS   = DT_MINUTES / 60.0
ETA        = 0.90           # charging efficiency (grid -> battery)

# --- Energy tolerance ---
# Maximum fraction of extra energy allowed above actual need.
# With fine time steps (1 min), the optimizer naturally stays within this.
# With coarse steps (15 min) some vehicles may slightly exceed if no charger fits exactly.
OVERCHARGE_FRAC = 0.0       # 0 % - deliver exactly the minimum slots to cover energy need

# --- Utility / cost parameters (SMUD C&I Secondary 21-299 kW TOD) ---
C_DEMAND_GLOBAL      = 6.454   # $/kW  - SMUD site infrastructure demand charge
C_DEMAND_PEAK_WIN    = 9.960   # $/kW  - SMUD summer peak demand charge
PEAK_WIN_START_H     = 16.0    # 4:00 p.m. local time (America/Los_Angeles)
PEAK_WIN_END_H       = 21.0    # 9:00 p.m. local time
LAMBDA_SMOOTH        = 2.5     # $/kW  - power-smoothing penalty (0 = disabled)
LAMBDA_ENERGY_ERROR  = 1.0     # $/kWh - penalty on energy overdelivered above each vehicle's need

# SMUD C&I Secondary 21-299kW time-of-day energy rates ($/kWh)
C_ENERGY_SUMMER_PEAK        = 0.2341   # weekdays 4-9 p.m., summer (Jun-Sep)
C_ENERGY_SUMMER_OFFPEAK     = 0.1215   # all other summer hours
C_ENERGY_NONSUMMER_PEAK     = 0.1477   # weekdays 4-9 p.m., non-summer
C_ENERGY_NONSUMMER_OFFSAVER = 0.0888   # every day 9 a.m.-4 p.m., non-summer
C_ENERGY_NONSUMMER_OFFPEAK  = 0.1264   # all other non-summer hours
SMUD_TZ                     = "America/Los_Angeles"

# --- Gurobi solver parameters ---
GUROBI_TIME_LIMIT    = 3600    # seconds
GUROBI_MIP_GAP       = 0.05   # 5 % optimality gap (constant-power model is harder to solve)
GUROBI_THREADS       = 0       # 0 = use all available cores
GUROBI_OUTPUT_FLAG   = 1

# --- Charger count upper bounds (keep the search space manageable) ---
CHARGER_UPPER_BOUNDS = {
    "L2_19p2kW":  30,
    "DC_50kW":    20,
    "DC_150kW":   15,
    "DC_350kW":   20,
}

# --- Sensitivity analysis ---
RUN_SENSITIVITY        = False
LAMBDA_SMOOTH_VALUES   = [0, 0.5, 1.5, 5, 10]

# --- Feasibility tolerance ---
ENERGY_TOL = 1e-3   # kWh - allowed shortfall in energy delivery check

# ============================================================
# CHARGER HARDWARE SPECS
# ============================================================

def build_charger_specs() -> Dict[str, dict]:
    """
    Return a dictionary of charger hardware specifications.

    Daily CapEx formula per charger unit:
        C_daily = [(purchase + install) / (life_years * 12) + annual_maint / 12] / 30.42

    This converts one-time and recurring costs to a daily equivalent.
    """
    raw = {
        "L2_19p2kW": {
            "ac_dc":          "AC",
            "power_kw":        19.2,
            "purchase_cost":   2_500,
            "install_cost":    5_000,
            "annual_maint":      500,
            "life_years":         10,
        },
        "DC_50kW": {
            "ac_dc":          "DC",
            "power_kw":        50.0,
            "purchase_cost":  30_000,
            "install_cost":   25_000,
            "annual_maint":    3_000,
            "life_years":          8,
        },
        "DC_150kW": {
            "ac_dc":          "DC",
            "power_kw":       150.0,
            "purchase_cost":  75_000,
            "install_cost":   50_000,
            "annual_maint":    7_500,
            "life_years":          8,
        },
        "DC_350kW": {
            "ac_dc":          "DC",
            "power_kw":       350.0,
            "purchase_cost": 140_000,
            "install_cost":   90_000,
            "annual_maint":   14_000,
            "life_years":          8,
        },
    }
    return raw


def compute_daily_capex(charger_specs: Dict[str, dict]) -> Dict[str, float]:
    """
    Compute and print daily CapEx ($/charger/day) for each charger type.

    Formula:
        C_daily = [(purchase + install) / (life_years * 12)
                   + (annual_maint + annual_warranty) / 12] / 30.42

    annual_warranty defaults to 0 for charger types that have no warranty field.
    """
    DAYS_PER_MONTH = 30.42
    daily = {}
    print("\n" + "=" * 70)
    print("CHARGER DAILY CapEx BREAKDOWN")
    print("=" * 70)
    for ctype, spec in charger_specs.items():
        monthly_capex  = (spec["purchase_cost"] + spec["install_cost"]) / (spec["life_years"] * 12)
        monthly_recur  = (spec["annual_maint"] + spec.get("annual_warranty", 0)) / 12
        c_daily        = (monthly_capex + monthly_recur) / DAYS_PER_MONTH
        daily[ctype]   = c_daily
        spec["daily_capex"] = c_daily          # store back for convenience
        w = spec.get("annual_warranty", 0)
        print(
            f"  {ctype:<18}  power={spec['power_kw']:>6.1f} kW  "
            f"purchase=${spec['purchase_cost']:>8,}  install=${spec['install_cost']:>6,}  "
            f"maint=${spec['annual_maint']:>6,}/yr  warranty=${w:>6,}/yr  "
            f"life={spec['life_years']}yr  "
            f"-> ${c_daily:.4f}/charger/day"
        )
    print("=" * 70 + "\n")
    return daily


# ============================================================
# DATA LOADING AND CLEANING
# ============================================================

def load_events_data(input_path: Path | None = None) -> pd.DataFrame:
    """
    Load charging events from CSV.

    Tries INPUT_PATH_PRIMARY first, then INPUT_PATH_FALLBACK.
    If input_path is explicitly given, use that instead.
    """
    if input_path is not None:
        paths = [Path(input_path)]
    else:
        paths = [INPUT_PATH_PRIMARY, INPUT_PATH_FALLBACK]

    for p in paths:
        if p.exists():
            print(f"Loading events from: {p}")
            df = pd.read_csv(p)
            print(f"  -> {len(df):,} rows loaded.")
            return df

    tried = "\n  ".join(str(p) for p in paths)
    raise FileNotFoundError(
        f"No input CSV found. Tried:\n  {tried}\n"
        "Please supply a valid path or place the file at one of these locations."
    )


def clean_events_df(events_df: pd.DataFrame) -> pd.DataFrame:
    """
    Parse, validate, and clean a raw events DataFrame.

    Steps:
    1.  Parse arrival_time / departure_time as timezone-aware timestamps.
    2.  Remove rows with missing or invalid timestamps / energy.
    3.  Fill missing charge-power columns with 0.
    4.  Synthesise ID columns if absent.
    5.  Sort by arrival_time.
    """
    df = events_df.copy()
    original_len = len(df)

    # --- Parse timestamps ---
    for col in ["arrival_time", "departure_time"]:
        if col not in df.columns:
            raise KeyError(f"Required column '{col}' not found in events data.")
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")

    # --- Drop rows with missing timestamps ---
    missing_arr = df["arrival_time"].isna()
    missing_dep = df["departure_time"].isna()
    if missing_arr.any() or missing_dep.any():
        n = (missing_arr | missing_dep).sum()
        warnings.warn(f"Dropping {n} row(s) with missing arrival or departure time.")
        df = df[~(missing_arr | missing_dep)].copy()

    # --- Drop rows where departure <= arrival ---
    bad_window = df["departure_time"] <= df["arrival_time"]
    if bad_window.any():
        warnings.warn(f"Dropping {bad_window.sum()} row(s) where departure_time <= arrival_time.")
        df = df[~bad_window].copy()

    # --- Energy column ---
    if "energy_needed_kwh_for_visit" not in df.columns:
        raise KeyError("Required column 'energy_needed_kwh_for_visit' not found.")
    df["energy_needed_kwh_for_visit"] = pd.to_numeric(
        df["energy_needed_kwh_for_visit"], errors="coerce"
    )
    bad_energy = df["energy_needed_kwh_for_visit"].isna() | (df["energy_needed_kwh_for_visit"] <= 0)
    if bad_energy.any():
        warnings.warn(f"Dropping {bad_energy.sum()} row(s) with missing/zero energy requirement.")
        df = df[~bad_energy].copy()

    # --- Charge-power columns ---
    for col in ["max_ac_charge_kw", "max_dc_charge_kw"]:
        if col not in df.columns:
            warnings.warn(f"Column '{col}' not found; filling with 0.")
            df[col] = 0.0
        else:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    # --- Synthesise ID columns if absent ---
    if "charging_event_id" not in df.columns:
        df["charging_event_id"] = [f"event_{i+1:04d}" for i in range(len(df))]
    if "vehicle_id" not in df.columns:
        df["vehicle_id"] = [f"vehicle_{i+1:04d}" for i in range(len(df))]
    if "ev_equivalent_model" not in df.columns:
        df["ev_equivalent_model"] = "unknown"

    # --- Compute dwell_hours if missing ---
    if "dwell_hours" not in df.columns:
        df["dwell_hours"] = (
            (df["departure_time"] - df["arrival_time"]).dt.total_seconds() / 3600
        )

    df = df.sort_values("arrival_time").reset_index(drop=True)

    dropped = original_len - len(df)
    print(f"Data cleaning: {original_len} -> {len(df)} events  ({dropped} dropped).")
    return df


# ============================================================
# TIME GRID
# ============================================================

def build_time_grid(
    events_df: pd.DataFrame,
    dt_hours: float = DT_HOURS,
) -> List[pd.Timestamp]:
    """
    Build a list of UTC timestamps representing 15-minute time-step starts.

    The grid spans from the floor-15min of the earliest arrival to the
    ceil-15min of the latest departure.  Each element t_k represents the
    half-open interval [t_k, t_k + dt).

    Approximation note: a vehicle is considered available at step t if
    [t, t+dt) overlaps [arrival, departure).  We treat this as a full
    15-minute step even if the overlap is partial.
    """
    dt_ns   = int(dt_hours * 3600 * 1e9)     # nanoseconds per step
    t_min   = events_df["arrival_time"].min()
    t_max   = events_df["departure_time"].max()

    # Floor to nearest dt boundary
    t_start = t_min.floor(f"{int(dt_hours*60)}min")
    # Ceil to nearest dt boundary
    t_end   = t_max.ceil(f"{int(dt_hours*60)}min")

    steps = pd.date_range(start=t_start, end=t_end - pd.Timedelta(nanoseconds=dt_ns),
                          freq=f"{int(dt_hours*60)}min", tz="UTC")
    return list(steps)


def smud_energy_rates(time_grid: list) -> list:
    """Return per-step SMUD C&I 21-299kW energy rate ($/kWh) for each UTC timestamp."""
    rates = []
    for t in time_grid:
        t_loc      = t.tz_convert(SMUD_TZ)
        hour       = t_loc.hour + t_loc.minute / 60
        is_summer  = t_loc.month in (6, 7, 8, 9)
        is_weekday = t_loc.weekday() < 5
        is_peak_hr = 16.0 <= hour < 21.0
        if is_summer:
            rate = C_ENERGY_SUMMER_PEAK if (is_weekday and is_peak_hr) else C_ENERGY_SUMMER_OFFPEAK
        elif is_weekday and is_peak_hr:
            rate = C_ENERGY_NONSUMMER_PEAK
        elif 9.0 <= hour < 16.0:
            rate = C_ENERGY_NONSUMMER_OFFSAVER
        else:
            rate = C_ENERGY_NONSUMMER_OFFPEAK
        rates.append(rate)
    return rates


# ============================================================
# EFFECTIVE POWER AND FEASIBLE KEYS
# ============================================================

def compute_effective_power(
    events_df: pd.DataFrame,
    charger_specs: Dict[str, dict],
) -> Dict[Tuple[str, str], float]:
    """
    Compute P_eff[v, c] = effective charging power (kW) for every
    (event_id, charger_type) pair.

    If charger c is AC:  P_eff = min(P_charger_c, max_ac_charge_kw_v)
    If charger c is DC:  P_eff = min(P_charger_c, max_dc_charge_kw_v)
    If P_eff <= 0, the vehicle is incompatible with that charger type.
    """
    P_eff: Dict[Tuple[str, str], float] = {}
    for _, row in events_df.iterrows():
        v = row["charging_event_id"]
        mac = float(row["max_ac_charge_kw"])
        mdc = float(row["max_dc_charge_kw"])
        for ctype, spec in charger_specs.items():
            if spec["ac_dc"] == "AC":
                peff = 0.0 if mac <= 0 else min(spec["power_kw"], mac)
            else:
                peff = 0.0 if mdc <= 0 else min(spec["power_kw"], mdc)
            if peff > 0:
                P_eff[(v, ctype)] = peff
    return P_eff


def build_feasible_keys(
    events_df: pd.DataFrame,
    time_grid: List[pd.Timestamp],
    charger_specs: Dict[str, dict],
    P_eff: Dict[Tuple[str, str], float],
    dt_hours: float = DT_HOURS,
) -> Tuple[List[Tuple], Dict, Dict, Dict, Dict[str, List]]:
    """
    Build the list of feasible (v, t_idx, c) index triples for x and u variables.

    Only creates a variable when:
      1. Vehicle v is present (arrival <= t < departure) at time step t.
      2. P_eff[v, c] > 0  (vehicle is compatible with charger type c).

    Also returns pre-computed lookup dictionaries to avoid repeated
    DataFrame filtering inside constraint loops.

    Returns
    -------
    feasible_keys   : list of (v, t_idx, c)
    E               : dict  v  -> required energy kWh
    arrival_idx     : dict  v  -> index of first available time step
    departure_idx   : dict  v  -> index of first step AFTER departure
    available_times : dict  v  -> sorted list of t_idx values where v is present
    """
    dt_td = pd.Timedelta(hours=dt_hours)
    n_steps = len(time_grid)

    # Build per-event lookup tables from DataFrame (one pass, no repeated filtering)
    E:              Dict[str, float]      = {}
    arrival_map:    Dict[str, pd.Timestamp] = {}
    departure_map:  Dict[str, pd.Timestamp] = {}

    for _, row in events_df.iterrows():
        v = row["charging_event_id"]
        E[v]           = float(row["energy_needed_kwh_for_visit"])
        arrival_map[v]  = row["arrival_time"]
        departure_map[v] = row["departure_time"]

    # Convert timestamps to step indices for fast lookup
    step_series = pd.Series(time_grid)

    available_times: Dict[str, List[int]] = {}
    for v in E:
        arr = arrival_map[v]
        dep = departure_map[v]
        # Only include slots that are fully within [arr, dep):
        #   arrival  -> round UP   to nearest slot boundary (ceiling)
        #   departure-> slot must end at or before departure (t + dt <= dep)
        # This ensures no partial-slot energy overcounting at either end.
        freq_str = f"{int(dt_hours * 60)}min"
        arr_ceil = arr.ceil(freq_str)
        avail = [
            tidx for tidx, t in enumerate(time_grid)
            if t >= arr_ceil and (t + dt_td) <= dep
        ]
        available_times[v] = avail

    # Build feasible keys
    feasible_keys: List[Tuple[str, int, str]] = []
    for v, tidxs in available_times.items():
        for tidx in tidxs:
            for ctype in charger_specs:
                if (v, ctype) in P_eff:
                    feasible_keys.append((v, tidx, ctype))

    print(f"  Feasible (v,t,c) triples : {len(feasible_keys):,}")
    return feasible_keys, E, arrival_map, departure_map, available_times


# ============================================================
# ENERGY UPPER BOUNDS
# ============================================================

def _compute_E_max(events_df: pd.DataFrame, E: Dict[str, float]) -> Dict[str, float]:
    """
    Compute E_max[v] = maximum energy [kWh] the battery of event v can physically
    accept, i.e., the available battery room at arrival.

    Formula (when SOC data is present):
        E_max[v] = (SOC_target_v - SOC_arr_v) / 100 * battery_capacity_kwh_v

    This prevents the optimizer from scheduling more charging than the battery
    can physically store, which is not prevented by the lower-bound constraint A
    alone.  The constraint pair becomes:

        E[v]  <=  sum_t sum_c x[v,t,c] * dt * eta  <=  E_max[v]

    Falls back to float('inf') (no upper constraint added) when battery
    capacity or SOC columns are absent or NaN.

    Guarantees E_max[v] >= E[v] to avoid creating an infeasible lower > upper
    bound pair.
    """
    E_max: Dict[str, float] = {}
    needed_cols = {"battery_capacity_kwh", "assumed_initial_soc_percent", "target_soc_percent"}
    has_cols = needed_cols.issubset(events_df.columns)

    for _, row in events_df.iterrows():
        v = row["charging_event_id"]
        if has_cols:
            try:
                battery = float(row["battery_capacity_kwh"])
                soc_arr = float(row["assumed_initial_soc_percent"])
                soc_tgt = float(row["target_soc_percent"])
                if all(not math.isnan(x) for x in [battery, soc_arr, soc_tgt]):
                    emax = max(0.0, (soc_tgt - soc_arr) / 100.0 * battery)
                    E_max[v] = max(emax, E.get(v, 0.0))
                    continue
            except (ValueError, TypeError):
                pass
        E_max[v] = float("inf")

    finite = sum(1 for v in E_max if E_max[v] < 1e9)
    print(f"  Energy upper bounds: {finite}/{len(E_max)} events capped at battery room (SOC-based)")
    return E_max


# ============================================================
# GUROBI SOLVER
# ============================================================

def solve_with_gurobi(
    events_df:      pd.DataFrame,
    time_grid:      List[pd.Timestamp],
    charger_specs:  Dict[str, dict],
    daily_capex:    Dict[str, float],
    P_eff:          Dict[Tuple[str, str], float],
    feasible_keys:  List[Tuple],
    E:              Dict[str, float],
    available_times: Dict[str, List[int]],
    dt_hours:       float = DT_HOURS,
    eta:            float = ETA,
    c_demand_global:  float = C_DEMAND_GLOBAL,
    c_demand_peak_win: float = C_DEMAND_PEAK_WIN,
    lambda_smooth:       float = LAMBDA_SMOOTH,
    lambda_energy_error: float = LAMBDA_ENERGY_ERROR,
    # c_energy: removed — rates computed per time step via smud_energy_rates()
) -> dict:
    """
    Formulate and solve the charger-sizing MIQP with Gurobi.

    This is an EXACT global optimisation model.  Unlike the previous
    greedy method (which assigned chargers step-by-step using priority
    rules), the MILP/MIQP solver searches the entire feasible space and
    minimises total daily cost subject to all constraints simultaneously.

    x[v,t,c]  - charging power in kW, NOT energy.
                 Energy = x[v,t,c] * dt * eta.
    u[v,t,c]  - binary plug-in assignment; enforces charger capacity and
                 caps power at P_eff[v,c].

    Smoothing (L2 / RMSD^2):
        The old L1 total-variation penalty (sum |DeltaP_t|) has been replaced
        with an L2 penalty: (lambda / n_diff) * sum_t (P_total[t] - P_total[t-1])^2.
        This equals lambda * RMSD^2 where RMSD is the root-mean-square of the
        15-minute power differences.  The quadratic term makes this a Mixed-Integer
        Quadratic Program (MIQP); Gurobi solves MIQP natively.

    Energy bounds (A_lower / A_upper):
        Each vehicle must receive >= E[v] kWh (lower bound, ensuring trip readiness)
        AND <= E_max[v] kWh (upper bound, ensuring we do not exceed battery room).
        E_max[v] = (SOC_target - SOC_arr) / 100 * battery_capacity_kwh.
    """
    import gurobipy as gp
    from gurobipy import GRB

    charger_types = list(charger_specs.keys())
    n_events      = len(E)
    n_steps       = len(time_grid)
    dt            = dt_hours

    # --- Peak window step indices (4-9 p.m. local time) ---
    peak_steps = [
        tidx for tidx, t in enumerate(time_grid)
        if PEAK_WIN_START_H <= (
            t.tz_convert(SMUD_TZ).hour + t.tz_convert(SMUD_TZ).minute / 60
        ) < PEAK_WIN_END_H
    ]
    non_first_steps = list(range(1, n_steps))

    # --- Pre-compute ordered available slots per (v,c) for contiguity constraints ---
    feasible_keys_set = set(feasible_keys)
    avail_by_vc: Dict[Tuple[str, str], List[int]] = {}
    for v in available_times:
        for c in charger_specs:
            slots = [tidx for tidx in available_times[v] if (v, tidx, c) in feasible_keys_set]
            if slots:
                avail_by_vc[(v, c)] = slots

    # Build (v,c,i) stop-indicator keys for consecutive slot pairs
    s_keys_gurobi = []
    for (v, c), slots in avail_by_vc.items():
        for i in range(len(slots) - 1):
            s_keys_gurobi.append((v, c, i))

    # --- Print model dimensions ---
    vc_pairs_set = list({(v, c) for (v, _, c) in feasible_keys})
    n_p_vars     = sum(len(sl) for sl in avail_by_vc.values())  # p_start variables
    n_binary     = len(feasible_keys) + len(vc_pairs_set) + len(s_keys_gurobi) + n_p_vars  # u + z + s(compat) + p
    # No x variables in constant-power model; P_total + P_max + P_peak_win only.
    n_continuous = n_steps + 2
    n_integer    = len(charger_types)
    print("\n" + "=" * 60)
    print("GUROBI MIQP MODEL DIMENSIONS")
    print("=" * 60)
    print(f"  Vehicle events          : {n_events}")
    print(f"  Time steps              : {n_steps}")
    print(f"  Charger types           : {len(charger_types)}")
    print(f"  Feasible (v,t,c) keys   : {len(feasible_keys):,}")
    print(f"  Binary variables u      : {len(feasible_keys):,}")
    print(f"  Binary variables z      : {len(vc_pairs_set):,}  (charger exclusivity)")
    print(f"  Binary variables p      : {n_p_vars:,}  (contiguous charging - session-start)")
    print(f"  Integer variables N     : {n_integer}")
    print(f"  Continuous variables    : {n_continuous:,}")
    print(f"  Peak window steps       : {len(peak_steps)}")
    print("=" * 60)

    # --- Build model ---
    model = gp.Model("northgate_charger_sizing_milp")
    model.Params.TimeLimit   = GUROBI_TIME_LIMIT
    model.Params.MIPGap      = GUROBI_MIP_GAP
    model.Params.Threads     = GUROBI_THREADS
    model.Params.OutputFlag  = GUROBI_OUTPUT_FLAG

    # ---- Decision variables ----

    # N_c: number of installed chargers of type c
    N = model.addVars(
        charger_types,
        vtype=GRB.INTEGER,
        lb=0,
        ub={c: CHARGER_UPPER_BOUNDS[c] for c in charger_types},
        name="N_chargers",
    )

    # Constant-power model: no x variable.
    # When u[v,t,c] = 1, power delivered is exactly P_eff[(v,c)] = min(P_charger, P_vehicle).
    # The optimizer controls only WHEN each vehicle is plugged in, not at what rate.

    # u[v,t,c]: binary plug-in variable
    u = model.addVars(
        feasible_keys,
        vtype=GRB.BINARY,
        name="u_plugged",
    )

    # z[v,c]: binary charger-assignment variable — 1 if vehicle v uses charger type c
    #         for any part of its session (enforces no mid-session charger switch).
    vc_pairs = list({(v, c) for (v, _, c) in feasible_keys})
    z = model.addVars(vc_pairs, vtype=GRB.BINARY, name="z_charger_assign")

    # P_total[t]: total site charging power at step t
    P_total = model.addVars(
        range(n_steps),
        lb=0.0,
        vtype=GRB.CONTINUOUS,
        name="P_total_kw",
    )

    # P_max: global peak demand over the full horizon
    P_max = model.addVar(lb=0.0, vtype=GRB.CONTINUOUS, name="P_max_kw")

    # P_peak_win: peak demand specifically during 17:00-20:00
    P_peak_win = model.addVar(lb=0.0, vtype=GRB.CONTINUOUS, name="P_peak_window_kw")

    model.update()

    # ---- Build index structures for efficient constraint generation ----

    # Group feasible keys by (v) -> list of (t, c)
    keys_by_v: Dict[str, List[Tuple[int, str]]] = {v: [] for v in E}
    # Group feasible keys by (t, c) -> list of v
    keys_by_tc: Dict[Tuple[int, str], List[str]] = {}

    for (v, tidx, c) in feasible_keys:
        keys_by_v[v].append((tidx, c))
        key_tc = (tidx, c)
        if key_tc not in keys_by_tc:
            keys_by_tc[key_tc] = []
        keys_by_tc[key_tc].append(v)

    # Group by (v, t) -> list of c  (for single-plug constraint)
    keys_by_vt: Dict[Tuple[str, int], List[str]] = {}
    for (v, tidx, c) in feasible_keys:
        key_vt = (v, tidx)
        if key_vt not in keys_by_vt:
            keys_by_vt[key_vt] = []
        keys_by_vt[key_vt].append(c)

    # ---- Compute energy upper bounds (battery room per event) ----
    E_max = _compute_E_max(events_df, E)

    # Pre-compute per-vehicle-per-charger available slot counts (done once, reused below).
    import math as _math
    fk_set_gurobi = set(feasible_keys)
    per_charger_max: Dict[str, Dict[str, float]] = {}   # v -> {c -> max_achievable_kWh}
    for v in E:
        per_charger_max[v] = {}
        for c in charger_specs:
            if (v, c) in P_eff and P_eff[(v, c)] > 0:
                n_slots = sum(1 for tidx in available_times.get(v, [])
                              if (v, tidx, c) in fk_set_gurobi)
                per_charger_max[v][c] = n_slots * P_eff[(v, c)] * dt * eta

    # Maximum achievable energy across all charger types (for energy lower bound cap).
    max_achievable: Dict[str, float] = {}
    for v in E:
        best = max(per_charger_max[v].values()) if per_charger_max[v] else 0.0
        max_achievable[v] = best
        if best < E[v] - 1e-3:
            print(f"  [NOTE] {v}: max achievable {best:.2f} kWh < required {E[v]:.2f} kWh "
                  f"(short dwell); energy lower bound capped at {best:.2f} kWh.")

    # Tighten E_max: limit overcharge to min(OVERCHARGE_FRAC, minimum achievable overcharge).
    # min_ceil_del is the minimum delivery that covers E[v] with some charger that actually
    # has enough available slots.  This is used as the floor for the E_max cap, ensuring
    # the model is always feasible.
    for v in E:
        if not per_charger_max[v]:
            continue
        min_slot_e = min(P_eff[(v, c)] for c in per_charger_max[v]) * dt * eta

        # Chargers that have enough slots to cover E[v]
        serving_chargers = {c: P_eff[(v, c)] for c in per_charger_max[v]
                            if per_charger_max[v][c] >= E[v] - 1e-3}
        if serving_chargers:
            # Minimum sufficient delivery achievable with any serving charger
            min_ceil_del = min(
                _math.ceil(E[v] / (p * dt * eta)) * (p * dt * eta)
                for p in serving_chargers.values()
            )
        else:
            # No charger can reach E[v] — energy_lower already capped at max_achievable
            # Allow E_max = max_achievable so the upper bound doesn't further restrict
            min_ceil_del = max_achievable[v]

        target_cap = max(E[v] * (1.0 + OVERCHARGE_FRAC), min_ceil_del)
        E_max[v] = min(E_max[v], target_cap)
        # Floor: allow at least min_ceil_del so the model stays feasible when battery_room
        # exactly equals E[v] (nearly empty battery → no exact slot match possible).
        E_max[v] = max(E_max[v], min_ceil_del, min_slot_e)

    # ---- Constraints ----

    # A_lower. Energy lower bound: delivered energy >= min(E[v], max_achievable[v]).
    #    Uses max_achievable as a cap so that short-dwell vehicles (which can't
    #    physically receive E[v] kWh in discrete 15-min slots) do not make the
    #    model infeasible. The objective's energy-cost term will still push the
    #    optimizer to deliver as much energy as possible.
    model.addConstrs(
        (
            gp.quicksum(u[v, tidx, c] * P_eff[(v, c)] * dt * eta
                        for (tidx, c) in keys_by_v[v])
            >= min(E[v], max_achievable.get(v, E[v]))
            for v in E
            if keys_by_v[v]
        ),
        name="energy_lower_bound",
    )

    # A_upper. Energy upper bound: delivered energy <= E_max[v] (battery room).
    #    Prevents the RMSD-smoothing penalty from driving extra charging slots onto
    #    long-dwell vehicles as demand buffers.  Applied only where E_max is finite.
    model.addConstrs(
        (
            gp.quicksum(u[v, tidx, c] * P_eff[(v, c)] * dt * eta
                        for (tidx, c) in keys_by_v[v])
            <= E_max[v]
            for v in E
            if keys_by_v[v] and E_max[v] < 1e9
        ),
        name="energy_upper_bound",
    )

    # C. Single-plug rule: at most one charger type active per vehicle per step
    # Iterate over hashable (v, tidx) keys only; look up ctypes list inside.
    model.addConstrs(
        (
            gp.quicksum(u[v, tidx, c] for c in keys_by_vt[(v, tidx)]) <= 1
            for (v, tidx) in keys_by_vt
        ),
        name="single_plug",
    )

    # C2. Charger exclusivity: once a vehicle is plugged in it cannot switch charger types.
    #     Link u -> z: if plugged at (v,t,c) then z[v,c] must be 1.
    model.addConstrs(
        (u[v, tidx, c] <= z[v, c] for (v, tidx, c) in feasible_keys),
        name="charger_assign_link",
    )
    #     Each vehicle may commit to at most one charger type across its whole session.
    keys_by_v_c = {}
    for (v, c) in vc_pairs:
        keys_by_v_c.setdefault(v, []).append(c)
    model.addConstrs(
        (
            gp.quicksum(z[v, c] for c in keys_by_v_c[v]) <= 1
            for v in keys_by_v_c
            if len(keys_by_v_c[v]) > 1
        ),
        name="charger_exclusivity",
    )

    # C3. Contiguous charging: the set of time slots where u[v,t,c]=1 must be a
    #     single uninterrupted block for each (v,c) pair.  This prevents mid-session
    #     charger sharing and ensures a vehicle is never unplugged and re-plugged.
    #
    #     Formulation: introduce binary "session-start" indicator p[v,c,i] = 1 iff
    #     slot i is the FIRST slot where u=1 for this (v,c) pair:
    #
    #       p[v,c,0]  = u[v,slots[0],c]             (first slot is a start if plugged in)
    #       p[v,c,i] >= u[v,slots[i],c] - u[v,slots[i-1],c]   for i > 0
    #       p[v,c,i] <= u[v,slots[i],c]              (can only start if plugged in)
    #       p[v,c,i] <= 1 - u[v,slots[i-1],c]        (can only start if NOT plugged in before)
    #       sum_i p[v,c,i] <= 1                       (at most ONE start = one contiguous block)
    #
    #     A single start guarantees a single contiguous block (no restart after stopping).
    if s_keys_gurobi:
        p_keys = s_keys_gurobi + [(v, c, len(avail_by_vc[(v, c)]) - 1)
                                   for (v, c) in avail_by_vc if avail_by_vc[(v, c)]]
        # s_keys_gurobi already has entries for i in range(len(slots)-1), i.e. 0..N-2.
        # We need indices 0..N-1 for each (v,c). Build them fresh:
        p_keys_all = [
            (v, c, i)
            for (v, c), slots in avail_by_vc.items()
            for i in range(len(slots))
        ]
        p = model.addVars(p_keys_all, vtype=GRB.BINARY, name="p_start")
        model.update()

        for (v, c), slots in avail_by_vc.items():
            n_slots = len(slots)
            for i in range(n_slots):
                t_i = slots[i]
                # p[i] <= u[i]  (start only when plugged in)
                model.addConstr(p[v, c, i] <= u[v, t_i, c],
                                name=f"pstart_ub_u[{v},{c},{i}]")
                if i == 0:
                    # First slot: p[0] = u[0]  (if plugged in at first slot, that IS the start)
                    model.addConstr(p[v, c, 0] >= u[v, t_i, c],
                                    name=f"pstart_first[{v},{c}]")
                else:
                    t_prev = slots[i - 1]
                    # p[i] >= u[i] - u[i-1]  (detect 0->1 transition)
                    model.addConstr(p[v, c, i] >= u[v, t_i, c] - u[v, t_prev, c],
                                    name=f"pstart_detect[{v},{c},{i}]")
                    # p[i] <= 1 - u[i-1]  (only a start if prev slot was NOT plugged in)
                    model.addConstr(p[v, c, i] <= 1 - u[v, t_prev, c],
                                    name=f"pstart_gap[{v},{c},{i}]")

            # At most one start per (v,c): enforces single contiguous block
            model.addConstr(
                gp.quicksum(p[v, c, i] for i in range(n_slots)) <= 1,
                name=f"one_start[{v},{c}]",
            )

    # D. Charger capacity: active vehicles at (t, c) <= N_c installed
    # Iterate over hashable (tidx, c) keys only; look up vlist inside.
    model.addConstrs(
        (
            gp.quicksum(u[v, tidx, c] for v in keys_by_tc[(tidx, c)]) <= N[c]
            for (tidx, c) in keys_by_tc
        ),
        name="charger_capacity",
    )

    # E. Charging window: variables outside [arrival, departure) are simply not
    #    created (handled in build_feasible_keys), so no explicit constraint needed.

    # F. Site power definition: P_total[t] = sum_v sum_c u[v,t,c] * P_eff[(v,c)]
    #    With constant power, P_total is fully determined by the plug-in decisions.
    keys_by_t: Dict[int, List[Tuple[str, str]]] = {tidx: [] for tidx in range(n_steps)}
    for (v, tidx, c) in feasible_keys:
        keys_by_t[tidx].append((v, c))

    model.addConstrs(
        (
            P_total[tidx] == gp.quicksum(u[v, tidx, c] * P_eff[(v, c)]
                                         for (v, c) in keys_by_t[tidx])
            for tidx in range(n_steps)
        ),
        name="site_power_def",
    )

    # G. Global peak demand: P_max >= P_total[t] for all t
    model.addConstrs(
        (P_max >= P_total[tidx] for tidx in range(n_steps)),
        name="global_peak",
    )

    # H. Peak-window demand: P_peak_win >= P_total[t] for t in [17:00, 20:00)
    if peak_steps:
        model.addConstrs(
            (P_peak_win >= P_total[tidx] for tidx in peak_steps),
            name="peak_window_demand",
        )

    # ---- Objective function ----
    # Each component is computed and stored separately for reporting.
    c_energy_rates = smud_energy_rates(time_grid)   # per-step TOD rate ($/kWh)
    capex_expr    = gp.quicksum(N[c] * daily_capex[c] for c in charger_types)
    energy_expr   = gp.quicksum(
        P_total[tidx] * dt * c_energy_rates[tidx] for tidx in range(n_steps)
    )
    g_demand_expr = P_max * c_demand_global
    p_demand_expr = P_peak_win * c_demand_peak_win

    # One-sided (above-mean) variance smoothing: lambda * (1/N) * sum_t [max(0, P[t]-P_mean)]^2
    # Penalises only time steps where site power EXCEEDS the mean — shaves peaks without
    # penalising light-load slots.  Auxiliary variables:
    #   P_mean_var : mean site power,  N * P_mean_var = sum_t P[t]
    #   d_plus[t]  : positive deviation above mean, d_plus[t] >= P[t] - P_mean_var, >= 0
    # Minimisation drives d_plus[t] to exactly max(0, P[t]-P_mean_var).
    P_mean_var = model.addVar(lb=0.0, name="P_mean")
    model.addConstr(
        gp.quicksum(P_total[tidx] for tidx in range(n_steps)) == n_steps * P_mean_var,
        name="mean_power_def"
    )
    d_plus = model.addVars(range(n_steps), lb=0.0, name="d_plus")
    for tidx in range(n_steps):
        model.addConstr(d_plus[tidx] >= P_total[tidx] - P_mean_var, name=f"d_plus_lb_{tidx}")
    if n_steps > 0:
        smooth_expr = (lambda_smooth / n_steps) * gp.quicksum(
            d_plus[tidx] * d_plus[tidx]
            for tidx in range(n_steps)
        )
    else:
        smooth_expr = 0

    # Energy-error penalty: penalise kWh delivered above each vehicle's actual need.
    # E_del[v] = sum_{t,c} u[v,t,c] * P_eff * dt * eta  (energy stored in battery)
    # Penalty = lambda_energy_error * sum_v (E_del[v] - E[v])
    # The constant -sum_v E[v] shifts the value to "excess kWh" but doesn't affect optimisation.
    e_del_expr = gp.quicksum(
        u[v, tidx, c] * P_eff[(v, c)] * dt * eta
        for (v, tidx, c) in feasible_keys
    )
    e_need_total = sum(E[v] for v in E)
    energy_error_expr = lambda_energy_error * (e_del_expr - e_need_total)

    model.setObjective(
        capex_expr + energy_expr + g_demand_expr + p_demand_expr + smooth_expr + energy_error_expr,
        GRB.MINIMIZE,
    )

    # ---- Write model files ----
    lp_path  = OUTPUT_DIR / "exact_northgate_charger_sizing_milp.lp"
    mps_path = OUTPUT_DIR / "exact_northgate_charger_sizing_milp.mps"
    model.write(str(lp_path))
    model.write(str(mps_path))
    print(f"Model written: {lp_path.name}  and  {mps_path.name}")

    # ---- Solve ----
    print("\nStarting Gurobi optimisation...\n")
    t_solve_start = time.time()
    model.optimize()
    solve_time = time.time() - t_solve_start

    # ---- Handle infeasibility ----
    if model.Status == 3:   # GRB.INFEASIBLE
        print("\n[ERROR] Model is INFEASIBLE.")
        print("Computing Irreducible Inconsistent Subsystem (IIS)...")
        model.computeIIS()
        iis_path = OUTPUT_DIR / "exact_northgate_charger_sizing_milp.ilp"
        model.write(str(iis_path))
        print(f"IIS saved to: {iis_path}")
        print("Inspect the IIS file to identify conflicting constraints.")
        return {"status": "infeasible", "model": model, "solver": "gurobi"}

    if model.SolCount == 0:
        print(f"\n[WARNING] Solver stopped with no feasible solution found. Status={model.Status}")
        return {"status": "no_solution", "model": model, "solver": "gurobi"}

    # ---- Extract solution ----
    print(f"\nSolver finished.  Status={model.Status}  ObjVal={model.ObjVal:.4f}  "
          f"MIPGap={model.MIPGap:.4f}  Runtime={solve_time:.1f}s")

    N_vals       = {c: int(round(N[c].X)) for c in charger_types}
    P_total_vals = {tidx: P_total[tidx].X for tidx in range(n_steps)}
    P_max_val    = P_max.X
    P_peak_val   = P_peak_win.X

    # delta_vals computed from the optimised power profile (not optimization variables).
    # Reports absolute power differences for CSV output and validation.
    delta_vals = {tidx: abs(P_total_vals[tidx] - P_total_vals[tidx - 1])
                  for tidx in non_first_steps}

    # x values derived from u: when plugged in, power = P_eff[(v,c)] (constant).
    x_vals: Dict[Tuple[str, int, str], float] = {}
    for (v, tidx, c) in feasible_keys:
        if u[v, tidx, c].X > 0.5:
            x_vals[(v, tidx, c)] = P_eff[(v, c)]

    u_vals: Dict[Tuple[str, int, str], float] = {}
    for (v, tidx, c) in feasible_keys:
        val = u[v, tidx, c].X
        if val > 0.5:
            u_vals[(v, tidx, c)] = 1.0

    # Objective component values
    daily_capex_cost   = sum(N_vals[c] * daily_capex[c] for c in charger_types)
    energy_cost_val    = sum(P_total_vals[tidx] * dt * c_energy_rates[tidx] for tidx in range(n_steps))
    global_demand_cost = P_max_val * c_demand_global
    peak_window_cost   = P_peak_val * c_demand_peak_win
    # Smoothing cost = lambda * (1/N) * sum_t [max(0, P[t]-P_mean)]^2  (above-mean only)
    p_mean_val      = sum(P_total_vals[t] for t in range(n_steps)) / max(1, n_steps)
    above_sq_vals   = [max(0.0, P_total_vals[t] - p_mean_val) ** 2 for t in range(n_steps)]
    variance_above  = sum(above_sq_vals) / max(1, n_steps)
    rmsd_val        = math.sqrt(variance_above)   # RMS of above-mean deviations
    smoothing_cost  = lambda_smooth * variance_above
    # Energy-error cost: lambda * total excess kWh delivered above vehicle needs
    e_del_total        = sum(P_eff[(v, c)] * dt * eta
                            for (v, tidx, c) in feasible_keys if u_vals.get((v, tidx, c), 0) > 0.5)
    energy_error_kwh   = e_del_total - sum(E[v] for v in E)
    energy_error_cost  = lambda_energy_error * energy_error_kwh
    total_obj          = model.ObjVal

    return {
        "status":               "optimal" if model.Status == 2 else "suboptimal",
        "solver":               "gurobi",
        "model":                model,
        "N_vals":               N_vals,
        "P_total_vals":         P_total_vals,
        "P_max_val":            P_max_val,
        "P_peak_val":           P_peak_val,
        "delta_vals":           delta_vals,
        "x_vals":               x_vals,
        "u_vals":               u_vals,
        "daily_capex_cost":     daily_capex_cost,
        "energy_cost":          energy_cost_val,
        "global_demand_cost":   global_demand_cost,
        "peak_window_cost":     peak_window_cost,
        "smoothing_cost":       smoothing_cost,
        "energy_error_kwh":     energy_error_kwh,
        "energy_error_cost":    energy_error_cost,
        "rmsd_kw":              rmsd_val,
        "p_mean_kw":            p_mean_val,
        "total_objective_cost": total_obj,
        "solve_time":           solve_time,
        "mip_gap":              model.MIPGap,
        "obj_val":              model.ObjVal,
        "peak_steps":           peak_steps,
    }


# ============================================================
# PYOMO + HIGHS FALLBACK
# ============================================================

def solve_with_pyomo_highs(
    events_df:      pd.DataFrame,
    time_grid:      List[pd.Timestamp],
    charger_specs:  Dict[str, dict],
    daily_capex:    Dict[str, float],
    P_eff:          Dict[Tuple[str, str], float],
    feasible_keys:  List[Tuple],
    E:              Dict[str, float],
    available_times: Dict[str, List[int]],
    dt_hours:       float = DT_HOURS,
    eta:            float = ETA,
    c_demand_global:  float = C_DEMAND_GLOBAL,
    c_demand_peak_win: float = C_DEMAND_PEAK_WIN,
    lambda_smooth:       float = LAMBDA_SMOOTH,
    lambda_energy_error: float = LAMBDA_ENERGY_ERROR,
    # c_energy: removed — rates computed per time step via smud_energy_rates()
) -> dict:
    """
    Fallback solver: same mathematical model implemented in Pyomo + HiGHS.

    Preserves the same variables, objective, and constraints as the Gurobi
    implementation.  May be slower for large instances.
    """
    try:
        import pyomo.environ as pyo
    except ImportError:
        raise ImportError(
            "Pyomo is not installed.  Install with:\n"
            "  pip install pyomo\n"
            "Also install HiGHS:\n"
            "  pip install highspy\n"
            "or download the HiGHS binary and add it to PATH."
        )

    print("Building Pyomo model...")
    charger_types  = list(charger_specs.keys())
    n_steps        = len(time_grid)
    dt             = dt_hours
    c_energy_rates = smud_energy_rates(time_grid)   # per-step TOD rate ($/kWh)

    peak_steps    = set(
        tidx for tidx, t in enumerate(time_grid)
        if PEAK_WIN_START_H <= (
            t.tz_convert(SMUD_TZ).hour + t.tz_convert(SMUD_TZ).minute / 60
        ) < PEAK_WIN_END_H
    )
    non_first     = list(range(1, n_steps))

    # Build index helpers
    keys_by_v: Dict[str, List[Tuple[int, str]]] = {v: [] for v in E}
    keys_by_tc: Dict[Tuple[int, str], List[str]] = {}
    keys_by_vt: Dict[Tuple[str, int], List[str]] = {}
    keys_by_t:  Dict[int, List[Tuple[str, str]]] = {tidx: [] for tidx in range(n_steps)}

    for (v, tidx, c) in feasible_keys:
        keys_by_v[v].append((tidx, c))
        keys_by_tc.setdefault((tidx, c), []).append(v)
        keys_by_vt.setdefault((v, tidx), []).append(c)
        keys_by_t[tidx].append((v, c))

    m = pyo.ConcreteModel(name="northgate_charger_sizing_milp")

    # Sets
    m.V  = pyo.Set(initialize=list(E.keys()))
    m.T  = pyo.Set(initialize=list(range(n_steps)))
    m.C  = pyo.Set(initialize=charger_types)
    m.FK = pyo.Set(initialize=feasible_keys, dimen=3)

    # Variables (no delta variables -- smoothing is quadratic in the objective)
    m.N          = pyo.Var(m.C, domain=pyo.NonNegativeIntegers,
                           bounds=lambda m, c: (0, CHARGER_UPPER_BOUNDS[c]))
    # Constant-power model: no x variable; power = u * P_eff (derived).
    m.u          = pyo.Var(m.FK, domain=pyo.Binary)
    m.P_total    = pyo.Var(m.T, domain=pyo.NonNegativeReals)
    m.P_max      = pyo.Var(domain=pyo.NonNegativeReals)
    m.P_peak_win = pyo.Var(domain=pyo.NonNegativeReals)

    # z[v,c]: 1 if vehicle v uses charger type c for any step (charger exclusivity)
    vc_pairs_pyomo = list({(v, c) for (v, _, c) in feasible_keys})
    m.VC = pyo.Set(initialize=vc_pairs_pyomo, dimen=2)
    m.z  = pyo.Var(m.VC, domain=pyo.Binary)

    # Objective: MIQP with one-sided (above-mean) variance smoothing
    # = (lambda / N) * sum_t [max(0, P[t] - P_mean)]^2
    # Auxiliary Pyomo variables: P_mean_var and d_plus[t] per step.
    m.P_mean_var = pyo.Var(domain=pyo.NonNegativeReals)
    m.d_plus = pyo.Var(range(n_steps), domain=pyo.NonNegativeReals)

    def mean_def_rule(m):
        return sum(m.P_total[t] for t in range(n_steps)) == n_steps * m.P_mean_var
    m.mean_power_def = pyo.Constraint(rule=mean_def_rule)

    m.d_plus_lb = pyo.ConstraintList()
    for t in range(n_steps):
        m.d_plus_lb.add(m.d_plus[t] >= m.P_total[t] - m.P_mean_var)

    def obj_rule(m):
        capex  = sum(m.N[c] * daily_capex[c] for c in charger_types)
        energy = sum(m.P_total[t] * dt * c_energy_rates[t] for t in range(n_steps))
        gdem   = m.P_max * c_demand_global
        pdem   = m.P_peak_win * c_demand_peak_win
        n_s    = max(1, n_steps)
        smooth = (lambda_smooth / n_s) * sum(m.d_plus[t] ** 2 for t in range(n_steps))
        e_del  = sum(m.u[v, tidx, c] * P_eff[(v, c)] * dt * eta
                     for (v, tidx, c) in feasible_keys)
        energy_error = lambda_energy_error * (e_del - sum(E[v] for v in E))
        return capex + energy + gdem + pdem + smooth + energy_error
    m.obj = pyo.Objective(rule=obj_rule, sense=pyo.minimize)

    # Constraints
    # Compute energy upper and lower bounds (same logic as Gurobi path)
    import math as _math_pyomo
    E_max = _compute_E_max(events_df, E)
    fk_set_local = set(feasible_keys)

    per_charger_max_pyomo: Dict[str, Dict[str, float]] = {}
    for v in E:
        per_charger_max_pyomo[v] = {}
        for c in charger_specs:
            if (v, c) in P_eff and P_eff[(v, c)] > 0:
                n_slots = sum(1 for tidx in available_times.get(v, []) if (v, tidx, c) in fk_set_local)
                per_charger_max_pyomo[v][c] = n_slots * P_eff[(v, c)] * dt * eta

    max_achievable_pyomo: Dict[str, float] = {
        v: (max(per_charger_max_pyomo[v].values()) if per_charger_max_pyomo[v] else 0.0)
        for v in E
    }

    for v in E:
        if not per_charger_max_pyomo[v]:
            continue
        min_slot_e = min(P_eff[(v, c)] for c in per_charger_max_pyomo[v]) * dt * eta
        serving = {c: P_eff[(v, c)] for c in per_charger_max_pyomo[v]
                   if per_charger_max_pyomo[v][c] >= E[v] - 1e-3}
        if serving:
            min_ceil_del = min(
                _math_pyomo.ceil(E[v] / (p * dt * eta)) * (p * dt * eta)
                for p in serving.values()
            )
        else:
            min_ceil_del = max_achievable_pyomo[v]
        target_cap = max(E[v] * (1.0 + OVERCHARGE_FRAC), min_ceil_del)
        E_max[v] = min(E_max[v], target_cap)
        E_max[v] = max(E_max[v], min_ceil_del, min_slot_e)

    def energy_lower(m, v):
        terms = keys_by_v.get(v, [])
        if not terms:
            return pyo.Constraint.Skip
        lb = min(E[v], max_achievable_pyomo.get(v, E[v]))
        return sum(m.u[v, tidx, c] * P_eff[(v, c)] * dt * eta for (tidx, c) in terms) >= lb
    m.c_energy_lower = pyo.Constraint(m.V, rule=energy_lower)

    def energy_upper(m, v):
        terms = keys_by_v.get(v, [])
        if not terms or E_max.get(v, float("inf")) >= 1e9:
            return pyo.Constraint.Skip
        return sum(m.u[v, tidx, c] * P_eff[(v, c)] * dt * eta for (tidx, c) in terms) <= E_max[v]
    m.c_energy_upper = pyo.Constraint(m.V, rule=energy_upper)

    def single_plug(m, v, tidx):
        ctypes = keys_by_vt.get((v, tidx), [])
        if not ctypes:
            return pyo.Constraint.Skip
        return sum(m.u[v, tidx, c] for c in ctypes) <= 1
    vt_pairs = list(keys_by_vt.keys())
    m.VT = pyo.Set(initialize=vt_pairs, dimen=2)
    m.c_single_plug = pyo.Constraint(m.VT, rule=lambda m, v, tidx: single_plug(m, v, tidx))

    # Charger exclusivity: link u -> z, then restrict each vehicle to one charger type.
    def charger_assign_link(m, v, tidx, c):
        return m.u[v, tidx, c] <= m.z[v, c]
    m.c_charger_assign_link = pyo.Constraint(m.FK, rule=charger_assign_link)

    keys_by_v_c_pyomo = {}
    for (v, c) in vc_pairs_pyomo:
        keys_by_v_c_pyomo.setdefault(v, []).append(c)
    v_multi_pyomo = [v for v, ctypes in keys_by_v_c_pyomo.items() if len(ctypes) > 1]
    if v_multi_pyomo:
        m.V_multi = pyo.Set(initialize=v_multi_pyomo)
        def charger_excl(m, v):
            return sum(m.z[v, c] for c in keys_by_v_c_pyomo[v]) <= 1
        m.c_charger_excl = pyo.Constraint(m.V_multi, rule=charger_excl)

    # C3. Contiguous charging (Pyomo version) — at-most-one-start formulation.
    fk_set_pyomo = set(feasible_keys)
    avail_by_vc_pyomo: Dict[Tuple[str, str], List[int]] = {}
    for v in available_times:
        for c in charger_types:
            slots = [tidx for tidx in available_times[v] if (v, tidx, c) in fk_set_pyomo]
            if slots:
                avail_by_vc_pyomo[(v, c)] = slots

    p_keys_pyomo = [
        (v, c, i)
        for (v, c), slots in avail_by_vc_pyomo.items()
        for i in range(len(slots))
    ]
    if p_keys_pyomo:
        m.PK = pyo.Set(initialize=p_keys_pyomo, dimen=3)
        m.p  = pyo.Var(m.PK, domain=pyo.Binary)
        m.c_pstart = pyo.ConstraintList()

        for (v, c), slots in avail_by_vc_pyomo.items():
            n_sl = len(slots)
            for i in range(n_sl):
                t_i = slots[i]
                m.c_pstart.add(m.p[v, c, i] <= m.u[v, t_i, c])
                if i == 0:
                    m.c_pstart.add(m.p[v, c, 0] >= m.u[v, t_i, c])
                else:
                    t_prev = slots[i - 1]
                    m.c_pstart.add(m.p[v, c, i] >= m.u[v, t_i, c] - m.u[v, t_prev, c])
                    m.c_pstart.add(m.p[v, c, i] <= 1 - m.u[v, t_prev, c])
            m.c_pstart.add(
                sum(m.p[v, c, i] for i in range(n_sl)) <= 1
            )

    def charger_cap(m, tidx, c):
        vlist = keys_by_tc.get((tidx, c), [])
        if not vlist:
            return pyo.Constraint.Skip
        return sum(m.u[v, tidx, c] for v in vlist) <= m.N[c]
    tc_pairs = list(keys_by_tc.keys())
    m.TC = pyo.Set(initialize=tc_pairs, dimen=2)
    m.c_charger_cap = pyo.Constraint(m.TC, rule=lambda m, tidx, c: charger_cap(m, tidx, c))

    def site_power_def(m, tidx):
        vc_list = keys_by_t.get(tidx, [])
        if not vc_list:
            return m.P_total[tidx] == 0
        return m.P_total[tidx] == sum(m.u[v, tidx, c] * P_eff[(v, c)] for (v, c) in vc_list)
    m.c_site_power = pyo.Constraint(m.T, rule=site_power_def)

    def global_peak(m, tidx):
        return m.P_max >= m.P_total[tidx]
    m.c_global_peak = pyo.Constraint(m.T, rule=global_peak)

    def peak_win_demand(m, tidx):
        if tidx not in peak_steps:
            return pyo.Constraint.Skip
        return m.P_peak_win >= m.P_total[tidx]
    m.c_peak_win = pyo.Constraint(m.T, rule=peak_win_demand)

    # Solve
    print("Calling HiGHS solver via Pyomo...")
    t_start = time.time()
    solver_options = {"time_limit": GUROBI_TIME_LIMIT, "mip_rel_gap": GUROBI_MIP_GAP}
    try:
        solver = pyo.SolverFactory("appsi_highs")
        if not solver.available():
            solver = pyo.SolverFactory("highs")
        results = solver.solve(m, tee=True, options=solver_options)
    except Exception as exc:
        raise RuntimeError(
            f"HiGHS solver failed: {exc}\n"
            "Install HiGHS with:  pip install highspy\n"
            "or download from https://github.com/ERGO-Code/HiGHS/releases"
        )
    solve_time = time.time() - t_start

    term = results.solver.termination_condition
    if str(term) not in ("optimal", "feasible"):
        print(f"[WARNING] Pyomo/HiGHS termination: {term}")
        return {"status": str(term), "solver": "pyomo_highs"}

    # Extract solution
    N_vals       = {c: int(round(pyo.value(m.N[c]))) for c in charger_types}
    P_total_vals = {tidx: pyo.value(m.P_total[tidx]) for tidx in range(n_steps)}
    P_max_val    = pyo.value(m.P_max)
    P_peak_val   = pyo.value(m.P_peak_win)

    # Absolute power differences computed from the optimal P_total profile (for reporting)
    delta_vals = {tidx: abs(P_total_vals[tidx] - P_total_vals[tidx - 1])
                  for tidx in non_first}

    x_vals = {}
    for (v, tidx, c) in feasible_keys:
        if pyo.value(m.u[v, tidx, c]) and pyo.value(m.u[v, tidx, c]) > 0.5:
            x_vals[(v, tidx, c)] = P_eff[(v, c)]

    u_vals = {}
    for (v, tidx, c) in feasible_keys:
        val = pyo.value(m.u[v, tidx, c])
        if val and val > 0.5:
            u_vals[(v, tidx, c)] = 1.0

    daily_capex_cost   = sum(N_vals[c] * daily_capex[c] for c in charger_types)
    energy_cost_val    = sum(P_total_vals[tidx] * dt * c_energy_rates[tidx] for tidx in range(n_steps))
    global_demand_cost = P_max_val * c_demand_global
    peak_window_cost   = P_peak_val * c_demand_peak_win
    p_mean_val      = sum(P_total_vals[t] for t in range(n_steps)) / max(1, n_steps)
    above_sq_vals   = [max(0.0, P_total_vals[t] - p_mean_val) ** 2 for t in range(n_steps)]
    variance_above  = sum(above_sq_vals) / max(1, n_steps)
    rmsd_val        = math.sqrt(variance_above)
    smoothing_cost  = lambda_smooth * variance_above
    e_del_total        = sum(P_eff[(v, c)] * dt * eta
                            for (v, tidx, c) in feasible_keys if u_vals.get((v, tidx, c), 0) > 0.5)
    energy_error_kwh   = e_del_total - sum(E[v] for v in E)
    energy_error_cost  = lambda_energy_error * energy_error_kwh
    total_obj          = pyo.value(m.obj)

    return {
        "status":               "optimal",
        "solver":               "pyomo_highs",
        "N_vals":               N_vals,
        "P_total_vals":         P_total_vals,
        "P_max_val":            P_max_val,
        "P_peak_val":           P_peak_val,
        "delta_vals":           delta_vals,
        "x_vals":               x_vals,
        "u_vals":               u_vals,
        "daily_capex_cost":     daily_capex_cost,
        "energy_cost":          energy_cost_val,
        "global_demand_cost":   global_demand_cost,
        "peak_window_cost":     peak_window_cost,
        "smoothing_cost":       smoothing_cost,
        "energy_error_kwh":     energy_error_kwh,
        "energy_error_cost":    energy_error_cost,
        "rmsd_kw":              rmsd_val,
        "p_mean_kw":            p_mean_val,
        "total_objective_cost": total_obj,
        "solve_time":           solve_time,
        "mip_gap":              float("nan"),
        "obj_val":              total_obj,
        "peak_steps":           list(peak_steps),
    }


# ============================================================
# SOLUTION EXPORT
# ============================================================

def export_solution(
    sol:           dict,
    events_df:     pd.DataFrame,
    time_grid:     List[pd.Timestamp],
    charger_specs: Dict[str, dict],
    daily_capex:   Dict[str, float],
    P_eff:         Dict[Tuple[str, str], float],
    E:             Dict[str, float],
    feasible_keys: List[Tuple],
    dt_hours:      float = DT_HOURS,
    eta:           float = ETA,
) -> None:
    """
    Write all CSV output files to OUTPUT_DIR.
    """
    if sol.get("status") in ("infeasible", "no_solution"):
        print("No feasible solution to export.")
        return

    N_vals       = sol["N_vals"]
    P_total_vals = sol["P_total_vals"]
    P_max_val    = sol["P_max_val"]
    P_peak_val   = sol["P_peak_val"]
    delta_vals   = sol["delta_vals"]
    x_vals       = sol["x_vals"]
    peak_steps   = set(sol["peak_steps"])
    dt           = dt_hours
    n_steps      = len(time_grid)
    charger_types = list(charger_specs.keys())

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Build event lookup
    ev_lookup = events_df.set_index("charging_event_id")

    # ----------------------------------------------------------------
    # 1. Selected charger mix
    # ----------------------------------------------------------------
    rows = []
    for c in charger_types:
        spec = charger_specs[c]
        rows.append({
            "charger_type":       c,
            "ac_dc":              spec["ac_dc"],
            "power_kw":           spec["power_kw"],
            "count":              N_vals[c],
            "purchase_cost":      spec["purchase_cost"],
            "installation_cost":  spec["install_cost"],
            "annual_maintenance": spec["annual_maint"],
            "annual_warranty":    spec.get("annual_warranty", 0),
            "life_years":         spec["life_years"],
            "daily_capex_per_unit": daily_capex[c],
            "total_daily_capex":  N_vals[c] * daily_capex[c],
        })
    pd.DataFrame(rows).to_csv(OUTPUT_DIR / "exact_milp_selected_charger_mix.csv", index=False)
    print("Saved: exact_milp_selected_charger_mix.csv")

    # ----------------------------------------------------------------
    # 2. Per-event results
    # ----------------------------------------------------------------
    # Compute delivered energy per event from x_vals
    delivered: Dict[str, float] = {v: 0.0 for v in E}
    for (v, tidx, c), pwr in x_vals.items():
        delivered[v] = delivered.get(v, 0.0) + pwr * dt * eta

    ev_rows = []
    for _, row in events_df.iterrows():
        v   = row["charging_event_id"]
        req = E[v]
        dlv = delivered.get(v, 0.0)
        ev_rows.append({
            "charging_event_id":    v,
            "vehicle_id":           row.get("vehicle_id", ""),
            "ev_equivalent_model":  row.get("ev_equivalent_model", "unknown"),
            "arrival_time":         row["arrival_time"],
            "departure_time":       row["departure_time"],
            "required_energy_kwh":  req,
            "delivered_energy_kwh": dlv,
            "unmet_energy_kwh":     max(0.0, req - dlv),
            "served_binary":        int(dlv >= req - ENERGY_TOL),
            "max_ac_charge_kw":     row.get("max_ac_charge_kw", 0),
            "max_dc_charge_kw":     row.get("max_dc_charge_kw", 0),
        })
    pd.DataFrame(ev_rows).to_csv(OUTPUT_DIR / "exact_milp_event_results.csv", index=False)
    print("Saved: exact_milp_event_results.csv")

    # ----------------------------------------------------------------
    # 3. Charging schedule (non-trivial x rows only)
    # ----------------------------------------------------------------
    sched_rows = []
    for (v, tidx, c), pwr in x_vals.items():
        t_start = time_grid[tidx]
        t_end   = t_start + pd.Timedelta(hours=dt)
        peff    = P_eff.get((v, c), 0.0)
        sched_rows.append({
            "time_step_start":          t_start,
            "time_step_end":            t_end,
            "charging_event_id":        v,
            "vehicle_id":               ev_lookup.at[v, "vehicle_id"] if v in ev_lookup.index else "",
            "charger_type":             c,
            "power_kw":                 pwr,
            "energy_delivered_kwh":     pwr * dt * eta,
            "effective_power_limit_kw": peff,
        })
    pd.DataFrame(sched_rows).sort_values(
        ["time_step_start", "charging_event_id"]
    ).to_csv(OUTPUT_DIR / "exact_milp_charging_schedule.csv", index=False)
    print("Saved: exact_milp_charging_schedule.csv")

    # ----------------------------------------------------------------
    # 4. Site power profile
    # ----------------------------------------------------------------
    prof_rows = []
    for tidx in range(n_steps):
        t_start = time_grid[tidx]
        t_end   = t_start + pd.Timedelta(hours=dt)
        ptot    = P_total_vals[tidx]
        delt    = delta_vals.get(tidx, 0.0)
        hour_f  = t_start.hour + t_start.minute / 60.0
        in_peak = tidx in peak_steps
        prof_rows.append({
            "time_step_start":        t_start,
            "time_step_end":          t_end,
            "hour":                   hour_f,
            "P_total_kw":             ptot,
            "energy_from_grid_kwh":   ptot * dt,
            "energy_to_vehicle_kwh":  ptot * dt * eta,
            "delta_power_change_kw":  delt,
            "is_smud_peak_window":    in_peak,
        })
    pd.DataFrame(prof_rows).to_csv(OUTPUT_DIR / "exact_milp_site_power_profile.csv", index=False)
    print("Saved: exact_milp_site_power_profile.csv")

    # ----------------------------------------------------------------
    # 5. Cost breakdown
    # ----------------------------------------------------------------
    total_grid_energy   = sum(P_total_vals[t] * dt for t in range(n_steps))
    total_veh_energy    = total_grid_energy * eta
    total_power_change  = sum(delta_vals.values())

    cost_rows = [
        {"component": "daily_capex_cost",               "value": sol["daily_capex_cost"]},
        {"component": "energy_cost",                    "value": sol["energy_cost"]},
        {"component": "global_demand_cost",             "value": sol["global_demand_cost"]},
        {"component": "peak_window_demand_cost",        "value": sol["peak_window_cost"]},
        {"component": "smoothing_cost",                 "value": sol["smoothing_cost"]},
        {"component": "energy_error_cost",              "value": sol.get("energy_error_cost", 0.0)},
        {"component": "energy_error_kwh",               "value": sol.get("energy_error_kwh", 0.0)},
        {"component": "total_objective_cost",           "value": sol["total_objective_cost"]},
        {"component": "P_max_kw",                       "value": P_max_val},
        {"component": "P_peak_window_kw",               "value": P_peak_val},
        {"component": "total_grid_energy_kwh",          "value": total_grid_energy},
        {"component": "total_vehicle_energy_kwh",       "value": total_veh_energy},
        {"component": "total_power_change_kw",          "value": total_power_change},
        {"component": "total_power_change_integral_kw_step", "value": total_power_change * dt},
    ]
    pd.DataFrame(cost_rows).to_csv(OUTPUT_DIR / "exact_milp_cost_breakdown.csv", index=False)
    print("Saved: exact_milp_cost_breakdown.csv")


# ============================================================
# VALIDATION
# ============================================================

def validate_solution(
    sol:           dict,
    events_df:     pd.DataFrame,
    time_grid:     List[pd.Timestamp],
    charger_specs: Dict[str, dict],
    P_eff:         Dict[Tuple[str, str], float],
    E:             Dict[str, float],
    feasible_keys: List[Tuple],
    dt_hours:      float = DT_HOURS,
    eta:           float = ETA,
) -> None:
    """
    Post-solve sanity checks.  Prints warnings for any violations.
    """
    if sol.get("status") in ("infeasible", "no_solution"):
        return

    print("\n--- Validation ---")
    N_vals       = sol["N_vals"]
    P_total_vals = sol["P_total_vals"]
    P_max_val    = sol["P_max_val"]
    P_peak_val   = sol["P_peak_val"]
    x_vals       = sol["x_vals"]
    u_vals       = sol["u_vals"]
    peak_steps   = set(sol["peak_steps"])
    dt           = dt_hours
    n_steps      = len(time_grid)
    charger_types = list(charger_specs.keys())
    tol           = 1e-3

    any_warn = False

    # 1. Energy satisfaction
    delivered: Dict[str, float] = {}
    for (v, tidx, c), pwr in x_vals.items():
        delivered[v] = delivered.get(v, 0.0) + pwr * dt * eta
    for v, req in E.items():
        dlv = delivered.get(v, 0.0)
        if dlv < req - ENERGY_TOL:
            print(f"  [WARN] Energy shortfall: event {v}  required={req:.3f}  delivered={dlv:.3f}  gap={req-dlv:.4f}")
            any_warn = True

    # 2. No overcharging beyond battery room (E_max).
    #    With constant-power discrete slots, delivered may slightly exceed E[v] (by at most
    #    one slot's worth) but must not exceed E_max[v] (battery capacity room).
    E_max_val = _compute_E_max(events_df, E)
    for v in E:
        dlv   = delivered.get(v, 0.0)
        e_cap = E_max_val.get(v, float("inf"))
        if dlv > e_cap + tol:
            print(f"  [WARN] Overcharge beyond battery room: event {v}  "
                  f"required={E[v]:.3f}  delivered={dlv:.3f}  battery_room={e_cap:.3f}")
            any_warn = True
        elif dlv > E[v] * 1.01 + tol:
            print(f"  [INFO] Slight overcharge (within battery room): event {v}  "
                  f"required={E[v]:.3f}  delivered={dlv:.3f}  battery_room={e_cap:.3f}")

    # 3. Charger capacity
    active_count: Dict[Tuple[int, str], int] = {}
    for (v, tidx, c) in u_vals:
        key = (tidx, c)
        active_count[key] = active_count.get(key, 0) + 1
    for (tidx, c), cnt in active_count.items():
        if cnt > N_vals[c] + tol:
            print(f"  [WARN] Charger capacity exceeded at t={tidx} c={c}: {cnt} > {N_vals[c]}")
            any_warn = True

    # 4. Single-plug rule
    plug_count: Dict[Tuple[str, int], int] = {}
    for (v, tidx, c) in u_vals:
        key = (v, tidx)
        plug_count[key] = plug_count.get(key, 0) + 1
    for (v, tidx), cnt in plug_count.items():
        if cnt > 1 + tol:
            print(f"  [WARN] Single-plug violated: event {v} at t={tidx} has {cnt} active chargers")
            any_warn = True

    # 5. Power profile consistency
    for tidx in range(n_steps):
        computed = sum(x_vals.get((v, tidx, c), 0.0)
                       for (v, t2, c) in x_vals if t2 == tidx)
        if abs(computed - P_total_vals[tidx]) > tol:
            print(f"  [WARN] Power profile mismatch at t={tidx}: "
                  f"computed={computed:.3f}  model={P_total_vals[tidx]:.3f}")
            any_warn = True

    # 6. P_max
    actual_max = max(P_total_vals.values())
    if actual_max > P_max_val + tol:
        print(f"  [WARN] P_max under-reported: actual={actual_max:.3f}  model={P_max_val:.3f}")
        any_warn = True

    # 7. P_peak_window
    if peak_steps:
        actual_peak_win = max(P_total_vals[t] for t in peak_steps)
        if actual_peak_win > P_peak_val + tol:
            print(f"  [WARN] P_peak_window under-reported: "
                  f"actual={actual_peak_win:.3f}  model={P_peak_val:.3f}")
            any_warn = True

    if not any_warn:
        print("  All validation checks passed.")


# ============================================================
# PLOTTING
# ============================================================

def plot_power_profile(
    sol:        dict,
    time_grid:  List[pd.Timestamp],
    dt_hours:   float = DT_HOURS,
) -> None:
    """
    Plot the optimised 24-hour site charging power profile and save to PNG.
    """
    if sol.get("status") in ("infeasible", "no_solution"):
        return

    P_total_vals = sol["P_total_vals"]
    peak_steps   = set(sol["peak_steps"])
    P_max_val    = sol["P_max_val"]
    P_peak_val   = sol["P_peak_val"]
    n_steps      = len(time_grid)

    hours = [(t.hour + t.minute / 60.0) for t in time_grid]
    ptot  = [P_total_vals[i] for i in range(n_steps)]

    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(hours, ptot, color="#1a6faf", linewidth=2.0, label="Site power (kW)")
    ax.fill_between(hours, ptot, alpha=0.15, color="#1a6faf")

    # Shade SMUD peak window
    if peak_steps:
        peak_h = [(t.hour + t.minute / 60.0) for i, t in enumerate(time_grid) if i in peak_steps]
        if peak_h:
            ax.axvspan(min(peak_h), max(peak_h) + dt_hours,
                       alpha=0.18, color="#e07b00", label="SMUD peak window 17-20h")

    # Annotate peaks
    ax.axhline(P_max_val, color="#c0392b", linestyle="--", linewidth=1.2,
               label=f"P_max = {P_max_val:.1f} kW")
    if P_peak_val > 0:
        ax.axhline(P_peak_val, color="#e07b00", linestyle=":", linewidth=1.2,
                   label=f"P_peak_win = {P_peak_val:.1f} kW")

    ax.set_xlabel("Hour of day", fontsize=12)
    ax.set_ylabel("Total site charging power (kW)", fontsize=12)
    ax.set_title("Optimized Smooth EV Charging Power Profile -- Northgate", fontsize=13, fontweight="bold")
    ax.set_xlim(0, 24)
    ax.set_xticks(range(0, 25, 2))
    ax.legend(fontsize=10)
    ax.grid(True, linestyle="--", alpha=0.4)

    plt.tight_layout()
    out = OUTPUT_DIR / "exact_milp_power_profile.png"
    fig.savefig(out, dpi=300)
    plt.close(fig)
    print(f"Saved: {out.name}")

    # ----------------------------------------------------------------
    # Second plot: power profile + per-vehicle activity overlay
    # ----------------------------------------------------------------
    fig2, axes = plt.subplots(2, 1, figsize=(14, 10),
                              gridspec_kw={"height_ratios": [2, 1]})

    ax_top, ax_bot = axes

    # Top: site power
    ax_top.plot(hours, ptot, color="#1a6faf", linewidth=2.0, label="Site power (kW)")
    ax_top.fill_between(hours, ptot, alpha=0.15, color="#1a6faf")
    if peak_steps:
        ax_top.axvspan(min(peak_h), max(peak_h) + dt_hours,
                       alpha=0.18, color="#e07b00", label="SMUD peak window")
    ax_top.axhline(P_max_val, color="#c0392b", linestyle="--", linewidth=1.2,
                   label=f"P_max={P_max_val:.1f} kW")
    ax_top.set_ylabel("Power (kW)", fontsize=11)
    ax_top.set_title("Optimized Smooth EV Charging Power Profile -- Northgate", fontsize=12, fontweight="bold")
    ax_top.legend(fontsize=9)
    ax_top.grid(True, linestyle="--", alpha=0.4)
    ax_top.set_xlim(0, 24)
    ax_top.set_xticks(range(0, 25, 2))

    # Bottom: vehicle charging activity (active vehicle count per step)
    x_vals    = sol["x_vals"]
    active_cnt = [0] * n_steps
    for (v, tidx, c), pwr in x_vals.items():
        if pwr > 1e-5:
            active_cnt[tidx] += 1

    ax_bot.bar(hours, active_cnt, width=dt_hours * 0.9,
               color="#2ecc71", alpha=0.7, align="edge", label="Active charging sessions")
    ax_bot.set_xlabel("Hour of day", fontsize=11)
    ax_bot.set_ylabel("Active sessions", fontsize=11)
    ax_bot.legend(fontsize=9)
    ax_bot.grid(True, linestyle="--", alpha=0.3)
    ax_bot.set_xlim(0, 24)
    ax_bot.set_xticks(range(0, 25, 2))

    plt.tight_layout()
    out2 = OUTPUT_DIR / "exact_milp_power_profile_with_events.png"
    fig2.savefig(out2, dpi=300)
    plt.close(fig2)
    print(f"Saved: {out2.name}")


# ============================================================
# SUMMARY TEXT FILE
# ============================================================

def write_summary(
    sol:           dict,
    events_df:     pd.DataFrame,
    charger_specs: Dict[str, dict],
    E:             Dict[str, float],
    daily_capex:   Dict[str, float],
    lambda_smooth: float = LAMBDA_SMOOTH,
) -> None:
    """
    Write a human-readable summary of the optimisation result.
    """
    out_path = OUTPUT_DIR / "exact_milp_summary.txt"
    delivered: Dict[str, float] = {}
    x_vals = sol.get("x_vals", {})
    dt     = DT_HOURS
    for (v, tidx, c), pwr in x_vals.items():
        delivered[v] = delivered.get(v, 0.0) + pwr * dt * ETA

    total_req = sum(E.values())
    total_dlv = sum(delivered.values())
    total_unmet = max(0.0, total_req - total_dlv)
    n_served = sum(1 for v in E if delivered.get(v, 0.0) >= E[v] - ENERGY_TOL)
    N_vals   = sol.get("N_vals", {})
    charger_types = list(charger_specs.keys())

    lines = [
        "=" * 70,
        "EXACT MILP CHARGER SIZING OPTIMIZATION -- NORTHGATE",
        "=" * 70,
        f"Solver used          : {sol.get('solver', 'unknown')}",
        f"Solver status        : {sol.get('status', 'unknown')}",
        f"Objective value      : ${sol.get('obj_val', float('nan')):.4f}",
        f"MIP gap              : {sol.get('mip_gap', float('nan')):.4f}",
        f"Solve time           : {sol.get('solve_time', 0):.1f} s",
        "",
        "--- Selected Charger Mix ---",
    ]
    for c in charger_types:
        n = N_vals.get(c, 0)
        spec = charger_specs[c]
        lines.append(
            f"  {c:<14}  {n:>2} unit(s)  "
            f"power={spec['power_kw']:>6.1f} kW  "
            f"daily_capex=${daily_capex[c]:.4f}/unit  "
            f"total=${n * daily_capex[c]:.4f}/day"
        )
    lines += [
        "",
        "--- Event Statistics ---",
        f"  Total events           : {len(E)}",
        f"  Events served          : {n_served}  / {len(E)}",
        f"  Total required energy  : {total_req:.2f} kWh",
        f"  Total delivered energy : {total_dlv:.2f} kWh",
        f"  Total unmet energy     : {total_unmet:.4f} kWh",
        "",
        "--- Power Statistics ---",
        f"  Peak site power (P_max)        : {sol.get('P_max_val', 0):.2f} kW",
        f"  Peak-window power (17-20h)     : {sol.get('P_peak_val', 0):.2f} kW",
        f"  Mean site power (P_mean)       : {sol.get('p_mean_kw', 0):.2f} kW",
        f"  RMS above-mean deviation       : {sol.get('rmsd_kw', 0):.4f} kW",
        f"  Above-mean variance            : {sol.get('rmsd_kw', 0)**2:.4f} kW^2",
        f"  Smoothing cost (lambda*V_above): ${sol.get('smoothing_cost', 0):.4f}",
        "",
        "--- Cost Breakdown ---",
        f"  Daily CapEx (charger ownership) : ${sol.get('daily_capex_cost', 0):.4f}",
        f"  Energy cost                     : ${sol.get('energy_cost', 0):.4f}",
        f"  Global demand charge            : ${sol.get('global_demand_cost', 0):.4f}",
        f"  Peak-window demand charge       : ${sol.get('peak_window_cost', 0):.4f}",
        f"  Smoothing penalty               : ${sol.get('smoothing_cost', 0):.4f}",
        f"  TOTAL OBJECTIVE COST            : ${sol.get('total_objective_cost', 0):.4f}",
        "",
        "--- Parameters ---",
        f"  dt          : {DT_HOURS:.5f} h ({DT_MINUTES}-min steps)",
        f"  eta         : {ETA}",
        f"  C_energy (SMUD TOD, America/Los_Angeles):",
        f"    Summer Peak   (wkdy 16-21h) : ${C_ENERGY_SUMMER_PEAK}/kWh",
        f"    Summer Off-Peak             : ${C_ENERGY_SUMMER_OFFPEAK}/kWh",
        f"    Non-Summer Peak (wkdy 16-21h): ${C_ENERGY_NONSUMMER_PEAK}/kWh",
        f"    Non-Summer Off-Peak Saver   : ${C_ENERGY_NONSUMMER_OFFSAVER}/kWh",
        f"    Non-Summer Off-Peak         : ${C_ENERGY_NONSUMMER_OFFPEAK}/kWh",
        f"  Fixed charge (info, not in obj): $412.90/month",
        f"  C_demand_global   : ${C_DEMAND_GLOBAL}/kW",
        f"  C_demand_peak_win : ${C_DEMAND_PEAK_WIN}/kW  (16:00-21:00 local)",
        f"  lambda_smooth     : {lambda_smooth} $/kW",
        "",
        "--- IMPORTANT NOTE on Demand Charges ---",
        "  Demand charges ($/kW) are normally billed on the monthly peak.",
        "  This script applies them to the representative-day peak as a",
        "  planning proxy.  For accurate monthly cost estimation, repeat",
        "  the optimisation on the worst-case day of the billing month,",
        "  or scale by selecting the appropriate monthly peak day.",
        "=" * 70,
    ]

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved: {out_path.name}")
    # Also echo to terminal
    print("\n" + "\n".join(lines))


# ============================================================
# SENSITIVITY ANALYSIS
# ============================================================

def run_sensitivity(
    events_df:      pd.DataFrame,
    time_grid:      List[pd.Timestamp],
    charger_specs:  Dict[str, dict],
    daily_capex:    Dict[str, float],
    P_eff:          Dict[Tuple[str, str], float],
    feasible_keys:  List[Tuple],
    E:              Dict[str, float],
    available_times: Dict[str, List[int]],
    use_gurobi:     bool,
) -> None:
    """
    Sweep lambda_smooth values and record cost / peak statistics for each.
    """
    print("\n" + "=" * 60)
    print("SENSITIVITY ANALYSIS: lambda_smooth sweep")
    print("=" * 60)

    rows = []
    charger_types = list(charger_specs.keys())

    for lam in LAMBDA_SMOOTH_VALUES:
        print(f"\n  Solving with lambda_smooth = {lam} ...")
        try:
            if use_gurobi:
                sol = solve_with_gurobi(
                    events_df, time_grid, charger_specs, daily_capex,
                    P_eff, feasible_keys, E, available_times,
                    lambda_smooth=lam,
                )
            else:
                sol = solve_with_pyomo_highs(
                    events_df, time_grid, charger_specs, daily_capex,
                    P_eff, feasible_keys, E, available_times,
                    lambda_smooth=lam,
                )
        except Exception as exc:
            print(f"  [ERROR] lambda={lam}: {exc}")
            continue

        if sol.get("status") in ("infeasible", "no_solution"):
            continue

        N_vals = sol["N_vals"]
        row = {
            "lambda_smooth":         lam,
            "total_objective_cost":  sol["total_objective_cost"],
            "daily_capex_cost":      sol["daily_capex_cost"],
            "energy_cost":           sol["energy_cost"],
            "global_demand_cost":    sol["global_demand_cost"],
            "peak_window_cost":      sol["peak_window_cost"],
            "smoothing_cost":        sol["smoothing_cost"],
            "P_max_kw":              sol["P_max_val"],
            "P_peak_window_kw":      sol["P_peak_val"],
            "total_power_variation": sum(sol.get("delta_vals", {}).values()),
            "solve_time_s":          sol.get("solve_time", 0),
        }
        for c in charger_types:
            row[f"N_{c}"] = N_vals.get(c, 0)
        rows.append(row)
        print(f"    obj={sol['total_objective_cost']:.4f}  "
              f"P_max={sol['P_max_val']:.1f} kW  "
              f"variation={sum(sol.get('delta_vals',{}).values()):.1f} kW")

    if rows:
        df_sens = pd.DataFrame(rows)
        df_sens.to_csv(OUTPUT_DIR / "exact_milp_smoothing_sensitivity.csv", index=False)
        print("\nSaved: exact_milp_smoothing_sensitivity.csv")


# ============================================================
# MAIN
# ============================================================

def main(charger_specs_override: dict | None = None,
         events_df_override: "pd.DataFrame | None" = None) -> None:
    """
    Entry point.  Orchestrates data loading, MILP construction,
    solving, validation, export, and plotting.

    Parameters
    ----------
    charger_specs_override : dict, optional
        If provided, use these charger specs instead of the default
        Caltrans pricing.  Must follow the same structure as
        build_charger_specs_caltrans().  Also ensure that
        CHARGER_UPPER_BOUNDS is patched at the module level before
        calling main() when using non-standard charger type keys.
    events_df_override : pd.DataFrame, optional
        If provided, skip CSV loading entirely and use this pre-processed
        DataFrame.  Must already be cleaned (same schema as clean_events_df
        output).  Used by scenario_runner to apply the multi-day dwell rule
        before passing events to the solver.
    """
    print("=" * 70)
    print("Northgate EV Charging Infrastructure -- Exact MILP Optimizer")
    print("=" * 70)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # --- Load and clean data ---
    if events_df_override is not None:
        events_df = events_df_override
        print(f"[INFO] Using events_df_override: {len(events_df)} events.")
    else:
        raw_df    = load_events_data()
        events_df = clean_events_df(raw_df)

    # --- Charger specs and daily cost ---
    if charger_specs_override is not None:
        charger_specs = charger_specs_override
        print("[INFO] Using charger_specs_override (not Caltrans defaults).")
    else:
        charger_specs = build_charger_specs_caltrans()  # Caltrans Q3 FY2025/26 pricing
    daily_capex   = compute_daily_capex(charger_specs)

    # --- Time grid ---
    time_grid = build_time_grid(events_df)
    print(f"Time grid: {len(time_grid)} steps  "
          f"({time_grid[0]} -> {time_grid[-1]})")

    # --- Effective power and feasible keys ---
    P_eff = compute_effective_power(events_df, charger_specs)
    print(f"Effective power entries (v,c): {len(P_eff)}")

    feasible_keys, E, arrival_map, departure_map, available_times = build_feasible_keys(
        events_df, time_grid, charger_specs, P_eff
    )

    # --- Attempt Gurobi, fall back to Pyomo + HiGHS ---
    use_gurobi = False
    try:
        import gurobipy  # noqa: F401
        use_gurobi = True
    except ImportError:
        print("\n[WARNING] Gurobi not available. Falling back to Pyomo + HiGHS.")

    if use_gurobi:
        sol = solve_with_gurobi(
            events_df, time_grid, charger_specs, daily_capex,
            P_eff, feasible_keys, E, available_times,
            lambda_energy_error=LAMBDA_ENERGY_ERROR,
        )
    else:
        sol = solve_with_pyomo_highs(
            events_df, time_grid, charger_specs, daily_capex,
            P_eff, feasible_keys, E, available_times,
            lambda_energy_error=LAMBDA_ENERGY_ERROR,
        )

    if sol.get("status") in ("infeasible", "no_solution"):
        print("\nOptimisation failed -- no solution exported.")
        return

    # --- Validate ---
    validate_solution(sol, events_df, time_grid, charger_specs, P_eff, E, feasible_keys)

    # --- Export ---
    export_solution(sol, events_df, time_grid, charger_specs, daily_capex, P_eff, E, feasible_keys)

    # --- Plots ---
    plot_power_profile(sol, time_grid)

    # --- Summary ---
    write_summary(sol, events_df, charger_specs, E, daily_capex)

    # --- Optional sensitivity sweep ---
    if RUN_SENSITIVITY:
        run_sensitivity(
            events_df, time_grid, charger_specs, daily_capex,
            P_eff, feasible_keys, E, available_times, use_gurobi,
        )

    print("\nDone.  All outputs saved to:", OUTPUT_DIR)


if __name__ == "__main__":
    main()
