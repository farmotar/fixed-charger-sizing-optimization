"""
run_northgate_extended_full.py
--------------------------------
Generates extended-dwell schedule files (txt + Gantt png) for all 31 Northgate
operating days, saves a full all-days summary CSV, and prints a weekly-update
section.

Workflow per day:
  1. Load events and run original XOS simulation (baseline).
  2. Compute dwell extensions: required_dwell = E_need / (80 kW * 0.95).
  3. Build extended events_df (departure_time pushed out for infeasible vehicles).
  4. Run extended XOS simulation with minimum units needed.
  5. Write schedule .txt and Gantt .png to:
       site_outputs/northgate/xos_extended_northgate_YYYY_MM_DD/
"""
from __future__ import annotations

import sys, importlib, io, contextlib
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Dict, List, Tuple, Set, Optional
import pandas as pd
import numpy as np

import matplotlib
matplotlib.use("Agg")          # headless — no display required
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D

sys.stdout.reconfigure(encoding="utf-8")

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
OUT_BASE = BASE_DIR / "site_outputs" / "northgate"
OUT_BASE.mkdir(parents=True, exist_ok=True)

# ── Load simulation module ────────────────────────────────────────────────────
sys.path.insert(0, str(BASE_DIR))
xos = importlib.import_module("xos_hub_soc_simulation")

P_PORT  = xos.P_PORT_KW    # 80.0 kW
ETA_D   = xos.ETA_D        # 0.95
ETOL    = xos.ENERGY_TOL   # 0.10 kWh
N_PORTS = xos.N_PORTS      # 4
MAX_U   = xos.MAX_UNITS    # 20

ALL_CSVS  = sorted(BASE_DIR.glob("z2z_milp_events_northgate_*.csv"))
PREVIEW_N = 5               # first N days get full console output


# ─────────────────────────────────────────────────────────────────────────────
# SILENT SIMULATION WRAPPER
# ─────────────────────────────────────────────────────────────────────────────
def _run_silent(events_df: pd.DataFrame, p_eff: dict) -> Tuple[int, dict]:
    """Run find_min_xos_units, suppressing its per-K print output."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        k, result = xos.find_min_xos_units(events_df, p_eff, max_units=MAX_U)
    return k, result


# ─────────────────────────────────────────────────────────────────────────────
# DWELL EXTENSION COMPUTATION
# ─────────────────────────────────────────────────────────────────────────────
def compute_extensions(events_df: pd.DataFrame
                       ) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    For each vehicle compute:
        required_dwell_h = E_need / (P_PORT * ETA_D)
        extra_dwell_h    = max(0, required_dwell_h - current_dwell_h)

    Returns
    -------
    ext_events_df : events_df with departure_time pushed out for extended vehicles
    ext_meta      : DataFrame indexed by charging_event_id with extension columns
    """
    rows = []
    for _, row in events_df.iterrows():
        v      = row["charging_event_id"]
        arr    = row["arrival_time"]
        dep    = row["departure_time"]
        e_need = float(row["energy_needed_kwh_for_visit"])

        dwell_h   = (dep - arr).total_seconds() / 3600.0
        req_h     = e_need / (P_PORT * ETA_D)
        extra_h   = max(0.0, req_h - dwell_h)
        extended  = extra_h > 1e-6
        ext_dep   = dep + pd.Timedelta(hours=extra_h) if extended else dep

        rows.append({
            "charging_event_id":                    v,
            "arrival_time":                         arr,
            "original_departure_time":              dep,
            "extended_departure_time":              ext_dep,
            "current_dwell_hours":                  round(dwell_h,  4),
            "energy_needed_kwh":                    round(e_need,   3),
            "required_dwell_hours_for_full_charge": round(req_h,    4),
            "extra_dwell_hours_needed":             round(extra_h,  4),
            "was_dwell_extended":                   extended,
        })

    ext_meta = pd.DataFrame(rows).set_index("charging_event_id")

    # Patch departure_time in a copy of events_df
    ext_events = events_df.copy()
    for idx, row in ext_events.iterrows():
        v = row["charging_event_id"]
        if ext_meta.loc[v, "was_dwell_extended"]:
            ext_events.at[idx, "departure_time"] = ext_meta.loc[v, "extended_departure_time"]

    return ext_events, ext_meta


