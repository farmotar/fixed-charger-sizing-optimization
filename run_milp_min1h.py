"""
run_milp_min1h.py
-----------------
Runs the Northgate charger-sizing MILP on the min-1h dwell dataset
(16 events, 396.34 kWh) with smoothing lambda=1.5 and plots results.

Assumptions vs original run:
  - Visits with actual dwell < 1h are extended to 1h effective dwell
  - No visit is excluded (even 0-min GPS blips)
  - Smoothing penalty: lambda = 1.5 (MIQP, same as baseline)
"""

import sys, math
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import pandas as pd

# ── Patch config before any function uses the globals ─────────────────────────
import exact_northgate_charger_sizing_milp as milp

NEW_INPUT  = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\northgate_2025_06_30_min1h_events.csv")
NEW_OUTPUT = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\exact_milp_outputs_min1h")
NEW_OUTPUT.mkdir(parents=True, exist_ok=True)

milp.INPUT_PATH_PRIMARY  = NEW_INPUT
milp.OUTPUT_DIR          = NEW_OUTPUT
milp.LAMBDA_SMOOTH       = 1.5   # smoothing ON

# ── Run pipeline ──────────────────────────────────────────────────────────────
print("=" * 70)
print("Northgate MILP -- Min-1h Dwell Assumption  |  lambda=1.5 (smoothing ON)")
print("=" * 70)

raw_df      = milp.load_events_data()
events_df   = milp.clean_events_df(raw_df)
charger_specs = milp.build_charger_specs()
daily_capex   = milp.compute_daily_capex(charger_specs)
time_grid     = milp.build_time_grid(events_df)
P_eff         = milp.compute_effective_power(events_df, charger_specs)
feasible_keys, E, arrival_map, departure_map, available_times = milp.build_feasible_keys(
    events_df, time_grid, charger_specs, P_eff
)

try:
    import gurobipy
    sol = milp.solve_with_gurobi(
        events_df, time_grid, charger_specs, daily_capex,
        P_eff, feasible_keys, E, available_times,
        lambda_smooth=1.5,
    )
except ImportError:
    sol = milp.solve_with_pyomo_highs(
        events_df, time_grid, charger_specs, daily_capex,
        P_eff, feasible_keys, E, available_times,
        lambda_smooth=1.5,
    )

if sol.get("status") in ("infeasible", "no_solution"):
    print("MILP failed -- no solution.")
    sys.exit(1)

milp.validate_solution(sol, events_df, time_grid, charger_specs, P_eff, E, feasible_keys)
milp.export_solution(sol, events_df, time_grid, charger_specs, daily_capex, P_eff, E, feasible_keys)
milp.write_summary(sol, events_df, charger_specs, E, daily_capex, lambda_smooth=1.5)

# ── Extract key values ────────────────────────────────────────────────────────
P_vals   = sol["P_total_vals"]
n_steps  = len(time_grid)
dt       = milp.DT_HOURS
eta      = milp.ETA
non_first = list(range(1, n_steps))
hours    = [t.hour + t.minute / 60.0 for t in time_grid]

pmax     = sol["P_max_val"]
pkwin    = sol["P_peak_val"]
capex    = sol["daily_capex_cost"]
energy_c = sol["energy_cost"]
demand_c = sol["global_demand_cost"]
pkwin_c  = sol["peak_window_cost"]
smooth_c = sol["smoothing_cost"]
total_c  = sol["total_objective_cost"]
N_vals   = sol["N_vals"]

rmsd = math.sqrt(sum((P_vals[t] - P_vals[t-1])**2 for t in non_first) / max(1, len(non_first)))

charger_mix_str = "  +  ".join(
    f"{v}x {c.replace('_',' ')}" for c, v in N_vals.items() if v > 0
)

print(f"\nCharger mix : {charger_mix_str}")
print(f"Peak power  : {pmax:.2f} kW")
print(f"Total cost  : ${total_c:.4f}/day")
print(f"RMSD        : {rmsd:.4f} kW/step")

