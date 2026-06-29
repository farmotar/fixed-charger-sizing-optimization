"""
diagnose_ev_mapping.py
────────────────────────
Check what fraction of Z2Z vehicles successfully map to an EV equivalent.
Vehicles that fail to map are silently excluded from the simulation —
this script surfaces those exclusions so we can audit them.
"""
from __future__ import annotations
import sys, re, difflib, openpyxl
from pathlib import Path
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR        = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
EV_CATEGORIES_XLSX = Path(r"D:\Geotab_EV_Parameters\final_categories.xlsx")

# Same matching logic as run_site_full_year.py
EV_DIRECT_PATTERNS = [
    ("tesla model 3",          "Tesla Model 3"),
    ("silverado ev",           "Chevrolet Silverado EV WT"),
    ("f-150 lightning",        "Ford F-150 Lightning"),
    ("ecascadia",              "Freightliner eCascadia"),
    ("em2",                    "Freightliner eM2"),
    ("rivian r1t",             "Rivian R1T"),
    ("rivian r1s",             "Rivian R1S"),
    ("hummer ev",              "GMC Hummer EV"),
    ("bolt ev",                "Chevrolet Bolt EV"),
    ("kia ev6",                "Kia EV6"),
    ("promaster ev",           "Ram ProMaster EV (cargo)"),
    ("e-transit",              "Ford E-Transit"),
    ("volkswagen id.4",        "Volkswagen ID.4"),
    ("volkswagen id. buzz",    "Volkswagen ID. Buzz"),
    ("id.4",                   "Volkswagen ID.4"),
    ("id. buzz",               "Volkswagen ID. Buzz"),
    ("blue arc",               "Blue Arc EV"),
    ("volvo vnr",              "Volvo VNR 4X2 Electric"),
    ("global electric sweeper","Global Electric Street Sweeper (M4E)"),
]
EXTRA_ICE_OVERRIDES = {
    "international hv":       "Freightliner eCascadia",
    "international hx":       "Freightliner eCascadia",
    "international workstar": "Freightliner eCascadia",
    "international paystar":  "Freightliner eCascadia",
    "western star":           "Volvo VNR 4X2 Electric",
    "freightliner 114 sd":    "Freightliner eCascadia",
    "freightliner m2":        "Freightliner eM2",
    "international durastar": "Freightliner eM2",
    "ford f-250":             "Ford F-150 Lightning",
    "ford f-350":             "GMC Hummer EV",
    "ford f-450":             "GMC Hummer EV",
    "ford f-550":             "GMC Hummer EV",
    "ford f-650":             "BYD 6F Cab-Forward Truck",
    "chevrolet tahoe":        "Rivian R1S",
    "nissan frontier":        "Rivian R1T",
    "ram 3500":               "GMC Hummer EV",
    "ram promaster":          "Ram ProMaster EV (cargo)",
}

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())

def _load_ice_to_ev():
    wb = openpyxl.load_workbook(str(EV_CATEGORIES_XLSX), read_only=True, data_only=True)
    ws = wb["EV Equivalencies"]
    SKIP = {"ice example","iceexample","equivalent ev","category",
            "mpge city","mpge hwy","mpge comb","battery (kwh)","battery (kw)",
            "range (mi)","energy consumption at gvwr (kwh/mi)",
            "energy consumption (kwh/mi)","sweeping speed (mph)","sweeping time (h)"}
    out = {}
    for row in ws.iter_rows(values_only=True):
        c1 = row[1] if len(row) > 1 else None
        c2 = row[2] if len(row) > 2 else None
        if not isinstance(c1, str) or not isinstance(c2, str): continue
        k = c1.strip().lower()
        if k in SKIP or not k or not c2.strip(): continue
        out[k] = c2.strip()
    wb.close()
    return out