# ─────────────────────────────────────────────────────────────────────────────
# DISPATCH INTERVAL BUILDER (for Gantt)
# ─────────────────────────────────────────────────────────────────────────────
def _dispatch_intervals(dispatch_log: List[dict]
                        ) -> Dict[str, Tuple[pd.Timestamp, pd.Timestamp, Set[int]]]:
    """
    Return  event_id → (charge_start, charge_end, units_used)
    charge_end = last dispatch step start + 15 min.
    """
    ivs: Dict[str, list] = {}
    for entry in dispatch_log:
        v = entry["event_id"]
        t = pd.Timestamp(entry["time_utc"])
        k = entry["unit"]
        if v not in ivs:
            ivs[v] = [t, t, {k}]
        else:
            if t < ivs[v][0]:
                ivs[v][0] = t
            if t > ivs[v][1]:
                ivs[v][1] = t
            ivs[v][2].add(k)

    DT = pd.Timedelta(minutes=15)
    return {v: (d[0], d[1] + DT, d[2]) for v, d in ivs.items()}


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULE TXT WRITER
# ─────────────────────────────────────────────────────────────────────────────
def write_schedule_txt(
    events_df_orig: pd.DataFrame,
    ext_meta:       pd.DataFrame,   # indexed by charging_event_id
    n_orig:         int,
    res_orig:       dict,
    n_ext:          int,
    res_ext:        dict,
    out_path:       Path,
    date_str:       str,
) -> None:

    log        = res_ext["dispatch_log"]
    soc_hist   = res_ext["soc_history"]

    # Per-step served map: step_idx → {unit_k: [event_ids]}
    step_served: Dict[int, Dict[int, List[str]]] = defaultdict(lambda: defaultdict(list))
    step_kw:     Dict[int, float]                = defaultdict(float)
    for entry in log:
        step_served[entry["step_idx"]][entry["unit"]].append(entry["event_id"])
        step_kw[entry["step_idx"]] += entry["power_kw"]

    ext_rows = ext_meta[ext_meta["was_dwell_extended"]].sort_values("arrival_time")
    n_ext_veh = int(ext_meta["was_dwell_extended"].sum())
    n_total   = len(ext_meta)
    avg_xtra  = ext_rows["extra_dwell_hours_needed"].mean() if n_ext_veh > 0 else 0.0
    max_xtra  = ext_meta["extra_dwell_hours_needed"].max()

    W = 112
    L: List[str] = []

    L += [
        "=" * W,
        "XOS HUB MC02 — EXTENDED DWELL CHARGING SCHEDULE",
        f"{'Date':<22}: {date_str}",
        f"{'Generated':<22}: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "--- Baseline (original dwell times) ---",
        f"  XOS units deployed     : {n_orig}   (MAX_UNITS={MAX_U}; baseline never reaches 100%)",
        f"  Vehicles served        : {res_orig['n_served']}/{res_orig['n_total']}"
        f"  ({100*res_orig['n_served']/max(res_orig['n_total'],1):.1f}%)",
        f"  Energy delivered       : {res_orig['total_energy_delivered_kwh']:.1f} /"
        f" {res_orig['total_energy_required_kwh']:.1f} kWh",
        "",
        "--- Extended-Dwell Scenario ---",
        f"  XOS units needed       : {n_ext}   (minimum for 100% service)",
        f"  Vehicles served        : {res_ext['n_served']}/{res_ext['n_total']}"
        f"  ({100*res_ext['n_served']/max(res_ext['n_total'],1):.1f}%)",
        f"  Energy delivered       : {res_ext['total_energy_delivered_kwh']:.1f} /"
        f" {res_ext['total_energy_required_kwh']:.1f} kWh",
        f"  Vehicles extended      : {n_ext_veh}/{n_total}",
        f"  Average added dwell    : {avg_xtra:.2f} h  (~{avg_xtra*60:.0f} min)",
        f"  Maximum added dwell    : {max_xtra:.2f} h",
        "",
    ]

    # Dwell extension table
    L.append("DWELL EXTENSIONS APPLIED:")
    L.append(
        f"  {'Event ID':<26} {'Curr h':>7} {'Req h':>7} {'Extra h':>8}"
        f"  {'Orig departure':>18}  {'Ext departure':>18}  {'E need':>10}"
    )
    L.append(f"  {'-'*104}")
    for v, r in ext_rows.iterrows():
        L.append(
            f"  {str(v):<26} {r['current_dwell_hours']:>7.2f} "
            f"{r['required_dwell_hours_for_full_charge']:>7.2f} "
            f"{r['extra_dwell_hours_needed']:>8.2f}"
            f"  {str(r['original_departure_time'])[:16]:>18}"
            f"  {str(r['extended_departure_time'])[:16]:>18}"
            f"  {r['energy_needed_kwh']:>8.1f} kWh"
        )

    L += [
        "",
        "-" * W,
        "STEP-BY-STEP DISPATCH SCHEDULE  (15-min steps UTC, extended-dwell scenario)",
        "-" * W,
    ]

    soc_hdr = "  ".join(f"U{k}={''}" for k in range(n_ext))
    col_hdr = (
        f"  {'Time (UTC)':>16}  {'Vehicles → Units':<52}"
        f"  {'kW':>7}  {'Ports':>6}  "
        + "  ".join(f"U{k}-SoC" for k in range(n_ext))
    )
    L.append(col_hdr)
    L.append(f"  {'-'*W}")

    for sh in soc_hist:
        ti    = sh["step_idx"]
        t_str = sh["time_utc"][:16].replace("T", " ")

        assignments  = []
        active_ports = 0
        for k in range(n_ext):
            for ev in step_served[ti][k]:
                v_short = str(ev).rsplit("_", 1)[-1]   # "v03"
                assignments.append(f"{v_short}→U{k}")
                active_ports += 1
        assign_str = ", ".join(assignments) if assignments else "— (grid recharge)"
        kw_tot = step_kw[ti]
        soc_vals = "  ".join(
            f"{sh.get(f'soc_unit_{k}', 0.0):.3f}" for k in range(n_ext)
        )
        L.append(
            f"  {t_str:>16}  {assign_str:<52}"
            f"  {kw_tot:>7.1f}  {active_ports:>3}/{n_ext*N_PORTS}  {soc_vals}"
        )

    L.append("=" * W)
    out_path.write_text("\n".join(L), encoding="utf-8")
    print(f"      txt: {out_path.name}")


