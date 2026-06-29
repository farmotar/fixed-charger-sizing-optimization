"""
generate_fixed_charger_presentation.py
========================================
Generates a professional PDF presentation of the fixed DCFC charger
optimization results for all 4 Caltrans sites.

Slides produced:
  1.  Title / Executive Summary
  2.  Finalized Cost Assumptions
  3.  Methodology Overview
  4.  Site Overview -- all 4 sites at a glance
  5.  Northgate -- optimal config + full-year stats
  6.  Northgate -- 10 worst days config comparison table
  7.  Fresno -- optimal config + full-year stats
  8.  Fresno -- 10 worst days config comparison table
  9.  Glendale -- optimal config + full-year stats
  10. Glendale -- 10 worst days config comparison table
  11. San Diego -- optimal config + full-year stats
  12. San Diego -- 10 worst days config comparison table
  13. Final Comparison -- worst-day robustness (all sites)
  14. Conclusions & Recommendations

Output: fixed_charger_outputs/Fixed_Charger_Optimization_Report.pdf
"""

from __future__ import annotations
import sys
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np

from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.units import inch
from reportlab.lib.colors import (
    HexColor, white, black, Color,
    lightgrey, darkgrey
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, HRFlowable, KeepTogether
)
from reportlab.platypus.flowables import BalancedColumns
from reportlab.lib import colors

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE   = Path(r"D:\Geotab_EV_Parameters\charger_sizing_test")
OUTDIR = BASE / "fixed_charger_outputs"
PDF    = OUTDIR / "Fixed_Charger_Optimization_Report.pdf"

# ── Brand palette ──────────────────────────────────────────────────────────────
NAVY      = HexColor("#0D2B55")   # dark navy  (headers, titles)
TEAL      = HexColor("#1A6B7C")   # teal       (section banners)
GOLD      = HexColor("#C8922A")   # gold       (accent / selected)
MID_BLUE  = HexColor("#3A7EC2")   # mid blue   (alternating rows)
LIGHT_BG  = HexColor("#EEF4FB")   # very light blue (alt rows)
OFFWHITE  = HexColor("#F7F7F7")
RED_SOFT  = HexColor("#C0392B")
GREEN_OK  = HexColor("#27AE60")
GREY_LINE = HexColor("#BDC3C7")

# ── Page setup (landscape letter) ─────────────────────────────────────────────
PW, PH = landscape(letter)   # 11 x 8.5 in
MARGIN  = 0.55 * inch

# ══════════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════════════════════

SITES = {
    "northgate": "Northgate",
    "fresno":    "Fresno",
    "glendale":  "Glendale",
    "san_diego": "San Diego",
}

SELECTED_CONFIGS = {
    "northgate": "2xDC 150 kW",
    "fresno":    "2xDC 50 kW",
    "glendale":  "1xL2 (19.2 kW)",
    "san_diego": "2xDC 150 kW",
}

CONFIGS_ORDER = [
    "1xL2 (19.2 kW)",
    "1xDC 50 kW",
    "2xDC 50 kW",
    "1xDC 150 kW",
    "2xDC 150 kW",
    "1xDC 350 kW",
]

CHARGER_SPECS = {
    "L2_19p2kW": dict(label="Level 2 AC",      power=19.2,  purchase=11_000, install=14_000, life=9, om=550,   daily=9.12),
    "DC_50kW":   dict(label="Low-power DCFC",   power=50.0,  purchase=50_000, install=50_000, life=9, om=1_750, daily=35.23),
    "DC_150kW":  dict(label="Med-power DCFC",   power=150.0, purchase=90_000, install=110_000,life=9, om=3_000, daily=69.09),
    "DC_350kW":  dict(label="High-power DCFC",  power=350.0, purchase=160_000,install=225_000,life=9, om=4_500, daily=129.51),
}


def load_results():
    data = {}
    for site in SITES:
        try:
            data[site] = {
                "w10":  pd.read_csv(OUTDIR / f"{site}_worst10_all_configs.csv"),
                "rank": pd.read_csv(OUTDIR / f"{site}_all_days_optimal_ranking.csv"),
                "all":  pd.read_csv(OUTDIR / f"{site}_all_days_config_analysis.csv"),
            }
        except FileNotFoundError as e:
            print(f"WARNING: {e}")
            data[site] = {}
    return data


