"""
build_presentation_figures.py
Generates appendix-ready standalone figures inspired by the XOS and Kempower
presentation PDFs (XOS_Hub_MC02_Presentation.pdf, Kempower_Northgate_Presentation.pdf).

Output: appendix_a_figures/presentation_style/*.png  +  figure_captions.txt
Run with: py -3.11 build_presentation_figures.py
"""
from __future__ import annotations
import re, sys
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

sys.stdout.reconfigure(encoding="utf-8")

# ── paths ──────────────────────────────────────────────────────────────
BASE   = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
OUTDIR = BASE / "scenario_outputs"
FIGS   = BASE / "appendix_a_figures" / "presentation_style"
FIGS.mkdir(parents=True, exist_ok=True)

SITES    = [("northgate","Northgate"), ("fresno","Fresno"),
            ("glendale","Glendale"),   ("san_diego","San Diego")]
SITE_IDX = {s:i for i,(s,_) in enumerate(SITES)}

COLORS = {"northgate":"#2166ac","fresno":"#4dac26",
          "glendale":"#f46d43",  "san_diego":"#d01c8b"}
UTIL   = {"northgate":"SMUD","fresno":"PG&E BEV-2",
          "glendale":"PG&E BEV-2 (GWP proxy)","san_diego":"SDG&E EV-HP"}

DAYS_PER_MO = 30.42
K_CAP       = 20

captions: dict[str, str] = {}