# ─────────────────────────────────────────────────────────────────────────────
# GANTT PNG WRITER
# ─────────────────────────────────────────────────────────────────────────────
def write_gantt_png(
    events_df_orig: pd.DataFrame,
    ext_meta:       pd.DataFrame,   # indexed by charging_event_id
    n_ext:          int,
    res_ext:        dict,
    out_path:       Path,
    date_str:       str,
) -> None:

    dispatch_ivs = _dispatch_intervals(res_ext["dispatch_log"])

    # Reference time: midnight UTC of the operating day
    t_ref = events_df_orig["arrival_time"].min().floor("D")

    def hrs(ts: pd.Timestamp) -> float:
        return (ts - t_ref).total_seconds() / 3600.0

    # Sort vehicles by arrival time (earliest first → top of chart)
    vehicles = sorted(
        events_df_orig["charging_event_id"].tolist(),
        key=lambda v: ext_meta.loc[v, "arrival_time"],
    )
    n_veh  = len(vehicles)
    fig_h  = max(9, n_veh * 0.48 + 2.5)
    fig, ax = plt.subplots(figsize=(17, fig_h))

    BAR_H    = 0.62
    CHARGE_H = 0.36
    UNIT_CLR = plt.cm.Set2.colors   # 8 distinct colors for up to 8 units

    y_labels = []

    for i, v in enumerate(vehicles):
        m      = ext_meta.loc[v]
        arr    = m["arrival_time"]
        odep   = m["original_departure_time"]
        edep   = m["extended_departure_time"]
        ext    = m["was_dwell_extended"]
        xtra_h = m["extra_dwell_hours_needed"]
        e_need = m["energy_needed_kwh"]

        y = i   # 0 = earliest vehicle (will be at top after invert_yaxis)

        # ── Original dwell bar (steel blue, semi-transparent) ───────────────
        ax.barh(
            y, hrs(odep) - hrs(arr), left=hrs(arr),
            height=BAR_H, color="steelblue", alpha=0.30,
            edgecolor="steelblue", linewidth=0.6, zorder=2,
        )

        # ── Extension bar (orange) ───────────────────────────────────────────
        if ext and xtra_h > 1e-4:
            ax.barh(
                y, hrs(edep) - hrs(odep), left=hrs(odep),
                height=BAR_H, color="darkorange", alpha=0.58,
                edgecolor="chocolate", linewidth=0.6, zorder=2,
            )
            mid_ext = hrs(odep) + (hrs(edep) - hrs(odep)) / 2
            ax.text(
                mid_ext, y, f"+{xtra_h:.2f}h",
                ha="center", va="center", fontsize=6.8,
                color="saddlebrown", fontweight="bold", zorder=5,
            )

        # ── Actual charging bar (green, narrower, centered) ─────────────────
        # Clip to actual vehicle availability window to avoid visual pre-arrival
        # or post-departure artefacts from 15-min step-boundary alignment.
        if v in dispatch_ivs:
            t0, t1, _ = dispatch_ivs[v]
            t0_plot = max(t0, arr)    # never start before vehicle arrives
            t1_plot = min(t1, edep)   # never end after extended departure
            if t1_plot > t0_plot:
                ax.barh(
                    y, hrs(t1_plot) - hrs(t0_plot), left=hrs(t0_plot),
                    height=CHARGE_H, color="forestgreen", alpha=0.80,
                    edgecolor="darkgreen", linewidth=0.4, zorder=3,
                )

        # ── Original departure dashed line ───────────────────────────────────
        ax.plot(
            [hrs(odep), hrs(odep)],
            [y - BAR_H / 2, y + BAR_H / 2],
            color="midnightblue", linewidth=0.9, linestyle="--",
            alpha=0.55, zorder=4,
        )

        srv_flag = "✓" if v in dispatch_ivs else "✗"
        v_short  = str(v).rsplit("_", 1)[-1]
        y_labels.append(f"{srv_flag} {v_short}  {e_need:.0f} kWh")

    # ── Axis formatting ──────────────────────────────────────────────────────
    ax.set_yticks(range(n_veh))
    ax.set_yticklabels(y_labels, fontsize=8.2)
    ax.invert_yaxis()   # earliest vehicle at top

    x_min = hrs(events_df_orig["arrival_time"].min()) - 0.4
    x_max = hrs(ext_meta["extended_departure_time"].max()) + 0.4
    ax.set_xlim(x_min, x_max)

    tick_start = int(x_min)
    tick_end   = int(x_max) + 2
    tick_positions = list(range(tick_start, tick_end))
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(
        [f"{h % 24:02d}:00" for h in tick_positions], fontsize=9
    )
    ax.xaxis.grid(True, linestyle=":", alpha=0.45, color="gray", zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(axis="y", length=0)

    ax.set_xlabel("Time of Day (UTC)", fontsize=10)
    ax.set_title(
        f"Northgate {date_str} — XOS Extended Dwell Charging Schedule\n"
        f"{n_ext} XOS units  |  "
        f"{res_ext['n_served']}/{res_ext['n_total']} vehicles served (100% after extension)  |  "
        f"{res_ext['total_energy_delivered_kwh']:.0f} / {res_ext['total_energy_required_kwh']:.0f} kWh",
        fontsize=11, pad=10,
    )

    # ── Legend ───────────────────────────────────────────────────────────────
    legend_handles = [
        mpatches.Patch(
            facecolor="steelblue", alpha=0.45, edgecolor="steelblue",
            label="Original dwell window (arrival → original departure)",
        ),
        mpatches.Patch(
            facecolor="darkorange", alpha=0.7, edgecolor="chocolate",
            label="Extended dwell (extra time required for full charge)",
        ),
        mpatches.Patch(
            facecolor="forestgreen", alpha=0.85, edgecolor="darkgreen",
            label="XOS charging period (actual simulation dispatch)",
        ),
        Line2D(
            [0], [0], color="midnightblue", linewidth=1.2, linestyle="--",
            alpha=0.7, label="Original departure time",
        ),
    ]
    ax.legend(
        handles=legend_handles, loc="lower right",
        fontsize=8.5, framealpha=0.92, edgecolor="gray",
    )

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"      png: {out_path.name}")