# ══════════════════════════════════════════════════════════════════════════════
# STYLE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def styles():
    ss = getSampleStyleSheet()
    base = dict(fontName="Helvetica", leading=14)

    def S(name, **kw):
        return ParagraphStyle(name, parent=ss["Normal"], **{**base, **kw})

    return {
        "slide_title":  S("ST",  fontName="Helvetica-Bold", fontSize=22,
                          textColor=white, alignment=TA_CENTER, leading=28),
        "slide_sub":    S("SS",  fontName="Helvetica",      fontSize=13,
                          textColor=HexColor("#D0E8FF"), alignment=TA_CENTER, leading=18),
        "section_hdr":  S("SH",  fontName="Helvetica-Bold", fontSize=14,
                          textColor=white, alignment=TA_LEFT,   leading=18),
        "body":         S("BD",  fontSize=10, textColor=black, leading=14),
        "body_c":       S("BC",  fontSize=10, textColor=black, alignment=TA_CENTER, leading=14),
        "body_r":       S("BR",  fontSize=10, textColor=black, alignment=TA_RIGHT,  leading=14),
        "bold":         S("BL",  fontName="Helvetica-Bold", fontSize=10,
                          textColor=black, leading=14),
        "small":        S("SM",  fontSize=8,  textColor=darkgrey, leading=11),
        "small_c":      S("SMC", fontSize=8,  textColor=darkgrey,
                          alignment=TA_CENTER, leading=11),
        "caption":      S("CA",  fontSize=9,  textColor=TEAL,
                          fontName="Helvetica-Oblique", leading=12),
        "h2":           S("H2",  fontName="Helvetica-Bold", fontSize=12,
                          textColor=NAVY, leading=16),
        "bullet":       S("BU",  fontSize=10, textColor=black,
                          leftIndent=14, bulletIndent=0, leading=15),
        "green":        S("GR",  fontSize=10, textColor=GREEN_OK,
                          fontName="Helvetica-Bold", alignment=TA_CENTER, leading=14),
        "red":          S("RD",  fontSize=10, textColor=RED_SOFT,
                          fontName="Helvetica-Bold", alignment=TA_CENTER, leading=14),
        "gold_c":       S("GC",  fontSize=10, textColor=GOLD,
                          fontName="Helvetica-Bold", alignment=TA_CENTER, leading=14),
    }


ST = styles()


def p(text, style="body"):
    return Paragraph(str(text), ST[style])


def sp(h=6):
    return Spacer(1, h)


def hr():
    return HRFlowable(width="100%", thickness=1, color=GREY_LINE, spaceAfter=4, spaceBefore=4)


def banner(text, color=TEAL, text_style="section_hdr"):
    t = Table([[p(text, text_style)]], colWidths=[PW - 2 * MARGIN])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), color),
        ("TOPPADDING",    (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
    ]))
    return t


def title_block(title, subtitle=""):
    """Full-width navy title block for slide top."""
    rows = [[p(title, "slide_title")]]
    if subtitle:
        rows.append([p(subtitle, "slide_sub")])
    t = Table(rows, colWidths=[PW - 2 * MARGIN])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("TOPPADDING",    (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING",   (0, 0), (-1, -1), 12),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 12),
    ]))
    return t


def footer_text():
    return p(f"Caltrans EV Fleet Charging Infrastructure Study  |  Generated {datetime.now().strftime('%B %d, %Y')}  |  CONFIDENTIAL DRAFT",
             "small_c")


# ══════════════════════════════════════════════════════════════════════════════
# TABLE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def config_color(cfg, selected):
    """Gold for selected config, else alternating light/white."""
    if cfg == selected:
        return HexColor("#FEF9E7")
    return None   # caller sets alternating


def pct_style(val):
    """Return style name based on percentage value."""
    if val >= 95:  return "green"
    if val >= 75:  return "body_c"
    return "red"


