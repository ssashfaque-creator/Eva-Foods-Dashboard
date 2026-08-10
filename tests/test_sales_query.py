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
            assert out["matrix"].get("hierarchical") is True
            packs = {
                r["packing_category"]
                for r in out["matrix"]["rows"]
                if r.get("row_kind") == "leaf"
            }
            assert "Stand up" in packs or "Pouch" in packs
            # Column totals footer present (BU hierarchy: Total on first header col)
            assert any(
                r.get("row_kind") == "total"
                or r.get("business_unit") == "Total"
                or r.get("packing_category") == "Total"
                for r in out["matrix"]["rows"]
            )
            assert any(
                r.get("row_kind") == "subtotal_business_unit" for r in out["matrix"]["rows"]
            )
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
            cols = first["matrix"]["columns"]
            assert "AMS (3 months)" in cols
            assert "AMS (6 months)" in cols
            assert "Average" not in cols
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
            # Keep packing grain when adding a BU (do not jump to SKU / collapse)
            assert follow["row_dimension"] == "packing_category"
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
            # Named month / last month → lean Volume + AMS + % (not analytical 3-pack)
            assert what["mode"] == "trend"
            assert what["required_table_count"] == 1
            assert "### 3. Trend vs AMS" not in (what.get("answer_markdown") or "")
            assert "Volume vs AMS" in (what.get("answer_markdown") or "")

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


