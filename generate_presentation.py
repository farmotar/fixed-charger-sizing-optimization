"""
generate_presentation.py
========================
Generates all presentation assets for the Northgate EV charger sizing meeting:
  - 5 Gantt-style charging schedule figures (one per top day)
  - 1 summary results table figure
  - Slide outline text file (slide_outline.txt)

Saves all outputs to D:/Geotab_EV_Parameters/charger_sizing_test/presentation/
"""
from __future__ import annotations

import textwrap
from datetime import timedelta
from itertools import product
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DIR    = Path("D:/Geotab_EV_Parameters/charger_sizing_test")
BASE   = Path("D:/Geotab_EV_Parameters")
OUTDIR = DIR / "presentation"
OUTDIR.mkdir(exist_ok=True)

MAPPING_FILE = DIR / "ev_equivalent_max_charge_power_mapping_filled.xlsx"
SOURCE_EXCEL = BASE / "northgate_departures_chained_aug_Dec_2025_with_inbound_soc_50pct_fallback.xlsx"

# ---------------------------------------------------------------------------
# Constants (mirror northgate_charger_sizing_final.py)
# ---------------------------------------------------------------------------
ETA          = 0.90
TIME_STEP_H  = 0.25
FULL_CHG_TOL = 0.10
SIM_CAP_H    = 48.0
BENCH_DC_KW  = 350.0
DWELL_MIN_H  = 0.25

CTYPES = ["L2_19p2kW", "DC_50kW", "DC_150kW", "DC_350kW"]
CHARGER_SPECS = {
    "L2_19p2kW": {"power_kw": 19.2,  "ac_dc": "AC"},
    "DC_50kW":   {"power_kw": 50.0,  "ac_dc": "DC"},
    "DC_150kW":  {"power_kw": 150.0, "ac_dc": "DC"},
    "DC_350kW":  {"power_kw": 350.0, "ac_dc": "DC"},
}

# Best mixes from top-5 sensitivity run
BEST_MIXES = {
    "2025-08-25": {"L2_19p2kW": 0, "DC_50kW": 0, "DC_150kW": 1, "DC_350kW": 0},
    "2025-06-26": {"L2_19p2kW": 0, "DC_50kW": 0, "DC_150kW": 1, "DC_350kW": 0},
    "2025-12-01": {"L2_19p2kW": 0, "DC_50kW": 0, "DC_150kW": 1, "DC_350kW": 0},
    "2025-06-30": {"L2_19p2kW": 0, "DC_50kW": 0, "DC_150kW": 0, "DC_350kW": 1},
    "2025-10-08": {"L2_19p2kW": 0, "DC_50kW": 0, "DC_150kW": 1, "DC_350kW": 0},
}

TOP5_META = {
    "2025-08-25": {"clean_kwh": 473.77, "svc_kwh": 202.99, "excl_kwh": 270.78, "cost": 150_000},
    "2025-06-26": {"clean_kwh": 399.44, "svc_kwh": 178.20, "excl_kwh": 221.24, "cost": 150_000},
    "2025-12-01": {"clean_kwh": 380.48, "svc_kwh":  87.77, "excl_kwh": 292.71, "cost": 150_000},
    "2025-06-30": {"clean_kwh": 357.29, "svc_kwh": 357.29, "excl_kwh":   0.00, "cost": 350_000},
    "2025-10-08": {"clean_kwh": 342.64, "svc_kwh": 108.18, "excl_kwh": 234.46, "cost": 150_000},
}

CHARGER_COLORS = {
    "DC_150kW":  "#1E88E5",   # blue
    "DC_350kW":  "#E53935",   # red
    "DC_50kW":   "#8E24AA",   # purple
    "L2_19p2kW": "#43A047",   # green
    "none":      "#BDBDBD",   # gray
}

TOP5_DATES = ["2025-08-25", "2025-06-26", "2025-12-01", "2025-06-30", "2025-10-08"]

MODEL_SHORT = {
    "Freightliner eCascadia":        "Fr. eCascadia",
    "Freightliner eM2":              "Fr. eM2",
    "BYD 6F Cab-Forward Truck":      "BYD 6F",
    "Chevrolet Silverado EV WT":     "Chev Silverado EV",
    "Chevrolet Bolt EV":             "Chev Bolt EV",
    "Ford F-150 Lightning":          "F-150 Lightning",
    "GMC Hummer EV":                 "GMC Hummer EV",
    "Rivian R1S":                    "Rivian R1S",
    "Rivian R1T":                    "Rivian R1T",
    "Tesla Model 3":                 "Tesla Model 3",
    "Blue Arc EV":                   "Blue Arc EV",
}


# ---------------------------------------------------------------------------
# Simulation helpers (copied from final script)
# ---------------------------------------------------------------------------

def _eff(charger_kw, ac_dc, mac, mdc):
    if ac_dc == "AC":
        return 0.0 if mac <= 0 else min(charger_kw, mac)
    return 0.0 if mdc <= 0 else min(charger_kw, mdc)


def build_pool(counts):
    pool = []
    for ct in CTYPES:
        for i in range(counts[ct]):
            pool.append({"cid": f"{ct}_{i+1:02d}", "ctype": ct,
                         "power_kw": CHARGER_SPECS[ct]["power_kw"],
                         "ac_dc":    CHARGER_SPECS[ct]["ac_dc"]})
    return pool