# ── Load original (12-event) results for comparison ──────────────────────────
ORIG_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\exact_milp_outputs")
orig_prof = pd.read_csv(ORIG_DIR / "exact_milp_site_power_profile.csv")
orig_cost = pd.read_csv(ORIG_DIR / "exact_milp_cost_breakdown.csv")
orig_mix  = pd.read_csv(ORIG_DIR / "exact_milp_selected_charger_mix.csv")
orig_evts = pd.read_csv(ORIG_DIR / "exact_milp_event_results.csv")

def get_cost(df, name):
    r = df[df["component"] == name]
    return float(r["value"].iloc[0]) if len(r) else 0.0

o_pmax   = get_cost(orig_cost, "P_max_kw")
o_capex  = sum(orig_mix["total_daily_capex"])
o_energy = get_cost(orig_cost, "energy_cost")
o_demand = get_cost(orig_cost, "global_demand_cost")
o_pkwin  = get_cost(orig_cost, "peak_window_demand_cost")
o_smooth = get_cost(orig_cost, "smoothing_cost")
o_total  = o_capex + o_energy + o_demand + o_pkwin + o_smooth
o_N      = {row["charger_type"]: int(row["count"]) for _, row in orig_mix.iterrows()}
o_mix_str = "  +  ".join(
    f"{v}x {c.replace('_',' ')}" for c, v in o_N.items() if v > 0
)
orig_p   = orig_prof["P_total_kw"].values
o_rmsd   = math.sqrt(sum((orig_p[i]-orig_p[i-1])**2 for i in range(1,len(orig_p))) / max(1,len(orig_p)-1))
orig_hrs = orig_prof["hour"].values

# ═══════════════════════════════════════════════════════════════════════════════
# PLOT
# ═══════════════════════════════════════════════════════════════════════════════

BG     = "#0A0E1A"
BG2    = "#12182B"
BG3    = "#1A2235"
DGRAY  = "#252D40"
LGRAY  = "#4A5470"
WHITE  = "#E8E8FF"
BLUE   = "#00BFFF"
GREEN  = "#39FF14"
AMBER  = "#FFB300"
RED    = "#FF4B4B"
PURPLE = "#9B59B6"
CYAN   = "#00E5FF"
GOLD   = "#FFD700"

plt.rcParams.update({
    "figure.facecolor": BG, "axes.facecolor": BG2,
    "axes.edgecolor": LGRAY, "axes.labelcolor": WHITE,
    "xtick.color": WHITE, "ytick.color": WHITE, "text.color": WHITE,
    "grid.color": LGRAY, "grid.alpha": 0.2, "font.family": "DejaVu Sans",
    "legend.facecolor": BG2, "legend.edgecolor": LGRAY,
})

SOURCE = "Source: Northgate MILP | Min-1h Dwell Assumption | SMUD Tariff | Gurobi | 2025"

fig = plt.figure(figsize=(22, 20))
fig.patch.set_facecolor(BG)
fig.suptitle(
    "Northgate MILP Optimization  --  Min-1h Dwell Assumption  |  June 30, 2025",
    fontsize=18, fontweight="bold", color=WHITE, y=0.98
)

gs = fig.add_gridspec(3, 2, hspace=0.38, wspace=0.28,
                      left=0.07, right=0.97, top=0.94, bottom=0.05)

# ── Panel 1: Power profiles side-by-side ─────────────────────────────────────
ax1 = fig.add_subplot(gs[0, :])
ax1.set_facecolor(BG2)

# Original (12 events)
for a, lw in [(0.06, 8), (0.12, 4), (0.3, 2)]:
    ax1.fill_between(orig_hrs, 0, orig_p, color=AMBER, alpha=a)
    ax1.plot(orig_hrs, orig_p, color=AMBER, lw=lw, alpha=a)
