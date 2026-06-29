"""
northgate_plot_all_days.py
==========================
Generate per-day 4-panel XOS day-view figures for all Northgate days from
the pre-saved per_day CSVs (dispatch, state, grid_draw, vehicle_results).

Layout per figure:
  ax1  XOS A2 hub Gantt  (disconnect at 20% SOC)
  ax2  XOS A1 hub Gantt  (always-grid-connected)
  ax3  Site grid demand  (A2=red, A1=blue, SMUD peak window shaded)
  ax4  Vehicle legend    (V-number → make/model)

Saves:  per_day/{date}/day_view_{date}.png

Usage:
    python northgate_plot_all_days.py              # all days
    python northgate_plot_all_days.py 2025-07-17   # single day (for testing)
"""
from __future__ import annotations

import io, sys, math, contextlib
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))
import scenario_runner as sr

BASE_DIR  = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
PER_DAY   = BASE_DIR / "scenario_outputs" / "northgate_analysis" / "per_day"
CSV_STEM  = "z2z_milp_events_northgate"
TZ        = "America/Los_Angeles"
NP        = 4   # CCS1 ports per hub
DT_MIN    = 15  # minutes per time step

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
    col    = f"state_unit_{hub_k}"
    if col not in state_df.columns:
        return []
    times  = pd.to_datetime(state_df["time_utc"], utc=True)
    states = state_df[col].tolist()
    n      = len(states)
    blocks = []
    i = 0
    while i < n:
        s = states[i]
        j = i + 1
        while j < n and states[j] == s:
            j += 1
        x0 = to_x(times.iloc[i])
        x1 = to_x(times.iloc[j - 1]) + DT_MIN
        blocks.append((x0, x1, s))
        i = j
    return blocks


def _load_events(date_str: str) -> pd.DataFrame | None:
    """Load + preprocess events for one day (with multiday rule + extended dwell)."""
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


def _draw_xos_gantt(ax, dispatch_df: pd.DataFrame, state_df: pd.DataFrame,
                    to_x, x_end_min: float, tick_xs, tick_labels,
                    vid_dwell: dict, vid_color: dict, date_str: str,
                    scenario_label: str):
    """Draw one XOS hub Gantt panel onto ax."""
    PORT_H  = 0.36
    HUB_GAP = 0.22

    # Build port windows: (unit, port) → list of (t_start, t_end, event_id)
    port_windows: dict[tuple, list] = {}
    for (unit_k, port_p, eid), grp in dispatch_df.groupby(["unit", "port", "event_id"]):
        ts = grp["time_utc"].sort_values()
        t0 = ts.iloc[0]
        t1 = ts.iloc[-1] + pd.Timedelta(minutes=DT_MIN)
        port_windows.setdefault((int(unit_k), int(port_p)), []).append((t0, t1, eid))

    active_hubs = sorted({u for u, _ in port_windows})
    if not active_hubs:
        ax.text(0.5, 0.5, "No dispatch data", ha="center", va="center",
                transform=ax.transAxes, fontsize=11, color="gray")
        return 0.5

    # Y-coordinate map
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
    y_total = y_cur - HUB_GAP

    for hub_k in active_hubs:
        y_bot, y_top = hub_y_spans[hub_k]

        # Hub-state background
        for xb0, xb1, state in _hub_state_blocks(state_df, hub_k, to_x):
            color, alpha = STATE_COLOR.get(state, ("#ffffff", 0.0))
            ax.fill_betweenx([y_bot, y_top], xb0, xb1,
                              color=color, alpha=alpha, linewidth=0, zorder=1)

        # Port separator lines
        for p in range(1, NP):
            ax.axhline(y_bot + p * PORT_H, color="gray",
                       linewidth=0.3, linestyle=":", alpha=0.5, zorder=2)

        # Vehicle dwell + charge bars
        for p in range(NP):
            y_c = y_map[(hub_k, p)]
            for t0, t1, eid in port_windows.get((hub_k, p), []):
                color = vid_color.get(eid, "steelblue")
                xv0, xv1 = to_x(t0), to_x(t1)

                # Dwell window (light bar)
                if eid in vid_dwell:
                    arr_t, dep_t = vid_dwell[eid]
                    xa, xd = to_x(arr_t), to_x(dep_t)
                    ax.barh(y_c, max(xd - xa, 1), left=xa, height=PORT_H * 0.78,
                            color=color, alpha=0.18, edgecolor=color,
                            linewidth=0.5, zorder=2)

                # Charge bar (solid)
                ax.barh(y_c, max(xv1 - xv0, 1), left=xv0, height=PORT_H * 0.78,
                        color=color, alpha=0.88, edgecolor="white",
                        linewidth=0.3, zorder=3)

                # V-number label
                lbl = _vid_label(eid, date_str)
                ax.text((xv0 + xv1) / 2, y_c, lbl,
                        ha="center", va="center", fontsize=4.8,
                        color="black", fontweight="bold", clip_on=True, zorder=4)

        # Hub group separator
        ax.axhline(y_top, color="#888888", linewidth=0.6, alpha=0.5, zorder=2)

    # Port sub-labels (P0–P3) on the left margin
    for hub_k in active_hubs:
        for p in range(NP):
            ax.text(-x_end_min * 0.003, y_map[(hub_k, p)],
                    f"P{p}", ha="right", va="center",
                    fontsize=3.8, color="#555555", clip_on=False)

    ax.set_xlim(0, x_end_min)
    ax.set_ylim(y_total + 0.10, -0.10)
    ax.set_yticks([hub_label_y[k] for k in active_hubs])
    ax.set_yticklabels([f"Hub {k+1}" for k in active_hubs], fontsize=7, va="center")
    ax.set_xticks(tick_xs); ax.set_xticklabels(tick_labels, fontsize=7.5, rotation=30, ha="right")
    ax.grid(axis="x", linestyle=":", alpha=0.35, color="gray")
    ax.set_ylabel(scenario_label, fontsize=7.5)

    # State legend
    patches = [
        mpatches.Patch(color=STATE_COLOR["idle"][0],       alpha=STATE_COLOR["idle"][1],       label="Hub idle"),
        mpatches.Patch(color=STATE_COLOR["serving"][0],    alpha=STATE_COLOR["serving"][1],    label="Hub serving"),
        mpatches.Patch(color=STATE_COLOR["recharging"][0], alpha=STATE_COLOR["recharging"][1], label="Hub recharging (grid)"),
        mpatches.Patch(facecolor="gray", alpha=0.22, edgecolor="gray", linewidth=0.6,          label="Vehicle dwell"),
    ]
    ax.legend(handles=patches, loc="upper right", fontsize=6.5, ncol=4, framealpha=0.90)

    return y_total