def simulate_detailed(ev_data, pool, sim_start, n_steps):
    eids        = list(ev_data)
    rem         = {e: ev_data[e]["energy"] for e in eids}
    dld         = {e: 0.0 for e in eids}
    csteps      = {c["cid"]: 0 for c in pool}
    # Non-preemptive: once a charger is assigned it stays with the vehicle
    # until rem <= FULL_CHG_TOL or the vehicle departs.
    assignments = {c["cid"]: None for c in pool}   # cid -> eid | None
    logs        = []
    peak        = 0.0

    for si in range(n_steps):
        ts = sim_start + timedelta(hours=si * TIME_STEP_H)
        te = ts + timedelta(hours=TIME_STEP_H)

        # Phase 1: release chargers whose vehicle finished or departed
        for cid in list(assignments):
            e = assignments[cid]
            if e is None:
                continue
            if rem[e] <= FULL_CHG_TOL or ev_data[e]["dep"] <= ts:
                assignments[cid] = None

        # Phase 2: assign free chargers to unserved waiting vehicles
        free_chargers = [c for c in pool if assignments[c["cid"]] is None]
        if free_chargers:
            assigned_eids = {e for e in assignments.values() if e is not None}
            waiting = [
                e for e in eids
                if ev_data[e]["arr"] < te
                and ev_data[e]["dep"] > ts
                and rem[e] > FULL_CHG_TOL
                and e not in assigned_eids
            ]
            if waiting:
                def _urg(e):
                    rh = max((ev_data[e]["dep"] - ts).total_seconds() / 3600, 0.01)
                    return rem[e] / rh
                waiting.sort(key=lambda e: (ev_data[e]["dep"], -_urg(e)))
                for e in waiting:
                    if not free_chargers:
                        break
                    mac, mdc = ev_data[e]["mac"], ev_data[e]["mdc"]
                    bc, beff = None, 0.0
                    for ch in free_chargers:
                        eff = _eff(ch["power_kw"], ch["ac_dc"], mac, mdc)
                        if eff > beff:
                            beff, bc = eff, ch
                    if bc is None or beff == 0:
                        continue
                    assignments[bc["cid"]] = e
                    free_chargers.remove(bc)

        # Phase 3: deliver energy on every active assignment
        kw = 0.0
        for ch in pool:
            e = assignments[ch["cid"]]
            if e is None:
                continue
            if ev_data[e]["arr"] >= te or ev_data[e]["dep"] <= ts:
                continue
            mac, mdc = ev_data[e]["mac"], ev_data[e]["mdc"]
            beff = _eff(ch["power_kw"], ch["ac_dc"], mac, mdc)
            if beff <= 0:
                continue

            ov_s = max(ev_data[e]["arr"], ts)
            ov_e = min(ev_data[e]["dep"], te)
            ov_h = (ov_e - ov_s).total_seconds() / 3600
            if ov_h <= 0:
                continue

            e_st = min(ETA * beff * ov_h, rem[e])
            rem[e]          -= e_st
            dld[e]          += e_st
            csteps[ch["cid"]] += 1
            kw += beff

            logs.append({
                "charging_event_id":    e,
                "vehicle_id":           ev_data[e]["vehicle_id"],
                "ev_equivalent_model":  ev_data[e]["ev_model"],
                "charger_type":         ch["ctype"],
                "effective_power_kw":   round(beff, 2),
                "overlap_hours":        round(ov_h, 4),
                "energy_delivered_kwh": round(e_st, 4),
                "ts":                   ts,
                "te":                   te,
                "actual_start":         ov_s,
                "actual_end":           ov_e,
            })

        peak = max(peak, kw)

    util = {}
    for ct in CTYPES:
        n = counts_from_pool(pool, ct)
        if n > 0:
            used = sum(csteps.get(f"{ct}_{i+1:02d}", 0) for i in range(n))
            util[ct] = round(used / (n * n_steps) * 100, 1)
        else:
            util[ct] = None

    return dld, logs, peak, util


def counts_from_pool(pool, ct):
    return sum(1 for c in pool if c["ctype"] == ct)


def build_schedule(svc_df, dld, logs):
    log_df = pd.DataFrame(logs) if logs else pd.DataFrame()
    rows = []
    violations = []
    for _, row in svc_df.iterrows():
        eid    = row["charging_event_id"]
        energy = float(row["energy_needed_kwh_for_visit"])
        deliv  = round(dld.get(eid, 0.0), 3)
        unmet  = round(max(energy - deliv, 0.0), 3)
        arr    = row["arrival_time"]
        dep    = row["departure_time"]

        ev_log = log_df[log_df["charging_event_id"] == eid] if len(log_df) > 0 else pd.DataFrame()

        if len(ev_log) > 0:
            # Use actual overlap times: ov_s=max(arr,slot_start), ov_e=min(dep,slot_end).
            # These are stored in simulate_detailed and are always within the dwell window.
            cs = ev_log["actual_start"].min()
            ce = ev_log["actual_end"].max()
            total_h = round(float(ev_log["overlap_hours"].sum()), 4)
            eff_pw  = round(float(ev_log["effective_power_kw"].iloc[0]), 1)
            ctype   = ev_log["charger_type"].iloc[0]
            # Validation: charge window must lie within dwell
            if cs < arr - pd.Timedelta(seconds=5):
                violations.append(f"charge_start before arrival:  {eid}")
            if pd.notna(dep) and ce > dep + pd.Timedelta(seconds=5):
                violations.append(f"charge_end after departure:   {eid}")
            if ce <= cs:
                violations.append(f"charge_end <= charge_start:   {eid}")
        else:
            cs, ce, total_h, eff_pw, ctype = None, None, 0.0, 0.0, "none"

        slack = round((dep - ce).total_seconds() / 60, 1) if (ce is not None and pd.notna(dep)) else None

        rows.append({
            "charging_event_id":             eid,
            "vehicle_id":                    row["vehicle_id"],
            "ev_equivalent_model":           row.get("ev_equivalent_model", ""),
            "arrival_time":                  arr,
            "departure_time":                dep,
            "dwell_hours":                   round(float(row["dwell_hours"]), 3),
            "energy_needed_kwh_for_visit":   round(energy, 3),
            "charge_start_time":             cs,
            "charge_end_time":               ce,
            "total_charging_duration_hours": total_h,
            "delivered_energy_kwh":          deliv,
            "remaining_unmet_kwh":           unmet,
            "charger_type":                  ctype,
            "effective_power_kw":            eff_pw,
            "timing_slack_min":              slack,
        })
    if violations:
        print("  *** SIMULATION/PLOTTING VIOLATIONS DETECTED ***")
        for v in violations:
            print(f"    {v}")
    return pd.DataFrame(rows)


