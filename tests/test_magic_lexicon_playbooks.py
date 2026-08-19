"""Personal lexicon, playbooks, and ask grounding — magic layer."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from eva_dashboard.ask_grounding import ground_ask_for_agent
from eva_dashboard.personal_lexicon import (
    expand_aliases_in_text,
    lexicon_prompt_block,
    load_lexicon,
    remember_party_alias,
    remember_pref,
    sync_prefs_from_memory,
)
from eva_dashboard.playbooks import match_playbooks, playbook_prompt_block
from eva_dashboard.tools.intent_router import route_ask


def test_lexicon_learn_and_expand(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EVA_DATA_DIR", str(tmp_path / "data"))
    remember_party_alias("pepsi", "PEPSI-COLA INTERNATIONAL")
    remember_pref("default_city", "Lahore")
    data = load_lexicon()
    assert "pepsi" in data["party_aliases"]
    assert data["prefs"]["default_city"] == "Lahore"
    _, expansions = expand_aliases_in_text("what's the pepsi rate")
    assert any(e["spoken"] == "pepsi" for e in expansions)
    block = lexicon_prompt_block("pepsi rate in lahore")
    assert "PERSONAL_LEXICON" in block
    assert "pepsi" in block.lower()


def test_sync_prefs_from_memory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EVA_DATA_DIR", str(tmp_path / "data"))
    sync_prefs_from_memory(
        {
            "filters": {"city": "Multan", "client_type": "Eva Distributors"},
            "business_units": ["Eva Consumer", "Eva Bulk"],
        }
    )
    data = load_lexicon()
    assert data["prefs"]["default_city"] == "Multan"
    assert "Eva Consumer" in data["prefs"]["default_business_units"]


def test_playbooks_match_magic_asks() -> None:
    lowest = match_playbooks(
        "what was the lowest rate Pepsi was sold at and who was the buyer?"
    )
    assert any(p["id"] == "lowest_rate_then_buyer" for p in lowest)
    math = match_playbooks("take the Pepsi rate multiply by 24.7 and divide by 6")
    assert any(p["id"] == "rate_then_math" for p in math)
    grown = match_playbooks("which distributors have grown sales")
    assert any(p["id"] == "distributors_grown" for p in grown)
    declined = match_playbooks(
        "Which distributors declined the most vs AMS?"
    )
    assert any(p["id"] == "distributors_declined" for p in declined)
    disp = match_playbooks(
        "which distributors were sold at different prices on the same date"
    )
    assert any(p["id"] == "same_date_price_variance" for p in disp)
    assert any(
        p["id"] == "yoy_compare"
        for p in match_playbooks(
            "compare distributor sales in July 2025 vs 2026"
        )
    )
    stacked = match_playbooks(
        "show me all distributors with sales more than 10 MT but less than "
        "5 % growth last 6 months vs the same 6 months last year"
    )
    ids = {p["id"] for p in stacked}
    assert "yoy_compare" in ids
    assert "compound_metric_rank" in ids
    assert "distributors_grown" not in ids
    block = playbook_prompt_block(
        "lowest rate for pepsi and multiply by 24.7"
    )
    assert "PLAYBOOK" in block


def test_ground_ask_injects_lexicon(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EVA_DATA_DIR", str(tmp_path / "data"))
    from eva_dashboard.db import connect, init_db
    import eva_dashboard.sales_query as sq

    sq._CLIENTS_CACHE = None
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, "
            "payload_json, updated_at) VALUES "
            "('1', 'PEPSI-COLA INTERNATIONAL (PRIVATE) LIMIT', "
            "'Direct Customers', 'Lahore', 'Lahore', '', '{}', datetime('now'))"
        )
        conn.commit()
    remember_party_alias("pepsi", "PEPSI")
    out = ground_ask_for_agent("lowest rate for Pepsi")
    assert "GROUNDED_PARTIES" in out["prompt_block"] or "PERSONAL_LEXICON" in out[
        "prompt_block"
    ]
    assert out["party_hits"] or out["expansions"]


def test_golden_magic_routing_smoke() -> None:
    """Eval set: router + playbook coverage for the user's magic examples."""
    cases = [
        (
            "Show me volume and AMS by Business Unit for the last 6 months.",
            "standard",
            None,
        ),
        (
            "What was the lowest rate Pepsi was sold at, and who was the buyer?",
            "discovery",
            "lowest_rate_then_buyer",
        ),
        (
            "Take the Pepsi rate, multiply it by 24.7 and divide by 6.",
            {"math", "mixed"},
            "rate_then_math",
        ),
        (
            "Which distributors have grown sales?",
            "standard",
            "distributors_grown",
        ),
        (
            "Which distributors have been sold at different prices on the same date?",
            "discovery",
            "same_date_price_variance",
        ),
        ("what's the pepsi price", "clarify", None),
    ]
    for text, expect_kind, playbook_id in cases:
        route = route_ask(text)
        if isinstance(expect_kind, set):
            assert route["kind"] in expect_kind, text
        else:
            assert route["kind"] == expect_kind, (text, route["kind"])
        if playbook_id:
            ids = {p["id"] for p in match_playbooks(text)}
            assert playbook_id in ids, text