def make_worst10_table(df_w10, selected_cfg, site_label, col_w=None):
    """Build a compact worst-10-days × config comparison table."""
    if df_w10.empty:
        return p("No data available.")

    dates = df_w10["date"].unique().tolist()
    # Sort by cost under selected config (descending)
    sel_rows = df_w10[df_w10["config"] == selected_cfg].sort_values("total_cost", ascending=False)
    date_order = {d: i for i, d in enumerate(sel_rows["date"].tolist())}
    dates_sorted = sorted(dates, key=lambda d: date_order.get(d, 99))[:10]

    # Header row
    hdr = ["Date", "Rank", "Configuration",
           "Total Cost", "E Demand\n(kWh)", "E Served\n(kWh)", "Dmnd\nSvc%",
           "Vehicles", "Veh\nServed", "Veh\nSvc%"]

    rows = [hdr]
    cmd_colors = []

    for rank, date in enumerate(dates_sorted, 1):
        day = df_w10[df_w10["date"] == date]
        first = True
        for cfg in CONFIGS_ORDER:
            r = day[day["config"] == cfg]
            if r.empty:
                continue
            r = r.iloc[0]
            star = "*" if cfg == selected_cfg else " "
            date_col = date if first else ""
            rank_col = f"#{rank}" if first else ""

            veh_pct = r["vehicles_served_pct"]
            dmnd_pct = r["demand_served_pct"]

            rows.append([
                p(date_col, "small_c"),
                p(rank_col, "small_c"),
                p(f"{star} {cfg}", "small"),
                p(f"${r['total_cost']:,.0f}", "small_c"),
                p(f"{r['energy_demanded_kwh']:,.0f}", "small_c"),
                p(f"{r['energy_served_kwh']:,.0f}", "small_c"),
                p(f"{dmnd_pct:.0f}%",  "small_c"),
                p(f"{int(r['n_vehicles'])}",        "small_c"),
                p(f"{int(r['n_vehicles_served'])}",   "small_c"),
                p(f"{veh_pct:.0f}%",  "small_c"),
            ])
            is_sel = (cfg == selected_cfg)
            cmd_colors.append((rank, is_sel, veh_pct >= 95))
            first = False

    # Column widths
    total_w = PW - 2 * MARGIN
    cw = col_w or [
        0.82 * inch,  # date
        0.38 * inch,  # rank
        1.38 * inch,  # config
        0.82 * inch,  # cost
        0.78 * inch,  # e demand
        0.78 * inch,  # e served
        0.58 * inch,  # dmnd%
        0.62 * inch,  # vehicles
        0.58 * inch,  # veh served
        0.58 * inch,  # veh%
    ]
    # Scale to fit
    scale = total_w / sum(cw)
    cw = [c * scale for c in cw]

    t = Table(rows, colWidths=cw, repeatRows=1)

    ts = [
        # Header
        ("BACKGROUND",  (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR",   (0, 0), (-1, 0), white),
        ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, 0), 8),
        ("ALIGN",       (0, 0), (-1, 0), "CENTER"),
        ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",  (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
        ("GRID",        (0, 0), (-1, -1), 0.4, GREY_LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING",(0, 0), (-1, -1), 3),
    ]

    # Colour data rows
    i_row = 1
    for rank, is_sel, full_svc in cmd_colors:
        bg = HexColor("#FEF3CD") if is_sel else (LIGHT_BG if rank % 2 == 0 else white)
        ts.append(("BACKGROUND", (0, i_row), (-1, i_row), bg))
        if is_sel:
            ts.append(("FONTNAME", (0, i_row), (-1, i_row), "Helvetica-Bold"))
            ts.append(("TEXTCOLOR", (2, i_row), (2, i_row), GOLD))
        i_row += 1

    t.setStyle(TableStyle(ts))
    return t


# ══════════════════════════════════════════════════════════════════════════════
# SLIDE BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def slide_title_page(story):
    story.append(sp(40))
    t = Table([[p("Caltrans EV Fleet", "slide_title")],
               [p("Fixed DCFC Charger Sizing Optimization", "slide_title")],
               [p("All 4 Maintenance Stations  |  Full-Year Analysis", "slide_sub")],
               [p(" ", "slide_sub")],
               [p("Using Finalized Cost Assumptions", "slide_sub")],
               [p(f"Generated: {datetime.now().strftime('%B %d, %Y')}", "slide_sub")]],
              colWidths=[PW - 2 * MARGIN])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), NAVY),
        ("TOPPADDING",    (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING",   (0, 0), (-1, -1), 20),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 20),
        ("LINEBELOW",     (0, 1), (-1, 1), 1.5, GOLD),
    ]))
    story.append(t)
    story.append(sp(18))

    bullets = [
        "Greedy simulation across 1,217 operating days (May 2025 - Apr 2026)",
        "Six charger configurations tested per day per site",
        "10 worst days identified per site, analysed under all configurations",
        "Cost model: SMUD C&I Secondary TOD energy rates + demand charges",
    ]
    items = [[p(f"  •  {b}", "body")] for b in bullets]
    bt = Table(items, colWidths=[PW - 2 * MARGIN])
    bt.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HexColor("#F0F4FA")),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 16),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 16),
        ("BOX",           (0, 0), (-1, -1), 1, TEAL),
    ]))
    story.append(bt)
    story.append(sp(14))
    story.append(footer_text())
    story.append(PageBreak())