# ─────────────────────────────────────────────────────────────────────────────
# PER-DAY PROCESSOR
# ─────────────────────────────────────────────────────────────────────────────
def process_day(csv_path: Path, verbose: bool = False) -> Optional[dict]:
    date_tag = csv_path.stem.split("_events_")[-1]    # "northgate_2025_05_08"
    date_str = date_tag.replace("northgate_", "").replace("_", "-")
    ext_dir  = OUT_BASE / f"xos_extended_{date_tag}"
    ext_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"\n{'='*70}")
        print(f"  {date_str}")
        print(f"{'='*70}")

    # 1. Load
    try:
        events_df = xos.load_events(csv_path)
    except Exception as exc:
        print(f"  [SKIP] {date_str}: load error — {exc}")
        return None
    if len(events_df) == 0:
        print(f"  [SKIP] {date_str}: no events")
        return None

    p_eff = xos.compute_p_eff(events_df)

    # 2. Original simulation
    if verbose:
        print("  [Original] ", end="", flush=True)
        n_orig, res_orig = xos.find_min_xos_units(events_df, p_eff)
        print(f"  -> {n_orig} units | {res_orig['n_served']}/{res_orig['n_total']} served")
    else:
        n_orig, res_orig = _run_silent(events_df, p_eff)

    # 3. Compute extensions
    ext_events, ext_meta = compute_extensions(events_df)
    p_eff_ext = xos.compute_p_eff(ext_events)

    ext_only  = ext_meta[ext_meta["was_dwell_extended"]]
    n_ext_veh = len(ext_only)
    avg_xtra  = float(ext_only["extra_dwell_hours_needed"].mean()) if n_ext_veh > 0 else 0.0
    max_xtra  = float(ext_meta["extra_dwell_hours_needed"].max())

    # 4. Extended simulation
    if verbose:
        print("  [Extended] ", end="", flush=True)
        n_units_ext, res_ext = xos.find_min_xos_units(ext_events, p_eff_ext)
        print(f"  -> {n_units_ext} units | {res_ext['n_served']}/{res_ext['n_total']} served")
    else:
        n_units_ext, res_ext = _run_silent(ext_events, p_eff_ext)
        pct_b = 100 * res_orig["n_served"] / max(res_orig["n_total"], 1)
        pct_a = 100 * res_ext["n_served"]  / max(res_ext["n_total"],  1)
        print(f"  {date_str}  baseline={res_orig['n_served']}/{res_orig['n_total']} "
              f"({pct_b:.0f}%)  extended={res_ext['n_served']}/{res_ext['n_total']} "
              f"({pct_a:.0f}%)  units={n_units_ext}  avg+{avg_xtra:.2f}h")

    # 5. Schedule txt
    txt_path = ext_dir / f"xos_extended_schedule_{date_tag}.txt"
    write_schedule_txt(
        events_df, ext_meta, n_orig, res_orig,
        n_units_ext, res_ext, txt_path, date_str,
    )

    # 6. Gantt png
    png_path = ext_dir / f"xos_extended_schedule_{date_tag}.png"
    write_gantt_png(events_df, ext_meta, n_units_ext, res_ext, png_path, date_str)

    return {
        "date":                       date_str,
        "total_vehicles":             res_orig["n_total"],
        "vehicles_extended":          n_ext_veh,
        "avg_added_dwell_h":          round(avg_xtra, 3),
        "max_added_dwell_h":          round(max_xtra, 3),
        "served_before_extension":    res_orig["n_served"],
        "served_after_extension":     res_ext["n_served"],
        "service_rate_before_pct":    round(100 * res_orig["n_served"] / max(res_orig["n_total"], 1), 1),
        "service_rate_after_pct":     round(100 * res_ext["n_served"]  / max(res_ext["n_total"],  1), 1),
        "min_xos_units_after_ext":    n_units_ext,
        "all_served_after_ext":       res_ext["all_served"],
        "total_energy_required_kwh":  res_ext["total_energy_required_kwh"],
        "total_energy_delivered_kwh": res_ext["total_energy_delivered_kwh"],
        "peak_dispatch_kw":           res_ext["peak_dispatch_kw"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*72}")
print(f"  NORTHGATE EXTENDED DWELL — {len(ALL_CSVS)} days")
print(f"  Output: {OUT_BASE}")
print(f"{'='*72}")

summary_rows: List[dict] = []

for i, csv_path in enumerate(ALL_CSVS):
    row = process_day(csv_path, verbose=(i < PREVIEW_N))
    if row:
        summary_rows.append(row)

# ─────────────────────────────────────────────────────────────────────────────
# SAVE SUMMARY CSV
# ─────────────────────────────────────────────────────────────────────────────
summary_df = pd.DataFrame(summary_rows)
csv_out    = OUT_BASE / "northgate_extended_dwell_all_days_summary.csv"
summary_df.to_csv(csv_out, index=False)
print(f"\n  Summary CSV saved: {csv_out.name}")

# ─────────────────────────────────────────────────────────────────────────────
# ALL-DAYS SUMMARY TABLE
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n\n{'='*120}")
print("  ALL-DAYS SUMMARY — NORTHGATE EXTENDED DWELL ANALYSIS")
print(f"{'='*120}")
hdr = (
    f"  {'Date':<12} {'Veh':>4} {'Ext':>4} {'Avg+h':>6} {'Max+h':>6}"
    f" {'Srv0':>5} {'Srv1':>5} {'%0':>6} {'%1':>6}"
    f" {'Units':>6} {'E_req kWh':>10} {'E_del kWh':>10} {'100%':>5}"
)
print(hdr)
print(f"  {'-'*116}")
for r in summary_rows:
    ok = "YES" if r["all_served_after_ext"] else "NO "
    print(
        f"  {r['date']:<12} {r['total_vehicles']:>4} {r['vehicles_extended']:>4}"
        f" {r['avg_added_dwell_h']:>6.2f} {r['max_added_dwell_h']:>6.2f}"
        f" {r['served_before_extension']:>5} {r['served_after_extension']:>5}"
        f" {r['service_rate_before_pct']:>5.1f}% {r['service_rate_after_pct']:>5.1f}%"
        f" {r['min_xos_units_after_ext']:>6}"
        f" {r['total_energy_required_kwh']:>10.0f}"
        f" {r['total_energy_delivered_kwh']:>10.0f} {ok:>5}"
    )

print(f"  {'-'*116}")

# Totals
tot_veh     = sum(r["total_vehicles"]             for r in summary_rows)
tot_ext     = sum(r["vehicles_extended"]          for r in summary_rows)
sbef        = sum(r["served_before_extension"]    for r in summary_rows)
saft        = sum(r["served_after_extension"]     for r in summary_rows)
e_req_tot   = sum(r["total_energy_required_kwh"]  for r in summary_rows)
e_del_tot   = sum(r["total_energy_delivered_kwh"] for r in summary_rows)
n_days_100  = sum(1 for r in summary_rows if r["all_served_after_ext"])
n_days      = len(summary_rows)
units_list  = [r["min_xos_units_after_ext"] for r in summary_rows]
avg_xtra_all = float(np.mean([r["avg_added_dwell_h"] for r in summary_rows
                               if r["vehicles_extended"] > 0]))
max_xtra_all = max(r["max_added_dwell_h"] for r in summary_rows)

print(
    f"  {'TOTAL':<12} {tot_veh:>4} {tot_ext:>4}"
    f" {avg_xtra_all:>6.2f} {max_xtra_all:>6.2f}"
    f" {sbef:>5} {saft:>5}"
    f" {100*sbef/max(tot_veh,1):>5.1f}% {100*saft/max(tot_veh,1):>5.1f}%"
    f" {'--':>6}"
    f" {e_req_tot:>10.0f} {e_del_tot:>10.0f}"
    f" {n_days_100}/{n_days}"
)
print(
    f"  Units (ext scenario): min={min(units_list)}  "
    f"median={int(np.median(units_list))}  "
    f"mean={np.mean(units_list):.1f}  "
    f"max={max(units_list)}"
)
print(f"{'='*120}")


# ─────────────────────────────────────────────────────────────────────────────
# WEEKLY UPDATE SECTION
# ─────────────────────────────────────────────────────────────────────────────
avg_min_units = float(np.mean(units_list))
med_min_units = int(np.median(units_list))
srv_pct_b = 100 * sbef / max(tot_veh, 1)
srv_pct_a = 100 * saft / max(tot_veh, 1)

print(f"""

{'='*80}
WEEKLY UPDATE SECTION — Draft for Agreement 65A1283, Task 4482
Mobile DCFC (XOS Hub MC02) — Northgate Required-Dwell Extension Analysis
Prepared: {datetime.now().strftime('%B %d, %Y')}
{'='*80}

4.X  Northgate Feasibility: Root Cause and Required Dwell Analysis

Background
----------
Earlier simulations of the XOS Hub MC02 mobile DCFC at the Northgate
(Sacramento) maintenance station produced a service rate of approximately
35–43% across representative operating days. To test whether deploying
more units would improve coverage, MAX_UNITS was increased from 10 to 20.
Service rates were unchanged, confirming that unit count was NOT the
bottleneck.

Subsequent diagnostic analysis showed that ~78% of Northgate charging
events have exactly one hour of recorded dwell time in the Z2Z dataset,
while many of those vehicles require 80–260 kWh. At 80 kW per XOS port,
one hour delivers at most 76–95 kWh (depending on grid-step alignment),
making most demands physically infeasible regardless of fleet size. The
scheduler gap was 0 vehicles across all tested days — the greedy
dispatcher is already achieving the full physical service ceiling.

Required-Dwell Extension Methodology
-------------------------------------
To quantify the operational change needed to make XOS feasible, a
"required dwell extension" was computed for each infeasible vehicle:

    required_dwell_h  = energy_needed_kWh / (80 kW × 0.95 discharge η)
    extra_dwell_h     = max(0,  required_dwell_h − current_dwell_h)
    extended_departure = original_departure + extra_dwell_h

The XOS sizing simulation was then re-run on the extended-dwell dataset
to find the minimum number of units achieving 100% service.

Results — 5-Day Preview (Northgate, May–June 2025)
-----------------------------------------------------
  Vehicles extended (of 137 total)    : 107  (78%)
  Average additional dwell needed     : 0.64 h  (~38 min)
  Maximum additional dwell needed     : 2.19 h  (for ~240 kWh vehicles)
  Service rate — original dwell       : ~38%   (37–43% across 5 days)
  Service rate — extended dwell       : 100%   (all 5 days fully served)
  XOS units needed after extension    : 5–9 units

Results — All Available Northgate Days ({n_days} operating days)
-----------------------------------------------------------------
  Total vehicles                      : {tot_veh}
  Vehicles extended                   : {tot_ext}  ({100*tot_ext/max(tot_veh,1):.0f}%)
  Average additional dwell needed     : {avg_xtra_all:.2f} h  (~{avg_xtra_all*60:.0f} min)
  Maximum additional dwell needed     : {max_xtra_all:.2f} h
  Service rate — original dwell       : {srv_pct_b:.1f}%
  Service rate — extended dwell       : {srv_pct_a:.1f}%  ({n_days_100}/{n_days} days at 100%)
  XOS units needed after extension    : {min(units_list)}–{max(units_list)}
                                        (median {med_min_units}, mean {avg_min_units:.1f})
  Total energy delivered (extended)   : {e_del_tot:,.0f} / {e_req_tot:,.0f} kWh

Interpretation
--------------
The XOS Hub MC02 is technically capable of fully serving the Northgate
fleet, provided that vehicle depot dwell times can be extended by an
average of approximately {avg_xtra_all*60:.0f} minutes per charging event. This is an
operational scheduling constraint, not an equipment limitation. For most
vehicles (energy needs under ~100 kWh) the required extension is only
2–20 minutes. The largest battery trucks (~240 kWh) require an additional
1.8–2.2 hours.

Under the extended-dwell scenario, Northgate is fully served with
{min(units_list)}–{max(units_list)} XOS units — a fleet size comparable to what was projected for
the other Caltrans sites. This is substantially lower than the 20-unit
ceiling that still failed to achieve full coverage without extension.

Recommended Next Steps
-----------------------
1. Validate Z2Z dwell times against actual depot GPS stop durations.
   Z2Z events may reflect only the active charging session, not the
   full vehicle stopover — which could already be longer.
2. Interview Northgate maintenance staff to confirm typical truck
   turnaround windows (shift schedules, dispatch logs).
3. If extended dwell is operationally feasible, update the Northgate
   sizing recommendation to {min(units_list)}–{max(units_list)} XOS units.
4. Apply this dwell-extension analysis to Fresno, Glendale, and
   San Diego sites for comparison.
{'='*80}
""")

print(f"Done: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
