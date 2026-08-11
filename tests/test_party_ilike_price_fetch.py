"""Silent party ILIKE + dedicated Price Fetch table + product/SKU vocab."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.db import connect, init_db
from eva_dashboard.party_match import resolve_party_filter, resolve_party_filters
from eva_dashboard.query_executor import execute_query_spec
from eva_dashboard.sales_query import normalize_row_dimension


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
                ("Eva Canola Oil (5 Ltr Bottle)", "Eva Consumer", "Eva Canola", "Pet bottle"),
                ("Eva Cooking Oil (StandUpPouch)", "Eva Consumer", "Eva Cooking", "Stand up"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "AL SHAHEER CORPORATION LIMITED", "Eva Distributors", "Karachi", "Karachi"),
                ("2", "AL SHAHEER TRADERS", "Eva Distributors", "Lahore", "Lahore"),
                ("3", "Metro Habib Cash & Carry", "Modern Trade", "Karachi", "Karachi"),
                ("4", "Alpha Dist", "Eva Distributors", "Lahore", "Lahore"),
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
        conn.execute(
            """
            INSERT OR REPLACE INTO factor_costs
            (client_type, prod_id, product, unit, product_cost, packing_cost,
             total_factor_cost, updated_at)
            VALUES ('Eva Distributors', 2, 'Eva Canola Oil (5 Ltr Bottle)', 'Ltrs',
                    110, 40, 150.0, datetime('now'))
            """
        )
        rows = [
            # Al Shaheer Corp — standup (mt large enough to survive mt_round)
            (
                "2026-07-10",
                "AL SHAHEER CORPORATION LIMITED",
                "Eva Canola Oil (StandUpPouch)",
                1000,
                "Ctn",
                5000,
                "Ltrs",
                4.575,
                2000,
                2_000_000,
                2_360_000,
            ),
            # Al Shaheer Traders — 5 Ltr
            (
                "2026-07-11",
                "AL SHAHEER TRADERS",
                "Eva Canola Oil (5 Ltr Bottle)",
                800,
                "Ctn",
                4000,
                "Ltrs",
                3.66,
                2500,
                2_000_000,
                2_360_000,
            ),
            # Metro Habib
            (
                "2026-07-12",
                "Metro Habib Cash & Carry",
                "Eva Canola Oil (StandUpPouch)",
                500,
                "Ctn",
                2500,
                "Ltrs",
                2.2875,
                2100,
                1_050_000,
                1_239_000,
            ),
            # Alpha (noise)
            (
                "2026-07-13",
                "Alpha Dist",
                "Eva Cooking Oil (StandUpPouch)",
                400,
                "Ctn",
                2000,
                "Ltrs",
                1.83,
                1900,
                760_000,
                896_800,
            ),
        ]
        for i, (
            dt,
            party,
            product,
            qty,
            unit,
            mes,
            mes_u,
            mt,
            rate,
            basic,
            incl,
        ) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mes_qty, mes_unit, mt_qty, rate, basic_amount,
                  incl_gst_fed_amount, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'Eva Distributors', '{}')
                """,
                (
                    f"pil-{i}",
                    dt,
                    party,
                    product,
                    qty,
                    unit,
                    mes,
                    mes_u,
                    mt,
                    rate,
                    basic,
                    incl,
                ),
            )
        conn.commit()


def test_spoken_product_maps_to_packing_sku_to_product() -> None:
    # Schema token "product" stays SKU; spoken product-wise → packing
    assert normalize_row_dimension("product") == "product"
    assert normalize_row_dimension("product-wise") == "packing_category"
    assert normalize_row_dimension("by product") == "packing_category"
    assert normalize_row_dimension("sku") == "product"
    assert normalize_row_dimension("sku-wise") == "product"


def test_resolve_party_silent_ilike_for_al_shaheer() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            hit = resolve_party_filter("al shaheer")
            assert hit["ok"] is True
            assert hit.get("party") is None
            assert hit.get("party_ilike") == ["al shaheer"]
            assert len(hit.get("matches") or []) >= 2
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_execute_al_shaheer_does_not_error() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "row_dimensions": ["party"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "filters": {"party": "al shaheer"},
                },
                user_text="al shaheer sales last 6 months",
            )
            assert out["ok"] is True, out
            md = out.get("answer_markdown") or ""
            assert "SHAHEER" in md.upper() or "shaheer" in md.lower()
            # Must not kick ambiguous error to LLM
            assert not out.get("plan_errors")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_sku_wise_price_fetch_dedicated_table() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    # LLM mistakenly plans a monthly trend — Python must override
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["avg_price"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "filters": {"party": "al shaheer"},
                },
                user_text=(
                    "can you show me a sku wise breakup of al shaheer "
                    "with average prices and the price fetch"
                ),
            )
            assert out["ok"] is True, out
            assert out.get("mode") == "price_fetch_table"
            md = out.get("answer_markdown") or ""
            assert "Price Fetch" in md
            assert "Avg Price (Incl GST/unit)" in md
            assert "Cost Factor" in md
            assert "Price Fetch / Maund" in md
            assert "Amount / kg" not in md
            # SKU names present
            assert "StandUpPouch" in md or "5 Ltr" in md
            # Both Al Shaheer branches' SKUs, not Alpha Cooking
            assert "Eva Cooking" not in md
            rows = out.get("rows") or []
            assert len(rows) >= 2
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_compare_two_parties_via_parties_filter() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            multi = resolve_party_filters(["al shaheer", "Metro Habib"])
            assert multi["ok"] is True
            assert multi.get("party_ilike") or multi.get("parties")

            out = execute_query_spec(
                {
                    "row_dimensions": ["party"],
                    "metrics": ["volume"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "filters": {
                        "parties": ["al shaheer", "Metro Habib"],
                    },
                },
                user_text="compare al shaheer sales with Metro Habib",
            )
            assert out["ok"] is True, out
            md = out.get("answer_markdown") or ""
            assert "SHAHEER" in md.upper()
            assert "Metro" in md or "metro" in md.lower()
            assert "Alpha" not in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_extracted_entities_party_ilike() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "row_dimensions": ["product"],
                    "metrics": ["price_fetch"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "extracted_entities": ["al shaheer"],
                },
                user_text="sku wise price fetch for al shaheer",
            )
            assert out["ok"] is True, out
            assert out.get("mode") == "price_fetch_table"
            filt = (out.get("query_spec") or {}).get("filters") or {}
            assert filt.get("party_ilike") or filt.get("party")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
