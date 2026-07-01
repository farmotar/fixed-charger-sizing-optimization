# Fixed DCFC Charger Sizing — Caltrans EV Fleet Study

A Mixed-Integer Linear Programming (MILP) pipeline that sizes fixed DC Fast Chargers at EV fleet maintenance stations. Given real Geotab telematics data, it finds the optimal number and mix of chargers that minimize total daily cost (equipment ownership + utility demand charges + energy) while guaranteeing every vehicle is fully charged before its next shift.

Developed for the **Caltrans EV Fleet Electrification** study, covering four maintenance stations.

---

## Sites and Utility Rates

| Site key | Location | Utility | Rate Structure |
|----------|----------|---------|----------------|
| `northgate` | Sacramento, CA | SMUD | C&I 21–299 kW TOU — 2-tier demand charge |
| `fresno` | Fresno, CA | PG&E | BEV-2 Secondary — subscription demand |
| `glendale` | Glendale, CA | PG&E BEV-2 *(proxy)* | BEV-2 Secondary — subscription demand |
| `san_diego` | San Diego, CA | SDG&E | EV-HP Secondary — subscription demand |

> **Glendale note:** Glendale Water & Power's actual tariff (Schedule LD-2/PC-1) was not available at the time of analysis. PG&E BEV-2 is used as a proxy. Replace with the actual GWP rate when available (call GWP Customer Service at 855-550-4497 or download Schedule LD-2/PC-1 from the GWP website).

---

## Prerequisites

### Python
Python 3.11 or later. Install dependencies:
```bash
pip install -r requirements.txt
```