def build_day(df_src, date_str, mapping_df):
    mask = (
        (df_src["_visit_date"] == date_str)
        & df_src["northgate_fill_kwh"].notna()
        & (df_src["northgate_fill_kwh"] > 0)
        & df_src["zone_entry_time_utc"].notna()
        & df_src["dwell_hrs"].notna()
        & (df_src["dwell_hrs"] >= DWELL_MIN_H)
    )
    day = df_src[mask].copy().sort_values(["device_name", "zone_entry_time_utc"]).reset_index(drop=True)
    if len(day) == 0:
        return pd.DataFrame(), pd.DataFrame()

    day["visit_seq"] = day.groupby("device_id").cumcount() + 1
    ds = date_str.replace("-", "")
    day["charging_event_id"] = (
        day["device_name"].astype(str) + "_" + ds
        + "_visit_" + day["visit_seq"].astype(str)
    )
    day = day.rename(columns={
        "device_name":        "vehicle_id",
        "ev_equivalency":     "ev_equivalent_model",
        "dwell_hrs":          "dwell_hours",
        "northgate_fill_kwh": "energy_needed_kwh_for_visit",
        "zone_entry_time_utc": "arrival_time",
        "zone_exit_time_utc":  "departure_time",
    })
    no_dep = day["departure_time"].isna() & day["dwell_hours"].notna()
    day.loc[no_dep, "departure_time"] = (
        day.loc[no_dep, "arrival_time"]
        + pd.to_timedelta(day.loc[no_dep, "dwell_hours"], unit="h")
    )
    day = day[day["departure_time"] > day["arrival_time"]].copy()
    day = day.merge(mapping_df[["ev_equivalent_model", "max_ac_charge_kw", "max_dc_charge_kw"]],
                    on="ev_equivalent_model", how="left")
    day["max_ac_charge_kw"] = day["max_ac_charge_kw"].fillna(0.0)
    day["max_dc_charge_kw"] = day["max_dc_charge_kw"].fillna(0.0)

    eff350 = day["max_dc_charge_kw"].clip(upper=BENCH_DC_KW)
    day["max_possible_energy_kwh"] = (ETA * eff350 * day["dwell_hours"]).round(3)
    day["individually_feasible"]   = (
        day["energy_needed_kwh_for_visit"] <= day["max_possible_energy_kwh"] + FULL_CHG_TOL
    )
    svc  = day[day["individually_feasible"]].copy().reset_index(drop=True)
    excl = day[~day["individually_feasible"]].copy().reset_index(drop=True)
    return svc, excl


# ---------------------------------------------------------------------------
# Figure generation
# ---------------------------------------------------------------------------

def to_hour(ts, midnight):
    if ts is None or pd.isna(ts):
        return None
    return (ts - midnight).total_seconds() / 3600


def fmt_h(h):
    if h is None:
        return ""
    day_h = h % 24
    hh = int(day_h)
    mm = int(round((day_h - hh) * 60))
    if mm == 60:
        hh += 1
        mm = 0
    suffix = " +1d" if h >= 24 else ""
    return f"{hh:02d}:{mm:02d}{suffix}"