def slide_cost_assumptions(story):
    story.append(title_block("Finalized Charger Cost Assumptions",
                             "Applied consistently across all 4 Caltrans sites"))
    story.append(sp(10))

    hdr = ["Charger Type", "Power (kW)", "Purchase Cost", "Installation Cost",
           "$/kW", "Lifespan", "O&M + Network", "Daily CapEx"]
    data = [hdr,
            ["Level 2 AC",         "19.2 kW", "$11,000",  "$14,000",  "$1,316", "9 years", "$550/port-yr",    "$9.12/day"],
            ["Low-power DCFC",     "50 kW",   "$50,000",  "$50,000",  "$2,000", "9 years", "$1,750/disp-yr",  "$35.23/day"],
            ["Medium-power DCFC",  "150 kW",  "$90,000",  "$110,000", "$1,333", "9 years", "$3,000/disp-yr",  "$69.09/day"],
            ["High-power DCFC",    "350 kW",  "$160,000", "$225,000", "$1,100", "9 years", "$4,500/disp-yr",  "$129.51/day"],
            ]

    row_colors = [NAVY, white, LIGHT_BG, white, LIGHT_BG]
    txt_colors = [white, black, black, black, black]

    styled = []
    for i, row in enumerate(data):
        align = TA_CENTER if i > 0 else TA_CENTER
        if i == 0:
            styled.append([p(c, "section_hdr") for c in row])
        else:
            styled.append([p(row[0], "bold")] + [p(c, "body_c") for c in row[1:]])

    cw_total = PW - 2 * MARGIN
    cw = [c * cw_total for c in [0.19, 0.09, 0.12, 0.14, 0.08, 0.09, 0.15, 0.14]]
    t = Table(styled, colWidths=cw)
    ts = [
        ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR",     (0, 0), (-1, 0), white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("GRID",          (0, 0), (-1, -1), 0.5, GREY_LINE),
        ("BACKGROUND",    (0, 2), (-1, 2), LIGHT_BG),
        ("BACKGROUND",    (0, 4), (-1, 4), LIGHT_BG),
        ("FONTNAME",      (0, 1), (0, -1), "Helvetica-Bold"),
        ("LINEBELOW",     (0, -1), (-1, -1), 1, TEAL),
    ]
    t.setStyle(TableStyle(ts))
    story.append(t)

    story.append(sp(14))
    story.append(banner("  Daily CapEx formula:   C_daily = [ (Purchase + Install) / (Life x 12 months)  +  Annual O&M / 12 ]  /  30.42 days/month",
                        color=HexColor("#EEF4FB"), text_style="body"))
    story.append(sp(8))
    story.append(p("Note: Demand charges billed on daily peak as a planning proxy for monthly demand charges.", "small"))
    story.append(p("Energy rates: SMUD C&I Secondary 21-299 kW TOD -- Summer peak $0.2341/kWh (wkdy 4-9pm) to off-peak $0.0888/kWh.", "small"))
    story.append(sp(8))
    story.append(footer_text())
    story.append(PageBreak())


def slide_methodology(story):
    story.append(title_block("Methodology",
                             "Greedy simulation -- full year across all sites and configurations"))
    story.append(sp(10))

    left = [
        banner("  Simulation Approach", TEAL),
        sp(6),
        p("For each operating day, a 5-minute resolution greedy simulation was run:", "body"),
        sp(4),
        p("1.  Build UTC time grid (arrival floor -> departure ceil, 5-min steps)", "bullet"),
        p("2.  Compute effective vehicle power: min(charger kW, on-board AC/DC limit)", "bullet"),
        p("3.  At each step, assign up to N chargers to vehicles with most remaining need", "bullet"),
        p("4.  Track energy delivered, power draw, and residual unmet demand", "bullet"),
        p("5.  Compute daily cost = CapEx + energy cost + demand charges", "bullet"),
        sp(8),
        banner("  Optimal Config Selection (per day)", TEAL),
        sp(6),
        p("Priority: (1) highest vehicle service %, (2) lowest total cost, (3) highest energy served %", "body"),
        sp(4),
        p("Site-level recommendation = most-frequently-selected config across all days", "body"),
    ]

    right = [
        banner("  Cost Components", TEAL),
        sp(6),
        p("Daily CapEx", "bold"),
        p("Annualised ownership cost (purchase + install amortised over life + O&M)", "body"),
        sp(4),
        p("Energy Cost", "bold"),
        p("SMUD TOD rates applied per 5-min step ($/kWh x kWh consumed)", "body"),
        sp(4),
        p("Global Demand Charge", "bold"),
        p("$6.454/kW x daily peak site power (infrastructure charge proxy)", "body"),
        sp(4),
        p("Peak-Window Demand Charge", "bold"),
        p("$9.960/kW x peak power 4:00-9:00 PM local time (SMUD summer peak window)", "body"),
        sp(10),
        banner("  Configurations Tested", TEAL),
        sp(6),
    ]
    cfg_rows = [
        ["1x L2 AC (19.2 kW)", "$9.12/day"],
        ["1x DC 50 kW",         "$35.23/day"],
        ["2x DC 50 kW",         "$70.46/day"],
        ["1x DC 150 kW",        "$69.09/day"],
        ["2x DC 150 kW",       "$138.18/day"],
        ["1x DC 350 kW",       "$129.51/day"],
    ]
    ct = Table(cfg_rows, colWidths=[2.4 * inch, 1.2 * inch])
    ct.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), white),
        ("ROWBACKGROUNDS",(0, 0), (-1, -1), [white, LIGHT_BG]),
        ("FONTNAME",      (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("ALIGN",         (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("GRID",          (0, 0), (-1, -1), 0.4, GREY_LINE),
    ]))
    right.append(ct)

    col_w = (PW - 2 * MARGIN - 10) / 2
    t2 = Table([[left, right]], colWidths=[col_w, col_w], rowHeights=None)
    t2.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t2)
    story.append(sp(8))
    story.append(footer_text())
    story.append(PageBreak())


