"""
run_site_pipeline.py
====================
Full XOS Hub MC02 analysis pipeline for one Caltrans site.

Usage:
    python run_site_pipeline.py fresno
    python run_site_pipeline.py glendale
    python run_site_pipeline.py san_diego

Phases (run in sequence):
  1. Adaptive-K simulation for all days (A1 + A2, proactive recharge)
     → summary.csv, cost_detail.csv, sanity_log.csv, analysis_report.txt
     → per_day/{date}/ CSVs (dispatch, state, grid_draw, vehicle_results)
  2. Day-view figures for every day
     → per_day/{date}/day_view_{date}.png
  3. Top-10 worst days + Shima K-sweep coverage analysis
     → worst_days/ report, figures, k_sweep_coverage.png
"""
from __future__ import annotations

import io, sys, glob, re, math, contextlib
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import matplotlib.gridspec as gridspec

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))
import scenario_runner as sr
from charger_costs_xos_hub import XOS_HUB_SPECS, electrical_infra_cost

# ── Site registry ──────────────────────────────────────────────────────────────
SITE_LABELS = {
    "fresno":    "Fresno",
    "glendale":  "Glendale",
    "san_diego": "San Diego",
    "northgate": "Northgate",
}

# ── XOS constants ──────────────────────────────────────────────────────────────
BASE_DIR    = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
MAX_K       = sr._XOS["MAX_UNITS"]
NP          = sr._XOS["N_PORTS"]
PP          = sr._XOS["P_PORT"]
ED          = sr._XOS["ETA_D"]
DT          = sr._XOS["DT_H"]
DT_MIN      = int(DT * 60)
SMIN        = sr._XOS["SOC_MIN"]
SMAX        = sr._XOS["SOC_MAX"]
PG          = sr._XOS["P_GRID"]
DAYS_PER_YEAR = 365.25
TZ          = "America/Los_Angeles"
ENERGY_TOL  = sr.ENERGY_TOL

S                = XOS_HUB_SPECS
PURCHASE         = S["purchase_cost"]
ANNUAL_MAINT     = S["annual_maint"]
ANNUAL_WARRANTY  = S["annual_warranty"]
LIFE_YEARS       = S["life_years"]

STATE_COLOR = {
    "idle":       ("#cccccc", 0.55),
    "serving":    ("#b8d4f0", 0.45),
    "recharging": ("#90e090", 0.70),
}

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Simulation helpers
# ══════════════════════════════════════════════════════════════════════════════

def sanity_check(sim, events_ext, date_str, scenario):
    issues = []
    K = sim["n_units"]
    if K > MAX_K: issues.append(f"UNREASONABLE_K: K={K} > MAX_K={MAX_K}")
    if K == 0:    issues.append("ZERO_K: no hubs deployed")

    df     = pd.DataFrame(sim["dispatch_log"]) if sim["dispatch_log"] else pd.DataFrame()
    soc_df = pd.DataFrame(sim["soc_history"])
    soc_df["time_utc"] = pd.to_datetime(soc_df["time_utc"], utc=True)

    soc_cols = [c for c in soc_df.columns if c.startswith("soc_unit_")]
    for col in soc_cols:
        if (soc_df[col] < SMIN - 0.005).any():
            bad = soc_df[soc_df[col] < SMIN - 0.005][col].min()
            issues.append(f"SOC_BELOW_MIN: {col} min={bad:.4f}")
        if (soc_df[col] > SMAX + 0.005).any():
            bad = soc_df[soc_df[col] > SMAX + 0.005][col].max()
            issues.append(f"SOC_ABOVE_MAX: {col} max={bad:.4f}")

    if df.empty:
        return {"date": date_str, "scenario": scenario, "pass": not issues,
                "n_checks": 6, "n_violations": len(issues), "details": issues}

    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    ev_map = {row["charging_event_id"]: {"arr": row["arrival_time"], "dep": row["departure_time"]}
              for _, row in events_ext.iterrows()}

    max_per_step = PP * DT * ED * 1.02
    bad_p = df[df["energy_to_vehicle_kwh"] > max_per_step]
    if not bad_p.empty:
        issues.append(f"PORT_POWER_EXCESS: {len(bad_p)} steps (max={bad_p['energy_to_vehicle_kwh'].max():.3f})")

    win_viol = 0
    for _, row in df.iterrows():
        v = row["event_id"]
        if v not in ev_map: continue
        t = row["time_utc"]; t1 = t + pd.Timedelta(hours=DT)
        if t1 <= ev_map[v]["arr"] or t >= ev_map[v]["dep"]: win_viol += 1
    if win_viol > 0:
        issues.append(f"WINDOW_VIOLATION: {win_viol} steps")

    hub_step = df.groupby(["step_idx", "unit"])["energy_to_vehicle_kwh"].sum()
    bad_hub  = hub_step[hub_step > PP * NP * DT * 1.02]
    if not bad_hub.empty:
        issues.append(f"HUB_POWER_EXCESS: {len(bad_hub)} hub-steps")

    hub_vc = df.groupby(["step_idx", "unit"])["event_id"].nunique()
    over_c = hub_vc[hub_vc > NP]
    if not over_c.empty:
        issues.append(f"PORT_CAPACITY_EXCEEDED: {len(over_c)} hub-steps")

    log_del = df.groupby("event_id")["energy_to_vehicle_kwh"].sum()
    for v, dv in sim["delivered"].items():
        ld = log_del.get(v, 0.0)
        if abs(dv - ld) > 1.0:
            issues.append(f"ENERGY_MISMATCH: {v} dict={dv:.2f} log={ld:.2f}")

    for v in sim["delivered"]:
        if sim["remaining"].get(v, 0) <= ENERGY_TOL and sim["delivered"][v] < ENERGY_TOL:
            issues.append(f"SERVED_ZERO_ENERGY: {v}")

    if sim["grid_draw"]:
        peak = max(sim["grid_draw"])
        if peak > K * PG * 1.01:
            issues.append(f"PEAK_POWER_IMPLAUSIBLE: {peak:.0f} > {K*PG:.0f}")

    return {"date": date_str, "scenario": scenario, "pass": len(issues) == 0,
            "n_checks": 9, "n_violations": len(issues), "details": issues}


def cost_breakdown(sim, date_str):
    K     = sim["n_units"]
    costs = sr._xos_a1_cost(sim, date_str)
    infra       = electrical_infra_cost(K, "mid")
    infra_total = infra["total"]
    infra_daily = infra_total / (LIFE_YEARS * DAYS_PER_YEAR)
    purchase_capex = PURCHASE / (LIFE_YEARS * DAYS_PER_YEAR) * K
    maint_total    = ANNUAL_MAINT    / DAYS_PER_YEAR * K
    warranty_total = ANNUAL_WARRANTY / DAYS_PER_YEAR * K
    energy_cost    = costs["energy_cost"]
    total_var      = purchase_capex + infra_daily + maint_total + warranty_total + energy_cost
    d_global       = costs["demand_global"]
    d_peak         = costs["demand_peak_win"]
    return {
        "date": date_str, "K": K,
        "purchase_capex_daily": round(purchase_capex, 2),
        "infra_capex_daily":    round(infra_daily, 2),
        "maint_daily":          round(maint_total, 2),
        "warranty_daily":       round(warranty_total, 2),
        "energy_cost_daily":    round(energy_cost, 2),
        "demand_global_monthly_$":   round(d_global, 2),
        "demand_peak_win_monthly_$": round(d_peak, 2),
        "total_daily_excl_demand":   round(total_var, 2),
        "total_daily_incl_demand":   round(total_var + d_global / 30, 2),
        "total_grid_kwh":        round(costs["total_grid_kwh"], 2),
        "vehicle_kwh_delivered": round(costs["vehicle_kwh"], 2),
        "peak_grid_kw":          round(costs["p_max_kw"], 1),
        "peak_win_kw":           round(costs["p_peak_win_kw"], 1),
        "infra_total_mid_$":     round(infra_total, 0),
        "infra_per_unit_mid_$":  round(infra["per_unit_avg"], 0),
    }


