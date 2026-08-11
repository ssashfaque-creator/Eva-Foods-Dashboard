"""Plan→execute QuerySpec path — explicit plans, no silent sticky filters."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.query_executor import execute_query_spec
from eva_dashboard.query_spec import (
    merge_prior_into_spec,
    normalize_query_spec,
    prior_context_payload,
    validate_query_spec,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.chatbot import system_prompt, TOOLS


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed() -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, payload_json, updated_at) "
            "VALUES ('P', 'Eva Consumer', 'Eva VTF', 'Pouch', '{}', datetime('now'))"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "A Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("2", "B Dist", "Eva Distributors", "Karachi", "Karachi"),
                ("3", "C Dist", "Eva Distributors", "Islamabad", "Islamabad"),
            ],
        )
        rows = [
            ("2026-02-05", "A Dist", 30),
            ("2026-03-05", "A Dist", 30),
            ("2026-04-05", "A Dist", 30),
            ("2026-05-05", "A Dist", 10),
            ("2026-06-05", "A Dist", 10),
            ("2026-07-05", "A Dist", 10),
            ("2026-02-05", "B Dist", 5),
            ("2026-03-05", "B Dist", 5),
            ("2026-04-05", "B Dist", 5),
            ("2026-05-05", "B Dist", 20),
            ("2026-06-05", "B Dist", 20),
            ("2026-07-05", "B Dist", 20),
            ("2026-02-05", "C Dist", 8),
            ("2026-03-05", "C Dist", 8),
            ("2026-04-05", "C Dist", 8),
            ("2026-05-05", "C Dist", 9),
            ("2026-06-05", "C Dist", 9),
            ("2026-07-05", "C Dist", 9),
        ]
        for i, (dt, party, mt) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, 'P', ?, 'MT', ?,
                          'Eva Distributors', '{}')
                """,
                (f"pe-{i}", dt, party, mt, mt),
            )
        conn.commit()


def test_plan_query_tool_is_primary() -> None:
    names = [t["function"]["name"] for t in TOOLS]
    assert names[0] == "plan_query"
    text = system_prompt()
    assert "plan_query" in text
    assert "clear_filters" in text


def test_merge_prior_clear_city() -> None:
    prior = prior_context_payload(
        party_spec={
            "kind": "analyze_parties",
            "metric": "ams_growth",
            "group_by": "party",
            "filters": {"city": "Lahore", "client_type": "Eva Distributors"},
        }
    )
    spec = normalize_query_spec(
        {
            "intent": "party_rank",
            "context_handling": "prior",
            "clear_filters": ["city"],
            "period_type": "MTD",
            "group_by": "city",
            "ranking_metric": "ams_growth",
            "title_mode": "by_growth",
        }
    )
    merged = merge_prior_into_spec(spec, prior)
    assert merged["filters"].get("city") is None
    assert merged["filters"].get("client_type") == "Eva Distributors"
    assert merged["grain"]["group_by"] == "city"


def test_prior_requires_clear_filters() -> None:
    """context_handling=prior without clear_filters → plan_errors."""
    prior = prior_context_payload(
        party_spec={
            "kind": "analyze_parties",
            "metric": "ams_growth",
            "filters": {"city": "Lahore"},
        }
    )
    out = execute_query_spec(
        {
            "intent": "party_rank",
            "context_handling": "prior",
            "period_type": "MTD",
            "group_by": "city",
            "ranking_metric": "ams_growth",
            # clear_filters omitted on purpose
        },
        prior=prior,
    )
    assert out["ok"] is False
    assert any("clear_filters" in e for e in out.get("plan_errors") or [])


def test_national_followup_clears_city_via_query_spec() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            prior = prior_context_payload(
                party_spec={
                    "kind": "analyze_parties",
                    "metric": "ams_growth",
                    "filters": {
                        "city": "Lahore",
                        "client_type": "Eva Distributors",
                    },
                    "period_phrase": "July",
                }
            )
            out = execute_query_spec(
                {
                    "intent": "party_rank",
                    "context_handling": "prior",
                    "clear_filters": ["city"],
                    "period_type": "NAMED_MONTH",
                    "named_month": "July",
                    "group_by": "city",
                    "ranking_metric": "ams_growth",
                    "sort_order": "desc",
                },
                prior=prior,
            )
            assert out["ok"] is True, out
            assert (out.get("query_spec") or {}).get("filters", {}).get("city") is None
            # Multiple cities in result — not stuck on Lahore only
            parties = out.get("parties") or []
            cities = {p.get("city") for p in parties}
            assert len(cities) >= 2 or "Karachi" in cities or "Islamabad" in cities
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_prompt_teaches_eva_maan_consumer_brands() -> None:
    text = system_prompt()
    assert "Eva Consumer" in text and "Eva Bulk" in text
    assert "Maan Consumer" in text and "Maan Bulk" in text
    assert "clear_filters" in text
    assert "Karachi" in text  # geo fallback taught or live briefing


def test_last_n_months_plan_executes_month_grid() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            with connect() as conn:
                conn.execute(
                    """
                    INSERT INTO sales (
                      source_file_id, row_hash, imported_at, date, party, product,
                      qty, unit, mt_qty, client_type, payload_json
                    ) VALUES (NULL, 'pe-aug-khi', datetime('now'), '2026-08-03',
                              'B Dist', 'P', 1, 'MT', 1, 'Eva Distributors', '{}')
                    """
                )
                conn.commit()

            plan = {
                "intent": "sales_matrix",
                "context_handling": "none",
                "clear_filters": [],
                "period_type": "LAST_N_MONTHS",
                "months_back": 6,
                "filters": {
                    "city": "Lahore",
                    "client_type": "Eva Distributors",
                },
                "business_units": ["Eva Consumer", "Eva Bulk"],
            }
            out = execute_query_spec(plan)
            assert out["ok"] is True, out
            label = str((out.get("period") or {}).get("label") or "")
            assert "Aug 2026 MTD" not in label
            assert out.get("column_dimension") == "month"
            md = out.get("answer_markdown") or ""
            assert "_No data._" not in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_validate_sticky_city_with_city_grain() -> None:
    spec = normalize_query_spec(
        {
            "intent": "party_rank",
            "context_handling": "prior",
            "clear_filters": [],  # forgot to clear city
            "period_type": "MTD",
            "group_by": "city",
            "filters": {"city": "Lahore"},
            "ranking_metric": "ams_growth",
        }
    )
    errs = validate_query_spec(spec)
    assert any("clear_filters" in e and "city" in e for e in errs)
