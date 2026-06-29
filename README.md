# Fixed DCFC Charger Sizing Optimization

MILP-based optimization tool for sizing fixed DC Fast Chargers (DCFC) at Caltrans EV fleet maintenance stations. Uses Geotab telematics data to determine the optimal number and type of chargers under real-world dwell-time, energy, and utility-rate constraints.

## Overview

This project analyzes fleet charging needs across **4 Caltrans maintenance stations**:

| Site | Location |
|------|----------|
| Northgate | Sacramento, CA |
| Fresno | Fresno, CA |
| Glendale | Glendale, CA |
| San Diego | San Diego, CA |

### Charger Types Modeled

| Type | Power | Purchase Cost | Install Cost | Daily Cost (amortized) |
|------|-------|--------------|--------------|----------------------|
| Level 2 AC | 19.2 kW | $11,000 | $14,000 | $9.12/day |
| Low-power DC | 50 kW | $50,000 | $50,000 | $35.23/day |
| Medium-power DC | 150 kW | $90,000 | $110,000 | $69.10/day |
| High-power DC | 350 kW | $160,000 | $225,000 | $129.52/day |

Costs amortized over a 9-year equipment lifespan with SMUD C&I utility rates (Time-of-Use energy + demand charges).

## Optimization Approach

### MILP Optimizer (`exact_northgate_charger_sizing_milp.py`)
Globally optimizes charger count and charging schedule simultaneously using a Mixed-Integer Linear Program (MILP):

- **Solver:** Gurobi (primary), Pyomo + HiGHS (fallback)
- **Decision variables:** number of chargers per type, per-vehicle charging schedule, site peak power
- **Objective:** minimize total daily cost = charger ownership + SMUD demand charges + energy cost + power-smoothing penalty
- **Constraints:** vehicle dwell windows, energy demand, charger capacity

### Greedy Simulation (`fixed_charger_analysis_all_sites.py`)
Full-year simulation across all 4 sites with 5-minute resolution:
- Phase 1: Simulate all days × all charger configurations
- Phase 2: Select optimal configuration per site (best service rate at minimum cost)
- Phase 3: Rank worst 10 days by daily cost
- Phase 4: Compare all configurations on worst days

## Project Structure

```
charger_sizing_test/
│
├── Core optimization
│   ├── exact_northgate_charger_sizing_milp.py   # MILP solver (Gurobi/HiGHS)
│   ├── fixed_charger_analysis_all_sites.py       # Greedy optimizer, all 4 sites
│   ├── northgate_charger_sizing_final.py         # Final Northgate sizing
│   ├── optimize_northgate_charger_mix.py         # Charger mix optimizer
│   └── scenario_runner.py                        # Core simulation engine
│
├── Pipeline runners
│   ├── run_all_sites_pipeline.py                 # Run all 4 sites
│   ├── run_site_pipeline.py                      # Single-site pipeline
│   ├── run_site_full_year.py                     # Full-year simulation
│   ├── run_milp_min1h.py                         # MILP with 1-hr minimum charge
│   └── run_multi_day.py                          # Multi-day batch runner
│
├── Cost models
│   └── charger_costs_caltrans.py                 # Amortized charger cost model
│
├── Data extraction
│   ├── extract_z2z_events.py                     # Zone-to-zone event extraction
│   ├── batch_extract_northgate.py                # Northgate batch extraction
│   └── build_northgate_representative_day.py     # Representative day builder
│
├── Diagnostics & analysis
│   ├── diagnose_all_sites.py                     # Cross-site diagnostics
│   ├── northgate_full_analysis.py                # Northgate deep analysis
│   ├── northgate_worst_days.py                   # Worst-day identification
│   ├── screen_candidate_days.py                  # Candidate day screening
│   └── audit_dispatch_violations.py              # Dispatch constraint auditing
│
├── Visualization
│   ├── generate_fixed_charger_presentation.py    # PowerPoint report generator
│   ├── plot_day_view.py                          # Daily charging profile plots
│   ├── plot_min1h_charger_assignment.py          # Charger assignment plots
│   └── northgate_plot_all_days.py                # All-days plot grid
│
└── requirements.txt
```

## Installation

```bash
pip install -r requirements.txt
```

> **Note:** Gurobi (`gurobipy`) requires a separate license from [gurobi.com](https://www.gurobi.com). Free academic/research licenses are available. After installing Gurobi, activate your license with:
> ```bash
> grbgetkey <your-license-key>
> ```
> If Gurobi is unavailable, the MILP script automatically falls back to **HiGHS** via Pyomo (open-source, no license required).

## Usage

### Run full-year greedy analysis for all 4 sites
```bash
python fixed_charger_analysis_all_sites.py
```

### Run MILP optimizer for Northgate
```bash
python exact_northgate_charger_sizing_milp.py
```

### Run pipeline for a single site
```bash
python run_site_pipeline.py fresno
python run_site_pipeline.py glendale
python run_site_pipeline.py san_diego
python run_site_pipeline.py northgate
```

### Run all sites pipeline
```bash
python run_all_sites_pipeline.py
```

### Generate presentation slides
```bash
python generate_fixed_charger_presentation.py
```

## Data Requirements

The tool reads Geotab **Zone-to-Zone (Z2Z)** trip data exported from MyGeotab. Data should be placed in the `Geotab_Zone_to_Zone_Dataset/` folder (not tracked in git — add your own data locally).

Required columns include vehicle VIN, arrival/departure timestamps, site zone, and trip energy consumption.

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `pandas` | ≥ 1.5 | Data processing |
| `numpy` | ≥ 1.23 | Numerical computation |
| `matplotlib` | ≥ 3.6 | Visualization |
| `openpyxl` | ≥ 3.0 | Excel output |
| `gurobipy` | ≥ 10.0 | MILP solver (requires license) |
| `pytz` | latest | Timezone handling |

## Project Context

Developed for the **Caltrans District 6 EV Fleet Electrification** study. The goal is to right-size fixed DCFC infrastructure at maintenance stations to serve the electrified fleet at minimum total cost while maintaining service reliability on worst-case demand days.