### Gurobi (required for the MILP solver)
The MILP uses [Gurobi](https://www.gurobi.com). A **free academic license** is available.

1. Download and install Gurobi from [gurobi.com/downloads](https://www.gurobi.com/downloads/)
2. Request a free academic license at [gurobi.com/academia](https://www.gurobi.com/academia/academic-program-and-licenses/)
3. Activate your license:
   ```bash
   grbgetkey <your-license-key>
   ```

Without Gurobi, the code will fall back to HiGHS via Pyomo (`pip install pyomo highspy`), but solve times will be much longer for large days.

### Input data
Place all per-day charging event CSV files in the project root. The pipeline searches for files named:
```
z2z_milp_events_{site}_{YYYY_MM_DD}.csv
```
For example:
```
z2z_milp_events_northgate_2025_05_01.csv
z2z_milp_events_fresno_2025_09_15.csv
```
These files are extracted from Geotab Zone-to-Zone exports using `extract_z2z_events.py` (see [Data Preparation](#data-preparation) below).

---

## Repository Structure

```
charger_sizing_test/
│
├── ── MAIN PIPELINE ─────────────────────────────────────────────────────────
│
├── run_fixed_charger_milp_pipeline.py   ← START HERE. Runs the full MILP
│                                           sizing pipeline across all 4 sites
│                                           and all operating days.
│
├── rebuild_worst10_schedules.py         Re-solves the 10 worst days per site
│                                        and regenerates per-vehicle schedule
│                                        sheets in the Excel output.
│
├── ── CORE SOLVER ───────────────────────────────────────────────────────────
│
├── exact_northgate_charger_sizing_milp.py   MILP model definition (Gurobi).
│                                             Simultaneously optimises charger
│                                             counts N_c and charging schedule
│                                             for a single day. Used by the
│                                             pipeline as an importable module.
│
├── scenario_runner.py                   Greedy/heuristic charging simulation
│                                        (used for fast pre-screening, not
│                                        for the final MILP results).
│
├── fixed_charger_analysis_all_sites.py  Older greedy full-year analysis script.
│                                        Superseded by run_fixed_charger_milp_pipeline.py
│                                        for the final MILP results.
│
├── ── COST AND RATE MODELS ──────────────────────────────────────────────────
│
├── charger_costs_caltrans.py   Finalized cost table for all charger types
│                               (purchase, installation, O&M, 10-yr life).
│                               Edit this file to update cost assumptions.
│
├── utility_rates.py            Site-specific TOU energy and demand/subscription
│                               rate functions for all 4 utilities.
│                               Edit this file to update tariff numbers.
│
├── recalc_site_costs.py        One-off script that recomputes cost summaries
│                               after updating rates or costs.
│
├── ── DATA EXTRACTION ───────────────────────────────────────────────────────
│
├── extract_z2z_events.py            Parse raw Geotab Zone-to-Zone export
│                                    → produce z2z_milp_events_*.csv files.
│
├── batch_extract_northgate.py       Batch version of extraction for Northgate.
│
├── build_northgate_representative_day.py   Build a synthetic representative
│                                            day for testing.
│
├── ── FIGURE AND REPORT BUILDERS ────────────────────────────────────────────
│
├── build_appendix_a.py          Generate Appendix A cost-breakdown figures
│                                (one per site).
│
├── build_appendix_a_v2.py       Updated figure builder with additional panels.
│
├── build_presentation_figures.py   Publication-style figures for quarterly
│                                   report (output → appendix_a_figures/).
│
├── ── SUPPORTING / ONE-OFF SCRIPTS ──────────────────────────────────────────
│
├── _reprice_glendale_xos_pge.py      Reprice Glendale XOS results under
│                                     PG&E BEV-2 rate (was run once).
├── _rerun_kempower_fresno_glendale.py  Re-run Kempower MILP for Fresno and
│                                       Glendale with corrected rates.
├── _rerun_kempower_glendale_pge.py   Glendale-only Kempower rerun (PG&E proxy).
├── _run_glendale_smud.py             Glendale run under SMUD rate (archived).
│
├── ── DIAGNOSTICS ───────────────────────────────────────────────────────────
│
├── diagnose_all_sites.py          Cross-site diagnostic checks.
├── audit_dispatch_violations.py   Verify no constraint violations in results.
├── analyze_time_windows.py        Inspect dwell-window distributions.
│
├── ── OUTPUTS (generated, tracked in git) ───────────────────────────────────
│
├── fixed_charger_milp_outputs/
│   ├── Fixed_Charger_MILP_Results.xlsx        ← Main deliverable
│   ├── northgate_all_days_milp.csv            All operating days for Northgate
│   ├── northgate_worst10_schedule.csv         Per-vehicle schedule, 10 worst days
│   ├── fresno_all_days_milp.csv
│   ├── fresno_worst10_schedule.csv
│   ├── glendale_all_days_milp.csv
│   ├── glendale_worst10_schedule.csv
│   ├── san_diego_all_days_milp.csv
│   ├── san_diego_worst10_schedule.csv
│   └── _scratch/                              Temp LP/MPS files (not tracked)
│
├── appendix_a_figures/
│   ├── {site}_cost_breakdown.png              Stacked cost bar chart per site
│   ├── {site}_daily_cost.png                  Cost over the analysis year
│   ├── xos_{site}_*.png                       XOS mobile charger comparisons
│   ├── kmp_{site}_*.png                       Kempower mobile charger figures
│   └── presentation_style/                    High-res figures for the report
│
├── ── CONFIGURATION ─────────────────────────────────────────────────────────
│
├── requirements.txt
└── .gitignore
```

---

## Data Preparation

Skip this section if the `z2z_milp_events_*.csv` files are already in the project folder.

**Step 1 — Export from MyGeotab**

In MyGeotab, export a Zone-to-Zone trip history for the relevant station zone and date range. Save the Excel export to `Geotab_Zone_to_Zone_Dataset/` (excluded from git, not synced to this repo).

**Step 2 — Extract charging events**

```bash
python extract_z2z_events.py
```

This reads the raw Geotab export and writes one CSV per operating day per site:
```
z2z_milp_events_northgate_2025_05_01.csv
z2z_milp_events_fresno_2025_05_01.csv
...
```

Each row in these CSVs is one vehicle visit with columns:
- `charging_event_id` — unique ID for this vehicle-day
- `vehicle_id`, `ev_equivalent_model`
- `arrival_time`, `departure_time` — UTC timestamps
- `battery_capacity_kwh` — vehicle battery size
- `assumed_initial_soc_percent` — state-of-charge at arrival
- `energy_needed_kwh` — energy that must be delivered before departure

---

## Running the MILP Pipeline

This is the main workflow. It processes all 1,200+ operating days across all 4 sites in about 25 minutes on a modern laptop with Gurobi.

```bash
python run_fixed_charger_milp_pipeline.py
```

What happens internally:

1. **For every operating day at every site** — run the exact MILP solver to find the minimum-cost charger configuration that achieves 100% vehicle service on that day.
2. **Sort all days by total cost** (worst = highest). Total cost = charger CapEx + energy charges + demand/subscription charges.
3. **Take the 10 highest-cost days** per site (the "worst-case" planning days).
4. **Recommend an envelope configuration** — take the maximum charger count of each type across the 10 worst days. This is the minimum permanent installation that can handle any single worst-case day.
5. **Write outputs** to `fixed_charger_milp_outputs/`.

### Key solver settings (inside the script)

| Setting | Value | Why |
|---------|-------|-----|
| `DT_MINUTES = 15` | 15-min time steps | Keeps binary variable count ~5,000/day so Gurobi solves in <5 seconds |
| `LAMBDA_SMOOTH = 0.0` | No power smoothing | Prevents the model from becoming a harder MIQP problem |
| `GUROBI_MIP_GAP = 0.05` | 5% optimality gap | Sufficient for planning; tightening to 1% roughly triples solve time |
| `GUROBI_MIP_FOCUS = 1` | Feasibility-first | Finds a good solution fast rather than proving optimality |
| `GUROBI_TIME_LIMIT = 60` | 60 s per day | Hard cap; nearly all days solve in <5 s |

To change these settings, edit the constants at the top of `run_fixed_charger_milp_pipeline.py`.

---

## Output Files

### `Fixed_Charger_MILP_Results.xlsx`

The main deliverable. Sheets:

| Sheet | Contents |
|-------|----------|
| `Summary` | One row per site — recommended charger configuration, average service rate on worst 10 days, average and 90th-percentile total cost |
| `Northgate_AllDays` | One row per operating day for Northgate — date, MILP-optimal config, each cost component, service rate |
| `Fresno_AllDays` | Same for Fresno |
| `Glendale_AllDays` | Same for Glendale |
| `SanDiego_AllDays` | Same for San Diego |
| `Northgate_Worst10Sched` | Per-vehicle charging schedule for the 10 worst days at Northgate |
| `Fresno_Worst10Sched` | Same for Fresno |
| `Glendale_Worst10Sched` | Same for Glendale |
| `SanDiego_Worst10Sched` | Same for San Diego |
| `Envelope_Recommendation` | Final charger recommendation per site (max N_c across 10 worst days) |
| `Worst10_Combined` | All 40 worst-day rows (10 per site) in one table |

### `{site}_all_days_milp.csv`

One row per operating day. Columns:

| Column | Meaning |
|--------|---------|
| `date` | Calendar date |
| `n_vehicles` | Number of vehicles that arrived that day |
| `config_label` | MILP-optimal charger mix, e.g. `2×DC150 + 3×DC350` |
| `n_L2`, `n_DC50`, `n_DC150`, `n_DC350` | Count of each charger type in the optimal config |
| `capex_cost` | Daily amortized charger ownership cost ($) |
| `energy_cost` | Energy charge for that day ($) |
| `global_demand_cost` | Monthly demand charge pro-rated to one day ($) |
| `peak_window_cost` | Peak-window demand charge pro-rated to one day (SMUD sites only) |
| `total_op_cost` | Sum of all cost components ($) |
| `service_rate_pct` | % of vehicles fully served |
| `n_full`, `n_partial`, `n_unserved` | Vehicle service breakdown |
| `cost_rank` | 1 = highest-cost day overall |
| `is_worst10` | True for the 10 highest-cost days |

### `{site}_worst10_schedule.csv`

Per-vehicle charging schedule for the 10 worst days. Columns:

| Column | Meaning |
|--------|---------|
| `date` | Calendar date |
| `worst_day_rank` | 1 = most expensive day |
| `total_op_cost` | Total cost for that day ($) |
| `config_label` | Charger config used on this day |
| `charging_event_id` | Unique vehicle-day ID |
| `vehicle_id` | Vehicle identifier |
| `ev_model` | EV equivalent model name |
| `arrival_local`, `departure_local` | Arrival/departure (Pacific time, HH:MM) |
| `dwell_h` | Total dwell time at station (hours) |
| `energy_needed_kwh` | Energy needed for full recharge |
| `energy_delivered_kwh` | Energy actually delivered by MILP |
| `energy_gap_kwh` | Shortfall (0 if fully served) |
| `status` | `full`, `partial`, or `unserved` |
| `charger_type_used` | Which charger type the MILP assigned |
| `charge_start`, `charge_end` | Charging session times (Pacific, HH:MM) |
| `charge_duration_h` | Session length (hours) |
| `soc_start_pct`, `soc_end_pct` | State of charge at start/end (%, max 100) |

---

## Updating Cost Assumptions

### Charger costs
Edit `charger_costs_caltrans.py`. The `build_charger_specs_caltrans()` function returns a dict with `purchase_cost`, `install_cost`, `annual_maint`, and `life_years` for each charger type. The pipeline picks this up automatically on the next run.

Amortized daily CapEx is computed as:
```
C_daily = [(purchase + install) / (life_years × 12) + annual_maint / 12] / 30.42
```

| Charger type | Power | Purchase | Install | O&M/yr | Daily CapEx |
|---|---|---|---|---|---|
| Level 2 AC (`L2_19p2kW`) | 19.2 kW | $11,000 | $14,000 | $550 | $8.36/day |
| Low-power DCFC (`DC_50kW`) | 50 kW | $50,000 | $50,000 | $1,750 | $32.19/day |
| Medium-power DCFC (`DC_150kW`) | 150 kW | $90,000 | $110,000 | $3,000 | $63.01/day |
| High-power DCFC (`DC_350kW`) | 350 kW | $160,000 | $225,000 | $4,500 | $117.80/day |

### Utility rates
Edit `utility_rates.py`. Each utility has an energy rate function and a capacity charge function. Rate constants are defined as module-level variables at the top of each section.

| Utility | Peak hours | Energy rate (peak) | Capacity charge |
|---------|-----------|-------------------|-----------------|
| SMUD | Weekdays 16:00–21:00 | $0.2341/kWh (summer) | $6.454/kW global + $9.960/kW peak-window |
| PG&E BEV-2 | Every day 16:00–21:00 | $0.36977/kWh | $1.91/kW subscription |
| SDG&E EV-HP | Every day 16:00–21:00 | $0.29036/kWh (summer) | $4.81/kW subscription |

After editing, rerun the pipeline to regenerate results.

---

## Adding a New Site

1. Make sure the per-day event CSVs exist with the naming pattern `z2z_milp_events_{newsite}_{YYYY_MM_DD}.csv`.
2. Add the site's utility rate functions to `utility_rates.py` (copy one of the existing blocks as a template).
3. Add the site key and utility to the `SITE_UTILITY` dict in `utility_rates.py`.
4. Add a tuple to the `SITES` list in `run_fixed_charger_milp_pipeline.py`:
   ```python
   ("newsite", "Display Name", "Utility Name"),
   ```
5. Run the pipeline.

---

## Rebuilding Schedule Sheets Only

If you need to regenerate just the per-vehicle schedule sheets in the Excel file (e.g., after changing a cost assumption) without rerunning all 1,200+ days:

```bash
python rebuild_worst10_schedules.py
```

This re-solves only the 10 worst-cost days per site (40 days total, ~2 minutes) and rewrites the `{Site}_Worst10Sched` sheets and CSV files.

---

## How the MILP Works (Technical Summary)

The optimizer is a **Mixed-Integer Linear Program** — a class of optimization where some variables must be integers (you can't install 2.7 chargers).

**Decision variables**

| Variable | Type | Meaning |
|----------|------|---------|
| `N_c` | Integer ≥ 0 | Number of chargers of type `c` to install |
| `x[v,t,c]` | Continuous | Power (kW) delivered to vehicle `v` at timestep `t` by charger type `c` |
| `u[v,t,c]` | Binary | 1 if vehicle `v` is plugged into charger type `c` at timestep `t` |
| `P_total[t]` | Continuous | Total site power draw at timestep `t` |
| `P_max` | Continuous | Global peak demand (kW) |
| `P_peak_win` | Continuous | Peak-window demand (kW) |

**Objective** — minimize total daily cost:
```
min:  Σ_c [N_c × C_daily_c]           ← charger ownership (CapEx + O&M)
    + P_max × c_demand_global          ← demand charge (all-hours peak, $/kW)
    + P_peak_win × c_demand_peak_win   ← peak-window demand charge (SMUD only)
    + Σ_t [P_total[t] × rate(t) × dt] ← energy cost at site TOU rate
```

**Key constraints**
- Each vehicle can only be charged during its dwell window (arrival → departure)
- A vehicle can use at most one charger type per timestep
- The number of vehicles simultaneously on charger type `c` ≤ `N_c`
- Every vehicle must receive its full energy demand by departure (hard constraint)
- Power delivered ≤ charger rated power × efficiency (η = 0.92)

**Time resolution**

The pipeline uses 15-minute timesteps for tractable batch performance. At 5-minute resolution (the original setting), each day has ~1,400 timesteps and ~15,000 binary variables, which takes 60+ seconds per day with no guarantee of a solution. At 15 minutes, each day has ~475 timesteps and ~5,000 binary variables, and Gurobi typically solves in 1–5 seconds.

**Solver performance** with the current settings:
- Each day solves in 1–5 seconds on average
- Total runtime for all 1,217 days across 4 sites: ~25 minutes
- Solution quality: within 5% of global optimum (MIP gap = 5%)

---

## Troubleshooting

**`GurobiError: No Gurobi license found`**
Run `grbgetkey <your-key>` to activate your license. Academic licenses are tied to a specific machine — request a new one if you switch computers.

**`no_solution` for a day**
The solver hit the 60-second time limit without finding any feasible solution. This can happen on unusually busy days (many vehicles, tight dwell windows). Try increasing `GUROBI_TIME_LIMIT` to 120 seconds in the pipeline script.

**SoC values exactly at 100%**
Expected — the pipeline caps state-of-charge at 100% by design. A 15-minute time step can deliver slightly more energy than the battery has room for in that final slot; the cap prevents physically impossible values in the schedule output.

**Excel file locked (`PermissionError`)**
Close `Fixed_Charger_MILP_Results.xlsx` in Excel before running the pipeline or rebuild script.

**Results look wrong for Glendale**
Glendale uses PG&E BEV-2 as a proxy rate, not the actual Glendale Water & Power tariff. Cost estimates for Glendale should be treated as approximations only (see note at the top).

---

## File Naming Conventions

| Pattern | Meaning |
|---------|---------|
| `z2z_milp_events_{site}_{YYYY_MM_DD}.csv` | Raw input — one per operating day per site |
| `{site}_all_days_milp.csv` | Pipeline output — all days summary for one site |
| `{site}_worst10_schedule.csv` | Pipeline output — per-vehicle schedule for 10 worst days |
| `_*.py` | Internal/one-off scripts (underscore prefix = not part of main workflow) |
| `*_log.txt`, `*_err.txt` | Run logs (excluded from git) |

---

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `pandas` | ≥ 1.5 | Data wrangling and CSV/Excel I/O |
| `numpy` | ≥ 1.23 | Numerical operations |
| `matplotlib` | ≥ 3.6 | Figures |
| `openpyxl` | ≥ 3.0 | Read/write Excel files |
| `gurobipy` | ≥ 10.0 | MILP solver (requires Gurobi license) |
| `pytz` | latest | Timezone conversion (UTC → America/Los_Angeles) |

---

## License

MIT License — see [LICENSE](LICENSE) for details.
