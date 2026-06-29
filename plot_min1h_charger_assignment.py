"""
plot_min1h_charger_assignment.py  (v3 — 4-charger-type mix)
- Handles L2_19p2kW, DC_50kW (x2), DC_150kW, DC_350kW
- Panel 2: per-charger-type Gantt rows (with physical disambiguation for x2 types)
- Panel 3: vehicle dwell + session windows colored by charger type
- Panel 5: detailed table per vehicle
"""

import math
from pathlib import Path
from collections import defaultdict

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

# ── Paths ─────────────────────────────────────────────────────────────────────
import os as _os
NEW_DIR    = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\exact_milp_outputs_min1h_exact")
ORIG_DIR   = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\exact_milp_outputs")
EVENTS_CSV = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\z2z_milp_events_northgate_2025_06_30.csv")
if _os.environ.get("MILP_NEW_DIR"):
    NEW_DIR  = Path(_os.environ["MILP_NEW_DIR"])
    ORIG_DIR = NEW_DIR   # no separate baseline for batch runs
if _os.environ.get("MILP_EVENTS_CSV"):
    EVENTS_CSV = Path(_os.environ["MILP_EVENTS_CSV"])

# ── Load data ─────────────────────────────────────────────────────────────────
sched_df  = pd.read_csv(NEW_DIR / "exact_milp_charging_schedule.csv")
prof_df   = pd.read_csv(NEW_DIR / "exact_milp_site_power_profile.csv")
evres_df  = pd.read_csv(NEW_DIR / "exact_milp_event_results.csv")
events_df = pd.read_csv(EVENTS_CSV)
cost_df   = pd.read_csv(NEW_DIR / "exact_milp_cost_breakdown.csv")
mix_df    = pd.read_csv(NEW_DIR / "exact_milp_selected_charger_mix.csv")
orig_prof = pd.read_csv(ORIG_DIR / "exact_milp_site_power_profile.csv")
orig_cost = pd.read_csv(ORIG_DIR / "exact_milp_cost_breakdown.csv")
orig_mix  = pd.read_csv(ORIG_DIR / "exact_milp_selected_charger_mix.csv")

# ── Parse timestamps ──────────────────────────────────────────────────────────
TZ_LA    = "America/Los_Angeles"
_ref_date = _os.environ.get("MILP_DATE", "2025-06-30")
REF       = pd.Timestamp(_ref_date, tz=TZ_LA)

def to_pdt_h(ts_str):
    ts = pd.to_datetime(ts_str, utc=True).tz_convert(TZ_LA)
    return (ts - REF).total_seconds() / 3600.0

sched_df["t_start_h"] = sched_df["time_step_start"].apply(to_pdt_h)
sched_df["t_end_h"]   = sched_df["time_step_end"].apply(to_pdt_h)
events_df["arr_h"]    = events_df["arrival_time"].apply(to_pdt_h)
events_df["dep_h"]    = events_df["departure_time"].apply(to_pdt_h)
events_df = events_df.sort_values("arr_h").reset_index(drop=True)

# Map prof_df hour column (stored as offset hours from start of day) to PDT hours
# The prof_df may use absolute UTC or offset hours — detect from column
if "hour" in prof_df.columns:
    try:
        prof_df["hour_pdt"] = prof_df["hour"].apply(
            lambda h: to_pdt_h(REF + pd.Timedelta(hours=float(h)))
        )
    except Exception:
        prof_df["hour_pdt"] = prof_df["hour"]

# ── Colour palette ────────────────────────────────────────────────────────────
BG, BG2, BG3  = "#0A0E1A", "#12182B", "#1A2235"
DGRAY, LGRAY  = "#252D40", "#4A5470"
WHITE         = "#E8E8FF"
BLUE          = "#00BFFF"
ORANGE        = "#FF7043"
BLUE_SESSION  = "#003366"
ORANGE_SESSION= "#4A1500"
GREEN         = "#39FF14"
AMBER         = "#FFB300"
RED           = "#FF4B4B"
PURPLE        = "#9B59B6"
CYAN          = "#00E5FF"
TEAL          = "#00BFA5"
PINK          = "#FF4081"

# Charger-type colour mapping
CTYPE_COLOR = {
    "L2_19p2kW": TEAL,
    "DC_50kW":   GREEN,
    "DC_150kW":  BLUE,
    "DC_350kW":  ORANGE,
}
CTYPE_SESSION = {
    "L2_19p2kW": "#003322",
    "DC_50kW":   "#003300",
    "DC_150kW":  BLUE_SESSION,
    "DC_350kW":  ORANGE_SESSION,
}

