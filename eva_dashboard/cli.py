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
            "Eva Foods dashboard: manage sales/cost/client Excel uploads and build PDF reports."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    app = sub.add_parser("app", help="Launch the Eva Foods web app (browser UI)")
    app.add_argument(
        "--port",
        type=int,
        default=8501,
        help="Port for the local web app (default: 8501)",
    )
    app.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Override data directory (default: ./data or EVA_DATA_DIR)",
    )

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

    update = sub.add_parser(
        "update",
        help="Download the latest app from GitHub and reinstall (keeps data/)",
    )
    update.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Install folder (default: current folder, EVA_HOME, or package root)",
    )
    update.add_argument(
        "--branch",
        default=None,
        help="GitHub branch (default: EVA_UPDATE_BRANCH or cursor/sales-dashboard-pdf-8203)",
    )
    update.add_argument(
        "--repo",
        default=None,
        help="GitHub owner/repo (default: EVA_UPDATE_REPO or ssashfaque-creator/Eva-Foods-Dashboard)",
    )
    update.add_argument(
        "--no-reinstall",
        action="store_true",
        help="Only refresh files; skip pip install -e .",
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


def cmd_app(args: argparse.Namespace) -> int:
    import os
    from streamlit.web import cli as stcli

    if args.data_dir is not None:
        os.environ["EVA_DATA_DIR"] = str(args.data_dir.expanduser().resolve())

    app_path = Path(__file__).resolve().parent / "app.py"
    sys.argv = [
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        str(args.port),
        "--browser.gatherUsageStats",
        "false",
    ]
    raise SystemExit(stcli.main())


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


def cmd_update(args: argparse.Namespace) -> int:
    from eva_dashboard.update import run_update

    print("Downloading latest Eva Foods Dashboard…")
    try:
        result = run_update(
            install_dir=args.dir,
            repo=args.repo,
            branch=args.branch,
            reinstall=not args.no_reinstall,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Install folder : {result['install_root']}")
    print(f"Source         : {result['repo']} @ {result['branch']}")
    print(f"Version        : {result['old_version']} → {result['new_version']}")
    print(f"Files updated  : {', '.join(result['copied'])}")
    print(f"Data kept      : {'yes' if result['data_preserved'] else 'n/a'}")
    print(f"venv kept      : {'yes' if result['venv_preserved'] else 'n/a'}")
    print()
    print("Done. Restart the app:")
    print(f'  cd "{result["install_root"]}"')
    print("  source .venv/bin/activate")
    print("  eva-dashboard app")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "app":
        raise SystemExit(cmd_app(args))
    if args.command == "report":
        raise SystemExit(cmd_report(args))
    if args.command == "costs":
        raise SystemExit(cmd_costs(args))
    if args.command == "update":
        raise SystemExit(cmd_update(args))
    parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
