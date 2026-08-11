"""Last sold price metric + named-month year vs month-grid July."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.db import connect, init_db
from eva_dashboard.metrics_catalog import (
    apply_metric_synonyms_to_spec,
    load_metrics_catalog,
    resolve_metrics_from_text,
)
from eva_dashboard.query_executor import execute_query_spec
from eva_dashboard.sales_query import query_price_fetch_table


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed_prices() -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, payload_json, "
            "updated_at) VALUES "
            "('Eva Canola Oil (StandUpPouch)', 'Eva Consumer', 'Eva Canola', "
            "'Stand up', '{}', datetime('now'))"
        )
        conn.execute(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, "
            "payload_json, updated_at) VALUES "
            "('1', 'Imtiaz A', 'Imtiaz Store', 'Lahore', 'Lahore', '', '{}', "
            "datetime('now'))"
        )
        # Older then newer rate — last price must pick the later invoice
        for i, (dt, rate) in enumerate(
            [("2026-05-01", 500.0), ("2026-08-10", 555.0)]
        ):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mes_qty, mes_unit, mt_qty, rate,
                  incl_gst_fed_amount, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, 'Imtiaz A',
                  'Eva Canola Oil (StandUpPouch)', 10, 'Ltrs', 10, 'Ltrs',
                  0.01, ?, ?, 'Imtiaz Store', '{}')
                """,
                (f"lp-{i}", dt, rate, rate * 10),
            )
        conn.execute(
            "INSERT OR REPLACE INTO factor_costs "
            "(client_type, prod_id, product, unit, product_cost, packing_cost, "
            "total_factor_cost, updated_at) VALUES "
            "('Imtiaz Store', 1, 'Eva Canola Oil (StandUpPouch)', 'Ltrs', 80, 20, "
            "100, datetime('now'))"
        )
        # Month-grid context: Jul 2025 has volume, Jul 2026 empty, Aug 2026 has volume
        for i, (dt, party, mt) in enumerate(
            [
                ("2025-07-15", "Imtiaz A", 50),
                ("2026-06-15", "Imtiaz A", 20),
                ("2026-08-05", "Imtiaz A", 15),
            ]
        ):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type,
                  payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, 
                  'Eva Canola Oil (StandUpPouch)', ?, 'MT', ?, 100, ?,
                  'Imtiaz Store', '{}')
                """,
                (f"m-{i}", dt, party, mt, mt, mt * 100),
            )
        conn.commit()


def test_last_price_synonym_beats_avg() -> None:
    load_metrics_catalog.cache_clear()
    text = (
        "can you show me the last price sold to Imtiaz for all sku "
        "with the price date of sale and the price fetch"
    )
    mets = resolve_metrics_from_text(text)
    assert "last_price" in mets
    assert "avg_price" not in mets
    assert "price_fetch" in mets

    spec = apply_metric_synonyms_to_spec(
        {
            "metrics": ["avg_price", "price_fetch"],
            "row_dimensions": ["business_unit"],
            "filters": {"client_type": "Imtiaz Store"},
        },
        text,
    )
    assert "last_price" in (spec.get("metrics") or [])
    assert "avg_price" not in (spec.get("metrics") or [])
    assert "product" in (spec.get("row_dimensions") or [])


def test_last_price_table_uses_latest_invoice() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            import eva_dashboard.sales_query as sq

            sq._CLIENTS_CACHE = None
            _seed_prices()
            out = query_price_fetch_table(
                row_dimensions=["product"],
                period=None,
                date_from="2026-03-01",
                date_to="2026-08-11",
                client_type="Imtiaz Store",
                price_mode="last",
            )
            assert out.get("ok"), out.get("error")
            assert out.get("price_mode") == "last"
            rows = out.get("rows") or []
            assert rows
            assert float(rows[0].get("last_price") or 0) == 555.0
            assert str(rows[0].get("sale_date") or "").startswith("2026-08")
            md = out.get("answer_markdown") or ""
            assert "Last Price" in md
            assert "Sale Date" in md
            assert "Avg Price" not in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_execute_last_price_ask() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            import eva_dashboard.sales_query as sq
            from eva_dashboard.metrics_catalog import load_metrics_catalog

            load_metrics_catalog.cache_clear()
            sq._CLIENTS_CACHE = None
            _seed_prices()
            text = (
                "last price sold to Imtiaz for all sku with the price date "
                "of sale and the price fetch"
            )
            out = execute_query_spec(
                {
                    "operation": "pivot",
                    "row_dimensions": ["product"],
                    "metrics": ["avg_price", "price_fetch"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "filters": {"client_type": "Imtiaz Store"},
                },
                user_text=text,
            )
            assert out.get("ok"), out.get("error")
            md = out.get("answer_markdown") or ""
            assert "Last Price" in md or "last_price" in str(
                (out.get("query_spec") or {}).get("metrics")
            )
            assert "Avg Price (Incl GST/unit)" not in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_july_bare_name_follows_max_sales_year() -> None:
    """With max sales in Aug 2026, bare 'July' must be 2026-07 not 2025-07."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            import eva_dashboard.sales_query as sq
            from eva_dashboard.sales_query import resolve_period

            sq._CLIENTS_CACHE = None
            _seed_prices()
            info = resolve_period("July")
            assert info.get("ok") is not False
            assert str(info.get("date_from") or "").startswith("2026-07"), info
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
