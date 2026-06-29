"""
Full K-sweep coverage analysis for Shima.
Runs K=1..20 across all 310 days, records days fully/partially/uncovered.
Saves: scenario_outputs/northgate_analysis/worst_days/k_sweep_coverage.csv
       scenario_outputs/northgate_analysis/worst_days/k_sweep_coverage.png
"""
import io, sys, contextlib
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')
import scenario_runner as sr
from pathlib import Path
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

ENERGY_TOL = 0.05
BASE_DIR = Path(r'D:\Geotab_EV_Parameters\charger_sizing_test')
OUT_DIR  = BASE_DIR / 'scenario_outputs/northgate_analysis/worst_days'
CSV_STEM = 'z2z_milp_events_northgate'

def run_k(events_ext, K):
    with contextlib.redirect_stdout(io.StringIO()):
        sim = sr._simulate_xos(events_ext, K, mode='a2')
    delivered = sim['delivered']; remaining = sim['remaining']
    n_full    = sum(1 for v in delivered if remaining.get(v, 0) <= ENERGY_TOL)
    n_partial = sum(1 for v in delivered if delivered[v] > ENERGY_TOL and remaining.get(v, 0) > ENERGY_TOL)
    n_unserved= sum(1 for v in delivered if delivered[v] <= ENERGY_TOL)
    e_del = sum(delivered.values())
    e_unmet = sum(r for r in remaining.values() if r > ENERGY_TOL)
    return n_full, n_partial, n_unserved, e_del, e_del + e_unmet

all_csv = sorted(BASE_DIR.glob(f'{CSV_STEM}_*.csv'))
K_RANGE = list(range(1, 21))

# Pre-load all events (once)
print(f'Loading events for {len(all_csv)} days...')
day_events = {}
for csv_path in all_csv:
    date_tag  = csv_path.stem.split('northgate_')[-1]
    d_str     = date_tag.replace('_', '-')[:10]
    stem_parts    = csv_path.stem.rsplit('_', 3)
    site_csv_stem = '_'.join(stem_parts[:-3]) if len(stem_parts) > 3 else csv_path.stem
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            ev = sr.load_site_day_data(csv_path)
            ev = sr.apply_multiday_rule(ev, d_str, site_csv_dir=csv_path.parent,
                                         site_csv_stem=site_csv_stem)
            ev = sr._xos_extended_dwell(ev)
        day_events[d_str] = ev
    except Exception:
        pass

print(f'Loaded {len(day_events)} days successfully.')

# K sweep
rows = []
for K in K_RANGE:
    fully = partial = uncov = total_del = total_dem = 0
    for d_str, ev in day_events.items():
        try:
            nf, np_, nu, ed, em = run_k(ev, K)
            if nu + np_ == 0:
                fully += 1
            elif nu > 0:
                uncov += 1
            else:
                partial += 1
            total_del += ed; total_dem += em
        except Exception:
            pass
    n_days = len(day_events)
    rows.append({
        'K': K,
        'days_fully_covered': fully,
        'days_partial': partial,
        'days_uncovered': uncov,
        'pct_fully_covered': 100*fully/n_days,
        'pct_partial': 100*partial/n_days,
        'overall_energy_svc_pct': 100*total_del/total_dem if total_dem else 0,
    })
    print(f'  K={K:2d}: full={fully:3d} ({100*fully/n_days:.0f}%)  partial={partial:3d}  uncov={uncov}  energy_svc={100*total_del/total_dem:.1f}%')

df = pd.DataFrame(rows)
df.to_csv(OUT_DIR / 'k_sweep_coverage.csv', index=False)
print(f'\nSaved: k_sweep_coverage.csv')

# Plot
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 9))
fig.suptitle('Northgate — XOS Hub Coverage vs Fleet Size (K)\n'
             'A2 scenario, proactive recharge, all 307 operating days',
             fontsize=13, fontweight='bold')

n = len(day_events)
ax1.bar(df['K'] - 0.25, df['days_fully_covered'], 0.5, label='Days 100% served', color='#2166ac', alpha=0.85)
ax1.bar(df['K'] + 0.25, df['days_partial'],        0.5, label='Days partial',    color='#f4a582', alpha=0.85)

# Mark the worst-day K thresholds
for kk, lbl, clr in [(14, 'Top10 rank 7-10\nK=14', '#d73027'),
                      (15, 'rank 5-6\nK=15', '#ff7f00'),
                      (16, 'rank 2-4\nK=16', '#984ea3'),
                      (17, 'rank 1\nK=17',   '#4dac26')]:
    ax1.axvline(kk, color=clr, linewidth=1.8, linestyle='--', alpha=0.70)
    r = df[df['K']==kk].iloc[0]
    ax1.text(kk+0.05, n*0.93 - list(K_RANGE).index(kk)*n*0.06,
             lbl, color=clr, fontsize=7.5, va='top')

ax1.set_xticks(K_RANGE)
ax1.set_ylabel('Number of days (out of 307)', fontsize=10)
ax1.set_title('Days fully vs partially served per fleet size K', fontsize=11)
ax1.legend(loc='lower right', fontsize=9)
ax1.grid(axis='y', linestyle=':', alpha=0.4)
ax1.set_ylim(0, n * 1.08)

ax2.plot(df['K'], df['pct_fully_covered'],   color='#2166ac', linewidth=2.5, marker='o', ms=6, label='Days 100% served (%)')
ax2.plot(df['K'], df['overall_energy_svc_pct'], color='#d73027', linewidth=2.0, marker='s', ms=5,
         linestyle='--', label='Overall energy service (%)')
ax2.axhline(100, color='gray', linewidth=0.8, linestyle=':')
for kk in (14, 15, 16, 17):
    ax2.axvline(kk, color='gray', linewidth=1.2, linestyle='--', alpha=0.55)
ax2.set_xticks(K_RANGE)
ax2.set_xlabel('Number of XOS Hub MC02 units (K)', fontsize=10)
ax2.set_ylabel('Coverage (%)', fontsize=10)
ax2.set_title('Coverage percentage vs K — note plateau above K≈13 (dwell-window ceiling)', fontsize=11)
ax2.legend(loc='lower right', fontsize=9)
ax2.grid(axis='both', linestyle=':', alpha=0.4)
ax2.set_ylim(0, 105)
ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'{v:.0f}%'))

# Annotate the ceiling
ceil_k = df[df['pct_fully_covered'] == df['pct_fully_covered'].max()]['K'].min()
ax2.annotate(
    f"Coverage ceiling: {df['pct_fully_covered'].max():.0f}%\n(dwell-window limited — no hub count can fix this)",
    xy=(ceil_k, df['pct_fully_covered'].max()),
    xytext=(ceil_k + 1.5, df['pct_fully_covered'].max() - 12),
    fontsize=8.5, arrowprops=dict(arrowstyle='->', color='#333'),
    color='#2166ac'
)

fig.tight_layout()
fig.savefig(OUT_DIR / 'k_sweep_coverage.png', dpi=150, bbox_inches='tight')
plt.close(fig)
print('Saved: k_sweep_coverage.png')
