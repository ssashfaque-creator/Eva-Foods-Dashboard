"""Tests for client-type aliases, party lookup, and price queries."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    _dispatch_tool,
    _looks_party_lookup,
    _looks_price_query,
    _looks_sales_matrix,
)
from eva_dashboard.client_language import (
    extract_client_type_from_text,
    lookup_party,
    normalize_client_type,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.sales_query import query_price, query_sales, resolve_period


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed() -> None:
    init_db()
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, '{}', datetime('now'))",
            [
                ("Eva Canola Oil (StandUpPouch)", "Eva Consumer", "Eva Canola", "Stand up"),
                ("Eva Cooking Oil (StandUpPouch)", "Eva Consumer", "Eva Cooking", "Stand up"),
                ("Eva Cooking Oil (16 Ltr Tin)", "Eva Bulk", "Eva Bulk", "Tin"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', ?, datetime('now'))",
            [
                ("1", "Alpha Dist", "Eva Distributors", "Lahore", "Lahore", "{}"),
                ("2", "Imtiaz Super Market", "Imtiaz Store", "Karachi", "Karachi", "{}"),
                (
                    "3",
                    "Al-Bari Traders",
                    "Other Clients",
                    "Faisalabad",
                    "Faisalabad",
                    '{"Locality": "Jhang Road"}',
                ),
                ("4", "Al Bari Oil House", "Other Clients", "Multan", "Multan", "{}"),
            ],
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO factor_costs
            (client_type, prod_id, product, unit, product_cost, packing_cost,
             total_factor_cost, updated_at)
            VALUES ('Eva Distributors', 1, 'Eva Canola Oil (StandUpPouch)', 'Ltrs',
                    100, 50, 150.0, datetime('now'))
            """
        )
        rows = [
            # May–Aug for month-wise
            ("2026-05-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 20.0, 500.0, 100000.0, "Eva Distributors"),
            ("2026-06-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 22.0, 510.0, 112200.0, "Eva Distributors"),
            ("2026-07-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 25.0, 520.0, 130000.0, "Eva Distributors"),
            ("2026-07-06", "Imtiaz Super Market", "Eva Cooking Oil (StandUpPouch)", 40.0, 480.0, 192000.0, "Imtiaz Store"),
            ("2026-07-07", "Al-Bari Traders", "Eva Canola Oil (StandUpPouch)", 5.0, 530.0, 26500.0, "Other Clients"),
            ("2026-08-01", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 8.0, 525.0, 42000.0, "Eva Distributors"),
            ("2026-08-02", "Imtiaz Super Market", "Eva Cooking Oil (StandUpPouch)", 10.0, 485.0, 48500.0, "Imtiaz Store"),
            ("2026-08-03", "Al Bari Oil House", "Eva Cooking Oil (16 Ltr Tin)", 3.0, 400.0, 12000.0, "Other Clients"),
        ]
        for i, (dt, party, product, mt, rate, incl, ctype) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, ?, ?, '{}')
                """,
                (f"ct-{i}-{dt}", dt, party, product, mt, mt, rate, incl, ctype),
            )
        conn.commit()


def test_client_type_aliases() -> None:
    assert normalize_client_type("Imtiaz") == "Imtiaz Store"
    assert normalize_client_type("imtiaz stores") == "Imtiaz Store"
    assert normalize_client_type("store") == "Imtiaz Store"
    assert normalize_client_type("Distributor") == "Eva Distributors"
    assert normalize_client_type("Eva distributors") == "Eva Distributors"
    assert extract_client_type_from_text(
        "What's the Average sale for Imtiaz store last 6 months"
    ) == "Imtiaz Store"
    assert extract_client_type_from_text(
        "Canola standup price for Distributors last week"
    ) == "Eva Distributors"
    assert extract_client_type_from_text("canteen store sales") == (
        "Canteen Store Department"
    )


def test_imtiaz_month_wise_filters_client_type() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = query_sales(
                client_type="Imtiaz",
                columns="month",
                months_back=6,
            )
            assert out["ok"] is True
            assert out["filters"]["client_type"] == "Imtiaz Store"
            assert out["column_dimension"] == "month"
            assert out["filters"].get("business_unit") in (None, "")
            # Only Imtiaz volume in August
            matrix = out["matrix"]
            total_row = [r for r in matrix["rows"] if r.get(out["row_dimension"]) == "Total"][0]
            assert total_row.get("2026-08") == 10.0
            assert "Imtiaz Store" in out["answer_markdown"]
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_dispatch_drops_invented_bu_for_imtiaz() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = _dispatch_tool(
                "query_sales",
                {"business_unit": "Eva Consumer", "columns": "month"},
                user_text="What's the Average sale for Imtiaz store last 6 months",
            )
            assert out["ok"] is True
            assert out["filters"]["client_type"] == "Imtiaz Store"
            assert not out["filters"].get("business_unit")
            assert out["column_dimension"] == "month"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_lookup_al_bari() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = lookup_party("Al Bari")
            assert out["ok"] is True
            names = [m["client"] for m in out["matches"]]
            assert any("Al-Bari" in n or "Al Bari" in n for n in names)
            assert out["matches"][0]["client_type"]
            assert "Client Type" in out["answer_markdown"]
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_resolve_last_week() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            week = resolve_period("last week")
            assert week["date_to"] == "2026-08-03"
            assert week["date_from"] == "2026-07-28"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_query_price_rate_and_price_fetch() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = query_price(
                period="July 2026",
                client_type="Distributors",
                packing_category="Stand up",
                oil_type="canola",
                include_price_fetch=False,
            )
            assert out["ok"] is True
            assert out["filters"]["client_type"] == "Eva Distributors"
            assert out["avg_rate"] is not None
            assert out["avg_rate"] == 520.0
            assert "Avg Rate" in out["answer_markdown"]

            pf = query_price(
                period="July 2026",
                client_type="Eva Distributors",
                product="Eva Canola Oil (StandUpPouch)",
                include_price_fetch=True,
            )
            assert pf["include_price_fetch"] is True
            assert pf["price_fetch"] is not None
            assert "Price Fetch" in pf["answer_markdown"]
            # Cost factor shown in stored unit (Ltrs) alongside Price Fetch
            assert pf["cost_factor"] == 150.0
            assert pf["cost_unit"] == "Ltrs"
            assert "Cost Factor (Ltrs)" in pf["answer_markdown"]
            assert "150" in pf["answer_markdown"]
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_cost_factor_and_packing_cost_asks() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            from eva_dashboard.chatbot import (
                _dispatch_tool,
                _looks_cost_factor_ask,
                _looks_factor_only_ask,
                resolve_forced_tool,
            )
            from eva_dashboard.sales_query import query_factor_costs

            assert _looks_cost_factor_ask("what's the cost factor?")
            assert _looks_cost_factor_ask("show factor breakdown")
            assert _looks_cost_factor_ask(
                "what's the packing cost for Eva Canola Oil (StandUpPouch)"
            )
            assert _looks_factor_only_ask("show factor breakdown")
            assert resolve_forced_tool("what's the cost factor?") == "query_price"
            assert resolve_forced_tool(
                "packing cost for standup canola distributors"
            ) == "query_price"

            factors = query_factor_costs(
                client_type="Eva Distributors",
                product="Eva Canola Oil (StandUpPouch)",
                breakdown=True,
            )
            assert factors["ok"] is True
            assert factors["rows"]
            assert factors["rows"][0]["unit"] == "Ltrs"
            assert factors["rows"][0]["packing_cost"] == 50.0
            assert "Packing Cost (Ltrs)" in factors["answer_markdown"]
            assert "Product Cost (Ltrs)" in factors["answer_markdown"]
            assert "Total Factor Cost (Ltrs)" in factors["answer_markdown"]

            out = _dispatch_tool(
                "query_price",
                {},
                user_text="show factor breakdown for distributors canola standup",
            )
            assert out["ok"] is True
            assert out.get("mode") == "factor_costs"
            assert "Packing Cost" in out["answer_markdown"]
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_routing_helpers() -> None:
    assert _looks_party_lookup("Who is Al Bari?")
    assert not _looks_sales_matrix("Who is Al Bari?")
    assert _looks_price_query("Canola standup price for Distributors last week")
    assert not _looks_sales_matrix("Canola standup price for Distributors last week")
    assert _looks_sales_matrix("Average sale for Imtiaz store last 6 months")
    assert not _looks_price_query("Average sale for Imtiaz store last 6 months")
    assert _looks_price_query("what's the cost factor for this")


def test_row_drilldown_bu_to_packing_to_sku() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            base = query_sales(
                client_type="Imtiaz Store",
                columns="month",
                months_back=6,
            )
            assert base["row_dimension"] == "business_unit"
            prior = base["table_spec"]

            from eva_dashboard.chatbot import (
                _dispatch_tool,
                resolve_row_dimension_request,
            )

            assert resolve_row_dimension_request("show by product") == "packing_category"
            assert resolve_row_dimension_request("can you show product wise") == (
                "packing_category"
            )
            assert resolve_row_dimension_request("product-wise") == "packing_category"
            assert resolve_row_dimension_request(
                "dissect further", prior_row_dimension="business_unit"
            ) == "packing_category"
            assert resolve_row_dimension_request(
                "dissect further", prior_row_dimension="packing_category"
            ) == "product"
            assert resolve_row_dimension_request("SKU wise breakdown") == "product"
            assert resolve_row_dimension_request("break it down further") == (
                "packing_category"
            )

            packing = _dispatch_tool(
                "query_sales",
                {},
                user_text="Can you show by product",
                prior_spec=prior,
            )
            assert packing["ok"] is True
            assert packing["row_dimension"] == "packing_category"
            # Model wrongly asking for SKU on "product wise" must stay packing
            packing_wise = _dispatch_tool(
                "query_sales",
                {"row_dimension": "product"},
                user_text="can you show product wise",
                prior_spec=prior,
            )
            assert packing_wise["ok"] is True
            assert packing_wise["row_dimension"] == "packing_category"
            assert packing["column_dimension"] == "month"
            assert packing["filters"]["client_type"] == "Imtiaz Store"

            sku = _dispatch_tool(
                "query_sales",
                {},
                user_text="dissect further / show SKU wise",
                prior_spec=packing["table_spec"],
            )
            assert sku["ok"] is True
            assert sku["row_dimension"] == "product"
            assert sku["column_dimension"] == "month"
            assert sku["filters"]["client_type"] == "Imtiaz Store"
            products = [
                r["product"]
                for r in sku["matrix"]["rows"]
                if r.get("product")
                and r.get("row_kind", "leaf") == "leaf"
            ]
            assert products  # at least one SKU row
            assert sku["matrix"].get("hierarchical") is True
            assert sku["matrix"].get("row_headers") == [
                "business_unit",
                "packing_category",
                "product",
            ]
            assert any(
                r.get("row_kind") == "subtotal_packing_category" for r in sku["matrix"]["rows"]
            )
            assert any(
                r.get("row_kind") == "subtotal_business_unit"
                for r in sku["matrix"]["rows"]
            )
            assert packing["matrix"].get("hierarchical") is True
            assert packing["matrix"].get("row_headers") == [
                "business_unit",
                "packing_category",
            ]
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
