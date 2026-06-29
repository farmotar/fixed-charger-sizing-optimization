import sys, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
from charger_costs_xos_hub import electrical_infra_cost
import scenario_runner as sr

mix = pd.DataFrame([
    {"charger_type": "Kempower_50kW",  "count": 1},
    {"charger_type": "Kempower_150kW", "count": 2},
    {"charger_type": "Kempower_250kW", "count": 4},
])
n_kmp = 7
kmp_lo = sr._kempower_elec_cost(n_kmp, mix, "low")
kmp_md = sr._kempower_elec_cost(n_kmp, mix, "mid")
kmp_hi = sr._kempower_elec_cost(n_kmp, mix, "high")

K_xos = 16
xos_lo = electrical_infra_cost(K_xos, "low")
xos_md = electrical_infra_cost(K_xos, "mid")
xos_hi = electrical_infra_cost(K_xos, "high")

print("=== Kempower 7-unit building-side electrical infra ===")
print(f"  Shared (panel/switchboard/permit): ${kmp_md['shared_infra']:>8,.0f}")
print(f"  Circuits x7 (mix 50/150/250kW)  : ${kmp_md['circuit_cost']:>8,.0f}")
nt = kmp_md['n_tier_upgrades']
print(f"  Tier upgrades (x{nt})             : ${kmp_md['tier_upgrades']:>8,.0f}")
print(f"  TOTAL building-side              : low=${kmp_lo['total']:>8,.0f}  mid=${kmp_md['total']:>8,.0f}  high=${kmp_hi['total']:>8,.0f}")
print(f"  Peak simultaneous draw           : 1+2+4 = 7 chargers x avg 222kW = 1,350 kW")

print()
print("=== XOS Hub 16-unit building-side electrical infra ===")
print(f"  Shared (panel/switchboard/permit): ${xos_md['shared_infra']:>8,.0f}")
print(f"  Circuits x16 (480V 3ph 100A each): ${xos_md['circuit_cost']:>8,.0f}")
nt2 = xos_md['n_tier_upgrades']
print(f"  Tier upgrades (x{nt2})             : ${xos_md['tier_upgrades']:>8,.0f}")
print(f"  TOTAL building-side              : low=${xos_lo['total']:>8,.0f}  mid=${xos_md['total']:>8,.0f}  high=${xos_hi['total']:>8,.0f}")
print(f"  Peak theoretical draw            : 16 x 83 kW = {16*83:,} kW (all recharge at once)")
print(f"  Scheduled 4 hubs at a time       : 4 x 83 kW = {4*83} kW")
print(f"  Observed July 17 peak            : 166 kW (2 hubs simultaneously)")

print()
diff_mid = xos_md["total"] - kmp_md["total"]
print(f"=== Building-side cost delta (XOS - Kempower, mid) ===")
print(f"  ${diff_mid:+,.0f}  ({'XOS costs more' if diff_mid>0 else 'XOS costs less'})")

print()
print("=== Utility-side upgrade (NOT in current model) ===")
print("  Kempower 1,350 kW service:")
print("    - New 1.5-2 MVA transformer at site pad")
print("    - New switchgear / service entrance (>1000A at 480V 3-phase)")
print("    - Feeder upgrade if SMUD circuit is undersized")
print("    - Rough estimate: $300,000 - $2,000,000+ depending on feeder distance")
print()
print("  XOS Hub 332 kW service (4 hubs at a time, scheduled):")
print("    - Standard 480V commercial 3-phase service upgrade")
print("    - Likely already available or minor transformer tap upgrade")
print("    - Rough estimate: $50,000 - $300,000")
print()
print("  SMUD demand charge comparison (July 17 peak day):")
print(f"  Kempower peak draw: 1,350 kW -> demand charge: 1,350 x $6.454 = ${1350*6.454:,.0f}/day global")
print(f"  XOS Hub peak draw : 166 kW  -> demand charge:  166 x $6.454 = ${166*6.454:,.0f}/day global")
print(f"  XOS peak-window   : recharge can be scheduled OFF-PEAK -> peak-window demand ~$0")
print(f"  Kempower peak-window possible: 1,350 kW during 16:00-21:00 -> {1350*9.960:,.0f}/kW-month")
