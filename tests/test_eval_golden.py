"""Offline golden eval for v0.4.0 slim forced routing + preferred-tool hints."""

from __future__ import annotations

import json
from pathlib import Path

from eva_dashboard.chatbot import resolve_forced_tool, suggest_preferred_tool

GOLDEN_PATH = Path(__file__).resolve().parent / "eval_golden.json"


def _load_cases() -> list[dict]:
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert data.get("version")
    cases = data.get("cases") or []
    assert cases, "eval_golden.json has no cases"
    return cases


def test_golden_forced_tool_v040() -> None:
    failures: list[str] = []
    for case in _load_cases():
        q = case["q"]
        expected = case["expected_forced"]
        got = resolve_forced_tool(
            q,
            prior_table_spec=case.get("prior_table_spec"),
            prior_party_spec=case.get("prior_party_spec"),
            explicit_followup=bool(case.get("explicit_followup")),
        )
        if got != expected:
            failures.append(
                f"{case.get('id')}: forced {got!r} != {expected!r} | {q}"
            )
    assert not failures, "Forced-tool mismatches:\n" + "\n".join(failures)


def test_golden_preferred_tool_hint() -> None:
    """Preferred labels stay available for CSV / training even when not forced."""
    failures: list[str] = []
    for case in _load_cases():
        preferred = case.get("preferred_tool")
        if not preferred or preferred == "auto":
            continue
        got = suggest_preferred_tool(
            case["q"],
            prior_table_spec=case.get("prior_table_spec"),
            prior_party_spec=case.get("prior_party_spec"),
            explicit_followup=bool(case.get("explicit_followup")),
        )
        if got != preferred:
            failures.append(
                f"{case.get('id')}: preferred {got!r} != {preferred!r} | {case['q']}"
            )
    assert not failures, "Preferred-tool mismatches:\n" + "\n".join(failures)


def test_v040_forces_only_high_confidence() -> None:
    """General factual asks must not hard-pin a tool name."""
    assert resolve_forced_tool("Show me Lahore sales") == "required"
    assert resolve_forced_tool("Top 10 distributors by AMS last month") == "required"
    assert resolve_forced_tool("Compare Lahore vs Karachi last month") == (
        "advanced_query"
    )
    assert resolve_forced_tool("what Food Panda are active") == "list_clients"
    assert resolve_forced_tool("Price Fetch for Eva Consumer last month") == (
        "query_price"
    )
    # High-confidence party / named-party asks stay forced
    assert resolve_forced_tool("Who are the distributors in Lahore?") == "list_clients"
    assert resolve_forced_tool("which distributor is selling maan") == "list_clients"
    assert resolve_forced_tool("Show me Alpha Dist sales") == "lookup_party"
    assert resolve_forced_tool("Who is Al Bari?") == "lookup_party"
    assert (
        resolve_forced_tool(
            "group by city",
            prior_table_spec={"column_dimension": "month"},
        )
        == "query_sales"
    )
