"""Tests for structured sales query / pivot engine."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.db import connect, init_db
from eva_dashboard.sales_query import query_sales, resolve_period


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
                ("Eva VTF Banaspati 1x5 Pouch", "Eva Consumer", "Eva VTF", "Pouch"),
                ("Eva Cooking Oil (16 Ltr Tin)", "Eva Bulk", "Eva Bulk", "Tin"),
                ("Maan Banaspati 16 Kgs Tin", "Maan Bulk", "Maan Bulk", "Tin"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, '', '', '{}', datetime('now'))",
            [
                ("1", "Alpha Dist", "Eva Distributors", "Lahore"),
                ("2", "Beta Dist", "Maan Distributors", "Lahore"),
                ("3", "Gamma Dist", "Eva Distributors", "Karachi"),
            ],
        )
        # Prior months for AMS: May, June, July volumes; August partial
        rows = []
        # May
        rows += [
            ("2026-05-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30.0, "Eva Distributors"),
            ("2026-05-11", "Alpha Dist", "Eva Cooking Oil (16 Ltr Tin)", 12.0, "Eva Distributors"),
            ("2026-05-12", "Beta Dist", "Maan Banaspati 16 Kgs Tin", 20.0, "Maan Distributors"),
        ]
        # June
        rows += [
            ("2026-06-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30.0, "Eva Distributors"),
            ("2026-06-11", "Alpha Dist", "Eva Cooking Oil (16 Ltr Tin)", 12.0, "Eva Distributors"),
            ("2026-06-12", "Beta Dist", "Maan Banaspati 16 Kgs Tin", 20.0, "Maan Distributors"),
        ]
        # July (full)
        rows += [
            ("2026-07-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 40.0, "Eva Distributors"),
            ("2026-07-06", "Alpha Dist", "Eva Cooking Oil (StandUpPouch)", 10.0, "Eva Distributors"),
            ("2026-07-06", "Alpha Dist", "Eva Cooking Oil (16 Ltr Tin)", 18.0, "Eva Distributors"),
            ("2026-07-07", "Beta Dist", "Eva VTF Banaspati 1x5 Pouch", 5.0, "Maan Distributors"),
            ("2026-07-08", "Beta Dist", "Maan Banaspati 16 Kgs Tin", 25.0, "Maan Distributors"),
            ("2026-07-09", "Gamma Dist", "Eva Canola Oil (StandUpPouch)", 15.0, "Eva Distributors"),
        ]
        # August partial (through day 6)
        rows += [
            ("2026-08-01", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 8.0, "Eva Distributors"),
            ("2026-08-02", "Alpha Dist", "Eva Cooking Oil (StandUpPouch)", 2.0, "Eva Distributors"),
            ("2026-08-02", "Alpha Dist", "Eva Cooking Oil (16 Ltr Tin)", 4.0, "Eva Distributors"),
            ("2026-08-03", "Beta Dist", "Eva VTF Banaspati 1x5 Pouch", 1.0, "Maan Distributors"),
            ("2026-08-04", "Gamma Dist", "Eva Canola Oil (StandUpPouch)", 3.0, "Eva Distributors"),
        ]
        for i, (dt, party, product, mt, ctype) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, '{}')
                """,
                (f"h{i}-{dt}-{product}", dt, party, product, mt, mt, ctype),
            )
        conn.commit()