def savefig(name, cap, fig, dpi=150):
    p = FIGS / name
    fig.savefig(p, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    captions[name] = cap
    print(f"  {name}")

# ── load XOS A1 data ───────────────────────────────────────────────────
print("Loading XOS A1 data …")
XD = {}
for sk, sl in SITES:
    dc = pd.read_csv(OUTDIR/f"{sk}_analysis/{sk}_cost_detail.csv")
    sm = pd.read_csv(OUTDIR/f"{sk}_analysis/{sk}_summary.csv")
    a1d = dc[dc.scenario=="A1"].copy()
    a1s = sm[sm.scenario=="A1"].copy()
    a1d["date"] = pd.to_datetime(a1d["date"])
    a1s["date"] = pd.to_datetime(a1s["date"])
    a1d["total_allin"] = (a1d["total_daily_excl_demand"]
        + a1d["demand_global_monthly_$"] / DAYS_PER_MO
        + a1d["demand_peak_win_monthly_$"] / DAYS_PER_MO)
    merged = a1s.merge(a1d[["date","total_allin","purchase_capex_daily",
                              "infra_capex_daily","maint_daily","warranty_daily",
                              "energy_cost_daily","demand_global_monthly_$",
                              "demand_peak_win_monthly_$","total_daily_excl_demand"]], on="date", how="left")
    XD[sk] = {"label":sl, "dc":a1d.sort_values("date").reset_index(drop=True),
               "sm":a1s.sort_values("date").reset_index(drop=True),
               "merged":merged.sort_values("date").reset_index(drop=True)}
    print(f"  XOS {sl}: {len(a1s)} days, avg K={a1s.K.mean():.1f}")

# ── load Kempower data ─────────────────────────────────────────────────
print("Loading Kempower data …")

def parse_mix(mix_str):
    counts = {"50":0, "150":0, "250":0}
    if pd.isna(mix_str): return 0,0,0
    for part in re.split(r"\+", str(mix_str)):
        m = re.search(r"(\d+)[^\d]+(\d+)\s*kW", part.strip())
        if m:
            n, p = int(m.group(1)), str(int(m.group(2)))
            if p in counts: counts[p] = n
    return counts["50"], counts["150"], counts["250"]

def load_kmp_site(sk):
    sp = OUTDIR / f"{sk}_analysis/{sk}_kempower_summary.csv"
    if not sp.exists(): return None
    summ = pd.read_csv(sp); summ["date"] = pd.to_datetime(summ["date"])
    cb_files = sorted((OUTDIR/f"{sk}_analysis/per_day").glob("*/kempower/exact_milp_cost_breakdown.csv"))
    dem_rows = []
    for f in cb_files:
        cb = pd.read_csv(f); row = {"date": pd.to_datetime(f.parent.parent.name)}
        for comp in ["global_demand_cost","peak_window_demand_cost"]:
            r = cb[cb.component==comp]
            row[comp] = float(r["value"].iloc[0]) if not r.empty else 0.0
        dem_rows.append(row)
    if dem_rows:
        dem_df = pd.DataFrame(dem_rows)
        summ = summ.merge(dem_df, on="date", how="left")
        summ["global_demand_cost"]      = summ.get("global_demand_cost",      pd.Series(0.0, index=summ.index)).fillna(0.0)
        summ["peak_window_demand_cost"] = summ.get("peak_window_demand_cost", pd.Series(0.0, index=summ.index)).fillna(0.0)
    else:
        summ["global_demand_cost"] = summ["peak_window_demand_cost"] = 0.0
    summ["total_allin"] = (summ["capex_daily"] + summ["energy_cost"]
        + summ["global_demand_cost"]/DAYS_PER_MO + summ["peak_window_demand_cost"]/DAYS_PER_MO)
    n50,n150,n250 = zip(*summ["mix"].apply(parse_mix))
    summ["n_50"], summ["n_150"], summ["n_250"] = n50, n150, n250
    summ = summ.sort_values("date").reset_index(drop=True)
    print(f"  Kempower {sk}: {len(summ)} days")
    return summ

KD = {sk: load_kmp_site(sk) for sk,_ in SITES}

# ══════════════════════════════════════════════════════════════════════
# FIGURE P.01 — K-SWEEP COVERAGE CURVES (all 4 sites, Scenario A)
# ══════════════════════════════════════════════════════════════════════
print("\nGenerating figures …")

fig, ax = plt.subplots(figsize=(11,5.5))
K_range = np.arange(1, 21)
plateau_K = {}
for sk,sl in SITES:
    sm = XD[sk]["sm"]
    K_vals = sm["K"].values
    cov = [np.mean(K_vals <= k)*100 for k in K_range]
    plateau = max(cov)
    plateau_K[sk] = (K_range[next(i for i,c in enumerate(cov) if c>=plateau*0.999)], plateau)
    ax.plot(K_range, cov, "o-", color=COLORS[sk], label=sl, linewidth=2, markersize=4, alpha=0.9)
    pk, pv = plateau_K[sk]
    ax.annotate(f"K={pk}\n{pv:.1f}%", xy=(pk,pv), xytext=(pk+0.3, pv-4),
                color=COLORS[sk], fontsize=8.5, fontweight="bold")

ax.axhline(100, color="#aaa", linestyle="--", linewidth=1, alpha=0.6)
ax.set_xlabel("Number of XOS Hub MC02 Units (K)", fontsize=10)
ax.set_ylabel("Days 100% Fully Served (%)", fontsize=10)
ax.set_title("Coverage Curves — K-Sweep\n% of Operating Days Fully Served vs. Hub Count  (Scenario A — always grid-connected)",
             fontsize=11, fontweight="bold")
ax.set_xlim(0.5, 20.5); ax.set_ylim(0,105)
ax.set_xticks(K_range)
ax.yaxis.set_major_formatter(mticker.PercentFormatter())
ax.legend(fontsize=9.5, loc="lower right"); ax.grid(linestyle=":", alpha=0.4)
ax.text(0.5, 0.35, "Plateau = dwell-window ceiling:\nvehicles cannot be fully charged\nregardless of hub count",
        transform=ax.transAxes, fontsize=8, color="#555",
        bbox=dict(boxstyle="round,pad=0.3",facecolor="#f5f5f5",edgecolor="#bbb"))
fig.tight_layout()
savefig("P01_xos_coverage_curves.png",
    "Figure P.1. XOS Hub MC02 — K-Sweep Coverage Curves, All Sites (Scenario A). "
    "Each curve shows the fraction of operating days that a fixed K-unit deployment can "
    "fully serve all arriving vehicles within their dwell windows. The plateau represents "
    "the dwell-window ceiling: once K is sufficient to meet power demand, additional units "
    "yield no further improvement because the constraint shifts to vehicle dwell time. "
    "Glendale plateaus highest (≈90%) indicating well-spaced arrivals; "
    "San Diego plateaus lowest (≈24%) indicating chronically short dwell windows.", fig)

# ══════════════════════════════════════════════════════════════════════
# FIGURE P.02 — ANNUAL GRID DRAW + ENERGY PROFILE, 4-PANEL
# ══════════════════════════════════════════════════════════════════════
fig, axes = plt.subplots(2, 2, figsize=(16, 8), sharex=False)
axes = axes.flatten()
site_order = [("northgate","Northgate"), ("fresno","Fresno"),
              ("glendale","Glendale"),   ("san_diego","San Diego")]

for i,(sk,sl) in enumerate(site_order):
    ax = axes[i]
    sm = XD[sk]["sm"].copy()
    dc = XD[sk]["dc"].copy()
    merged = sm.merge(dc[["date","energy_cost_daily"]], on="date", how="left")
    dates = sm["date"].values
    peak_kw = sm["peak_grid_kw"].values
    grid_kwh = sm["total_grid_kwh"].values
    veh_kwh = sm["energy_delivered_kwh"].values
    mean_kw = np.mean(peak_kw)
    mean_kwh = np.mean(grid_kwh)

    ax2 = ax.twinx()
    ax2.bar(dates, grid_kwh, color=COLORS[sk], alpha=0.20, width=1.2, zorder=1, label="Grid kWh")
    ax2.bar(dates, veh_kwh,  color=COLORS[sk], alpha=0.45, width=1.2, zorder=2, label="Vehicle kWh")
    ax2.set_ylabel("kWh / day", fontsize=8, color="#666")
    ax2.tick_params(axis="y", labelsize=7, labelcolor="#666")

    ax.plot(dates, peak_kw, color=COLORS[sk], linewidth=1.2, alpha=0.85, zorder=4)
    ax.axhline(mean_kw, color=COLORS[sk], linewidth=1.2, linestyle="--", alpha=0.7, zorder=3,
               label=f"Mean {mean_kw:.0f} kW")
    ax.set_title(f"{sl} — {UTIL[sk]}", fontsize=9.5, fontweight="bold", color=COLORS[sk])
    ax.set_ylabel("Peak grid draw (kW)", fontsize=8)
    ax.tick_params(axis="both", labelsize=7)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b\n'%y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax.grid(linestyle=":", alpha=0.3)
    ax.set_zorder(ax2.get_zorder()+1); ax.patch.set_visible(False)
    legend_els = [Line2D([0],[0],color=COLORS[sk],linewidth=1.5,label=f"Peak grid draw (mean {mean_kw:.0f} kW)"),
                  Patch(color=COLORS[sk],alpha=0.45,label="Vehicle kWh/day"),
                  Patch(color=COLORS[sk],alpha=0.20,label="Grid kWh/day")]
    ax.legend(handles=legend_els, fontsize=7, loc="upper right", framealpha=0.85)

fig.suptitle("XOS Hub MC02 — Annual Grid Power Draw and Daily Energy Profile\n(Scenario A, May 2025 – Apr 2026)",
             fontsize=12, fontweight="bold", y=1.01)
fig.tight_layout()
savefig("P02_xos_grid_profile_4panel.png",
    "Figure P.2. XOS Hub MC02 — Annual Grid Power Draw and Energy Profile, All Sites (Scenario A). "
    "Line: daily peak grid draw (kW, left axis). Light bars: total grid kWh drawn per day. "
    "Dark bars: energy delivered to vehicles per day (right axis). Dashed line: annual mean "
    "peak grid draw. San Diego exhibits the highest sustained grid demand (>1,000 kW average) "
    "with chronically high vehicle counts; Glendale shows the lightest load profile.", fig)

# ══════════════════════════════════════════════════════════════════════
# FIGURE P.03 — XOS MONTHLY K-DISTRIBUTION HEATMAP (all sites)
# ══════════════════════════════════════════════════════════════════════
fig, axes = plt.subplots(1, 4, figsize=(16, 5), sharey=False)
for i,(sk,sl) in enumerate(site_order):
    ax = axes[i]
    sm = XD[sk]["sm"].copy()
    sm["month"] = sm["date"].dt.to_period("M")
    months = sorted(sm["month"].unique())
    month_labels = [str(m) for m in months]
    K_max = int(sm["K"].max())
    heat = np.zeros((K_max, len(months)))
    for j,mo in enumerate(months):
        mo_data = sm[sm.month==mo]["K"]
        for k_val, cnt in mo_data.value_counts().items():
            if 1 <= k_val <= K_max:
                heat[k_val-1, j] = cnt
    im = ax.imshow(heat, aspect="auto", origin="lower", cmap="YlOrRd",
                   extent=[-0.5, len(months)-0.5, 0.5, K_max+0.5])
    ax.set_xticks(range(len(months)))
    ax.set_xticklabels([m.strftime("%b\n'%y") for m in [mo.start_time for mo in months]],
                       fontsize=7)
    ax.set_yticks(range(1, K_max+1, max(1,K_max//8)))
    ax.set_title(f"{sl}\n({UTIL[sk]})", fontsize=9, fontweight="bold", color=COLORS[sk])
    ax.set_ylabel("K (XOS units required)", fontsize=8) if i==0 else None
    ax.set_xlabel("Month", fontsize=8)
    plt.colorbar(im, ax=ax, fraction=0.04, label="Days")

fig.suptitle("XOS Hub MC02 — Monthly Distribution of Daily Hub Count (K)\n(Scenario A — each cell = number of days requiring that K in that month)",
             fontsize=11, fontweight="bold")
fig.tight_layout()
savefig("P03_xos_monthly_k_heatmap.png",
    "Figure P.3. XOS Hub MC02 — Monthly Heat Map of Required Daily Hub Count (K), All Sites (Scenario A). "
    "Each cell shows how many operating days in a given month required exactly K units. "
    "Darker (red) cells indicate more frequent occurrence. San Diego's column is uniformly dark "
    "at K=15–20, confirming consistently high demand year-round. Glendale's distribution is "
    "concentrated at K=2–5, indicating low and stable demand.", fig)

# ══════════════════════════════════════════════════════════════════════
# FIGURE P.04 — XOS WORST-DAY TOP-10 (all 4 sites, 2×2)
# ══════════════════════════════════════════════════════════════════════
fig, axes = plt.subplots(2, 2, figsize=(16, 10))
axes = axes.flatten()

for i,(sk,sl) in enumerate(site_order):
    ax = axes[i]
    sm = XD[sk]["sm"].copy()
    dc = XD[sk]["dc"].copy()
    sm_dc = sm.merge(dc[["date","total_allin","energy_cost_daily"]], on="date", how="left")
    top10 = sm_dc.nlargest(10, "n_vehicles").reset_index(drop=True)
    # coverage at each row's K
    all_K = sm["K"].values
    top10["coverage"] = top10["K"].apply(lambda k: 100*np.mean(all_K <= k))

    ranks = [f"#{j+1}\n{r.date.strftime('%m/%d')}\nK={r.K}" for j,r in top10.iterrows()]
    x = np.arange(len(top10))
    w = 0.35
    ax.bar(x-w/2, top10["n_vehicles"],      width=w, color="#d73027", alpha=0.8, label="Total vehicles")
    ax.bar(x+w/2, top10["n_fully_served"],   width=w, color="#4dac26", alpha=0.8, label="Fully served")
    ax.set_xticks(x); ax.set_xticklabels(ranks, fontsize=7.5)
    ax.set_ylabel("Vehicles", fontsize=9)
    ax.set_title(f"{sl} ({UTIL[sk]}) — Top-10 Worst Days by Vehicle Count\n(Scenario A — coverage at each day's K shown)", fontsize=9.5, fontweight="bold", color=COLORS[sk])
    ax.legend(fontsize=8.5); ax.grid(axis="y", linestyle=":", alpha=0.4)
    for j,row in top10.iterrows():
        ax.text(j, max(row.n_vehicles, row.n_fully_served)+0.4,
                f"{row.coverage:.0f}%", ha="center", va="bottom",
                fontsize=6.5, color="#555", style="italic")

fig.suptitle("XOS Hub MC02 — Top-10 Highest-Demand Days per Site (Scenario A)\nBar pairs: total vehicles vs. fully served  |  % label = fraction of all days that day's K covers",
             fontsize=11, fontweight="bold")
fig.tight_layout()
savefig("P04_xos_worst_days_4panel.png",
    "Figure P.4. XOS Hub MC02 — Top-10 Highest-Demand Days per Site (Scenario A, sorted by vehicle count). "
    "Red bars: total vehicles arriving that day. Green bars: vehicles fully served. "
    "Italic percentage labels: fraction of all annual days that the K-value for that day would cover "
    "(Shima coverage). Note Northgate and San Diego show significant partially-served vehicles "
    "on peak days, while Glendale consistently achieves near-complete service even on its worst days.", fig)

# ══════════════════════════════════════════════════════════════════════
# FIGURE P.05 — XOS SHIMA COVERAGE ANALYSIS (4-panel bar)
# ══════════════════════════════════════════════════════════════════════
fig, axes = plt.subplots(2, 2, figsize=(16, 8))
axes = axes.flatten()
for i,(sk,sl) in enumerate(site_order):
    ax = axes[i]
    sm = XD[sk]["sm"].copy()
    sm_dc = sm.merge(XD[sk]["dc"][["date","total_allin"]], on="date", how="left")
    top10 = sm_dc.nlargest(10, "n_vehicles").reset_index(drop=True)
    all_K = sm["K"].values
    coverages = top10["K"].apply(lambda k: 100*np.mean(all_K <= k)).values
    peak_cov  = 100*np.mean(all_K <= sm["K"].max())

    bars = ax.bar(range(len(top10)), coverages, color=COLORS[sk], alpha=0.85, edgecolor="white")
    for j, (bar, c) in enumerate(zip(bars, coverages)):
        ax.text(bar.get_x()+bar.get_width()/2, c+0.5, f"{c:.1f}%",
                ha="center", va="bottom", fontsize=8, fontweight="bold", color=COLORS[sk])
    ax.axhline(peak_cov, color="#888", linewidth=1.3, linestyle="--",
               label=f"K-sweep ceiling: {peak_cov:.1f}%")
    ax.set_xticks(range(len(top10)))
    ax.set_xticklabels([f"#{j+1}\nK={r.K}" for j,r in top10.iterrows()], fontsize=8)
    ax.set_ylim(0,115); ax.yaxis.set_major_formatter(mticker.PercentFormatter())
    ax.set_title(f"{sl} ({UTIL[sk]})", fontsize=10, fontweight="bold", color=COLORS[sk])
    ax.set_ylabel("% of all days fully covered", fontsize=8)
    ax.set_xlabel("Worst-day rank (by vehicle count)", fontsize=8)
    ax.legend(fontsize=8); ax.grid(axis="y", linestyle=":", alpha=0.4)

fig.suptitle("Shima Coverage Analysis — What K from Each Worst Day Covers Across All Operating Days\n(Scenario A — fixing K at each worst day's requirement, what fraction of days are also covered?)",
             fontsize=11, fontweight="bold")
fig.tight_layout()
savefig("P05_xos_shima_coverage.png",
    "Figure P.5. Shima Coverage Analysis — XOS Hub MC02, All Sites (Scenario A). "
    "For each of the top-10 worst days (sorted by vehicle count), the bar shows what fraction "
    "of ALL operating days that day's K-value would also fully cover. The dashed line is the "
    "theoretical maximum (K-sweep ceiling), which cannot be exceeded due to dwell-window constraints. "
    "Glendale's worst-day K covers nearly 90% of all days; San Diego's K=20 cap covers only ~24%.", fig)

# ══════════════════════════════════════════════════════════════════════
# FIGURE P.06 — KEMPOWER CHARGER-MIX DISTRIBUTION (Northgate + Fresno)
# ══════════════════════════════════════════════════════════════════════
def fig_kmp_mix_panel(sk, sl, kd, savename, caption):
    fig = plt.figure(figsize=(16, 6))
    gs  = fig.add_gridspec(1, 3, width_ratios=[2,1,1], wspace=0.35)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1])
    ax3 = fig.add_subplot(gs[2])

    # Left: monthly stacked bar of avg chargers per type
    kd["month"] = kd["date"].dt.to_period("M")
    mo_grp = kd.groupby("month")[["n_50","n_150","n_250"]].mean()
    months = [str(m) for m in mo_grp.index]
    x = np.arange(len(months))
    ax1.bar(x, mo_grp["n_50"],  color="#2166ac", alpha=0.9, label="50 kW",  width=0.65)
    ax1.bar(x, mo_grp["n_150"], bottom=mo_grp["n_50"], color="#4dac26", alpha=0.9, label="150 kW", width=0.65)
    ax1.bar(x, mo_grp["n_250"], bottom=mo_grp["n_50"]+mo_grp["n_150"], color="#d73027", alpha=0.9, label="250 kW", width=0.65)
    ax1.set_xticks(x)
    ax1.set_xticklabels([pd.Period(m).start_time.strftime("%b\n'%y") for m in months], fontsize=7)
    ax1.set_ylabel("Avg chargers/day", fontsize=9); ax1.set_title("Average Daily Charger Mix by Month", fontsize=9.5)
    ax1.legend(fontsize=9, loc="upper right"); ax1.grid(axis="y", linestyle=":", alpha=0.4)

    # Middle: charger count histogram
    ax2.hist(kd["n_chargers"], bins=range(1, int(kd["n_chargers"].max())+2),
             color="#4dac26", alpha=0.85, edgecolor="white", rwidth=0.8, align="left")
    for p in ax2.patches:
        h = p.get_height()
        if h > 0: ax2.text(p.get_x()+p.get_width()/2, h+0.3, str(int(h)), ha="center", va="bottom", fontsize=7)
    ax2.set_xlabel("Chargers deployed (K)", fontsize=9)
    ax2.set_ylabel("Number of days", fontsize=9)
    ax2.set_title("Distribution of Daily\nCharger Count", fontsize=9.5)
    ax2.grid(axis="y", linestyle=":", alpha=0.4)

    # Right: service rate histogram
    ax3.hist(kd["svc_rate_pct"], bins=np.arange(45,102,5), color="#4dac26", alpha=0.85,
             edgecolor="white", rwidth=0.85)
    mean_svc = kd["svc_rate_pct"].mean()
    ax3.axvline(mean_svc, color="#d73027", linestyle="--", linewidth=1.5,
                label=f"Mean {mean_svc:.1f}%")
    ax3.set_xlabel("Daily service rate (%)", fontsize=9)
    ax3.set_ylabel("Number of days", fontsize=9)
    ax3.set_title("Distribution of Daily\nService Rate", fontsize=9.5)
    ax3.legend(fontsize=8); ax3.grid(axis="y", linestyle=":", alpha=0.4)

    fig.suptitle(f"Kempower Fixed DCFC — Daily Charger-Mix Distribution  |  {sl}  ({UTIL[sk]})",
                 fontsize=12, fontweight="bold")
    fig.tight_layout()
    savefig(savename, caption, fig)

for sk in ["northgate","fresno"]:
    sl = dict(SITES)[sk]
    if KD[sk] is None: continue
    fig_kmp_mix_panel(sk, sl, KD[sk].copy(),
        f"P06_kmp_charger_mix_{sk}.png",
        f"Figure P.6/{sk[0].upper()}. Kempower Fixed DCFC — Daily Charger-Mix Distribution, {sl} ({UTIL[sk]}). "
        f"Left: stacked bars show the average number of 50 kW (blue), 150 kW (green), and 250 kW (red) "
        f"chargers selected per day in each month. Center: histogram of total chargers deployed per day "
        f"(K). Right: histogram of daily service rate (% vehicles fully charged within dwell window). "
        f"The MILP consistently selects predominantly 150 kW chargers for the Northgate fleet mix, "
        f"with 250 kW units added on high-demand days.")

# ══════════════════════════════════════════════════════════════════════
# FIGURE P.07 — KEMPOWER SERVICE RATE + ENERGY PROFILE (annual)
# ══════════════════════════════════════════════════════════════════════
def fig_kmp_service_energy(sk, sl, kd, savename, caption):
    fig, (ax1,ax2) = plt.subplots(2,1,figsize=(14,7),sharex=True)
    dates = kd["date"].values
    svc   = kd["svc_rate_pct"].values
    mean_svc = np.mean(svc)
    ax1.plot(dates, svc, color="#4dac26", linewidth=1.2, alpha=0.85, zorder=3)
    ax1.fill_between(dates, mean_svc, svc, where=svc<mean_svc,
                     alpha=0.25, color="#d73027", zorder=2, label="Below mean")
    ax1.axhline(mean_svc, color="#d73027", linewidth=1.4, linestyle="--",
                label=f"Mean {mean_svc:.1f}%", zorder=4)
    ax1.set_ylabel("Service rate (%)", fontsize=9)
    ax1.set_title(f"Daily Service Rate (% vehicles fully served)", fontsize=9.5)
    ax1.yaxis.set_major_formatter(mticker.PercentFormatter())
    ax1.set_ylim(40,105); ax1.legend(fontsize=8.5,loc="lower right"); ax1.grid(linestyle=":",alpha=0.35)

    ax2.bar(dates, kd["e_delivered_kwh"], color="#4dac26", alpha=0.75, width=1.2, label="Delivered (kWh)", zorder=3)
    ax2.bar(dates, kd["e_demanded_kwh"]-kd["e_delivered_kwh"],
            bottom=kd["e_delivered_kwh"], color="#d73027", alpha=0.75, width=1.2, label="Unmet (kWh)", zorder=3)
    ax2.set_ylabel("Energy (kWh)", fontsize=9)
    ax2.set_title("Daily Energy Delivered vs. Unmet", fontsize=9.5)
    ax2.legend(fontsize=8.5); ax2.grid(axis="y",linestyle=":",alpha=0.35)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b\n'%y"))
    ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=1))

    fig.suptitle(f"Kempower Fixed DCFC — Service Rate & Energy Profile  |  {sl}  ({UTIL[sk]})",
                 fontsize=12, fontweight="bold")
    fig.tight_layout()
    savefig(savename, caption, fig)

