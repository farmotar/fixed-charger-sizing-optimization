"""
northgate_worst_days.py
=======================
Top-10 worst Northgate days (by K used, then vehicle count) + Shima analysis:
  For each worst-day K, run ALL 307 days at that fixed K and report coverage.

Outputs
-------
  scenario_outputs/northgate_analysis/worst_days/
    worst_day_report_card_{date}.png  — per-day 4-panel figure
    coverage_analysis.csv             — 10 rows × all-days coverage stats
    worst_days_report.txt             — printable summary
"""
from __future__ import annotations

import io, sys, math, contextlib
from pathlib import Path
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import matplotlib.gridspec as gridspec
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))
import scenario_runner as sr

# ── paths ──────────────────────────────────────────────────────────────────────
BASE_DIR  = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
PER_DAY   = BASE_DIR / "scenario_outputs" / "northgate_analysis" / "per_day"
OUT_DIR   = BASE_DIR / "scenario_outputs" / "northgate_analysis" / "worst_days"
CSV_STEM  = "z2z_milp_events_northgate"
TZ        = "America/Los_Angeles"
NP        = 4       # CCS1 ports per XOS hub
DT_MIN    = 15
ENERGY_TOL = 0.05

STATE_COLOR = {
    "idle":       ("#cccccc", 0.55),
    "serving":    ("#b8d4f0", 0.45),
    "recharging": ("#90e090", 0.70),
}

# ── helpers ────────────────────────────────────────────────────────────────────

def _vid_label(eid: str, date_str: str) -> str:
    parts = eid.split("_")
    date_compact   = parts[-2]
    num            = int(parts[-1][1:])
    target_compact = date_str.replace("-", "")
    suffix = "p" if date_compact < target_compact else ""
    return f"V{num}{suffix}"


def _hub_state_blocks(state_df: pd.DataFrame, hub_k: int, to_x):
    col = f"state_unit_{hub_k}"
    if col not in state_df.columns:
        return []
    times  = pd.to_datetime(state_df["time_utc"], utc=True)
    states = state_df[col].tolist()
    blocks = []
    i = 0
    while i < len(states):
        s = states[i]; j = i + 1
        while j < len(states) and states[j] == s:
            j += 1
        blocks.append((to_x(times.iloc[i]), to_x(times.iloc[j - 1]) + DT_MIN, s))
        i = j
    return blocks


def _load_events(date_str: str) -> pd.DataFrame | None:
    date_tag = date_str.replace("-", "_")
    csv_path = BASE_DIR / f"{CSV_STEM}_{date_tag}.csv"
    if not csv_path.exists():
        return None
    stem_parts    = csv_path.stem.rsplit("_", 3)
    site_csv_stem = "_".join(stem_parts[:-3]) if len(stem_parts) > 3 else csv_path.stem
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            events = sr.load_site_day_data(csv_path)
            events = sr.apply_multiday_rule(events, date_str,
                                            site_csv_dir=csv_path.parent,
                                            site_csv_stem=site_csv_stem)
        return sr._xos_extended_dwell(events)
    except Exception:
        return None


def _all_csv_paths() -> list[Path]:
    return sorted(BASE_DIR.glob(f"{CSV_STEM}_*.csv"))


def _run_fixed_k(events_ext: pd.DataFrame, K_fixed: int, mode: str) -> dict | None:
    """Run simulation with exactly K_fixed hubs; return summary stats."""
    if events_ext is None or events_ext.empty:
        return None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            sim = sr._simulate_xos(events_ext, K_fixed, mode=mode)
        delivered = sim.get("delivered", {})
        remaining = sim.get("remaining", {})
        n_total    = sim.get("n_vehicles", len(delivered))
        n_full     = sum(1 for v in delivered if remaining.get(v, 0) <= ENERGY_TOL)
        n_partial  = sum(1 for v in delivered
                         if delivered[v] > ENERGY_TOL and remaining.get(v, 0) > ENERGY_TOL)
        n_unserved = sum(1 for v in delivered if delivered[v] <= ENERGY_TOL)
        e_del      = sum(delivered.values())
        e_unmet    = sum(r for r in remaining.values() if r > ENERGY_TOL)
        e_dem      = e_del + e_unmet
        return {
            "K_used":      K_fixed,
            "n_vehicles":  n_total,
            "n_full":      n_full,
            "n_partial":   n_partial,
            "n_unserved":  n_unserved,
            "svc_rate":    100 * n_full / n_total if n_total else 100.0,
            "e_demanded":  e_dem,
            "e_delivered": e_del,
            "e_unmet":     max(0.0, e_unmet),
        }
    except Exception:
        return None


