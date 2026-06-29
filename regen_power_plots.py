"""
regen_power_plots.py
Re-generates exact_milp_power_profile.png and exact_milp_power_profile_with_events.png
for any MILP output directory, using stacked solid vehicle bars (no floating lines).
Run once per output directory.
"""
from pathlib import Path
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

DIRS_TO_REGEN = [
    Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\exact_milp_outputs_min1h"),
    Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\exact_milp_outputs_min1h_exact"),
    Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\exact_milp_outputs"),
]

EVENTS_CSV = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\northgate_2025_06_30_min1h_events.csv")
EVENTS_ORIG = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\northgate_2025_06_30_serviceable_charging_events.csv")

TZ_LA = "America/Los_Angeles"
REF   = pd.Timestamp("2025-06-30", tz=TZ_LA)

def to_h(ts):
    return (pd.to_datetime(ts, utc=True).tz_convert(TZ_LA) - REF).total_seconds() / 3600.0

VEH_PALETTE = [
    "#FF6B6B","#FFA500","#FFD700","#7FFF00","#00CED1",
    "#1E90FF","#DA70D6","#FF1493","#00FA9A","#FF4500",
    "#9370DB","#20B2AA","#F08080","#90EE90","#87CEEB","#DDA0DD",
]

BG, BG2   = "#0A0E1A", "#12182B"
LGRAY     = "#4A5470"
WHITE     = "#E8E8FF"
AMBER     = "#FFB300"

plt.rcParams.update({
    "figure.facecolor": BG, "axes.facecolor": BG2,
    "text.color": WHITE, "axes.labelcolor": WHITE,
    "xtick.color": WHITE, "ytick.color": WHITE,
    "axes.edgecolor": LGRAY,
    "grid.color": LGRAY, "grid.alpha": 0.15,
    "legend.facecolor": BG2, "legend.edgecolor": LGRAY,
})

DT = 5 / 60  # 5-min slot width in hours


