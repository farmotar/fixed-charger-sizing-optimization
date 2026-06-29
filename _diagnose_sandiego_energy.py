import pandas as pd, sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
OUT = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test\scenario_outputs")

sites = [("northgate","Northgate"),("fresno","Fresno"),
         ("glendale","Glendale"),("san_diego","San Diego")]

print("\n=== SITE-LEVEL ENERGY COMPARISON (A2) ===")
hdr = ("Site", "Days", "Veh/day", "kWh demanded/day", "kWh/vehicle", "Peak kW avg", "K avg")
print(f"{hdr[0]:<12} {hdr[1]:>6} {hdr[2]:>9} {hdr[3]:>18} {hdr[4]:>13} {hdr[5]:>13} {hdr[6]:>7}")
print("-"*82)
for site, label in sites:
    dc  = pd.read_csv(OUT / f"{site}_analysis/{site}_cost_detail.csv")
    sm  = pd.read_csv(OUT / f"{site}_analysis/{site}_summary.csv")
    a2c = dc[dc.scenario=="A2"]
    a2s = sm[sm.scenario=="A2"]
    days     = len(a2s)
    veh_day  = a2s.n_vehicles.mean()
    edem_day = a2s.energy_demanded_kwh.mean() if "energy_demanded_kwh" in a2s.columns else 0
    kwh_veh  = a2s.energy_demanded_kwh.sum() / max(a2s.n_vehicles.sum(),1) if "energy_demanded_kwh" in a2s.columns else 0
    peak_avg = a2c.peak_grid_kw.mean()
    k_avg    = a2s.K.mean()
    print(f"{label:<12} {days:>6} {veh_day:>9.1f} {edem_day:>18.1f} {kwh_veh:>13.1f} {peak_avg:>13.0f} {k_avg:>7.1f}")

# Now look at per-vehicle energy demand - vehicle model breakdown
print("\n\n=== PER-VEHICLE ENERGY DEMAND — sample days (worst day per site, A2) ===")
for site, label in sites:
    sm = pd.read_csv(OUT / f"{site}_analysis/{site}_summary.csv")
    a2 = sm[sm.scenario=="A2"].sort_values("energy_demanded_kwh", ascending=False)
    worst = a2.iloc[0]
    date  = worst["date"]
    K     = int(worst["K"])
    n_v   = int(worst["n_vehicles"])
    e_dem = float(worst["energy_demanded_kwh"])
    e_del = float(worst["energy_delivered_kwh"])
    print(f"\n{label} worst day: {date}  K={K}  vehicles={n_v}  "
          f"demanded={e_dem:,.0f} kWh  ({e_dem/n_v:.1f} kWh/veh)  delivered={e_del:,.0f} kWh")

    # load per-vehicle CSV if available
    veh_csv = OUT / f"{site}_analysis/per_day/{date}/A2_vehicle_results_{date}.csv"
    if veh_csv.exists():
        vdf = pd.read_csv(veh_csv)
        print(f"  Per-vehicle energy_needed_kwh stats:")
        print(f"    min={vdf.energy_needed_kwh.min():.1f}  mean={vdf.energy_needed_kwh.mean():.1f}  "
              f"max={vdf.energy_needed_kwh.max():.1f}  total={vdf.energy_needed_kwh.sum():.1f}")
        print(f"  Top models by energy need:")
        if "model" in vdf.columns:
            mv = vdf.groupby("model")["energy_needed_kwh"].agg(["mean","count"]).sort_values("mean",ascending=False)
            print(mv.head(5).to_string())

# Compare average kWh/vehicle across all days
print("\n\n=== AVERAGE kWh/VEHICLE ACROSS ALL DAYS (A2) ===")
for site, label in sites:
    sm  = pd.read_csv(OUT / f"{site}_analysis/{site}_summary.csv")
    a2  = sm[sm.scenario=="A2"]
    if "energy_demanded_kwh" in a2.columns and "n_vehicles" in a2.columns:
        kwh_per_veh = (a2.energy_demanded_kwh / a2.n_vehicles.replace(0,1))
        print(f"{label:<12}  kWh/veh: min={kwh_per_veh.min():.1f}  "
              f"mean={kwh_per_veh.mean():.1f}  max={kwh_per_veh.max():.1f}  "
              f"veh/day mean={a2.n_vehicles.mean():.1f}")
