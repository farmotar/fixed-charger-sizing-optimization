"""
utility_rates.py
=================
Site-specific electricity rate structures for the Caltrans DCFC/XOS sizing project.

Site -> utility mapping:
    Northgate  -> SMUD          (C&I 21-299 kW, TOU)
    Fresno     -> PG&E          (BEV-2 Secondary, TOU)
    Glendale   -> PG&E BEV-2   (proxy — actual GWP tariff not obtained)
    San Diego  -> SDG&E         (EV-HP Secondary, TOU)

IMPORTANT - Glendale proxy:
    Glendale Water & Power's actual commercial tariff (Schedule LD-2 / PC-1)
    could not be retrieved. Per user direction (2026-07-01), PG&E BEV-2 is
    used as the proxy instead of SMUD (previous proxy). PG&E BEV-2 was chosen
    as it is a California IOU subscription-based EV tariff that avoids the
    two-tier demand structure of SMUD which is less representative of a
    Southern California municipal utility. All Glendale costs are estimates
    only — replace with actual GWP tariff when available (call GWP Customer
    Service at 855-550-4497, or obtain Schedule LD-2/PC-1 tariff PDF).
"""
from __future__ import annotations

import pandas as pd

TZ_PAC = "America/Los_Angeles"


# ═══════════════════════════════════════════════════════════════════════════
# 1. SMUD C&I 21-299 kW  (Northgate; also Glendale placeholder)
# ═══════════════════════════════════════════════════════════════════════════
_SMUD_DEMAND_GLOBAL   = 6.454   # $/kW-month
_SMUD_DEMAND_PEAK_WIN = 9.960   # $/kW-month


def smud_energy_rate(t_utc: pd.Timestamp) -> float:
    """SMUD C&I 21-299 kW TOU energy rate ($/kWh)."""
    t  = t_utc.tz_convert(TZ_PAC)
    h  = t.hour + t.minute / 60.0
    su = t.month in (6, 7, 8, 9)
    wk = t.weekday() < 5
    pk = wk and 16 <= h < 21
    sv = (not su) and 9 <= h < 16
    if su:
        return 0.2341 if pk else 0.1215
    return 0.1477 if pk else (0.0888 if sv else 0.1264)


def smud_capacity_charge(p_max_kw: float, p_peak_win_kw: float) -> dict:
    """SMUD monthly demand charges."""
    return {
        "demand_global_monthly":   p_max_kw * _SMUD_DEMAND_GLOBAL,
        "demand_peak_win_monthly": p_peak_win_kw * _SMUD_DEMAND_PEAK_WIN,
    }


def smud_is_peak_win(t_utc: pd.Timestamp) -> bool:
    t = t_utc.tz_convert(TZ_PAC)
    h = t.hour + t.minute / 60.0
    return t.weekday() < 5 and 16 <= h < 21


# ═══════════════════════════════════════════════════════════════════════════
# 2. PG&E BEV-2 Secondary  (Fresno)
# ═══════════════════════════════════════════════════════════════════════════
_PGE_SUBSCRIPTION = 1.91   # $/kW-month (subscribed capacity)
_PGE_OVERAGE      = 3.82   # $/kW (over subscribed capacity, not modeled -- assume subscribed = peak)


def pge_bev2_energy_rate(t_utc: pd.Timestamp) -> float:
    """PG&E BEV-2 Secondary TOU energy rate ($/kWh).

    Peak:           16:00-21:00, every day        $0.36977
    Super Off-Peak:  9:00-14:00, every day         $0.13327
    Off-Peak:        all other hours               $0.15654
    """
    t = t_utc.tz_convert(TZ_PAC)
    h = t.hour + t.minute / 60.0
    if 16 <= h < 21:
        return 0.36977
    if 9 <= h < 14:
        return 0.13327
    return 0.15654


def pge_bev2_capacity_charge(p_max_kw: float) -> dict:
    """PG&E BEV-2 subscription charge (monthly). Assumes subscribed kW = peak kW (no overage)."""
    return {
        "subscription_monthly": p_max_kw * _PGE_SUBSCRIPTION,
        "overage_monthly":      0.0,
    }


def pge_bev2_is_peak_win(t_utc: pd.Timestamp) -> bool:
    t = t_utc.tz_convert(TZ_PAC)
    h = t.hour + t.minute / 60.0
    return 16 <= h < 21


# ═══════════════════════════════════════════════════════════════════════════
# 3. SDG&E EV-HP Secondary  (San Diego)
# ═══════════════════════════════════════════════════════════════════════════
_SDGE_SUBSCRIPTION = 4.81  # $/kW-month (subscribed capacity)