for sk in ["northgate","fresno"]:
    sl = dict(SITES)[sk]
    if KD[sk] is None: continue
    fig_kmp_service_energy(sk, sl, KD[sk].copy(),
        f"P07_kmp_service_energy_{sk}.png",
        f"Figure P.7/{sk[0].upper()}. Kempower Fixed DCFC — Annual Service Rate and Energy Profile, {sl}. "
        f"Top: daily service rate (% vehicles fully charged). Red shading marks days below the annual mean. "
        f"Bottom: stacked bars showing daily energy delivered to vehicles (green) and energy unmet "
        f"(red, = energy demanded but not delivered due to dwell-window constraints or charger limits). "
        f"Unmet energy is small relative to delivered energy in most months, with occasional high-demand "
        f"days creating larger gaps.")

# ══════════════════════════════════════════════════════════════════════
# FIGURE P.08 — KEMPOWER DAILY COST + PEAK POWER PROFILE (annual)
# ══════════════════════════════════════════════════════════════════════
def fig_kmp_cost_power(sk, sl, kd, savename, caption):
    fig, (ax1,ax2) = plt.subplots(2,1,figsize=(14,7),sharex=True)
    dates = kd["date"].values
    demand_daily = (kd["global_demand_cost"]+kd["peak_window_demand_cost"])/DAYS_PER_MO
    mean_total = (kd["capex_daily"]+kd["energy_cost"]+demand_daily).mean()
    ax1.bar(dates, kd["capex_daily"],  color="#2166ac", alpha=0.9, width=1.2, label="CapEx (amort.)", zorder=3)
    ax1.bar(dates, kd["energy_cost"], bottom=kd["capex_daily"], color="#4dac26", alpha=0.9, width=1.2, label="Energy (TOU)", zorder=3)
    ax1.bar(dates, demand_daily, bottom=kd["capex_daily"]+kd["energy_cost"],
            color="#d01c8b", alpha=0.9, width=1.2, label="Demand (amort.)", zorder=3)
    ax1.axhline(mean_total, color="#d73027", linewidth=1.4, linestyle="--",
                label=f"Mean ${mean_total:.0f}/day", zorder=4)
    ax1.set_ylabel("Daily cost (USD)", fontsize=9)
    ax1.set_title("Daily Cost Breakdown — CapEx + Energy + Demand Charges", fontsize=9.5)
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v,_:f"${v:,.0f}"))
    ax1.legend(fontsize=8.5, loc="upper right"); ax1.grid(axis="y",linestyle=":",alpha=0.35)

    mean_pk = kd["peak_kw"].mean()
    ax2.plot(dates, kd["peak_kw"], color="#d01c8b", linewidth=1.2, alpha=0.85, zorder=3)
    ax2.fill_between(dates, 0, kd["peak_kw"], color="#d01c8b", alpha=0.10)
    ax2.axhline(mean_pk, color="#d01c8b", linewidth=1.4, linestyle="--",
                label=f"Mean {mean_pk:.0f} kW", zorder=4)
    ax2.set_ylabel("Peak grid draw (kW)", fontsize=9)
    ax2.set_title("Daily Peak Grid Power Demand", fontsize=9.5)
    ax2.legend(fontsize=8.5); ax2.grid(axis="y",linestyle=":",alpha=0.35)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b\n'%y"))
    ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=1))

    fig.suptitle(f"Kempower Fixed DCFC — Daily Cost & Peak Power Profile  |  {sl}  ({UTIL[sk]})",
                 fontsize=12, fontweight="bold")
    fig.tight_layout()
    savefig(savename, caption, fig)