ax1.plot(orig_hrs, orig_p, color=AMBER, lw=2.5, label="Original: 12 events  (actual dwell)")

# Min-1h (16 events)
p_new = [P_vals[i] for i in range(n_steps)]
for a, lw in [(0.06, 8), (0.12, 4), (0.3, 2)]:
    ax1.fill_between(hours, 0, p_new, color=BLUE, alpha=a)
    ax1.plot(hours, p_new, color=BLUE, lw=lw, alpha=a)
ax1.plot(hours, p_new, color=BLUE, lw=2.5, label=f"Min-1h: 16 events  (extended dwell)")

# Peak lines
ax1.axhline(o_pmax, color=AMBER, lw=1.2, ls="--", alpha=0.7)
ax1.axhline(pmax,   color=BLUE,  lw=1.2, ls="--", alpha=0.7)

# Peak window shading
ax1.axvspan(17, 20, alpha=0.1, color=AMBER, zorder=0)
ax1.text(18.5, ax1.get_ylim()[1] if ax1.get_ylim()[1] > 0 else 50,
         "Peak Tariff\n17-20h", ha="center", fontsize=8, color=AMBER,
         bbox=dict(boxstyle="round,pad=0.3", facecolor=BG3, edgecolor=AMBER))

ax1.annotate(f"Original peak\n{o_pmax:.1f} kW",
             xy=(orig_hrs[np.argmax(orig_p)], o_pmax),
             xytext=(12, o_pmax + 15),
             fontsize=9, color=AMBER, fontweight="bold",
             arrowprops=dict(arrowstyle="->", color=AMBER, lw=1.5),
             bbox=dict(boxstyle="round,pad=0.3", facecolor=BG3, edgecolor=AMBER))

ax1.annotate(f"Min-1h peak\n{pmax:.1f} kW",
             xy=(hours[np.argmax(p_new)], pmax),
             xytext=(12, pmax - 30),
             fontsize=9, color=BLUE, fontweight="bold",
             arrowprops=dict(arrowstyle="->", color=BLUE, lw=1.5),
             bbox=dict(boxstyle="round,pad=0.3", facecolor=BG3, edgecolor=BLUE))

ax1.set_xlim(0, 28)
ax1.set_ylim(0, max(o_pmax, pmax) * 1.35)
ax1.set_xlabel("Time of Day (PDT)", fontsize=10)
ax1.set_ylabel("Site Power Demand (kW)", fontsize=11)
ax1.set_xticks(range(0, 29, 2))
ax1.set_xticklabels([f"{h:02d}:00" for h in range(0, 29, 2)], fontsize=8)
ax1.grid(alpha=0.2)
ax1.legend(fontsize=10, loc="upper right")
ax1.set_title(
    f"Power Profiles: Original ({len(orig_evts)} events, {sum(orig_evts['required_energy_kwh']):.1f} kWh)"
    f" vs Min-1h ({len(events_df)} events, {sum(E.values()):.1f} kWh)",
    fontsize=11, color=LGRAY, pad=6
)

# ── Panel 2: Gantt chart (min-1h events) ─────────────────────────────────────
ax2 = fig.add_subplot(gs[1, :])
ax2.set_facecolor(BG2)

events_df_sorted = events_df.sort_values("arrival_time").reset_index(drop=True)

TYPE_COLOR = {"HD": RED, "MD": AMBER, "LD": GREEN}
VEHICLE_TYPE = {
    "Freightliner eCascadia": "HD", "Freightliner eM2": "HD",
    "Ford F-150 Lightning": "LD", "Tesla Model 3": "LD",
    "Rivian R1T": "LD", "Rivian R1S": "LD", "Chevrolet Bolt EV": "LD",
    "GMC Hummer EV": "MD", "Chevrolet Silverado EV": "MD",
    "BYD 6F Cab-Forward Truck": "MD",
}

tz_la = "America/Los_Angeles"

