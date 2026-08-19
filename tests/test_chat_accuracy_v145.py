"""v1.4.5 chat accuracy: ReAct briefing, profile vs who-is, sales routing."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    _extract_profile_subject,
    _looks_explicit_party_profile,
    _looks_party_lookup,
    _looks_who_is_with_analytics,
    chat_completion,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.playbooks import playbook_ids
from eva_dashboard.react_briefing import (
    react_commercial_briefing,
    react_queryspec_contract,
)
from eva_dashboard.tools.intent_router import route_ask, tool_allowed


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed() -> None:
    init_db()
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, '{}', datetime('now'))",
            [
                ("P1", "Eva Consumer", "Eva Canola", "Stand up"),
                ("P2", "Eva Consumer", "Eva Cooking", "Tin"),
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES ('1', 'Alpha Dist', 'Eva Distributors', 'Lahore', 'Lahore', '', "
            "'{}', datetime('now'))"
        )
        rows = [
            ("2026-04-05", "P1", 10, 100),
            ("2026-05-05", "P1", 12, 105),
            ("2026-06-05", "P1", 14, 110),
            ("2026-07-01", "P1", 18, 115),
            ("2026-07-10", "P2", 6, 90),
            ("2026-07-20", "P1", 4, 120),
        ]
        for i, (dt, prod, mt, rate) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, 'Alpha Dist', ?,
                          ?, 'MT', ?, ?, ?, 'Eva Distributors', '{}')
                """,
                (f"acc-{i}", dt, prod, mt, mt, rate, mt * rate),
            )
        conn.commit()


def test_who_is_not_stolen_by_profile_or_combined_sales() -> None:
    assert _looks_party_lookup("Who is Al Bari?")
    assert _looks_party_lookup("who's pepsi")
    assert not _looks_party_lookup("tell me about Alpha Dist")
    assert not _looks_party_lookup("tell me about Alpha Dist in July")
    assert not _looks_party_lookup("who is pepsi and show sales")
    assert not _looks_party_lookup("who is Al Bari and AMS")
    assert _extract_profile_subject("tell me about Alpha Dist in July") == "Alpha Dist"
    assert _extract_profile_subject("tell me about Alpha Dist") == "Alpha Dist"
    assert _looks_who_is_with_analytics("who is pepsi and show sales")
    assert _looks_explicit_party_profile("tell me about Alpha Dist in July")
    assert not _looks_explicit_party_profile(
        "How are Eva Consumer sales doing in Lahore this month?"
    )


def test_router_sales_and_profile_prefer_pivot() -> None:
    sales = route_ask("Show me sales")
    assert sales["kind"] == "standard"
    assert "run_standard_analytics_pivot" in sales["preferred_tools"]
    ok, _ = tool_allowed("execute_read_only_sql", sales)
    assert ok is False

    profile = route_ask("tell me about Alpha Dist in July")
    assert profile["kind"] == "standard"
    assert "run_standard_analytics_pivot" in profile["preferred_tools"]
    ok, _ = tool_allowed("execute_read_only_sql", profile)
    assert ok is False
    assert "party_profile" in playbook_ids("tell me about Alpha Dist in July")

    who = route_ask("who is pepsi and show sales")
    assert who["kind"] in {"standard", "mixed"}
    assert "who_is_then_sales" in playbook_ids("who is pepsi and show sales")


def test_react_briefing_teaches_live_db_and_queryspec(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EVA_DATA_DIR", str(tmp_path / "data"))
    init_db()
    contract = react_queryspec_contract()
    assert "party_profile" in contract
    assert "Eva Consumer" in contract
    assert "city_filter" in contract
    brief = react_commercial_briefing()
    assert "LIVE DATABASE STATE" in brief
    assert "QUERY SPEC" in brief
    assert "VOCABULARY" in brief
    assert "packing_category" in brief
    assert "run_standard_analytics_pivot" in brief
    assert "compare='yoy'" in contract or "compare=yoy" in contract
    assert "metric='yoy'" in contract or "metric=yoy" in contract
    assert "metric='pop'" in contract or "compare='prior'" in contract or "compare=prior" in contract
    assert "10 MT AMS" in contract
    assert "empty spec_dict" in contract
    assert "Flattened" in contract


def test_profile_fast_path_returns_card_not_whois() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            md, msgs = chat_completion(
                [{"role": "user", "content": "tell me about Alpha Dist in July"}],
                api_key="sk-test-not-used",
            )
            assert "Customer profile" in md
            assert "Alpha Dist" in md
            assert "% vs AMS" in md or "vs AMS" in md
            assert "Last purchase" in md
            blob = " ".join(str(m) for m in msgs)
            assert "party_profile_fast_path" in blob
            assert "whois_fast_path" not in blob
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_pure_who_is_still_fast_path() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            md, msgs = chat_completion(
                [{"role": "user", "content": "Who is Alpha Dist?"}],
                api_key="sk-test-not-used",
            )
            assert "Alpha Dist" in md
            blob = " ".join(str(m) for m in msgs)
            assert "whois_fast_path" in blob
            assert "Customer profile" not in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