def utilization_metrics(sim):
    K = sim["n_units"]; total_steps = sim["n_steps"]
    df = pd.DataFrame(sim["dispatch_log"]) if sim["dispatch_log"] else pd.DataFrame()
    if df.empty or total_steps == 0:
        return {"hub_utilization_pct": 0.0, "port_utilization_pct": 0.0,
                "avg_ports_active_per_hub": 0.0,
                "recharge_steps_total": 0, "serving_steps_total": 0, "idle_steps_total": 0}
    active_ps  = len(df[df["energy_to_vehicle_kwh"] > ENERGY_TOL])
    port_util  = 100 * active_ps / max(K * NP * total_steps, 1)
    hub_act    = df[df["energy_to_vehicle_kwh"] > ENERGY_TOL].groupby(["step_idx","unit"]).size()
    hub_util   = 100 * hub_act.shape[0] / max(K * total_steps, 1)
    avg_ports  = active_ps / max(K * total_steps, 1)
    sdf        = pd.DataFrame(sim["soc_history"])
    sc         = [c for c in sdf.columns if c.startswith("state_unit_")]
    all_s      = sdf[sc].values.flatten()
    return {
        "hub_utilization_pct":      round(hub_util, 1),
        "port_utilization_pct":     round(port_util, 1),
        "avg_ports_active_per_hub": round(avg_ports, 2),
        "recharge_steps_total":     int((all_s == "recharging").sum()),
        "serving_steps_total":      int((all_s == "serving").sum()),
        "idle_steps_total":         int((all_s == "idle").sum()),
    }


def _save_day_outputs(sim, events_ext, day_dir, mode, date_str):
    day_dir.mkdir(parents=True, exist_ok=True)
    tag = mode.upper()
    if sim["dispatch_log"]:
        df_d = pd.DataFrame(sim["dispatch_log"])
        df_d["time_pac"] = pd.to_datetime(df_d["time_utc"], utc=True).dt.tz_convert(
            sr.SMUD_TZ).dt.strftime("%H:%M")
        df_d.to_csv(day_dir / f"{tag}_dispatch_{date_str}.csv", index=False)
    pd.DataFrame(sim["soc_history"]).to_csv(day_dir / f"{tag}_state_{date_str}.csv", index=False)
    times_utc = [r["time_utc"] for r in sim["soc_history"]]
    pd.DataFrame({
        "time_utc": times_utc,
        "time_pac": [pd.Timestamp(t, tz="UTC").tz_convert(sr.SMUD_TZ).strftime("%H:%M")
                     for t in times_utc],
        "grid_kw":  sim["grid_draw"],
    }).to_csv(day_dir / f"{tag}_grid_draw_{date_str}.csv", index=False)
    rows = []
    for _, row in events_ext.sort_values("arrival_time").iterrows():
        v = row["charging_event_id"]
        needed = float(row["energy_needed_kwh_for_visit"])
        deliv  = sim["delivered"].get(v, 0.0)
        rem    = sim["remaining"].get(v, needed)
        status = ("fully_served" if rem <= ENERGY_TOL else
                  "partially_served" if deliv > ENERGY_TOL else "unserved")
        rows.append({"event_id": v, "model": row.get("ev_equivalent_model", ""),
                     "arrival_pac":   pd.Timestamp(row["arrival_time"]).tz_convert(sr.SMUD_TZ).strftime("%H:%M"),
                     "departure_pac": pd.Timestamp(row["departure_time"]).tz_convert(sr.SMUD_TZ).strftime("%H:%M"),
                     "energy_needed_kwh":    round(needed, 2),
                     "energy_delivered_kwh": round(deliv,  2),
                     "energy_unmet_kwh":     round(rem,    2),
                     "status": status})
    pd.DataFrame(rows).to_csv(day_dir / f"{tag}_vehicle_results_{date_str}.csv", index=False)


def run_one_day(csv_path, date_str, csv_stem, per_day_root=None):
    stem_parts    = csv_path.stem.rsplit("_", 3)
    site_csv_stem = "_".join(stem_parts[:-3]) if len(stem_parts) > 3 else csv_path.stem
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            ev = sr.load_site_day_data(csv_path)
            ev = sr.apply_multiday_rule(ev, date_str, site_csv_dir=csv_path.parent,
                                        site_csv_stem=site_csv_stem)
    except Exception as e:
        return {"error": str(e), "date": date_str}
    if ev.empty:
        return None
    events_ext = sr._xos_extended_dwell(ev)
    n_events   = len(events_ext)
    results    = {}
    for mode in ("a1", "a2"):
        best_served = 0; best_K = 1; sim_best = None
        for K in range(1, MAX_K + 1):
            sim = sr._simulate_xos(events_ext, K, mode=mode)
            if sim["n_served"] > best_served:
                best_served = sim["n_served"]; best_K = K; sim_best = sim
            if sim["n_served"] >= sim["n_vehicles"]:
                break
        sim = sim_best
        if per_day_root is not None:
            _save_day_outputs(sim, events_ext, per_day_root / date_str, mode, date_str)
        sc       = sanity_check(sim, events_ext, date_str, mode.upper())
        n_total  = sim["n_vehicles"]; n_served = sim["n_served"]
        n_partial  = sum(1 for v, r in sim["remaining"].items()
                         if r > ENERGY_TOL and sim["delivered"].get(v, 0) > ENERGY_TOL)
        n_unserved = sum(1 for v in sim["delivered"] if sim["delivered"][v] <= ENERGY_TOL)
        e_unmet    = sum(sim["remaining"].values())
        e_demanded = sum(float(r["energy_needed_kwh_for_visit"]) for _, r in events_ext.iterrows())
        cost = cost_breakdown(sim, date_str)
        util = utilization_metrics(sim)
        results[mode] = {
            "date": date_str, "scenario": mode.upper(), "n_events": n_events,
            "K": sim["n_units"], "n_vehicles": n_total, "n_fully_served": n_served,
            "n_partial": n_partial, "n_unserved": n_unserved,
            "energy_demanded_kwh":  round(e_demanded, 1),
            "energy_delivered_kwh": round(sum(sim["delivered"].values()), 1),
            "energy_unmet_kwh":     round(e_unmet, 1),
            "service_rate_pct":     round(100 * n_served / max(n_total, 1), 1),
            "sanity_pass": sc["pass"], "sanity_issues": "; ".join(sc["details"]),
            **cost, **util,
        }
    return results


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Main simulation loop
# ══════════════════════════════════════════════════════════════════════════════

