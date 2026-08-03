"""PDF report generation for sales dashboard."""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
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
    CITY_DETAIL_CATEGORIES,
    SalesReportData,
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
    }


def _fmt_mt(value: float) -> str:
    return f"{value:,.3f}"


def _fmt_qty(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}"
    return f"{value:,.2f}"


def _fmt_money(value: float) -> str:
    return f"{value:,.2f}"


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
        Paragraph("Month-to-Date Sales (MT)", styles["cell_bold"]),
    ]
    rows: list[list] = [header]
    for row in data.category_summary:
        rows.append(
            [
                Paragraph(row.category1, styles["cell"]),
                Paragraph(_fmt_mt(row.daily_mt), styles["cell_right"]),
                Paragraph(_fmt_mt(row.mtd_mt), styles["cell_right"]),
            ]
        )
    rows.append(
        [
            Paragraph("Total", styles["cell_bold_dark"]),
            Paragraph(_fmt_mt(data.total_daily_mt), styles["cell_right"]),
            Paragraph(_fmt_mt(data.total_mtd_mt), styles["cell_right"]),
        ]
    )

    table = Table(rows, colWidths=[70 * mm, 50 * mm, 55 * mm], hAlign="LEFT")
    table.setStyle(TableStyle(_base_table_style(len(rows))))
    return table


