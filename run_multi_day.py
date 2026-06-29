"""
run_multi_day.py
----------------
Runs the Northgate MILP charger-sizing optimizer for N_DATES randomly-selected
dates from the Z2Z dataset (excluding the reference day 2025-06-30).

For each date:
  1. extract_z2z_events.py                  -> events CSV
  2. exact_northgate_charger_sizing_milp.py -> solve + export CSVs + power-profile plot
  3. plot_min1h_charger_assignment.py       -> charger-assignment figure (subprocess)

Optimization: Z2Z dataset is read ONCE up-front and filtered to Northgate
rows only; a temp cache CSV is written so each per-date extract call is fast.

Output for date YYYY-MM-DD goes into:
  D:/Geotab_EV_Parameters/charger_sizing_test/exact_milp_outputs_YYYY_MM_DD/
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import traceback
from pathlib import Path

import pandas as pd

# ── Config ────────────────────────────────────────────────────────────────────
CHARGER_DIR = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
Z2Z_CSV = (
    CHARGER_DIR
    / "Geotab_Zone_to_Zone_Dataset"
    / "Geotab_Zone_to_Zone_Dataset"
    / "01_Final_Dataset"
    / "final_zone_to_zone_all_rows.csv"
)
NORTHGATE_CACHE_CSV = CHARGER_DIR / "_northgate_z2z_cache.csv"

SKIP_DATE        = "2025-06-30"
N_DATES          = 10
MIN_EVENTS       = 3          # minimum Northgate use_for_opt events on a date
TARGET_ZONE      = "Northgate"
RANKED_DAYS_CSV  = CHARGER_DIR / "northgate_ranked_days.csv"   # from screen_candidate_days.py

# Columns that extract_z2z_events.py needs from the Z2Z CSV
Z2Z_NEEDED_COLS = [
    "vehicle_name", "make", "model", "year",
    "to_zone", "to_entry_time", "to_exit_time", "to_dwell_minutes",
    "trip_first_distance_miles_between",
    "use_for_optimization_bool",
]


# ── Z2Z pre-filter ────────────────────────────────────────────────────────────

def build_northgate_cache() -> tuple[list[str], Path]:
    """
    Read full Z2Z CSV once, filter to Northgate + use_for_optimization_bool,
    save to a small cache CSV, and return (eligible_dates, cache_path).
    """
    print(f"[scan] Reading full Z2Z CSV — this may take a minute …")
    z2z = pd.read_csv(str(Z2Z_CSV), usecols=Z2Z_NEEDED_COLS, low_memory=False)
    print(f"[scan] {len(z2z):,} total rows loaded")

    z2z = z2z[z2z["use_for_optimization_bool"].fillna(False).astype(bool)].copy()
    print(f"[scan] {len(z2z):,} rows after use_for_optimization_bool filter")

    z2z = z2z[z2z["to_zone"].str.contains(TARGET_ZONE, case=False, na=False)].copy()
    print(f"[scan] {len(z2z):,} {TARGET_ZONE} rows")

    # Derive Pacific calendar date for counting / selection
    z2z["to_entry_time"] = pd.to_datetime(z2z["to_entry_time"], utc=True, errors="coerce")
    z2z["_date_pacific"] = (
        z2z["to_entry_time"]
        .dt.tz_convert("America/Los_Angeles")
        .dt.strftime("%Y-%m-%d")
    )

    counts   = z2z.groupby("_date_pacific").size()
    eligible = counts[counts >= MIN_EVENTS].index.tolist()
    eligible = sorted(d for d in eligible if d != SKIP_DATE)
    print(f"[scan] {len(eligible)} eligible dates (>={MIN_EVENTS} events, excl. {SKIP_DATE})")

    # Save cache (drop helper column so extract script is unaffected)
    z2z.drop(columns=["_date_pacific"]).to_csv(NORTHGATE_CACHE_CSV, index=False)
    print(f"[scan] Northgate cache saved: {NORTHGATE_CACHE_CSV}  ({len(z2z):,} rows)")

    return eligible, NORTHGATE_CACHE_CSV


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    sys.path.insert(0, str(CHARGER_DIR))

    _, cache_csv = build_northgate_cache()

    # Load ranked days from screen_candidate_days.py output
    if not RANKED_DAYS_CSV.exists():
        print(f"[ERROR] Ranked days CSV not found: {RANKED_DAYS_CSV}")
        print("        Run screen_candidate_days.py first.")
        sys.exit(1)

    ranked   = pd.read_csv(RANKED_DAYS_CSV)
    # Skip dates that already have a completed output folder
    already_done = {
        d.replace("exact_milp_outputs_", "").replace("_", "-")
        for d in os.listdir(CHARGER_DIR)
        if d.startswith("exact_milp_outputs_") and
           (CHARGER_DIR / d / "exact_milp_selected_charger_mix.csv").exists()
    }
    candidates = ranked[~ranked["date"].isin(already_done) & (ranked["date"] != SKIP_DATE)]
    selected   = candidates.head(N_DATES)["date"].tolist()

    print(f"\nTop {len(selected)} worst-case dates (from {RANKED_DAYS_CSV.name}):")
    for i, d in enumerate(selected, 1):
        row = ranked[ranked["date"] == d].iloc[0]
        print(f"  {i:>2}. {d}  score={row['score']:.3f}  "
              f"pk_tight={int(row['peak_tight_sim'])}  "
              f"pk_heavy={int(row['peak_sim_heavy'])}  "
              f"events={int(row['n_events'])}")

    # Import the two modules that have proper main() functions
    print("\n[import] Loading extract_z2z_events …")
    ez   = importlib.import_module("extract_z2z_events")
    print("[import] Loading exact_northgate_charger_sizing_milp …")
    milp = importlib.import_module("exact_northgate_charger_sizing_milp")

    # Point extract at the small Northgate-only cache (already filtered)
    ez.Z2Z_CSV           = cache_csv
    ez.FILTER_USE_FOR_OPT = True   # cache rows all have use_for_opt=True — still correct

    plot_script = CHARGER_DIR / "plot_min1h_charger_assignment.py"
    results     = []

    for i, date_str in enumerate(selected, 1):
        date_tag   = date_str.replace("-", "_")
        events_csv = CHARGER_DIR / f"z2z_milp_events_northgate_{date_tag}.csv"
        output_dir = CHARGER_DIR / f"exact_milp_outputs_{date_tag}"
        output_dir.mkdir(parents=True, exist_ok=True)

        sep = "=" * 70
        print(f"\n{sep}")
        print(f"[{i}/{len(selected)}]  DATE: {date_str}")
        print(sep)

        # ── Step 1: Extract events ─────────────────────────────────────────
        print(f"\n--- Step 1/3: extract events ---")
        try:
            ez.TARGET_DATE = date_str
            ez.OUTPUT_CSV  = events_csv
            ez.main()
        except Exception:
            print(f"[ERROR] extract_z2z_events failed for {date_str}:")
            traceback.print_exc()
            results.append(dict(date=date_str, status="extract_error"))
            continue

        if not events_csv.exists():
            print(f"[SKIP] Events CSV not created for {date_str} — no events found.")
            results.append(dict(date=date_str, status="no_events"))
            continue

        try:
            n_ev = len(pd.read_csv(events_csv))
        except Exception:
            n_ev = -1
        print(f"[info] {n_ev} events in {events_csv.name}")
        if n_ev < 1:
            print(f"[SKIP] Empty events CSV for {date_str} — skipping MILP.")
            results.append(dict(date=date_str, status="no_events"))
            continue

        # ── Step 2: MILP optimiser ─────────────────────────────────────────
        print(f"\n--- Step 2/3: MILP optimiser ---")
        try:
            milp.INPUT_PATH_PRIMARY = events_csv
            milp.OUTPUT_DIR         = output_dir
            milp.main()
        except Exception:
            print(f"[ERROR] MILP failed for {date_str}:")
            traceback.print_exc()
            results.append(dict(date=date_str, status="milp_error", output_dir=str(output_dir)))
            continue

        # ── Step 3: Charger-assignment figure ──────────────────────────────
        print(f"\n--- Step 3/3: charger-assignment figure ---")
        env = os.environ.copy()
        env["MILP_NEW_DIR"]    = str(output_dir)
        env["MILP_EVENTS_CSV"] = str(events_csv)
        env["MILP_DATE"]       = date_str
        try:
            proc = subprocess.run(
                [sys.executable, str(plot_script)],
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if proc.returncode != 0:
                print(f"[WARNING] plot script exit code {proc.returncode}")
                if proc.stderr:
                    print(proc.stderr[-3000:])
            else:
                out_fig = output_dir / "milp_min1h_charger_assignment.png"
                print(f"  Figure saved -> {out_fig}")
        except subprocess.TimeoutExpired:
            print(f"[WARNING] plot script timed out for {date_str}")
        except Exception:
            print(f"[ERROR] plot subprocess failed:")
            traceback.print_exc()

        results.append(dict(date=date_str, status="ok", output_dir=str(output_dir)))

    # ── Final summary ──────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("MULTI-DAY RUN — SUMMARY")
    print(f"{'='*70}")
    for r in results:
        tag = f"[{r['status']:>14}]"
        out = f"  {r.get('output_dir', '')}"
        print(f"  {r['date']}  {tag}{out}")

    n_ok = sum(1 for r in results if r["status"] == "ok")
    print(f"\n{n_ok}/{len(results)} dates completed successfully.")


if __name__ == "__main__":
    main()