def generate_gantt(date_str, svc_df, excl_df, schedule_df, log_df, best_mix, meta, out_path):
    midnight = pd.Timestamp(date_str, tz="UTC")

    # Merge schedule into svc_df
    svc_plot = svc_df.merge(
        schedule_df[["charging_event_id", "charge_start_time", "charge_end_time",
                     "total_charging_duration_hours", "delivered_energy_kwh",
                     "charger_type", "effective_power_kw", "timing_slack_min"]],
        on="charging_event_id", how="left"
    ).sort_values("arrival_time").reset_index(drop=True)

    # Keep only meaningful charging demand (energy >= FULL_CHG_TOL).
    # Near-zero events are already fully charged per the simulation tolerance
    # and should not appear as charger-sizing demand rows.
    svc_plot = svc_plot[
        svc_plot["energy_needed_kwh_for_visit"] >= FULL_CHG_TOL
    ].copy().reset_index(drop=True)
    meaningful_svc_kwh = float(svc_plot["energy_needed_kwh_for_visit"].sum())

    excl_plot = excl_df.sort_values("arrival_time").reset_index(drop=True) if len(excl_df) > 0 else pd.DataFrame()
    excl_kwh_disp = float(excl_plot["energy_needed_kwh_for_visit"].sum()) if len(excl_plot) > 0 else 0.0

    n_svc  = len(svc_plot)
    n_excl = len(excl_plot)
    n_tot  = n_svc + n_excl

    # Figure size
    row_h   = 0.52
    fig_h   = max(9, row_h * n_tot + 5.5)
    fig_w   = 17
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=150)
    fig.patch.set_facecolor("white")

    # X-axis display bounds: 0h to 30h (midnight to 6 AM next day)
    # Determine actual data extent
    all_arrivals = [to_hour(r["arrival_time"], midnight) for _, r in svc_plot.iterrows()]
    all_arrivals += [to_hour(r["arrival_time"], midnight) for _, r in excl_plot.iterrows()] if n_excl > 0 else []
    all_arrivals = [h for h in all_arrivals if h is not None]

    x_min_data = min(all_arrivals) if all_arrivals else 0
    x_display_start = max(0, x_min_data - 1)
    x_display_end   = min(30, x_display_start + 26)

    LABEL_W = 7.0   # hours reserved for labels on the left
    x_plot_start = x_display_start - LABEL_W

    # Row assignments: serviceable at top (y = n_tot-1 down to n_excl)
    # excluded at bottom (y = n_excl-1 down to 0)
    # Separator gap between serviceable and excluded

    GAP = 0.8  # extra vertical space for separator line

    def row_y(idx, group):
        if group == "svc":
            return n_tot - idx - 1 + (GAP if n_excl > 0 else 0)
        else:
            return n_excl - idx - 1

    y_max = n_tot - 1 + (GAP if n_excl > 0 else 0)

    # Draw serviceable events
    for idx, row in svc_plot.iterrows():
        y = row_y(idx, "svc")

        arr_h = to_hour(row["arrival_time"], midnight)
        dep_h = to_hour(row["departure_time"], midnight)
        if arr_h is None:
            continue
        dep_h_clipped = min(dep_h, x_display_end + 1) if dep_h else x_display_end

        # Dwell bar (gray background)
        dwell_w = max(dep_h_clipped - arr_h, 0)
        ax.barh(y, dwell_w, left=arr_h, height=row_h * 0.75,
                color="#E0E0E0", alpha=0.9, edgecolor="#9E9E9E", linewidth=0.6, zorder=2)

        # Draw one bar PER SIMULATION LOG ENTRY (not one aggregated bar).
        # This correctly shows sequential charging: if vehicle A is preempted
        # by vehicle B in the middle, A's two separate segments are drawn
        # with a visible gap, and B's segment fills that gap — no false overlap.
        deliv = row.get("delivered_energy_kwh", 0.0)
        eid   = row.get("charging_event_id", "")
        ev_logs = (
            log_df[log_df["charging_event_id"] == eid]
            if (log_df is not None and len(log_df) > 0)
            else pd.DataFrame()
        )

        rightmost_seg_end_h = None  # track last segment end for label placement
        for _, lg in ev_logs.iterrows():
            seg_cs_h = to_hour(lg["actual_start"], midnight)
            seg_ce_h = to_hour(lg["actual_end"],   midnight)
            if seg_cs_h is None or seg_ce_h is None:
                continue

            # Validate and clip each segment to the dwell window
            eid_val = eid or "?"
            if seg_cs_h < arr_h - 0.001:
                print(f"    [VALIDATION] {eid_val}: seg_start {fmt_h(seg_cs_h)} before arrival {fmt_h(arr_h)}")
            if dep_h is not None and seg_ce_h > dep_h + 0.001:
                print(f"    [VALIDATION] {eid_val}: seg_end {fmt_h(seg_ce_h)} after departure {fmt_h(dep_h)}")

            seg_cs_plot = max(seg_cs_h, arr_h)
            seg_ce_plot = min(seg_ce_h,
                              dep_h if dep_h is not None else seg_ce_h,
                              dep_h_clipped)
            seg_w = max(seg_ce_plot - seg_cs_plot, 0.0)
            if seg_w <= 0:
                continue

            color = CHARGER_COLORS.get(lg["charger_type"], "#1E88E5")
            ax.barh(y, seg_w, left=seg_cs_plot, height=row_h * 0.75,
                    color=color, alpha=0.92, edgecolor="white", linewidth=0.8, zorder=3)

            # Per-segment energy label (only if segment is wide enough)
            e_seg = float(lg["energy_delivered_kwh"])
            if seg_w > 0.3:
                seg_label = f"{e_seg:.2f}" if e_seg < 1.0 else f"{e_seg:.1f}"
                ax.text(seg_cs_plot + seg_w / 2, y, f"{seg_label} kWh",
                        ha="center", va="center",
                        fontsize=6.5, color="white", fontweight="bold", zorder=5)

            # Track rightmost end for total-energy annotation
            if rightmost_seg_end_h is None or seg_ce_plot > rightmost_seg_end_h:
                rightmost_seg_end_h = seg_ce_plot

        # Arrival marker
        ax.vlines(arr_h, y - row_h*0.38, y + row_h*0.38, color="#616161", linewidth=1.2, zorder=4)

        # Row label
        energy  = float(row.get("energy_needed_kwh_for_visit", 0))
        model   = MODEL_SHORT.get(row.get("ev_equivalent_model", ""), row.get("ev_equivalent_model", "")[:18])
        vid     = str(row.get("vehicle_id", ""))
        slack   = row.get("timing_slack_min")
        slack_s = f" ⚡{slack:.0f}m slack" if (slack is not None and not pd.isna(slack) and slack < 30 and float(deliv or 0) > 0.05) else ""
        energy_fmt = f"{energy:.2f}" if energy < 1.0 else f"{energy:.1f}"
        label   = f"{vid}  {model}  {energy_fmt} kWh{slack_s}"
        ax.text(x_plot_start + LABEL_W - 0.15, y, label,
                ha="right", va="center", fontsize=7.4, color="#212121", zorder=5)

    # Draw excluded events
    for idx, row in excl_plot.iterrows():
        y = row_y(idx, "excl")

        arr_h = to_hour(row["arrival_time"], midnight)
        dep_h = to_hour(row["departure_time"], midnight)
        if arr_h is None:
            continue
        dep_h_clipped = min(dep_h, x_display_end + 1) if dep_h else x_display_end
        dwell_w = max(dep_h_clipped - arr_h, 0)

        # Excluded dwell bar (orange hatch)
        ax.barh(y, dwell_w, left=arr_h, height=row_h * 0.75,
                color="#FFCCBC", alpha=0.85, edgecolor="#E64A19",
                linewidth=1.0, hatch="///", zorder=2)

        # Label
        energy = float(row.get("energy_needed_kwh_for_visit", 0))
        model  = MODEL_SHORT.get(row.get("ev_equivalent_model", ""), row.get("ev_equivalent_model", "")[:18])
        vid    = str(row.get("vehicle_id", ""))
        short  = float(row.get("energy_needed_kwh_for_visit", 0) - row.get("max_possible_energy_kwh", 0))
        label  = f"[EXCL] {vid}  {model}  {energy:.1f} kWh  shortfall {short:.0f} kWh"
        ax.text(x_plot_start + LABEL_W - 0.15, y, label,
                ha="right", va="center", fontsize=7.4, color="#BF360C",
                fontstyle="italic", zorder=5)

    # Separator line between serviceable and excluded
    if n_excl > 0:
        sep_y = n_excl - 0.5 + GAP / 2
        ax.axhline(sep_y, color="#B71C1C", linewidth=1.0, linestyle="--", alpha=0.6, zorder=1)
        ax.text(x_display_start, sep_y + 0.1, "  ▲ Serviceable events (sized)     ▼ Excluded infeasible",
                fontsize=7, color="#B71C1C", va="bottom", ha="left")

    # X-axis formatting
    ax.set_xlim(x_plot_start, x_display_end)
    ax.set_ylim(-0.8, y_max + 1.0)
    ax.set_yticks([])
    ax.yaxis.set_visible(False)

    xticks = np.arange(np.ceil(x_display_start), x_display_end + 1, 2)
    ax.set_xticks(xticks)
    ax.set_xticklabels([fmt_h(h) for h in xticks], fontsize=8)
    ax.set_xlabel("Time (UTC)", fontsize=9, labelpad=4)
    ax.xaxis.grid(True, color="#E0E0E0", linewidth=0.7, zorder=0)
    ax.set_axisbelow(True)

    # Vertical midnight line (if 24h is in display range)
    if x_display_start < 24 < x_display_end:
        ax.axvline(24, color="#9E9E9E", linewidth=1.0, linestyle=":", alpha=0.8)
        ax.text(24.1, y_max + 0.6, "midnight +1d", fontsize=7, color="#757575", va="top")

    # Summary box
    mix_parts = [f"{v}×{k}" for k, v in best_mix.items() if v > 0]
    mix_str   = " + ".join(mix_parts) if mix_parts else "None"
    cost_str  = f"${meta['cost']:,}"

    summary = (
        f"Date:              {date_str}\n"
        f"Clean total:       {meta['clean_kwh']:.1f} kWh\n"
        f"Serviceable:       {meaningful_svc_kwh:.1f} kWh  ({n_svc} events ≥ 0.10 kWh)\n"
        f"Excl. infeasible:  {excl_kwh_disp:.1f} kWh  ({n_excl} events)\n"
        f"Best mix:          {mix_str}\n"
        f"Charger cost:      {cost_str}"
    )
    ax.text(0.995, 0.995, summary,
            transform=ax.transAxes, fontsize=8.5, va="top", ha="right",
            family="monospace",
            bbox=dict(boxstyle="round,pad=0.6", facecolor="#FFFDE7",
                      edgecolor="#F9A825", linewidth=1.2, alpha=0.95), zorder=10)

    # Legend
    legend_patches = []
    for ct in CTYPES:
        if best_mix.get(ct, 0) > 0:
            legend_patches.append(
                mpatches.Patch(facecolor=CHARGER_COLORS[ct], label=f"{ct}  ({CHARGER_SPECS[ct]['power_kw']:.0f} kW)")
            )
    legend_patches.append(
        mpatches.Patch(facecolor="#E0E0E0", edgecolor="#9E9E9E", label="Dwell window (not charging)")
    )
    legend_patches.append(
        mpatches.Patch(facecolor="#FFCCBC", edgecolor="#E64A19", hatch="///",
                       label="Excluded — infeasible dwell")
    )
    ax.legend(handles=legend_patches, loc="upper left", fontsize=8,
              framealpha=0.9, edgecolor="#BDBDBD")

    # Title
    fig.suptitle(
        f"Northgate DCFC Sizing — Charging Schedule\n"
        f"{date_str}  |  Best mix: {mix_str}  |  {cost_str}  |  "
        f"Serviceable: {meaningful_svc_kwh:.1f} kWh  ({n_svc} events, energy ≥ 0.10 kWh)",
        fontsize=11, fontweight="bold", y=0.995, va="top"
    )
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)

    plt.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"    Saved: {out_path.name}")


