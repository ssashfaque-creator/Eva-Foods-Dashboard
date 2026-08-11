"""Phase 4 agent loop — verify, clarify, multi-hop / mixed-compare hints."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.agent_loop import (
    apply_verification,
    build_mixed_compare_subplans,
    looks_mixed_party_channel_compare,
    looks_multi_hop,
    party_matches_look_like_branches,
    should_clarify_party,
    verify_query_result,
)
from eva_dashboard.chatbot import system_prompt
from eva_dashboard.db import connect, init_db
from eva_dashboard.query_executor import execute_query_spec


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
        for cid, name, city in (
            ("1", "Alpha Dist", "Lahore"),
            ("2", "Alpha Dist North", "Islamabad"),
            ("3", "Beta Foods", "Karachi"),
            ("4", "Gamma Traders", "Lahore"),
            ("5", "Delta Mart", "Lahore"),
        ):
            conn.execute(
                "INSERT OR REPLACE INTO clients "
                "(client_id, client, type, city_filter, city, inactive, "
                "payload_json, updated_at) "
                "VALUES (?, ?, 'Eva Distributors', ?, ?, '', '{}', datetime('now'))",
                (cid, name, city, city),
            )
        conn.execute(
            """
            INSERT INTO sales (
              source_file_id, row_hash, imported_at, date, party, product,
              qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type, payload_json
            ) VALUES (NULL, 'al-1', datetime('now'), '2026-07-05', 'Alpha Dist', 'P1',
                      10, 'MT', 10, 100, 1000, 'Eva Distributors', '{}')
            """
        )
        conn.commit()


def test_branch_vs_divergent_party_clarify() -> None:
    assert party_matches_look_like_branches(
        "Alpha Dist",
        ["Alpha Dist", "Alpha Dist North"],
    )
    assert party_matches_look_like_branches(
        "al shaheer",
        ["Al Shaheer Lahore", "Al Shaheer Karachi"],
    )
    # Divergent company stems must NOT look like one branch family
    assert not party_matches_look_like_branches(
        "alpha",
        ["Alpha Dist", "Alpha Foods Pvt"],
    )
    assert not should_clarify_party(
        query="Alpha Dist",
        matches=["Alpha Dist", "Alpha Dist North"],
        operation="pivot",
        empty_result=False,
    )
    assert should_clarify_party(
        query="zz",
        matches=["Beta Foods", "Gamma Traders", "Delta Mart"],
        operation="party_profile",
        empty_result=False,
    )


def test_multi_hop_and_mixed_compare_detectors() -> None:
    assert looks_multi_hop("show Lahore sales and then product wise")
    assert looks_mixed_party_channel_compare("compare al shaheer with Imtiaz")
    assert not looks_mixed_party_channel_compare("compare Lahore vs Karachi")


def test_verify_empty_restrictive_triggers_retry() -> None:
    result = {
        "ok": True,
        "mode": "matrix",
        "matrix": {"rows": []},
        "answer_markdown": "No pivot data.\n",
        "query_spec": {
            "operation": "pivot",
            "filters": {"party": "Nobody Corp", "city": "Lahore"},
            "period": {"label": "July 2026"},
            "period_type": "SPECIFIC_MONTH",
        },
    }
    v = verify_query_result(result, user_text="Nobody Corp sales in Lahore July")
    assert v["empty"] is True
    assert v["retry_errors"]
    applied = apply_verification(result, user_text="Nobody Corp sales in Lahore July")
    assert applied.get("ok") is False
    assert applied.get("plan_errors")


def test_verify_investigation_hint_on_mixed_compare() -> None:
    result = {
        "ok": True,
        "mode": "matrix",
        "matrix": {
            "rows": [
                {"party": "Alpha Dist", "total": 10, "is_total": False},
            ]
        },
        "answer_markdown": "| Party | Total |\n| Alpha Dist | 10 |\n",
        "query_spec": {
            "operation": "pivot",
            "filters": {"party": "Alpha Dist"},
            "metrics": ["volume", "ams"],
        },
    }
    applied = apply_verification(
        result,
        user_text="compare Alpha Dist with Imtiaz last 6 months",
    )
    assert applied.get("ok") is True
    assert "INVESTIGATION" in (applied.get("response_instructions") or "")
    assert "TWO" in (applied.get("response_instructions") or "") or "twice" in (
        applied.get("response_instructions") or ""
    ).lower()


def test_mixed_compare_subplans() -> None:
    plans = build_mixed_compare_subplans(
        user_text="compare Alpha Dist vs Imtiaz in Lahore",
        base_spec={
            "metrics": ["volume", "ams"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 6,
            "filters": {"city": "Lahore"},
        },
    )
    assert len(plans) >= 2
    assert any(
        (p.get("extracted_entities") or p.get("filters", {}).get("party"))
        for p in plans
    )
    assert any(
        (p.get("filters") or {}).get("client_type") == "Imtiaz Store" for p in plans
    )


def test_empty_unknown_party_returns_plan_errors() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "row_dimensions": ["business_unit"],
                    "metrics": ["volume", "ams"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "filters": {"party": "Completely Unknown Party XYZ"},
                },
                user_text="Completely Unknown Party XYZ sales in July",
            )
            # Empty restrictive → retry for the model
            assert out.get("ok") is False
            assert out.get("plan_errors")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_prompt_teaches_investigation_loop() -> None:
    text = system_prompt()
    assert "INVESTIGATION" in text