for i, row in events_df_sorted.iterrows():
    arr_t   = pd.to_datetime(row["arrival_time"], utc=True).tz_convert(tz_la)
    dep_t   = pd.to_datetime(row["departure_time"], utc=True).tz_convert(tz_la)
    dep_act = dep_t  # may be extended

    # Compute actual dwell (stored in CSV)
    arr_h = arr_t.hour + arr_t.minute / 60 + (arr_t.date().toordinal() - pd.Timestamp("2025-06-30").toordinal()) * 24
    dep_h = dep_t.hour + dep_t.minute / 60 + (dep_t.date().toordinal() - pd.Timestamp("2025-06-30").toordinal()) * 24

    dwell_actual = row.get("dwell_hours_actual", row["dwell_hours"])
    dwell_eff    = row["dwell_hours"]
    extended     = dwell_eff > dwell_actual + 0.01

    model = str(row.get("ev_equivalent_model", ""))
    vtype = VEHICLE_TYPE.get(model, "LD")
    col   = TYPE_COLOR[vtype]
    energy = row["energy_needed_kwh_for_visit"]

    # Actual dwell bar
    ax2.barh(i, dwell_actual, left=arr_h, height=0.45,
             color=col, alpha=0.85, edgecolor=WHITE, linewidth=0.7, zorder=2)

    # Extension bar (lighter, hatched)
    if extended:
        ax2.barh(i, dwell_eff - dwell_actual, left=arr_h + dwell_actual,
                 height=0.45, color=col, alpha=0.35,
                 edgecolor=CYAN, linewidth=1.2, linestyle="--",
                 hatch="//", zorder=2)
        ax2.text(arr_h + dwell_actual + (dwell_eff - dwell_actual) / 2,
                 i, "ext.", ha="center", va="center", fontsize=5.5, color=CYAN)

    # Label
    bar_mid = arr_h + dwell_eff / 2
    if dwell_eff > 1.5:
        ax2.text(bar_mid, i, f"{model[:18]}  {energy:.1f} kWh",
                 va="center", ha="center", fontsize=7, color=WHITE, fontweight="bold", zorder=3)
    else:
        ax2.text(arr_h + dwell_eff + 0.15, i, f"{energy:.1f} kWh",
                 va="center", fontsize=6.5, color=col)

    vid = row["charging_event_id"].split("_v")[-1]
    ax2.text(arr_h - 0.15, i, f"v{vid}", va="center", ha="right",
             fontsize=7.5, color=col, fontweight="bold")

ax2.axvspan(17, 20, alpha=0.12, color=AMBER, zorder=0)
ax2.text(18.5, len(events_df_sorted) - 0.3, "Peak\n17-20h",
         ha="center", fontsize=7.5, color=AMBER)
ax2.axvline(24, color=WHITE, lw=0.8, ls=":", alpha=0.5)
ax2.text(24.1, -0.8, "Midnight", fontsize=7, color=WHITE, alpha=0.5)

ax2.set_xlim(0, 30)
ax2.set_ylim(-1, len(events_df_sorted))
ax2.set_yticks(range(len(events_df_sorted)))
ax2.set_yticklabels([])
ax2.set_xticks(range(0, 31, 2))
ax2.set_xticklabels([f"{h%24:02d}:00" for h in range(0, 31, 2)], fontsize=8)
ax2.set_xlabel("Time of Day (PDT)  |  Hours > 24 = Next Day (July 1)", fontsize=10)
ax2.grid(axis="x", alpha=0.2)
ax2.set_title("Vehicle Dwell Windows -- Min-1h Assumption  "
              "(solid = actual dwell, hatched = 1h extension)",
              fontsize=11, color=LGRAY, pad=6)

