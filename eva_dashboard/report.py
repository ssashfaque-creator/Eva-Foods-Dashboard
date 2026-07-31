"""PDF report generation for sales dashboard."""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
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

from eva_dashboard.data import SalesReportData


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
    }


def _fmt_mt(value: float) -> str:
    return f"{value:,.3f}"


def _fmt_qty(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}"
    return f"{value:,.2f}"


def _fmt_money(value: float) -> str:
    return f"{value:,.2f}"


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
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), HEADER_FG),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("BACKGROUND", (0, -1), (-1, -1), colors.Color(0.86, 0.91, 0.88)),
        ("LINEABOVE", (0, -1), (-1, -1), 1.0, BRAND),
    ]
    for i in range(1, len(rows) - 1):
        if i % 2 == 0:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), ROW_ALT))
    table.setStyle(TableStyle(style_cmds))
    return table


def _sales_table(data: SalesReportData, styles: dict[str, ParagraphStyle]) -> Table:
    header = [
        Paragraph("Category", styles["cell_bold"]),
        Paragraph("Party", styles["cell_bold"]),
        Paragraph("Product", styles["cell_bold"]),
        Paragraph("Qty", styles["cell_bold"]),
        Paragraph("Unit", styles["cell_bold"]),
        Paragraph("M.T Qty", styles["cell_bold"]),
        Paragraph("Rate", styles["cell_bold"]),
        Paragraph("Basic Amount", styles["cell_bold"]),
        Paragraph("Incl Gst/Fed", styles["cell_bold"]),
        Paragraph("Amount per KG", styles["cell_bold"]),
    ]
    rows: list[list] = [header]
    for _, row in data.daily_sales.iterrows():
        rows.append(
            [
                Paragraph(str(row["category"]), styles["cell"]),
                Paragraph(str(row["party"]), styles["cell"]),
                Paragraph(str(row["product"]), styles["cell"]),
                Paragraph(_fmt_qty(float(row["qty"])), styles["cell_right"]),
                Paragraph(str(row["unit"]), styles["cell"]),
                Paragraph(_fmt_mt(float(row["mt_qty"])), styles["cell_right"]),
                Paragraph(_fmt_money(float(row["rate"])), styles["cell_right"]),
                Paragraph(_fmt_money(float(row["basic_amount"])), styles["cell_right"]),
                Paragraph(_fmt_money(float(row["incl_gst_fed"])), styles["cell_right"]),
                Paragraph(_fmt_money(float(row["amount_per_kg"])), styles["cell_right"]),
            ]
        )

    # Landscape A4 usable width ~273mm; keep columns readable with party included
    col_widths = [
        22 * mm,  # Category (Category 2)
        42 * mm,  # Party
        48 * mm,  # Product
        16 * mm,  # Qty
        12 * mm,  # Unit
        18 * mm,  # M.T Qty
        22 * mm,  # Rate
        26 * mm,  # Basic Amount
        26 * mm,  # Incl Gst/Fed
        24 * mm,  # Amount per KG
    ]
    table = Table(rows, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("GRID", (0, 0), (-1, -1), 0.3, LINE),
    ]
    for i in range(1, len(rows)):
        if i % 2 == 0:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), ROW_ALT))
    table.setStyle(TableStyle(style_cmds))
    return table


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
        16 * mm,
        16 * mm,
        A4[0] - 32 * mm,
        A4[1] - 34 * mm,
        id="portrait",
    )
    landscape_size = landscape(A4)
    landscape_frame = Frame(
        12 * mm,
        14 * mm,
        landscape_size[0] - 24 * mm,
        landscape_size[1] - 30 * mm,
        id="landscape",
    )

    def draw_portrait(canvas, doc_):
        canvas.saveState()
        width, height = A4
        canvas.setStrokeColor(ACCENT)
        canvas.setLineWidth(1.5)
        canvas.line(15 * mm, height - 12 * mm, width - 15 * mm, height - 12 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawCentredString(width / 2, 10 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    def draw_landscape(canvas, doc_):
        canvas.saveState()
        width, height = landscape(A4)
        canvas.setStrokeColor(ACCENT)
        canvas.setLineWidth(1.5)
        canvas.line(12 * mm, height - 10 * mm, width - 12 * mm, height - 10 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawCentredString(width / 2, 8 * mm, f"Page {doc_.page}")
        canvas.setFont("Helvetica-Bold", 8)
        canvas.setFillColor(BRAND)
        canvas.drawString(12 * mm, height - 8 * mm, "EVA FOODS — Daily Sales Detail")
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

    story = [
        Paragraph("EVA FOODS", styles["brand"]),
        Paragraph("Daily Sales Summary", styles["title"]),
        Paragraph(
            f"Report date: <b>{data.report_date.strftime('%d %B %Y')}</b> "
            f"(latest date in workbook treated as current)<br/>"
            f"Month-to-date window: {data.month_start.strftime('%d %b %Y')} "
            f"to {data.report_date.strftime('%d %b %Y')}<br/>"
            f"Source: {data.source_path.name}",
            styles["meta"],
        ),
        Paragraph("Sales by Category (Metric Tons)", styles["section"]),
        _summary_table(data, styles),
        Spacer(1, 6 * mm),
        Paragraph(
            "Daily Sales (MT) uses rows dated on the report date. "
            "Month-to-Date Sales (MT) sums all rows from the 1st of the month through the report date. "
            "For bulk Kgs lines where Excel M.T Qty is blank/zero, Qty ÷ 1000 is used.",
            styles["meta"],
        ),
        NextPageTemplate("detail"),
        PageBreak(),
        Paragraph(
            f"All sales for {data.report_date.strftime('%d %B %Y')} — "
            f"{len(data.daily_sales)} line(s)",
            styles["section"],
        ),
        _sales_table(data, styles),
    ]

    doc.build(story)
    return output_path
