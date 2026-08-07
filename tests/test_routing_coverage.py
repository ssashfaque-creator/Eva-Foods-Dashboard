"""Question-bank routing coverage — forced tool choice + named-party extraction."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.advanced_routing import infer_advanced_from_text, looks_advanced
from eva_dashboard.chatbot import (
    _dispatch_tool,
    _extract_named_party_query,
    _looks_named_party_sales,
    resolve_forced_tool,
)
from eva_dashboard.db import connect, init_db


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


# (question, expected_tool)
ROUTING_CASES: list[tuple[str, str]] = [
    # Sales matrices
    ("What were Eva Consumer sales in Lahore last month?", "query_sales"),
    ("Show distributor sales in Karachi for July", "query_sales"),
    ("Eva Consumer packing breakdown for Karachi last month", "query_sales"),
    ("How are Eva Consumer sales doing in Lahore this month?", "query_sales"),
    ("Show Eva Consumer sales city wise for July", "query_sales"),
    ("Month wise Eva Consumer sales last 6 months", "query_sales"),
    ("Product breakdown for Eva Consumer Stand up in Lahore July", "query_sales"),
    ("Compare Eva Consumer sales vs last year in Lahore", "query_sales"),
    ("Show Imtiaz sales for July", "query_sales"),
    ("Eva Bulk sales this month so far", "query_sales"),
    ("VTF bulk sales last month", "query_sales"),
    ("Canola standup sales in Lahore July", "query_sales"),
    # Client lists / distributor break
    ("Who are the distributors in Lahore?", "list_clients"),
    ("List Imtiaz stores in Karachi", "list_clients"),
    ("Break of the distributors", "list_clients"),
    ("By individual distributors", "list_clients"),
    ("Show distributors wise", "list_clients"),
    ("Break down of distributors for Eva Consumer Karachi July", "list_clients"),
    # Party analytics
    ("Top 10 distributors by AMS last month", "analyze_parties"),
    ("Which distributors are falling behind on AMS?", "analyze_parties"),
    ("New distributors this month", "analyze_parties"),
    ("Lost parties last quarter", "analyze_parties"),
    ("Silent distributors this week", "analyze_parties"),
    ("Product mix for Imtiaz last month", "analyze_parties"),
    ("Product breakdown for each distributor in Lahore July", "analyze_parties"),
    ("Packing mix for distributors in Karachi", "analyze_parties"),
    ("Share of Imtiaz in Lahore Eva Consumer July", "analyze_parties"),
    ("Top 10 cities by volume last month", "analyze_parties"),
    ("Invoice frequency for distributors in Lahore", "analyze_parties"),
    # Named party sales
    ("Show sales for Alpha Dist in July", "lookup_party"),
    ("What were Rubina Shaheen sales last month?", "lookup_party"),
    ("Sales of Gamma Dist this month", "lookup_party"),
    ("Who is Al Bari?", "lookup_party"),
    ("show me Alpha Dist sales in July", "lookup_party"),
    # Advanced
    ("Compare Lahore vs Karachi last month", "advanced_query"),
    ("Compare Imtiaz vs distributors growth last month", "advanced_query"),
    ("Week over week sales change", "advanced_query"),
    ("Which packing is growing fastest?", "advanced_query"),
    ("What are our expected sales for this month?", "advanced_query"),
    ("Distributors with AMS but zero this week", "advanced_query"),
    ("Which distributors have not ordered Stand up this month?", "advanced_query"),
    ("Identify any dumping this month", "advanced_query"),
    ("Show reactivated parties", "advanced_query"),
    ("Days since last invoice for distributors", "advanced_query"),
    ("Which cities declined more than 20% YoY?", "advanced_query"),
    ("Parties that grew more than 30% last month", "advanced_query"),
    ("Concentration of sales in Lahore", "advanced_query"),
    ("Top SKUs in Karachi last month", "advanced_query"),
    # Price
    ("What is the average rate for Eva Canola in Lahore July?", "query_price"),
    ("Price Fetch for Eva Consumer last month", "query_price"),
    # Standalone exclude → sales (not table-remove follow-up)
    ("Exclude online customers from Lahore sales last month", "query_sales"),
]


def test_question_bank_forced_tool_routing() -> None:
    failures: list[str] = []
    for question, expected in ROUTING_CASES:
        got = resolve_forced_tool(question)
        if got != expected:
            failures.append(f"{got!r} != {expected!r}: {question}")
    assert not failures, "Routing mismatches:\n" + "\n".join(failures)


def test_table_followups_require_prior_spec() -> None:
    assert resolve_forced_tool("Does this include bulk?") in {"query_sales", "required", "auto"}
    assert resolve_forced_tool(
        "Does this include bulk?",
        prior_table_spec={"filters": {"city": "Lahore"}},
    ) == "query_sales"
    assert resolve_forced_tool(
        "City wise",
        prior_table_spec={"filters": {"city": "Lahore"}},
    ) == "query_sales"
    assert resolve_forced_tool(
        "Remove Lahore",
        prior_table_spec={"filters": {"city": "Lahore"}},
    ) == "query_sales"
    assert resolve_forced_tool(
        "Group by oil type",
        prior_table_spec={"column_dimension": "client_type"},
    ) == "query_sales"


def test_named_party_natural_phrasing() -> None:
    cases = {
        "Show sales for Alpha Dist in July": "Alpha Dist",
        "What were Rubina Shaheen sales last month?": "Rubina Shaheen",
        "Sales of Gamma Dist this month": "Gamma Dist",
        "show me Alpha Dist sales in July": "Alpha Dist",
        "Can you show me sales for the client rubina Shaheen in July": "rubina Shaheen",
    }
    for q, name in cases.items():
        assert _extract_named_party_query(q) == name, q
        assert _looks_named_party_sales(q), q
    # Client-type sales stay on the matrix path
    assert _extract_named_party_query("Show Imtiaz sales for July") is None
    assert not _looks_named_party_sales("Show Imtiaz sales for July")
    assert resolve_forced_tool("Show Imtiaz sales for July") == "query_sales"


def test_advanced_modes_not_stolen_by_party_analytics() -> None:
    assert infer_advanced_from_text(
        "Compare Imtiaz vs distributors growth last month"
    )["mode"] == "compare_client_types"
    assert infer_advanced_from_text("Show reactivated parties")["mode"] == "reactivated"
    assert looks_advanced("Days since last invoice for distributors")
    assert looks_advanced("Which cities declined more than 20% YoY?")


def test_named_party_dist_suffix_and_exclude_online_execute() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            init_db()
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO category "
                    "(product, category_1, category_2, packing_category, "
                    "payload_json, updated_at) VALUES "
                    "('Eva Canola Oil (StandUpPouch)', 'Eva Consumer', "
                    "'Eva Canola', 'Stand up', '{}', datetime('now'))"
                )
                conn.executemany(
                    "INSERT OR REPLACE INTO clients "
                    "(client_id, client, type, city_filter, city, inactive, "
                    "payload_json, updated_at) VALUES "
                    "(?, ?, ?, ?, ?, '', '{}', datetime('now'))",
                    [
                        ("1", "Alpha Dist", "Eva Distributors", "Lahore", "Lahore"),
                        ("2", "Online Buyer", "Online Customer", "Lahore", "Lahore"),
                        ("3", "Gamma Dist", "Eva Distributors", "Karachi", "Karachi"),
                    ],
                )
                rows = [
                    ("2026-07-05", "Alpha Dist", 40.0, "Eva Distributors"),
                    ("2026-07-06", "Online Buyer", 10.0, "Online Customer"),
                    ("2026-07-07", "Gamma Dist", 15.0, "Eva Distributors"),
                    ("2026-08-01", "Alpha Dist", 8.0, "Eva Distributors"),
                ]
                for i, (dt, party, mt, ctype) in enumerate(rows):
                    conn.execute(
                        """
                        INSERT INTO sales (
                          source_file_id, row_hash, imported_at, date, party,
                          product, qty, unit, mt_qty, client_type, payload_json
                        ) VALUES (
                          NULL, ?, datetime('now'), ?, ?,
                          'Eva Canola Oil (StandUpPouch)', ?, 'MT', ?, ?, '{}'
                        )
                        """,
                        (f"rt-{i}", dt, party, mt, mt, ctype),
                    )
                conn.commit()

            named = _dispatch_tool(
                "lookup_party",
                {},
                user_text="Show sales for Alpha Dist in July",
            )
            assert named["ok"] is True
            assert named["mode"] == "party_sales"
            assert named["party"] == "Alpha Dist"

            excl = _dispatch_tool(
                "query_sales",
                {"period": "July", "city": "Lahore"},
                user_text="Exclude online customers from Lahore sales last month",
            )
            assert excl["ok"] is True
            ex = (excl.get("table_spec") or excl).get("excludes") or excl.get("excludes")
            assert ex
            assert any(
                "online" in str(v).lower()
                for vals in (ex or {}).values()
                for v in vals
            )
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