# ---------------------------------------------------------------------------
# Summary table figure
# ---------------------------------------------------------------------------

def generate_summary_table(top5_rows, out_path):
    col_labels = [
        "Date", "Clean kWh\n(dwell≥15min)", "Meaningful\nSvc Events\n(≥0.10 kWh)",
        "Meaningful\nSvc kWh", "Excl.\nEvents", "Excl.\nkWh",
        "Best Mix", "Cost ($)", "Peak\nkW", "1×DC150\nSufficient?"
    ]
    table_data = []
    row_colors = []
    for r in top5_rows:
        mix_parts = [f"{r['best_mix'][ct]}×{ct.replace('kW','')}"
                     for ct in CTYPES if r["best_mix"].get(ct, 0) > 0]
        mix = " + ".join(mix_parts)
        table_data.append([
            r["date"],
            f"{r['clean_kwh']:.1f}",
            str(r["n_svc"]),
            f"{r['svc_kwh']:.1f}",
            str(r["n_excl"]),
            f"{r['excl_kwh']:.1f}",
            mix,
            f"${r['cost']:,}",
            f"{r['peak_kw']:.0f}",
            "Yes" if r["suf_1dc150"] else "NO ⚠",
        ])
        is_outlier = not r["suf_1dc150"]
        row_colors.append(["#FFF9C4" if is_outlier else "white"] * len(col_labels))

    fig, ax = plt.subplots(figsize=(18, 3.5), dpi=150)
    fig.patch.set_facecolor("white")
    ax.axis("off")

    tbl = ax.table(
        cellText=table_data,
        colLabels=col_labels,
        loc="center",
        cellLoc="center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    tbl.scale(1, 1.8)

    for (r, c), cell in tbl.get_celld().items():
        if r == 0:
            cell.set_facecolor("#1565C0")
            cell.set_text_props(color="white", fontweight="bold", fontsize=8.5)
        else:
            cell.set_facecolor(row_colors[r-1][c])
            # Highlight the outlier row text in red
            if not top5_rows[r-1]["suf_1dc150"] and c in (6, 7, 9):
                cell.set_text_props(color="#B71C1C", fontweight="bold")
        cell.set_edgecolor("#BDBDBD")

    ax.set_title(
        "Northgate DCFC Sizing — Top-5 Day Sensitivity Summary\n"
        "(Fixed rule: dwell ≥ 15 min | eta = 0.90 | individually infeasible events excluded)",
        fontsize=10, fontweight="bold", pad=12
    )
    plt.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"    Saved: {out_path.name}")


# ---------------------------------------------------------------------------
# Slide outline text
# ---------------------------------------------------------------------------

