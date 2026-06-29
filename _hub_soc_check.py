import sys, pandas as pd
sys.stdout.reconfigure(encoding='utf-8')
TZ = 'America/Los_Angeles'

disp  = pd.read_csv(r'scenario_outputs\northgate_2025_07_17\xos_a2_fixed\scenario_A2_dispatch_2025-07-17.csv')
state = pd.read_csv(r'scenario_outputs\northgate_2025_07_17\xos_a2_fixed\scenario_A2_state_2025-07-17.csv')
disp['time_utc']  = pd.to_datetime(disp['time_utc'], utc=True)
state['time_utc'] = pd.to_datetime(state['time_utc'], utc=True)
disp['time_pac']  = disp['time_utc'].dt.tz_convert(TZ)
state['time_pac'] = state['time_utc'].dt.tz_convert(TZ)

B = 282; SOC_MIN = 0.20; ETA_D = 0.95
midday = pd.Timestamp('2025-07-17 13:00:00', tz=TZ)

print("Hub  Morning vehicles               SOC@13:00  Avail kWh  Recharged?  Notes")
print("-"*100)

for hub_k in range(16):
    col_st  = "state_unit_{}".format(hub_k)
    col_soc = "soc_unit_{}".format(hub_k)
    if col_st not in state.columns:
        continue

    hub_disp = disp[disp['unit'] == hub_k]
    if hub_disp.empty:
        continue

    # vehicles served (show as v-numbers)
    vehs = hub_disp['event_id'].unique().tolist()
    veh_labels = ", ".join(["v" + e.split('_v')[-1] for e in vehs])

    # State/SOC at midday
    state_mid = state[state['time_pac'] <= midday].iloc[-1]
    soc_mid   = state_mid[col_soc]
    avail_mid = max(0.0, (soc_mid - SOC_MIN) * B * ETA_D)
    st_mid    = state_mid[col_st]

    # Did it ever recharge?
    recharged = (state[col_st] == 'recharging').any()

    # When did morning service end (last dispatch step before midday)?
    morn_disp = hub_disp[hub_disp['time_pac'] <= midday]
    if not morn_disp.empty:
        last_morn = morn_disp.iloc[-1]
        soc_after_morn = last_morn['soc_after']
        avail_after_morn = max(0.0, (soc_after_morn - SOC_MIN) * B * ETA_D)
    else:
        soc_after_morn = soc_mid
        avail_after_morn = avail_mid

    note = "RECHARGE TRIGGERED" if recharged else "Sat idle — no recharge all day"
    print("Hub{:2d}  {:32s}  {:6.1f}%    {:7.0f} kWh  {:10s}  {}".format(
        hub_k+1, veh_labels[:32], soc_after_morn*100, avail_after_morn,
        "YES" if recharged else "NO", note))

print()
print("Midday gap (no vehicle demand): ~10:30 to 13:45 Pacific = 3h15min window")
print("Full recharge from 20% SOC: 2.86h  |  From 30%: ~2.4h  |  From 45%: ~1.7h")