def _match_ev(make: str, model: str, ice_to_ev: dict) -> str | None:
    make, model = (make or "").strip(), (model or "").strip()
    if not make and not model: return None
    combo, model_n = _norm(f"{make} {model}"), _norm(model)
    for pat, ev in EV_DIRECT_PATTERNS:
        if pat in combo or pat in model_n: return ev
    for pat, ev in EXTRA_ICE_OVERRIDES.items():
        if pat in combo or pat in model_n: return ev
    if combo in ice_to_ev: return ice_to_ev[combo]
    if model_n in ice_to_ev: return ice_to_ev[model_n]
    for key, ev in ice_to_ev.items():
        if combo.startswith(key) or key.startswith(combo): return ev
        if model_n.startswith(key) or key.startswith(model_n): return ev
    close = difflib.get_close_matches(combo, list(ice_to_ev), n=1, cutoff=0.60)
    if not close:
        close = difflib.get_close_matches(model_n, list(ice_to_ev), n=1, cutoff=0.60)
    return ice_to_ev[close[0]] if close else None

# ─────────────────────────────────────────────────────────────────────────────
ice_to_ev = _load_ice_to_ev()
W = 100

SITES = {
    "northgate": "_northgate_z2z_cache.csv",
    "fresno":    "_fresno_z2z_cache.csv",
    "glendale":  "_glendale_z2z_cache.csv",
    "san_diego": "_san_diego_z2z_cache.csv",
}

for slug, cache_file in SITES.items():
    cache_path = BASE_DIR / cache_file
    z2z = pd.read_csv(str(cache_path), low_memory=False)
    z2z_f = z2z[z2z["use_for_optimization_bool"].fillna(False).astype(bool)].copy()
    z2z_f["make"]  = z2z_f["make"].fillna("").astype(str)
    z2z_f["model"] = z2z_f["model"].fillna("").astype(str)

    # Unique vehicles
    veh = (z2z_f[["vehicle_name","make","model"]]
           .drop_duplicates("vehicle_name")
           .reset_index(drop=True))

    # Apply mapping to each unique vehicle
    veh["ev_match"] = veh.apply(
        lambda r: _match_ev(r["make"], r["model"], ice_to_ev), axis=1)

    mapped   = veh[veh["ev_match"].notna()]
    unmapped = veh[veh["ev_match"].isna()]

    print(f"\n{'='*W}")
    print(f"  {slug.upper()} — EV MAPPING AUDIT")
    print(f"{'='*W}")
    print(f"  Unique vehicles in Z2Z (use_for_opt=True): {len(veh)}")
    print(f"  Successfully mapped to EV equivalent      : {len(mapped)} "
          f"({100*len(mapped)/max(len(veh),1):.1f}%)")
    print(f"  FAILED to map (excluded from simulation)  : {len(unmapped)} "
          f"({100*len(unmapped)/max(len(veh),1):.1f}%)")

    # How many Z2Z events do unmapped vehicles account for?
    unmapped_names = set(unmapped["vehicle_name"])
    events_unmapped = z2z_f[z2z_f["vehicle_name"].isin(unmapped_names)]
    events_total    = len(z2z_f)
    print(f"  Events from unmapped vehicles             : {len(events_unmapped):,} "
          f"/ {events_total:,} ({100*len(events_unmapped)/max(events_total,1):.1f}%)")

    if not unmapped.empty:
        print(f"\n  UNMAPPED VEHICLES (make / model → no EV equivalent found):")
        print(f"  {'vehicle_name':<15} {'make':<25} {'model':<35} {'events':>7}")
        print(f"  {'-'*85}")
        # Count events per vehicle
        ev_counts = z2z_f.groupby("vehicle_name").size().rename("n_events")
        unmapped2 = unmapped.join(ev_counts, on="vehicle_name").fillna({"n_events": 0})
        unmapped2 = unmapped2.sort_values("n_events", ascending=False)
        for _, r in unmapped2.iterrows():
            m_make  = str(r["make"])[:24]
            m_model = str(r["model"])[:34]
            print(f"  {str(r['vehicle_name']):<15} {m_make:<25} {m_model:<35} "
                  f"{int(r.get('n_events', 0)):>7}")

    print(f"\n  EV equivalents mapped to (top 10 by vehicle count):")
    mc = (mapped.groupby("ev_match")["vehicle_name"]
          .count().sort_values(ascending=False).head(10))
    for ev_name, cnt in mc.items():
        print(f"    {cnt:>3} vehicles → {ev_name}")

print(f"\n{'='*W}")
print("  MAPPING AUDIT COMPLETE")
print(f"{'='*W}\n")
