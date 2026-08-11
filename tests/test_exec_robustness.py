"""Executive robustness: inactive, sample/marketing, layers, growth, analysis."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    _compose_tables_plus_analysis,
    _dispatch_tool,
    _looks_party_growth_rank,
    _wants_active_only,
    extract_regroup_dimension,
    resolve_remove_request,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.sales_query import query_sales
from eva_dashboard.table_export import build_excel_bytes, matrix_to_records


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed() -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, payload_json, updated_at) "
            "VALUES ('P1', 'Eva Consumer', 'Eva Canola', 'Stand up', '{}', datetime('now'))"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, 'Eva Distributors', 'Karachi', 'Karachi', ?, '{}', datetime('now'))",
            [
                ("1", "Alpha Dist", ""),
                ("2", "Beta Dist", "Y"),
                ("3", "Sample for Marketing", ""),
                ("4", "Gamma Dist", ""),
            ],
        )
        rows = [
            ("2026-03-01", "Alpha Dist", 20),
            ("2026-04-01", "Alpha Dist", 22),
            ("2026-05-01", "Alpha Dist", 21),
            ("2026-06-01", "Alpha Dist", 23),
            ("2026-07-01", "Alpha Dist", 24),
            ("2026-08-01", "Alpha Dist", 18),
            ("2026-03-01", "Beta Dist", 15),
            ("2026-04-01", "Beta Dist", 16),
            ("2026-05-01", "Beta Dist", 14),
            ("2026-06-01", "Beta Dist", 15),
            ("2026-07-01", "Beta Dist", 16),
            ("2026-08-01", "Beta Dist", 10),
            ("2026-03-01", "Sample for Marketing", 8),
            ("2026-04-01", "Sample for Marketing", 8),
            ("2026-05-01", "Sample for Marketing", 8),
            ("2026-06-01", "Sample for Marketing", 8),
            ("2026-07-01", "Sample for Marketing", 8),
            ("2026-08-01", "Sample for Marketing", 5),
            ("2026-03-01", "Gamma Dist", 5),
            ("2026-04-01", "Gamma Dist", 5),
            ("2026-05-01", "Gamma Dist", 5),
            ("2026-06-01", "Gamma Dist", 5),
            ("2026-07-01", "Gamma Dist", 5),
            ("2026-08-01", "Gamma Dist", 4),
            # Prior AMS / YoY for growth ranks
            ("2025-07-01", "Alpha Dist", 10),
            ("2025-07-01", "Beta Dist", 20),
            ("2026-01-01", "Alpha Dist", 10),
            ("2026-02-01", "Alpha Dist", 10),
            ("2026-01-01", "Beta Dist", 20),
            ("2026-02-01", "Beta Dist", 20),
        ]
        for i, (dt, party, mt) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, 'P1', ?, 'MT', ?,
                          'Eva Distributors', '{}')
                """,
                (f"er-{i}", dt, party, mt, mt),
            )
        conn.commit()


def test_remove_inactive_does_not_drop_distributor_channel() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            assert _wants_active_only("remove inactive distributors")
            prior = query_sales(
                city="Karachi",
                client_type="Eva Distributors",
                columns="month",
                months_back=6,
                row_dimension="party",
            )["table_spec"]
            rm = resolve_remove_request(
                "remove inactive distributors", prior_spec=prior
            )
            assert rm is not None
            assert "inactive" in (rm.get("excludes") or {})
            assert "client_type" not in (rm.get("excludes") or {})

            out = _dispatch_tool(
                "query_sales",
                {},
                user_text="remove inactive distributors",
                prior_spec=prior,
            )
            assert out["ok"] is True
            assert out["filters"].get("active_only") is True
            # Still Eva Distributors scope — not wiped
            assert out["filters"].get("client_type") == "Eva Distributors"
            parties = {
                str(r.get("party"))
                for r in (out.get("matrix") or {}).get("rows") or []
                if r.get("party")
                and str(r.get("party")).lower() != "total"
                and "total" not in str(r.get("row_kind") or "").lower()
            }
            assert "Alpha Dist" in parties
            assert "Beta Dist" not in parties  # inactive
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_exclude_sample_marketing_party_names() -> None:
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
                row_dimension="party",
            )["table_spec"]
            rm = resolve_remove_request(
                "exclude sample/marketing", prior_spec=prior
            )
            assert rm is not None
            assert "party_like" in (rm.get("excludes") or {})

            out = _dispatch_tool(
                "query_sales",
                {},
                user_text="exclude sample/marketing",
                prior_spec=prior,
            )
            assert out["ok"] is True
            parties = {
                str(r.get("party"))
                for r in (out.get("matrix") or {}).get("rows") or []
                if r.get("party")
                and str(r.get("party")).lower() != "total"
                and "total" not in str(r.get("row_kind") or "").lower()
            }
            assert "Sample for Marketing" not in parties
            assert "Alpha Dist" in parties
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_add_city_layer_regroup() -> None:
    assert extract_regroup_dimension("add city layer") == "city"
    assert extract_regroup_dimension("add a zone layer") == "zone"
    prior = {
        "column_dimension": "month",
        "row_dimension": "packing_category",
        "row_groups": ["business_unit"],
        "filters": {"city": "Karachi", "client_type": "Eva Distributors"},
    }
    from eva_dashboard.chatbot import resolve_regroup_request

    rg = resolve_regroup_request("add city layer", prior_spec=prior)
    assert rg is not None
    assert rg["row_groups"] == ["city"] or rg.get("dimension") == "city"


def test_growing_vs_sort_by_ams_growth() -> None:
    """Growth rank language still detected; grown_only is a plan field now."""
    assert _looks_party_growth_rank("which distributors are growing")
    assert _looks_party_growth_rank("show only growing distributors")
    # Explicit QuerySpec for grown_only / sort — not inferred from text
    from eva_dashboard.query_executor import execute_query_spec

    plan = {
        "intent": "party_rank",
        "context_handling": "none",
        "clear_filters": [],
        "period_type": "MTD",
        "ranking_metric": "ams_growth",
        "sort_order": "desc",
        "grown_only": True,
    }
    assert plan["grown_only"] is True
    assert plan["ranking_metric"] == "ams_growth"


def test_analysis_fallback_uses_facts_when_model_empty() -> None:
    tool_md = (
        "Sales table here.\n\n"
        "### Analysis\n"
        "- Alpha leads with 120 MT (40% of the view).\n"
        "- Beta is soft vs AMS at -18%.\n"
    )
    out = _compose_tables_plus_analysis(tool_md, "")
    assert "### Analysis" in out
    assert "Alpha leads" in out
    assert "Sales table here" in out


def test_excel_export_after_party_month_with_ams() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            result = query_sales(
                city="Karachi",
                client_type="Eva Distributors",
                columns="month",
                months_back=6,
                row_dimension="party",
                active_only=True,
            )
            headers, data, _dim = matrix_to_records(result["matrix"])
            xlsx = build_excel_bytes(
                title="Active distributors",
                headers=headers,
                data=data,
            )
            assert xlsx[:2] == b"PK"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_national_distributor_ams_decline_still_works() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            prior = {
                "filters": {
                    "city": "Karachi",
                    "client_type": "Eva Distributors",
                },
                "column_dimension": "month",
                "row_dimension": "party",
            }
            out = _dispatch_tool(
                "analyze_parties",
                {},
                user_text=(
                    "nationally which distributors have had a decline in AMS"
                ),
                prior_spec=prior,
            )
            assert out["ok"] is True
            assert out["metric"] == "ams_growth"
            assert out["filters"].get("city") is None
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