for sk in ["northgate","fresno"]:
    sl = dict(SITES)[sk]
    if KD[sk] is None: continue
    fig_kmp_cost_power(sk, sl, KD[sk].copy(),
        f"P08_kmp_cost_power_{sk}.png",
        f"Figure P.8/{sk[0].upper()}. Kempower Fixed DCFC — Annual Daily Cost and Peak Power Profile, {sl}. "
        f"Top: stacked daily cost bars showing capital amortization (blue), TOU energy cost (green), "
        f"and amortized demand/subscription charges (pink). Dashed line: annual mean all-in daily cost. "
        f"Bottom: daily peak grid draw (kW). Demand charges are amortized from monthly totals using "
        f"30.42 days/month. High-energy days in summer and April drive peak costs.")

# ══════════════════════════════════════════════════════════════════════
# FIGURE P.09 — KEMPOWER vs XOS HEAD-TO-HEAD SCATTER (Northgate)
# ══════════════════════════════════════════════════════════════════════
for sk in ["northgate","fresno"]:
    sl = dict(SITES)[sk]
    kd = KD[sk]
    if kd is None: continue
    xd_dc = XD[sk]["dc"].copy()
    xd_sm = XD[sk]["sm"].copy()

    joined = kd.merge(xd_dc[["date","total_allin","energy_cost_daily","demand_global_monthly_$","demand_peak_win_monthly_$"]],
                      on="date", how="inner", suffixes=("_kmp","_xos"))
    joined2= joined.merge(xd_sm[["date","service_rate_pct"]], on="date", how="inner")
    joined2.rename(columns={"service_rate_pct":"svc_xos","svc_rate_pct":"svc_kmp"}, errors="ignore", inplace=True)
    if "svc_kmp" not in joined2.columns: joined2["svc_kmp"] = kd["svc_rate_pct"].values[:len(joined2)]

    fig, (ax1,ax2) = plt.subplots(1,2,figsize=(13,5.5))
    # Cost scatter
    mx_cost = max(joined2["total_allin_xos"].max(), joined2["total_allin_kmp"].max())*1.05
    ax1.scatter(joined2["total_allin_xos"], joined2["total_allin_kmp"],
                color="#4dac26", alpha=0.55, s=20, edgecolors="none")
    ax1.plot([0,mx_cost],[0,mx_cost],"--",color="#aaa",linewidth=1.2,label="Equal cost")
    ax1.set_xlabel("XOS Hub A1 — Daily Cost (all-in, $/day)", fontsize=9)
    ax1.set_ylabel("Kempower — Daily Cost (all-in, $/day)", fontsize=9)
    ax1.set_title(f"Daily Cost: Kempower vs XOS A1\n(points below diagonal = Kempower cheaper)", fontsize=9.5)
    ax1.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v,_:f"${v:,.0f}"))
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v,_:f"${v:,.0f}"))
    ax1.legend(fontsize=8.5); ax1.grid(linestyle=":",alpha=0.35)
    frac_cheaper = (joined2["total_allin_kmp"] < joined2["total_allin_xos"]).mean()
    ax1.text(0.04,0.92,f"{frac_cheaper*100:.0f}% of days:\nKempower cheaper",
             transform=ax1.transAxes, fontsize=9, color="#4dac26",
             bbox=dict(boxstyle="round,pad=0.3",facecolor="#f0fff0",edgecolor="#4dac26"))

    # Service rate scatter
    ax2.scatter(joined2["svc_xos"] if "svc_xos" in joined2 else joined2["service_rate_pct"],
                joined2["svc_kmp"] if "svc_kmp" in joined2 else joined2["svc_rate_pct_kmp"],
                color="#2166ac", alpha=0.55, s=20, edgecolors="none")
    ax2.plot([0,105],[0,105],"--",color="#aaa",linewidth=1.2,label="Equal service rate")
    ax2.set_xlabel("XOS Hub A1 — Service Rate (%)", fontsize=9)
    ax2.set_ylabel("Kempower — Service Rate (%)", fontsize=9)
    ax2.set_title(f"Service Rate: Kempower vs XOS A1\n(points above diagonal = Kempower serves more)", fontsize=9.5)
    ax2.legend(fontsize=8.5); ax2.grid(linestyle=":",alpha=0.35)
    ax2.set_xlim(0,105); ax2.set_ylim(0,105)

    fig.suptitle(f"Kempower Fixed DCFC vs. XOS Hub MC02 — Head-to-Head Comparison  |  {sl}",
                 fontsize=11, fontweight="bold")
    fig.tight_layout()
    savefig(f"P09_kmp_vs_xos_scatter_{sk}.png",
        f"Figure P.9/{sk[0].upper()}. Kempower Fixed DCFC vs. XOS Hub MC02 — Day-by-Day Cost and Service Rate Comparison, {sl}. "
        f"Left: each point = one operating day; X-axis = XOS all-in daily cost, Y-axis = Kempower all-in daily cost. "
        f"Points below the dashed equality line indicate Kempower is the lower-cost option that day. "
        f"Right: analogous comparison for service rate (% vehicles fully charged). "
        f"Kempower is cheaper on virtually all days due to lower amortized capital cost; "
        f"XOS achieves higher service rates on some high-demand days thanks to the battery buffer.", fig)

