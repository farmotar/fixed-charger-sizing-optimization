"""
charger_costs_caltrans.py
--------------------------
Finalized charger cost assumptions used for all Caltrans site analyses.

Values match the agreed cost table (Purchase Cost / Installation-Only Cost columns):

  Charger type         Power    Purchase    Install    $/kW    Life   O&M+network
  ─────────────────── ──────── ─────────── ────────── ─────── ────── ────────────
  Level 2 AC          19.2 kW  $11,000     $14,000    $1,316  10 yr  $550/port-yr
  Low-power DCFC       50 kW   $50,000     $50,000    $2,000  10 yr  $1,750/disp-yr
  Medium-power DCFC   150 kW   $90,000    $110,000    $1,333  10 yr  $3,000/disp-yr
  High-power DCFC     350 kW  $160,000    $225,000    $1,100  10 yr  $4,500/disp-yr

Daily CapEx formula (same across all uses):
    C_daily = [(purchase + install) / (life_years * 12)
               + annual_maint / 12] / 30.42

Resulting daily CapEx per unit:
    L2_19p2kW  :  $8.36 / charger / day
    DC_50kW    : $32.19 / charger / day
    DC_150kW   : $63.01 / charger / day
    DC_350kW   : $117.80 / charger / day
"""

from __future__ import annotations


def build_charger_specs_caltrans() -> dict:
    """
    Return charger hardware specs using the finalized cost table.
    Drop-in replacement for build_charger_specs().

    Daily CapEx formula:
        C_daily = [(purchase + install) / (life_years * 12)
                   + annual_maint / 12] / 30.42
    """
    return {
        "L2_19p2kW": {
            "ac_dc":         "AC",
            "power_kw":       19.2,
            # Finalized: purchase=$11,000, install-only=$14,000, life=10yr, O&M=$550/yr
            # Daily CapEx: [(11k+14k)/(10×12) + 550/12] / 30.42 = $8.36/day
            "purchase_cost":  11_000,
            "install_cost":   14_000,
            "annual_maint":      550,
            "life_years":         10,
        },
        "DC_50kW": {
            "ac_dc":         "DC",
            "power_kw":       50.0,
            # Finalized: purchase=$50,000, install-only=$50,000, life=10yr, O&M=$1,750/yr
            # Daily CapEx: [(50k+50k)/(10×12) + 1750/12] / 30.42 = $32.19/day
            "purchase_cost":  50_000,
            "install_cost":   50_000,
            "annual_maint":    1_750,
            "life_years":         10,
        },
        "DC_150kW": {
            "ac_dc":         "DC",
            "power_kw":      150.0,
            # Finalized: purchase=$90,000, install-only=$110,000, life=10yr, O&M=$3,000/yr
            # Daily CapEx: [(90k+110k)/(10×12) + 3000/12] / 30.42 = $63.01/day
            "purchase_cost":  90_000,
            "install_cost":  110_000,
            "annual_maint":    3_000,
            "life_years":         10,
        },
        "DC_350kW": {
            "ac_dc":         "DC",
            "power_kw":      350.0,
            # Finalized: purchase=$160,000, install-only=$225,000, life=10yr, O&M=$4,500/yr
            # Daily CapEx: [(160k+225k)/(10×12) + 4500/12] / 30.42 = $117.80/day
            "purchase_cost": 160_000,
            "install_cost":  225_000,
            "annual_maint":    4_500,
            "life_years":         10,
        },
    }


# ── Quick comparison printout ─────────────────────────────────────────────────

if __name__ == "__main__":
    DAYS_PER_MONTH = 30.42

    original = {
        "L2_19p2kW": dict(purchase_cost=2_500,   install_cost=5_000,  annual_maint=500,    life_years=10),
        "DC_50kW":   dict(purchase_cost=30_000,  install_cost=25_000, annual_maint=3_000,  life_years=8),
        "DC_150kW":  dict(purchase_cost=75_000,  install_cost=50_000, annual_maint=7_500,  life_years=8),
        "DC_350kW":  dict(purchase_cost=140_000, install_cost=90_000, annual_maint=14_000, life_years=8),
    }
    caltrans = build_charger_specs_caltrans()

    def daily(spec):
        mc = (spec["purchase_cost"] + spec["install_cost"]) / (spec["life_years"] * 12)
        mm = spec["annual_maint"] / 12
        return (mc + mm) / DAYS_PER_MONTH

    print(f"\n{'Type':<14} {'Orig $/day':>12} {'Caltrans $/day':>15} {'Change':>8}")
    print("-" * 55)
    for k in original:
        d_old = daily(original[k])
        d_new = daily(caltrans[k])
        pct   = (d_new - d_old) / d_old * 100
        print(f"{k:<14} {d_old:>12.2f} {d_new:>15.2f} {pct:>+7.1f}%")
    print()
    print("Caltrans detail:")
    for k, s in caltrans.items():
        d = daily(s)
        print(f"  {k:<14}  purchase=${s['purchase_cost']:>8,}  "
              f"install=${s['install_cost']:>7,}  "
              f"maint=${s['annual_maint']:>5,}/yr  "
              f"life={s['life_years']}yr  -> ${d:.2f}/charger/day")
