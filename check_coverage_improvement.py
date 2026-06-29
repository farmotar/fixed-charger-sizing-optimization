"""Quick before/after comparison of event coverage after EV mapping fix."""
import sys, re, difflib, openpyxl
from pathlib import Path
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
EV_XLSX = Path(r"D:\Geotab_EV_Parameters\final_categories.xlsx")

# Import the new match logic from the updated script
sys.path.insert(0, str(BASE))
import importlib
rs = importlib.import_module("run_site_full_year")

ice_to_ev    = rs._load_ev_equivalencies(rs.EV_CATEGORIES_XLSX)
ft_lookup    = rs._load_final_table_lookup(rs.EV_CATEGORIES_XLSX)

SITES = {
    "northgate": {"cache": "_northgate_z2z_cache.csv",
                  "master": str(Path(r"D:\Geotab_EV_Parameters\northgate_vehicle_master.csv"))},
    "fresno":    {"cache": "_fresno_z2z_cache.csv"},
    "glendale":  {"cache": "_glendale_z2z_cache.csv"},
    "san_diego": {"cache": "_san_diego_z2z_cache.csv"},
}

W = 105
print(f"\n{'='*W}")
print("  EV MAPPING COVERAGE — BEFORE vs AFTER (new Tier 0 + Tier 2.5 lookups)")
print(f"{'='*W}")

for slug, cfg in SITES.items():
    cache_path = BASE / cfg["cache"]
    z2z = pd.read_csv(str(cache_path), low_memory=False)
    z2z_f = z2z[z2z["use_for_optimization_bool"].fillna(False).astype(bool)].copy()
    z2z_f["make"]  = z2z_f["make"].fillna("").astype(str)
    z2z_f["model"] = z2z_f["model"].fillna("").astype(str)
    total_events = len(z2z_f)

    # Load master if available
    master_file = cfg.get("master", "")
    master_path = Path(master_file) if master_file else None
    master = rs._load_vehicle_master(master_path) if (master_path and master_path.exists()) else {}

    # Apply OLD matching (no master, no ft_lookup)
    veh = z2z_f[["vehicle_name","make","model"]].drop_duplicates("vehicle_name").reset_index(drop=True)
    veh["ev_old"] = veh.apply(lambda r: rs._match_ev(r["make"], r["model"], ice_to_ev), axis=1)
    veh["ev_new"] = veh.apply(lambda r: rs._match_ev(
        r["make"], r["model"], ice_to_ev,
        vehicle_name=r["vehicle_name"],
        master_lookup=master,
        ft_lookup=ft_lookup), axis=1)

    ev_counts = z2z_f.groupby("vehicle_name").size().rename("n")
    veh2 = veh.join(ev_counts, on="vehicle_name").fillna({"n": 0})

    events_old = z2z_f[z2z_f["vehicle_name"].isin(set(veh2[veh2["ev_old"].notna()]["vehicle_name"]))].shape[0]
    events_new = z2z_f[z2z_f["vehicle_name"].isin(set(veh2[veh2["ev_new"].notna()]["vehicle_name"]))].shape[0]

    newly_mapped = veh2[(veh2["ev_old"].isna()) & (veh2["ev_new"].notna())]
    still_missing = veh2[veh2["ev_new"].isna()]

    print(f"\n  {slug.upper()}")
    print(f"    Total events in Z2Z cache : {total_events:,}")
    print(f"    OLD coverage              : {events_old:,} / {total_events:,}  "
          f"({100*events_old/max(total_events,1):.1f}%)")
    print(f"    NEW coverage              : {events_new:,} / {total_events:,}  "
          f"({100*events_new/max(total_events,1):.1f}%)  "
          f"[+{events_new-events_old:,} events]")
    print(f"    Newly mapped vehicles     : {len(newly_mapped)}")

    if not newly_mapped.empty:
        newly_mapped2 = newly_mapped.sort_values("n", ascending=False)
        print(f"    {'vehicle_name':<15} {'make':<20} {'model':<30} {'events':>7}  {'→ EV equivalent'}")
        print(f"    {'-'*100}")
        for _, r in newly_mapped2.head(15).iterrows():
            print(f"    {str(r['vehicle_name']):<15} {str(r['make'])[:19]:<20} "
                  f"{str(r['model'])[:29]:<30} {int(r['n']):>7}  → {r['ev_new']}")

    if not still_missing.empty:
        missing2 = still_missing.sort_values("n", ascending=False)
        print(f"\n    Still unmapped ({len(still_missing)} vehicles):")
        print(f"    {'vehicle_name':<15} {'make':<20} {'model':<30} {'events':>7}")
        print(f"    {'-'*75}")
        for _, r in missing2[missing2["n"] > 0].head(12).iterrows():
            print(f"    {str(r['vehicle_name']):<15} {str(r['make'])[:19]:<20} "
                  f"{str(r['model'])[:29]:<30} {int(r['n']):>7}")

print(f"\n{'='*W}\n")
