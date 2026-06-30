# Fixed DCFC Charger Sizing Optimization

A Mixed-Integer Linear Programming (MILP) tool for sizing fixed DC Fast Chargers (DCFC) at EV fleet maintenance stations. Given real fleet telematics data, it finds the **optimal number and type of chargers** that minimize total daily cost while ensuring every vehicle gets the energy it needs within its dwell window.

Developed for the **Caltrans EV Fleet Electrification** study.

---

## What Problem Does This Solve?

When a fleet of electric vehicles returns to a maintenance station, each vehicle has:
- A **dwell window** (the time it stays parked before its next trip)
- An **energy demand** (how much it needs to recharge)

The question is: **how many chargers, and at what power level, should be installed?**

Too few chargers → vehicles don't finish charging before their next shift.  
Too many chargers → unnecessary capital and operating costs.

This tool finds the exact answer by solving an optimization problem that simultaneously:
1. Selects the number of chargers of each type to install
2. Schedules which vehicle charges when and at what rate
3. Minimizes total daily cost (charger ownership + utility charges)

---

## Sites Covered

| Site | Location |
|------|----------|
| Northgate | Sacramento, CA |
| Fresno | Fresno, CA |
| Glendale | Glendale, CA |
| San Diego | San Diego, CA |

---

## Charger Types Modeled

| Type | Power | Purchase Cost | Install Cost | Daily Cost (amortized) |
|------|-------|--------------|--------------|------------------------|
| Level 2 AC | 19.2 kW | $11,000 | $14,000 | $8.36/day |
| Low-power DCFC | 50 kW | $50,000 | $50,000 | $32.19/day |
| Medium-power DCFC | 150 kW | $90,000 | $110,000 | $63.01/day |
| High-power DCFC | 350 kW | $160,000 | $225,000 | $117.80/day |

Daily cost is amortized over a **10-year equipment lifespan** using the formula:

```
C_daily = [(purchase + install) / (life_years × 12) + annual_O&M / 12] / 30.42
```

Utility costs use **SMUD C&I Time-of-Use rates** (energy + demand charges).

---

## How the MILP Works