def phase1_simulate(site, site_label, out_dir, csv_stem):
    pattern   = str(BASE_DIR / f"{csv_stem}_*.csv")
    all_files = sorted(glob.glob(pattern))
    n_files   = len(all_files)
    print(f"\n{'='*70}")
    print(f"  PHASE 1 — {site_label}: simulating {n_files} days")
    print(f"{'='*70}")

    rows_sum, rows_cost, rows_san = [], [], []

    for i, fpath in enumerate(all_files, 1):
        m = re.search(r"(\d{4}_\d{2}_\d{2})\.csv$", fpath)
        if not m: continue
        date_str = m.group(1).replace("_", "-")
        pct = 100 * i / n_files
        print(f"  [{i:3d}/{n_files}] {date_str} ({pct:.0f}%)", end="  ", flush=True)

        result = run_one_day(Path(fpath), date_str, csv_stem,
                             per_day_root=out_dir / "per_day")
        if result is None:
            print("skipped"); continue
        if "error" in result:
            print(f"ERROR: {result['error']}"); continue

        for mode in ("a1", "a2"):
            if mode not in result: continue
            r = result[mode]
            print(f"{mode.upper()} K={r['K']} svc={r['n_fully_served']}/{r['n_vehicles']}",
                  end="  ", flush=True)
            rows_sum.append({k: v for k, v in r.items()
                             if k not in ("sanity_issues",)})
            rows_cost.append({"date": r["date"], "scenario": r["scenario"], "K": r["K"],
                              **{k: r.get(k, 0) for k in (
                                  "purchase_capex_daily","infra_capex_daily","maint_daily",
                                  "warranty_daily","energy_cost_daily",
                                  "demand_global_monthly_$","demand_peak_win_monthly_$",
                                  "total_daily_excl_demand","total_daily_incl_demand",
                                  "total_grid_kwh","vehicle_kwh_delivered",
                                  "peak_grid_kw","peak_win_kw",
                                  "infra_total_mid_$")}})
            rows_san.append({"date": r["date"], "scenario": r["scenario"],
                             "pass": r["sanity_pass"],
                             "n_violations": 0 if r["sanity_pass"]
                                             else r["sanity_issues"].count(":"),
                             "details": r["sanity_issues"]})
        print()

    df_sum  = pd.DataFrame(rows_sum)
    df_cost = pd.DataFrame(rows_cost)
    df_san  = pd.DataFrame(rows_san)
    df_sum .to_csv(out_dir / f"{site}_summary.csv",      index=False)
    df_cost.to_csv(out_dir / f"{site}_cost_detail.csv",  index=False)
    df_san .to_csv(out_dir / f"{site}_sanity_log.csv",   index=False)

    # Brief text report
    lines = [f"{'='*70}", f"  {site_label.upper()} XOS HUB MC02 — ANALYSIS SUMMARY",
             f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"  Days: {df_sum['date'].nunique()}  |  Scenarios: A1 + A2", ""]
    for scen in ("A1","A2"):
        ds = df_sum[df_sum["scenario"]==scen]
        dc = df_cost[df_cost["scenario"]==scen]
        if ds.empty: continue
        lines += [
            f"  --- Scenario {scen} ---",
            f"  Days analyzed: {len(ds)}",
            f"  Total vehicles: {ds['n_vehicles'].sum()}",
            f"  Fully served:   {ds['n_fully_served'].sum()} "
            f"({100*ds['n_fully_served'].sum()/max(ds['n_vehicles'].sum(),1):.1f}%)",
            f"  Partial:        {ds['n_partial'].sum()}",
            f"  Unserved:       {ds['n_unserved'].sum()}",
            f"  K (min/avg/max): {ds['K'].min()}/{ds['K'].mean():.1f}/{ds['K'].max()}",
            f"  Daily cost (excl demand) — avg: ${dc['total_daily_excl_demand'].mean():.2f}  "
            f"max: ${dc['total_daily_excl_demand'].max():.2f}",
            f"  Peak grid kW — avg: {dc['peak_grid_kw'].mean():.0f}  "
            f"max: {dc['peak_grid_kw'].max():.0f}",
            "",
        ]
    rpt = "\n".join(lines)
    (out_dir / f"{site}_analysis_report.txt").write_text(rpt, encoding="utf-8")
    print(rpt)
    print(f"  Saved: {out_dir}")
    return df_sum, df_cost


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Day-view figures
# ══════════════════════════════════════════════════════════════════════════════

def _vid_label(eid, date_str):
    parts = eid.split("_")
    dc    = parts[-2]; num = int(parts[-1][1:])
    tc    = date_str.replace("-","")
    return f"V{num}{'p' if dc < tc else ''}"


def _hub_state_blocks(state_df, hub_k, to_x):
    col = f"state_unit_{hub_k}"
    if col not in state_df.columns: return []
    times  = pd.to_datetime(state_df["time_utc"], utc=True)
    states = state_df[col].tolist()
    blocks = []; i = 0
    while i < len(states):
        s = states[i]; j = i+1
        while j < len(states) and states[j] == s: j += 1
        blocks.append((to_x(times.iloc[i]), to_x(times.iloc[j-1]) + DT_MIN, s))
        i = j
    return blocks


def _load_events_for_day(date_str, csv_stem):
    date_tag  = date_str.replace("-","_")
    csv_path  = BASE_DIR / f"{csv_stem}_{date_tag}.csv"
    if not csv_path.exists(): return None
    stem_parts    = csv_path.stem.rsplit("_", 3)
    site_csv_stem = "_".join(stem_parts[:-3]) if len(stem_parts) > 3 else csv_path.stem
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            ev = sr.load_site_day_data(csv_path)
            ev = sr.apply_multiday_rule(ev, date_str, site_csv_dir=csv_path.parent,
                                        site_csv_stem=site_csv_stem)
        return sr._xos_extended_dwell(ev)
    except Exception:
        return None


def _draw_gantt(ax, a2_d, a2_s, to_x, x_beg, x_end, tick_xs, tick_labels,
                vid_dwell, vid_color, date_str, label):
    PORT_H = 0.36; HUB_GAP = 0.22
    port_wins = {}
    if not a2_d.empty and "unit" in a2_d.columns:
        for (uk, pp_, eid), grp in a2_d.groupby(["unit","port","event_id"]):
            ts_g = grp["time_utc"].sort_values()
            t0   = ts_g.iloc[0]; t1 = ts_g.iloc[-1] + pd.Timedelta(minutes=DT_MIN)
            port_wins.setdefault((int(uk), int(pp_)), []).append((t0, t1, eid))
    act_hubs = sorted({u for u, _ in port_wins})
    if not act_hubs:
        ax.text(0.5, 0.5, "No dispatch data", ha="center", va="center",
                transform=ax.transAxes, fontsize=11, color="gray")
        ax.set_ylabel(label, fontsize=7.5); return
    y_map = {}; hub_spans = {}; hub_ly = {}; y_cur = 0.0
    for hk in act_hubs:
        y_bot = y_cur
        for p in range(NP):
            y_map[(hk, p)] = y_cur + PORT_H/2; y_cur += PORT_H
        y_top = y_cur; hub_spans[hk] = (y_bot, y_top)
        hub_ly[hk] = (y_bot + y_top)/2; y_cur += HUB_GAP
    y_total = y_cur - HUB_GAP
    for hk in act_hubs:
        y_bot, y_top = hub_spans[hk]
        for xb0, xb1, state in _hub_state_blocks(a2_s, hk, to_x):
            c, a = STATE_COLOR.get(state, ("#fff", 0))
            ax.fill_betweenx([y_bot, y_top], xb0, xb1, color=c, alpha=a, linewidth=0, zorder=1)
        for p in range(1, NP):
            ax.axhline(y_bot + p*PORT_H, color="gray", lw=0.3, ls=":", alpha=0.5, zorder=2)
        for p in range(NP):
            y_c = y_map[(hk, p)]
            for t0, t1, eid in port_wins.get((hk, p), []):
                clr = vid_color.get(eid, "steelblue")
                xv0, xv1 = to_x(t0), to_x(t1)
                if eid in vid_dwell:
                    arr_t, dep_t = vid_dwell[eid]
                    ax.barh(y_c, max(to_x(dep_t)-to_x(arr_t), 1), left=to_x(arr_t),
                            height=PORT_H*0.78, color=clr, alpha=0.18,
                            edgecolor=clr, linewidth=0.5, zorder=2)
                ax.barh(y_c, max(xv1-xv0, 1), left=xv0, height=PORT_H*0.78,
                        color=clr, alpha=0.88, edgecolor="white", lw=0.3, zorder=3)
                ax.text((xv0+xv1)/2, y_c, _vid_label(eid, date_str),
                        ha="center", va="center", fontsize=4.8, fontweight="bold",
                        color="black", clip_on=True, zorder=4)
        ax.axhline(y_top, color="#888", lw=0.6, alpha=0.5, zorder=2)
    ax.set_xlim(x_beg, x_end); ax.set_ylim(y_total+0.1, -0.1)
    ax.set_yticks([hub_ly[k] for k in act_hubs])
    ax.set_yticklabels([f"Hub {k+1}" for k in act_hubs], fontsize=7)
    ax.set_xticks(tick_xs); ax.set_xticklabels(tick_labels, fontsize=7.5, rotation=30, ha="right")
    ax.grid(axis="x", linestyle=":", alpha=0.35, color="gray")
    ax.set_ylabel(label, fontsize=7.5)
    patches = [mpatches.Patch(color=STATE_COLOR[s][0], alpha=STATE_COLOR[s][1], label=s.capitalize())
               for s in ("idle","serving","recharging")]
    patches.append(mpatches.Patch(facecolor="gray", alpha=0.22, label="Dwell window"))
    ax.legend(handles=patches, loc="upper right", fontsize=6.5, ncol=4, framealpha=0.90)