VEH_PALETTE = [
    "#FF6B6B","#FFA500","#FFD700","#7FFF00","#00CED1",
    "#1E90FF","#DA70D6","#FF1493","#00FA9A","#FF4500",
    "#9370DB","#20B2AA","#F08080","#90EE90","#87CEEB","#DDA0DD",
]
MODEL_SHORT = {
    "Freightliner eCascadia":       "eCascadia",
    "Freightliner eM2":             "eM2",
    "Ford F-150 Lightning":         "F-150",
    "Tesla Model 3":                "Tesla M3",
    "Rivian R1T":                   "Rivian",
    "Rivian R1S":                   "Rivian S",
    "GMC Hummer EV":                "Hummer",
    "BYD 6F Cab-Forward Truck":     "BYD 6F",
    "BYD 6F Cab-Forward":           "BYD 6F",
    "Chevrolet Bolt EV":            "Bolt",
    "Chevrolet Silverado EV WT":    "Silverado EV",
    "Blue Arc EV":                  "Blue Arc",
    "Global Electric Street Sweeper (M4E)": "Sweeper",
}

veh_ids = sorted(events_df["charging_event_id"].unique())
VEH_COLOR = {v: VEH_PALETTE[i % len(VEH_PALETTE)] for i, v in enumerate(veh_ids)}

vid_info = {}
for _, row in events_df.iterrows():
    model = str(row.get("ev_equivalent_model", ""))
    short = MODEL_SHORT.get(model, model[:10])
    vid_info[row["charging_event_id"]] = {
        "short":        short,
        "energy":       row["energy_needed_kwh_for_visit"],
        "arr_h":        row["arr_h"],
        "dep_h":        row["dep_h"],
        "dwell_actual": row.get("dwell_hours_actual", row["dwell_hours"]),
        "dwell_eff":    row["dwell_hours"],
        "model":        model,
        "idx":          events_df[events_df["charging_event_id"] == row["charging_event_id"]].index[0],
    }

# ── Build per-charger-type session segments ───────────────────────────────────
# For charger types with count > 1, greedily assign to physical units.
def build_phys_assignments(sched, charger_type, n_units):
    """Return dict: vid -> unit_id (1-indexed), and segments per unit."""
    sub = sched[sched["charger_type"] == charger_type].copy()
    if sub.empty:
        return {}, {i: [] for i in range(1, n_units + 1)}

    # Session extents per vehicle
    veh_sess = {}
    for vid, grp in sub.groupby("charging_event_id"):
        veh_sess[vid] = (grp["t_start_h"].min(), grp["t_end_h"].max(),
                         grp["power_kw"].mean(), grp["energy_delivered_kwh"].sum())

    sorted_v = sorted(veh_sess.items(), key=lambda x: x[1][0])
    free_until = {i: -999.0 for i in range(1, n_units + 1)}
    vid_to_unit = {}

    for vid, (start, end, avg_pw, kwh) in sorted_v:
        # Assign to earliest-freeing unit
        best_unit = min(free_until, key=lambda u: free_until[u])
        if free_until[best_unit] <= start + 1e-3:
            vid_to_unit[vid] = best_unit
            free_until[best_unit] = end
        else:
            # All busy: assign to earliest freeing (overlap allowed)
            vid_to_unit[vid] = best_unit
            free_until[best_unit] = end

    # Build segment lists per unit
    unit_segs = {i: [] for i in range(1, n_units + 1)}
    for _, row in sub.sort_values("t_start_h").iterrows():
        vid = row["charging_event_id"]
        u   = vid_to_unit.get(vid, 1)
        unit_segs[u].append((vid, row["t_start_h"], row["t_end_h"], row["power_kw"]))

    return vid_to_unit, unit_segs, veh_sess

# Per-charger-type counts from mix
count_map = {row["charger_type"]: int(row["count"]) for _, row in mix_df.iterrows()}
ACTIVE_TYPES = [ct for ct in ["DC_350kW", "DC_150kW", "DC_50kW", "L2_19p2kW"]
                if count_map.get(ct, 0) > 0]

phys_assign = {}   # ctype -> vid_to_unit dict
unit_segs   = {}   # ctype -> {unit_id -> [(vid, ts, te, pw)]}
veh_sess_by_ctype = {}  # ctype -> {vid -> (start, end, avg, kwh)}

for ct in ACTIVE_TYPES:
    n = count_map.get(ct, 0)
    res = build_phys_assignments(sched_df, ct, n)
    phys_assign[ct] = res[0]
    unit_segs[ct]   = res[1]
    veh_sess_by_ctype[ct] = res[2]