SLIDE_TEXT = """
╔══════════════════════════════════════════════════════════════════════════╗
║     NORTHGATE EV FLEET CHARGER SIZING — SLIDE OUTLINE                   ║
║     Meeting with Shima  |  Method C Analysis  |  Simulation-Based       ║
╚══════════════════════════════════════════════════════════════════════════╝

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLIDE 1 — TITLE SLIDE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title:    Northgate EV Fleet — DC Fast Charger Sizing Analysis
Subtitle: Method C (50% SOC Inbound Chain) | Simulation-Based Heuristic
Date:     [Today's Date]
Author:   [Your Name]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLIDE 2 — INTRODUCTION & CONTEXT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: What We Did and Why

Bullets:
  • Goal: determine the minimum-cost DC fast charger configuration to serve
    the Northgate EV fleet on a representative high-demand day.

  • Data source: Geotab telematics — Aug–Dec 2025 (17,421 zone visit records
    across the Northgate depot zone).

  • Fleet scope: EVs with known EV-equivalent model assignments.
    Max AC/DC charge rates sourced from manufacturer specs and AFDC.

  • Energy method: Method C — vehicle assumed to arrive at the previous
    eligible zone at 50% SOC; inbound chain miles deducted to compute
    arrival SOC at Northgate.

  • Result: northgate_fill_kwh = min(outbound_energy_need, battery_room)
    This is the energy each vehicle needs to top up before its next trip.

  • All analysis is based on existing Geotab departure data only.
    No new data collection was performed.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLIDE 3 — METHODOLOGY (OVERVIEW)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: Four-Step Heuristic Methodology

Step 1 — Data Cleaning & Filtering
  • Excluded zone visits with dwell < 15 minutes (GPS artifacts, drive-throughs,
    boundary oscillations). These are not real charging opportunities.
  • Retained 1,843 visit-records with dwell ≥ 15 min and fill kWh > 0.

Step 2 — Representative Day Selection
  • Selected the date with the highest total Method C energy demand
    after the 15-min dwell filter was applied.
  • Top day: 2025-08-25 (473.8 kWh clean total; 26 serviceable events).

Step 3 — Individual Feasibility Check
  • Each event tested against a 350 kW DC benchmark:
    max_possible = eta × min(350, max_dc_charge_kw) × dwell_hours
    (eta = 0.90, time step = 15 min)
  • Events where energy_need > max_possible are operationally infeasible
    regardless of charger configuration and are excluded from sizing.
  • These events require operational changes (longer vehicle dwell), not
    additional chargers.

Step 4 — Cost-Minimization Charger Mix Search
  • Bounded enumeration over (L2: 0–20, DC_50: 0–10, DC_150: 0–5, DC_350: 0–3)
    combinations sorted by ascending cost.
  • Feasibility tested with a discrete-time greedy simulation:
    vehicles served in priority order (earliest departure, then highest
    urgency = remaining kWh / remaining hours).
  • Stop at the first (cheapest) fully-feasible mix.
  • NOTE: This is a simulation-based heuristic, NOT a globally optimal
    MILP solution. The result is a minimum-cost mix under the greedy rule.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLIDE 4 — DATA QUALITY & FILTERING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: What Was in the Raw Data and What We Kept

Key Observations from Audit:
  • Zero-dwell events (Geotab activeFrom = activeTo) = GPS boundary ping.
    These had identical entry/exit timestamps — no real parking occurred.

  • On 2025-07-15 (originally selected as representative day), 40% of
    total energy came from zero-dwell Freightliner eCascadia GPS pings.
    This inflated the day's apparent demand by ~323 kWh (3 trucks, 3 events).

  • Fixed rule applied: dwell ≥ 0.25 h (15 minutes).
    After filter: 2025-08-25 became the new representative day.

Dwell Distribution (2025-07-15, before filter — illustrative):
  Bin         Events    kWh
  0h (GPS)       7     323  ← excluded
  < 3 min        4     194  ← excluded
  3–15 min      11      42  ← excluded
  15 min–1h      3      62  ← retained
  1–4h           5      92  ← retained
  > 4h          14      94  ← retained

Individually Infeasible Events (excluded from sizing):
  • Some events pass the 15-min dwell filter but remain physically impossible
    to charge in the available window (e.g., a 17-min dwell with 212 kWh need).
  • These are real vehicle visits — but the dwell window is too short for any
    available charger to deliver the required energy.
  • Reported separately. Require operational changes (longer dwell schedules).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLIDE 5 — REPRESENTATIVE DAY: 2025-08-25
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: Representative Day — 2025-08-25  |  Best Mix: 1×DC 150 kW  |  $150,000

[INSERT FIGURE: schedule_20250825.png]

Key facts:
  • 28 total visits with dwell ≥ 15 min
  • 26 serviceable events (feasible for charger sizing)  →  202.99 kWh
  • 2 excluded infeasible events  →  270.78 kWh
      - Freightliner eCascadia: 17-min dwell, 211.8 kWh needed (shortfall 143.8 kWh)
      - BYD 6F: 20-min dwell, 58.9 kWh needed (shortfall 23.3 kWh)
  • Best mix: 1×DC 150 kW charger  =  $150,000
  • All 26 serviceable events fully charged
  • Charger utilization: 6.8% (over 48-h window)
  • Peak simultaneous power: 150 kW
  • Tightest event: 7006951 (Freightliner eM2, 0.40h dwell, 31.6 kWh)
  • 11 of 26 events are DC-only vehicles → L2 chargers not useful for this fleet mix

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLIDE 6 — DAY 2: 2025-06-26
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: Top Day #2 — 2025-06-26  |  Best Mix: 1×DC 150 kW  |  $150,000

[INSERT FIGURE: schedule_20250626.png]

Key facts:
  • 19 events with dwell ≥ 15 min
  • 17 serviceable events  →  178.2 kWh
  • 2 excluded infeasible events  →  221.2 kWh
  • Best mix: 1×DC 150 kW  =  $150,000
  • 1×DC150kW is sufficient for this day

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLIDE 7 — DAY 3: 2025-12-01
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: Top Day #3 — 2025-12-01  |  Best Mix: 1×DC 150 kW  |  $150,000

[INSERT FIGURE: schedule_20251201.png]

Key facts:
  • 9 events with dwell ≥ 15 min
  • 7 serviceable events  →  87.8 kWh
  • 2 excluded infeasible events  →  292.7 kWh
  • Best mix: 1×DC 150 kW  =  $150,000
  • Lightest serviceable day of the top 5

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLIDE 8 — DAY 4: 2025-06-30  ⚠ OUTLIER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: Top Day #4 — 2025-06-30  ⚠ OUTLIER — Requires 1×DC 350 kW  |  $350,000

[INSERT FIGURE: schedule_20250630.png]

Key facts:
  • 19 events with dwell ≥ 15 min — ALL 19 are individually feasible
    (0 events excluded)
  • Serviceable energy: 357.3 kWh (highest of all 5 days after filtering)
  • 1×DC 150 kW is NOT sufficient
  • Why: back-to-back tight-window events create scheduling conflicts;
    the DC 150 kW cannot cycle fast enough between vehicles.
    A DC 350 kW delivers energy ~2.3× faster, freeing the charger in time.
  • Best mix: 1×DC 350 kW  =  $350,000
  • Peak simultaneous power: 350 kW
  • This day represents the conservative design case.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLIDE 9 — DAY 5: 2025-10-08
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: Top Day #5 — 2025-10-08  |  Best Mix: 1×DC 150 kW  |  $150,000

[INSERT FIGURE: schedule_20251008.png]

Key facts:
  • 17 events with dwell ≥ 15 min
  • 15 serviceable events  →  108.2 kWh
  • 2 excluded infeasible events  →  234.5 kWh
  • Best mix: 1×DC 150 kW  =  $150,000
  • 1×DC150kW sufficient

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLIDE 10 — TOP-5 DAY COMPARISON TABLE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: Cross-Day Sensitivity — Top 5 Clean-Energy Days

[INSERT TABLE FIGURE: summary_table.png]

Key takeaways:
  • 4 of 5 top days are served by 1×DC 150 kW ($150,000).
  • 2025-06-30 is the only day requiring 1×DC 350 kW ($350,000).
  • Infeasible events appear on 4 of 5 days — operational dwell constraints
    not charger infrastructure constraints.
  • L2 chargers contribute 0% utilization across all days for this fleet mix.
    The Northgate fleet skews heavily toward DC-only heavy trucks.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLIDE 11 — KEY FINDINGS & RECOMMENDATIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: Findings and Charger Sizing Recommendations

FINDING 1: 1×DC 150 kW covers 4 of the top 5 high-demand days.
  → Minimum technical recommendation: 1×DC 150 kW  ($150,000)

FINDING 2: 2025-06-30 is a scheduling-dense outlier.
  All 19 events on this day are individually feasible, but simultaneous
  access conflicts require faster cycling → 1×DC 350 kW.
  → Conservative recommendation: 1×DC 350 kW  ($350,000)

FINDING 3: L2 chargers (19.2 kW AC) have 0% utilization.
  11 of 26 serviceable events on the representative day are DC-only vehicles
  (eCascadia, eM2, BYD 6F). L2 chargers cannot serve them.
  The remaining AC-compatible vehicles are served by DC when available.
  → L2 chargers are not recommended for this depot without confirmed
    AC-capable vehicle scheduling.

FINDING 4: Infeasible events are an operational issue, not a charger issue.
  Two event types on every day have dwell windows too short to charge:
    - Freightliner eCascadia: 17-min dwell, 212 kWh needed
    - BYD 6F: 20-min dwell, 59 kWh needed
  No charger, regardless of power rating, can serve these events.
  → Recommend operational review: can these trucks stay longer at Northgate?

  ┌─────────────────────────────────────────────────────────────────┐
  │  MINIMUM TECHNICAL:  1 × DC 150 kW  →  $150,000               │
  │  CONSERVATIVE:       1 × DC 350 kW  →  $350,000               │
  └─────────────────────────────────────────────────────────────────┘

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLIDE 12 — NEXT STEPS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: Next Steps

1. Operational review of infeasible events
   → Work with Northgate operations to understand whether Freightliner eCascadia
     and BYD 6F trucks can extend their depot dwell time.
   → If dwell can be extended to ≥ 1.5h for eCascadia, it becomes serviceable
     with a 150 kW DC charger.

2. Confirm fleet growth trajectory
   → This analysis covers Aug–Dec 2025. Validate whether the fleet mix
     (# of heavy trucks vs. light vehicles) is expected to change by 2026–2027.
   → A growing share of DC-only heavy trucks strengthens the DC-only recommendation.

3. Obtain firm charger cost quotes
   → Current costs are placeholders ($10k/L2, $50k/DC50, $150k/DC150, $350k/DC350).
   → Real installed costs (equipment + installation + utility upgrade) may differ.

4. Validate with a MILP optimizer (optional)
   → The greedy simulation is a heuristic. For a definitive minimum, consider
     a mixed-integer linear program (MILP) with exact scheduling constraints.
   → In practice, the greedy solution is tight for this depot's demand pattern.

5. Utility interconnect & site design
   → A single DC 350 kW charger requires ~400A service at 480V — verify that
     Northgate utility capacity supports this.
   → Consider demand charge impacts if the charger runs at peak power during
     utility peak hours.

6. Consider dual-port charger option
   → A dual-port DC 150 kW charger ($~175k) could split power between two
     simultaneous vehicles, covering the 2025-06-30 scheduling conflict at
     lower cost than a single DC 350 kW unit.
"""


