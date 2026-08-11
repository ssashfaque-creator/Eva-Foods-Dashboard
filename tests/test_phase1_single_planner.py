"""Phase 1: single planner path + sticky party follow-ups."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    plan_query_redirect_result,
    resolve_forced_tool,
    should_redirect_to_plan_query,
    system_prompt,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.query_executor import (
    _looks_party_metric_followup,
    execute_query_spec,
)
from eva_dashboard.query_spec import prior_context_for_prompt, prior_context_payload


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
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES ('1', 'Alpha Dist', 'Eva Distributors', 'Lahore', 'Lahore', '', "
            "'{}', datetime('now'))"
        )
        for i, (dt, mt) in enumerate(
            [
                ("2026-04-01", 10),
                ("2026-05-01", 12),
                ("2026-06-01", 14),
                ("2026-07-01", 18),
            ]
        ):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, 'Alpha Dist', 'P1',
                          ?, 'MT', ?, 100, ?, 'Eva Distributors', '{}')
                """,
                (f"p1-{i}", dt, mt, mt, mt * 1000),
            )
        conn.commit()


def test_resolve_forced_tool_never_pins_query_sales() -> None:
    assert resolve_forced_tool("Show me Lahore sales") == "required"
    assert (
        resolve_forced_tool(
            "City wise",
            prior_table_spec={"filters": {"city": "Lahore"}},
        )
        == "required"
    )
    assert (
        resolve_forced_tool(
            "Remove Lahore",
            prior_table_spec={"filters": {"city": "Lahore"}},
            explicit_followup=True,
        )
        == "required"
    )
    assert resolve_forced_tool("Thanks!") == "auto"


def test_legacy_analytics_tools_redirect_to_plan_query() -> None:
    for name in (
        "query_sales",
        "list_clients",
        "analyze_parties",
        "lookup_party",
        "advanced_query",
        "product_sales",
    ):
        assert should_redirect_to_plan_query(name, user_text="Show Lahore sales")
        payload = plan_query_redirect_result(name)
        assert payload["ok"] is False
        assert "plan_query" in payload["error"]
    assert not should_redirect_to_plan_query("plan_query")
    assert not should_redirect_to_plan_query("run_sql")
    assert not should_redirect_to_plan_query("get_schema")
    assert not should_redirect_to_plan_query(
        "query_price", user_text="what's the cost factor?"
    )
    assert should_redirect_to_plan_query(
        "query_price", user_text="Price Fetch for Eva Consumer"
    )


def test_system_prompt_teaches_single_planner() -> None:
    text = system_prompt()
    assert "plan_query" in text
    assert "Do NOT call query_sales" in text or "only plan_query" in text.lower()


def test_party_metric_followup_detector() -> None:
    assert _looks_party_metric_followup("what's the price?")
    assert _looks_party_metric_followup("what % of their AMS is this")
    assert _looks_party_metric_followup("last purchase date")
    assert _looks_party_metric_followup("Price Fetch")
    assert not _looks_party_metric_followup("Show me Lahore sales last month")
    assert not _looks_party_metric_followup(
        "What's the average rate for Eva Canola in Lahore July"
    )


def test_prior_context_surfaces_party_scope() -> None:
    prior = prior_context_payload(
        table_spec={
            "filters": {"party": "Alpha Dist", "city": "Lahore"},
            "period_phrase": "July",
            "column_dimension": "month",
        }
    )
    assert prior is not None
    assert prior.get("party_scope", {}).get("party") == "Alpha Dist"
    prompt = prior_context_for_prompt(prior)
    assert "CUSTOMER SCOPE ACTIVE" in prompt
    assert "Alpha Dist" in prompt


def test_party_followup_keeps_customer_on_price() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            prior = prior_context_payload(
                table_spec={
                    "filters": {"party": "Alpha Dist", "city": "Lahore"},
                    "period_phrase": "July",
                    "business_units": ["Eva Consumer"],
                }
            )
            # Model forgot context_handling=prior and omitted party
            out = execute_query_spec(
                {
                    "row_dimensions": ["product"],
                    "metrics": ["avg_price"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                },
                prior=prior,
                user_text="what's the price?",
            )
            assert out.get("ok") is True, out
            filters = (out.get("query_spec") or {}).get("filters") or {}
            assert filters.get("party") == "Alpha Dist" or filters.get("party_ilike")
            # table/price spec stamped for next turn
            stamped = out.get("price_spec") or out.get("table_spec") or {}
            stamped_f = stamped.get("filters") or {}
            assert stamped_f.get("party") == "Alpha Dist" or stamped_f.get(
                "party_ilike"
            )
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_party_followup_explicit_prior_keeps_ams() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            prior = prior_context_payload(
                table_spec={
                    "filters": {"party": "Alpha Dist"},
                    "period_phrase": "July",
                }
            )
            out = execute_query_spec(
                {
                    "row_dimensions": ["party"],
                    "metrics": ["volume", "vs_ams"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "prior",
                    "clear_filters": [],
                },
                prior=prior,
                user_text="what % of their AMS is this",
            )
            assert out.get("ok") is True, out
            filters = (out.get("query_spec") or {}).get("filters") or {}
            assert filters.get("party") == "Alpha Dist"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_clear_party_drops_all_party_scope_keys() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            prior = prior_context_payload(
                table_spec={
                    "filters": {
                        "party": "Alpha Dist",
                        "party_ilike": ["alpha"],
                        "city": "Lahore",
                    }
                }
            )
            out = execute_query_spec(
                {
                    "row_dimensions": ["business_unit"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "column_dimensions": ["month"],
                    "context_handling": "prior",
                    "clear_filters": ["party"],
                    "filters": {"city": "Lahore"},
                },
                prior=prior,
                user_text="now show all Lahore sales without Alpha",
            )
            assert out.get("ok") is True, out
            filters = (out.get("query_spec") or {}).get("filters") or {}
            assert not filters.get("party")
            assert not filters.get("party_ilike")
            assert filters.get("city") == "Lahore"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