def plot_one_day(date_str, day_dir, events_ext, site_label):
    def _ldf(p):
        return pd.read_csv(p) if p.exists() else pd.DataFrame()
    a2_d = _ldf(day_dir / f"A2_dispatch_{date_str}.csv")
    a2_s = _ldf(day_dir / f"A2_state_{date_str}.csv")
    a2_g = _ldf(day_dir / f"A2_grid_draw_{date_str}.csv")
    a1_d = _ldf(day_dir / f"A1_dispatch_{date_str}.csv")
    a1_s = _ldf(day_dir / f"A1_state_{date_str}.csv")
    a1_g = _ldf(day_dir / f"A1_grid_draw_{date_str}.csv")
    if a2_d.empty and a1_d.empty: return None

    for df in (a2_d, a1_d):
        if "time_utc" in df.columns:
            df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    for df in (a2_s, a1_s):
        if "time_utc" in df.columns:
            df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    if not a2_g.empty and "time_utc" in a2_g.columns:
        a2_g["time_pac"] = pd.to_datetime(a2_g["time_utc"], utc=True).dt.tz_convert(TZ)
    if not a1_g.empty and "time_utc" in a1_g.columns:
        a1_g["time_pac"] = pd.to_datetime(a1_g["time_utc"], utc=True).dt.tz_convert(TZ)

    all_vids  = events_ext["charging_event_id"].tolist()
    vid_dwell = {r["charging_event_id"]: (pd.to_datetime(r["arrival_time"], utc=True),
                                           pd.to_datetime(r["departure_time"], utc=True))
                 for _, r in events_ext.iterrows()}
    cmap      = plt.cm.get_cmap("tab20", max(len(all_vids), 20))
    vid_color = {v: cmap(i) for i, v in enumerate(all_vids)}

    t_ref = pd.Timestamp(date_str, tz=TZ)
    def to_x(t):
        tl = t.tz_convert(TZ) if hasattr(t, "tz_convert") else t
        return (tl - t_ref).total_seconds() / 60.0

    all_times = list(events_ext["arrival_time"]) + list(events_ext["departure_time"])
    for g in (a2_g, a1_g):
        if not g.empty and "time_pac" in g.columns: all_times += list(g["time_pac"])
    t_gs  = min(pd.to_datetime(t, utc=True) for t in all_times).tz_convert(TZ).floor("1h")
    t_ge  = max(pd.to_datetime(t, utc=True) for t in all_times).tz_convert(TZ).ceil("1h")
    x_beg = (t_gs - t_ref).total_seconds() / 60.0
    x_end = (t_ge - t_ref).total_seconds() / 60.0

    t_tick = t_gs.ceil("2h"); tick_xs, tick_labels = [], []
    while t_tick <= t_ge:
        tick_xs.append((t_tick - t_ref).total_seconds() / 60.0)
        tick_labels.append(t_tick.strftime("%H:%M"))
        t_tick += pd.Timedelta(hours=2)

    PORT_H = 0.36; HUB_GAP = 0.22
    n_a2 = (int(a2_d["unit"].max()) + 1) if not a2_d.empty and "unit" in a2_d.columns else 1
    n_a1 = (int(a1_d["unit"].max()) + 1) if not a1_d.empty and "unit" in a1_d.columns else 1
    a2_h = max(n_a2 * (NP * PORT_H + HUB_GAP), 3.5)
    a1_h = max(n_a1 * (NP * PORT_H + HUB_GAP), 3.5)
    dem_h = 3.8
    leg_h = max(2.2, math.ceil(len(all_vids) / 5) * 0.38 + 0.6)
    fig_h = max(18, a2_h + a1_h + dem_h + leg_h + 2.0)

    fig, (ax1, ax2, ax3, ax4) = plt.subplots(
        4, 1, figsize=(26, fig_h),
        gridspec_kw={"height_ratios": [a2_h, a1_h, dem_h, leg_h]})
    fig.subplots_adjust(hspace=0.10, left=0.09, right=0.99, top=0.97, bottom=0.02)

    ax1.set_title(f"{site_label}  |  {date_str}  |  XOS Hub MC02  —  A1 & A2 scenarios  "
                  f"(proactive recharge enabled)", fontsize=12, fontweight="bold", pad=6)
    _draw_gantt(ax1, a2_d, a2_s, to_x, x_beg, x_end, tick_xs, tick_labels,
                vid_dwell, vid_color, date_str, f"XOS A2 — {n_a2} hubs\ndisconnect 20%")
    _draw_gantt(ax2, a1_d, a1_s, to_x, x_beg, x_end, tick_xs, tick_labels,
                vid_dwell, vid_color, date_str, f"XOS A1 — {n_a1} hubs\nalways-grid")

    if not a2_g.empty and "grid_kw" in a2_g.columns:
        xs2 = [(t - t_ref).total_seconds()/60.0 for t in a2_g["time_pac"]]
        ax3.plot(xs2, a2_g["grid_kw"], color="#d73027", lw=1.6,
                 label=f"XOS A2 (peak {int(a2_g['grid_kw'].max())} kW)")
        ax3.fill_between(xs2, a2_g["grid_kw"], alpha=0.10, color="#d73027")
    if not a1_g.empty and "grid_kw" in a1_g.columns:
        xs1 = [(t - t_ref).total_seconds()/60.0 for t in a1_g["time_pac"]]
        ax3.plot(xs1, a1_g["grid_kw"], color="#2166ac", lw=1.6, ls="--",
                 label=f"XOS A1 (peak {int(a1_g['grid_kw'].max())} kW)")
    pk0 = (t_ref + pd.Timedelta(hours=16) - t_ref).total_seconds() / 60
    pk1 = (t_ref + pd.Timedelta(hours=21) - t_ref).total_seconds() / 60
    if pk0 < x_end:
        ax3.axvspan(pk0, min(pk1, x_end), color="#fee08b", alpha=0.30, label="SMUD peak 16-21h")
    ax3.set_xlim(x_beg, x_end)
    y3_max = max((a2_g["grid_kw"].max() if not a2_g.empty and "grid_kw" in a2_g.columns else 0),
                 (a1_g["grid_kw"].max() if not a1_g.empty and "grid_kw" in a1_g.columns else 0), 1) * 1.15
    ax3.set_ylim(0, y3_max)
    ax3.set_xticks(tick_xs); ax3.set_xticklabels(tick_labels, fontsize=8.5, rotation=30, ha="right")
    ax3.set_ylabel("Grid draw (kW)", fontsize=9)
    ax3.set_xlabel(f"Time (Pacific)  —  {date_str}", fontsize=9)
    ax3.legend(loc="upper left", fontsize=8, framealpha=0.92)
    ax3.grid(axis="both", linestyle=":", alpha=0.30)
    ax3.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))

    ax4.axis("off")
    ax4.set_title("Vehicle legend  (solid=charging, light=dwell window)",
                  fontsize=8.5, fontweight="bold", pad=3, loc="left")
    vid_model = {r["charging_event_id"]: str(r.get("ev_equivalent_model","") or "")
                 for _, r in events_ext.iterrows()}
    lrows = sorted([(v.endswith("p"), int(v.rstrip("p")[1:]) if v.rstrip("p")[1:].isdigit() else 999,
                     v, vid_model.get(e,""), vid_color[e])
                    for e, v in ((eid, _vid_label(eid, date_str)) for eid in all_vids)])
    N_COLS = 5; PATCH_W = 0.024; PATCH_H = 0.052; n_rows = max(1, math.ceil(len(lrows)/N_COLS))
    COL_W = 1.0 / N_COLS
    for idx, (_, _, lbl, model, color) in enumerate(lrows):
        col = idx % N_COLS; row_i = idx // N_COLS
        x0  = col * COL_W + 0.005; y0 = 1.0 - (row_i+1)*(1.0/(n_rows+0.5))
        ax4.add_patch(mpatches.FancyBboxPatch((x0, y0), PATCH_W, PATCH_H,
            boxstyle="round,pad=0.002", facecolor=color, edgecolor="none",
            transform=ax4.transAxes, clip_on=True, zorder=3))
        ax4.text(x0+PATCH_W+0.007, y0+PATCH_H/2, f"{lbl}: {model}",
                 ha="left", va="center", fontsize=7.0, transform=ax4.transAxes, clip_on=True)

    out = day_dir / f"day_view_{date_str}.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


