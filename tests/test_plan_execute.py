"""Plan→execute QuerySpec path — explicit plans, no silent sticky filters."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.query_executor import execute_query_spec, heuristic_plan_query
from eva_dashboard.query_spec import (
    merge_prior_into_spec,
    normalize_query_spec,
    prior_context_payload,
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
    assert "PRIOR_QUERY_CONTEXT" in text or "plan→execute" in text.lower() or "plan" in text.lower()


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
            "base": "prior",
            "clear": ["city"],
            "grain": {"group_by": "city"},
            "metric": "ams_growth",
            "title_mode": "by_growth",
        }
    )
    merged = merge_prior_into_spec(spec, prior)
    assert merged["filters"].get("city") is None
    assert merged["filters"].get("client_type") == "Eva Distributors"
    assert merged["grain"]["group_by"] == "city"
    assert merged["metric"] == "ams_growth"


def test_execute_growth_vs_other_cities() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            prior = prior_context_payload(
                party_spec={
                    "kind": "analyze_parties",
                    "metric": "ams_growth",
                    "group_by": "party",
                    "filters": {
                        "city": "Lahore",
                        "client_type": "Eva Distributors",
                    },
                    "period": {
                        "date_from": "2026-03-01",
                        "date_to": "2026-08-05",
                    },
                }
            )
            q = "How is this growth compared to other cities"
            plan = {
                "intent": "party_rank",
                "base": "prior",
                "clear": ["city"],
                "grain": {"group_by": "city"},
                "metric": "ams_growth",
                "sort": "desc",
                "grown_only": False,
                "title_mode": "by_growth",
            }
            out = execute_query_spec(plan, prior=prior, user_text=q)
            assert out["ok"] is True
            assert out["filters"].get("city") is None
            assert out["filters"].get("client_type") == "Eva Distributors"
            md = out["answer_markdown"]
            assert "Cities by AMS growth %" in md
            assert "Biggest AMS gains" not in md
            cities = [p["city"] for p in out["parties"]]
            assert "Karachi" in cities and "Lahore" in cities
            assert cities[0] == "Karachi"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_prompt_teaches_eva_maan_consumer_brands() -> None:
    """Brand shorthand is taught to the model — not forced in the executor."""
    text = system_prompt()
    assert "Eva Consumer" in text and "Eva Bulk" in text
    assert "Maan Consumer" in text and "Maan Bulk" in text
    # Consumer alone → Eva Consumer
    assert "Consumer" in text
    low = text.lower()
    assert "eva" in low and "bulk" in low


def test_eva_distributor_sales_expands_brand_and_channel() -> None:
    """Eva distributor sales = Eva Consumer+Bulk brand AND Eva Distributors channel."""
    from eva_dashboard.chatbot import _extract_business_units_from_text
    from eva_dashboard.client_language import extract_client_type_from_text

    q = "show me how Eva distributor sales in lahore are doing last 6 months"
    assert set(_extract_business_units_from_text(q)) == {
        "Eva Consumer",
        "Eva Bulk",
    }
    assert extract_client_type_from_text(q) == "Eva Distributors"
    hp = heuristic_plan_query(q)
    assert set(hp.get("business_units") or []) == {"Eva Consumer", "Eva Bulk"}
    assert (hp.get("filters") or {}).get("client_type") == "Eva Distributors"
    assert hp["period"].get("phrase") == "last 6 months"
    assert hp["grain"].get("column_dimension") == "month"


def test_plan_omitted_period_does_not_become_mtd_when_last_n_months() -> None:
    """Model plan with filters but blank period must not fall through to MTD.

    Regression: "how Eva distributor sales in lahore are doing last 6 months"
    became Sales for Aug 2026 MTD → _No data._ because execute_query_spec
    filled city/client_type but not period, and resolve_period(None) → MTD.
    Helpers must ONLY fill blanks — never override an explicit period.
    """
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            # Extend seed with early Aug (max sales date) but no Lahore Aug rows
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

            q = (
                "show me how Eva distributor sales in lahore are doing "
                "last 6 months"
            )
            # Exact failure shape: analytical + filters + Eva BUs, empty period
            bad_plan = {
                "intent": "sales_analytical",
                "base": "none",
                "filters": {
                    "city": "Lahore",
                    "client_type": "Eva Distributors",
                },
                "business_units": ["Eva Consumer", "Eva Bulk"],
                "period": {},
                "grain": {},
            }
            out = execute_query_spec(bad_plan, user_text=q)
            assert out["ok"] is True
            label = str((out.get("period") or {}).get("label") or "")
            assert "MTD" not in label or "Last 6" in label
            assert "Aug 2026 MTD" not in label
            assert out.get("column_dimension") == "month"
            assert int((out.get("table_spec") or {}).get("months_back") or 0) == 6
            md = out.get("answer_markdown") or ""
            assert "Aug 2026 MTD" not in md
            assert "_No data._" not in md
            assert "Last 6 months" in md or "Mar 2026" in md

            # Explicit period + explicit grain must not be rewritten by helpers
            keep = execute_query_spec(
                {
                    "intent": "sales_analytical",
                    "base": "none",
                    "filters": {
                        "city": "Lahore",
                        "client_type": "Eva Distributors",
                    },
                    "period": {"phrase": "last 6 months"},
                    "grain": {"column_dimension": "client_type"},
                },
                user_text=q,
            )
            assert keep["ok"] is True
            assert (keep.get("query_spec") or {}).get("period", {}).get(
                "phrase"
            ) == "last 6 months"
            assert keep.get("column_dimension") == "city"  # ctype filter flips cols
            keep_label = str((keep.get("period") or {}).get("label") or "")
            assert "Last 6 months" in keep_label
            assert "Aug 2026 MTD" not in keep_label

            hp = heuristic_plan_query(q)
            assert hp["period"].get("phrase") == "last 6 months"
            assert hp["grain"].get("column_dimension") == "month"
            assert hp["intent"] == "sales_matrix"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_heuristic_plan_least_gains_and_other_cities() -> None:
    prior = prior_context_payload(
        party_spec={
            "kind": "analyze_parties",
            "metric": "ams_growth",
            "filters": {"city": "Lahore", "client_type": "Eva Distributors"},
        }
    )
    hp = heuristic_plan_query(
        "How is this growth compared to other cities", prior=prior
    )
    assert hp["intent"] == "party_rank"
    assert hp["base"] == "prior"
    assert "city" in hp["clear"]
    assert hp["grain"]["group_by"] == "city"
    assert hp["metric"] == "ams_growth"
    assert hp["title_mode"] == "by_growth"

    least = heuristic_plan_query(
        "which distributors have the least AMS gains", prior=None
    )
    assert least["intent"] == "party_rank"
    assert least["sort"] == "asc"
    assert least["grown_only"] is False
    assert least["title_mode"] == "smallest_gains"
