# package_for_transfer.ps1
# -------------------------
# Creates a self-contained transfer folder with every file the optimizer
# needs to run on another machine.
#
# Usage (run from anywhere):
#   powershell -ExecutionPolicy Bypass -File package_for_transfer.ps1
#
# Output:  northgate_optimizer_transfer\   (in the same folder as this script)
#          northgate_optimizer_transfer.zip (optional, see bottom)

$SRC = Split-Path -Parent $MyInvocation.MyCommand.Path
$DEST = Join-Path $SRC "northgate_optimizer_transfer"

Write-Host ""
Write-Host "Source : $SRC"
Write-Host "Dest   : $DEST"
Write-Host ""

# ── Create destination ────────────────────────────────────────────────────────
New-Item -ItemType Directory -Force $DEST | Out-Null

# ── Python scripts ────────────────────────────────────────────────────────────
$scripts = @(
    "launch_optimizer.py",                      # portable launcher (run this on target machine)
    "exact_northgate_charger_sizing_milp.py",   # MILP solver
    "extract_z2z_events.py",                    # event extractor
    "screen_candidate_days.py",                 # worst-case screener
    "run_multi_day.py",                         # alternative batch runner
    "plot_min1h_charger_assignment.py",         # charger assignment figure
    "charger_costs_caltrans.py",               # Caltrans Q3 FY2025/26 pricing
    "requirements.txt"                          # pip dependencies
)

Write-Host "=== Copying Python scripts ==="
foreach ($f in $scripts) {
    $src_path = Join-Path $SRC $f
    if (Test-Path $src_path) {
        Copy-Item $src_path $DEST -Force
        $size = [math]::Round((Get-Item $src_path).Length / 1KB, 1)
        Write-Host "  [ok] $f  ($size KB)"
    } else {
        Write-Host "  [MISSING] $f  <-- CHECK THIS"
    }
}

# ── Data files ────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Copying data files ==="

$data_files = @(
    @{ Path = Join-Path $SRC "_northgate_z2z_cache.csv";                           Label = "Northgate Z2Z cache (pre-filtered, 1.5 MB)" },
    @{ Path = Join-Path $SRC "northgate_ranked_days.csv";                          Label = "Ranked worst-case days (311 days)" },
    @{ Path = Join-Path $SRC "ev_equivalent_max_charge_power_mapping_filled.xlsx"; Label = "EV charge-rate mapping" },
    @{ Path = "D:\Geotab_EV_Parameters\final_categories.xlsx";                     Label = "EV categories (ICE->EV equivalencies)" }
)

foreach ($item in $data_files) {
    if (Test-Path $item.Path) {
        Copy-Item $item.Path $DEST -Force
        $size = [math]::Round((Get-Item $item.Path).Length / 1MB, 2)
        Write-Host "  [ok] $(Split-Path -Leaf $item.Path)  ($size MB)  -- $($item.Label)"
    } else {
        Write-Host "  [MISSING] $(Split-Path -Leaf $item.Path)  <-- CHECK THIS"
        Write-Host "            Expected at: $($item.Path)"
    }
}

# ── README ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Writing README.txt ==="

$readme = @"
Northgate EV Charging Optimizer — Transfer Package
===================================================
Built: $(Get-Date -Format "yyyy-MM-dd HH:mm")

WHAT'S IN THIS FOLDER
---------------------
  launch_optimizer.py                      <- START HERE on the target machine
  exact_northgate_charger_sizing_milp.py   Main MILP solver (Gurobi)
  extract_z2z_events.py                    Event extractor from Z2Z data
  screen_candidate_days.py                 Worst-case day screener (optional)
  run_multi_day.py                         Alternative batch runner
  plot_min1h_charger_assignment.py         Charger assignment figure generator
  charger_costs_caltrans.py               Caltrans Q3 FY2025/26 charger costs
  requirements.txt                         Python package list

  _northgate_z2z_cache.csv                Pre-filtered Northgate events (1.5 MB)
  northgate_ranked_days.csv               311 days ranked by worst-case score
  ev_equivalent_max_charge_power_mapping_filled.xlsx
  final_categories.xlsx                   ICE->EV model equivalency table

SETUP ON TARGET MACHINE
-----------------------
1. Install Python 3.10 or newer
   https://www.python.org/downloads/

2. Install Gurobi and activate your license
   https://www.gurobi.com/downloads/
   After install:  grbgetkey <your-license-key>

3. Install Python packages:
   pip install -r requirements.txt

4. Put ALL files from this folder into ONE directory
   (any path is fine — the launcher auto-detects its location)

HOW TO RUN
----------
Open a terminal in the folder, then:

   python launch_optimizer.py

The script will:
  - Read northgate_ranked_days.csv to find the top 10 worst-case dates
  - Skip any date that already has a completed output folder
  - For each date: extract events -> solve MILP -> generate figures
  - Save results in: exact_milp_outputs_YYYY_MM_DD/

To change how many dates to run, edit the N_DATES line near the top of
launch_optimizer.py (default: 10).

OUTPUT PER DATE
---------------
  exact_milp_outputs_YYYY_MM_DD/
    exact_milp_selected_charger_mix.csv      Charger counts by type
    exact_milp_charging_schedule.csv         Full 5-min charging schedule
    exact_milp_event_results.csv             Per-vehicle energy delivery
    exact_milp_cost_breakdown.csv            Cost components
    exact_milp_power_profile_with_events.png Power profile figure
    milp_min1h_charger_assignment.png        Charger assignment Gantt chart

CHARGER COST BASIS
------------------
Costs come from the Caltrans Research Quarterly Report (IA 65A1281, Q3 FY2025/26).
See charger_costs_caltrans.py for full detail and source ranges.

  L2  19.2 kW  : $4.04/charger/day   (purchase $5,300 + install $3,950)
  DC  50 kW    : $30.48/charger/day  (purchase $32,500 + install $42,500)
  DC 150 kW    : $57.87/charger/day  (purchase $67,500 + install $77,500)
  DC 350 kW    : $103.07/charger/day (purchase $160,000 + install $105,000)

CONTACT / SOURCE
----------------
Code: D:\Geotab_EV_Parameters\charger_sizing_test\  (original machine)
"@

$readme | Out-File -FilePath (Join-Path $DEST "README.txt") -Encoding utf8
Write-Host "  [ok] README.txt"

# ── Summary ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Transfer package contents ==="
Get-ChildItem $DEST | Sort-Object Name | ForEach-Object {
    $sizeKB = [math]::Round($_.Length / 1KB, 1)
    Write-Host ("  {0,-55} {1,8} KB" -f $_.Name, $sizeKB)
}

$totalMB = [math]::Round((Get-ChildItem $DEST | Measure-Object Length -Sum).Sum / 1MB, 2)
Write-Host ""
Write-Host "Total package size: $totalMB MB"
Write-Host "Package ready at  : $DEST"
Write-Host ""

# ── Optional: create ZIP ──────────────────────────────────────────────────────
$zip_path = "$DEST.zip"
if (Test-Path $zip_path) { Remove-Item $zip_path -Force }
try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory($DEST, $zip_path)
    $zipMB = [math]::Round((Get-Item $zip_path).Length / 1MB, 2)
    Write-Host "ZIP created       : $zip_path  ($zipMB MB)"
} catch {
    Write-Host "ZIP creation skipped (copy the folder manually if needed)"
}
