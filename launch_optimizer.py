"""
launch_optimizer.py
--------------------
Portable launcher for the Northgate MILP charger-sizing optimizer.

Put this file in the same folder as all the other project files and run:
    python launch_optimizer.py

Paths are resolved relative to this file's location — no hardcoded drive
letters.  The pre-filtered Northgate cache (_northgate_z2z_cache.csv) and
the pre-ranked days table (northgate_ranked_days.csv) must be in the same
folder (they are included in the transfer package).

Batch checkpointing:
    Dates are processed in batches of BATCH_SIZE.  After every batch a
    progress log (run_progress_log.csv) is appended so you have a full
    record of every run.  If the process is interrupted (crash, power loss,
    Ctrl-C), just re-run — completed dates are detected from their output
    folders and skipped automatically.  No work is ever repeated.

Steps per date:
    1. extract_z2z_events.py      -> events CSV
    2. exact_northgate_charger_sizing_milp.py -> solve + export CSVs + power plot
    3. plot_min1h_charger_assignment.py       -> charger-assignment figure

Output for date YYYY-MM-DD goes into:
    <this folder>/exact_milp_outputs_YYYY_MM_DD/
"""

from __future__ import annotations

import importlib
import math
import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd

# ── Config ────────────────────────────────────────────────────────────────────
THIS_DIR           = Path(__file__).parent.resolve()

CACHE_CSV          = THIS_DIR / "_northgate_z2z_cache.csv"
RANKED_DAYS_CSV    = THIS_DIR / "northgate_ranked_days.csv"
EV_CATEGORIES_XLSX = THIS_DIR / "final_categories.xlsx"
CHARGE_RATE_XLSX   = THIS_DIR / "ev_equivalent_max_charge_power_mapping_filled.xlsx"
PROGRESS_LOG_CSV   = THIS_DIR / "run_progress_log.csv"

SKIP_DATE   = "2025-06-30"  # the original reference day — skip it
BATCH_SIZE  = 5             # dates per batch; progress is saved after each batch


# ── Startup checks ────────────────────────────────────────────────────────────

def _check_files() -> None:
    required = {
        "Cache CSV (pre-filtered Z2Z)": CACHE_CSV,
        "Ranked days CSV":              RANKED_DAYS_CSV,
        "EV categories XLSX":           EV_CATEGORIES_XLSX,
        "Charge rate XLSX":             CHARGE_RATE_XLSX,
        "extract_z2z_events.py":        THIS_DIR / "extract_z2z_events.py",
        "exact_northgate_charger_sizing_milp.py":
            THIS_DIR / "exact_northgate_charger_sizing_milp.py",
        "plot_min1h_charger_assignment.py":
            THIS_DIR / "plot_min1h_charger_assignment.py",
        "charger_costs_caltrans.py":    THIS_DIR / "charger_costs_caltrans.py",
    }
    missing = [name for name, path in required.items() if not path.exists()]
    if missing:
        print("[ERROR] The following required files are missing:")
        for name in missing:
            print(f"  - {name}")
        sys.exit(1)
    print("[ok] All required files found.")


# ── Progress log ──────────────────────────────────────────────────────────────

def _append_progress_log(batch_results: list[dict], run_ts: str) -> None:
    """Append batch results to the cumulative progress log CSV."""
    rows = []
    for r in batch_results:
        rows.append({
            "run_started":  run_ts,
            "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "date":         r["date"],
            "status":       r["status"],
            "output_dir":   r.get("output_dir", ""),
        })
    df = pd.DataFrame(rows)
    write_header = not PROGRESS_LOG_CSV.exists()
    df.to_csv(PROGRESS_LOG_CSV, mode="a", index=False, header=write_header)
    print(f"  [log] Progress saved -> {PROGRESS_LOG_CSV.name}  "
          f"({sum(1 for r in batch_results if r['status'] == 'ok')}/"
          f"{len(batch_results)} ok in this batch)")


def _print_batch_summary(batch_results: list[dict], batch_num: int,
                         total_batches: int, total_done: int,
                         total_selected: int) -> None:
    n_ok  = sum(1 for r in batch_results if r["status"] == "ok")
    n_err = len(batch_results) - n_ok
    print(f"\n{'─'*70}")
    print(f"  BATCH {batch_num}/{total_batches} COMPLETE  "
          f"| {n_ok} ok  {n_err} errors  "
          f"| overall {total_done}/{total_selected} done")
    if n_err:
        for r in batch_results:
            if r["status"] != "ok":
                print(f"    [{r['status']:>14}]  {r['date']}")
    print(f"{'─'*70}")


# ── Per-date pipeline ─────────────────────────────────────────────────────────