def test_matrix_includes_analysis_bullets() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = query_sales(
                period="July",
                city="Lahore",
                business_unit="Eva Consumer",
                mode="matrix",
            )
            assert out["ok"] is True
            md = out["answer_markdown"]
            assert "### Analysis" in md
            assert "- " in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_routing_packing_breakdown_and_silent_and_drill() -> None:
    from eva_dashboard.chatbot import (
        _looks_party_analytics,
        _looks_sales_matrix,
        _looks_row_drilldown,
    )
    from eva_dashboard.party_analytics import infer_party_analytics_from_text

    assert not _looks_party_analytics(
        "Eva Consumer packing breakdown for Karachi in June"
    )
    assert _looks_sales_matrix("Eva Consumer packing breakdown for Karachi in June")
    assert _looks_row_drilldown("Show by product")
    assert not _looks_party_analytics("Show by product")
    silent = infer_party_analytics_from_text("Silent distributors in Faisalabad")
    assert silent["metric"] == "lost_parties"
    assert silent["client_type"] == "Eva Distributors"
    inv = infer_party_analytics_from_text("Most invoices for distributors")
    assert inv["metric"] == "invoices"
    share = infer_party_analytics_from_text("Which Imtiaz has highest VTF share")
    assert share["metric"] == "share_of_segment"
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            init_db()
            from eva_dashboard.chatbot import system_prompt, TOOLS

            text = system_prompt()
            assert "plan_query" in text or "query_sales" in text
            assert "analytical" in text.lower() or "party_rank" in text
            names = [t["function"]["name"] for t in TOOLS]
            assert names[0] == "plan_query"
            assert "query_sales" in names
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_sales_yoy_compare_same_period_last_year() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            # Prior-year same days for August so far
            with connect() as conn:
                conn.execute(
                    """
                    INSERT INTO sales (
                      source_file_id, row_hash, imported_at, date, party, product,
                      qty, unit, mt_qty, client_type, payload_json
                    ) VALUES (NULL, 'yoy-1', datetime('now'), '2025-08-02',
                      'Alpha Dist', 'Eva Canola Oil (StandUpPouch)',
                      20.0, 'MT', 20.0, 'Eva Distributors', '{}')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO sales (
                      source_file_id, row_hash, imported_at, date, party, product,
                      qty, unit, mt_qty, client_type, payload_json
                    ) VALUES (NULL, 'yoy-2', datetime('now'), '2025-08-03',
                      'Gamma Dist', 'Eva Canola Oil (StandUpPouch)',
                      5.0, 'MT', 5.0, 'Eva Distributors', '{}')
                    """
                )
                conn.commit()

            from eva_dashboard.chatbot import (
                _dispatch_tool,
                _looks_party_analytics,
                _looks_sales_matrix,
                _looks_sales_yoy_compare,
            )

            q = "analyze these sales and compare with the same period last year"
            assert _looks_sales_yoy_compare(q)
            assert not _looks_party_analytics(q)
            assert _looks_sales_matrix(q)

            prior = {
                "period_phrase": "August so far",
                "period": {
                    "date_from": "2026-08-01",
                    "date_to": "2026-08-04",
                    "label": "Aug 2026 (through 2026-08-04)",
                },
                "filters": {
                    "city": "Lahore",
                    "business_unit": "Eva Consumer",
                    "oil_type": None,
                    "packing_category": None,
                    "client_type": None,
                },
                "business_units": ["Eva Consumer"],
                "column_dimension": "client_type",
                "row_dimension": "packing_category",
            }
            out = _dispatch_tool(
                "query_sales",
                {},
                user_text=q,
                prior_spec=prior,
            )
            assert out["ok"] is True
            assert out["mode"] == "yoy"
            assert out["filters"]["city"] == "Lahore"
            assert out["filters"]["business_unit"] == "Eva Consumer"
            assert out["filters"]["client_type"] is None  # do not invent distributors
            assert out["compare_period"]["date_from"] == "2025-08-01"
            assert out["compare_period"]["date_to"] == "2025-08-04"
            assert out["prior_total_mt"] > 0
            assert "YoY" in out["answer_markdown"]
            assert "Eva Distributors" in out["answer_markdown"] or out["yoy_by_col"]
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_include_check_bulk_excluded_then_combine() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            from eva_dashboard.chatbot import _dispatch_tool

            first = _dispatch_tool(
                "query_sales",
                {
                    "period": "July",
                    "city": "Karachi",
                    "business_unit": "Eva Consumer",
                    "client_type": "Eva Distributors",
                },
                user_text="What were Eva Consumer sales for distributors in Karachi in July?",
            )
            assert first["ok"] is True
            prior = first["table_spec"]
            assert prior["filters"]["business_unit"] == "Eva Consumer"

            check = _dispatch_tool(
                "query_sales",
                {},
                user_text="Does this include bulk?",
                prior_spec=prior,
            )
            assert check["ok"] is True
            assert check["mode"] == "include_check"
            assert check["included"] is False
            assert check["checked_segment"] == "Eva Bulk"
            assert check["filters"]["city"] == "Karachi"
            assert check["filters"]["client_type"] == "Eva Distributors"
            assert check["filters"]["business_unit"] == "Eva Bulk"
            assert "not included" in check["answer_markdown"].lower()
            assert "Eva Bulk" in check["answer_markdown"]

            combine = _dispatch_tool(
                "query_sales",
                {},
                user_text="Combine the tables",
                prior_spec=check["table_spec"],
            )
            assert combine["ok"] is True
            units = set(combine.get("business_units") or [])
            assert "Eva Consumer" in units and "Eva Bulk" in units
            assert combine["filters"]["city"] == "Karachi"
            assert combine["filters"]["client_type"] == "Eva Distributors"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_include_check_bulk_already_included() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            from eva_dashboard.chatbot import _dispatch_tool

            first = _dispatch_tool(
                "query_sales",
                {
                    "period": "July",
                    "business_units": ["Eva Consumer", "Eva Bulk"],
                },
                user_text="Show Eva Consumer and Eva Bulk sales in July",
            )
            check = _dispatch_tool(
                "query_sales",
                {},
                user_text="Does this include Eva Bulk?",
                prior_spec=first["table_spec"],
            )
            assert check["included"] is True
            assert "is included" in check["answer_markdown"].lower()
            assert check["filters"]["business_unit"] == "Eva Bulk"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_include_bulk_short_phrase_combine() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            from eva_dashboard.chatbot import _dispatch_tool

            first = _dispatch_tool(
                "query_sales",
                {"period": "July", "business_unit": "Eva Consumer", "city": "Lahore"},
                user_text="Eva Consumer sales in Lahore in July",
            )
            follow = _dispatch_tool(
                "query_sales",
                {},
                user_text="include bulk",
                prior_spec=first["table_spec"],
            )
            units = set(follow.get("business_units") or [])
            assert "Eva Consumer" in units and "Eva Bulk" in units
            assert follow["filters"]["city"] == "Lahore"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_sku_hierarchy_rowspan_survives_packing_subtotals() -> None:
    """BU rowspan must include packing subtotals or later packing groups shift left."""
    from eva_dashboard.sales_query import _matrix_to_markdown, _rowspan_map
    import re

    headers = ["business_unit", "packing_category", "product"]
    rows = [
        {
            "business_unit": "Eva Consumer",
            "packing_category": "Stand up",
            "product": "SKU A",
            "row_kind": "leaf",
            "Total": 10,
        },
        {
            "business_unit": "",
            "packing_category": "",
            "product": "SKU B",
            "row_kind": "leaf",
            "Total": 5,
        },
        {
            "business_unit": "",
            "packing_category": "Stand up Total",
            "product": "",
            "row_kind": "subtotal_packing_category",
            "Total": 15,
        },
        {
            "business_unit": "",
            "packing_category": "Pet bottle",
            "product": "SKU C",
            "row_kind": "leaf",
            "Total": 3,
        },
        {
            "business_unit": "",
            "packing_category": "Pet bottle Total",
            "product": "",
            "row_kind": "subtotal_packing_category",
            "Total": 3,
        },
        {
            "business_unit": "Eva Consumer Total",
            "packing_category": "",
            "product": "",
            "row_kind": "subtotal_business_unit",
            "Total": 18,
        },
        {
            "business_unit": "Total",
            "packing_category": "",
            "product": "",
            "row_kind": "total",
            "Total": 18,
        },
    ]
    spans = _rowspan_map(rows, headers)
    assert [s["business_unit"] for s in spans] == [5, 0, 0, 0, 0, 1, 1]
    md = _matrix_to_markdown(
        {
            "hierarchical": True,
            "row_headers": headers,
            "columns": ["Total"],
            "rows": rows,
        },
        "product",
    )
    body_rows = re.findall(r"<tbody>.*?</tbody>", md, flags=re.S)[0]
    trs = re.findall(r"<tr[^>]*>(.*?)</tr>", body_rows, flags=re.S)
    td_counts = [len(re.findall(r"<td", tr)) for tr in trs]
    # Every body row must contribute the same logical width (4 cols):
    # omitted cells are covered by rowspan from earlier rows.
    assert td_counts[0] == 4  # BU+Pack+SKU+Total (BU/Pack start rowspan)
    assert td_counts[3] == 3  # Pack+SKU+Total (BU covered by rowspan)
    assert "Pet bottle" in trs[3]
    assert "SKU C" in trs[3]


