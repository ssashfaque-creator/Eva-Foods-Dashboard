"""v1.0 Semantic Planner — LLM plans; executor does not mutate."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.query_executor import execute_query_spec
from eva_dashboard.query_spec import (
    PLAN_QUERY_TOOL,
    normalize_query_spec,
    validate_query_spec,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.chatbot import system_prompt


def test_plan_query_schema_requires_period_type() -> None:
    params = PLAN_QUERY_TOOL["function"]["parameters"]
    assert "period_type" in params["required"]
    assert "intent" in params["required"]
    enums = params["properties"]["period_type"]["enum"]
    assert "MTD" in enums and "LAST_N_MONTHS" in enums


def test_prompt_is_semantic_planner() -> None:
    text = system_prompt()
    assert "Semantic Planner" in text or "plan_query" in text
    assert "BLINDLY" in text or "blindly" in text.lower()
    assert "period_type" in text
    assert "distributor-wise" in text.lower() or "DISTRIBUTOR" in text


def test_executor_does_not_invent_filters_from_user_text() -> None:
    """Blind execute: user_text must not inject city/client_type."""
    plan = {
        "intent": "sales_matrix",
        "period_type": "MTD",
        "context_handling": "none",
        "filters": {},
        "business_units": ["Eva Consumer"],
    }
    # Even with spoken Lahore / distributors in user_text, filters stay empty
    out = execute_query_spec(
        plan,
        user_text="Eva distributor sales in Lahore last 6 months",
    )
    # May fail on empty DB — but query_spec filters must stay empty
    qs = out.get("query_spec") or {}
    filters = qs.get("filters") or {}
    assert not filters.get("city")
    assert not filters.get("client_type")
    # And period must be MTD as planned — not rewritten to last 6 months
    assert (qs.get("period") or {}).get("phrase") == "this month"


def test_legacy_phrase_derives_period_type() -> None:
    spec = normalize_query_spec(
        {
            "intent": "sales_matrix",
            "period": {"phrase": "last 6 months"},
            "filters": {"city": "Lahore"},
        }
    )
    assert spec["period_type"] == "LAST_N_MONTHS"
    assert not validate_query_spec(spec)


def test_complete_eva_distributor_last_6_months() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")
        try:
            init_db()
            with connect() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO category "
                    "(product, category_1, category_2, packing_category, "
                    "payload_json, updated_at) VALUES (?, ?, ?, ?, '{}', datetime('now'))",
                    [
                        ("Eva Canola", "Eva Consumer", "Eva Canola", "Stand up"),
                        ("Eva Bulk Tin", "Eva Bulk", "Eva Bulk", "Tin"),
                    ],
                )
                conn.execute(
                    "INSERT OR REPLACE INTO clients "
                    "(client_id, client, type, city_filter, city, inactive, "
                    "payload_json, updated_at) VALUES "
                    "('1', 'Alpha', 'Eva Distributors', 'Lahore', 'Lahore', '', "
                    "'{}', datetime('now'))"
                )
                for i, (dt, mt) in enumerate(
                    [
                        ("2026-03-10", 10),
                        ("2026-04-10", 12),
                        ("2026-05-10", 14),
                        ("2026-06-10", 16),
                        ("2026-07-10", 18),
                        ("2026-08-05", 8),
                    ]
                ):
                    conn.execute(
                        """
                        INSERT INTO sales (
                          source_file_id, row_hash, imported_at, date, party,
                          product, qty, unit, mt_qty, client_type, payload_json
                        ) VALUES (NULL, ?, datetime('now'), ?, 'Alpha',
                          'Eva Canola', ?, 'MT', ?, 'Eva Distributors', '{}')
                        """,
                        (f"sp-{i}", dt, mt, mt),
                    )
                conn.commit()

            out = execute_query_spec(
                {
                    "intent": "sales_matrix",
                    "context_handling": "none",
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "filters": {
                        "city_filter": "Lahore",
                        "client_type": "Eva Distributors",
                    },
                    "business_units": ["Eva Consumer", "Eva Bulk"],
                }
            )
            assert out["ok"] is True, out
            md = out.get("answer_markdown") or ""
            assert "Aug 2026 MTD" not in md
            assert "Last 6 months" in md
            assert "Lahore" in md
            assert "Eva Distributors" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