def slide_site_overview(story, data):
    story.append(title_block("Site Overview -- All 4 Caltrans Maintenance Stations",
                             "Full-year summary under selected optimal configuration"))
    story.append(sp(10))

    summary_data = {
        "northgate": ("310", "2x DC 150 kW", "$4,510", "$5,718", "95.5%", "72.7%", "30-42",  "3,500-5,200"),
        "fresno":    ("313", "2x DC 50 kW",  "$1,265", "$1,960", "97.9%", "44.4%", "13-29",  "1,500-3,400"),
        "glendale":  ("255", "1x L2 19.2kW", "$162",   "$351",   "89.7%", "10.8%", "4-12",   "500-1,400"),
        "san_diego": ("339", "2x DC 150 kW", "$4,600", "$5,778", "80.3%", "43.1%", "70-97",  "8,000-11,300"),
    }

    hdr = ["Site", "Op. Days", "Selected Config", "Avg Daily\nCost",
           "Max Daily\nCost", "Annual\nVeh Svc %", "Annual\nEnergy Svc %",
           "Typical\nVehicles/Day", "Typical Energy\nDemand (kWh)"]
    rows = [hdr]
    for site, sl in SITES.items():
        d = summary_data[site]
        rows.append([sl, d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7]])

    cw_total = PW - 2 * MARGIN
    cw = [c * cw_total for c in [0.10, 0.08, 0.17, 0.10, 0.10, 0.11, 0.12, 0.12, 0.10]]
    t = Table(rows, colWidths=cw)
    ts = [
        ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR",     (0, 0), (-1, 0), white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("GRID",          (0, 0), (-1, -1), 0.5, GREY_LINE),
        ("FONTNAME",      (0, 1), (0, -1), "Helvetica-Bold"),
        ("TEXTCOLOR",     (0, 1), (0, -1), NAVY),
    ]
    # Alternating rows
    for i in range(1, len(rows)):
        if i % 2 == 0:
            ts.append(("BACKGROUND", (0, i), (-1, i), LIGHT_BG))
    # Highlight selected config column
    ts.append(("BACKGROUND", (2, 1), (2, -1), HexColor("#FEF3CD")))
    ts.append(("FONTNAME",   (2, 1), (2, -1), "Helvetica-Bold"))
    ts.append(("TEXTCOLOR",  (2, 1), (2, -1), HexColor("#7D5A00")))

    t.setStyle(TableStyle(ts))
    story.append(t)

    story.append(sp(16))
    story.append(banner("  Key Observations", TEAL))
    story.append(sp(6))

    obs = [
        "Northgate and San Diego are HIGH-DEMAND sites with 70-97 vehicles/day and 8,000-11,000 kWh/day during peak periods -- both select 2x DC 150 kW.",
        "Fresno is a MEDIUM-DEMAND site (13-29 vehicles, 1,500-3,400 kWh worst days) -- 2x DC 50 kW provides cost-efficient service at 97.9% annual vehicle rate.",
        "Glendale is a LOW-DEMAND site (4-12 vehicles, 500-1,400 kWh worst days) -- 1x L2 AC is cheapest at $162/day; a DC upgrade adds meaningful energy coverage.",
        "San Diego shows the lowest annual vehicle service (80.3%) despite 2x DC 150 kW, indicating extremely high-demand days where additional capacity may be warranted.",
    ]
    for ob in obs:
        story.append(p(f"  •  {ob}", "body"))
        story.append(sp(3))

    story.append(sp(10))
    story.append(footer_text())
    story.append(PageBreak())