# ── main per-day plot ──────────────────────────────────────────────────────────

def plot_one_day(date_str: str, day_dir: Path, events_ext: pd.DataFrame) -> Path | None:
    a2_disp  = day_dir / f"A2_dispatch_{date_str}.csv"
    a2_state = day_dir / f"A2_state_{date_str}.csv"
    a2_grid  = day_dir / f"A2_grid_draw_{date_str}.csv"
    a1_disp  = day_dir / f"A1_dispatch_{date_str}.csv"
    a1_state = day_dir / f"A1_state_{date_str}.csv"
    a1_grid  = day_dir / f"A1_grid_draw_{date_str}.csv"

    # At least one scenario must have dispatch data
    if not a2_disp.exists() and not a1_disp.exists():
        return None

    def _load(p: Path) -> pd.DataFrame:
        return pd.read_csv(p) if p.exists() else pd.DataFrame()

    a2_d = _load(a2_disp);  a2_s = _load(a2_state);  a2_g = _load(a2_grid)
    a1_d = _load(a1_disp);  a1_s = _load(a1_state);  a1_g = _load(a1_grid)

    for df in (a2_d, a1_d):
        if "time_utc" in df.columns:
            df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    for df in (a2_s, a1_s):
        if "time_utc" in df.columns:
            df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    for df in (a2_g, a1_g):
        if "time_utc" in df.columns:
            df["time_pac"] = pd.to_datetime(df["time_utc"], utc=True).dt.tz_convert(TZ)

    # Vehicle info from events_ext
    all_vids  = events_ext["charging_event_id"].tolist()
    vid_model = {row["charging_event_id"]: str(row.get("ev_equivalent_model", "") or "")
                 for _, row in events_ext.iterrows()}
    vid_dwell = {row["charging_event_id"]: (
                     pd.to_datetime(row["arrival_time"], utc=True),
                     pd.to_datetime(row["departure_time"], utc=True))
                 for _, row in events_ext.iterrows()}

    # Colour map — one colour per vehicle
    cmap      = plt.cm.get_cmap("tab20", max(len(all_vids), 20))
    vid_color = {v: cmap(i) for i, v in enumerate(all_vids)}

    # Common x-axis: minutes from midnight Pacific of date_str
    t_ref_pac = pd.Timestamp(date_str, tz=TZ)

    def to_x(t) -> float:
        tl = t.tz_convert(TZ) if hasattr(t, "tz_convert") else t
        return (tl - t_ref_pac).total_seconds() / 60.0

    # Determine x range from events + grid data
    all_times = list(events_ext["arrival_time"]) + list(events_ext["departure_time"])
    if not a2_g.empty and "time_pac" in a2_g.columns:
        all_times += list(a2_g["time_pac"])
    if not a1_g.empty and "time_pac" in a1_g.columns:
        all_times += list(a1_g["time_pac"])
    t_global_start = min(pd.to_datetime(t, utc=True) for t in all_times).tz_convert(TZ).floor("1h")
    t_global_end   = max(pd.to_datetime(t, utc=True) for t in all_times).tz_convert(TZ).ceil("1h")

    # Use t_ref_pac as x=0; negative x for anything before midnight is fine
    x_end_min = (t_global_end - t_ref_pac).total_seconds() / 60.0
    x_beg_min = (t_global_start - t_ref_pac).total_seconds() / 60.0

    t_tick = t_global_start.ceil("2h")
    tick_xs, tick_labels = [], []
    while t_tick <= t_global_end:
        tick_xs.append((t_tick - t_ref_pac).total_seconds() / 60.0)
        tick_labels.append(t_tick.strftime("%H:%M"))
        t_tick += pd.Timedelta(hours=2)

    # Hub counts for panel height
    n_a2_hubs = int(a2_d["unit"].max()) + 1 if not a2_d.empty and "unit" in a2_d.columns else 1
    n_a1_hubs = int(a1_d["unit"].max()) + 1 if not a1_d.empty and "unit" in a1_d.columns else 1

    PORT_H = 0.36; HUB_GAP = 0.22
    a2_h = n_a2_hubs * (NP * PORT_H + HUB_GAP)
    a1_h = n_a1_hubs * (NP * PORT_H + HUB_GAP)
    dem_h  = 3.8
    leg_h  = max(2.2, math.ceil(len(all_vids) / 5) * 0.38 + 0.6)

    fig_h = a2_h + a1_h + dem_h + leg_h + 2.0
    fig, (ax1, ax2, ax3, ax4) = plt.subplots(
        4, 1, figsize=(26, max(fig_h, 18)),
        gridspec_kw={"height_ratios": [a2_h, a1_h, dem_h, leg_h]},
    )
    fig.subplots_adjust(hspace=0.10, left=0.09, right=0.99, top=0.97, bottom=0.02)

    # ── ax1: XOS A2 Gantt ────────────────────────────────────────────────────
    ax1.set_title(
        f"Northgate  |  {date_str}  |  XOS Hub MC02  —  A1 & A2 scenarios  "
        f"(proactive recharge enabled)",
        fontsize=12, fontweight="bold", pad=6)
    _draw_xos_gantt(ax1, a2_d, a2_s, to_x, x_end_min, tick_xs, tick_labels,
                    vid_dwell, vid_color, date_str,
                    f"XOS A2 — {n_a2_hubs} hubs\ndisconnect at 20% SOC")

    # ── ax2: XOS A1 Gantt ────────────────────────────────────────────────────
    _draw_xos_gantt(ax2, a1_d, a1_s, to_x, x_end_min, tick_xs, tick_labels,
                    vid_dwell, vid_color, date_str,
                    f"XOS A1 — {n_a1_hubs} hubs\nalways-grid-connected")

    # ── ax3: Power demand ─────────────────────────────────────────────────────
    if not a2_g.empty and "grid_kw" in a2_g.columns:
        xs2 = [(t - t_ref_pac).total_seconds() / 60.0 for t in a2_g["time_pac"]]
        ax3.plot(xs2, a2_g["grid_kw"], color="#d73027", linewidth=1.6,
                 label=f"XOS A2  ({n_a2_hubs} hubs, peak {int(a2_g['grid_kw'].max())} kW)")
        ax3.fill_between(xs2, a2_g["grid_kw"], alpha=0.10, color="#d73027")

    if not a1_g.empty and "grid_kw" in a1_g.columns:
        xs1 = [(t - t_ref_pac).total_seconds() / 60.0 for t in a1_g["time_pac"]]
        ax3.plot(xs1, a1_g["grid_kw"], color="#2166ac", linewidth=1.6, linestyle="--",
                 label=f"XOS A1  ({n_a1_hubs} hubs, peak {int(a1_g['grid_kw'].max())} kW)")
        ax3.fill_between(xs1, a1_g["grid_kw"], alpha=0.08, color="#2166ac")

    # SMUD peak window (16–21h)
    for day_offset in (0, 1440):
        pk0 = (t_ref_pac + pd.Timedelta(hours=16 + day_offset / 60) - t_ref_pac).total_seconds() / 60
        pk1 = (t_ref_pac + pd.Timedelta(hours=21 + day_offset / 60) - t_ref_pac).total_seconds() / 60
        if pk0 < x_end_min:
            ax3.axvspan(pk0, min(pk1, x_end_min), color="#fee08b", alpha=0.30,
                        label="SMUD peak window (16–21h)" if day_offset == 0 else "")

    ax3.set_xlim(x_beg_min, x_end_min)
    y3_max = max(
        (a2_g["grid_kw"].max() if not a2_g.empty and "grid_kw" in a2_g.columns else 0),
        (a1_g["grid_kw"].max() if not a1_g.empty and "grid_kw" in a1_g.columns else 0),
        1,
    ) * 1.15
    ax3.set_ylim(0, y3_max)
    ax3.set_xticks(tick_xs); ax3.set_xticklabels(tick_labels, fontsize=8.5, rotation=30, ha="right")
    ax3.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax3.set_ylabel("Grid draw (kW)", fontsize=9)
    ax3.set_xlabel(f"Time (Pacific)  —  {date_str}", fontsize=9)
    ax3.legend(loc="upper left", fontsize=8, framealpha=0.92)
    ax3.grid(axis="both", linestyle=":", alpha=0.30)
    ax3.axhline(0, color="gray", linewidth=0.5)

    # ── ax4: Vehicle legend ───────────────────────────────────────────────────
    ax4.axis("off")
    ax4.set_title("Vehicle legend  (solid bar = charging,  light bar = on-site dwell window)",
                  fontsize=8.5, fontweight="bold", pad=3, loc="left")

    legend_rows = []
    for eid in all_vids:
        lbl     = _vid_label(eid, date_str)
        model   = vid_model.get(eid, "")
        color   = vid_color[eid]
        num_str = lbl.rstrip("p")
        num     = int(num_str[1:]) if num_str[1:].isdigit() else 999
        legend_rows.append((lbl.endswith("p"), num, lbl, model, color))
    legend_rows.sort()

    N_COLS = 5
    n_rows = math.ceil(len(legend_rows) / N_COLS)
    PATCH_W = 0.024; PATCH_H = 0.052; COL_W = 1.0 / N_COLS

    for idx, (_, _, lbl, model, color) in enumerate(legend_rows):
        col = idx % N_COLS
        row = idx // N_COLS
        x0  = col * COL_W + 0.005
        y0  = 1.0 - (row + 1) * (1.0 / (n_rows + 0.5))

        ax4.add_patch(mpatches.FancyBboxPatch(
            (x0, y0), PATCH_W, PATCH_H, boxstyle="round,pad=0.002",
            facecolor=color, edgecolor="none",
            transform=ax4.transAxes, clip_on=True, zorder=3))
        ax4.text(x0 + PATCH_W + 0.007, y0 + PATCH_H / 2,
                 f"{lbl}: {model}",
                 ha="left", va="center", fontsize=7.0,
                 transform=ax4.transAxes, clip_on=True)

    out = day_dir / f"day_view_{date_str}.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


# ── batch runner ───────────────────────────────────────────────────────────────

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None  # optional single-day filter

    day_dirs = sorted(PER_DAY.iterdir()) if PER_DAY.exists() else []
    n = len(day_dirs)
    print(f"Generating day-view figures for {n} day folders.")
    print(f"Output: per_day/{{date}}/day_view_{{date}}.png\n")

    ok = skip = fail = 0
    for i, day_dir in enumerate(day_dirs, 1):
        date_str = day_dir.name
        if target and date_str != target:
            continue
        if not day_dir.is_dir():
            continue

        pct = 100 * i / n
        print(f"  [{i:3d}/{n}] {date_str}  ({pct:.0f}%)", end="  ", flush=True)

        events_ext = _load_events(date_str)
        if events_ext is None or events_ext.empty:
            print("skipped (no events)")
            skip += 1
            continue

        try:
            out = plot_one_day(date_str, day_dir, events_ext)
            if out:
                print(f"saved  ({out.name})")
                ok += 1
            else:
                print("skipped (no dispatch data)")
                skip += 1
        except Exception as e:
            print(f"ERROR: {e}")
            fail += 1

    print(f"\nDone.  Saved={ok}  Skipped={skip}  Errors={fail}")


if __name__ == "__main__":
    main()
