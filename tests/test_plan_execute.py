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
