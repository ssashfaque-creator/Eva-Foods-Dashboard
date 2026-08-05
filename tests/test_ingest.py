"""Tests for SQLite ingest and file archive behaviour."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.ingest import (
    DuplicateFileError,
    ingest_clients,
    ingest_packing_costs,
    ingest_product_costs,
    ingest_sales,
    list_factor_client_types,
    load_clients_table,
    load_factor_costs_table,
    load_sales_table,
    sales_count,
)


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_SALES = ROOT / "data" / "sales.xlsx"
SAMPLE_CLIENTS = ROOT / "data" / "clients.xlsx"
SAMPLE_PCOST = ROOT / "data" / "product_costs.xlsx"
SAMPLE_PACK = ROOT / "data" / "packing_costs.xlsx"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _with_temp_data(fn):
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")
        try:
            fn()
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_ingest_sales_appends_and_dedupes_file():
    if not SAMPLE_SALES.exists():
        return

    def run():
        result = ingest_sales(SAMPLE_SALES, original_name="sales.xlsx")
        assert result["inserted"] > 0
        assert sales_count() == result["inserted"]
        archived = Path(result["stored_path"])
        assert archived.exists()
        assert "sales" in str(archived)

        try:
            ingest_sales(SAMPLE_SALES, original_name="sales.xlsx")
            raise AssertionError("expected DuplicateFileError")
        except DuplicateFileError:
            pass

        frame = load_sales_table(limit=100)
        assert not frame.empty
        assert list(frame["date"]) == sorted(frame["date"], reverse=True)

    _with_temp_data(run)


def test_ingest_clients_and_search():
    if not SAMPLE_CLIENTS.exists():
        return

    def run():
        result = ingest_clients(SAMPLE_CLIENTS, original_name="clients.xlsx")
        assert result["upserted"] > 0
        frame = load_clients_table(search="Imtiaz")
        assert len(frame) >= 1

    _with_temp_data(run)


def test_ingest_costs_builds_factor_table():
    product = FIXTURES / "product_costs.xlsx"
    packing = FIXTURES / "packing_costs.xlsx"
    if not product.exists() or not packing.exists():
        return

    def run():
        r1 = ingest_product_costs(product, original_name=product.name)
        assert r1["inserted"] > 0
        r2 = ingest_packing_costs(packing, original_name=packing.name)
        assert r2["inserted"] > 0
        assert r2["factor_rows"] > 0
        types = list_factor_client_types()
        assert types
        factors = load_factor_costs_table(types[0])
        assert not factors.empty
        assert "total_factor_cost" in factors.columns

    _with_temp_data(run)


if __name__ == "__main__":
    test_ingest_sales_appends_and_dedupes_file()
    test_ingest_clients_and_search()
    test_ingest_costs_builds_factor_table()
    print("ok")