def phase2_figures(site, site_label, out_dir, csv_stem):
    per_day = out_dir / "per_day"
    day_dirs = sorted(per_day.iterdir()) if per_day.exists() else []
    n = len(day_dirs)
    print(f"\n{'='*70}")
    print(f"  PHASE 2 — {site_label}: generating {n} day-view figures")
    print(f"{'='*70}")
    ok = skip = fail = 0
    for i, day_dir in enumerate(day_dirs, 1):
        date_str = day_dir.name
        if not day_dir.is_dir(): continue
        print(f"  [{i:3d}/{n}] {date_str}", end="  ", flush=True)
        events_ext = _load_events_for_day(date_str, csv_stem)
        if events_ext is None or events_ext.empty:
            print("skipped"); skip += 1; continue
        try:
            out = plot_one_day(date_str, day_dir, events_ext, site_label)
            if out: print(f"saved"); ok += 1
            else:   print("skipped"); skip += 1
        except Exception as e:
            print(f"ERROR: {e}"); fail += 1
    print(f"\n  Done. Saved={ok}  Skipped={skip}  Errors={fail}")


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — Worst-day analysis + Shima K-sweep
# ══════════════════════════════════════════════════════════════════════════════

def _run_fixed_k_sim(events_ext, K_fixed, mode="a2"):
    if events_ext is None or events_ext.empty: return None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            sim = sr._simulate_xos(events_ext, K_fixed, mode=mode)
        delivered = sim["delivered"]; remaining = sim["remaining"]
        n_full    = sum(1 for v in delivered if remaining.get(v, 0) <= ENERGY_TOL)
        n_partial = sum(1 for v in delivered if delivered[v] > ENERGY_TOL and remaining.get(v, 0) > ENERGY_TOL)
        n_unserved= sum(1 for v in delivered if delivered[v] <= ENERGY_TOL)
        e_del  = sum(delivered.values())
        e_unmet= sum(r for r in remaining.values() if r > ENERGY_TOL)
        return {"n_full": n_full, "n_partial": n_partial, "n_unserved": n_unserved,
                "n_veh": sim["n_vehicles"], "e_del": e_del, "e_dem": e_del + e_unmet}
    except Exception:
        return None


