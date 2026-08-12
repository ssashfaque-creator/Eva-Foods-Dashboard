"""Production chat hardening — money baseline, price prefs, feedback."""

from __future__ import annotations

from pathlib import Path

import pytest

from eva_dashboard.chat_feedback import list_eval_failures, record_chat_feedback
from eva_dashboard.chatbot import DEFAULT_MODEL
from eva_dashboard.eval_harness import run_eval
from eva_dashboard.personal_lexicon import (
    default_price_metric,
    parse_price_preference,
    remember_pref,
    remember_price_preference_from_text,
)
from eva_dashboard.tools.answer_verifier import verify_agent_answer
from eva_dashboard.tools.intent_router import route_ask


def test_default_model_is_gpt4o() -> None:
    assert DEFAULT_MODEL == "gpt-4o"


def test_money_baseline_all_pass() -> None:
    out = run_eval()
    assert out["failed"] == 0, [r for r in out["results"] if not r["ok"]]
    assert out["money_total"] >= 4
    assert out["money_passed"] == out["money_total"]


def test_verify_rejects_ams_via_sql_only() -> None:
    check = verify_agent_answer(
        "show volume and AMS by BU last 6 months",
        "| BU | AMS |\n|---|---|\n| Eva | 10 |",
        tool_trace=[
            {"tool": "execute_read_only_sql", "ok": True},
        ],
        route={"kind": "standard"},
    )
    assert check["ok"] is False
    assert any("pivot" in i.lower() or "sql" in i.lower() for i in check["issues"])


def test_price_pref_clarify_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EVA_DATA_DIR", str(tmp_path / "data"))
    first = route_ask("what's the pepsi price")
    assert first["kind"] == "clarify"
    remember_price_preference_from_text("last sold")
    assert default_price_metric() == "last_price"
    second = route_ask("what's the pepsi price")
    assert second["kind"] == "standard"
    assert "learned pref" in second["rationale"]


def test_parse_price_preference_variants() -> None:
    assert parse_price_preference("average rate") == "avg_price"
    assert parse_price_preference("Price Fetch") == "price_fetch"
    assert parse_price_preference("lowest") == "min_rate"


def test_feedback_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EVA_DATA_DIR", str(tmp_path / "data"))
    row_id = record_chat_feedback(
        rating="down",
        user_text="show AMS by BU",
        answer="wrong table",
        route={"kind": "standard"},
        tool_trace=[{"tool": "execute_read_only_sql", "ok": True}],
        model="gpt-4o",
        source="test",
    )
    assert row_id > 0
    rows = list_eval_failures(rating="down", limit=10)
    assert any(r["id"] == row_id for r in rows)


def test_react_agent_deprecation_warning(monkeypatch) -> None:
    from eva_dashboard.agent_loop import react_agent_enabled

    monkeypatch.setenv("EVA_REACT_AGENT", "0")
    with pytest.warns(DeprecationWarning):
        assert react_agent_enabled() is False
    monkeypatch.setenv("EVA_REACT_AGENT", "1")
    assert react_agent_enabled() is True
