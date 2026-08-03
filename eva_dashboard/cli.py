"""Command-line interface for Eva Dashboard."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from eva_dashboard import __version__
from eva_dashboard.costs import compute_total_factor_costs, save_factor_costs
from eva_dashboard.data import load_factor_costs_frame, prepare_report_data
from eva_dashboard.report import generate_pdf


def _parse_date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eva-dashboard",
        description=(
            "Build Eva Foods sales dashboard PDFs from Excel sales + client workbooks."
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
        "--clients",
        type=Path,
        default=None,
        help="Path to the clients Excel workbook (must include City-Filter)",
    )
    report.add_argument(
        "--product-costs",
        type=Path,
        default=None,
        help="Product cost factors Excel workbook (used with --packing-costs)",
    )
    report.add_argument(
        "--packing-costs",
        type=Path,
        default=None,
        help="Packing costs Excel workbook (used with --product-costs)",
    )
    report.add_argument(
        "--factor-costs",
        type=Path,
        default=None,
        help="Precomputed total factor costs CSV/XLSX (skips product+packing compute)",
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

    costs = sub.add_parser(
        "costs",
        help="Compute total factor cost per product for every client type",
    )
    costs.add_argument(
        "product_costs",
        type=Path,
        help="Product cost factors Excel workbook",
    )
    costs.add_argument(
        "packing_costs",
        type=Path,
        help="Packing costs Excel workbook",
    )
    costs.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=(
            "Output path (.csv or .xlsx). "
            "Default: output/total_factor_costs.csv"
        ),
    )
    return parser


def _resolve_factor_costs(args: argparse.Namespace):
    if args.factor_costs is not None:
        path = args.factor_costs.expanduser().resolve()
        if not path.exists():
            print(f"error: Factor costs file not found: {path}", file=sys.stderr)
            return None, 1
        return load_factor_costs_frame(path), 0

    if args.product_costs is not None or args.packing_costs is not None:
        if args.product_costs is None or args.packing_costs is None:
            print(
                "error: provide both --product-costs and --packing-costs "
                "(or pass --factor-costs)",
                file=sys.stderr,
            )
            return None, 1
        product_costs = args.product_costs.expanduser().resolve()
        packing_costs = args.packing_costs.expanduser().resolve()
        if not product_costs.exists():
            print(f"error: Product costs file not found: {product_costs}", file=sys.stderr)
            return None, 1
        if not packing_costs.exists():
            print(f"error: Packing costs file not found: {packing_costs}", file=sys.stderr)
            return None, 1
        result = compute_total_factor_costs(product_costs, packing_costs)
        return result.frame, 0

    return None, 0


def cmd_report(args: argparse.Namespace) -> int:
    excel = args.excel.expanduser().resolve()
    if not excel.exists():
        print(f"error: Excel file not found: {excel}", file=sys.stderr)
        return 1

    clients = None
    if args.clients is not None:
        clients = args.clients.expanduser().resolve()
        if not clients.exists():
            print(f"error: Clients file not found: {clients}", file=sys.stderr)
            return 1

    factor_costs, err = _resolve_factor_costs(args)
    if err:
        return err

    data = prepare_report_data(
        excel,
        clients_path=clients,
        report_date=args.date,
        factor_costs=factor_costs,
    )
    output = args.output
    if output is None:
        output = Path("output") / f"sales_report_{data.report_date.isoformat()}.pdf"
    output = output.expanduser().resolve()

    pdf_path = generate_pdf(data, output)
    matched = int(data.daily_sales["cost_factor"].notna().sum()) if "cost_factor" in data.daily_sales else 0
    print(f"Report date : {data.report_date.isoformat()}")
    print(f"Categories  : {len(data.category_summary)}")
    print(f"Cities daily: {len(data.city_daily)}")
    print(f"Cities MTD  : {len(data.city_mtd)}")
    print(f"Daily lines : {len(data.daily_sales)}")
    print(f"Cost matched: {matched}/{len(data.daily_sales)}")
    print(f"Price fetch : {len(data.price_fetch_summary)} client type(s)")
    print(f"Bulk prices : {len(data.bulk_product_prices)} product(s)")
    print(f"Daily MT    : {data.total_daily_mt:,.3f}")
    print(f"MTD MT      : {data.total_mtd_mt:,.3f}")
    print(f"Wrote PDF   : {pdf_path}")
    return 0


def cmd_costs(args: argparse.Namespace) -> int:
    product_costs = args.product_costs.expanduser().resolve()
    packing_costs = args.packing_costs.expanduser().resolve()
    if not product_costs.exists():
        print(f"error: Product costs file not found: {product_costs}", file=sys.stderr)
        return 1
    if not packing_costs.exists():
        print(f"error: Packing costs file not found: {packing_costs}", file=sys.stderr)
        return 1

    result = compute_total_factor_costs(product_costs, packing_costs)
    output = args.output
    if output is None:
        output = Path("output") / "total_factor_costs.csv"
    output = output.expanduser().resolve()

    out_path = save_factor_costs(result, output)
    units = sorted(result.frame["Unit"].dropna().unique().tolist())
    print(f"Client types : {result.frame['ClientType'].nunique()}")
    print(f"Products     : {result.frame['ProdID'].nunique()}")
    print(f"Factor rows  : {len(result.frame)}")
    print(f"Units        : {', '.join(units)}")
    print(f"No packing   : {result.products_without_packing}")
    print(f"Wrote        : {out_path}")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "report":
        raise SystemExit(cmd_report(args))
    if args.command == "costs":
        raise SystemExit(cmd_costs(args))
    parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