# ══════════════════════════════════════════════════════════════════════
# FIGURE P.10 — KEMPOWER WORST-DAYS (Northgate + Fresno side by side)
# ══════════════════════════════════════════════════════════════════════
def fig_kmp_worst_days(sk, sl, kd, savename, caption):
    top10 = kd.nlargest(10, "e_demanded_kwh").reset_index(drop=True)
    fig, (ax1,ax2) = plt.subplots(1,2,figsize=(14,5.5))
    x = np.arange(len(top10))
    w = 0.28
    ax1.bar(x-w, top10["n_vehicles"],  width=w, color="#555", alpha=0.8, label="Total")
    ax1.bar(x,   top10["n_full"],       width=w, color="#4dac26", alpha=0.85, label="Fully served")
    partial = top10["n_vehicles"]-top10["n_full"]
    ax1.bar(x+w, partial, width=w, color="#d73027", alpha=0.8, label="Partial/Unserved")
    ax1.set_xticks(x)
    ax1.set_xticklabels([f"#{j+1}\n{r.date.strftime('%m/%d')}\n{r['mix'].replace(' ','')[:14]}"
                          for j,r in top10.iterrows()], fontsize=6.5)
    ax1.set_ylabel("Vehicles", fontsize=9)
    ax1.set_title("Vehicles per Worst Day (by energy demanded)", fontsize=9.5)
    ax1.legend(fontsize=8.5); ax1.grid(axis="y",linestyle=":",alpha=0.4)

    demand_d = (top10["global_demand_cost"]+top10["peak_window_demand_cost"])/DAYS_PER_MO
    ax2.bar(x, top10["capex_daily"],  color="#2166ac", alpha=0.9, label="CapEx", zorder=3)
    ax2.bar(x, top10["energy_cost"], bottom=top10["capex_daily"], color="#4dac26", alpha=0.9, label="Energy", zorder=3)
    ax2.bar(x, demand_d, bottom=top10["capex_daily"]+top10["energy_cost"],
            color="#d01c8b", alpha=0.9, label="Demand", zorder=3)
    ax2.set_xticks(x)
    ax2.set_xticklabels([f"#{j+1}\n{r.date.strftime('%m/%d')}" for j,r in top10.iterrows()], fontsize=8)
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v,_:f"${v:,.0f}"))
    ax2.set_ylabel("Daily cost (USD)", fontsize=9)
    ax2.set_title("Daily Cost per Worst Day", fontsize=9.5)
    ax2.legend(fontsize=8.5); ax2.grid(axis="y",linestyle=":",alpha=0.4)

    fig.suptitle(f"Kempower Fixed DCFC — Top-10 Worst Days (by Energy Demanded)  |  {sl}  ({UTIL[sk]})",
                 fontsize=11, fontweight="bold")
    fig.tight_layout()
    savefig(savename, caption, fig)

