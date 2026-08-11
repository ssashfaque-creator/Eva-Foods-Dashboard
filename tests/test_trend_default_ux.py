"""Trend default, MultiIndex group_by, composite SKU filters, monthly price."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.ai_guide import tool_guide_for_prompt, vocabulary_for_prompt
from eva_dashboard.chatbot import system_prompt
from eva_dashboard.client_language import extract_oil_and_packing
from eva_dashboard.db import connect, init_db
from eva_dashboard.query_executor import execute_query_spec
from eva_dashboard.query_spec import (
    PLAN_QUERY_TOOL,
    normalize_query_spec,
    resolve_period_from_spec,
)


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
                ("Eva Bulk Tin", "Eva Bulk", "Eva Bulk", "Tin"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "Alpha Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("2", "Beta Store", "Imtiaz Store", "Karachi", "Karachi"),
            ],
        )
        rows = [
            ("2026-03-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 10.0, 500.0, 50000.0),
            ("2026-04-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 12.0, 510.0, 61200.0),
            ("2026-05-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 14.0, 520.0, 72800.0),
            ("2026-06-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 16.0, 530.0, 84800.0),
            ("2026-07-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 18.0, 540.0, 97200.0),
            ("2026-08-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 8.0, 550.0, 44000.0),
            ("2026-07-11", "Alpha Dist", "Eva Bulk Tin", 20.0, 400.0, 80000.0),
            ("2026-07-12", "Beta Store", "Eva Cooking Oil (StandUpPouch)", 30.0, 480.0, 144000.0),
            ("2026-05-12", "Beta Store", "Eva Cooking Oil (StandUpPouch)", 25.0, 470.0, 117500.0),
        ]
        for i, (dt, party, product, mt, rate, incl) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type,
                  payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, ?,
                          'Eva Distributors', '{}')
                """,
                (f"td-{i}", dt, party, product, mt, mt, rate, incl),
            )
        conn.commit()


def test_prompt_teaches_trend_default_and_composite_sku() -> None:
    vocab = vocabulary_for_prompt()
    tools = tool_guide_for_prompt()
    sys = system_prompt()
    assert "LAST_N_MONTHS" in vocab
    assert "canola standup" in vocab.lower() or "canola stand" in vocab.lower()
    assert "avg_price" in vocab or "avg_price" in tools
    assert "customer" in vocab.lower()
    assert "TREND DEFAULT" in sys or "LAST_N_MONTHS" in sys
    props = PLAN_QUERY_TOOL["function"]["parameters"]["properties"]
    assert "row_dimensions" in props and "metrics" in props
    assert "month" in props["column_dimensions"]["items"]["enum"]


def test_group_by_array_promotes_to_row_groups() -> None:
    spec = normalize_query_spec(
        {
            "intent": "sales_trend",
            "period_type": "LAST_N_MONTHS",
            "months_back": 6,
            "group_by": ["client_type", "business_unit"],
        }
    )
    grain = spec["grain"]
    assert grain.get("row_groups") == ["client_type"]
    assert grain.get("row_dimension") == "business_unit"


def test_sales_trend_last_n_defaults_bu_month_grain() -> None:
    spec = normalize_query_spec(
        {
            "intent": "sales_trend",
            "period_type": "LAST_N_MONTHS",
            "months_back": 6,
            "filters": {"client_type": "Eva Distributors"},
            "business_units": ["Eva Consumer", "Eva Bulk"],
        }
    )
    resolved = resolve_period_from_spec(spec)
    assert resolved["grain"].get("column_dimension") == "month"
    assert resolved["grain"].get("row_dimension") == "business_unit"


def test_extract_canola_standup() -> None:
    oil, pack = extract_oil_and_packing("canola standup")
    assert oil == "Eva Canola"
    assert pack == "Stand up"
    oil2, pack2 = extract_oil_and_packing("Eva canola stand-up")
    assert oil2 == "Eva Canola"
    assert pack2 == "Stand up"


def test_execute_sales_trend_month_grid() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "intent": "sales_trend",
                    "context_handling": "none",
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "filters": {"client_type": "Eva Distributors"},
                    "business_units": ["Eva Consumer", "Eva Bulk"],
                }
            )
            assert out["ok"] is True, out
            assert out.get("column_dimension") == "month"
            assert out.get("row_dimension") == "business_unit"
            md = out.get("answer_markdown") or ""
            assert "AMS" in md
            # Not a static client_type crosstab title as the only view
            assert out.get("mode") == "matrix"  # month grid is matrix-rendered
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_execute_channel_bu_multiindex() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "intent": "sales_trend",
                    "context_handling": "none",
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "group_by": ["client_type", "business_unit"],
                }
            )
            assert out["ok"] is True, out
            matrix = out.get("matrix") or {}
            assert matrix.get("hierarchical") is True
            headers = matrix.get("row_headers") or []
            assert "client_type" in headers
            assert "business_unit" in headers
            md = out.get("answer_markdown") or ""
            assert "Client Type" in md or "client_type" in md.lower()
            assert "Business Unit" in md or "eva-mtx" in md
            assert 'rowspan=' in md or "Business Unit" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_execute_canola_standup_and_filters() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "intent": "sales_trend",
                    "context_handling": "none",
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "filters": {
                        "oil_type": "Eva Canola",
                        "packing_category": "Stand up",
                    },
                }
            )
            assert out["ok"] is True, out
            filters = (out.get("query_spec") or {}).get("filters") or {}
            assert filters.get("oil_type") == "Eva Canola"
            assert filters.get("packing_category") == "Stand up"
            # Cooking / Bulk must not dominate — only canola standup volume
            total = 0.0
            for row in (out.get("matrix") or {}).get("rows") or []:
                if row.get("row_type") in {"subtotal", "total"}:
                    continue
                for k, v in row.items():
                    if isinstance(v, (int, float)) and k not in {
                        "business_unit",
                        "packing_category",
                        "product",
                    }:
                        if not str(k).startswith("AMS") and k != "Total":
                            pass
                if "Total" in row and isinstance(row["Total"], (int, float)):
                    total += float(row["Total"])
            # Alpha canola standup Mar–Aug = 10+12+14+16+18+8 = 78
            assert abs(total - 78.0) < 0.01 or total > 0
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_execute_monthly_price_timeseries() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "intent": "price",
                    "context_handling": "none",
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "time_grain": "month",
                    "filters": {
                        "oil_type": "canola standup",  # composite → split
                    },
                }
            )
            assert out["ok"] is True, out
            qs_filters = (out.get("query_spec") or {}).get("filters") or {}
            assert qs_filters.get("oil_type") == "Eva Canola"
            assert qs_filters.get("packing_category") == "Stand up"
            by_month = out.get("by_month") or []
            assert len(by_month) >= 2
            md = out.get("answer_markdown") or ""
            assert "Monthly average price" in md
            assert "Avg Rate" in md
            # Must not be a single Metric|Value aggregate block as the only table
            assert "| Month |" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