def regen(out_dir: Path):
    sched_path = out_dir / "exact_milp_charging_schedule.csv"
    prof_path  = out_dir / "exact_milp_site_power_profile.csv"
    mix_path   = out_dir / "exact_milp_selected_charger_mix.csv"
    cost_path  = out_dir / "exact_milp_cost_breakdown.csv"

    if not sched_path.exists():
        print(f"  Skipping {out_dir.name} — no schedule CSV")
        return

    sched = pd.read_csv(sched_path)
    prof  = pd.read_csv(prof_path)
    mix   = pd.read_csv(mix_path)
    cost  = pd.read_csv(cost_path)

    sched["t_h"] = sched["time_step_start"].apply(to_h)

    h_vals = prof["hour"].values
    P      = prof["P_total_kw"].values
    p_max  = float(P.max()) if P.max() > 0 else 50
    x_max  = min(float(h_vals.max()) + 1, 34)

    # vehicle colour map
    vids = sorted(sched["charging_event_id"].unique())
    veh_color = {v: VEH_PALETTE[i % len(VEH_PALETTE)] for i, v in enumerate(vids)}

    mix_str = "  +  ".join(
        f"{int(r['count'])}×{r['charger_type']}" for _, r in mix.iterrows()
    )
    peak_win = cost[cost["component"] == "P_peak_win_kw"]
    p_peak = float(peak_win["value"].iloc[0]) if len(peak_win) else 0.0

    def make_axes_clean(ax):
        ax.set_facecolor(BG2)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_color(LGRAY)
        ax.spines["bottom"].set_color(LGRAY)
        ax.grid(axis="y", alpha=0.15)

    def draw_stacked_bars(ax, sub=None):
        df = sub if sub is not None else sched
        for t_h, grp in df[df["t_h"] < x_max].groupby("t_h"):
            bottom = 0.0
            for _, row in grp.sort_values("power_kw").iterrows():
                vid  = row["charging_event_id"]
                pw   = row["power_kw"]
                col  = veh_color.get(vid, WHITE)
                ax.add_patch(mpatches.Rectangle(
                    (t_h, bottom), DT, pw,
                    facecolor=col, edgecolor=BG2, linewidth=0.3, zorder=3
                ))
                bottom += pw

    def add_peak_shade(ax, ymax):
        ax.axvspan(17, 20, alpha=0.12, color=AMBER, zorder=0)
        ax.text(18.5, ymax * 0.96, "Peak\n17–20h",
                ha="center", fontsize=8, color=AMBER,
                bbox=dict(boxstyle="round,pad=0.2", facecolor=BG2, edgecolor=AMBER))

    def set_xticks(ax):
        ticks = list(range(0, int(x_max) + 1, 2))
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{int(h)%24:02d}:00" for h in ticks], fontsize=8)
        ax.set_xlim(0, x_max)
        ax.axvline(24, color=WHITE, lw=0.7, ls=":", alpha=0.3)

    # ── Figure 1: power profile only ─────────────────────────────────────────
    fig1, ax = plt.subplots(figsize=(18, 6))
    fig1.patch.set_facecolor(BG)
    make_axes_clean(ax)

    draw_stacked_bars(ax)
    add_peak_shade(ax, p_max * 1.2)
    ax.axhline(p_max, color="#c0392b", lw=1.2, ls="--", alpha=0.8,
               label=f"P_max = {p_max:.0f} kW")
    if p_peak > 0:
        ax.axhline(p_peak, color=AMBER, lw=1.0, ls=":", alpha=0.7,
                   label=f"Peak-win = {p_peak:.0f} kW")

    ax.set_ylim(0, p_max * 1.3)
    set_xticks(ax)
    ax.set_xlabel("Time of Day (PDT)  |  Hours > 24 = July 1", fontsize=10)
    ax.set_ylabel("Site Power (kW)", fontsize=10)
    ax.set_title(
        f"Northgate MILP  —  Site Power Demand  |  {out_dir.name}\n"
        f"Mix: {mix_str}  |  Each bar = one vehicle's 5-min charging slot",
        fontsize=11, color=WHITE, pad=8
    )
    handles = [mpatches.Patch(color=veh_color[v], label="v"+v.split("_v")[-1]) for v in vids]
    ax.legend(handles=handles, fontsize=7, ncol=4, loc="upper left",
              title="Vehicle colours", title_fontsize=7)

    out1 = out_dir / "exact_milp_power_profile.png"
    fig1.tight_layout()
    fig1.savefig(out1, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig1)
    print(f"  Saved: {out1}")

    # ── Figure 2: power profile + active sessions ─────────────────────────────
    fig2, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(18, 10), sharex=True,
        gridspec_kw={"height_ratios": [2.5, 1]}
    )
    fig2.patch.set_facecolor(BG)
    make_axes_clean(ax_top)
    make_axes_clean(ax_bot)

    draw_stacked_bars(ax_top)
    add_peak_shade(ax_top, p_max * 1.2)
    ax_top.axhline(p_max, color="#c0392b", lw=1.2, ls="--", alpha=0.8,
                   label=f"P_max = {p_max:.0f} kW")
    ax_top.set_ylim(0, p_max * 1.3)
    ax_top.set_ylabel("Site Power (kW)", fontsize=10)
    ax_top.set_title(
        f"Northgate MILP  —  {out_dir.name}  |  Mix: {mix_str}",
        fontsize=11, color=WHITE, pad=6
    )
    handles = [mpatches.Patch(color=veh_color[v], label="v"+v.split("_v")[-1]) for v in vids]
    ax_top.legend(handles=handles, fontsize=7, ncol=4, loc="upper left",
                  title="Vehicle colours", title_fontsize=7)

    # Bottom: active session count per slot
    active = sched[sched["t_h"] < x_max].groupby("t_h")["charging_event_id"].count()
    ax_bot.bar(active.index, active.values, width=DT * 0.92,
               color="#2ecc71", edgecolor=BG2, linewidth=0.2, align="edge",
               label="Active charging sessions")
    ax_bot.set_ylabel("Active sessions", fontsize=10)
    ax_bot.set_xlabel("Time of Day (PDT)  |  Hours > 24 = July 1", fontsize=10)
    ax_bot.axvspan(17, 20, alpha=0.10, color=AMBER, zorder=0)
    ax_bot.legend(fontsize=9)
    ax_bot.set_ylim(0, int(active.max()) + 1.5 if len(active) else 5)

    set_xticks(ax_top)
    set_xticks(ax_bot)

    out2 = out_dir / "exact_milp_power_profile_with_events.png"
    fig2.tight_layout()
    fig2.savefig(out2, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig2)
    print(f"  Saved: {out2}")


for d in DIRS_TO_REGEN:
    print(f"\nRegenerating: {d.name}")
    regen(d)

print("\nAll done.")