handles = [
    mpatches.Patch(color=RED,    label="HD -- Heavy Duty"),
    mpatches.Patch(color=AMBER,  label="MD -- Medium Duty"),
    mpatches.Patch(color=GREEN,  label="LD -- Light Duty"),
    mpatches.Patch(facecolor="none", edgecolor=CYAN, hatch="//",
                   label="Dwell extended to 1h"),
]
ax2.legend(handles=handles, fontsize=8, loc="upper right")

# ── Panel 3: Cost breakdown comparison bar chart ──────────────────────────────
ax3 = fig.add_subplot(gs[2, 0])
ax3.set_facecolor(BG2)

COST_KEYS   = ["capex", "energy", "demand", "pkwin", "smooth"]
COST_LABELS = ["Charger CapEx", "Energy Cost", "Global Demand", "Peak-Win Demand", "Smoothing"]
COST_COLORS = ["#1E3A5F", GREEN, RED, AMBER, PURPLE]

orig_vals = [o_capex, o_energy, o_demand, o_pkwin, o_smooth]
new_vals  = [capex,   energy_c, demand_c, pkwin_c, smooth_c]
orig_total = sum(orig_vals)
new_total  = sum(new_vals)

x_pos = [0.25, 0.75]
bar_w = 0.28

for xi, (vals, total, col, lbl) in zip(
    x_pos,
    [(orig_vals, orig_total, AMBER, f"Original\n12 events"),
     (new_vals,  new_total,  BLUE,  f"Min-1h\n16 events")]
):
    bottom = 0
    for val, clr, key in zip(vals, COST_COLORS, COST_KEYS):
        ax3.bar(xi, val, bottom=bottom, width=bar_w, color=clr,
                edgecolor=BG, linewidth=0.7)
        if val > 8:
            ax3.text(xi, bottom + val / 2, f"${val:.0f}",
                     ha="center", va="center", fontsize=8, color=WHITE, fontweight="bold")
        bottom += val
    ax3.text(xi, total + 12, f"${total:.2f}", ha="center",
             fontsize=12, color=col, fontweight="bold",
             bbox=dict(boxstyle="round,pad=0.3", facecolor=BG3, edgecolor=col, lw=2))

ax3.set_xticks(x_pos)
ax3.set_xticklabels([f"Original\n12 events\n{o_mix_str}",
                     f"Min-1h\n16 events\n{charger_mix_str}"], fontsize=9)
ax3.set_xlim(0, 1.0)
ax3.set_ylim(0, max(orig_total, new_total) * 1.22)
ax3.set_ylabel("Daily Cost ($/day)", fontsize=10)
ax3.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
ax3.grid(axis="y", alpha=0.2)
ax3.set_title("Daily Cost Breakdown\nOriginal vs Min-1h Dwell", fontsize=11, color=LGRAY, pad=6)

handles = [mpatches.Patch(color=c, label=l) for c, l in zip(COST_COLORS, COST_LABELS)]
ax3.legend(handles=handles, fontsize=8, loc="upper left")

# ── Panel 4: Key metrics comparison table ────────────────────────────────────
ax4 = fig.add_subplot(gs[2, 1])
ax4.set_facecolor(BG2)
ax4.axis("off")
ax4.set_title("Side-by-Side Metrics", fontsize=11, color=LGRAY, pad=6)

metrics = [
    ("Events",            f"{len(orig_evts)}",               f"{len(events_df)}"),
    ("Total energy (kWh)",f"{sum(orig_evts['required_energy_kwh']):.1f}",
                                                              f"{sum(E.values()):.1f}"),
    ("Charger mix",       o_mix_str.replace("x ", "x"),      charger_mix_str.replace("x ", "x")),
    ("Peak demand (kW)",  f"{o_pmax:.2f}",                   f"{pmax:.2f}"),
    ("RMSD (kW/step)",    f"{o_rmsd:.2f}",                   f"{rmsd:.2f}"),
    ("Charger CapEx",     f"${o_capex:.2f}",                 f"${capex:.2f}"),
    ("Energy cost",       f"${o_energy:.2f}",                f"${energy_c:.2f}"),
    ("Global demand",     f"${o_demand:.2f}",                f"${demand_c:.2f}"),
    ("Peak-win demand",   f"${o_pkwin:.2f}",                 f"${pkwin_c:.2f}"),
    ("Smoothing",         f"${o_smooth:.2f}",                f"${smooth_c:.2f}"),
    ("TOTAL COST",        f"${orig_total:.2f}",              f"${new_total:.2f}"),
]

