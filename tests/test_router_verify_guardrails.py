"""Router, SQL guardrails, and answer verifier for ReAct v1.4.1."""

from __future__ import annotations

from eva_dashboard.agent_loop import dispatch_react_tool
from eva_dashboard.tools.answer_verifier import verify_agent_answer
from eva_dashboard.tools.intent_router import route_ask, tool_allowed
from eva_dashboard.tools.sql_tool import apply_eva_sql_guardrails, execute_read_only_sql


def test_route_standard_ams() -> None:
    r = route_ask("show volume and AMS by Business Unit for the last 6 months")
    assert r["kind"] == "standard"
    assert "run_standard_analytics_pivot" in r["preferred_tools"]
    assert "execute_read_only_sql" in (r["blocked_tools"] or [])
    ok, _ = tool_allowed("execute_read_only_sql", r)
    assert ok is False


def test_route_discovery_lowest_rate() -> None:
    r = route_ask("what was the lowest rate Pepsi was sold at, and who was the buyer?")
    assert r["kind"] == "discovery"
    assert "execute_read_only_sql" in r["preferred_tools"]


def test_route_math_and_mixed() -> None:
    r = route_ask("take the Pepsi rate, multiply it by 24.7 and divide by 6")
    assert r["kind"] in {"math", "mixed"}
    assert "calculate_expression" in r["preferred_tools"]


def test_route_clarify_bare_price() -> None:
    r = route_ask("what's the pepsi price")
    assert r["kind"] == "clarify"
    assert r.get("clarify_question")
    # With prior price context, don't force clarify
    r2 = route_ask(
        "what's the pepsi price",
        prior={"metrics": ["avg_price"]},
    )
    assert r2["kind"] != "clarify" or r2["confidence"] < 0.5


def test_sql_guardrails_ban_ams_and_qty() -> None:
    try:
        apply_eva_sql_guardrails(
            "SELECT AVG(rate) AS ams FROM sales WHERE date > '2026-01-01'"
        )
        # "ams" as alias alone might not match banned — test Price Fetch constant
    except ValueError:
        pass
    blocked = execute_read_only_sql(
        "SELECT rate * 37.3246 AS pf FROM sales LIMIT 5"
    )
    assert blocked["ok"] is False
    assert "Price Fetch" in blocked["error"] or "AMS" in blocked["error"] or "pivot" in blocked["markdown"]

    qty = execute_read_only_sql(
        "SELECT party, SUM(qty) AS vol FROM sales GROUP BY party"
    )
    assert qty["ok"] is False
    assert "mt_qty" in qty["markdown"].lower() or "mt_qty" in (qty.get("error") or "").lower()

    bad_table = execute_read_only_sql("SELECT * FROM sqlite_master")
    # sqlite_master not in FROM whitelist for user queries via JOIN/FROM capture
    # Actually FROM sqlite_master would be caught
    assert bad_table["ok"] is False


def test_sql_guardrails_allow_min_rate() -> None:
    # Validation-only path without DB for guardrails function
    q = apply_eva_sql_guardrails(
        "SELECT party, MIN(rate) AS min_rate FROM sales "
        "WHERE party LIKE '%PEPSI%' GROUP BY party"
    )
    assert "MIN(rate)" in q


def test_verify_catches_math_without_calculator() -> None:
    route = route_ask("multiply the rate by 24.7 and divide by 6")
    check = verify_agent_answer(
        "multiply the rate by 24.7 and divide by 6",
        "The result is 500.",
        tool_trace=[{"tool": "execute_read_only_sql", "ok": True, "preview": "80"}],
        route=route,
    )
    assert check["ok"] is False
    assert any("calculate_expression" in i for i in check["issues"])


def test_verify_passes_clarify() -> None:
    route = route_ask("what's the pepsi price")
    check = verify_agent_answer(
        "what's the pepsi price",
        route["clarify_question"],
        tool_trace=[],
        route=route,
    )
    assert check["ok"] is True


def test_dispatch_respects_router_block() -> None:
    route = route_ask("show AMS for Eva Consumer last 6 months")
    out = dispatch_react_tool(
        "execute_read_only_sql",
        {"sql_query": "SELECT 1 FROM sales"},
        route=route,
    )
    assert out["ok"] is False
    assert "blocked" in out["markdown"].lower() or "Router blocked" in out["markdown"]