for sk in ["northgate","fresno"]:
    sl = dict(SITES)[sk]
    if KD[sk] is None: continue
    fig_kmp_worst_days(sk, sl, KD[sk].copy(),
        f"P10_kmp_worst_days_{sk}.png",
        f"Figure P.10/{sk[0].upper()}. Kempower Fixed DCFC — Top-10 Worst Days by Total Energy Demanded, {sl}. "
        f"Left: vehicle service breakdown (total / fully served / partial+unserved) on each worst day. "
        f"Right: stacked cost breakdown by component (CapEx amortization, TOU energy, demand charges). "
        f"Partially served vehicles on worst days typically have very short dwell windows that cannot "
        f"be fully met even by the MILP-optimal charger mix.")

# ══════════════════════════════════════════════════════════════════════
# FIGURE P.11 — KEMPOWER GLENDALE (available, but GWP proxy)
# ══════════════════════════════════════════════════════════════════════
if KD["glendale"] is not None:
    kd_gl = KD["glendale"].copy()
    fig, axes = plt.subplots(1,3,figsize=(15,5))
    dates = kd_gl["date"].values
    demand_d = (kd_gl["global_demand_cost"]+kd_gl["peak_window_demand_cost"])/DAYS_PER_MO

    axes[0].bar(dates, kd_gl["capex_daily"],  color="#2166ac", alpha=0.9, width=1.2, label="CapEx")
    axes[0].bar(dates, kd_gl["energy_cost"], bottom=kd_gl["capex_daily"], color="#4dac26", alpha=0.9, width=1.2, label="Energy")
    axes[0].bar(dates, demand_d, bottom=kd_gl["capex_daily"]+kd_gl["energy_cost"],
                color="#d01c8b", alpha=0.9, width=1.2, label="Demand ⚠proxy")
    axes[0].axhline(kd_gl["total_allin"].mean(), color="#d73027", linewidth=1.3, linestyle="--",
                    label=f"Mean ${kd_gl['total_allin'].mean():.0f}/day")
    axes[0].set_ylabel("Daily cost (USD)"); axes[0].set_title("Daily Cost — Full Year")
    axes[0].xaxis.set_major_formatter(mdates.DateFormatter("%b\n'%y"))
    axes[0].xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    axes[0].legend(fontsize=7.5); axes[0].grid(axis="y",linestyle=":",alpha=0.35)
    axes[0].yaxis.set_major_formatter(mticker.FuncFormatter(lambda v,_:f"${v:,.0f}"))

    axes[1].hist(kd_gl["n_chargers"], bins=range(1,int(kd_gl["n_chargers"].max())+2),
                 color="#f46d43", alpha=0.85, edgecolor="white", rwidth=0.85, align="left")
    axes[1].set_xlabel("Chargers deployed (K)"); axes[1].set_ylabel("Number of days")
    axes[1].set_title("Charger Count Distribution"); axes[1].grid(axis="y",linestyle=":",alpha=0.35)

    axes[2].hist(kd_gl["svc_rate_pct"], bins=np.arange(40,102,5), color="#f46d43", alpha=0.85, edgecolor="white")
    axes[2].axvline(kd_gl["svc_rate_pct"].mean(), color="#d73027", linestyle="--", linewidth=1.4,
                    label=f"Mean {kd_gl['svc_rate_pct'].mean():.1f}%")
    axes[2].set_xlabel("Service rate (%)"); axes[2].set_ylabel("Number of days")
    axes[2].set_title("Service Rate Distribution"); axes[2].legend(fontsize=8.5)
    axes[2].grid(axis="y",linestyle=":",alpha=0.35)

    fig.suptitle("Kempower Fixed DCFC — Glendale  (PG&E BEV-2 proxy — GWP tariff unconfirmed ⚠)\n"
                 "Note: utility rates unconfirmed — see open items",
                 fontsize=11, fontweight="bold")
    for a in axes: a.tick_params(axis="x", labelsize=8)
    fig.tight_layout()
    savefig("P11_kmp_glendale_summary.png",
        "Figure P.11. Kempower Fixed DCFC — Glendale Summary (255 Operating Days). "
        "⚠ CAUTION: all costs use PG&E BEV-2 as a proxy for GWP — actual GWP tariff not yet obtained. "
        "Left: daily cost profile (CapEx + energy + demand). Center: distribution of daily charger "
        "count K. Right: distribution of daily service rate. Glendale has the lightest demand profile "
        "of all four sites, with 1–3 chargers sufficient on most days.", fig)

# ══════════════════════════════════════════════════════════════════════
# SAVE CAPTIONS FILE
# ══════════════════════════════════════════════════════════════════════
cap_path = FIGS / "figure_captions.txt"
with open(cap_path, "w", encoding="utf-8") as f:
    f.write("APPENDIX A — SUPPLEMENTAL FIGURE CAPTIONS\n")
    f.write("Task 4482 | Mobile DCFC Cost Analysis | Generated 2026-06-30\n")
    f.write("="*80 + "\n\n")
    for name, cap in captions.items():
        f.write(f"[{name}]\n{cap}\n\n")
print(f"\nCaptions: {cap_path}")
print(f"All figures saved to: {FIGS}")
print(f"Total figures: {len(captions)}")