# ── Shima's coverage analysis ─────────────────────────────────────────────────

def shima_coverage_analysis(top10_rows: list[dict], all_csv_paths: list[Path]) -> list[dict]:
    """
    For each worst-day row, fix K = row['K'] and run ALL days.
    Returns list of coverage result dicts (one per worst day).
    """
    results = []
    n_days  = len(all_csv_paths)

    for rank, row in enumerate(top10_rows, 1):
        K_fixed   = int(row["K"])
        worst_date = row["date"]
        print(f"  Rank {rank:2d} | K={K_fixed:2d} (designed for {worst_date}) — running {n_days} days ...", flush=True)

        fully_covered     = 0
        partially_covered = 0
        uncovered         = 0
        total_vehicles    = 0
        total_delivered   = 0
        total_demanded    = 0

        for csv_path in all_csv_paths:
            date_tag  = csv_path.stem.split("northgate_")[-1]
            d_str     = date_tag.replace("_", "-")[:10]
            stem_parts    = csv_path.stem.rsplit("_", 3)
            site_csv_stem = "_".join(stem_parts[:-3]) if len(stem_parts) > 3 else csv_path.stem
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    ev = sr.load_site_day_data(csv_path)
                    ev = sr.apply_multiday_rule(ev, d_str,
                                                site_csv_dir=csv_path.parent,
                                                site_csv_stem=site_csv_stem)
                    ev = sr._xos_extended_dwell(ev)
                    sim_r = _run_fixed_k(ev, K_fixed, "a2")
                if sim_r is None:
                    continue
                if sim_r["n_unserved"] + sim_r["n_partial"] == 0:
                    fully_covered += 1
                elif sim_r["n_unserved"] > 0:
                    uncovered += 1
                else:
                    partially_covered += 1
                total_vehicles  += sim_r["n_vehicles"]
                total_delivered += sim_r["e_delivered"]
                total_demanded  += sim_r["e_demanded"]
            except Exception:
                pass

        pct_full = 100 * fully_covered / n_days if n_days else 0
        pct_partial = 100 * partially_covered / n_days if n_days else 0
        overall_svc = 100 * total_delivered / total_demanded if total_demanded else 0

        result = {
            "rank":              rank,
            "worst_date":        worst_date,
            "K_fixed":           K_fixed,
            "n_vehicles_worst":  int(row["n_vehicles"]),
            "days_fully_covered":    fully_covered,
            "days_partial":          partially_covered,
            "days_uncovered":        uncovered,
            "pct_fully_covered":     round(pct_full, 1),
            "pct_partial":           round(pct_partial, 1),
            "overall_energy_svc_pct": round(overall_svc, 1),
        }
        results.append(result)
        print(f"         → {fully_covered}/{n_days} days 100% served ({pct_full:.1f}%)")

    return results


# ── report card figure ─────────────────────────────────────────────────────────