# Flat vid -> primary charger type (the one with highest power used)
def primary_charger(vid):
    best_c, best_p = None, -1
    for _, row in sched_df[sched_df["charging_event_id"] == vid].iterrows():
        if row["power_kw"] > best_p:
            best_p = row["power_kw"]
            best_c = row["charger_type"]
    return best_c

vid_primary_ctype = {vid: primary_charger(vid) for vid in veh_ids}

# Per-vehicle slot list
veh_x_slots = defaultdict(list)
for _, row in sched_df.iterrows():
    veh_x_slots[row["charging_event_id"]].append(
        (row["t_start_h"], row["t_end_h"], row["power_kw"], row["charger_type"]))

# ── Cost helper ───────────────────────────────────────────────────────────────
def get_cost(df, name):
    r = df[df["component"] == name]
    return float(r["value"].iloc[0]) if len(r) else 0.0

capex_new   = float(mix_df["total_daily_capex"].sum())
energy_new  = get_cost(cost_df, "energy_cost")
demand_new  = get_cost(cost_df, "global_demand_cost")
pkwin_new   = get_cost(cost_df, "peak_window_demand_cost")
smooth_new  = get_cost(cost_df, "smoothing_cost")
total_new   = capex_new + energy_new + demand_new + pkwin_new + smooth_new
pmax_new    = get_cost(cost_df, "P_max_kw")

capex_orig  = float(orig_mix["total_daily_capex"].sum())
energy_orig = get_cost(orig_cost, "energy_cost")
demand_orig = get_cost(orig_cost, "global_demand_cost")
pkwin_orig  = get_cost(orig_cost, "peak_window_demand_cost")
smooth_orig = get_cost(orig_cost, "smoothing_cost")
total_orig  = capex_orig + energy_orig + demand_orig + pkwin_orig + smooth_orig
pmax_orig   = get_cost(orig_cost, "P_max_kw")

new_p, new_h   = prof_df["P_total_kw"].values, prof_df["hour_pdt"].values
orig_p, orig_h = orig_prof["P_total_kw"].values, orig_prof["hour"].values

# ── Matplotlib config ─────────────────────────────────────────────────────────
plt.rcParams.update({
    "figure.facecolor": BG, "axes.facecolor": BG2,
    "axes.edgecolor": LGRAY, "axes.labelcolor": WHITE,
    "xtick.color": WHITE, "ytick.color": WHITE, "text.color": WHITE,
    "grid.color": LGRAY, "grid.alpha": 0.2,
    "legend.facecolor": BG2, "legend.edgecolor": LGRAY,
    "font.family": "DejaVu Sans",
})

X_MAX  = 32
SOURCE = "Source: Northgate MILP | Z2Z Dataset (use_for_opt filter) | Min-1h Dwell | 5-min steps | lambda=1.5 | energy_error_penalty=1.0 | Gurobi | 2025-06-30"

