"""Offline golden eval for AI-first routing: required + preferred-tool hints."""

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


def test_golden_forced_tool_ai_first() -> None:
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


def test_phase1_never_pins_legacy_tools() -> None:
    """Phase 1: factual asks are required; Reply mutations no longer pin query_sales."""
    assert resolve_forced_tool("Show me Lahore sales") == "required"
    assert resolve_forced_tool("how are Maan sales in Karachi") == "required"
    assert resolve_forced_tool("how are distributor sales in karachi") == "required"
    assert resolve_forced_tool("Top 10 distributors by AMS last month") == "required"
    assert resolve_forced_tool("Compare Lahore vs Karachi last month") == "required"
    assert resolve_forced_tool("what Food Panda are active") == "required"
    assert resolve_forced_tool("Price Fetch for Eva Consumer last month") == "required"
    assert resolve_forced_tool("Who are the distributors in Lahore?") == "required"
    assert resolve_forced_tool("which distributor is selling maan") == "required"
    assert resolve_forced_tool("Show me Alpha Dist sales") == "required"
    assert resolve_forced_tool("Who is Al Bari?") == "required"
    assert suggest_preferred_tool("Show me Lahore sales") == "query_sales"
    assert suggest_preferred_tool("Show me Alpha Dist sales") == "lookup_party"
    assert (
        resolve_forced_tool(
            "group by city",
            prior_table_spec={"column_dimension": "month"},
        )
        == "required"
    )
    assert (
        resolve_forced_tool(
            "Does this include bulk?",
            prior_table_spec={"filters": {"city": "Lahore"}},
        )
        == "required"
    )
