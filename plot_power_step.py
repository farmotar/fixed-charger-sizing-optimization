"""
Stacked bar power profile — one solid bar per vehicle per time slot.
No floating lines. No transparency gaps.
"""
from pathlib import Path
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

DIRS = {
    "smooth":    Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\exact_milp_outputs_min1h"),
    "no_smooth": Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\exact_milp_outputs_min1h_exact"),
}
TITLES = {
    "smooth":    "With smoothing  (λ=1.5)  —  2×L2 + 2×DC50 + DC150 + DC350  |  $2,218/day",
    "no_smooth": "No smoothing / exact energy  —  1×L2 + 1×DC50 + DC150 + DC350  |  $1,940/day",
}

TZ_LA = "America/Los_Angeles"
REF   = pd.Timestamp("2025-06-30", tz=TZ_LA)

def to_h(ts):
    return (pd.to_datetime(ts, utc=True).tz_convert(TZ_LA) - REF).total_seconds() / 3600.0

BG, BG2   = "#0A0E1A", "#12182B"
LGRAY     = "#4A5470"
WHITE     = "#E8E8FF"
AMBER     = "#FFB300"
CYAN      = "#00E5FF"

# One distinct colour per vehicle (same palette used across both panels)
VEH_PALETTE = [
    "#FF6B6B","#FFA500","#FFD700","#7FFF00","#00CED1",
    "#1E90FF","#DA70D6","#FF1493","#00FA9A","#FF4500",
    "#9370DB","#20B2AA","#F08080","#90EE90","#87CEEB","#DDA0DD",
]

plt.rcParams.update({
    "figure.facecolor": BG, "axes.facecolor": BG2,
    "text.color": WHITE, "axes.labelcolor": WHITE,
    "xtick.color": WHITE, "ytick.color": WHITE,
    "axes.edgecolor": LGRAY,
})

DT = 5 / 60          # slot width in hours
X_MAX = 34

fig, axes = plt.subplots(2, 1, figsize=(24, 11), sharex=True)
fig.patch.set_facecolor(BG)
fig.suptitle(
    "Northgate MILP  —  Site Power Demand  |  2025-06-30 (PDT)\n"
    "Each solid bar = one vehicle charging for one 5-min slot  |  Colour = vehicle",
    fontsize=12, fontweight="bold", color=WHITE, y=0.99,
)

# Collect all vehicle IDs across both runs so colours are consistent
all_vids = set()
for key in DIRS:
    s = pd.read_csv(DIRS[key] / "exact_milp_charging_schedule.csv")
    all_vids.update(s["charging_event_id"].unique())
all_vids = sorted(all_vids)
VEH_COLOR = {v: VEH_PALETTE[i % len(VEH_PALETTE)] for i, v in enumerate(all_vids)}

for ax, key in zip(axes, DIRS):
    sched = pd.read_csv(DIRS[key] / "exact_milp_charging_schedule.csv")
    sched["t_h"] = sched["time_step_start"].apply(to_h)

    ax.set_facecolor(BG2)

    # --- Stacked bars: for each time slot, stack vehicles bottom-up ---
    # Group by time slot, then for each slot draw each vehicle as a rectangle
    for t_h, grp in sched[sched["t_h"] < X_MAX].groupby("t_h"):
        bottom = 0.0
        for _, row in grp.sort_values("power_kw").iterrows():
            vid = row["charging_event_id"]
            pw  = row["power_kw"]
            col = VEH_COLOR[vid]
            # Solid filled rectangle: left=t_h, width=DT, bottom=bottom, height=pw
            rect = plt.Rectangle(
                (t_h, bottom), DT, pw,
                facecolor=col, edgecolor=BG2, linewidth=0.3
            )
            ax.add_patch(rect)
            bottom += pw

    # --- Session labels: one text per vehicle at the midpoint of its session ---
    for vid, grp in sched[sched["t_h"] < X_MAX].groupby("charging_event_id"):
        s_h   = grp["t_h"].min()
        e_h   = grp["t_h"].max() + DT
        pw    = grp["power_kw"].iloc[0]          # constant power model — same for all slots
        vnum  = vid.split("_v")[-1]
        span  = e_h - s_h
        mid   = s_h + span / 2

        if span < 0.15:                          # too narrow — just a tick
            ax.axvline(s_h, color=VEH_COLOR[vid], lw=0.7, alpha=0.6)
            continue

        # Find the vertical centre of this vehicle's block at the midpoint slot
        # (approximate: assume it is at the bottom of the stack = 0 for single-vehicle slots)
        ax.text(
            mid, pw / 2,
            f"v{vnum}\n{pw:.0f} kW",
            ha="center", va="center",
            fontsize=5.5 if span < 0.4 else 6.5,
            color=WHITE, fontweight="bold",
            clip_on=True,
        )

    # --- Decoration ---
    ax.axvspan(17, 20, alpha=0.10, color=AMBER, zorder=0)
    ax.axvline(24, color=WHITE, lw=0.7, ls=":", alpha=0.35)
    ax.text(24.06, ax.get_ylim()[1] if ax.get_ylim()[1] > 0 else 200,
            "Midnight\nJul 1", fontsize=7, color=WHITE, alpha=0.5, va="top")

    # Re-scale y after patches are added
    p_max = sched["power_kw"].sum() if len(sched) else 400   # rough upper bound
    # Better: use the profile CSV for exact P_total max
    prof = pd.read_csv(DIRS[key] / "exact_milp_site_power_profile.csv")
    p_max = float(prof["P_total_kw"].max())
    ax.set_ylim(0, p_max * 1.25)
    ax.text(18.5, p_max * 1.10, "Peak 17–20h", ha="center", fontsize=8,
            color=AMBER,
            bbox=dict(boxstyle="round,pad=0.2", facecolor=BG2, edgecolor=AMBER))

    ax.set_xlim(0, X_MAX)
    ax.set_ylabel("Site Power (kW)", fontsize=9)
    ax.set_title(TITLES[key], fontsize=9, color=LGRAY, pad=4)
    ax.grid(axis="y", alpha=0.12, color=LGRAY)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    # Legend: vehicle colours
    handles = [
        mpatches.Patch(color=VEH_COLOR[v], label="v" + v.split("_v")[-1])
        for v in all_vids
    ]
    ax.legend(handles=handles, fontsize=6.5, loc="upper left",
              facecolor=BG2, edgecolor=LGRAY, framealpha=0.85,
              ncol=4, title="Vehicle", title_fontsize=7)

# x-axis ticks
ticks = list(range(0, X_MAX + 1, 2))
axes[1].set_xticks(ticks)
axes[1].set_xticklabels([f"{int(h) % 24:02d}:00" for h in ticks], fontsize=8)
axes[1].set_xlabel("Time of Day (PDT)  |  Hours > 24 = July 1", fontsize=9)

plt.tight_layout(rect=[0, 0, 1, 0.96])
out = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\exact_milp_outputs_min1h_exact\power_step_comparison.png")
fig.savefig(out, dpi=150, bbox_inches="tight", facecolor=BG)
plt.close()
print(f"Saved -> {out}")