def test_hierarchical_packing_and_sku_tables() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            # By packing (product category): BU | Packing + BU totals
            pack = query_sales(
                period="July",
                city="Lahore",
                row_dimension="packing_category",
                columns="client_type",
            )
            assert pack["ok"] is True
            m = pack["matrix"]
            assert m.get("hierarchical") is True
            assert m["row_headers"] == ["business_unit", "packing_category"]
            kinds = [r.get("row_kind") for r in m["rows"]]
            assert "leaf" in kinds
            assert "subtotal_business_unit" in kinds
            assert "total" in kinds
            md = pack["answer_markdown"]
            assert "Business Unit" in md and "Packing" in md
            assert "eva-mtx" in md
            assert "eva-subtotal" in md or "Total" in md
            assert "rowspan=" in md
            zero_leaves = [
                r
                for r in m["rows"]
                if r.get("row_kind") == "leaf" and float(r.get("Total") or 0) == 0
            ]
            assert not zero_leaves

            # By SKU: BU | Packing | SKU + packing + BU totals
            sku = query_sales(
                period="July",
                city="Lahore",
                row_dimension="product",
                columns="client_type",
            )
            assert sku["ok"] is True
            sm = sku["matrix"]
            assert sm["row_headers"] == [
                "business_unit",
                "packing_category",
                "product",
            ]
            assert any(r.get("row_kind") == "subtotal_packing_category" for r in sm["rows"])
            assert any(
                r.get("row_kind") == "subtotal_business_unit" for r in sm["rows"]
            )
            # Parent "merge": after first leaf in a BU, business_unit cell is blank
            leaves = [r for r in sm["rows"] if r.get("row_kind") == "leaf"]
            assert leaves
            # HTML shows SKU header and a packing total label
            smd = sku["answer_markdown"]
            assert "<th>SKU</th>" in smd or ">SKU<" in smd
            assert "eva-total" in smd
            assert "Total" in smd
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_regroup_city_wise_and_month_group_by_city() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            from eva_dashboard.chatbot import _dispatch_tool, resolve_regroup_request

            first = _dispatch_tool(
                "query_sales",
                {
                    "period": "July",
                    "city": "Lahore",
                    "business_unit": "Eva Consumer",
                },
                user_text="What were Eva Consumer sales in Lahore in July?",
            )
            assert first["ok"] is True
            assert first["filters"]["city"] == "Lahore"
            prior = first["table_spec"]

            plan = resolve_regroup_request(
                "can you show city wise?", prior_spec=prior
            )
            assert plan is not None
            assert plan["dimension"] == "city"
            assert plan["axis"] == "row"
            assert "city" in plan["clear_filters"]

            cityish = _dispatch_tool(
                "query_sales",
                {},
                user_text="can you show city wise?",
                prior_spec=prior,
            )
            assert cityish["ok"] is True
            assert cityish["filters"]["city"] is None  # filter lifted
            assert cityish["filters"]["business_unit"] == "Eva Consumer"
            headers = cityish["matrix"].get("row_headers") or [
                cityish["row_dimension"]
            ]
            assert headers[0] == "city"

            # Distributor month-wise then group by city (months stay on X)
            months = _dispatch_tool(
                "query_sales",
                {
                    "client_type": "Eva Distributors",
                    "columns": "month",
                    "months_back": 6,
                },
                user_text="Show me distributor sales for the last 6 months",
            )
            assert months["column_dimension"] == "month"
            by_city = _dispatch_tool(
                "query_sales",
                {},
                user_text="group by city",
                prior_spec=months["table_spec"],
            )
            assert by_city["column_dimension"] == "month"
            assert by_city["filters"]["client_type"] == "Eva Distributors"
            hdr = by_city["matrix"].get("row_headers") or [by_city["row_dimension"]]
            assert hdr[0] == "city"

            # Explicit columns axis
            as_cols = resolve_regroup_request(
                "as columns by client type",
                prior_spec=months["table_spec"],
            )
            assert as_cols["axis"] == "column"
            assert as_cols["columns"] == "client_type"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_remove_layer_and_exclude_value_followups() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            from eva_dashboard.chatbot import _dispatch_tool, resolve_remove_request

            # Eva sales (all client types as columns)
            first = _dispatch_tool(
                "query_sales",
                {"period": "July", "business_unit": "Eva Consumer"},
                user_text="Show me Eva Consumer sales in July",
            )
            assert first["ok"] is True
            prior = first["table_spec"]

            plan = resolve_remove_request(
                "remove distributors from the table", prior_spec=prior
            )
            assert plan is not None
            assert plan["mode"] == "exclude_value"
            assert plan["excludes"]["client_type"] == ["Eva Distributors"]

            filtered = _dispatch_tool(
                "query_sales",
                {},
                user_text="remove distributors from the table",
                prior_spec=prior,
            )
            assert filtered["ok"] is True
            assert "Eva Distributors" not in (filtered["matrix"].get("columns") or [])
            assert (filtered.get("excludes") or {}).get("client_type") == [
                "Eva Distributors"
            ]

            # City-wise then remove Lahore
            cityish = _dispatch_tool(
                "query_sales",
                {},
                user_text="group by city",
                prior_spec=filtered["table_spec"],
            )
            assert cityish["ok"] is True
            drop_lhr = _dispatch_tool(
                "query_sales",
                {},
                user_text="remove Lahore",
                prior_spec=cityish["table_spec"],
            )
            assert drop_lhr["ok"] is True
            assert (drop_lhr.get("excludes") or {}).get("city") == ["Lahore"]
            # Lahore should not appear as a leaf city row
            cities = {
                r.get("city")
                for r in drop_lhr["matrix"]["rows"]
                if r.get("row_kind", "leaf") == "leaf" and r.get("city")
            }
            assert "Lahore" not in cities

            # Remove city layer (structural)
            layer = resolve_remove_request(
                "remove the city",
                prior_spec=cityish["table_spec"],
            )
            assert layer["mode"] == "remove_layer"
            assert layer["dimension"] == "city"
            assert "city" not in (layer.get("row_groups") or [])
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