def xticks(ax, step=2):
    ticks = list(range(0, X_MAX + 1, step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([f"{int(h)%24:02d}:00" for h in ticks], fontsize=7.5)
    ax.set_xlim(0, X_MAX)

def add_peak_shade(ax, ymax=None):
    ax.axvspan(17, 20, alpha=0.10, color=AMBER, zorder=0)
    if ymax:
        ax.text(18.5, ymax * 0.92, "Peak\n17-20h",
                ha="center", fontsize=7, color=AMBER,
                bbox=dict(boxstyle="round,pad=0.2", facecolor=BG3, edgecolor=AMBER))

def h_to_hhmm(h):
    prefix = f"+{int(h)//24}d " if h >= 24 else ""
    hh = int(h) % 24
    mm = int(round((h % 1) * 60))
    if mm == 60:
        hh += 1; mm = 0
    return f"{prefix}{hh:02d}:{mm:02d}"

def unit_color(ctype, unit_id, n_units):
    """Slightly shade each physical unit for multi-unit charger types."""
    base_hex = CTYPE_COLOR.get(ctype, WHITE).lstrip("#")
    r, g, b  = [int(base_hex[i:i+2], 16) for i in (0, 2, 4)]
    if n_units > 1 and unit_id == 2:
        # Darken second unit slightly
        r = max(0, r - 50); g = max(0, g - 50); b = max(0, b - 50)
    return f"#{r:02X}{g:02X}{b:02X}"

# ─ Build Gantt row layout ─────────────────────────────────────────────────────
# Row order (bottom to top): L2 units, DC_50 units, DC_150 units, DC_350 units
gantt_rows = []   # list of (label, ctype, unit_id, row_y, edge_color, active_color)
y = 0
CTYPE_SHORT = {
    "L2_19p2kW": "L2  19.2 kW",
    "DC_50kW":   "DC  50 kW",
    "DC_150kW":  "DC  150 kW",
    "DC_350kW":  "DC  350 kW",
}
ctype_display_order = ["L2_19p2kW", "DC_50kW", "DC_150kW", "DC_350kW"]
for ct in ctype_display_order:
    n = count_map.get(ct, 0)
    if n == 0:
        continue
    base_col = CTYPE_COLOR[ct]
    for u in range(1, n + 1):
        label = f"{CTYPE_SHORT[ct]}  #{u}"
        gantt_rows.append((label, ct, u, y, base_col, unit_color(ct, u, n)))
        y += 1

n_gantt_rows = len(gantt_rows)

# ═════════════════════════════════════════════════════════════════════════════
# FIGURE
# ═════════════════════════════════════════════════════════════════════════════
fig = plt.figure(figsize=(22, 30))
fig.patch.set_facecolor(BG)

charger_mix_str = "  +  ".join(
    f"{int(row['count'])}x {row['charger_type']}" for _, row in mix_df.iterrows()
)
fig.suptitle(
    f"Northgate MILP  --  Min-1h Dwell  |  5-min Steps  |  Optimal Mix: {charger_mix_str}",
    fontsize=14, fontweight="bold", color=WHITE, y=0.99,
)

gs = fig.add_gridspec(5, 2,
                      height_ratios=[1.1, 1.4, 1.8, 0.9, 0.9],
                      hspace=0.48, wspace=0.28,
                      left=0.07, right=0.97, top=0.965, bottom=0.03)

# ─── Panel 1: Site power profile — stacked vehicle bars ──────────────────────
ax1 = fig.add_subplot(gs[0, :])
ax1.set_facecolor(BG2)

DT_SLOT = 5 / 60  # slot width in hours (5-min steps)

# Original baseline: solid amber step fill (background reference)
ax1.fill_between(orig_h, 0, orig_p, step="post", color=AMBER, alpha=0.22)
ax1.step(orig_h, orig_p, where="post", color=AMBER, lw=1.5, ls="--", alpha=0.85,
         label=f"Original (peak {pmax_orig:.1f} kW, ${total_orig:.0f}/day)")

# New run: stacked solid vehicle bars — each rect = one 5-min slot per vehicle
for t_h, grp in sched_df[sched_df["t_start_h"] < X_MAX].groupby("t_start_h"):
    bottom = 0.0
    for _, row in grp.sort_values("power_kw").iterrows():
        vid  = row["charging_event_id"]
        pw   = row["power_kw"]
        vcol = VEH_COLOR.get(vid, WHITE)
        rect = mpatches.Rectangle(
            (t_h, bottom), DT_SLOT, pw,
            facecolor=vcol, edgecolor=BG2, linewidth=0.25, zorder=3
        )
        ax1.add_patch(rect)
        bottom += pw

p1_ymax = max(new_p) if new_p.max() > 0 else 50
add_peak_shade(ax1, p1_ymax * 1.3)
ax1.axhline(pmax_new,  color=CYAN,  lw=0.9, ls=":", alpha=0.7,
            label=f"New peak = {pmax_new:.0f} kW")
ax1.axhline(pmax_orig, color=AMBER, lw=0.9, ls=":", alpha=0.6,
            label=f"Orig peak = {pmax_orig:.0f} kW")
# Vehicle colour legend handles (from VEH_COLOR)
veh_handles = [
    mpatches.Patch(color=VEH_COLOR[v], label="v" + v.split("_v")[-1])
    for v in sorted(VEH_COLOR)
]
n_events_new = len(events_df)
legend1 = ax1.legend(fontsize=8, loc="upper left", ncol=1,
                     title=f"Z2Z {n_events_new}-event (${total_new:.0f}/day)  |  colour = vehicle")
ax1.add_artist(legend1)
ax1.legend(handles=veh_handles, fontsize=6.5, loc="upper right", ncol=4,
           facecolor=BG2, edgecolor=LGRAY, title="Vehicles", title_fontsize=6.5)
ax1.set_ylabel("Site Power (kW)", fontsize=10)
ax1.set_ylim(0, p1_ymax * 1.45)
xticks(ax1)
ax1.grid(alpha=0.15)
ax1.set_title("Site Power Demand Profile  |  2025-06-30  (PDT)  "
              "|  Each bar = one vehicle's 5-min charging slot",
              fontsize=10, color=LGRAY, pad=5)

# ─── Panel 2: Physical charger Gantt ──────────────────────────────────────────
ax2 = fig.add_subplot(gs[1, :])
ax2.set_facecolor(BG2)

for (row_label, ct, unit_id, row_y, edge_col, active_col) in gantt_rows:
    n    = count_map.get(ct, 0)
    sess = veh_sess_by_ctype.get(ct, {})
    segs = unit_segs.get(ct, {}).get(unit_id, [])
    v2u  = phys_assign.get(ct, {})
    sess_col = CTYPE_SESSION.get(ct, DGRAY)

    # Background session windows
    for vid, (start, end, avg_pw, kwh) in sess.items():
        if v2u.get(vid) != unit_id:
            continue
        if start > X_MAX:
            continue
        ax2.barh(row_y, min(end, X_MAX) - start, left=start, height=0.72,
                 color=sess_col, alpha=0.55, edgecolor="none", zorder=2)

    # Active charging bars — collapse consecutive same-vehicle slots
    DT_GAP = 0.15  # hours
    def draw_gantt_segs(segs_list):
        state = {"prev_vid": None, "seg_start": None, "seg_end": None, "buf": []}

        def flush():
            if state["prev_vid"] is None:
                return
            seg_w = min(state["seg_end"], X_MAX) - state["seg_start"]
            if seg_w <= 0:
                state["prev_vid"] = None; return
            vcol = VEH_COLOR.get(state["prev_vid"], WHITE)
            ax2.barh(row_y, seg_w, left=state["seg_start"], height=0.52,
                     color=vcol, alpha=0.92, edgecolor=edge_col, linewidth=1.8, zorder=4)
            avg_pw_ = np.mean([pw for _, _, pw in state["buf"]]) if state["buf"] else 0
            info = vid_info.get(state["prev_vid"], {})
            short = info.get("short", state["prev_vid"][-4:])
            lbl = f"{short}\n{avg_pw_:.0f}kW"
            brightness = sum(int(vcol.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
            txt_c = BG if brightness > 380 else WHITE
            if seg_w >= 0.6:
                ax2.text(state["seg_start"] + seg_w / 2, row_y, lbl,
                         ha="center", va="center", fontsize=6.0, color=txt_c, fontweight="bold", zorder=6)
            elif seg_w >= 0.22:
                ax2.text(state["seg_start"] + seg_w / 2, row_y, short[:5],
                         ha="center", va="center", fontsize=5.0, color=WHITE, zorder=6)
            state["prev_vid"] = None; state["buf"] = []

        for (vid, ts, te, pw) in sorted(segs_list, key=lambda x: x[1]):
            if ts > X_MAX:
                continue
            if vid == state["prev_vid"] and abs(ts - state["seg_end"]) < DT_GAP:
                state["seg_end"] = te
                state["buf"].append((ts, te, pw))
            else:
                flush()
                state["prev_vid"], state["seg_start"], state["seg_end"] = vid, ts, te
                state["buf"] = [(ts, te, pw)]
        flush()

    draw_gantt_segs(segs)

add_peak_shade(ax2)
ax2.axvline(24, color=WHITE, lw=0.8, ls=":", alpha=0.4)

ytick_locs  = [r[3] for r in gantt_rows]
ytick_lbls  = [r[0] for r in gantt_rows]
ax2.set_yticks(ytick_locs)
ax2.set_yticklabels(ytick_lbls, fontsize=8.5, color=WHITE)
ax2.set_ylim(-0.6, n_gantt_rows - 0.3)
xticks(ax2)
ax2.set_xlabel("Time of Day (PDT)  |  Hours > 24 = July 1", fontsize=9)
ax2.grid(axis="x", alpha=0.2)
ax2.set_title(
    "Physical Charger Gantt  --  One row per physical unit\n"
    "Dark strip = vehicle plug-in window  |  Filled bar = active charging  |  Bar fill = vehicle color",
    fontsize=9, color=LGRAY, pad=5)

# Legend for charger type colors
legend_handles = [mpatches.Patch(color=CTYPE_COLOR[ct], label=ct.replace("_", " "))
                  for ct in ctype_display_order if count_map.get(ct, 0) > 0]
ax2.legend(handles=legend_handles, fontsize=8, loc="upper right", framealpha=0.85,
           title="Charger type (edge color)", title_fontsize=7)

# ─── Panel 3: Vehicle dwell Gantt ─────────────────────────────────────────────
ax3 = fig.add_subplot(gs[2, :])
ax3.set_facecolor(BG2)

for i, (_, evrow) in enumerate(events_df.iterrows()):
    vid      = evrow["charging_event_id"]
    arr_h    = evrow["arr_h"]
    dep_h    = evrow["dep_h"]
    dep_plot = min(dep_h, X_MAX)
    da       = evrow.get("dwell_hours_actual", evrow["dwell_hours"])
    de       = evrow["dwell_hours"]
    extended = de > da + 0.01
    info     = vid_info[vid]
    energy   = info["energy"]
    short    = info["short"]
    vnum     = vid.split("_v")[-1]

    # Layer 1: gray dwell background
    solid_end = min(max(arr_h + da, math.ceil(arr_h / (5/60)) * (5/60)), X_MAX)
    ax3.barh(i, solid_end - arr_h, left=arr_h, height=0.55,
             color=DGRAY, alpha=0.9, edgecolor=LGRAY, linewidth=0.6, zorder=2)
    if extended and arr_h + de > solid_end and solid_end < X_MAX:
        ax3.barh(i, min(arr_h + de, X_MAX) - solid_end, left=solid_end, height=0.55,
                 color=DGRAY, alpha=0.45, edgecolor=CYAN, linewidth=1.2,
                 linestyle="--", hatch="//", zorder=2)

    # Layer 2: continuous session window colored by primary charger type
    slots = veh_x_slots.get(vid, [])
    if slots:
        sess_s = min(s[0] for s in slots)
        sess_e = max(s[1] for s in slots)
        pct    = vid_primary_ctype.get(vid)
        sc     = CTYPE_SESSION.get(pct, DGRAY)
        ax3.barh(i, min(sess_e, X_MAX) - sess_s, left=sess_s, height=0.44,
                 color=sc, alpha=0.65, edgecolor="none", zorder=3)

    # Layer 3: active charging slots, colored by charger type
    for (ts, te, pw, ctype) in slots:
        if ts > X_MAX or pw <= 0:
            continue
        te_p  = min(te, X_MAX)
        c_col = CTYPE_COLOR.get(ctype, WHITE)
        ax3.barh(i, te_p - ts, left=ts, height=0.32,
                 color=c_col, alpha=0.92, edgecolor="none", zorder=4)

    ax3.text(arr_h - 0.15, i, f"v{vnum}", va="center", ha="right",
             fontsize=7.5, color=VEH_COLOR[vid], fontweight="bold")
    if dep_plot - arr_h > 1.0:
        mid = arr_h + (dep_plot - arr_h) / 2
        ax3.text(mid, i + 0.02, f"{short}  |  {energy:.1f} kWh",
                 va="center", ha="center", fontsize=7, color=WHITE, fontweight="bold", zorder=5)
    else:
        ax3.text(dep_plot + 0.1, i, f"{energy:.1f} kWh",
                 va="center", fontsize=6.5, color=WHITE)

add_peak_shade(ax3)
ax3.axvline(24, color=WHITE, lw=0.8, ls=":", alpha=0.4)
ax3.text(24.1, -1.0, "Midnight", fontsize=7, color=WHITE, alpha=0.5)
ax3.set_yticks(range(len(events_df)))
ax3.set_yticklabels([])
ax3.set_ylim(-1.2, len(events_df) + 0.2)
xticks(ax3)
ax3.set_xlabel("Time of Day (PDT)", fontsize=9)
ax3.grid(axis="x", alpha=0.2)
handles3 = [
    mpatches.Patch(color=DGRAY,         alpha=0.9, label="Dwell window"),
    mpatches.Patch(facecolor="none", edgecolor=CYAN, hatch="//", label="Extended dwell (min-1h)"),
] + [mpatches.Patch(color=CTYPE_COLOR[ct], label=ct.replace("_", " ") + " charging")
     for ct in ctype_display_order if count_map.get(ct, 0) > 0]
ax3.legend(handles=handles3, fontsize=7.5, loc="upper right", framealpha=0.85, ncol=3)
ax3.set_title(
    "Vehicle Dwell + Charging Sessions  --  Bar fill color = charger type used\n"
    "Medium strip = session window (includes idle gaps)  |  Thin inner bar = active charging",
    fontsize=9, color=LGRAY, pad=5)

# ─── Panel 4: Cost breakdown ──────────────────────────────────────────────────
ax4 = fig.add_subplot(gs[3, 0])
ax4.set_facecolor(BG2)
COST_LABELS = ["Charger CapEx", "Energy Cost", "Global Demand", "Peak-Win Demand", "Smoothing"]
COST_COLORS = ["#1E3A5F", GREEN, RED, AMBER, PURPLE]
orig_vals = [capex_orig, energy_orig, demand_orig, pkwin_orig, smooth_orig]
new_vals  = [capex_new,  energy_new,  demand_new,  pkwin_new,  smooth_new]
x_pos = [0.22, 0.68]
for xi, (vals, total, col) in zip(x_pos, [
    (orig_vals, total_orig, AMBER),
    (new_vals,  total_new,  BLUE),
]):
    bottom = 0
    for val, clr in zip(vals, COST_COLORS):
        ax4.bar(xi, val, bottom=bottom, width=0.28, color=clr, edgecolor=BG, linewidth=0.7)
        if val > 12:
            ax4.text(xi, bottom + val / 2, f"${val:.0f}",
                     ha="center", va="center", fontsize=8, color=WHITE, fontweight="bold")
        bottom += val
    ax4.text(xi, total + 10, f"${total:.2f}", ha="center", fontsize=11, color=col,
             fontweight="bold",
             bbox=dict(boxstyle="round,pad=0.3", facecolor=BG3, edgecolor=col, lw=2))
ax4.set_xticks(x_pos)
orig_mix_str = "  +  ".join(
    f"{int(r['count'])}x {r['charger_type']}" for _, r in orig_mix[orig_mix["count"] > 0].iterrows()
)
ax4.set_xticklabels([f"Original\n{orig_mix_str.replace('  +  ', '+')}",
                     f"Z2Z {n_events_new}-event\n" + charger_mix_str.replace("  +  ", "+")], fontsize=8)
ax4.set_xlim(0, 0.9)
ax4.set_ylim(0, max(total_orig, total_new) * 1.22)
ax4.set_ylabel("Daily Cost ($/day)", fontsize=10)
ax4.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
ax4.grid(axis="y", alpha=0.2)
ax4.set_title("Daily Cost Breakdown", fontsize=10, color=LGRAY, pad=5)
ax4.legend(handles=[mpatches.Patch(color=c, label=l) for c, l in zip(COST_COLORS, COST_LABELS)],
           fontsize=7.5, loc="upper left")

# ─── Panel 5: Detailed table ──────────────────────────────────────────────────
ax5 = fig.add_subplot(gs[3:, 1])
ax5.set_facecolor(BG2)
ax5.axis("off")
ax5.set_title("Vehicle Assignment  --  Start/End Charge Times, Avg Power, Energy", fontsize=9, color=LGRAY, pad=5)

cell_data = []
for _, evrow in events_df.iterrows():
    vid   = evrow["charging_event_id"]
    vnum  = vid.split("_v")[-1]
    info  = vid_info[vid]
    slots = veh_x_slots.get(vid, [])
    mac   = float(evrow.get("max_ac_charge_kw", 0))
    mdc   = float(evrow.get("max_dc_charge_kw", 0))
    if slots:
        sess_s    = min(s[0] for s in slots)
        sess_e    = max(s[1] for s in slots)
        total_kwh = sched_df[sched_df["charging_event_id"] == vid]["energy_delivered_kwh"].sum()
        avg_pw    = sched_df[sched_df["charging_event_id"] == vid]["power_kw"].mean()
        pct        = vid_primary_ctype.get(vid, "-")
        unit_id    = phys_assign.get(pct, {}).get(vid, 1)
        phys_label = f"{CTYPE_SHORT.get(pct, pct)}  #{unit_id}" if pct else "-"
    else:
        sess_s = sess_e = info["arr_h"]
        total_kwh = avg_pw = 0
        phys_label = "-"
    ext = "*" if info["dwell_eff"] > info["dwell_actual"] + 0.01 else " "
    pct_over = ((total_kwh / info["energy"] - 1.0) * 100) if info["energy"] > 0.01 and total_kwh > 0 else 0.0
    cell_data.append((
        f"v{vnum}",
        info["short"],
        phys_label,
        f"{mac:.0f}" if mac > 0 else "—",
        f"{mdc:.0f}" if mdc > 0 else "—",
        h_to_hhmm(sess_s),
        h_to_hhmm(sess_e),
        f"{avg_pw:.1f}",
        f"{info['energy']:.1f}",
        f"{total_kwh:.2f}",
        f"{pct_over:+.1f}%",
        ext,
    ))

col_labels = ["ID", "Model", "Charger", "Max\nAC kW", "Max\nDC kW", "1st\nCharge", "Last\nCharge", "Eff.\nkW", "Need\nkWh", "Del.\nkWh", "Over%", "Ext"]

tbl = ax5.table(cellText=cell_data, colLabels=col_labels, loc="center", cellLoc="center")
tbl.auto_set_font_size(False)
tbl.set_fontsize(6.5)
tbl.scale(1, 1.28)
for (row, col), cell in tbl.get_celld().items():
    cell.set_facecolor(BG3)
    cell.set_edgecolor(LGRAY)
    cell.set_text_props(color=WHITE)
    if row == 0:
        cell.set_facecolor("#1E3A5F")
        cell.set_text_props(color=CYAN, fontweight="bold")
    if row > 0 and col == 10:  # Over% column (shifted +1 for Max DC kW column)
        txt = cell.get_text().get_text()
        try:
            val = float(txt.strip("%+"))
            if val > 5.0:
                cell.set_facecolor("#4A0000")
                cell.set_text_props(color=RED)
            elif val > 0.1:
                cell.set_facecolor("#2A2A00")
                cell.set_text_props(color=AMBER)
        except ValueError:
            pass

ax5.text(0.5, -0.02,
         "* = dwell extended to 1-h minimum  |  Span = session window (first→last slot)  |  Over% = % above exact need",
         ha="center", fontsize=6, color=LGRAY, transform=ax5.transAxes)

# ─── Panel 6: Charger mix summary ────────────────────────────────────────────
ax6 = fig.add_subplot(gs[4, 0])
ax6.set_facecolor(BG2)
ax6.axis("off")
ax6.set_title("Selected Charger Mix Summary", fontsize=10, color=LGRAY, pad=5)

y0 = 0.96
for _, mrow in mix_df.iterrows():
    ct    = mrow["charger_type"]
    n     = int(mrow["count"])
    capex = mrow["total_daily_capex"]
    col   = CTYPE_COLOR.get(ct, WHITE)
    ax6.text(0.02, y0, f"{n}x  {ct.replace('_', ' ')}",
             fontsize=9, color=col, fontweight="bold", transform=ax6.transAxes)
    ax6.text(0.40, y0, f"{mrow['power_kw']:.0f} kW  —  ${capex:.2f}/day CapEx",
             fontsize=8.5, color=WHITE, transform=ax6.transAxes)
    y0 -= 0.16

ax6.text(0.02, y0 - 0.02, f"TOTAL CapEx:  ${capex_new:.2f}/day",
         fontsize=9, color=CYAN, fontweight="bold", transform=ax6.transAxes)
ax6.text(0.02, y0 - 0.14, f"TOTAL Cost:   ${total_new:.2f}/day  (incl. energy + demand + smoothing)",
         fontsize=9, color=WHITE, transform=ax6.transAxes)
n_served = evres_df["energy_delivered_kwh"].gt(0).sum() if "energy_delivered_kwh" in evres_df.columns else n_events_new
ax6.text(0.02, y0 - 0.26, f"Peak power:   {pmax_new:.1f} kW  |  Events served: {n_served}/{len(evres_df)}",
         fontsize=9, color=WHITE, transform=ax6.transAxes)

fig.text(0.5, 0.01, SOURCE, ha="center", fontsize=7.5, color=LGRAY, style="italic")

out_png = NEW_DIR / "milp_min1h_charger_assignment.png"
fig.savefig(out_png, dpi=150, bbox_inches="tight", facecolor=BG)
plt.close(fig)
print(f"Saved -> {out_png}")

# ── Console table ─────────────────────────────────────────────────────────────
print()
print("=" * 115)
print("CHARGER ASSIGNMENT DETAIL TABLE")
print("=" * 115)
hdr = (f"{'ID':>4}  {'Model':<12}  {'Charger':<22}  "
       f"{'MaxAC':>6}  {'MaxDC':>6}  "
       f"{'1st Charge':>10}  {'Last Charge':>11}  "
       f"{'Eff kW':>7}  {'Need kWh':>9}  {'Del. kWh':>9}  {'Over%':>7}  {'Ext':>3}")
print(hdr)
print("-" * 125)
for row in cell_data:
    vid_s, model_s, dcfc_s, mac_s, mdc_s, first_s, last_s, avgpw_s, need_s, deliv_s, over_s, ext_s = row
    print(f"{vid_s:>4}  {model_s:<12}  {dcfc_s:<22}  "
          f"{mac_s:>6}  {mdc_s:>6}  "
          f"{first_s:>10}  {last_s:>11}  "
          f"{avgpw_s:>7}  {need_s:>9}  {deliv_s:>9}  {over_s:>7}  {ext_s:>3}")
print("=" * 125)

print()
print(f"Charger mix: {charger_mix_str}")
print(f"Total daily cost: ${total_new:.2f}  (Original: ${total_orig:.2f})")
print(f"Peak power: {pmax_new:.1f} kW  (Original: {pmax_orig:.1f} kW)")