def slide_site_detail(story, site, site_label, data):
    """Two slides per site: (1) overview, (2) worst-10 table."""
    if site not in data or not data[site]:
        return

    sel_cfg = SELECTED_CONFIGS[site]
    df_all  = data[site].get("all", pd.DataFrame())
    df_rank = data[site].get("rank", pd.DataFrame())
    df_w10  = data[site].get("w10", pd.DataFrame())

    # ── SLIDE 1: site stats ────────────────────────────────────────────────────
    story.append(title_block(f"{site_label} Maintenance Station",
                             f"Optimal Configuration: {sel_cfg}"))
    story.append(sp(10))

    # Stats from the ranked (optimal) data
    if not df_rank.empty:
        n_days   = len(df_rank)
        avg_cost = df_rank["total_cost"].mean()
        max_cost = df_rank["total_cost"].max()
        min_cost = df_rank["total_cost"].min()
        avg_veh  = df_rank["vehicles_served_pct"].mean() if "vehicles_served_pct" in df_rank.columns else 0
        avg_dmnd = df_rank["demand_served_pct"].mean() if "demand_served_pct" in df_rank.columns else 0
        avg_eveh = df_rank["n_vehicles"].mean() if "n_vehicles" in df_rank.columns else 0
        avg_edem = df_rank["energy_demanded_kwh"].mean() if "energy_demanded_kwh" in df_rank.columns else 0
    else:
        n_days = avg_cost = max_cost = min_cost = avg_veh = avg_dmnd = avg_eveh = avg_edem = 0

    # KPI boxes
    kpis = [
        ("Operating Days",   f"{n_days}",          TEAL),
        ("Avg Daily Cost",   f"${avg_cost:,.0f}",   NAVY),
        ("Max Daily Cost",   f"${max_cost:,.0f}",   RED_SOFT),
        ("Avg Vehicle Svc",  f"{avg_veh:.1f}%",     GREEN_OK if avg_veh >= 90 else GOLD),
        ("Avg Energy Svc",   f"{avg_dmnd:.1f}%",    GREEN_OK if avg_dmnd >= 60 else GOLD),
        ("Avg Vehicles/Day", f"{avg_eveh:.0f}",     TEAL),
    ]
    kpi_cells = []
    kpi_colors = []
    for label, val, clr in kpis:
        cell_content = [
            p(val, "slide_title"),
            p(label, "slide_sub"),
        ]
        kpi_cells.append(cell_content)
        kpi_colors.append(clr)

    kpi_row = []
    for i, (cells, clr) in enumerate(zip(kpi_cells, kpi_colors)):
        inner = Table([[c] for c in cells], colWidths=[1.4 * inch])
        inner.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), clr),
            ("TOPPADDING",    (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ]))
        kpi_row.append(inner)

    kpi_total = PW - 2 * MARGIN
    kpi_t = Table([kpi_row], colWidths=[kpi_total / len(kpis)] * len(kpis))
    kpi_t.setStyle(TableStyle([
        ("LEFTPADDING",  (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("ALIGN",        (0, 0), (-1, -1), "CENTER"),
    ]))
    story.append(kpi_t)
    story.append(sp(12))

    # Config cost comparison mini-table
    story.append(banner(f"  Configuration Comparison -- {site_label} (averaged over all {n_days} days)", TEAL))
    story.append(sp(6))

    if not df_all.empty:
        cfg_summary = []
        for cfg in CONFIGS_ORDER:
            cr = df_all[df_all["config"] == cfg]
            if cr.empty:
                continue
            is_sel = (cfg == sel_cfg)
            cfg_summary.append({
                "config":   cfg,
                "avg_cost": cr["total_cost"].mean(),
                "avg_veh":  cr["vehicles_served_pct"].mean(),
                "avg_dmnd": cr["demand_served_pct"].mean(),
                "selected": is_sel,
            })

        cs_hdr = ["Configuration", "Avg Daily Cost", "Avg Vehicle Svc %",
                  "Avg Energy Svc %", "Capex Rank", ""]
        cs_rows = [cs_hdr]
        for i, r in enumerate(cfg_summary, 1):
            sel_mark = "[SELECTED]" if r["selected"] else ""
            cs_rows.append([
                r["config"],
                f"${r['avg_cost']:,.2f}",
                f"{r['avg_veh']:.1f}%",
                f"{r['avg_dmnd']:.1f}%",
                f"#{i}",
                sel_mark,
            ])

        cw_total2 = PW - 2 * MARGIN
        cw2 = [c * cw_total2 for c in [0.22, 0.16, 0.18, 0.18, 0.10, 0.16]]
        ct2 = Table(cs_rows, colWidths=cw2)
        cts = [
            ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR",     (0, 0), (-1, 0), white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("GRID",          (0, 0), (-1, -1), 0.4, GREY_LINE),
        ]
        for i in range(1, len(cs_rows)):
            r = cfg_summary[i - 1]
            if r["selected"]:
                cts.append(("BACKGROUND", (0, i), (-1, i), HexColor("#FEF3CD")))
                cts.append(("FONTNAME",   (0, i), (-1, i), "Helvetica-Bold"))
                cts.append(("TEXTCOLOR",  (-1, i), (-1, i), GOLD))
            elif i % 2 == 0:
                cts.append(("BACKGROUND", (0, i), (-1, i), LIGHT_BG))
        ct2.setStyle(TableStyle(cts))
        story.append(ct2)

    story.append(sp(10))
    story.append(footer_text())
    story.append(PageBreak())

    # ── SLIDE 2: worst-10 table ────────────────────────────────────────────────
    story.append(title_block(
        f"{site_label} -- Top 10 Worst Days: Configuration Analysis",
        f"Days ranked by total daily cost under selected config ({sel_cfg})"))
    story.append(sp(8))

    if not df_w10.empty:
        story.append(make_worst10_table(df_w10, sel_cfg, site_label))

    story.append(sp(8))
    # Quick legend
    legend_items = [
        ("* Gold row = selected optimal configuration", GOLD),
        ("Blue rows = alternating days for readability", MID_BLUE),
        ("Yellow highlight = selected config row", HexColor("#7D5A00")),
    ]
    leg_row = [[p(txt, "small")] for txt, _ in legend_items]
    # Simple note
    story.append(p("* Highlighted row (gold border) = site selected configuration.  "
                   "Energy Served / Demand Served = greedy simulation under fixed configuration.", "small"))
    story.append(sp(8))
    story.append(footer_text())
    story.append(PageBreak())


def slide_final_comparison(story, data):
    story.append(title_block(
        "Final Comparison -- Worst-Day Robustness",
        "Average metrics across the 10 highest-cost days per site"))
    story.append(sp(8))

    for site, site_label in SITES.items():
        df_w10 = data.get(site, {}).get("w10", pd.DataFrame())
        sel_cfg = SELECTED_CONFIGS[site]
        if df_w10.empty:
            continue

        story.append(banner(f"  {site_label.upper()}  --  Selected: {sel_cfg}", TEAL))
        story.append(sp(4))

        hdr = ["Configuration", "Avg Cost", "Avg Dmnd Svc%", "Avg Veh Svc%",
               "Days 100% Svc", "Min Veh Svc%", ""]
        rows = [hdr]
        for cfg in CONFIGS_ORDER:
            cr = df_w10[df_w10["config"] == cfg]
            if cr.empty:
                continue
            avg_cost = cr["total_cost"].mean()
            avg_dmnd = cr["demand_served_pct"].mean()
            avg_veh  = cr["vehicles_served_pct"].mean()
            days_100 = (cr["vehicles_served_pct"] >= 99.9).sum()
            min_veh  = cr["vehicles_served_pct"].min()
            n        = len(cr)
            star     = "[SELECTED]" if cfg == sel_cfg else ""
            rows.append([cfg, f"${avg_cost:,.0f}", f"{avg_dmnd:.1f}%",
                         f"{avg_veh:.1f}%", f"{days_100}/{n}", f"{min_veh:.1f}%", star])

        cw_t = PW - 2 * MARGIN
        cw = [c * cw_t for c in [0.20, 0.13, 0.15, 0.15, 0.13, 0.13, 0.11]]
        t = Table(rows, colWidths=cw)
        ts = [
            ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR",     (0, 0), (-1, 0), white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("GRID",          (0, 0), (-1, -1), 0.4, GREY_LINE),
            ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ]
        for i in range(1, len(rows)):
            cfg_lbl = rows[i][0]
            if cfg_lbl == sel_cfg:
                ts.append(("BACKGROUND", (0, i), (-1, i), HexColor("#FEF3CD")))
                ts.append(("FONTNAME",   (0, i), (-1, i), "Helvetica-Bold"))
                ts.append(("TEXTCOLOR",  (-1, i), (-1, i), GOLD))
            elif i % 2 == 0:
                ts.append(("BACKGROUND", (0, i), (-1, i), LIGHT_BG))
        t.setStyle(TableStyle(ts))
        story.append(t)
        story.append(sp(8))

    story.append(footer_text())
    story.append(PageBreak())


def slide_conclusions(story):
    story.append(title_block("Conclusions & Recommendations",
                             "Fixed DCFC Charger Sizing -- Caltrans Maintenance Stations"))
    story.append(sp(10))

    # Two-column layout
    left = [
        banner("  Site Recommendations", TEAL),
        sp(8),
        p("Northgate  --  2x DC 150 kW", "h2"),
        p("Best cost-service tradeoff for a high-demand fleet (30-42 vehicles/day). "
          "Avg daily cost $4,510. Achieves 95.5% annual vehicle service and 88.6% "
          "avg vehicle service on the 10 worst days.", "body"),
        sp(8),
        p("Fresno  --  2x DC 50 kW", "h2"),
        p("Cost-efficient for a medium fleet (13-29 vehicles/day). Avg daily cost $1,265. "
          "Achieves 97.9% annual vehicle service and 100% on 5 of the 10 worst days. "
          "Upgrading to 1x DC 150 kW adds ~40% more energy coverage at similar cost.", "body"),
        sp(8),
        p("Glendale  --  1x L2 (19.2 kW)", "h2"),
        p("Small fleet (4-12 vehicles/day) with long dwell times makes L2 cost-viable "
          "at only $162/day. However, energy delivery is low (10.8% annual). "
          "Consider 1x DC 50 kW ($895/day) for 92.8% vehicle service and better energy coverage.", "body"),
        sp(8),
        p("San Diego  --  2x DC 150 kW", "h2"),
        p("Largest fleet in the study (70-97 vehicles/day, up to 11,300 kWh demand). "
          "2x DC 150 kW achieves 80.3% annual vehicle service but only 37-50% energy delivery "
          "on worst days. A third charger or DC 350 kW unit may be warranted for peak days.", "body"),
    ]

    right = [
        banner("  Key Findings", TEAL),
        sp(8),
        p("1.  Two units outperform one on vehicle service", "bold"),
        p("Having 2 chargers of lower power nearly always serves more vehicles than a "
          "single higher-power unit, because multiple vehicles can charge simultaneously "
          "during short overlapping dwell windows.", "body"),
        sp(8),
        p("2.  2x DC 150 kW beats 1x DC 350 kW at lower cost", "bold"),
        p("For Northgate and San Diego, 2x DC 150 kW consistently achieves higher vehicle "
          "service % at lower total cost than 1x DC 350 kW. The 350 kW unit's higher capex "
          "does not compensate for its single-slot bottleneck.", "body"),
        sp(8),
        p("3.  Selected configs remain robust on worst days", "bold"),
        p("All four recommended configurations achieve their best service rates among "
          "cost-comparable options, even on the 10 highest-cost operating days.", "body"),
        sp(8),
        p("4.  Summer / peak-season drives worst days", "bold"),
        p("The top-10 worst days for all high-demand sites cluster in June-September, "
          "coinciding with peak SMUD energy rates and higher fleet activity.", "body"),
        sp(8),
        banner("  Cost Assumptions", HexColor("#EEF4FB"), "body"),
        sp(4),
        p("Updated to finalized table values (purchase + installation-only costs, 9-year "
          "lifespan for all charger types). Applied uniformly across all 4 sites.", "small"),
    ]

    col_w = (PW - 2 * MARGIN - 12) / 2
    t2 = Table([[left, right]], colWidths=[col_w, col_w])
    t2.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t2)
    story.append(sp(10))
    story.append(footer_text())


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    OUTDIR.mkdir(parents=True, exist_ok=True)
    data = load_results()

    doc = SimpleDocTemplate(
        str(PDF),
        pagesize=landscape(letter),
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN,  bottomMargin=MARGIN,
        title="Caltrans Fixed DCFC Charger Sizing Optimization",
        author="Caltrans EV Fleet Study",
        subject="Fixed Charger Optimization -- All 4 Sites",
    )

    story = []

    print("Building slides...")
    slide_title_page(story)
    print("  1. Title page")
    slide_cost_assumptions(story)
    print("  2. Cost assumptions")
    slide_methodology(story)
    print("  3. Methodology")
    slide_site_overview(story, data)
    print("  4. Site overview")

    for site, site_label in SITES.items():
        slide_site_detail(story, site, site_label, data)
        print(f"  5-6+. {site_label} detail slides")

    slide_final_comparison(story, data)
    print("  Final comparison")
    slide_conclusions(story)
    print("  Conclusions")

    print(f"\nBuilding PDF: {PDF}")
    doc.build(story)
    print(f"Done. Saved: {PDF}")
    print(f"Size: {PDF.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
