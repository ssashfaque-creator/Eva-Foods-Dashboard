"""Offline golden magic eval — router + playbook harness."""

from __future__ import annotations

from pathlib import Path

from eva_dashboard.eval_harness import load_golden_cases, run_eval, score_case
from eva_dashboard.personal_lexicon import (
    learn_from_turn,
    load_lexicon,
    remember_clarify,
    should_skip_clarify,
)


def test_golden_magic_eval_all_pass() -> None:
    out = run_eval()
    failed = [r for r in out["results"] if not r["ok"]]
    assert out["failed"] == 0, failed


def test_golden_cases_nonempty() -> None:
    cases = load_golden_cases()
    assert len(cases) >= 10
    assert score_case(cases[0])["id"] == cases[0]["id"]


def test_learn_from_turn_style_and_stats(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EVA_DATA_DIR", str(tmp_path / "data"))
    learn_from_turn(
        "pepsi volume",
        route={"kind": "standard"},
        grounding={
            "party_hits": [{"spoken": "pepsi", "resolved": "PEPSI-COLA INT"}]
        },
        tool_trace=[{"tool": "run_standard_analytics_pivot", "ok": True}],
        answer="| Party | MT |\n|---|---|\n| Pepsi | 12 |\n\n### Analysis\n- Steady.",
        verify_ok=True,
    )
    data = load_lexicon()
    assert data["style"].get("table_first") is True
    assert data["ask_stats"].get("standard", 0) >= 1
    assert "pepsi" in data["party_aliases"]


def test_clarify_budget_skip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EVA_DATA_DIR", str(tmp_path / "data"))
    q = "Which price — average rate, last sold, lowest rate, or Price Fetch?"
    remember_clarify(q, "what's the pepsi price")
    assert should_skip_clarify("what's the pepsi price", q) is True
    assert should_skip_clarify("show volume by BU", "unrelated clarify?") is False