def plot_report_card(row: dict, rank: int, coverage: dict,
                     events_ext: pd.DataFrame) -> Path:
    """
    4-panel report card for one worst day:
      ax_info  : text summary + coverage result
      ax_gantt : XOS A2 hub Gantt (charging schedule bar chart)
      ax_energy: vehicle energy bar chart (needed vs delivered)
      ax_power : grid power demand curve
    """
    date_str = row["date"]
    day_dir  = PER_DAY / date_str

    # Load per-day CSVs
    def _ldf(p: Path) -> pd.DataFrame:
        return pd.read_csv(p) if p.exists() else pd.DataFrame()

    a2_d = _ldf(day_dir / f"A2_dispatch_{date_str}.csv")
    a2_s = _ldf(day_dir / f"A2_state_{date_str}.csv")
    a2_g = _ldf(day_dir / f"A2_grid_draw_{date_str}.csv")
    a2_v = _ldf(day_dir / f"A2_vehicle_results_{date_str}.csv")

    for df in (a2_d,):
        if "time_utc" in df.columns:
            df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    for df in (a2_s,):
        if "time_utc" in df.columns:
            df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    if not a2_g.empty and "time_utc" in a2_g.columns:
        a2_g["time_pac"] = pd.to_datetime(a2_g["time_utc"], utc=True).dt.tz_convert(TZ)

    # Vehicle info
    all_vids  = events_ext["charging_event_id"].tolist()
    vid_dwell = {row2["charging_event_id"]: (
                     pd.to_datetime(row2["arrival_time"], utc=True),
                     pd.to_datetime(row2["departure_time"], utc=True))
                 for _, row2 in events_ext.iterrows()}
    cmap      = plt.cm.get_cmap("tab20", max(len(all_vids), 20))
    vid_color = {v: cmap(i) for i, v in enumerate(all_vids)}

    t_ref_pac = pd.Timestamp(date_str, tz=TZ)
    def to_x(t) -> float:
        tl = t.tz_convert(TZ) if hasattr(t, "tz_convert") else t
        return (tl - t_ref_pac).total_seconds() / 60.0

    # x range
    all_times = list(events_ext["arrival_time"]) + list(events_ext["departure_time"])
    if not a2_g.empty and "time_pac" in a2_g.columns:
        all_times += list(a2_g["time_pac"])
    t_gs = min(pd.to_datetime(t, utc=True) for t in all_times).tz_convert(TZ).floor("1h")
    t_ge = max(pd.to_datetime(t, utc=True) for t in all_times).tz_convert(TZ).ceil("1h")
    x_beg = (t_gs - t_ref_pac).total_seconds() / 60.0
    x_end = (t_ge - t_ref_pac).total_seconds() / 60.0

    t_tick = t_gs.ceil("2h")
    tick_xs, tick_labels = [], []
    while t_tick <= t_ge:
        tick_xs.append((t_tick - t_ref_pac).total_seconds() / 60.0)
        tick_labels.append(t_tick.strftime("%H:%M"))
        t_tick += pd.Timedelta(hours=2)

    # Hub count
    n_hubs = int(row["K"])
    PORT_H = 0.36; HUB_GAP = 0.22

    # Build port windows
    port_windows: dict[tuple, list] = {}
    if not a2_d.empty and "unit" in a2_d.columns:
        for (unit_k, port_p, eid), grp in a2_d.groupby(["unit", "port", "event_id"]):
            ts_g = grp["time_utc"].sort_values()
            t0 = ts_g.iloc[0]; t1 = ts_g.iloc[-1] + pd.Timedelta(minutes=DT_MIN)
            port_windows.setdefault((int(unit_k), int(port_p)), []).append((t0, t1, eid))
    active_hubs = sorted({u for u, _ in port_windows}) if port_windows else []

    # Layout
    n_leg = len(all_vids)
    gantt_h = (len(active_hubs) or n_hubs) * (NP * PORT_H + HUB_GAP)
    gantt_h = max(gantt_h, 3.5)
    fig_h   = max(18, 3.2 + gantt_h + 3.8 + 3.5)

    fig = plt.figure(figsize=(24, fig_h))
    gs  = gridspec.GridSpec(4, 2, figure=fig,
                            height_ratios=[3.2, gantt_h, 3.8, 3.5],
                            hspace=0.25, wspace=0.12,
                            left=0.10, right=0.99, top=0.97, bottom=0.03)
    ax_info  = fig.add_subplot(gs[0, :])
    ax_gantt = fig.add_subplot(gs[1, :])
    ax_power = fig.add_subplot(gs[2, 0])
    ax_nrg   = fig.add_subplot(gs[2, 1])
    ax_leg   = fig.add_subplot(gs[3, :])

    # ── ax_info: text summary ────────────────────────────────────────────────
    ax_info.axis("off")
    dow = pd.Timestamp(date_str).strftime("%A")
    day_idx = (pd.Timestamp(date_str) - pd.Timestamp("2025-05-01")).days + 1

    info_lines = [
        (f"Rank #{rank} Worst Day — {date_str}  ({dow},  Day {day_idx})", 14, "bold", "black"),
        (f"Site: Northgate  |  Scenario: A2 (disconnect at 20% SOC, proactive recharge)", 10, "normal", "#333333"),
        ("", 6, "normal", "white"),
        (f"XOS Hubs deployed: {int(row['K'])}   |   "
         f"Vehicles: {int(row['n_vehicles'])} total  "
         f"({int(row['n_fully_served'])} fully served  "
         f"/ {int(row['n_partial'])} partial  "
         f"/ {int(row['n_unserved'])} unserved)", 10.5, "bold", "#1a1a7a"),
        (f"Daily cost (excl. demand): ${float(row['total_daily_excl_demand']):,.2f}  |  "
         f"Daily cost (incl. demand): ${float(row['total_daily_incl_demand']):,.2f}  |  "
         f"Peak grid draw: {float(row['peak_grid_kw']):,.0f} kW", 10.5, "normal", "#333333"),
        (f"Energy demanded: {float(row['energy_demanded_kwh']):,.1f} kWh  |  "
         f"Energy delivered: {float(row['energy_delivered_kwh']):,.1f} kWh  |  "
         f"Unmet: {float(row['energy_unmet_kwh']):,.1f} kWh  |  "
         f"Service rate: {float(row['service_rate_pct']):.1f}%", 10.5, "normal", "#333333"),
        ("", 6, "normal", "white"),
        (f"[Shima coverage]  With K={int(row['K'])} hubs (designed for this day):  "
         f"{coverage['days_fully_covered']}/{coverage['days_fully_covered']+coverage['days_partial']+coverage['days_uncovered']} days fully served "
         f"({coverage['pct_fully_covered']:.1f}%)  |  "
         f"Partial: {coverage['days_partial']}  |  "
         f"Uncovered: {coverage['days_uncovered']}  |  "
         f"Overall energy service: {coverage['overall_energy_svc_pct']:.1f}%",
         10, "bold", "#7a1a1a"),
    ]
    y = 0.96
    for text, fs, fw, fc in info_lines:
        ax_info.text(0.01, y, text, transform=ax_info.transAxes,
                     fontsize=fs, fontweight=fw, color=fc, va="top",
                     fontfamily="monospace" if "Rank" not in text else "sans-serif")
        y -= (fs + 4) / (fig_h * 10)

    # ── ax_gantt: XOS A2 charging schedule Gantt ─────────────────────────────
    y_map, hub_y_spans, hub_label_y = {}, {}, {}
    y_cur = 0.0
    for hub_k in active_hubs:
        y_bot = y_cur
        for p in range(NP):
            y_map[(hub_k, p)] = y_cur + PORT_H / 2
            y_cur += PORT_H
        y_top = y_cur
        hub_y_spans[hub_k]  = (y_bot, y_top)
        hub_label_y[hub_k]  = (y_bot + y_top) / 2
        y_cur += HUB_GAP
    y_total = y_cur - HUB_GAP if active_hubs else 1.0

    for hub_k in active_hubs:
        y_bot, y_top = hub_y_spans[hub_k]
        for xb0, xb1, state in _hub_state_blocks(a2_s, hub_k, to_x):
            color, alpha = STATE_COLOR.get(state, ("#fff", 0.0))
            ax_gantt.fill_betweenx([y_bot, y_top], xb0, xb1,
                                    color=color, alpha=alpha, linewidth=0, zorder=1)
        for p in range(1, NP):
            ax_gantt.axhline(y_bot + p * PORT_H, color="gray",
                              linewidth=0.3, linestyle=":", alpha=0.5, zorder=2)
        for p in range(NP):
            y_c = y_map[(hub_k, p)]
            for t0, t1, eid in port_windows.get((hub_k, p), []):
                color = vid_color.get(eid, "steelblue")
                xv0, xv1 = to_x(t0), to_x(t1)
                if eid in vid_dwell:
                    arr_t, dep_t = vid_dwell[eid]
                    ax_gantt.barh(y_c, max(to_x(dep_t) - to_x(arr_t), 1), left=to_x(arr_t),
                                   height=PORT_H * 0.78, color=color, alpha=0.18,
                                   edgecolor=color, linewidth=0.5, zorder=2)
                ax_gantt.barh(y_c, max(xv1 - xv0, 1), left=xv0, height=PORT_H * 0.78,
                               color=color, alpha=0.88, edgecolor="white",
                               linewidth=0.3, zorder=3)
                lbl = _vid_label(eid, date_str)
                ax_gantt.text((xv0 + xv1) / 2, y_c, lbl,
                               ha="center", va="center", fontsize=5.5,
                               color="black", fontweight="bold", clip_on=True, zorder=4)
        ax_gantt.axhline(y_top, color="#888", linewidth=0.6, alpha=0.5, zorder=2)

    ax_gantt.set_xlim(x_beg, x_end)
    ax_gantt.set_ylim(y_total + 0.1, -0.1)
    ax_gantt.set_yticks([hub_label_y[k] for k in active_hubs])
    ax_gantt.set_yticklabels([f"Hub {k+1}" for k in active_hubs], fontsize=7.5)
    ax_gantt.set_xticks(tick_xs)
    ax_gantt.set_xticklabels(tick_labels, fontsize=8, rotation=30, ha="right")
    ax_gantt.set_title(f"XOS A2 Charging Schedule  (K={n_hubs} hubs)", fontsize=10, fontweight="bold")
    ax_gantt.grid(axis="x", linestyle=":", alpha=0.3, color="gray")

    patches = [
        mpatches.Patch(color=STATE_COLOR["idle"][0],       alpha=STATE_COLOR["idle"][1],       label="Idle"),
        mpatches.Patch(color=STATE_COLOR["serving"][0],    alpha=STATE_COLOR["serving"][1],    label="Serving"),
        mpatches.Patch(color=STATE_COLOR["recharging"][0], alpha=STATE_COLOR["recharging"][1], label="Grid recharge"),
        mpatches.Patch(facecolor="gray", alpha=0.22, label="Dwell window"),
    ]
    ax_gantt.legend(handles=patches, loc="upper right", fontsize=7, ncol=4, framealpha=0.90)

    # ── ax_power: grid demand curve ───────────────────────────────────────────
    if not a2_g.empty and "grid_kw" in a2_g.columns:
        xs2 = [(t - t_ref_pac).total_seconds() / 60.0 for t in a2_g["time_pac"]]
        ax_power.plot(xs2, a2_g["grid_kw"], color="#d73027", linewidth=2.0, label="XOS A2")
        ax_power.fill_between(xs2, a2_g["grid_kw"], alpha=0.12, color="#d73027")
    # SMUD peak window (16–21h)
    for day_off in (0,):
        pk0 = (t_ref_pac + pd.Timedelta(hours=16) - t_ref_pac).total_seconds() / 60
        pk1 = (t_ref_pac + pd.Timedelta(hours=21) - t_ref_pac).total_seconds() / 60
        if pk0 < x_end:
            ax_power.axvspan(pk0, min(pk1, x_end), color="#fee08b", alpha=0.35, label="SMUD peak (16–21h)")
    ax_power.set_xlim(x_beg, x_end)
    ax_power.set_xticks(tick_xs); ax_power.set_xticklabels(tick_labels, fontsize=7.5, rotation=30, ha="right")
    ax_power.set_ylabel("Grid draw (kW)", fontsize=9)
    ax_power.set_title("Site Grid Power Demand", fontsize=10, fontweight="bold")
    ax_power.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax_power.legend(loc="upper left", fontsize=8)
    ax_power.grid(axis="both", linestyle=":", alpha=0.3)

    # ── ax_nrg: vehicle energy bar chart ──────────────────────────────────────
    if not a2_v.empty and "energy_needed_kwh" in a2_v.columns:
        a2_v2 = a2_v.sort_values("event_id")
        labels = [_vid_label(e, date_str) for e in a2_v2["event_id"]]
        needed    = a2_v2["energy_needed_kwh"].values
        delivered = a2_v2["energy_delivered_kwh"].values
        ys = np.arange(len(labels))
        colors_e  = [vid_color.get(e, "steelblue") for e in a2_v2["event_id"]]
        ax_nrg.barh(ys, needed,    height=0.6, color="lightgray", edgecolor="gray", linewidth=0.5, label="Needed")
        ax_nrg.barh(ys, delivered, height=0.6, color=colors_e,    edgecolor="none", alpha=0.85,  label="Delivered")
        ax_nrg.set_yticks(ys); ax_nrg.set_yticklabels(labels, fontsize=6.5)
        ax_nrg.set_xlabel("Energy (kWh)", fontsize=9)
        ax_nrg.set_title("Vehicle Energy: Needed vs Delivered", fontsize=10, fontweight="bold")
        ax_nrg.legend(loc="lower right", fontsize=8, framealpha=0.90)
        ax_nrg.grid(axis="x", linestyle=":", alpha=0.35)
        ax_nrg.invert_yaxis()
    else:
        ax_nrg.text(0.5, 0.5, "No vehicle data", ha="center", va="center",
                    transform=ax_nrg.transAxes, fontsize=10, color="gray")
        ax_nrg.axis("off")

    # ── ax_leg: vehicle legend ────────────────────────────────────────────────
    ax_leg.axis("off")
    ax_leg.set_title("Vehicle legend", fontsize=9, fontweight="bold", pad=2, loc="left")
    vid_model = {row2["charging_event_id"]: str(row2.get("ev_equivalent_model", "") or "")
                 for _, row2 in events_ext.iterrows()}
    legend_rows = []
    for eid in all_vids:
        lbl = _vid_label(eid, date_str)
        num = int(lbl.rstrip("p")[1:]) if lbl.rstrip("p")[1:].isdigit() else 999
        legend_rows.append((lbl.endswith("p"), num, lbl, vid_model.get(eid, ""), vid_color[eid]))
    legend_rows.sort()

    N_COLS = 6; PATCH_W = 0.018; PATCH_H = 0.065
    n_rows = max(1, math.ceil(len(legend_rows) / N_COLS))
    COL_W  = 1.0 / N_COLS
    for idx, (_, _, lbl, model, color) in enumerate(legend_rows):
        col = idx % N_COLS; row_i = idx // N_COLS
        x0  = col * COL_W + 0.004
        y0  = 1.0 - (row_i + 1) * (1.0 / (n_rows + 0.5))
        ax_leg.add_patch(mpatches.FancyBboxPatch(
            (x0, y0), PATCH_W, PATCH_H, boxstyle="round,pad=0.002",
            facecolor=color, edgecolor="none",
            transform=ax_leg.transAxes, clip_on=True, zorder=3))
        ax_leg.text(x0 + PATCH_W + 0.005, y0 + PATCH_H / 2,
                    f"{lbl}: {model}", ha="left", va="center",
                    fontsize=7, transform=ax_leg.transAxes, clip_on=True)

    out = OUT_DIR / f"worst_day_rank{rank:02d}_{date_str}.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