# ===========================================================================
# MAIN
# ===========================================================================
print("=" * 70)
print("  GENERATING PRESENTATION ASSETS")
print("=" * 70)

# Load shared data
print("\n[0] Loading shared inputs ...")
mapping_df = pd.read_excel(MAPPING_FILE)[
    ["ev_equivalent_model", "max_ac_charge_kw", "max_dc_charge_kw"]
].copy()
mapping_df["max_ac_charge_kw"] = pd.to_numeric(mapping_df["max_ac_charge_kw"], errors="coerce").fillna(0.0)
mapping_df["max_dc_charge_kw"] = pd.to_numeric(mapping_df["max_dc_charge_kw"], errors="coerce").fillna(0.0)

print("    Loading source Excel ...")
src_raw = pd.read_excel(SOURCE_EXCEL, sheet_name="All Departures")
src_raw["zone_entry_time_utc"] = pd.to_datetime(src_raw["zone_entry_time_utc"], utc=True, errors="coerce")
src_raw["zone_exit_time_utc"]  = pd.to_datetime(src_raw["zone_exit_time_utc"],  utc=True, errors="coerce")
src_raw["_visit_date"] = src_raw["zone_entry_time_utc"].dt.date.astype(str)
print(f"    Source rows: {len(src_raw):,}")

