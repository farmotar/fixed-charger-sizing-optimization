"""
build_vin_daily_energy_pivot.py
--------------------------------
Read the Geotab Zone-to-Zone optimisation CSV and build a pivot table:
    rows    = vehicle VIN
    columns = calendar day (Pacific time, YYYY-MM-DD)
    values  = total kWh driven to that site on that day
              (trip_first_distance_miles_between × EV efficiency kWh/mile)

One Excel sheet is written per site:
    Northgate  -> to_zone contains "23143 Northgate"
    Fresno     -> to_zone contains "26101 Shop 26 Fresno"
    Glendale   -> to_zone contains "07 GLENDALE HMS"
    San Diego  -> to_zone contains "31101 Shop 31 Kearney Mesa"

Energy is computed by mapping each vehicle's ICE make/model to an EV
equivalent (same look-up used in extract_z2z_events.py), then multiplying
trip distance by the EV's kWh/mile efficiency.  Rows with no EV match or
zero distance contribute 0 kWh.

Missing (VIN, day) combinations are filled with 0.

Output: D:\Geotab_EV_Parameters\charger_sizing_test\vin_daily_energy_by_site.xlsx
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

import openpyxl
import pandas as pd

# ── Paths ─────────────────────────────────────────────────────────────────────
Z2Z_CSV = Path(
    r"D:\Geotab_EV_Parameters\charger_sizing_test"
    r"\Geotab_Zone_to_Zone_Dataset\Geotab_Zone_to_Zone_Dataset"
    r"\01_Final_Dataset\final_zone_to_zone_for_optimization.csv"
)
EV_CATEGORIES_XLSX = Path(r"D:\Geotab_EV_Parameters\final_categories.xlsx")
OUTPUT_XLSX = Path(
    r"D:\Geotab_EV_Parameters\charger_sizing_test\vin_daily_energy_by_site.xlsx"
)

# ── Site definitions (substring matched against to_zone, case-insensitive) ────
SITES: dict[str, str] = {
    "Northgate":  "23143 Northgate",
    "Fresno":     "26101 Shop 26 Fresno",
    "Glendale":   "07 GLENDALE HMS",
    "San Diego":  "31101 Shop 31 Kearney Mesa",
}

# ── EV specs: {canonical_ev_name: efficiency_kwh_per_mile} ────────────────────
EV_EFFICIENCY: dict[str, float] = {
    "Ford F-150 Lightning":                    0.4814,
    "Freightliner eCascadia":                  2.10,
    "Freightliner eM2":                        1.164,
    "Tesla Model 3":                           0.259,
    "Rivian R1T":                              0.427,
    "Rivian R1S":                              0.427,
    "GMC Hummer EV":                           0.640,
    "BYD 6F Cab-Forward Truck":               1.540,
    "Chevrolet Silverado EV WT":              0.350,
    "Chevrolet Bolt EV":                       0.281,
    "Kia EV6":                                 0.288,
    "Blue Arc EV":                             1.000,
    "Ram ProMaster EV (cargo)":               0.671,
    "Ford E-Transit":                          0.560,
    "Volkswagen ID.4":                         0.347,
    "Volkswagen ID. Buzz":                     0.406,
    "Volkswagen ID. Buzz (passenger)":         0.406,
    "Volvo VNR 4X2 Electric":                  1.630,
    "Global Electric Street Sweeper (M4E)":    4.421,
}

# ── EV direct patterns (vehicle already is an EV) ─────────────────────────────
EV_DIRECT_PATTERNS: list[tuple[str, str]] = [
    ("tesla model 3",           "Tesla Model 3"),
    ("silverado ev",            "Chevrolet Silverado EV WT"),
    ("f-150 lightning",         "Ford F-150 Lightning"),
    ("ecascadia",               "Freightliner eCascadia"),
    ("em2",                     "Freightliner eM2"),
    ("rivian r1t",              "Rivian R1T"),
    ("rivian r1s",              "Rivian R1S"),
    ("hummer ev",               "GMC Hummer EV"),
    ("bolt ev",                 "Chevrolet Bolt EV"),
    ("kia ev6",                 "Kia EV6"),
    ("promaster ev",            "Ram ProMaster EV (cargo)"),
    ("e-transit",               "Ford E-Transit"),
    ("volkswagen id.4",         "Volkswagen ID.4"),
    ("volkswagen id. buzz",     "Volkswagen ID. Buzz"),
    ("id.4",                    "Volkswagen ID.4"),
    ("id. buzz",                "Volkswagen ID. Buzz"),
    ("blue arc",                "Blue Arc EV"),
    ("volvo vnr",               "Volvo VNR 4X2 Electric"),
    ("global electric sweeper", "Global Electric Street Sweeper (M4E)"),
]

# ── ICE override patterns ─────────────────────────────────────────────────────
EXTRA_ICE_OVERRIDES: dict[str, str] = {
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


def _load_ev_equivalencies(xlsx: Path) -> dict[str, str]:
    """Return {ice_model_lower: canonical_ev_name} from 'EV Equivalencies' sheet."""
    wb = openpyxl.load_workbook(str(xlsx), read_only=True, data_only=True)
    ws = wb["EV Equivalencies"]
    SKIP = {
        "ice example", "iceexample", "equivalent ev", "category",
        "mpge city", "mpge hwy", "mpge comb", "battery (kwh)", "battery (kw)",
        "range (mi)", "energy consumption at gvwr (kwh/mi)",
        "energy consumption (kwh/mi)", "sweeping speed (mph)", "sweeping time (h)",
    }
    ice_to_ev: dict[str, str] = {}
    for row in ws.iter_rows(values_only=True):
        col1 = row[1] if len(row) > 1 else None
        col2 = row[2] if len(row) > 2 else None
        if not isinstance(col1, str) or not isinstance(col2, str):
            continue
        c1, c2 = col1.strip().lower(), col2.strip()
        if c1 in SKIP or not c1 or not c2:
            continue
        ice_to_ev[c1] = c2
    wb.close()
    return ice_to_ev


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _match_ev(make: str, model: str, ice_to_ev: dict[str, str]) -> str | None:
    make  = (make  or "").strip()
    model = (model or "").strip()
    if not make and not model:
        return None
    combo   = _normalize(f"{make} {model}")
    model_n = _normalize(model)

    for pattern, ev_name in EV_DIRECT_PATTERNS:
        if pattern in combo or pattern in model_n:
            return ev_name

    for pattern, ev_name in EXTRA_ICE_OVERRIDES.items():
        if pattern in combo or pattern in model_n:
            return ev_name

    if combo in ice_to_ev:
        return ice_to_ev[combo]
    if model_n in ice_to_ev:
        return ice_to_ev[model_n]

    for key, ev_name in ice_to_ev.items():
        if combo.startswith(key) or key.startswith(combo):
            return ev_name
        if model_n.startswith(key) or key.startswith(model_n):
            return ev_name

    candidates = list(ice_to_ev.keys())
    close = difflib.get_close_matches(combo, candidates, n=1, cutoff=0.60)
    if not close:
        close = difflib.get_close_matches(model_n, candidates, n=1, cutoff=0.60)
    if close:
        return ice_to_ev[close[0]]
    return None


def build_pivot(df: pd.DataFrame) -> pd.DataFrame:
    """
    Given a filtered DataFrame for one site, return a pivot table:
        index  = vin
        columns = date strings (YYYY-MM-DD, sorted)
        values  = sum of energy_kwh, NaN filled with 0
    """
    pivot = (
        df.groupby(["vin", "date"])["energy_kwh"]
        .sum()
        .unstack(fill_value=0)
    )
    pivot.index.name = "VIN"
    pivot.columns.name = None
    pivot = pivot.sort_index()
    pivot = pivot[sorted(pivot.columns)]
    return pivot


def main() -> None:
    print("=" * 70)
    print("build_vin_daily_energy_pivot.py")
    print("=" * 70)

    # 1. Load EV Equivalencies
    print(f"\n[1] Loading EV Equivalencies from {EV_CATEGORIES_XLSX.name} ...")
    ice_to_ev = _load_ev_equivalencies(EV_CATEGORIES_XLSX)
    print(f"    {len(ice_to_ev)} ICE->EV pairs loaded")

    # 2. Load z2z data (only needed columns)
    print(f"\n[2] Loading Z2Z optimisation CSV (this may take a while) ...")
    needed_cols = ["vin", "make", "model", "to_zone", "to_entry_time",
                   "trip_first_distance_miles_between"]
    df = pd.read_csv(str(Z2Z_CSV), usecols=needed_cols, low_memory=False)
    print(f"    {len(df):,} rows loaded")

    # 3. Keep only rows matching one of our target sites
    site_patterns = "|".join(SITES.values())
    mask = df["to_zone"].str.contains(site_patterns, case=False, na=False, regex=True)
    df = df[mask].copy()
    print(f"    {len(df):,} rows after site filter")

    if df.empty:
        print("  !! No rows matched any target site. Exiting.")
        return

    # 4. Parse timestamp -> Pacific date
    print("\n[3] Parsing timestamps to Pacific dates ...")
    df["to_entry_time"] = pd.to_datetime(df["to_entry_time"], utc=True, errors="coerce")
    df["date"] = (
        df["to_entry_time"]
        .dt.tz_convert("America/Los_Angeles")
        .dt.strftime("%Y-%m-%d")
    )
    df = df[df["date"].notna()].copy()

    # 5. Build EV match table for all unique (make, model) pairs
    print("\n[4] Classifying vehicles by make/model -> EV efficiency ...")
    df["make"]  = df["make"].fillna("").astype(str)
    df["model"] = df["model"].fillna("").astype(str)

    unique_mm = df[["make", "model"]].drop_duplicates()
    ev_map: dict[tuple[str, str], float | None] = {}
    no_match: list[str] = []
    for _, row in unique_mm.iterrows():
        ev_name = _match_ev(row["make"], row["model"], ice_to_ev)
        eff = EV_EFFICIENCY.get(ev_name) if ev_name else None
        ev_map[(row["make"], row["model"])] = eff
        if eff is None:
            no_match.append(f"{row['make']} {row['model']}")

    df["efficiency_kwh_per_mile"] = df.apply(
        lambda r: ev_map.get((r["make"], r["model"])), axis=1
    )

    matched   = df["efficiency_kwh_per_mile"].notna().sum()
    unmatched = df["efficiency_kwh_per_mile"].isna().sum()
    print(f"    Matched rows: {matched:,} | Unmatched (no EV equiv, excluded): {unmatched:,}")
    if no_match:
        unique_nm = sorted(set(no_match))
        print(f"    Models with no EV match ({len(unique_nm)} unique, excluded from pivot):")
        for m in unique_nm[:20]:
            print(f"      {m}")
        if len(unique_nm) > 20:
            print(f"      ... and {len(unique_nm)-20} more")

    # 6. Drop rows with no EV match, then compute energy
    df = df[df["efficiency_kwh_per_mile"].notna()].copy()
    print(f"    Rows kept after EV-match filter: {len(df):,}")
    dist = df["trip_first_distance_miles_between"].fillna(0).clip(lower=0)
    eff  = df["efficiency_kwh_per_mile"]
    df["energy_kwh"] = dist * eff

    # 7. Tag each row with its site
    for site_name, pattern in SITES.items():
        df.loc[
            df["to_zone"].str.contains(pattern, case=False, na=False),
            "site"
        ] = site_name

    # 8. Handle VIN: drop rows where VIN is missing
    df = df[df["vin"].notna() & (df["vin"].astype(str).str.strip() != "")].copy()
    df["vin"] = df["vin"].astype(str).str.strip()

    # 9. Build Excel with one sheet per site
    print(f"\n[5] Building Excel output: {OUTPUT_XLSX.name} ...")
    OUTPUT_XLSX.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(str(OUTPUT_XLSX), engine="openpyxl") as writer:
        for site_name in SITES:
            site_df = df[df["site"] == site_name]
            if site_df.empty:
                print(f"    [{site_name}] No data found — writing empty sheet")
                pd.DataFrame().to_excel(writer, sheet_name=site_name, index=False)
                continue

            pivot = build_pivot(site_df)
            pivot.to_excel(writer, sheet_name=site_name)

            n_vins = len(pivot)
            n_days = len(pivot.columns)
            total_kwh = site_df["energy_kwh"].sum()
            print(
                f"    [{site_name}] {n_vins} VINs x {n_days} days | "
                f"Total energy: {total_kwh:,.1f} kWh"
            )

    print(f"\nDone. Saved -> {OUTPUT_XLSX}")


if __name__ == "__main__":
    main()