def _plot_report_card(row, rank, coverage, events_ext, out_dir, site_label):
    date_str = row["date"]
    day_dir  = out_dir.parent / "per_day" / date_str

    def _ldf(p): return pd.read_csv(p) if p.exists() else pd.DataFrame()
    a2_d = _ldf(day_dir / f"A2_dispatch_{date_str}.csv")
    a2_s = _ldf(day_dir / f"A2_state_{date_str}.csv")
    a2_g = _ldf(day_dir / f"A2_grid_draw_{date_str}.csv")
    a2_v = _ldf(day_dir / f"A2_vehicle_results_{date_str}.csv")

    for df in (a2_d,):
        if "time_utc" in df.columns: df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    if not a2_s.empty and "time_utc" in a2_s.columns:
        a2_s["time_utc"] = pd.to_datetime(a2_s["time_utc"], utc=True)
    if not a2_g.empty and "time_utc" in a2_g.columns:
        a2_g["time_pac"] = pd.to_datetime(a2_g["time_utc"], utc=True).dt.tz_convert(TZ)

    all_vids  = events_ext["charging_event_id"].tolist()
    vid_dwell = {r["charging_event_id"]: (pd.to_datetime(r["arrival_time"], utc=True),
                                           pd.to_datetime(r["departure_time"], utc=True))
                 for _, r in events_ext.iterrows()}
    vid_model = {r["charging_event_id"]: str(r.get("ev_equivalent_model","") or "")
                 for _, r in events_ext.iterrows()}
    cmap      = plt.cm.get_cmap("tab20", max(len(all_vids), 20))
    vid_color = {v: cmap(i) for i, v in enumerate(all_vids)}

    t_ref = pd.Timestamp(date_str, tz=TZ)
    def to_x(t):
        tl = t.tz_convert(TZ) if hasattr(t, "tz_convert") else t
        return (tl - t_ref).total_seconds() / 60.0

    all_times = list(events_ext["arrival_time"]) + list(events_ext["departure_time"])
    if not a2_g.empty and "time_pac" in a2_g.columns: all_times += list(a2_g["time_pac"])
    t_gs  = min(pd.to_datetime(t, utc=True) for t in all_times).tz_convert(TZ).floor("1h")
    t_ge  = max(pd.to_datetime(t, utc=True) for t in all_times).tz_convert(TZ).ceil("1h")
    x_beg = (t_gs - t_ref).total_seconds() / 60.0
    x_end = (t_ge - t_ref).total_seconds() / 60.0
    t_tick = t_gs.ceil("2h"); tick_xs, tick_labels = [], []
    while t_tick <= t_ge:
        tick_xs.append((t_tick - t_ref).total_seconds() / 60.0)
        tick_labels.append(t_tick.strftime("%H:%M"))
        t_tick += pd.Timedelta(hours=2)

    PORT_H = 0.36; HUB_GAP = 0.22
    n_hubs = int(row["K"])
    port_wins = {}
    if not a2_d.empty and "unit" in a2_d.columns:
        for (uk, pp_, eid), grp in a2_d.groupby(["unit","port","event_id"]):
            ts_g = grp["time_utc"].sort_values()
            port_wins.setdefault((int(uk), int(pp_)), []).append(
                (ts_g.iloc[0], ts_g.iloc[-1]+pd.Timedelta(minutes=DT_MIN), eid))
    act_hubs = sorted({u for u, _ in port_wins})
    gantt_h = max((len(act_hubs) or n_hubs) * (NP*PORT_H+HUB_GAP), 3.5)
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

    ax_info.axis("off")
    dow = pd.Timestamp(date_str).strftime("%A")
    n_days_coverage = coverage["days_fully_covered"] + coverage["days_partial"] + coverage["days_uncovered"]
    info = [
        (f"Rank #{rank} Worst Day — {site_label}  |  {date_str}  ({dow})", 14, "bold", "black"),
        (f"Scenario: A2 (disconnect at 20% SOC, proactive recharge)", 10, "normal", "#333"),
        ("", 6, "normal", "white"),
        (f"XOS Hubs: {int(row['K'])}   |   Vehicles: {int(row['n_vehicles'])} total  "
         f"({int(row['n_fully_served'])} full / {int(row['n_partial'])} partial / {int(row['n_unserved'])} unserved)  "
         f"Svc={float(row['service_rate_pct']):.1f}%", 10.5, "bold", "#1a1a7a"),
        (f"Daily cost: ${float(row['total_daily_excl_demand']):,.2f} excl demand  |  "
         f"${float(row['total_daily_incl_demand']):,.2f} incl demand  |  "
         f"Peak: {float(row['peak_grid_kw']):,.0f} kW", 10.5, "normal", "#333"),
        (f"Energy demanded: {float(row['energy_demanded_kwh']):,.1f} kWh  |  "
         f"Delivered: {float(row['energy_delivered_kwh']):,.1f} kWh  |  "
         f"Unmet: {float(row['energy_unmet_kwh']):,.1f} kWh", 10.5, "normal", "#333"),
        ("", 6, "normal", "white"),
        (f"[Shima]  K={int(row['K'])} fixed for all {n_days_coverage} days:  "
         f"{coverage['days_fully_covered']} fully served ({coverage['pct_fully_covered']:.1f}%)  |  "
         f"Partial: {coverage['days_partial']}  |  Uncov: {coverage['days_uncovered']}  |  "
         f"Energy svc: {coverage['overall_energy_svc_pct']:.1f}%", 10, "bold", "#7a1a1a"),
    ]
    y = 0.96
    for text, fs, fw, fc in info:
        ax_info.text(0.01, y, text, transform=ax_info.transAxes,
                     fontsize=fs, fontweight=fw, color=fc, va="top")
        y -= (fs + 4) / (fig_h * 10)

    # Gantt
    y_map = {}; hub_spans = {}; hub_ly = {}; y_cur = 0.0
    for hk in act_hubs:
        y_bot = y_cur
        for p in range(NP): y_map[(hk,p)] = y_cur + PORT_H/2; y_cur += PORT_H
        y_top = y_cur; hub_spans[hk]=(y_bot,y_top); hub_ly[hk]=(y_bot+y_top)/2; y_cur += HUB_GAP
    y_total = y_cur - HUB_GAP if act_hubs else 1.0
    for hk in act_hubs:
        y_bot, y_top = hub_spans[hk]
        for xb0, xb1, state in _hub_state_blocks(a2_s, hk, to_x):
            c, a = STATE_COLOR.get(state, ("#fff",0))
            ax_gantt.fill_betweenx([y_bot,y_top], xb0, xb1, color=c, alpha=a, lw=0, zorder=1)
        for p in range(1, NP):
            ax_gantt.axhline(y_bot+p*PORT_H, color="gray", lw=0.3, ls=":", alpha=0.5, zorder=2)
        for p in range(NP):
            y_c = y_map[(hk, p)]
            for t0, t1, eid in port_wins.get((hk,p), []):
                clr = vid_color.get(eid, "steelblue")
                xv0, xv1 = to_x(t0), to_x(t1)
                if eid in vid_dwell:
                    arr_t, dep_t = vid_dwell[eid]
                    ax_gantt.barh(y_c, max(to_x(dep_t)-to_x(arr_t),1), left=to_x(arr_t),
                                   height=PORT_H*0.78, color=clr, alpha=0.18, edgecolor=clr, lw=0.5, zorder=2)
                ax_gantt.barh(y_c, max(xv1-xv0,1), left=xv0, height=PORT_H*0.78,
                               color=clr, alpha=0.88, edgecolor="white", lw=0.3, zorder=3)
                ax_gantt.text((xv0+xv1)/2, y_c, _vid_label(eid, date_str),
                               ha="center", va="center", fontsize=5.5, fontweight="bold", zorder=4)
        ax_gantt.axhline(y_top, color="#888", lw=0.6, alpha=0.5, zorder=2)
    ax_gantt.set_xlim(x_beg, x_end); ax_gantt.set_ylim(y_total+0.1, -0.1)
    ax_gantt.set_yticks([hub_ly[k] for k in act_hubs])
    ax_gantt.set_yticklabels([f"Hub {k+1}" for k in act_hubs], fontsize=7.5)
    ax_gantt.set_xticks(tick_xs); ax_gantt.set_xticklabels(tick_labels, fontsize=8, rotation=30, ha="right")
    ax_gantt.set_title(f"XOS A2 Charging Schedule  (K={n_hubs} hubs)", fontsize=10, fontweight="bold")
    ax_gantt.grid(axis="x", ls=":", alpha=0.3)
    patches = [mpatches.Patch(color=STATE_COLOR[s][0], alpha=STATE_COLOR[s][1], label=s.capitalize())
               for s in ("idle","serving","recharging")]
    patches.append(mpatches.Patch(facecolor="gray", alpha=0.22, label="Dwell"))
    ax_gantt.legend(handles=patches, loc="upper right", fontsize=7, ncol=4, framealpha=0.90)

    # Power
    if not a2_g.empty and "grid_kw" in a2_g.columns:
        xs2 = [(t-t_ref).total_seconds()/60.0 for t in a2_g["time_pac"]]
        ax_power.plot(xs2, a2_g["grid_kw"], color="#d73027", lw=2.0, label="XOS A2")
        ax_power.fill_between(xs2, a2_g["grid_kw"], alpha=0.12, color="#d73027")
    pk0 = (t_ref+pd.Timedelta(hours=16)-t_ref).total_seconds()/60
    pk1 = (t_ref+pd.Timedelta(hours=21)-t_ref).total_seconds()/60
    if pk0 < x_end:
        ax_power.axvspan(pk0, min(pk1, x_end), color="#fee08b", alpha=0.35, label="SMUD peak")
    ax_power.set_xlim(x_beg, x_end); ax_power.set_xticks(tick_xs)
    ax_power.set_xticklabels(tick_labels, fontsize=7.5, rotation=30, ha="right")
    ax_power.set_ylabel("Grid draw (kW)", fontsize=9); ax_power.set_title("Site Grid Power Demand", fontsize=10, fontweight="bold")
    ax_power.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax_power.legend(loc="upper left", fontsize=8); ax_power.grid(ls=":", alpha=0.3)

    # Energy bar chart
    if not a2_v.empty and "energy_needed_kwh" in a2_v.columns:
        a2_v2 = a2_v.sort_values("event_id")
        labels = [_vid_label(e, date_str) for e in a2_v2["event_id"]]
        needed = a2_v2["energy_needed_kwh"].values; deliv = a2_v2["energy_delivered_kwh"].values
        ys     = np.arange(len(labels))
        colors_e = [vid_color.get(e, "steelblue") for e in a2_v2["event_id"]]
        ax_nrg.barh(ys, needed, 0.6, color="lightgray", edgecolor="gray", lw=0.5, label="Needed")
        ax_nrg.barh(ys, deliv,  0.6, color=colors_e, alpha=0.85, label="Delivered")
        ax_nrg.set_yticks(ys); ax_nrg.set_yticklabels(labels, fontsize=6.5)
        ax_nrg.set_xlabel("Energy (kWh)", fontsize=9); ax_nrg.invert_yaxis()
        ax_nrg.set_title("Vehicle Energy: Needed vs Delivered", fontsize=10, fontweight="bold")
        ax_nrg.legend(loc="lower right", fontsize=8); ax_nrg.grid(axis="x", ls=":", alpha=0.35)
    else:
        ax_nrg.axis("off")

    # Legend
    ax_leg.axis("off"); ax_leg.set_title("Vehicle legend", fontsize=9, fontweight="bold", pad=2, loc="left")
    lrows = sorted([(v.endswith("p"), int(v.rstrip("p")[1:]) if v.rstrip("p")[1:].isdigit() else 999,
                     v, vid_model.get(e,""), vid_color[e])
                    for e, v in ((eid, _vid_label(eid, date_str)) for eid in all_vids)])
    N_COLS=6; PATCH_W=0.018; PATCH_H=0.065; n_rows=max(1,math.ceil(len(lrows)/N_COLS)); COL_W=1.0/N_COLS
    for idx, (_,_,lbl,model,color) in enumerate(lrows):
        col=idx%N_COLS; ri=idx//N_COLS
        x0=col*COL_W+0.004; y0=1.0-(ri+1)*(1.0/(n_rows+0.5))
        ax_leg.add_patch(mpatches.FancyBboxPatch((x0,y0),PATCH_W,PATCH_H,boxstyle="round,pad=0.002",
            facecolor=color, edgecolor="none", transform=ax_leg.transAxes, clip_on=True, zorder=3))
        ax_leg.text(x0+PATCH_W+0.005, y0+PATCH_H/2, f"{lbl}: {model}", ha="left", va="center",
                    fontsize=7, transform=ax_leg.transAxes, clip_on=True)

    out = out_dir / f"worst_day_rank{rank:02d}_{date_str}.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