def _city_brand_table(
    frame,
    styles: dict[str, ParagraphStyle],
) -> Table:
    brands = list(CITY_BRAND_COLUMNS)
    header = [Paragraph("City", styles["cell_bold"])] + [
        Paragraph(name, styles["cell_bold"]) for name in brands
    ] + [Paragraph("Total", styles["cell_bold"])]
    rows: list[list] = [header]

    totals = {name: 0.0 for name in brands}
    grand = 0.0
    for _, row in frame.iterrows():
        cells = [Paragraph(str(row["city"]), styles["cell"])]
        row_total = 0.0
        for name in brands:
            value = float(row.get(name, 0.0) or 0.0)
            totals[name] += value
            row_total += value
            cells.append(Paragraph(_fmt_mt(value), styles["cell_right"]))
        grand += row_total
        cells.append(Paragraph(_fmt_mt(row_total), styles["cell_right"]))
        rows.append(cells)

    total_row = [Paragraph("Total", styles["cell_bold_dark"])]
    for name in brands:
        total_row.append(Paragraph(_fmt_mt(totals[name]), styles["cell_right"]))
    total_row.append(Paragraph(_fmt_mt(grand), styles["cell_right"]))
    rows.append(total_row)

    col_widths = [32 * mm] + [28 * mm] * len(brands) + [28 * mm]
    table = Table(rows, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    table.setStyle(TableStyle(_base_table_style(len(rows))))
    return table



def _sales_identity_and_sku_widths() -> list[float]:
    return [
        24 * mm,  # Category = client Type (merged)
        20 * mm,  # City = City-Filter (merged)
        36 * mm,  # Party (merged)
        40 * mm,  # Product
        14 * mm,  # Qty
        11 * mm,  # Unit
        16 * mm,  # M.T Qty
        20 * mm,  # Rate
        24 * mm,  # Basic Amount
        24 * mm,  # Incl Gst/Fed
        22 * mm,  # Amount per KG
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
    ]


def _sales_header_cells(styles: dict[str, ParagraphStyle]) -> list:
    return [Paragraph(label, styles["cell_bold"]) for label in _sales_header_labels()]


def _blank_identity() -> list:
    return ["", "", ""]


def _sku_cells(row, styles: dict[str, ParagraphStyle]) -> list:
    return [
        Paragraph(str(row["product"]), styles["cell"]),
        Paragraph(_fmt_qty(float(row["qty"])), styles["cell_right"]),
        Paragraph(str(row["unit"]), styles["cell"]),
        Paragraph(_fmt_mt(float(row["mt_qty"])), styles["cell_right"]),
        Paragraph(_fmt_money(float(row["rate"])), styles["cell_right"]),
        Paragraph(_fmt_money(float(row["basic_amount"])), styles["cell_right"]),
        Paragraph(_fmt_money(float(row["incl_gst_fed"])), styles["cell_right"]),
        Paragraph(_fmt_money(float(row["amount_per_kg"])), styles["cell_right"]),
    ]


def _total_cells(
    label: str,
    total_mt: float,
    total_basic: float,
    total_incl: float,
    blended_rate: float,
    styles: dict[str, ParagraphStyle],
) -> list:
    return [
        Paragraph(label, styles["cell_bold_dark"]),
        Paragraph("", styles["cell"]),
        Paragraph("", styles["cell"]),
        Paragraph(_fmt_mt(total_mt), styles["cell_right_bold"]),
        Paragraph(_fmt_money(blended_rate), styles["cell_right_bold"]),
        Paragraph(_fmt_money(total_basic), styles["cell_right_bold"]),
        Paragraph(_fmt_money(total_incl), styles["cell_right_bold"]),
        Paragraph(_fmt_money(blended_rate), styles["cell_right_bold"]),
    ]


def _frame_totals(frame) -> tuple[float, float, float, float]:
    total_mt = float(frame["mt_qty"].sum())
    total_basic = float(frame["basic_amount"].sum())
    total_incl = float(frame["incl_gst_fed"].sum())
    total_kg = total_mt * 1000.0
    blended_rate = (total_incl / total_kg) if total_kg else 0.0
    return total_mt, total_basic, total_incl, blended_rate


def _party_mt_totals(frame) -> dict[tuple[str, str, str], float]:
    return (
        frame.groupby(["category", "city", "party"], sort=False)["mt_qty"]
        .sum()
        .to_dict()
    )


def _section_sales_table(
    frame,
    styles: dict[str, ParagraphStyle],
    section_total_label: str,
) -> Table:
    """City/product section table: header over data, repeats on each new page."""
    rows: list[list] = [_sales_header_cells(styles)]
    style_cmds: list = [
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

    party_mt = _party_mt_totals(frame)
    keys = sorted(
        party_mt.keys(),
        key=lambda key: (-party_mt[key], key[2], key[0], key[1]),
    )

    for index, (category, city, party) in enumerate(keys):
        group = frame[
            (frame["category"] == category)
            & (frame["city"] == city)
            & (frame["party"] == party)
        ]
        start = len(rows)
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

        total_mt, total_basic, total_incl, blended_rate = _frame_totals(group)
        rows.append(
            _blank_identity()
            + _total_cells(
                "Customer Total",
                total_mt,
                total_basic,
                total_incl,
                blended_rate,
                styles,
            )
        )
        end = len(rows) - 1
        style_cmds.extend(
            [
                ("SPAN", (0, start), (0, end)),
                ("SPAN", (1, start), (1, end)),
                ("SPAN", (2, start), (2, end)),
                ("VALIGN", (0, start), (2, end), "MIDDLE"),
                ("ALIGN", (0, start), (2, end), "CENTER"),
                ("BACKGROUND", (0, start), (2, end), ROW_ALT),
                ("BACKGROUND", (3, end), (-1, end), colors.Color(0.86, 0.91, 0.88)),
                ("LINEABOVE", (0, start), (-1, start), 0.7, ACCENT),
                ("LINEABOVE", (3, end), (-1, end), 0.7, BRAND),
            ]
        )
        if index % 2 == 1 and end > start:
            style_cmds.append(
                ("BACKGROUND", (3, start), (-1, end - 1), colors.Color(0.90, 0.94, 0.92))
            )

    total_mt, total_basic, total_incl, blended_rate = _frame_totals(frame)
    section_row = len(rows)
    rows.append(
        _blank_identity()
        + _total_cells(
            section_total_label,
            total_mt,
            total_basic,
            total_incl,
            blended_rate,
            styles,
        )
    )
    style_cmds.extend(
        [
            ("SPAN", (0, section_row), (2, section_row)),
            ("BACKGROUND", (0, section_row), (-1, section_row), colors.Color(0.82, 0.89, 0.85)),
            ("LINEABOVE", (0, section_row), (-1, section_row), 1.0, BRAND),
            ("VALIGN", (0, section_row), (-1, section_row), "MIDDLE"),
        ]
    )

    table = Table(
        rows,
        colWidths=_sales_identity_and_sku_widths(),
        repeatRows=1,
        hAlign="LEFT",
    )
    table.setStyle(TableStyle(style_cmds))
    return table


def _product_total_banner(
    product_type: str,
    frame,
    styles: dict[str, ParagraphStyle],
) -> Table:
    total_mt, total_basic, total_incl, blended_rate = _frame_totals(frame)
    row = _blank_identity() + _total_cells(
        f"Product Total — {product_type}",
        total_mt,
        total_basic,
        total_incl,
        blended_rate,
        styles,
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
    present = set(frame["product_type"].unique())
    ordered = [name for name in CATEGORY1_ORDER if name in present]
    extras = sorted(present - set(CATEGORY1_ORDER))
    return ordered + extras


def _ordered_cities_for_product(product_frame, city_rank: list[str]) -> list[str]:
    present = set(product_frame["city"].unique())
    ordered = [city for city in city_rank if city in present]
    leftover = sorted(
        present - set(ordered),
        key=lambda c: (
            -float(product_frame.loc[product_frame["city"] == c, "mt_qty"].sum()),
            c,
        ),
    )
    return ordered + leftover


def _sales_detail_flowables(
    data: SalesReportData, styles: dict[str, ParagraphStyle]
) -> list:
    """Section titles first; column header is the first row of each section table."""
    frame = data.daily_sales
    city_rank = (
        [str(c) for c in data.city_daily["city"].tolist()] if len(data.city_daily) else []
    )
    flowables: list = []

    for product_type in _ordered_product_types(frame):
        product_frame = frame[frame["product_type"] == product_type]
        if product_frame.empty:
            continue

        flowables.append(Paragraph(str(product_type), styles["product_heading"]))

        if product_type in CITY_DETAIL_CATEGORIES:
            for city in _ordered_cities_for_product(product_frame, city_rank):
                city_frame = product_frame[product_frame["city"] == city]
                if city_frame.empty:
                    continue
                flowables.append(Paragraph(f"City: {city}", styles["city_heading"]))
                flowables.append(
                    _section_sales_table(
                        city_frame,
                        styles,
                        section_total_label=f"City Total — {city}",
                    )
                )
                flowables.append(Spacer(1, 3 * mm))
            flowables.append(_product_total_banner(product_type, product_frame, styles))
            flowables.append(Spacer(1, 4 * mm))
        else:
            flowables.append(
                _section_sales_table(
                    product_frame,
                    styles,
                    section_total_label=f"Product Total — {product_type}",
                )
            )
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
            f"Source: {data.source_path.name} · {clients_note}",
            styles["meta"],
        ),
        Paragraph("Sales by Category (Metric Tons)", styles["section"]),
        _summary_table(data, styles),
        Spacer(1, 5 * mm),
        Paragraph("Daily Sales by City (MT)", styles["section"]),
        _city_brand_table(data.city_daily, styles),
        Spacer(1, 5 * mm),
        Paragraph("Month-to-Date Sales by City (MT)", styles["section"]),
        _city_brand_table(data.city_mtd, styles),
        Spacer(1, 4 * mm),
        Paragraph(
            "City rows use the client master <b>City-Filter</b> column and are sorted by total MT (high → low). "
            "Category summary follows Eva Consumer → Eva Bulk → Maan Consumer → Maan Bulk → "
            "Cusine King → Shortening → Bulk Oil → Meal → Byproducts. "
            "Detail is sectioned by product type (and city for consumer/bulk/cuisine brands).",
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
        *_sales_detail_flowables(data, styles),
    ]

    doc.build(story)
    return output_path
