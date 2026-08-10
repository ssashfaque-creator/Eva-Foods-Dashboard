"""Regressions: remove BU/totals, AMS growth labels, same-date price dispersion."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.advanced_routing import infer_advanced_from_text
from eva_dashboard.chatbot import (
    _dispatch_tool,
    _extract_remove_phrase,
    _party_matrix_row_layout,
    _phrase_as_struct_dim,
    resolve_forced_tool,
    suggest_preferred_tool,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.party_analytics import analyze_parties
from eva_dashboard.sales_query import query_sales


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
                ("P1", "Eva Consumer", "Eva Canola", "Pet bottle",),
                ("P2", "Eva Consumer", "Eva Canola", "Pet Bottle",),  # casing variant
                ("P3", "Eva Bulk", "Eva VTF", "Tin (oil)",),
            ],
        )
        for cid, name, city in [
            ("1", "Alpha Dist", "Karachi"),
            ("2", "Beta Dist", "Karachi"),
            ("3", "Gamma Dist", "Lahore"),
        ]:
            conn.execute(
                "INSERT OR REPLACE INTO clients "
                "(client_id, client, type, city_filter, city, inactive, "
                "payload_json, updated_at) "
                "VALUES (?, ?, 'Eva Distributors', ?, ?, '', '{}', datetime('now'))",
                (cid, name, city, city),
            )
        rows = [
            # Continuous AMS history for Alpha
            ("2026-02-01", "Alpha Dist", "P1", 10, 100),
            ("2026-03-01", "Alpha Dist", "P1", 10, 100),
            ("2026-04-01", "Alpha Dist", "P1", 10, 100),
            ("2026-05-01", "Alpha Dist", "P1", 12, 100),
            ("2026-06-01", "Alpha Dist", "P1", 12, 110),
            ("2026-07-01", "Alpha Dist", "P1", 8, 110),
            ("2026-08-01", "Alpha Dist", "P1", 9, 110),
            ("2026-07-01", "Alpha Dist", "P2", 3, 110),  # same packing, different case
            ("2026-07-01", "Alpha Dist", "P3", 5, 90),
            # Beta: prior AMS only (gap May–Jul) — should not dominate AMS decline noise
            ("2026-02-01", "Beta Dist", "P1", 20, 100),
            ("2026-03-01", "Beta Dist", "P1", 20, 100),
            ("2026-04-01", "Beta Dist", "P1", 20, 100),
            ("2026-08-01", "Beta Dist", "P1", 15, 120),
            # YoY priors
            ("2025-03-01", "Alpha Dist", "P1", 8, 95),
            ("2025-07-01", "Alpha Dist", "P1", 8, 95),
            ("2025-03-01", "Beta Dist", "P1", 10, 95),
            ("2025-08-01", "Beta Dist", "P1", 10, 95),
            # Same-date different rates
            ("2026-07-15", "Alpha Dist", "P1", 4, 100),
            ("2026-07-15", "Beta Dist", "P1", 4, 130),
            ("2026-07-15", "Gamma Dist", "P1", 4, 100),
        ]
        for i, (dt, party, prod, mt, rate) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?,
                          'Eva Distributors', '{}')
                """,
                (f"rcf-{i}", dt, party, prod, mt, mt, rate),
            )
        conn.commit()


def test_remove_bu_parenthetical_parses() -> None:
    assert _extract_remove_phrase(
        "Show individual distributors total sale (remove BU)"
    ) == "BU"
    assert _phrase_as_struct_dim("BU") == "business_unit"
    assert _phrase_as_struct_dim("business unit layer") == "business_unit"


def test_individual_distributors_total_sale_remove_bu() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            prior = query_sales(
                city="Karachi",
                client_type="Eva Distributors",
                columns="month",
                months_back=6,
                row_dimension="business_unit",
            )["table_spec"]
            q = "Show individual distributors total sale (remove BU)"
            leaf, groups = _party_matrix_row_layout(q, prior)
            assert leaf == "party"
            assert groups is None

            out = _dispatch_tool(
                "query_sales",
                {},
                user_text=q,
                prior_spec=prior,
            )
            assert out["ok"] is True
            assert out.get("row_dimension") == "party"
            headers = (out.get("matrix") or {}).get("row_headers") or []
            assert "business_unit" not in headers
            assert "packing_category" not in headers
            md = out.get("answer_markdown") or ""
            assert "Party × Month" in md or "Distributor" in md
            assert "Business Unit" not in md
            assert "→ Packing" not in md
            assert "Distributor → Business Unit" not in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_remove_bu_only_drops_middle_layer() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            prior = query_sales(
                city="Karachi",
                client_type="Eva Distributors",
                columns="month",
                months_back=6,
                row_dimension="business_unit",
            )["table_spec"]
            # First build Dist→BU→Packing
            stacked = _dispatch_tool(
                "query_sales",
                {},
                user_text="show individual distributor breakdown",
                prior_spec=prior,
            )
            assert stacked["ok"] is True
            assert (stacked.get("matrix") or {}).get("row_headers")[:3] == [
                "party",
                "business_unit",
                "packing_category",
            ]
            prior2 = stacked["table_spec"]
            out = _dispatch_tool(
                "query_sales",
                {},
                user_text="remove BU",
                prior_spec=prior2,
            )
            assert out["ok"] is True
            headers = (out.get("matrix") or {}).get("row_headers") or []
            assert "business_unit" not in headers
            assert headers[0] == "party"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_packing_case_collapses() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = query_sales(
                city="Karachi",
                client_type="Eva Distributors",
                business_unit="Eva Consumer",
                columns="month",
                months_back=6,
                row_dimension="packing_category",
            )
            labels = {
                str(r.get("packing_category"))
                for r in (out.get("matrix") or {}).get("rows") or []
                if r.get("packing_category")
                and "Total" not in str(r.get("packing_category"))
            }
            # Canonical form only — not both Pet bottle and Pet Bottle
            pet = [x for x in labels if "pet" in x.lower()]
            assert len(pet) <= 1, labels
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_ams_growth_report_columns_and_title() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = analyze_parties(
                metric="ams_growth",
                period="last 6 months",
                client_type="Eva Distributors",
                sort="asc",
                declined_only=True,
                limit=10,
            )
            assert out["ok"] is True
            md = out.get("answer_markdown") or ""
            assert "Biggest AMS declines" in md
            assert "AMS current (" in md
            assert "AMS prior (" in md
            assert "Volume in period (MT)" in md
            # Must not look like "last year AMS"
            assert "| Prior (MT) |" not in md
            assert "YoY %" not in md or "Volume YoY" in md
            # Gap-month -100% party with prior AMS still allowed, but no YoY confusion
            assert "Top parties by AMS growth %" not in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_price_dispersion_routing_and_dispatch() -> None:
    q = (
        "are there any distributors that have purchased at different prices "
        "than others on the same date"
    )
    assert infer_advanced_from_text(q).get("mode") == "price_dispersion"
    assert resolve_forced_tool(q) == "required"
    assert suggest_preferred_tool(q) == "advanced_query"

    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            # AI-first: honor the model's tool; advanced_query is the taught path
            out = _dispatch_tool("advanced_query", {}, user_text=q)
            assert out["ok"] is True, out
            assert out.get("mode") == "price_dispersion"
            md = out.get("answer_markdown") or ""
            assert "price differences" in md.lower() or "Min rate" in md
            assert "Top parties by Invoices" not in md
            assert out.get("case_count", 0) >= 1
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