col_labels = ["Metric", "Original\n(12 ev.)", "Min-1h\n(16 ev.)"]
row_colors_list = []
cell_data = []
for label, v_orig, v_new in metrics:
    cell_data.append([label, v_orig, v_new])
    row_colors_list.append([BG2, BG3, BG3])

# Highlight total row
row_colors_list[-1] = ["#001A00", "#001A00", "#001A00"]

tbl = ax4.table(
    cellText=cell_data,
    colLabels=col_labels,
    loc="center",
    cellLoc="center",
)
tbl.auto_set_font_size(False)
tbl.set_fontsize(9)
tbl.scale(1, 1.55)

for (row, col), cell in tbl.get_celld().items():
    cell.set_facecolor(BG3)
    cell.set_edgecolor(LGRAY)
    cell.set_text_props(color=WHITE)
    if row == 0:
        cell.set_facecolor("#1E3A5F")
        cell.set_text_props(color=CYAN, fontweight="bold")
    if row == len(metrics):  # total row
        cell.set_facecolor("#001A00")
        cell.set_text_props(color=GREEN, fontweight="bold")
    if col == 2 and row > 0:
        cell.set_text_props(color=BLUE)
    if col == 1 and row > 0:
        cell.set_text_props(color=AMBER)

fig.text(0.5, 0.015, SOURCE, ha="center", fontsize=7, color=LGRAY, style="italic")

out_png = NEW_OUTPUT / "milp_min1h_results.png"
fig.savefig(out_png, dpi=150, bbox_inches="tight", facecolor=BG)
plt.close(fig)
print(f"\nPlot saved -> {out_png}")

# ── Console comparison table ──────────────────────────────────────────────────
SEP = "=" * 68
sep = "-" * 68
print()
print(SEP)
print("  NORTHGATE MILP: Original (12 events) vs Min-1h Dwell (16 events)")
print("  Both runs: lambda=1.5 (smoothing ON)")
print(SEP)
print(f"  {'Metric':<38}  {'Original':>11}  {'Min-1h':>11}")
print(sep)
rows_out = [
    ("Events",              len(orig_evts),       len(events_df)),
    ("Total energy (kWh)",  sum(orig_evts['required_energy_kwh']), sum(E.values())),
    ("Charger mix",         o_mix_str,            charger_mix_str),
    ("Peak demand (kW)",    o_pmax,               pmax),
    ("RMSD (kW/step)",      o_rmsd,               rmsd),
    ("",                    "",                   ""),
    ("Charger CapEx ($/day)", o_capex,            capex),
    ("Energy cost ($/day)", o_energy,             energy_c),
    ("Global demand ($/day)", o_demand,           demand_c),
    ("Peak-win demand ($/day)", o_pkwin,          pkwin_c),
    ("Smoothing ($/day)",   o_smooth,             smooth_c),
    ("TOTAL COST ($/day)",  orig_total,           new_total),
    ("",                    "",                   ""),
    ("Solve time (s)",      0.4,                  sol["solve_time"]),
    ("MIP gap",             0.0,                  sol["mip_gap"]),
]
for row in rows_out:
    lbl, v_o, v_n = row
    if lbl == "":
        print()
        continue
    if isinstance(v_o, float):
        print(f"  {lbl:<38}  {v_o:>11.2f}  {v_n:>11.2f}")
    else:
        print(f"  {lbl:<38}  {str(v_o):>11}  {str(v_n):>11}")
print(SEP)
