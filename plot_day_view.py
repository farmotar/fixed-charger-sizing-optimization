"""
plot_day_view.py
=================
Full-day Gantt + demand chart for one site-day, comparing Kempower and XOS.

Layout (top -> bottom):
  ax1  Kempower -- charger-lane Gantt
       Light bar  = vehicle dwell window (arrival->departure)
       Solid bar  = active charging window (MILP schedule)
       Label      = V-number

  ax2  XOS -- hub-unit Gantt (4 port sub-rows per hub, hub-state backgrounds)
       GRAY background  = hub IDLE
       BLUE background  = hub SERVING (vehicle actively charging)
       GREEN background = hub RECHARGING from grid (no vehicle service)
       Light bar  = vehicle dwell window (arrival->departure)
       Solid bar  = active charge window on this hub/port
       Label      = V-number

  ax3  Site power demand (kW) vs time

  ax4  Vehicle legend: V-number -> make/model (one entry per vehicle)

Usage:
  python plot_day_view.py [date_str] [site_label] [scenario]
  e.g.  python plot_day_view.py 2025-07-17 Northgate a2
"""

from __future__ import annotations
import math
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
OUT_DIR  = BASE_DIR / "scenario_outputs"
TZ       = "America/Los_Angeles"

STATE_COLOR = {
    "idle":       ("#cccccc", 0.55),
    "serving":    ("#b8d4f0", 0.45),
    "recharging": ("#90e090", 0.70),
}


# ── helpers ────────────────────────────────────────────────────────────────────

def _vid_label(eid: str, date_str: str) -> str:
    """
    Convert event_id to a short V-label.
    Same-day vehicles:      'z2z_20250717_v01' -> 'V1'
    Previous-day carryover: 'z2z_20250716_v14' -> 'V14p'
    """
    parts = eid.split("_")
    date_compact = parts[-2]          # e.g. '20250717'
    num = int(parts[-1][1:])          # strip leading 'v', convert to int
    target_compact = date_str.replace("-", "")
    suffix = "p" if date_compact < target_compact else ""
    return f"V{num}{suffix}"


def _assign_kempower_lanes(schedule_df: pd.DataFrame,
                           mix_df: pd.DataFrame) -> pd.DataFrame:
    schedule_df = schedule_df.copy()
    schedule_df["time_step_start"] = pd.to_datetime(schedule_df["time_step_start"], utc=True)
    schedule_df["time_step_end"]   = pd.to_datetime(schedule_df["time_step_end"],   utc=True)

    veh_summary = (
        schedule_df.groupby(["charging_event_id", "charger_type"])
        .agg(charge_start=("time_step_start", "min"),
             charge_end=("time_step_end",   "max"),
             energy_del=("energy_delivered_kwh", "sum"))
        .reset_index()
        .sort_values("charge_start")
    )

    lane_end_times: dict[str, list[pd.Timestamp]] = {}
    for ct in mix_df["charger_type"].tolist():
        n = int(mix_df.loc[mix_df["charger_type"] == ct, "count"].values[0])
        lane_end_times[ct] = [pd.Timestamp.min.tz_localize("UTC")] * n

    assigned: list[int] = []
    for _, row in veh_summary.iterrows():
        ct   = row["charger_type"]
        ends = lane_end_times[ct]
        best = min(range(len(ends)), key=lambda i: ends[i])
        assigned.append(best)
        ends[best] = row["charge_end"]
    veh_summary["lane_within_type"] = assigned

    type_order = ["Kempower_50kW", "Kempower_150kW", "Kempower_250kW"]
    base: dict[str, int] = {}
    offset = 0
    for ct in type_order:
        base[ct] = offset
        n = int(mix_df.loc[mix_df["charger_type"] == ct, "count"].values[0]) if ct in mix_df["charger_type"].values else 0
        offset += n

    veh_summary["lane"] = veh_summary.apply(
        lambda r: base[r["charger_type"]] + r["lane_within_type"], axis=1
    )
    return veh_summary


