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
            ("2026-05-12", "Beta Dist", "Maan Banaspati 16 Kgs Tin", 20.0, "Maan Distributors"),
        ]
        # June
        rows += [
            ("2026-06-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30.0, "Eva Distributors"),
            ("2026-06-12", "Beta Dist", "Maan Banaspati 16 Kgs Tin", 20.0, "Maan Distributors"),
        ]
        # July (full)
        rows += [
            ("2026-07-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 40.0, "Eva Distributors"),
            ("2026-07-06", "Alpha Dist", "Eva Cooking Oil (StandUpPouch)", 10.0, "Eva Distributors"),
            ("2026-07-07", "Beta Dist", "Eva VTF Banaspati 1x5 Pouch", 5.0, "Maan Distributors"),
            ("2026-07-08", "Beta Dist", "Maan Banaspati 16 Kgs Tin", 25.0, "Maan Distributors"),
            ("2026-07-09", "Gamma Dist", "Eva Canola Oil (StandUpPouch)", 15.0, "Eva Distributors"),
        ]
        # August partial (through day 6)
        rows += [
            ("2026-08-01", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 8.0, "Eva Distributors"),
            ("2026-08-02", "Alpha Dist", "Eva Cooking Oil (StandUpPouch)", 2.0, "Eva Distributors"),
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


def test_eva_consumer_rows_are_oil_type() -> None:
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
            )
            assert out["row_dimension"] == "oil_type"
            oils = {r["oil_type"] for r in out["matrix"]["rows"]}
            assert "Eva Canola" in oils
            assert "Eva Cooking" in oils
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