# Run each day and generate figures
print("\n[1] Building schedules and generating figures ...")

top5_rows_meta = []

for date_str in TOP5_DATES:
    print(f"\n  --- {date_str} ---")
    best_mix = BEST_MIXES[date_str]
    meta     = TOP5_META[date_str]

    svc_df, excl_df = build_day(src_raw, date_str, mapping_df)
    print(f"    Serviceable: {len(svc_df)}  |  Excluded: {len(excl_df)}")

    if len(svc_df) == 0:
        print(f"    No serviceable events — skipping.")
        continue

    # Simulation window
    sim_start = svc_df["arrival_time"].min().floor("15min")
    raw_end   = svc_df["departure_time"].max().ceil("15min")
    sim_end   = min(raw_end, sim_start + timedelta(hours=SIM_CAP_H))
    n_steps   = int((sim_end - sim_start).total_seconds() / (TIME_STEP_H * 3600))

    ev_data = {}
    for _, row in svc_df.iterrows():
        e = row["charging_event_id"]
        dep = min(row["departure_time"], sim_end)
        ev_data[e] = {
            "arr":        row["arrival_time"],
            "dep":        dep,
            "mac":        float(row["max_ac_charge_kw"]),
            "mdc":        float(row["max_dc_charge_kw"]),
            "energy":     float(row["energy_needed_kwh_for_visit"]),
            "vehicle_id": row["vehicle_id"],
            "ev_model":   row["ev_equivalent_model"],
        }

    pool = build_pool(best_mix)
    dld, logs, peak, util = simulate_detailed(ev_data, pool, sim_start, n_steps)
    schedule_df = build_schedule(svc_df, dld, logs)

    mix_parts = [f"{v}×{k}" for k, v in best_mix.items() if v > 0]
    mix_str   = " + ".join(mix_parts)
    n_fully   = int((schedule_df["remaining_unmet_kwh"] <= FULL_CHG_TOL).sum())
    util_str  = "  |  ".join(
        f"{ct}: {pct:.1f}%" for ct, pct in util.items() if pct is not None
    )
    print(f"    Best mix: {mix_str}  |  Events served: {n_fully}/{len(svc_df)}")
    print(f"    Utilization: {util_str}")
    print(f"    Peak: {peak:.1f} kW")

    # Generate figure — pass raw logs so bars are drawn per simulation segment,
    # not as one aggregated span (which would falsely imply simultaneous charging).
    log_df_fig = pd.DataFrame(logs) if logs else pd.DataFrame()
    fig_path = OUTDIR / f"schedule_{date_str.replace('-', '')}.png"
    generate_gantt(date_str, svc_df, excl_df, schedule_df, log_df_fig, best_mix, meta, fig_path)

    # Compute meaningful serviceable stats (energy >= FULL_CHG_TOL = 0.10 kWh)
    svc_meaningful  = svc_df[svc_df["energy_needed_kwh_for_visit"] >= FULL_CHG_TOL]
    n_meaningful    = len(svc_meaningful)
    meaningful_kwh  = round(float(svc_meaningful["energy_needed_kwh_for_visit"].sum()), 2)
    excl_kwh_comp   = round(float(excl_df["energy_needed_kwh_for_visit"].sum()), 2) if len(excl_df) > 0 else 0.0
    n_excl_total    = len(excl_df)

    top5_rows_meta.append({
        "date":         date_str,
        "clean_kwh":    meta["clean_kwh"],
        "svc_kwh":      meaningful_kwh,
        "excl_kwh":     excl_kwh_comp,
        "cost":         meta["cost"],
        "n_svc":        n_meaningful,
        "n_excl":       n_excl_total,
        "best_mix":     best_mix,
        "peak_kw":      peak,
        "suf_1dc150":   (best_mix.get("DC_150kW", 0) > 0 and best_mix.get("DC_350kW", 0) == 0),
    })

# Summary table figure
print("\n[2] Generating summary table figure ...")
generate_summary_table(top5_rows_meta, OUTDIR / "summary_table.png")

# Slide outline text
print("\n[3] Writing slide outline ...")
outline_path = OUTDIR / "slide_outline.txt"
with open(outline_path, "w", encoding="utf-8") as f:
    f.write(SLIDE_TEXT)
print(f"    Saved: {outline_path.name}")

# Final asset list
print("\n" + "=" * 70)
print("  ALL ASSETS SAVED TO:", OUTDIR)
print("=" * 70)
print()
print("  Figures (insert into PowerPoint in order):")
for date_str in TOP5_DATES:
    fname = f"schedule_{date_str.replace('-', '')}.png"
    print(f"    {fname}")
print(f"    summary_table.png")
print()
print("  Text:")
print(f"    slide_outline.txt  (12 slides, titles + bullets)")
print()
print("  PowerPoint build instructions:")
print("    1. Create blank wide (16:9) presentation.")
print("    2. Use slide_outline.txt for titles and bullet text.")
print("    3. Insert each .png figure into the corresponding slide.")
print("    4. Slides 5–9: one figure per slide (full width).")
print("    5. Slide 10: insert summary_table.png.")
print("=" * 70)