def _run_date(date_str: str, ez, milp, plot_script: Path,
              global_idx: int, total: int) -> dict:
    """Run the full extract → MILP → plot pipeline for one date."""
    date_tag   = date_str.replace("-", "_")
    events_csv = THIS_DIR / f"z2z_milp_events_northgate_{date_tag}.csv"
    output_dir = THIS_DIR / f"exact_milp_outputs_{date_tag}"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*70}")
    print(f"[{global_idx}/{total}]  DATE: {date_str}")
    print(f"{'='*70}")

    # Step 1: extract events
    print("\n--- Step 1/3: extract events ---")
    try:
        ez.TARGET_DATE = date_str
        ez.OUTPUT_CSV  = events_csv
        ez.main()
    except Exception:
        print(f"[ERROR] extract_z2z_events failed for {date_str}:")
        traceback.print_exc()
        return dict(date=date_str, status="extract_error",
                    output_dir=str(output_dir))

    if not events_csv.exists():
        print(f"[SKIP] No events found for {date_str}.")
        return dict(date=date_str, status="no_events")

    try:
        n_ev = len(pd.read_csv(events_csv))
    except Exception:
        n_ev = -1
    print(f"[info] {n_ev} events in {events_csv.name}")
    if n_ev < 1:
        print(f"[SKIP] Empty events CSV for {date_str} — skipping MILP.")
        return dict(date=date_str, status="no_events")

    # Step 2: MILP optimiser
    print("\n--- Step 2/3: MILP optimiser ---")
    try:
        milp.INPUT_PATH_PRIMARY = events_csv
        milp.OUTPUT_DIR         = output_dir
        milp.main()
    except Exception:
        print(f"[ERROR] MILP failed for {date_str}:")
        traceback.print_exc()
        return dict(date=date_str, status="milp_error",
                    output_dir=str(output_dir))

    # Step 3: charger-assignment figure
    print("\n--- Step 3/3: charger-assignment figure ---")
    env = os.environ.copy()
    env["MILP_NEW_DIR"]    = str(output_dir)
    env["MILP_EVENTS_CSV"] = str(events_csv)
    env["MILP_DATE"]       = date_str
    try:
        proc = subprocess.run(
            [sys.executable, str(plot_script)],
            env=env, capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            print(f"[WARNING] plot script exit code {proc.returncode}")
            if proc.stderr:
                print(proc.stderr[-3000:])
        else:
            print(f"  Figure -> {output_dir / 'milp_min1h_charger_assignment.png'}")
    except subprocess.TimeoutExpired:
        print(f"[WARNING] plot script timed out for {date_str}")
    except Exception:
        traceback.print_exc()

    return dict(date=date_str, status="ok", output_dir=str(output_dir))


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    sys.path.insert(0, str(THIS_DIR))
    _check_files()

    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Import the two modules with proper main() functions
    print("\n[import] Loading extract_z2z_events …")
    ez = importlib.import_module("extract_z2z_events")
    print("[import] Loading exact_northgate_charger_sizing_milp …")
    milp = importlib.import_module("exact_northgate_charger_sizing_milp")

    # Patch all hardcoded paths to point at THIS_DIR
    ez.Z2Z_CSV            = CACHE_CSV
    ez.FILTER_USE_FOR_OPT = True
    ez.EV_CATEGORIES_XLSX = EV_CATEGORIES_XLSX
    ez.CHARGE_RATE_XLSX   = CHARGE_RATE_XLSX

    # Determine which dates still need to be run
    ranked = pd.read_csv(RANKED_DAYS_CSV)
    already_done = {
        d.replace("exact_milp_outputs_", "").replace("_", "-")
        for d in os.listdir(THIS_DIR)
        if d.startswith("exact_milp_outputs_")
        and (THIS_DIR / d / "exact_milp_selected_charger_mix.csv").exists()
    }
    candidates = ranked[
        ~ranked["date"].isin(already_done) & (ranked["date"] != SKIP_DATE)
    ]
    selected = candidates["date"].tolist()

    if not selected:
        print("\n[info] All ranked days already completed — nothing to do.")
        return

    n_total   = len(selected)
    n_batches = math.ceil(n_total / BATCH_SIZE)
    print(f"\n{len(already_done)} dates already completed, "
          f"{n_total} remaining (of {len(ranked)} total ranked days).")
    print(f"Running in {n_batches} batches of up to {BATCH_SIZE} dates each.")
    print(f"Progress log: {PROGRESS_LOG_CSV.name}\n")

    plot_script  = THIS_DIR / "plot_min1h_charger_assignment.py"
    all_results  = []
    total_done   = 0

    for batch_num, batch_start in enumerate(range(0, n_total, BATCH_SIZE), 1):
        batch = selected[batch_start : batch_start + BATCH_SIZE]

        print(f"\n{'#'*70}")
        print(f"  BATCH {batch_num}/{n_batches}  "
              f"({batch_start + 1}–{batch_start + len(batch)} of {n_total})")
        print(f"  Dates: {batch[0]}  …  {batch[-1]}")
        print(f"{'#'*70}")

        batch_results = []
        for j, date_str in enumerate(batch, 1):
            global_idx = batch_start + j
            result = _run_date(date_str, ez, milp, plot_script,
                               global_idx, n_total)
            batch_results.append(result)
            if result["status"] == "ok":
                total_done += 1

        # Save this batch to the progress log immediately
        _append_progress_log(batch_results, run_ts)
        _print_batch_summary(batch_results, batch_num, n_batches,
                             total_done + len(already_done), len(ranked) - 1)

        all_results.extend(batch_results)

    # ── Final summary ──────────────────────────────────────────────────────────
    n_ok  = sum(1 for r in all_results if r["status"] == "ok")
    n_err = sum(1 for r in all_results if r["status"] not in ("ok", "no_events"))
    n_skip = sum(1 for r in all_results if r["status"] == "no_events")

    print(f"\n{'='*70}")
    print("FULL RUN COMPLETE")
    print(f"{'='*70}")
    print(f"  Completed this session : {n_ok} ok  |  {n_err} errors  |  {n_skip} skipped (no events)")
    print(f"  Total ever completed   : {len(already_done) + n_ok} / {len(ranked) - 1} eligible days")
    print(f"  Progress log           : {PROGRESS_LOG_CSV}")
    if n_err:
        print(f"\n  Dates with errors (will be retried on next run):")
        for r in all_results:
            if r["status"] not in ("ok", "no_events"):
                print(f"    [{r['status']:>14}]  {r['date']}")
    print()


if __name__ == "__main__":
    main()
