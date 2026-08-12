"""Regression: do not drop spoken city via clear_filters (self-correct)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.db import connect, init_db
from eva_dashboard.query_executor import execute_query_spec
from eva_dashboard.query_spec import validate_query_spec, normalize_query_spec


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
        conn.execute(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, payload_json, updated_at) "
            "VALUES ('P2', 'Eva Bulk', 'Eva Bulk', 'Bulk', '{}', datetime('now'))"
        )
        for cid, name, city in (
            ("1", "Alpha Dist", "Lahore"),
            ("2", "Beta Foods", "Karachi"),
        ):
            conn.execute(
                "INSERT OR REPLACE INTO clients "
                "(client_id, client, type, city_filter, city, inactive, "
                "payload_json, updated_at) "
                "VALUES (?, ?, 'Eva Distributors', ?, ?, '', '{}', datetime('now'))",
                (cid, name, city, city),
            )
        # Lahore Eva Consumer
        conn.execute(
            """
            INSERT INTO sales (
              source_file_id, row_hash, imported_at, date, party, product,
              qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type, payload_json
            ) VALUES (NULL, 'lh-1', datetime('now'), '2026-03-05', 'Alpha Dist', 'P1',
                      100, 'MT', 100, 100, 10000, 'Eva Distributors', '{}')
            """
        )
        # Karachi Eva Consumer (must NOT appear when city=Lahore)
        conn.execute(
            """
            INSERT INTO sales (
              source_file_id, row_hash, imported_at, date, party, product,
              qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type, payload_json
            ) VALUES (NULL, 'kh-1', datetime('now'), '2026-03-05', 'Beta Foods', 'P1',
                      900, 'MT', 900, 100, 90000, 'Eva Distributors', '{}')
            """
        )
        conn.commit()


def test_validate_rejects_clear_city_when_user_says_lahore():
    spec = normalize_query_spec(
        {
            "state_action": "modify",
            "clear_filters": ["city"],
            "row_dimensions": ["business_unit"],
            "column_dimensions": ["month"],
            "metrics": ["volume", "ams"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 6,
            "filters": {},
            "business_units": ["Eva Consumer", "Eva Bulk"],
        }
    )
    # Simulate post-merge: city cleared away
    spec["clear"] = ["city"]
    spec["filters"] = {}
    errors = validate_query_spec(
        spec,
        user_text="how are Eva consumer and Eva bulk sales in lahore",
    )
    assert any("Lahore" in e and "clear" in e.lower() for e in errors), errors


def test_validate_allows_clear_city_for_national_ask():
    spec = normalize_query_spec(
        {
            "state_action": "modify",
            "clear_filters": ["city"],
            "row_dimensions": ["business_unit"],
            "metrics": ["volume"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 6,
            "filters": {},
        }
    )
    spec["clear"] = ["city"]
    errors = validate_query_spec(
        spec,
        user_text="now show national / all Pakistan",
    )
    assert not any("clear_filters includes 'city'" in e for e in errors), errors


def test_execute_rejects_bad_clear_city_plan():
    """Exact user bug: modify + clear city while still saying in lahore."""
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        _seed()
        prior = {
            "filters": {"city": "Lahore"},
            "row_dimensions": ["business_unit"],
            "column_dimensions": ["month"],
            "metrics": ["volume", "ams"],
            "months_back": 6,
        }
        out = execute_query_spec(
            {
                "row_dimensions": ["business_unit"],
                "column_dimensions": ["month"],
                "metrics": ["volume", "ams"],
                "period_type": "LAST_N_MONTHS",
                "months_back": 6,
                "filters": {"city": "Lahore"},
                "business_units": ["Eva Consumer", "Eva Bulk"],
                "state_action": "modify",
                "clear_filters": ["city"],
            },
            prior=prior,
            user_text="how are Eva consumer and Eva bulk sales in lahore",
        )
        assert out.get("ok") is False
        errs = out.get("plan_errors") or []
        assert any("Lahore" in e for e in errs), errs


def test_execute_keeps_lahore_when_plan_correct():
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        _seed()
        out = execute_query_spec(
            {
                "state_action": "clear",
                "row_dimensions": ["business_unit"],
                "column_dimensions": ["month"],
                "metrics": ["volume", "ams"],
                "period_type": "LAST_N_MONTHS",
                "months_back": 6,
                "filters": {"city": "Lahore"},
                "business_units": ["Eva Consumer", "Eva Bulk"],
            },
            user_text="how are Eva consumer and Eva bulk sales in lahore",
        )
        assert out.get("ok") is True, out.get("plan_errors") or out.get("error")
        qs = out.get("query_spec") or {}
        filters = qs.get("filters") or out.get("filters") or {}
        assert filters.get("city") == "Lahore"
        # Caption / subtitle should mention Lahore
        md = (out.get("answer_markdown") or "").lower()
        assert "lahore" in md