def test_resolve_last_month_and_partial_august() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            last = resolve_period("last month")
            assert last["date_from"] == "2026-07-01"
            assert last["date_to"] == "2026-07-31"
            assert last["partial_month"] is False

            aug = resolve_period("August so far")
            assert aug["date_from"] == "2026-08-01"
            assert aug["date_to"] == "2026-08-04"
            assert aug["partial_month"] is True
            assert aug["days_elapsed"] == 4
            assert aug["days_in_month"] == 31
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_matrix_defaults_business_unit_and_client_type() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = query_sales(period="last month", city="Lahore", mode="matrix")
            assert out["ok"] is True
            assert out["row_dimension"] == "business_unit"
            assert out["column_dimension"] == "client_type"
            matrix = out["matrix"]
            assert "Eva Distributors" in matrix["columns"]
            # Columns sorted highest first (before Total)
            data_cols = [c for c in matrix["columns"] if c != "Total"]
            totals = [matrix["column_totals"][c] for c in data_cols]
            assert totals == sorted(totals, reverse=True)
            row_names = [r["business_unit"] for r in matrix["rows"]]
            assert "Eva Consumer" in row_names
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_eva_consumer_rows_are_packing() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = query_sales(
                period="July 2026",
                city="Lahore",
                business_unit="Eva Consumer",
                columns="client_type",
                mode="matrix",
            )
            assert out["mode"] == "matrix"
            assert out["row_dimension"] == "packing_category"
            assert out["required_table_count"] == 1
            packs = {r["packing_category"] for r in out["matrix"]["rows"]}
            assert "Stand up" in packs or "Pouch" in packs
            # Column totals footer present
            assert any(r.get("packing_category") == "Total" for r in out["matrix"]["rows"])
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_month_wise_and_add_followup() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            from eva_dashboard.chatbot import _dispatch_tool

            first = _dispatch_tool(
                "query_sales",
                {"business_unit": "Eva Consumer", "columns": "month", "months_back": 6},
                user_text="Give me a month wise breakdown of Eva Consumer sales",
            )
            assert first["ok"] is True
            assert first["column_dimension"] == "month"
            assert first["row_dimension"] == "packing_category"
            assert "Average" in first["matrix"]["columns"]
            assert first.get("table_spec")

            follow = _dispatch_tool(
                "query_sales",
                {"business_unit": "Eva Bulk"},
                user_text="Add Eva Bulk to this table",
                prior_spec=first["table_spec"],
            )
            assert follow["column_dimension"] == "month"
            assert set(follow.get("business_units") or []) >= {
                "Eva Consumer",
                "Eva Bulk",
            }
            assert follow["row_dimension"] == "business_unit"
            names = {r["business_unit"] for r in follow["matrix"]["rows"]}
            assert "Eva Consumer" in names and "Eva Bulk" in names
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_analytical_markdown_includes_all_three_tables() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            from eva_dashboard.chatbot import _dispatch_tool

            out = _dispatch_tool(
                "query_sales",
                {
                    "period": "August so far",
                    "city": "Karachi",
                    "business_unit": "Eva Consumer",
                },
                user_text="How are Eva Consumer sales in Karachi so far in August?",
            )
            assert out["mode"] == "analytical"
            md = out["answer_markdown"]
            assert "### 1. City-wise breakdown" in md
            assert "### 2. Client-type breakdown" in md
            assert "### 3. Trend vs AMS" in md
            assert "AMS" in md
            assert "Expected" in md  # partial August
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_language_controls_matrix_vs_analytical() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            from eva_dashboard.chatbot import _dispatch_tool, _looks_analytical

            assert _looks_analytical("How were Eva Consumer sales in July?")
            assert _looks_analytical("Evaluate pet bottle performance last month")
            assert _looks_analytical("How are Stand up pouch sales doing so far?")
            assert not _looks_analytical("What were Eva Consumer sales in Lahore last month?")
            assert not _looks_analytical("What were Stand up pouch sales in July?")
            assert not _looks_analytical("How much Eva Consumer sold in July?")

            what = _dispatch_tool(
                "query_sales",
                {
                    "period": "July 2026",
                    "city": "Lahore",
                    "business_unit": "Eva Consumer",
                    "mode": "analytical",  # model wrong — language wins
                },
                user_text="What were Eva Consumer sales in Lahore last month?",
            )
            assert what["mode"] == "matrix"
            assert what["required_table_count"] == 1
            assert "### 3. Trend vs AMS" not in (what.get("answer_markdown") or "")

            how = _dispatch_tool(
                "query_sales",
                {
                    "period": "July 2026",
                    "business_unit": "Eva Consumer",
                    "mode": "matrix",  # model wrong — language wins
                },
                user_text="How were Eva Consumer sales in July?",
            )
            assert how["mode"] == "analytical"
            assert how["required_table_count"] == 3
            assert "trend" in how

            pack = _dispatch_tool(
                "query_sales",
                {
                    "period": "July 2026",
                    "packing_category": "Stand up",
                    "mode": "matrix",
                },
                user_text="Evaluate Stand up pouch sales in July",
            )
            assert pack["mode"] == "analytical"
            assert pack["row_dimension"] == "product"
            assert pack["required_table_count"] == 3
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_city_wise_columns() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = query_sales(
                period="July",
                business_unit="Eva Consumer",
                columns="city",
            )
            assert out["column_dimension"] == "city"
            assert "Lahore" in out["matrix"]["columns"] or "Karachi" in out["matrix"]["columns"]
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_analytical_partial_has_expected_full_does_not() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            partial = query_sales(
                period="August so far",
                business_unit="Eva Consumer",
                mode="analytical",
            )
            assert partial["mode"] == "analytical"
            assert "city_matrix" in partial and "client_matrix" in partial
            trend = partial["trend"]
            assert trend["partial_month"] is True
            assert "expected_mt" in trend["columns"]
            assert "pct_vs_expected" in trend["columns"]
            assert "pct_vs_ams" not in trend["columns"]

            full = query_sales(
                period="July",
                business_unit="Eva Consumer",
                mode="analytical",
            )
            ftrend = full["trend"]
            assert ftrend["partial_month"] is False
            assert "expected_mt" not in ftrend["columns"]
            assert "pct_vs_ams" in ftrend["columns"]
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_system_prompt_mentions_query_sales() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            init_db()
            from eva_dashboard.chatbot import system_prompt, TOOLS

            text = system_prompt()
            assert "query_sales" in text
            assert "analytical" in text.lower()
            names = [t["function"]["name"] for t in TOOLS]
            assert names[0] == "query_sales"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