def _hub_state_windows(state_df: pd.DataFrame, hub_k: int, to_x_fn):
    """
    Parse state history for hub_k into contiguous (x_start, x_end, state) blocks.
    Each 15-min timestep extends to the start of the NEXT timestep.
    """
    col    = f"state_unit_{hub_k}"
    times  = pd.to_datetime(state_df["time_utc"], utc=True)
    states = state_df[col].tolist()
    n      = len(states)
    dt_min = 15.0

    blocks = []
    i = 0
    while i < n:
        s = states[i]
        j = i + 1
        while j < n and states[j] == s:
            j += 1
        x0 = to_x_fn(times.iloc[i])
        x1 = to_x_fn(times.iloc[j - 1]) + dt_min
        blocks.append((x0, x1, s))
        i = j
    return blocks


# ── main plot ──────────────────────────────────────────────────────────────────

def plot_day_view(date_str: str, site_label: str = "Northgate",
                  scenario: str = "a2") -> Path:
    date_tag  = date_str.replace("-", "_")
    kmp_dir   = OUT_DIR / f"northgate_{date_tag}" / "kempower_only"
    scen_sub  = "xos_a1_fixed" if scenario == "a1" else "xos_a2_fixed"
    xos_dir   = OUT_DIR / f"northgate_{date_tag}" / scen_sub
    scen_pfx  = "A1" if scenario == "a1" else "A2"
    events_csv = BASE_DIR / f"z2z_milp_events_northgate_{date_tag}.csv"

    # ── Load Kempower outputs ─────────────────────────────────────────────────
    mix_df   = pd.read_csv(kmp_dir / "exact_milp_selected_charger_mix.csv")
    sched_df = pd.read_csv(kmp_dir / "exact_milp_charging_schedule.csv")
    evt_df   = pd.read_csv(kmp_dir / "exact_milp_event_results.csv")
    power_df = pd.read_csv(kmp_dir / "exact_milp_site_power_profile.csv")

    power_df["time_utc"] = pd.to_datetime(power_df["time_step_start"], utc=True)
    power_df["time_pac"] = power_df["time_utc"].dt.tz_convert(TZ)
    power_df["power_kw"] = power_df["P_total_kw"]

    evt_df["arrival_time"]   = pd.to_datetime(evt_df["arrival_time"], utc=True)
    evt_df["departure_time"] = pd.to_datetime(evt_df["departure_time"], utc=True)
    sched_df["time_step_start"] = pd.to_datetime(sched_df["time_step_start"], utc=True)
    sched_df["time_step_end"]   = pd.to_datetime(sched_df["time_step_end"],   utc=True)

    lane_df = _assign_kempower_lanes(sched_df, mix_df)
    evt_short = evt_df[["charging_event_id", "ev_equivalent_model",
                         "delivered_energy_kwh", "arrival_time", "departure_time"]].copy()
    evt_short = evt_short.rename(columns={"delivered_energy_kwh": "energy_delivered_kwh"})
    lane_df   = lane_df.merge(evt_short, on="charging_event_id", how="left")

    # ── Load XOS outputs ──────────────────────────────────────────────────────
    dispatch_df = pd.read_csv(xos_dir / f"scenario_{scen_pfx}_dispatch_{date_str}.csv")
    grid_df     = pd.read_csv(xos_dir / f"scenario_{scen_pfx}_grid_draw_{date_str}.csv")
    state_df    = pd.read_csv(xos_dir / f"scenario_{scen_pfx}_state_{date_str}.csv")

    grid_df["time_pac"]      = pd.to_datetime(grid_df["time_utc"], utc=True).dt.tz_convert(TZ)
    dispatch_df["time_utc"]  = pd.to_datetime(dispatch_df["time_utc"], utc=True)
    state_df["time_utc"]     = pd.to_datetime(state_df["time_utc"], utc=True)

    # ── Load events (with multiday rule + extended dwell) ─────────────────────
    import importlib
    milp_mod = importlib.import_module("exact_northgate_charger_sizing_milp")
    runner   = importlib.import_module("scenario_runner")
    raw_ev    = milp_mod.load_events_data(events_csv)
    events_df = milp_mod.clean_events_df(raw_ev)
    stem_parts    = events_csv.stem.rsplit("_", 3)
    site_csv_stem = "_".join(stem_parts[:-3]) if len(stem_parts) > 3 else events_csv.stem
    events_df  = runner.apply_multiday_rule(events_df, date_str,
                                            site_csv_dir=events_csv.parent,
                                            site_csv_stem=site_csv_stem)
    events_ext = runner._xos_extended_dwell(events_df)

    # Build vehicle lookup dicts
    all_vids  = events_ext["charging_event_id"].tolist()
    vid_model = {row["charging_event_id"]: str(row.get("ev_equivalent_model", "") or "")
                 for _, row in events_ext.iterrows()}
    vid_dwell = {row["charging_event_id"]: (
                     pd.to_datetime(row["arrival_time"], utc=True),
                     pd.to_datetime(row["departure_time"], utc=True))
                 for _, row in events_ext.iterrows()}

    # ── Common x-axis in minutes from midnight Pacific ────────────────────────
    t_global_start = min(events_ext["arrival_time"].min(),
                         power_df["time_utc"].min()).floor("1h")
    t_global_end   = max(events_ext["departure_time"].max(),
                         power_df["time_utc"].max()).ceil("1h")

    t_ref = t_global_start.tz_convert(TZ)

    def to_x(t) -> float:
        t_loc = t.tz_convert(TZ) if hasattr(t, "tz_convert") else t
        return (t_loc - t_ref).total_seconds() / 60.0

    x_end_min = to_x(t_global_end)

    t_tick = t_ref.ceil("2h")
    tick_xs, tick_labels = [], []
    while t_tick <= t_global_end.tz_convert(TZ):
        tick_xs.append((t_tick - t_ref).total_seconds() / 60.0)
        tick_labels.append(t_tick.strftime("%H:%M"))
        t_tick += pd.Timedelta(hours=2)

    # Colour map: one colour per vehicle
    cmap      = plt.cm.get_cmap("tab20", max(len(all_vids), 20))
    vid_color = {v: cmap(i) for i, v in enumerate(all_vids)}

    n_kmp_lanes = int(mix_df["count"].sum())
    n_xos_units = int(dispatch_df["unit"].max()) + 1
    hub_panel_h = n_xos_units * (4 * 0.36 + 0.22)
    kmp_panel_h = n_kmp_lanes * 0.62

    # ── Figure layout (4 panels) ──────────────────────────────────────────────
    legend_h = 2.8
    fig_h = 2.0 + kmp_panel_h + hub_panel_h + 4.0 + legend_h
    fig, (ax1, ax2, ax3, ax4) = plt.subplots(
        4, 1,
        figsize=(26, max(fig_h, 22)),
        gridspec_kw={"height_ratios": [kmp_panel_h, hub_panel_h, 4.5, legend_h]},
    )
    fig.subplots_adjust(hspace=0.12, left=0.09, right=0.99, top=0.97, bottom=0.03)

    # ── ax1: Kempower Gantt ───────────────────────────────────────────────────
    type_color = {
        "Kempower_50kW":  "#2166ac",
        "Kempower_150kW": "#1a9641",
        "Kempower_250kW": "#d73027",
    }
    type_power = {"Kempower_50kW": "50kW", "Kempower_150kW": "150kW", "Kempower_250kW": "250kW"}
    type_order = ["Kempower_50kW", "Kempower_150kW", "Kempower_250kW"]

    lane_labels = []
    for ct in type_order:
        sub = mix_df[mix_df["charger_type"] == ct]
        if sub.empty:
            continue
        n = int(sub["count"].values[0])
        for i in range(n):
            lane_labels.append(f"{type_power[ct]} #{i+1}")

    for _, row in lane_df.iterrows():
        lane  = int(row["lane"])
        ct    = row["charger_type"]
        color = type_color[ct]
        vid   = row["charging_event_id"]
        arr   = row["arrival_time"];  dep = row["departure_time"]
        c_s   = row["charge_start"]; c_e = row["charge_end"]
        e_del = float(row.get("energy_del", 0) or 0)

        x_arr = to_x(arr); x_dep = to_x(dep)
        x_cs  = to_x(c_s); x_ce  = to_x(c_e)

        # Dwell bar (light)
        ax1.barh(lane, max(x_dep - x_arr, 1), left=x_arr, height=0.60,
                 color=color, alpha=0.15, edgecolor=color, linewidth=0.6, zorder=1)
        # Charge bar (solid)
        ax1.barh(lane, max(x_ce - x_cs, 1), left=x_cs, height=0.60,
                 color=color, alpha=0.85, edgecolor="white", linewidth=0.2, zorder=3)
        # V-number label
        lbl = _vid_label(vid, date_str)
        ax1.text((x_cs + x_ce) / 2, lane, lbl,
                 ha="center", va="center", fontsize=6.0,
                 color="white", fontweight="bold", clip_on=True, zorder=4)

    ax1.set_xlim(0, x_end_min)
    ax1.set_ylim(-0.5, n_kmp_lanes - 0.5)
    ax1.set_yticks(range(n_kmp_lanes))
    ax1.set_yticklabels(lane_labels, fontsize=8)
    ax1.set_xticks(tick_xs)
    ax1.set_xticklabels(tick_labels, fontsize=8, rotation=30, ha="right")
    ax1.invert_yaxis()
    ax1.grid(axis="x", linestyle=":", alpha=0.35, color="gray")
    ax1.set_ylabel("Kempower\ncharger lane", fontsize=9)
    ax1.set_title(
        f"{site_label}  |  {date_str}  |  Full-day charger assignment & site demand",
        fontsize=13, fontweight="bold", pad=6)

    kmp_patches = [mpatches.Patch(color=type_color[ct], label=f"{type_power[ct]}")
                   for ct in type_order]
    dwell_p = mpatches.Patch(facecolor="gray", alpha=0.20, edgecolor="gray",
                              linewidth=0.8, label="Dwell window")
    ax1.legend(handles=kmp_patches + [dwell_p], loc="upper right",
               fontsize=7.5, ncol=2, framealpha=0.90)

    # ── ax2: XOS Hub Gantt — port-level (4 rows per hub) ─────────────────────
    port_windows: dict[tuple[int, int], list[tuple]] = {}
    for (unit_k, port_p, eid), grp in dispatch_df.groupby(["unit", "port", "event_id"]):
        ts = grp["time_utc"].sort_values()
        t0 = ts.iloc[0]
        t1 = ts.iloc[-1] + pd.Timedelta(minutes=15)
        key = (int(unit_k), int(port_p))
        port_windows.setdefault(key, []).append((t0, t1, eid))

    active_hubs = sorted({k for k, _ in port_windows.keys()})

    PORT_H  = 0.36
    HUB_GAP = 0.22
    NP      = 4

    y_map       = {}
    hub_y_spans = {}
    hub_label_y = {}
    y_cur = 0.0
    for hub_k in active_hubs:
        y_bottom = y_cur
        for p in range(NP):
            y_map[(hub_k, p)] = y_cur + PORT_H / 2
            y_cur += PORT_H
        y_top = y_cur
        hub_y_spans[hub_k] = (y_bottom, y_top)
        hub_label_y[hub_k] = (y_bottom + y_top) / 2
        y_cur += HUB_GAP
    y_total = y_cur - HUB_GAP

    for hub_k in active_hubs:
        y_bot, y_top = hub_y_spans[hub_k]

        # 1. Hub-state background
        if f"state_unit_{hub_k}" in state_df.columns:
            for xb0, xb1, state in _hub_state_windows(state_df, hub_k, to_x):
                color, alpha = STATE_COLOR.get(state, ("#ffffff", 0.0))
                ax2.fill_betweenx([y_bot, y_top], xb0, xb1,
                                   color=color, alpha=alpha, linewidth=0, zorder=1)

        # 2. Port separator lines
        for p in range(1, NP):
            y_line = y_bot + p * PORT_H
            ax2.axhline(y_line, color="gray", linewidth=0.3, linestyle=":",
                        alpha=0.55, zorder=2)

        # 3. Vehicle dwell + charge bars at port level
        for p in range(NP):
            y_c = y_map[(hub_k, p)]
            for t0, t1, eid in port_windows.get((hub_k, p), []):
                color = vid_color.get(eid, "steelblue")
                xv0   = to_x(t0); xv1 = to_x(t1)

                # Dwell window (light bar — vehicle arrival to departure)
                if eid in vid_dwell:
                    arr_t, dep_t = vid_dwell[eid]
                    xa = to_x(arr_t); xd = to_x(dep_t)
                    ax2.barh(y_c, max(xd - xa, 1), left=xa, height=PORT_H * 0.78,
                             color=color, alpha=0.18, edgecolor=color,
                             linewidth=0.5, zorder=2)

                # Charge bar (solid)
                ax2.barh(y_c, max(xv1 - xv0, 1), left=xv0, height=PORT_H * 0.78,
                         color=color, alpha=0.88, edgecolor="white",
                         linewidth=0.3, zorder=3)

                # V-number label inside charge bar
                lbl = _vid_label(eid, date_str)
                ax2.text((xv0 + xv1) / 2, y_c, lbl,
                         ha="center", va="center", fontsize=5.0,
                         color="black", fontweight="bold",
                         clip_on=True, zorder=4)

        # 4. Hub group border
        ax2.axhline(y_top, color="#888888", linewidth=0.6, alpha=0.50, zorder=2)

    ax2.set_xlim(0, x_end_min)
    ax2.set_ylim(y_total + 0.10, -0.10)
    ax2.set_yticks([hub_label_y[k] for k in active_hubs])
    ax2.set_yticklabels([f"Hub {k+1}" for k in active_hubs], fontsize=7, va="center")
    for hub_k in active_hubs:
        for p in range(NP):
            ax2.text(-x_end_min * 0.003, y_map[(hub_k, p)],
                     f"P{p}", ha="right", va="center", fontsize=4.0,
                     color="#555555", clip_on=False)
    ax2.set_xticks(tick_xs)
    ax2.set_xticklabels(tick_labels, fontsize=8, rotation=30, ha="right")
    ax2.grid(axis="x", linestyle=":", alpha=0.35, color="gray")
    scen_label = (f"XOS {scen_pfx.upper()} — always grid-connected\n(vehicles stay on port during recharge)"
                  if scenario == "a1"
                  else f"XOS {scen_pfx.upper()} — disconnect at 20% SOC\n(vehicles reassigned after hub recharge)")
    ax2.set_ylabel(scen_label, fontsize=8)

    idle_p = mpatches.Patch(color=STATE_COLOR["idle"][0],
                             alpha=STATE_COLOR["idle"][1], label="Hub idle")
    srv_p  = mpatches.Patch(color=STATE_COLOR["serving"][0],
                             alpha=STATE_COLOR["serving"][1], label="Hub serving")
    rch_p  = mpatches.Patch(color=STATE_COLOR["recharging"][0],
                             alpha=STATE_COLOR["recharging"][1], label="Hub recharging (grid)")
    dwell_xos = mpatches.Patch(facecolor="gray", alpha=0.25, edgecolor="gray",
                                linewidth=0.6, label="Vehicle dwell window")
    ax2.legend(handles=[idle_p, srv_p, rch_p, dwell_xos], loc="upper right",
               fontsize=7.5, ncol=4, framealpha=0.90)

    # ── ax3: Power demand ─────────────────────────────────────────────────────
    kmp_x = [to_x(t) for t in power_df["time_pac"]]
    ax3.plot(kmp_x, power_df["power_kw"], color="#2166ac", linewidth=1.8,
             label=f"Kempower (7 chargers, peak {int(power_df['power_kw'].max())} kW)")
    ax3.fill_between(kmp_x, power_df["power_kw"], alpha=0.10, color="#2166ac")

    xos_x = [to_x(t) for t in grid_df["time_pac"]]
    ax3.plot(xos_x, grid_df["grid_kw"], color="#d73027", linewidth=1.8,
             label=f"XOS {scen_pfx} ({n_xos_units} hubs, peak {int(grid_df['grid_kw'].max())} kW grid)")
    ax3.fill_between(xos_x, grid_df["grid_kw"], alpha=0.10, color="#d73027")

    pk0 = to_x(t_ref.normalize() + pd.Timedelta(hours=16))
    pk1 = to_x(t_ref.normalize() + pd.Timedelta(hours=21))
    ax3.axvspan(pk0, pk1, color="#fee08b", alpha=0.30,
                label="SMUD summer peak (16–21h, $0.234/kWh)")
    if x_end_min > 1440:
        ax3.axvspan(pk0 + 1440, min(pk1 + 1440, x_end_min), color="#fee08b", alpha=0.30)

    ax3.set_xlim(0, x_end_min)
    y_max = max(power_df["power_kw"].max(), grid_df["grid_kw"].max(), 1) * 1.15
    ax3.set_ylim(0, y_max)
    ax3.set_xticks(tick_xs)
    ax3.set_xticklabels(tick_labels, fontsize=9, rotation=30, ha="right")
    ax3.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax3.set_ylabel("Site grid draw (kW)", fontsize=9)
    ax3.set_xlabel(f"Time (Pacific)  —  {date_str}", fontsize=9)
    ax3.legend(loc="upper left", fontsize=8.5, framealpha=0.92)
    ax3.grid(axis="both", linestyle=":", alpha=0.30)
    ax3.axhline(0, color="gray", linewidth=0.5)

    # ── ax4: Vehicle legend panel ─────────────────────────────────────────────
    ax4.axis("off")
    ax4.set_title("Vehicle Legend  (solid bar = charging on hub/lane,  light bar = on-site dwell window)",
                  fontsize=9, fontweight="bold", pad=4, loc="left")

    # Build sorted legend entries
    legend_rows = []
    for eid in all_vids:
        lbl   = _vid_label(eid, date_str)
        model = vid_model.get(eid, "")
        color = vid_color[eid]
        # Sort key: previous-day carryover first (ends with 'p'), then by number
        num_str = lbl.rstrip("p")
        num = int(num_str[1:]) if num_str[1:].isdigit() else 999
        is_prev = lbl.endswith("p")
        legend_rows.append((is_prev, num, lbl, model, color))
    legend_rows.sort()   # sort by (is_prev, num)

    N_COLS = 5
    n_rows = math.ceil(len(legend_rows) / N_COLS)
    PATCH_W = 0.025
    PATCH_H = 0.055
    COL_W   = 1.0 / N_COLS

    for idx, (is_prev, num, lbl, model, color) in enumerate(legend_rows):
        col = idx % N_COLS
        row = idx // N_COLS
        # x,y in axes-fraction coords
        x0 = col * COL_W + 0.005
        y0 = 1.0 - (row + 1) * (1.0 / (n_rows + 0.5))

        # Colour patch
        rect = mpatches.FancyBboxPatch(
            (x0, y0), PATCH_W, PATCH_H,
            boxstyle="round,pad=0.002",
            facecolor=color, edgecolor="none",
            transform=ax4.transAxes, clip_on=True, zorder=3)
        ax4.add_patch(rect)

        # Label text: "V1: GMC Hummer EV"
        entry_text = f"{lbl}: {model}"
        ax4.text(x0 + PATCH_W + 0.008, y0 + PATCH_H / 2,
                 entry_text,
                 ha="left", va="center",
                 fontsize=7.2, transform=ax4.transAxes, clip_on=True)

    # ── Save ─────────────────────────────────────────────────────────────────
    out = OUT_DIR / f"northgate_{date_tag}" / f"day_view_{scenario}_{date_tag}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {out}")
    return out


if __name__ == "__main__":
    date_str   = sys.argv[1] if len(sys.argv) > 1 else "2025-07-17"
    site_label = sys.argv[2] if len(sys.argv) > 2 else "Northgate"
    scenario   = sys.argv[3] if len(sys.argv) > 3 else "a2"
    plot_day_view(date_str, site_label, scenario)
