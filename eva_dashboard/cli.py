"""Command-line interface for Eva Dashboard."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from eva_dashboard import __version__
from eva_dashboard.data import prepare_report_data
from eva_dashboard.report import generate_pdf


def _parse_date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eva-dashboard",
        description=(
            "Build Eva Foods sales dashboard PDFs from Excel sales workbooks. "
            "Sheet 1 = Sales, Sheet 2 = Category mapping."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="Generate the sales PDF report")
    report.add_argument(
        "excel",
        type=Path,
        help="Path to the sales Excel workbook (.xlsx)",
    )
    report.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output PDF path (default: output/sales_report_YYYY-MM-DD.pdf)",
    )
    report.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        help="Report date YYYY-MM-DD (default: latest date in the workbook)",
    )
    return parser


def cmd_report(args: argparse.Namespace) -> int:
    excel = args.excel.expanduser().resolve()
    if not excel.exists():
        print(f"error: Excel file not found: {excel}", file=sys.stderr)
        return 1

    data = prepare_report_data(excel, report_date=args.date)
    output = args.output
    if output is None:
        output = Path("output") / f"sales_report_{data.report_date.isoformat()}.pdf"
    output = output.expanduser().resolve()

    pdf_path = generate_pdf(data, output)
    print(f"Report date : {data.report_date.isoformat()}")
    print(f"Categories  : {len(data.category_summary)}")
    print(f"Daily lines : {len(data.daily_sales)}")
    print(f"Daily MT    : {data.total_daily_mt:,.3f}")
    print(f"MTD MT      : {data.total_mtd_mt:,.3f}")
    print(f"Wrote PDF   : {pdf_path}")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "report":
        raise SystemExit(cmd_report(args))
    parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