# ── text report builder ───────────────────────────────────────────────────────

def build_report(top10_rows: list[dict], coverage_results: list[dict]) -> str:
    lines = [
        "=" * 90,
        "  NORTHGATE — TOP 10 WORST DAYS  (by K used, then vehicle count)",
        "  Shima Coverage Analysis: effect of designing fleet for each worst day on all 307 days",
        "=" * 90,
        "",
    ]

    # Per-day report cards (text)
    for cov, row in zip(coverage_results, top10_rows):
        rank = cov["rank"]
        dow  = pd.Timestamp(row["date"]).strftime("%A")
        day_idx = (pd.Timestamp(row["date"]) - pd.Timestamp("2025-05-01")).days + 1
        lines += [
            f"{'─'*90}",
            f"  RANK #{rank}  |  {row['date']}  ({dow},  Day {day_idx})",
            f"{'─'*90}",
            f"  Site              : Northgate",
            f"  Charger config    : K = {int(row['K'])} XOS Hub MC02 units  "
            f"(each: 282 kWh bat, 80 kW×4 ports, P_grid=83 kW)",
            f"  Vehicles          : {int(row['n_vehicles'])} total  "
            f"| {int(row['n_fully_served'])} fully served "
            f"| {int(row['n_partial'])} partial "
            f"| {int(row['n_unserved'])} unserved",
            f"  Service rate      : {float(row['service_rate_pct']):.1f}%",
            f"  Energy demanded   : {float(row['energy_demanded_kwh']):>8.1f} kWh",
            f"  Energy delivered  : {float(row['energy_delivered_kwh']):>8.1f} kWh",
            f"  Energy unmet      : {float(row['energy_unmet_kwh']):>8.1f} kWh",
            f"  Peak grid draw    : {float(row['peak_grid_kw']):>8.0f} kW",
            f"  Daily cost        : ${float(row['total_daily_excl_demand']):>10,.2f}  (excl demand charge)",
            f"                      ${float(row['total_daily_incl_demand']):>10,.2f}  (incl demand charge)",
            "",
            f"  [Shima]  With K={int(row['K'])} hubs deployed every day:",
            f"    Days 100% served   : {cov['days_fully_covered']:>3d} / 307  ({cov['pct_fully_covered']:.1f}%)",
            f"    Days partial       : {cov['days_partial']:>3d} / 307  ({cov['pct_partial']:.1f}%)",
            f"    Days uncovered     : {cov['days_uncovered']:>3d} / 307",
            f"    Overall energy svc : {cov['overall_energy_svc_pct']:.1f}%  (all vehicles, all days)",
            "",
        ]

    # Summary coverage table
    lines += [
        "=" * 90,
        "  COVERAGE SUMMARY TABLE  (K from each worst day → coverage of all 307 days)",
        "=" * 90,
        f"  {'Rank':>4}  {'Date':>12}  {'K':>4}  {'Vehicles':>9}  "
        f"{'Days 100%':>10}  {'Pct 100%':>9}  {'Partial':>8}  {'Uncovered':>10}  {'Energy Svc%':>11}",
        f"  {'─'*4}  {'─'*12}  {'─'*4}  {'─'*9}  {'─'*10}  {'─'*9}  {'─'*8}  {'─'*10}  {'─'*11}",
    ]
    for cov in coverage_results:
        lines.append(
            f"  {cov['rank']:>4}  {cov['worst_date']:>12}  {cov['K_fixed']:>4}  "
            f"{cov['n_vehicles_worst']:>9}  "
            f"{cov['days_fully_covered']:>10}  {cov['pct_fully_covered']:>9.1f}  "
            f"{cov['days_partial']:>8}  {cov['days_uncovered']:>10}  "
            f"{cov['overall_energy_svc_pct']:>11.1f}"
        )
    lines += [
        "",
        "  Note: 'Days 100%' = days where every vehicle was fully charged with the fixed K.",
        "        Rank #1 K is sized for the hardest day → typically near 100% coverage.",
        "        Rank #10 K is smaller → fewer days fully covered.",
        "=" * 90,
    ]
    return "\n".join(lines)


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load summary
    df_sum = pd.read_csv(BASE_DIR / "scenario_outputs/northgate_analysis/northgate_summary.csv")
    a2 = df_sum[df_sum["scenario"] == "A2"].copy()
    top10 = (a2.sort_values(["K", "n_vehicles"], ascending=False)
               .head(10)
               .reset_index(drop=True))
    top10_rows = top10.to_dict("records")

    print(f"\n{'='*70}")
    print(f"  Top 10 worst Northgate days (A2 scenario):")
    print(f"{'='*70}")
    for i, r in enumerate(top10_rows, 1):
        print(f"  {i:2d}. {r['date']}  K={r['K']:2d}  Veh={r['n_vehicles']:2d}  "
              f"Svc={r['service_rate_pct']:.0f}%  Cost=${r['total_daily_excl_demand']:,.0f}")

    # Shima coverage analysis
    all_csv_paths = _all_csv_paths()
    print(f"\n{'='*70}")
    print(f"  Shima coverage: running {len(all_csv_paths)} days × 10 K values ...")
    print(f"{'='*70}")
    coverage_results = shima_coverage_analysis(top10_rows, all_csv_paths)

    # Per-day report card figures
    print(f"\n{'='*70}")
    print(f"  Generating report card figures ...")
    print(f"{'='*70}")
    for rank, (row, cov) in enumerate(zip(top10_rows, coverage_results), 1):
        date_str = row["date"]
        print(f"  [{rank:2d}/10] {date_str} ...", end=" ", flush=True)
        events_ext = _load_events(date_str)
        if events_ext is None or events_ext.empty:
            print("skipped (no events)")
            continue
        out = plot_report_card(row, rank, cov, events_ext)
        print(f"saved → {out.name}")

    # Coverage CSV
    cov_df = pd.DataFrame(coverage_results)
    cov_csv = OUT_DIR / "coverage_analysis.csv"
    cov_df.to_csv(cov_csv, index=False)
    print(f"\n  Coverage CSV → {cov_csv}")

    # Text report
    report = build_report(top10_rows, coverage_results)
    rpt_path = OUT_DIR / "worst_days_report.txt"
    rpt_path.write_text(report, encoding="utf-8")
    print(f"  Text report   → {rpt_path}")

    print()
    print(report)


if __name__ == "__main__":
    main()