The optimizer is formulated as a Mixed-Integer Linear Program (MILP) — a class of mathematical optimization where some variables must be integers (e.g., you can't install 2.7 chargers).

### Decision Variables

| Variable | Type | Meaning |
|----------|------|---------|
| `N_c` | Integer | Number of chargers of type `c` to install |
| `x[v,t,c]` | Continuous | Power (kW) delivered to vehicle `v` at time step `t` by charger type `c` |
| `u[v,t,c]` | Binary | 1 if vehicle `v` is plugged into charger type `c` at time `t` |
| `P_total[t]` | Continuous | Total site power draw at time step `t` |
| `P_max` | Continuous | Global peak demand (kW) |

### Objective Function

Minimize total daily cost:

```
min:  Σ_c (N_c × C_daily_c)          ← charger ownership cost
    + P_max × 6.45                    ← SMUD site infrastructure demand charge ($/kW)
    + P_peak_window × 9.96            ← SMUD peak-window demand charge (17:00–20:00)
    + λ × Σ_t |P_total[t] - P_total[t-1]|  ← power-smoothing penalty
    + C_energy × Σ_t P_total[t] × dt  ← energy cost
```

### Key Constraints

- Each vehicle can only charge during its **dwell window** (arrival → departure)
- Total power to a vehicle cannot exceed its charger's rated power
- Number of vehicles simultaneously using charger type `c` ≤ `N_c`
- Vehicle must receive its full **energy demand** by departure (or the event is flagged unserved)
- Power ramp changes are penalized to avoid grid stress (smoothing term)

### Solver

- **Primary:** [Gurobi](https://www.gurobi.com) (`gurobipy`) — commercial solver, free academic license available
- **Fallback:** HiGHS via [Pyomo](http://www.pyomo.org) — fully open-source, no license needed

---

## Project Structure

```
charger_sizing_test/
│
├── Core optimization
│   ├── exact_northgate_charger_sizing_milp.py   # Main MILP solver (Gurobi/HiGHS)
│   ├── fixed_charger_analysis_all_sites.py       # Full-year analysis across all 4 sites
│   ├── northgate_charger_sizing_final.py         # Final sizing results for Northgate
│   ├── optimize_northgate_charger_mix.py         # Charger mix search for Northgate
│   └── scenario_runner.py                        # Core charging simulation engine
│
├── Pipeline runners
│   ├── run_all_sites_pipeline.py                 # Run all 4 sites end-to-end
│   ├── run_site_full_year.py                     # Full calendar-year simulation per site
│   ├── run_milp_min1h.py                         # MILP with 1-hour minimum charge constraint
│   └── run_multi_day.py                          # Multi-day batch runner
│
├── Cost models
│   └── charger_costs_caltrans.py                 # Amortized cost model for all charger types
│
├── Data extraction
│   ├── extract_z2z_events.py                     # Parse Geotab Zone-to-Zone export
│   ├── batch_extract_northgate.py                # Batch extraction for Northgate
│   └── build_northgate_representative_day.py     # Build a representative charging day
│
├── Diagnostics
│   ├── diagnose_all_sites.py                     # Cross-site diagnostic checks
│   ├── northgate_full_analysis.py                # Detailed Northgate analysis
│   ├── northgate_worst_days.py                   # Identify worst-case demand days
│   ├── screen_candidate_days.py                  # Screen days for MILP input
│   └── audit_dispatch_violations.py              # Check constraint violations in results
│
├── Visualization
│   ├── plot_day_view.py                          # Plot daily charging profiles
│   ├── plot_min1h_charger_assignment.py          # Plot charger assignments over time
│   └── northgate_plot_all_days.py                # Grid plot across all simulated days
│
└── requirements.txt
```

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/farmotar/fixed-charger-sizing-optimization.git
cd fixed-charger-sizing-optimization
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Set up a solver

**Option A — Gurobi (recommended for speed and large problems):**
- Download from [gurobi.com](https://www.gurobi.com/downloads/)
- Request a free [academic license](https://www.gurobi.com/academia/academic-program-and-licenses/)
- Activate with:
  ```bash
  grbgetkey <your-license-key>
  ```

**Option B — HiGHS (open-source, no license needed):**
```bash
pip install pyomo highspy
```
The MILP script auto-detects and falls back to HiGHS if Gurobi is not found.

---

## Data Requirements

The tool reads **Geotab Zone-to-Zone (Z2Z)** trip data exported from MyGeotab. Place your data files in a `Geotab_Zone_to_Zone_Dataset/` folder in the project root (this folder is excluded from git).

Each export file should contain:
- Vehicle VIN
- Arrival and departure timestamps
- Site zone name
- Trip energy consumption (kWh)

To extract charging events from raw Z2Z data:
```bash
python extract_z2z_events.py
```

---

## Usage

### Step 1 — Extract and prepare charging events

```bash
python extract_z2z_events.py
python build_northgate_representative_day.py
```

### Step 2 — Run the MILP optimizer (Northgate)

```bash
python exact_northgate_charger_sizing_milp.py
```

This outputs:
- Optimal number of chargers per type
- Per-vehicle charging schedule
- Total daily cost breakdown (ownership + demand charges + energy)

### Step 3 — Run full-year analysis across all 4 sites

```bash
python fixed_charger_analysis_all_sites.py
```

This evaluates every charger configuration across the full calendar year and identifies the optimal configuration per site and the 10 worst-case demand days.

### Step 4 — Run the complete pipeline for a single site

```bash
python run_site_full_year.py northgate
python run_site_full_year.py fresno
python run_site_full_year.py glendale
python run_site_full_year.py san_diego
```

### Step 5 — Visualize results

```bash
python plot_day_view.py
python northgate_plot_all_days.py
```

---

## Output Files

Results are written to timestamped output folders (excluded from git):

| Folder | Contents |
|--------|----------|
| `exact_milp_outputs_<date>/` | MILP solution: charger counts, schedule CSVs, cost summary |
| `fixed_charger_outputs/` | Full-year simulation results per site |
| `site_outputs/` | Per-site daily metrics and reports |

---

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `pandas` | ≥ 1.5 | Data processing and tabular output |
| `numpy` | ≥ 1.23 | Numerical computation |
| `matplotlib` | ≥ 3.6 | Charging profile visualization |
| `openpyxl` | ≥ 3.0 | Excel file output |
| `gurobipy` | ≥ 10.0 | MILP solver (requires Gurobi license) |
| `pytz` | latest | Timezone handling for California sites |

---

## License

MIT License — see [LICENSE](LICENSE) for details.
