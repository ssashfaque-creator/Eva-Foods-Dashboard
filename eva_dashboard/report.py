"""PDF report generation for sales dashboard."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from xml.sax.saxutils import escape

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from eva_dashboard.data import (
    CATEGORY1_ORDER,
    CITY_BRAND_COLUMNS,
    SalesReportData,
    pct_change,
)


BRAND = colors.Color(0.09, 0.29, 0.22)
ACCENT = colors.Color(0.16, 0.45, 0.35)
HEADER_BG = colors.Color(0.09, 0.29, 0.22)
HEADER_FG = colors.white
ROW_ALT = colors.Color(0.93, 0.96, 0.94)
LINE = colors.Color(0.75, 0.82, 0.78)
MUTED = colors.Color(0.35, 0.40, 0.38)


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "brand": ParagraphStyle(
            "Brand",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=22,
            textColor=BRAND,
            alignment=TA_LEFT,
            spaceAfter=2 * mm,
        ),
        "title": ParagraphStyle(
            "Title",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=14,
            textColor=colors.black,
            alignment=TA_LEFT,
            spaceAfter=1 * mm,
        ),
        "meta": ParagraphStyle(
            "Meta",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=9,
            textColor=MUTED,
            alignment=TA_LEFT,
            spaceAfter=4 * mm,
        ),
        "section": ParagraphStyle(
            "Section",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=11,
            textColor=BRAND,
            spaceBefore=2 * mm,
            spaceAfter=3 * mm,
        ),
        "product_heading": ParagraphStyle(
            "ProductHeading",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=13,
            textColor=BRAND,
            spaceBefore=4 * mm,
            spaceAfter=2 * mm,
        ),
        "city_heading": ParagraphStyle(
            "CityHeading",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=10,
            textColor=ACCENT,
            spaceBefore=2 * mm,
            spaceAfter=1.5 * mm,
        ),
        "cell": ParagraphStyle(
            "Cell",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=7,
            leading=9,
            textColor=colors.black,
        ),
        "cell_right": ParagraphStyle(
            "CellRight",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=7,
            leading=9,
            alignment=TA_RIGHT,
        ),
        "cell_center": ParagraphStyle(
            "CellCenter",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=7,
            leading=9,
            alignment=TA_CENTER,
        ),
        "cell_bold": ParagraphStyle(
            "CellBold",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7,
            leading=9,
            textColor=colors.white,
        ),
        "cell_bold_dark": ParagraphStyle(
            "CellBoldDark",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=colors.black,
        ),
        "cell_right_bold": ParagraphStyle(
            "CellRightBold",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7,
            leading=9,
            alignment=TA_RIGHT,
            textColor=colors.black,
        ),
        "toc_title": ParagraphStyle(
            "TocTitle",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=12,
            textColor=BRAND,
            spaceBefore=1 * mm,
            spaceAfter=3 * mm,
        ),
        "toc_category": ParagraphStyle(
            "TocCategory",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=12,
            textColor=BRAND,
            spaceBefore=2.2 * mm,
            spaceAfter=0.6 * mm,
            leftIndent=0,
        ),
        "toc_city": ParagraphStyle(
            "TocCity",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=ACCENT,
            spaceBefore=0.8 * mm,
            spaceAfter=0.3 * mm,
            leftIndent=6 * mm,
        ),
        "toc_party": ParagraphStyle(
            "TocParty",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=7.5,
            leading=9.5,
            textColor=colors.black,
            spaceBefore=0,
            spaceAfter=0.2 * mm,
            leftIndent=12 * mm,
        ),
    }


UP = colors.Color(0.05, 0.45, 0.22)
DOWN = colors.Color(0.70, 0.12, 0.12)


def _fmt_mt(value: float) -> str:
    """Display MT to 1 decimal; value itself is not pre-rounded for totals."""
    return f"{value:,.1f}"


def _fmt_qty(value: float) -> str:
    return f"{int(round(value)):,}"


def _fmt_money(value: float) -> str:
    """Display money/rate figures as whole numbers."""
    return f"{int(round(value)):,}"


def _fmt_pct(change: float | None) -> tuple[str, colors.Color]:
    if change is None:
        return ("—", MUTED)
    color = UP if change >= 0 else DOWN
    whole = int(round(change))
    sign = "+" if whole > 0 else ""
    return (f"{sign}{whole}%", color)


def _pct_paragraph(
    current: float,
    baseline: float,
    styles: dict[str, ParagraphStyle],
) -> Paragraph:
    text, color = _fmt_pct(pct_change(current, baseline))
    style = ParagraphStyle(
        f"Pct_{text}_{id(color)}",
        parent=styles["cell_right"],
        textColor=color,
        fontName="Helvetica-Bold",
        fontSize=6.5,
    )
    return Paragraph(text, style)


def _base_table_style(row_count: int, has_total: bool = True) -> list:
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), HEADER_FG),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
    ]
    last_data = row_count - 2 if has_total else row_count - 1
    for i in range(1, last_data + 1):
        if i % 2 == 0:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), ROW_ALT))
    if has_total:
        style_cmds.extend(
            [
                ("BACKGROUND", (0, -1), (-1, -1), colors.Color(0.86, 0.91, 0.88)),
                ("LINEABOVE", (0, -1), (-1, -1), 1.0, BRAND),
            ]
        )
    return style_cmds


def _summary_table(data: SalesReportData, styles: dict[str, ParagraphStyle]) -> Table:
    header = [
        Paragraph("Category", styles["cell_bold"]),
        Paragraph("Daily Sales (MT)", styles["cell_bold"]),
        Paragraph("Avg Last 30 Days", styles["cell_bold"]),
        Paragraph("Δ% vs 30D", styles["cell_bold"]),
        Paragraph("MTD Sales (MT)", styles["cell_bold"]),
        Paragraph("AMS", styles["cell_bold"]),
        Paragraph("Δ% vs AMS", styles["cell_bold"]),
    ]
    rows: list[list] = [header]
    for row in data.category_summary:
        rows.append(
            [
                Paragraph(row.category1, styles["cell"]),
                Paragraph(_fmt_mt(row.daily_mt), styles["cell_right"]),
                Paragraph(_fmt_mt(row.avg_30d_mt), styles["cell_right"]),
                _pct_paragraph(row.daily_mt, row.avg_30d_mt, styles),
                Paragraph(_fmt_mt(row.mtd_mt), styles["cell_right"]),
                Paragraph(_fmt_mt(row.ams_mt), styles["cell_right"]),
                _pct_paragraph(row.mtd_mt, row.ams_mt, styles),
            ]
        )
    rows.append(
        [
            Paragraph("Total", styles["cell_bold_dark"]),
            Paragraph(_fmt_mt(data.total_daily_mt), styles["cell_right"]),
            Paragraph(_fmt_mt(data.total_avg_30d_mt), styles["cell_right"]),
            _pct_paragraph(data.total_daily_mt, data.total_avg_30d_mt, styles),
            Paragraph(_fmt_mt(data.total_mtd_mt), styles["cell_right"]),
            Paragraph(_fmt_mt(data.total_ams_mt), styles["cell_right"]),
            _pct_paragraph(data.total_mtd_mt, data.total_ams_mt, styles),
        ]
    )

    table = Table(
        rows,
        colWidths=[36 * mm, 24 * mm, 24 * mm, 18 * mm, 24 * mm, 22 * mm, 18 * mm],
        hAlign="LEFT",
    )
    table.setStyle(TableStyle(_base_table_style(len(rows))))
    return table


def _top_cities_with_other(
    frame: pd.DataFrame, top_n: int = 10
) -> tuple[pd.DataFrame, dict[str, float] | None]:
    """Return top N cities plus an aggregated Other row for the remainder."""
    if frame is None or len(frame) == 0:
        empty = frame if frame is not None else pd.DataFrame()
        return empty, None
    top = frame.head(top_n).copy()
    if len(frame) <= top_n:
        return top, None
    rest = frame.iloc[top_n:]
    other: dict[str, float] = {}
    for col in rest.columns:
        if col == "city":
            continue
        other[col] = float(pd.to_numeric(rest[col], errors="coerce").fillna(0.0).sum())
    other["city"] = "Other"
    return top, other


def _city_daily_table(data: SalesReportData, styles: dict[str, ParagraphStyle]) -> Table:
    brands = list(CITY_BRAND_COLUMNS)
    header = [Paragraph("City", styles["cell_bold"])] + [
        Paragraph(name, styles["cell_bold"]) for name in brands
    ] + [
        Paragraph("Total", styles["cell_bold"]),
        Paragraph("Avg 30D", styles["cell_bold"]),
        Paragraph("Δ%", styles["cell_bold"]),
    ]
    rows: list[list] = [header]

    top, other = _top_cities_with_other(data.city_daily, top_n=10)
    totals = {name: 0.0 for name in brands}
    grand = 0.0
    avg_sum = 0.0

    def _append_city_row(city_name: str, values: dict[str, float] | pd.Series) -> None:
        nonlocal grand, avg_sum
        cells = [Paragraph(str(city_name), styles["cell"])]
        row_total = 0.0
        for name in brands:
            value = float(values.get(name, 0.0) or 0.0)
            totals[name] += value
            row_total += value
            cells.append(Paragraph(_fmt_mt(value), styles["cell_right"]))
        avg_30d = float(values.get("avg_30d", 0.0) or 0.0)
        avg_sum += avg_30d
        grand += row_total
        cells.append(Paragraph(_fmt_mt(row_total), styles["cell_right"]))
        cells.append(Paragraph(_fmt_mt(avg_30d), styles["cell_right"]))
        cells.append(_pct_paragraph(row_total, avg_30d, styles))
        rows.append(cells)

    for _, row in top.iterrows():
        _append_city_row(str(row["city"]), row)
    if other is not None:
        _append_city_row("Other", other)

    total_avg = float(data.city_daily_ads.get("total", 0.0))
    total_row = [Paragraph("Total", styles["cell_bold_dark"])]
    for name in brands:
        total_row.append(Paragraph(_fmt_mt(totals[name]), styles["cell_right"]))
    total_row.append(Paragraph(_fmt_mt(grand), styles["cell_right"]))
    total_row.append(Paragraph(_fmt_mt(total_avg), styles["cell_right"]))
    total_row.append(_pct_paragraph(grand, total_avg, styles))
    rows.append(total_row)

    ads_row = [Paragraph("ADS (30D)", styles["cell_bold_dark"])]
    for name in brands:
        ads_row.append(
            Paragraph(_fmt_mt(float(data.city_daily_ads.get(name, 0.0))), styles["cell_right"])
        )
    ads_row.append(Paragraph(_fmt_mt(total_avg), styles["cell_right"]))
    ads_row.append(Paragraph("—", styles["cell_right"]))
    ads_row.append(Paragraph("—", styles["cell_right"]))
    rows.append(ads_row)

    col_widths = [24 * mm] + [22 * mm] * len(brands) + [20 * mm, 18 * mm, 14 * mm]
    table = Table(rows, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    total_idx = len(rows) - 2
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), HEADER_FG),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("BACKGROUND", (0, total_idx), (-1, total_idx), colors.Color(0.86, 0.91, 0.88)),
        ("LINEABOVE", (0, total_idx), (-1, total_idx), 1.0, BRAND),
        ("BACKGROUND", (0, -1), (-1, -1), colors.Color(0.88, 0.92, 0.95)),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, ACCENT),
    ]
    other_idx = (total_idx - 1) if other is not None else None
    for i in range(1, total_idx):
        if other_idx is not None and i == other_idx:
            continue
        if i % 2 == 0:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), ROW_ALT))
    if other_idx is not None:
        style_cmds.append(
            ("BACKGROUND", (0, other_idx), (-1, other_idx), colors.Color(0.94, 0.95, 0.93))
        )
    table.setStyle(TableStyle(style_cmds))
    return table


def _city_mtd_table(data: SalesReportData, styles: dict[str, ParagraphStyle]) -> Table:
    brands = list(CITY_BRAND_COLUMNS)
    header = [Paragraph("City", styles["cell_bold"])] + [
        Paragraph(name, styles["cell_bold"]) for name in brands
    ] + [
        Paragraph("Total", styles["cell_bold"]),
        Paragraph("AMS", styles["cell_bold"]),
        Paragraph("Δ%", styles["cell_bold"]),
    ]
    rows: list[list] = [header]

    top, other = _top_cities_with_other(data.city_mtd, top_n=10)
    totals = {name: 0.0 for name in brands}
    grand = 0.0

    def _append_city_row(city_name: str, values: dict[str, float] | pd.Series) -> None:
        nonlocal grand
        cells = [Paragraph(str(city_name), styles["cell"])]
        row_total = 0.0
        for name in brands:
            value = float(values.get(name, 0.0) or 0.0)
            totals[name] += value
            row_total += value
            cells.append(Paragraph(_fmt_mt(value), styles["cell_right"]))
        ams = float(values.get("ams", 0.0) or 0.0)
        grand += row_total
        cells.append(Paragraph(_fmt_mt(row_total), styles["cell_right"]))
        cells.append(Paragraph(_fmt_mt(ams), styles["cell_right"]))
        cells.append(_pct_paragraph(row_total, ams, styles))
        rows.append(cells)

    for _, row in top.iterrows():
        _append_city_row(str(row["city"]), row)
    if other is not None:
        _append_city_row("Other", other)

    total_ams = float(data.city_mtd_ams.get("total", 0.0))
    total_row = [Paragraph("Total", styles["cell_bold_dark"])]
    for name in brands:
        total_row.append(Paragraph(_fmt_mt(totals[name]), styles["cell_right"]))
    total_row.append(Paragraph(_fmt_mt(grand), styles["cell_right"]))
    total_row.append(Paragraph(_fmt_mt(total_ams), styles["cell_right"]))
    total_row.append(_pct_paragraph(grand, total_ams, styles))
    rows.append(total_row)

    ams_row = [Paragraph("AMS (3M)", styles["cell_bold_dark"])]
    for name in brands:
        ams_row.append(
            Paragraph(_fmt_mt(float(data.city_mtd_ams.get(name, 0.0))), styles["cell_right"])
        )
    ams_row.append(Paragraph(_fmt_mt(total_ams), styles["cell_right"]))
    ams_row.append(Paragraph("—", styles["cell_right"]))
    ams_row.append(Paragraph("—", styles["cell_right"]))
    rows.append(ams_row)

    col_widths = [24 * mm] + [22 * mm] * len(brands) + [20 * mm, 18 * mm, 14 * mm]
    table = Table(rows, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    total_idx = len(rows) - 2
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), HEADER_FG),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("BACKGROUND", (0, total_idx), (-1, total_idx), colors.Color(0.86, 0.91, 0.88)),
        ("LINEABOVE", (0, total_idx), (-1, total_idx), 1.0, BRAND),
        ("BACKGROUND", (0, -1), (-1, -1), colors.Color(0.88, 0.92, 0.95)),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, ACCENT),
    ]
    other_idx = (total_idx - 1) if other is not None else None
    for i in range(1, total_idx):
        if other_idx is not None and i == other_idx:
            continue
        if i % 2 == 0:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), ROW_ALT))
    if other_idx is not None:
        style_cmds.append(
            ("BACKGROUND", (0, other_idx), (-1, other_idx), colors.Color(0.94, 0.95, 0.93))
        )
    table.setStyle(TableStyle(style_cmds))
    return table



def _fmt_optional_money(value: float | None) -> str:
    if value is None:
        return "—"
    try:
        if value != value:  # NaN
            return "—"
    except TypeError:
        return "—"
    return _fmt_money(float(value))


def _price_fetch_table(data: SalesReportData, styles: dict[str, ParagraphStyle]) -> Table:
    header = [
        Paragraph("Client Type", styles["cell_bold"]),
        Paragraph("Oil (Eva)", styles["cell_bold"]),
        Paragraph("Ghee (Eva)", styles["cell_bold"]),
        Paragraph("Oil (Maan)", styles["cell_bold"]),
        Paragraph("Ghee (Maan)", styles["cell_bold"]),
    ]
    rows: list[list] = [header]
    for row in data.price_fetch_summary:
        rows.append(
            [
                Paragraph(row.client_type, styles["cell"]),
                Paragraph(_fmt_optional_money(row.eva_oil), styles["cell_right"]),
                Paragraph(_fmt_optional_money(row.eva_ghee), styles["cell_right"]),
                Paragraph(_fmt_optional_money(row.maan_oil), styles["cell_right"]),
                Paragraph(_fmt_optional_money(row.maan_ghee), styles["cell_right"]),
            ]
        )
    if len(rows) == 1:
        rows.append(
            [
                Paragraph("No oil/ghee sales with cost factors", styles["cell"]),
                Paragraph("—", styles["cell_right"]),
                Paragraph("—", styles["cell_right"]),
                Paragraph("—", styles["cell_right"]),
                Paragraph("—", styles["cell_right"]),
            ]
        )

    table = Table(
        rows,
        colWidths=[42 * mm, 28 * mm, 28 * mm, 28 * mm, 28 * mm],
        hAlign="LEFT",
    )
    table.setStyle(TableStyle(_base_table_style(len(rows), has_total=False)))
    return table


def _bulk_product_price_table(
    data: SalesReportData, styles: dict[str, ParagraphStyle]
) -> Table:
    header = [
        Paragraph("Product", styles["cell_bold"]),
        Paragraph("Category", styles["cell_bold"]),
        Paragraph("Daily Avg", styles["cell_bold"]),
        Paragraph("MTD Avg", styles["cell_bold"]),
        Paragraph("Unit", styles["cell_bold"]),
    ]
    rows: list[list] = [header]
    for row in data.bulk_product_prices:
        rows.append(
            [
                Paragraph(row.product, styles["cell"]),
                Paragraph(row.category1, styles["cell"]),
                Paragraph(_fmt_optional_money(row.daily_avg_price), styles["cell_right"]),
                Paragraph(_fmt_optional_money(row.mtd_avg_price), styles["cell_right"]),
                Paragraph(row.price_unit, styles["cell"]),
            ]
        )
    if len(rows) == 1:
        rows.append(
            [
                Paragraph("No bulk / industrial sales in daily or MTD window", styles["cell"]),
                Paragraph("—", styles["cell"]),
                Paragraph("—", styles["cell_right"]),
                Paragraph("—", styles["cell_right"]),
                Paragraph("—", styles["cell"]),
            ]
        )

    table = Table(
        rows,
        colWidths=[58 * mm, 26 * mm, 26 * mm, 26 * mm, 22 * mm],
        hAlign="LEFT",
    )
    table.setStyle(TableStyle(_base_table_style(len(rows), has_total=False)))
    return table


def _sales_identity_and_sku_widths() -> list[float]:
    return [
        20 * mm,  # Category = client Type (merged)
        16 * mm,  # City = City-Filter (merged)
        30 * mm,  # Party (merged)
        34 * mm,  # Product
        12 * mm,  # Qty
        10 * mm,  # Unit
        14 * mm,  # M.T Qty
        16 * mm,  # Rate
        20 * mm,  # Basic Amount
        20 * mm,  # Incl Gst/Fed
        18 * mm,  # Amount per KG
        16 * mm,  # Cost Factor
        18 * mm,  # Price Fetch
    ]


def _sales_header_labels() -> list[str]:
    return [
        "Category",
        "City",
        "Party",
        "Product",
        "Qty",
        "Unit",
        "M.T Qty",
        "Rate",
        "Basic Amount",
        "Incl Gst/Fed",
        "Amount per KG",
        "Cost Factor",
        "Price Fetch",
    ]


def _sales_header_cells(styles: dict[str, ParagraphStyle]) -> list:
    return [Paragraph(label, styles["cell_bold"]) for label in _sales_header_labels()]


def _blank_identity() -> list:
    return ["", "", ""]


def _sku_cells(row, styles: dict[str, ParagraphStyle]) -> list:
    import math

    import pandas as pd

    cost = row["cost_factor"] if "cost_factor" in row.index else None
    price_fetch = row["price_fetch"] if "price_fetch" in row.index else None

    cost_val = None
    if cost is not None and not (isinstance(cost, float) and math.isnan(cost)) and not pd.isna(cost):
        try:
            cost_val = float(cost)
        except (TypeError, ValueError):
            cost_val = None

    pf_val = None
    if (
        price_fetch is not None
        and not (isinstance(price_fetch, float) and math.isnan(price_fetch))
        and not pd.isna(price_fetch)
    ):
        try:
            pf_val = float(price_fetch)
        except (TypeError, ValueError):
            pf_val = None

    return [
        Paragraph(str(row["product"]), styles["cell"]),
        Paragraph(_fmt_qty(float(row["qty"])), styles["cell_right"]),
        Paragraph(str(row["unit"]), styles["cell"]),
        Paragraph(_fmt_mt(float(row["mt_qty"])), styles["cell_right"]),
        Paragraph(_fmt_money(float(row["rate"])), styles["cell_right"]),
        Paragraph(_fmt_money(float(row["basic_amount"])), styles["cell_right"]),
        Paragraph(_fmt_money(float(row["incl_gst_fed"])), styles["cell_right"]),
        Paragraph(_fmt_money(float(row["amount_per_kg"])), styles["cell_right"]),
        Paragraph(_fmt_optional_money(cost_val), styles["cell_right"]),
        Paragraph(_fmt_optional_money(pf_val), styles["cell_right"]),
    ]


def _total_cells(
    label: str,
    total_mt: float,
    total_basic: float,
    total_incl: float,
    weighted_rate: float,
    amount_per_kg: float,
    styles: dict[str, ParagraphStyle],
    blended_price_fetch: float | None = None,
) -> list:
    return [
        Paragraph(label, styles["cell_bold_dark"]),
        Paragraph("", styles["cell"]),
        Paragraph("", styles["cell"]),
        Paragraph(_fmt_mt(total_mt), styles["cell_right_bold"]),
        Paragraph(_fmt_money(weighted_rate), styles["cell_right_bold"]),
        Paragraph(_fmt_money(total_basic), styles["cell_right_bold"]),
        Paragraph(_fmt_money(total_incl), styles["cell_right_bold"]),
        Paragraph(_fmt_money(amount_per_kg), styles["cell_right_bold"]),
        Paragraph("", styles["cell"]),
        Paragraph(_fmt_optional_money(blended_price_fetch), styles["cell_right_bold"]),
    ]


def _frame_totals(
    frame,
) -> tuple[float, float, float, float, float, float | None]:
    """Return mt, basic, incl, weighted line Rate, amount/kg, price fetch."""
    from eva_dashboard.data import weighted_avg

    total_mt = float(frame["mt_qty"].sum())
    total_basic = float(frame["basic_amount"].sum())
    total_incl = float(frame["incl_gst_fed"].sum())
    total_kg = total_mt * 1000.0
    amount_per_kg = (total_incl / total_kg) if total_kg else 0.0

    # Rate on lines is typically per litre (Mes Qty); weight by Mes Qty when present.
    if "mes_qty" in frame.columns and float(frame["mes_qty"].sum()) > 0:
        weighted_rate = weighted_avg(frame["rate"], frame["mes_qty"]) or 0.0
    else:
        weighted_rate = weighted_avg(frame["rate"], frame["mt_qty"]) or 0.0

    if "price_fetch" in frame.columns and "mt_qty" in frame.columns:
        blended_pf = weighted_avg(frame["price_fetch"], frame["mt_qty"])
    else:
        blended_pf = None
    return total_mt, total_basic, total_incl, weighted_rate, amount_per_kg, blended_pf


def _party_mt_totals(frame) -> dict[tuple[str, str, str], float]:
    return (
        frame.groupby(["category", "city", "party"], sort=False)["mt_qty"]
        .sum()
        .to_dict()
    )


def _base_detail_table_style() -> list:
    return [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), HEADER_FG),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("GRID", (0, 0), (-1, -1), 0.3, LINE),
        ("BOX", (0, 0), (-1, -1), 0.7, BRAND),
    ]


def _party_detail_table(
    category: str,
    city: str,
    party: str,
    group,
    styles: dict[str, ParagraphStyle],
) -> Table:
    """One customer block — no row SPANs so ReportLab can split across pages."""
    rows: list[list] = [_sales_header_cells(styles)]
    sku_rows = list(group.iterrows())
    for offset, (_, row) in enumerate(sku_rows):
        if offset == 0:
            identity = [
                Paragraph(str(category), styles["cell_center"]),
                Paragraph(str(city), styles["cell_center"]),
                Paragraph(str(party), styles["cell_center"]),
            ]
        else:
            identity = _blank_identity()
        rows.append(identity + _sku_cells(row, styles))

    total_mt, total_basic, total_incl, weighted_rate, amount_per_kg, blended_pf = (
        _frame_totals(group)
    )
    total_idx = len(rows)
    rows.append(
        _blank_identity()
        + _total_cells(
            "Customer Total",
            total_mt,
            total_basic,
            total_incl,
            weighted_rate,
            amount_per_kg,
            styles,
            blended_price_fetch=blended_pf,
        )
    )
    style_cmds = _base_detail_table_style() + [
        ("BACKGROUND", (0, 1), (-1, total_idx - 1), colors.white),
        ("BACKGROUND", (0, total_idx), (-1, total_idx), colors.Color(0.86, 0.91, 0.88)),
        ("LINEABOVE", (0, 1), (-1, 1), 0.7, ACCENT),
        ("LINEABOVE", (0, total_idx), (-1, total_idx), 0.7, BRAND),
        ("VALIGN", (0, 1), (2, 1), "MIDDLE"),
        ("ALIGN", (0, 1), (2, 1), "CENTER"),
    ]
    table = Table(
        rows,
        colWidths=_sales_identity_and_sku_widths(),
        repeatRows=1,
        hAlign="LEFT",
    )
    table.setStyle(TableStyle(style_cmds))
    return table


def _section_total_table(
    frame,
    styles: dict[str, ParagraphStyle],
    section_total_label: str,
) -> Table:
    total_mt, total_basic, total_incl, weighted_rate, amount_per_kg, blended_pf = (
        _frame_totals(frame)
    )
    row = _blank_identity() + _total_cells(
        section_total_label,
        total_mt,
        total_basic,
        total_incl,
        weighted_rate,
        amount_per_kg,
        styles,
        blended_price_fetch=blended_pf,
    )
    table = Table([row], colWidths=_sales_identity_and_sku_widths(), hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("SPAN", (0, 0), (2, 0)),
                ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.82, 0.89, 0.85)),
                ("BOX", (0, 0), (-1, 0), 0.7, BRAND),
                ("LINEABOVE", (0, 0), (-1, 0), 1.0, BRAND),
                ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _section_sales_flowables(
    frame,
    styles: dict[str, ParagraphStyle],
    section_total_label: str,
) -> list:
    """One table per customer (page-break safe) + section total."""
    if frame is None or len(frame) == 0:
        return []

    party_mt = _party_mt_totals(frame)
    keys = sorted(
        party_mt.keys(),
        key=lambda key: (
            -float(party_mt[key] or 0.0),
            str(key[2] or ""),
            str(key[0] or ""),
            str(key[1] or ""),
        ),
    )

    flowables: list = []
    for category, city, party in keys:
        group = frame[
            (frame["category"] == category)
            & (frame["city"] == city)
            & (frame["party"] == party)
        ]
        if group.empty:
            continue
        flowables.append(_party_detail_table(category, city, party, group, styles))
        flowables.append(Spacer(1, 1.5 * mm))

    flowables.append(_section_total_table(frame, styles, section_total_label))
    return flowables


def _product_total_banner(
    product_type: str,
    frame,
    styles: dict[str, ParagraphStyle],
) -> Table:
    total_mt, total_basic, total_incl, weighted_rate, amount_per_kg, blended_pf = (
        _frame_totals(frame)
    )
    row = _blank_identity() + _total_cells(
        f"Product Total — {product_type}",
        total_mt,
        total_basic,
        total_incl,
        weighted_rate,
        amount_per_kg,
        styles,
        blended_price_fetch=blended_pf,
    )
    table = Table([row], colWidths=_sales_identity_and_sku_widths(), hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("SPAN", (0, 0), (2, 0)),
                ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.78, 0.86, 0.82)),
                ("BOX", (0, 0), (-1, 0), 1.0, BRAND),
                ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _ordered_product_types(frame) -> list[str]:
    present = {
        str(v).strip()
        for v in frame["product_type"].tolist()
        if v is not None and not (isinstance(v, float) and pd.isna(v)) and str(v).strip()
    }
    ordered = [name for name in CATEGORY1_ORDER if name in present]
    extras = sorted(present - set(CATEGORY1_ORDER))
    return ordered + extras


def _ordered_cities_for_product(product_frame, city_rank: list[str]) -> list[str]:
    present = {
        str(v).strip() if v is not None and not (isinstance(v, float) and pd.isna(v)) else "Unmapped"
        for v in product_frame["city"].tolist()
    }
    ordered = [city for city in city_rank if city in present]
    leftover = sorted(
        present - set(ordered),
        key=lambda c: (
            -float(product_frame.loc[product_frame["city"] == c, "mt_qty"].sum() or 0.0),
            str(c),
        ),
    )
    return ordered + leftover


def _dest_key(*parts: str) -> str:
    """Stable PDF destination name from category / city / party labels."""
    raw = "||".join("" if p is None else str(p) for p in parts)
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:20]
    slug = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_")[:36]
    return f"d_{slug}_{digest}" if slug else f"d_{digest}"


class _NamedDest(Flowable):
    """Invisible destination + optional PDF outline entry for internal links."""

    def __init__(
        self,
        name: str,
        outline_title: str | None = None,
        outline_level: int = 0,
        *,
        outline_closed: bool = False,
    ):
        super().__init__()
        self._name = name
        self._outline_title = outline_title
        self._outline_level = outline_level
        self._outline_closed = outline_closed
        self.width = 0
        self.height = 0

    def wrap(self, availWidth, availHeight):  # noqa: N802
        # Tiny non-zero height so the flowable is drawn (anchors register).
        return (0, 0.1)

    def draw(self) -> None:
        # Anchor at the current vertical position (not just page top).
        self.canv.bookmarkHorizontal(self._name, 0, 0.1)
        if self._outline_title:
            try:
                self.canv.addOutlineEntry(
                    self._outline_title,
                    self._name,
                    level=self._outline_level,
                    closed=self._outline_closed,
                )
            except Exception:
                # Outline entries can fail if parent levels are missing; links still work.
                pass


def _toc_link(text: str, dest: str, *, mt: float | None = None) -> str:
    label = escape(str(text))
    if mt is not None:
        label = f"{label}  ({mt:,.1f} MT)"
    return (
        f'<link href="#{escape(dest)}" color="#1a5c3a">'
        f"<u>{label}</u></link>"
    )


def _parties_for_city(city_frame) -> list[tuple[str, str, str, float]]:
    """Return (category, city, party, mt) ordered by MT desc."""
    party_mt = _party_mt_totals(city_frame)
    keys = sorted(
        party_mt.keys(),
        key=lambda key: (
            -float(party_mt[key] or 0.0),
            str(key[2] or ""),
            str(key[0] or ""),
            str(key[1] or ""),
        ),
    )
    return [
        (str(cat), str(city), str(party), float(party_mt[key] or 0.0))
        for key in keys
        for cat, city, party in [key]
    ]


def _detail_toc_flowables(
    data: SalesReportData, styles: dict[str, ParagraphStyle]
) -> list:
    """Clickable Category → City → Customer contents for the detail section."""
    frame = data.daily_sales
    if frame is None or len(frame) == 0:
        return []

    city_rank = (
        [str(c) for c in data.city_daily["city"].tolist()] if len(data.city_daily) else []
    )
    flowables: list = [
        Paragraph("Contents — Daily Sales Detail", styles["toc_title"]),
        Paragraph(
            "Click a category, city, or customer to jump to that section.",
            styles["meta"],
        ),
    ]

    for product_type in _ordered_product_types(frame):
        product_frame = frame[frame["product_type"] == product_type]
        if product_frame.empty:
            continue
        cat_mt = float(product_frame["mt_qty"].sum())
        cat_dest = _dest_key("cat", product_type)
        flowables.append(
            Paragraph(
                _toc_link(product_type, cat_dest, mt=cat_mt),
                styles["toc_category"],
            )
        )
        for city in _ordered_cities_for_product(product_frame, city_rank):
            city_frame = product_frame[product_frame["city"] == city]
            if city_frame.empty:
                continue
            city_mt = float(city_frame["mt_qty"].sum())
            city_dest = _dest_key("city", product_type, city)
            flowables.append(
                Paragraph(
                    _toc_link(city, city_dest, mt=city_mt),
                    styles["toc_city"],
                )
            )
            for _cat, _city, party, party_mt in _parties_for_city(city_frame):
                party_dest = _dest_key("party", product_type, city, party)
                flowables.append(
                    Paragraph(
                        _toc_link(party, party_dest, mt=party_mt),
                        styles["toc_party"],
                    )
                )

    flowables.append(Spacer(1, 4 * mm))
    return flowables


def _sales_detail_flowables(
    data: SalesReportData, styles: dict[str, ParagraphStyle]
) -> list:
    """Category → City → Customer detail blocks with link destinations."""
    frame = data.daily_sales
    city_rank = (
        [str(c) for c in data.city_daily["city"].tolist()] if len(data.city_daily) else []
    )
    flowables: list = []

    for product_type in _ordered_product_types(frame):
        product_frame = frame[frame["product_type"] == product_type]
        if product_frame.empty:
            continue

        cat_dest = _dest_key("cat", product_type)
        flowables.append(
            _NamedDest(
                cat_dest,
                outline_title=str(product_type),
                outline_level=0,
                outline_closed=False,
            )
        )
        flowables.append(Paragraph(str(product_type), styles["product_heading"]))

        for city in _ordered_cities_for_product(product_frame, city_rank):
            city_frame = product_frame[product_frame["city"] == city]
            if city_frame.empty:
                continue

            city_dest = _dest_key("city", product_type, city)
            flowables.append(
                _NamedDest(
                    city_dest,
                    outline_title=str(city),
                    outline_level=1,
                    outline_closed=True,
                )
            )
            flowables.append(Paragraph(f"City: {city}", styles["city_heading"]))

            party_mt = _party_mt_totals(city_frame)
            keys = sorted(
                party_mt.keys(),
                key=lambda key: (
                    -float(party_mt[key] or 0.0),
                    str(key[2] or ""),
                    str(key[0] or ""),
                    str(key[1] or ""),
                ),
            )
            for category, _city, party in keys:
                group = city_frame[
                    (city_frame["category"] == category)
                    & (city_frame["city"] == _city)
                    & (city_frame["party"] == party)
                ]
                if group.empty:
                    continue
                party_dest = _dest_key("party", product_type, city, party)
                flowables.append(
                    _NamedDest(
                        party_dest,
                        outline_title=str(party)[:80],
                        outline_level=2,
                        outline_closed=True,
                    )
                )
                flowables.append(
                    _party_detail_table(category, city, party, group, styles)
                )
                flowables.append(Spacer(1, 1.5 * mm))

            flowables.append(
                _section_total_table(
                    city_frame,
                    styles,
                    section_total_label=f"City Total — {city}",
                )
            )
            flowables.append(Spacer(1, 3 * mm))

        flowables.append(_product_total_banner(product_type, product_frame, styles))
        flowables.append(Spacer(1, 4 * mm))

    return flowables


def generate_pdf(data: SalesReportData, output_path: Path | str) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = _styles()

    doc = BaseDocTemplate(
        str(output_path),
        title=f"Sales Report {data.report_date.isoformat()}",
        author="Eva Dashboard",
    )

    portrait_frame = Frame(
        14 * mm,
        16 * mm,
        A4[0] - 28 * mm,
        A4[1] - 34 * mm,
        id="portrait",
    )
    landscape_size = landscape(A4)
    landscape_frame = Frame(
        10 * mm,
        14 * mm,
        landscape_size[0] - 20 * mm,
        landscape_size[1] - 30 * mm,
        id="landscape",
    )

    def draw_portrait(canvas, doc_):
        canvas.saveState()
        width, height = A4
        canvas.setStrokeColor(ACCENT)
        canvas.setLineWidth(1.5)
        canvas.line(14 * mm, height - 12 * mm, width - 14 * mm, height - 12 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawCentredString(width / 2, 10 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    def draw_landscape(canvas, doc_):
        canvas.saveState()
        width, height = landscape(A4)
        canvas.setStrokeColor(ACCENT)
        canvas.setLineWidth(1.5)
        canvas.line(10 * mm, height - 10 * mm, width - 10 * mm, height - 10 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawCentredString(width / 2, 8 * mm, f"Page {doc_.page}")
        canvas.setFont("Helvetica-Bold", 8)
        canvas.setFillColor(BRAND)
        canvas.drawString(10 * mm, height - 8 * mm, "EVA FOODS — Daily Sales Detail")
        canvas.restoreState()

    doc.addPageTemplates(
        [
            PageTemplate(id="summary", frames=[portrait_frame], onPage=draw_portrait, pagesize=A4),
            PageTemplate(
                id="detail",
                frames=[landscape_frame],
                onPage=draw_landscape,
                pagesize=landscape(A4),
            ),
        ]
    )

    clients_note = (
        f"Clients: {data.clients_path.name}" if data.clients_path else "Clients: not provided"
    )
    story = [
        Paragraph("EVA FOODS", styles["brand"]),
        Paragraph("Daily Sales Summary", styles["title"]),
        Paragraph(
            f"Report date: <b>{data.report_date.strftime('%d %B %Y')}</b> "
            f"(latest date in workbook treated as current)<br/>"
            f"Month-to-date window: {data.month_start.strftime('%d %b %Y')} "
            f"to {data.report_date.strftime('%d %b %Y')}<br/>"
            f"Avg Last 30 Days window: {data.trailing_30_start.strftime('%d %b %Y')} "
            f"to {data.report_date.strftime('%d %b %Y')} "
            f"(sum ÷ 30)<br/>"
            f"AMS months: {', '.join(data.ams_months) if data.ams_months else 'n/a'} "
            f"(mean of those monthly totals)<br/>"
            f"Source: {data.source_path.name} · {clients_note}",
            styles["meta"],
        ),
        Paragraph("Sales by Category (Metric Tons)", styles["section"]),
        _summary_table(data, styles),
        Spacer(1, 5 * mm),
        Paragraph("Daily Sales by City (MT)", styles["section"]),
        _city_daily_table(data, styles),
        Spacer(1, 5 * mm),
        Paragraph("Month-to-Date Sales by City (MT)", styles["section"]),
        _city_mtd_table(data, styles),
        Spacer(1, 5 * mm),
        Paragraph("Price Fetch by Client Type (Rs / Maund)", styles["section"]),
        _price_fetch_table(data, styles),
        Spacer(1, 5 * mm),
        Paragraph("Bulk Product Average Prices", styles["section"]),
        _bulk_product_price_table(data, styles),
        Spacer(1, 4 * mm),
        Paragraph(
            "Δ% is green when current is above the average baseline, red when below, "
            "and — when the baseline is zero (e.g. no prior-month history loaded yet). "
            "ADS = average daily sales over the last 30 days; AMS = average monthly sales "
            "over the prior 3 full months. City tables show the top 10 cities by total MT, "
            "plus an Other row for all remaining cities. "
            "Price Fetch = (Incl GST/FED per kg − cost factor per kg) × 37.3246; "
            "cost factors in litres are converted to per kg at 1 Ltr = 0.915 Kg "
            "(always computed in kg). "
            "Price Fetch columns split Eva / Maan × Oil / Ghee. "
            "Bulk product Daily Avg uses report-date sales; MTD Avg uses the month-to-date "
            "window. Bulk Oil prices are per maund (× 37.3246); other bulk categories per kg.",
            styles["meta"],
        ),
        NextPageTemplate("detail"),
        PageBreak(),
        Paragraph(
            f"All sales for {data.report_date.strftime('%d %B %Y')} — "
            f"{len(data.daily_sales)} line(s), "
            f"{data.daily_sales.groupby(['category', 'city', 'party'], sort=False).ngroups} customers",
            styles["section"],
        ),
        *_detail_toc_flowables(data, styles),
        PageBreak(),
        *_sales_detail_flowables(data, styles),
    ]

    try:
        doc.build(story)
    except TypeError as exc:
        # ReportLab's Table._culprit() can raise TypeError(None > int) when a
        # table fails to split; surface a clearer layout error instead.
        if "NoneType" in str(exc) and "int" in str(exc):
            raise RuntimeError(
                "A sales detail table was too large for the page and could not be "
                "split. Try again with the latest app version, or contact support "
                f"with report date {data.report_date.isoformat()}."
            ) from exc
        raise
    return output_path