def phase3_worst_days(site, site_label, out_dir, csv_stem):
    worst_dir = out_dir / "worst_days"
    worst_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{'='*70}")
    print(f"  PHASE 3 — {site_label}: worst-day analysis + Shima K-sweep")
    print(f"{'='*70}")

    df_sum = pd.read_csv(out_dir / f"{site}_summary.csv")
    a2     = df_sum[df_sum["scenario"] == "A2"].copy()
    top10  = a2.sort_values(["K","n_vehicles"], ascending=False).head(10).reset_index(drop=True)
    top10_rows = top10.to_dict("records")

    print("\nTop 10 worst days:")
    for i, r in enumerate(top10_rows, 1):
        print(f"  {i:2d}. {r['date']}  K={r['K']:2d}  Veh={r['n_vehicles']:2d}  "
              f"Svc={r['service_rate_pct']:.0f}%  Cost=${r['total_daily_excl_demand']:,.0f}")

    # Load all events once
    all_csv = sorted(BASE_DIR.glob(f"{csv_stem}_*.csv"))
    print(f"\nLoading events for {len(all_csv)} days...")
    all_events = {}
    for csv_path in all_csv:
        date_tag = csv_path.stem.split(f"{site}_")[-1]
        d_str    = date_tag.replace("_","-")[:10]
        stem_pts = csv_path.stem.rsplit("_", 3)
        site_stem= "_".join(stem_pts[:-3]) if len(stem_pts) > 3 else csv_path.stem
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ev = sr.load_site_day_data(csv_path)
                ev = sr.apply_multiday_rule(ev, d_str, site_csv_dir=csv_path.parent,
                                             site_csv_stem=site_stem)
                ev = sr._xos_extended_dwell(ev)
            all_events[d_str] = ev
        except Exception:
            pass
    n_days = len(all_events)
    print(f"Loaded {n_days} days.")

    # Shima coverage: top-10 worst-day K values
    print(f"\nShima coverage (10 K values × {n_days} days):")
    cov_results = []
    for rank, row in enumerate(top10_rows, 1):
        K_fixed = int(row["K"])
        print(f"  Rank {rank:2d} | K={K_fixed:2d} ...", end=" ", flush=True)
        fully = partial = uncov = e_del = e_dem = 0
        for d_str, ev in all_events.items():
            r = _run_fixed_k_sim(ev, K_fixed)
            if r is None: continue
            if r["n_unserved"] + r["n_partial"] == 0: fully += 1
            elif r["n_unserved"] > 0:                 uncov += 1
            else:                                     partial += 1
            e_del += r["e_del"]; e_dem += r["e_dem"]
        pct_f = 100*fully/n_days if n_days else 0
        esvc  = 100*e_del/e_dem  if e_dem  else 0
        cov_results.append({"rank": rank, "worst_date": row["date"], "K_fixed": K_fixed,
                             "n_vehicles_worst": int(row["n_vehicles"]),
                             "days_fully_covered": fully, "days_partial": partial,
                             "days_uncovered": uncov, "pct_fully_covered": round(pct_f, 1),
                             "pct_partial": round(100*partial/n_days if n_days else 0, 1),
                             "overall_energy_svc_pct": round(esvc, 1)})
        print(f"→ {fully}/{n_days} ({pct_f:.1f}%)")

    pd.DataFrame(cov_results).to_csv(worst_dir / "coverage_analysis.csv", index=False)

    # K-sweep K=1..20
    print(f"\nK-sweep K=1..20 across {n_days} days...")
    sweep_rows = []
    for K in range(1, 21):
        fully = partial = uncov = e_del = e_dem = 0
        for ev in all_events.values():
            r = _run_fixed_k_sim(ev, K)
            if r is None: continue
            if r["n_unserved"]+r["n_partial"] == 0: fully += 1
            elif r["n_unserved"] > 0:               uncov += 1
            else:                                   partial += 1
            e_del += r["e_del"]; e_dem += r["e_dem"]
        pct_f = 100*fully/n_days if n_days else 0
        esvc  = 100*e_del/e_dem  if e_dem  else 0
        sweep_rows.append({"K": K, "days_fully_covered": fully, "days_partial": partial,
                            "days_uncovered": uncov, "pct_fully_covered": round(pct_f,1),
                            "pct_partial": round(100*partial/n_days if n_days else 0, 1),
                            "overall_energy_svc_pct": round(esvc,1)})
        print(f"  K={K:2d}: full={fully:3d} ({pct_f:.0f}%)  partial={partial}  uncov={uncov}  esvc={esvc:.1f}%")

    df_sweep = pd.DataFrame(sweep_rows)
    df_sweep.to_csv(worst_dir / "k_sweep_coverage.csv", index=False)

    # K-sweep figure
    K_RANGE = list(range(1, 21))
    fig2, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 9))
    fig2.suptitle(f"{site_label} — XOS Hub Coverage vs Fleet Size (K)\n"
                  f"A2 scenario, proactive recharge, {n_days} operating days",
                  fontsize=13, fontweight="bold")
    ax1.bar(df_sweep["K"]-0.25, df_sweep["days_fully_covered"], 0.5,
            label="Days 100% served", color="#2166ac", alpha=0.85)
    ax1.bar(df_sweep["K"]+0.25, df_sweep["days_partial"], 0.5,
            label="Days partial", color="#f4a582", alpha=0.85)
    for kk, lbl, clr in [(int(row["K"]), f"Worst {i}", c)
                          for i, (row, c) in enumerate(
                              zip(top10_rows[:4], ["#d73027","#ff7f00","#984ea3","#4dac26"]), 1)]:
        ax1.axvline(kk, color=clr, lw=1.8, ls="--", alpha=0.70)
    ax1.set_xticks(K_RANGE); ax1.set_ylabel(f"Days (out of {n_days})", fontsize=10)
    ax1.set_title(f"Days fully vs partially served per K ({site_label})", fontsize=11)
    ax1.legend(loc="lower right", fontsize=9); ax1.grid(axis="y", ls=":", alpha=0.4)
    ax2.plot(df_sweep["K"], df_sweep["pct_fully_covered"],
             color="#2166ac", lw=2.5, marker="o", ms=6, label="Days 100% served (%)")
    ax2.plot(df_sweep["K"], df_sweep["overall_energy_svc_pct"],
             color="#d73027", lw=2.0, marker="s", ms=5, ls="--", label="Overall energy svc (%)")
    ax2.axhline(100, color="gray", lw=0.8, ls=":")
    ax2.set_xticks(K_RANGE)
    ax2.set_xlabel("Number of XOS Hub MC02 units (K)", fontsize=10)
    ax2.set_ylabel("Coverage (%)", fontsize=10)
    ceil_k = df_sweep[df_sweep["pct_fully_covered"]==df_sweep["pct_fully_covered"].max()]["K"].min()
    ax2.set_title(f"Coverage plateau above K={ceil_k} — dwell-window ceiling", fontsize=11)
    ax2.legend(loc="lower right", fontsize=9); ax2.grid(ls=":", alpha=0.4)
    ax2.set_ylim(0, 105)
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    fig2.tight_layout()
    fig2.savefig(worst_dir / "k_sweep_coverage.png", dpi=150, bbox_inches="tight")
    plt.close(fig2)
    print(f"  Saved: k_sweep_coverage.png  (ceiling at K={ceil_k})")

    # Report cards for top 10
    print(f"\nGenerating report card figures...")
    for rank, (row, cov) in enumerate(zip(top10_rows, cov_results), 1):
        date_str = row["date"]
        print(f"  [{rank:2d}/10] {date_str} ...", end=" ", flush=True)
        ev = all_events.get(date_str)
        if ev is None or ev.empty: print("skipped"); continue
        try:
            out = _plot_report_card(row, rank, cov, ev, worst_dir, site_label)
            print(f"saved")
        except Exception as e:
            print(f"ERROR: {e}")

    # Text report
    lines = [f"{'='*80}",
             f"  {site_label.upper()} — TOP 10 WORST DAYS + SHIMA COVERAGE ANALYSIS",
             f"{'='*80}", ""]
    for cov, row in zip(cov_results, top10_rows):
        dow = pd.Timestamp(row["date"]).strftime("%A")
        lines += [
            f"  RANK #{cov['rank']}  |  {row['date']}  ({dow})",
            f"  {'─'*70}",
            f"  Site: {site_label}  |  K={int(row['K'])} XOS Hub MC02 units",
            f"  Vehicles: {int(row['n_vehicles'])} total  |  "
            f"{int(row['n_fully_served'])} fully served  |  "
            f"{int(row['n_partial'])} partial  |  {int(row['n_unserved'])} unserved",
            f"  Service rate:   {float(row['service_rate_pct']):.1f}%",
            f"  Peak grid draw: {float(row['peak_grid_kw']):,.0f} kW",
            f"  Daily cost:     ${float(row['total_daily_excl_demand']):,.2f} (excl demand)  "
            f"  ${float(row['total_daily_incl_demand']):,.2f} (incl demand)",
            f"  Energy:         {float(row['energy_demanded_kwh']):,.1f} kWh demanded  "
            f"|  {float(row['energy_delivered_kwh']):,.1f} delivered  "
            f"|  {float(row['energy_unmet_kwh']):,.1f} unmet",
            f"  [Shima] K={int(row['K'])} fixed: "
            f"{cov['days_fully_covered']}/{n_days} days 100% ({cov['pct_fully_covered']:.1f}%)  "
            f"partial={cov['days_partial']}  uncov={cov['days_uncovered']}  "
            f"energy_svc={cov['overall_energy_svc_pct']:.1f}%", ""]
    lines += [f"{'='*80}", "  COVERAGE SUMMARY TABLE", f"{'='*80}",
              f"  {'Rank':>4}  {'Date':>12}  {'K':>3}  {'Veh':>4}  "
              f"{'Days100%':>8}  {'Pct':>6}  {'Partial':>8}  {'Uncov':>6}  {'EnerSvc':>8}"]
    for cov in cov_results:
        lines.append(f"  {cov['rank']:>4}  {cov['worst_date']:>12}  {cov['K_fixed']:>3}  "
                     f"{cov['n_vehicles_worst']:>4}  {cov['days_fully_covered']:>8}  "
                     f"{cov['pct_fully_covered']:>6.1f}  {cov['days_partial']:>8}  "
                     f"{cov['days_uncovered']:>6}  {cov['overall_energy_svc_pct']:>8.1f}%")
    lines += ["", f"  K-sweep coverage ceiling: K={ceil_k} ({df_sweep[df_sweep['K']==ceil_k]['pct_fully_covered'].values[0]:.1f}% days 100% served)", f"{'='*80}"]
    rpt = "\n".join(lines)
    (worst_dir / "worst_days_report.txt").write_text(rpt, encoding="utf-8")
    print(f"\n{rpt}")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def run_pipeline(site: str):
    if site not in SITE_LABELS:
        print(f"Unknown site '{site}'. Choose from: {list(SITE_LABELS.keys())}")
        sys.exit(1)
    site_label = SITE_LABELS[site]
    csv_stem   = f"z2z_milp_events_{site}"
    out_dir    = BASE_DIR / "scenario_outputs" / f"{site}_analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'#'*70}")
    print(f"  SITE PIPELINE: {site_label}  ({csv_stem}_*.csv)")
    print(f"  Output: {out_dir}")
    print(f"{'#'*70}")

    phase1_simulate(site, site_label, out_dir, csv_stem)
    phase2_figures(site, site_label, out_dir, csv_stem)
    phase3_worst_days(site, site_label, out_dir, csv_stem)

    print(f"\n{'#'*70}")
    print(f"  COMPLETE: {site_label}")
    print(f"{'#'*70}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_site_pipeline.py <site>")
        print(f"Sites: {list(SITE_LABELS.keys())}")
        sys.exit(1)
    run_pipeline(sys.argv[1].lower())