def sdge_evhp_energy_rate(t_utc: pd.Timestamp) -> float:
    """SDG&E EV-HP Secondary TOU energy rate ($/kWh).

    On-Peak:        16:00-21:00, every day (summer + winter)
    Super Off-Peak:  00:00-06:00, every day   <- assumed window; SDG&E EV-HP
                     publishes a super off-peak period but exact hours were
                     not in the supplied appendix text, using the standard
                     SDG&E residential/commercial EV midnight-6am window.
    Off-Peak:        all other hours

    Summer (Jun-Oct): On $0.29036, Off $0.12828, Super-Off $0.12089
    Winter (Nov-May): On $0.30199, Off $0.13067, Super-Off $0.11588
    """
    t  = t_utc.tz_convert(TZ_PAC)
    h  = t.hour + t.minute / 60.0
    su = t.month in (6, 7, 8, 9, 10)
    on_peak   = 16 <= h < 21
    super_off = h < 6
    if su:
        if on_peak:
            return 0.29036
        return 0.12089 if super_off else 0.12828
    if on_peak:
        return 0.30199
    return 0.11588 if super_off else 0.13067


def sdge_evhp_capacity_charge(p_max_kw: float) -> dict:
    """SDG&E EV-HP subscription charge (monthly). Assumes subscribed kW = peak kW."""
    return {
        "subscription_monthly": p_max_kw * _SDGE_SUBSCRIPTION,
        "overage_monthly":      0.0,
    }


def sdge_evhp_is_peak_win(t_utc: pd.Timestamp) -> bool:
    t = t_utc.tz_convert(TZ_PAC)
    h = t.hour + t.minute / 60.0
    return 16 <= h < 21


# ═══════════════════════════════════════════════════════════════════════════
# 4. Glendale Water & Power  (Glendale) -- ** PLACEHOLDER: mirrors SMUD **
# ═══════════════════════════════════════════════════════════════════════════
gwp_energy_rate      = smud_energy_rate
gwp_capacity_charge  = smud_capacity_charge
gwp_is_peak_win       = smud_is_peak_win
GWP_IS_PLACEHOLDER = True


# ═══════════════════════════════════════════════════════════════════════════
# Router
# ═══════════════════════════════════════════════════════════════════════════
SITE_UTILITY = {
    "northgate": "smud",
    "fresno":    "pge_bev2",
    "glendale":  "pge_bev2",   # proxy — actual GWP tariff not obtained; was SMUD proxy until 2026-07-01
    "san_diego": "sdge_evhp",
}

# scenario to use as the "headline" / reported scenario for each site
SITE_SCENARIO = {
    "northgate": "A1",   # always grid-connected per user direction
    "fresno":    "A2",
    "glendale":  "A2",
    "san_diego": "A2",
}

_ENERGY_RATE_FN = {
    "smud":      smud_energy_rate,
    "pge_bev2":  pge_bev2_energy_rate,
    "gwp":       gwp_energy_rate,
    "sdge_evhp": sdge_evhp_energy_rate,
}

_PEAK_WIN_FN = {
    "smud":      smud_is_peak_win,
    "pge_bev2":  pge_bev2_is_peak_win,
    "gwp":       gwp_is_peak_win,
    "sdge_evhp": sdge_evhp_is_peak_win,
}

# capacity charge fn takes (p_max_kw) except SMUD which takes (p_max_kw, p_peak_win_kw)
_CAPACITY_FN = {
    "smud":      smud_capacity_charge,
    "pge_bev2":  pge_bev2_capacity_charge,
    "gwp":       gwp_capacity_charge,
    "sdge_evhp": sdge_evhp_capacity_charge,
}

# label for the monthly capacity charge $ figure (SMUD has two components, others one)
CAPACITY_CHARGE_LABEL = {
    "smud":      "demand charge",
    "pge_bev2":  "subscription charge",
    "gwp":       "demand charge (SMUD proxy)",
    "sdge_evhp": "subscription charge",
}


def energy_rate_fn(site: str):
    return _ENERGY_RATE_FN[SITE_UTILITY[site]]


def peak_win_fn(site: str):
    return _PEAK_WIN_FN[SITE_UTILITY[site]]


def capacity_charge(site: str, p_max_kw: float, p_peak_win_kw: float) -> dict:
    util = SITE_UTILITY[site]
    if util == "smud" or util == "gwp":
        return _CAPACITY_FN[util](p_max_kw, p_peak_win_kw)
    return _CAPACITY_FN[util](p_max_kw)
