"""Phase 2: party_profile operation + query_state multi-turn continuity."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.advanced_analytics import party_profile
from eva_dashboard.chatbot import system_prompt
from eva_dashboard.db import connect, init_db
from eva_dashboard.query_executor import (
    _looks_party_profile_ask,
    execute_query_spec,
)
from eva_dashboard.query_spec import (
    build_query_state,
    prior_context_from_query_state,
    prior_context_payload,
)


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
                (f"p2-{i}", dt, prod, mt, mt, rate, mt * rate),
            )
        conn.commit()


def test_profile_ask_detector() -> None:
    assert _looks_party_profile_ask("tell me about Alpha Dist")
    assert _looks_party_profile_ask("customer profile for al shaheer")
    assert _looks_party_profile_ask("give me a rundown on Alpha Dist")
    assert _looks_party_profile_ask("how are they doing")
    assert _looks_party_profile_ask("last purchase date?")
    assert _looks_party_profile_ask("give me the full picture for Al Shaheer")
    assert not _looks_party_profile_ask("Show me Alpha Dist sales in July")


def test_party_profile_card_fields() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = party_profile(
                party="Alpha Dist",
                period="July",
                months_back=6,
            )
            assert out.get("ok") is True, out
            assert out.get("mode") == "party_profile"
            assert out.get("party") == "Alpha Dist"
            assert out.get("last_sale") == "2026-07-20"
            assert out.get("days_since") is not None
            assert (out.get("volume_mt") or 0) > 0
            assert out.get("ams_mt") is not None
            assert out.get("pct_vs_ams") is not None
            assert out.get("avg_rate") is not None
            assert out.get("top_skus")
            md = out.get("answer_markdown") or ""
            assert "Customer profile" in md
            assert "Last purchase" in md
            assert "% vs AMS" in md
            assert (out.get("party_spec") or {}).get("filters", {}).get(
                "party"
            ) == "Alpha Dist"
            assert (out.get("table_spec") or {}).get("filters", {}).get(
                "party"
            ) == "Alpha Dist"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_plan_query_party_profile_operation() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "operation": "party_profile",
                    "party_query": "Alpha Dist",
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "row_dimensions": ["party"],
                    "metrics": ["volume", "ams", "vs_ams"],
                },
                user_text="tell me about Alpha Dist in July",
            )
            assert out.get("ok") is True, out
            assert out.get("mode") == "party_profile"
            assert out.get("query_state")
            assert (out["query_state"].get("party_scope") or {}).get(
                "party"
            ) == "Alpha Dist"
            assert out.get("last_sale")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_soft_promote_profile_ask() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "row_dimensions": ["party"],
                    "metrics": ["volume", "ams"],
                    "period_type": "MTD",
                    "context_handling": "none",
                    "extracted_entities": ["Alpha Dist"],
                },
                user_text="tell me about Alpha Dist",
            )
            assert out.get("ok") is True, out
            assert out.get("mode") == "party_profile"
            qs = out.get("query_spec") or {}
            assert qs.get("operation") == "party_profile"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_query_state_prior_keeps_party_on_price_followup() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            profile = execute_query_spec(
                {
                    "operation": "party_profile",
                    "filters": {"party": "Alpha Dist"},
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "row_dimensions": ["party"],
                    "metrics": ["volume", "vs_ams"],
                },
                user_text="tell me about Alpha Dist",
            )
            state = profile.get("query_state")
            assert state
            prior = prior_context_from_query_state(state)
            assert prior is not None
            assert (prior.get("party_scope") or {}).get("party") == "Alpha Dist"

            follow = execute_query_spec(
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
            assert follow.get("ok") is True, follow
            filters = (follow.get("query_spec") or {}).get("filters") or {}
            assert filters.get("party") == "Alpha Dist"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_build_query_state_from_specs() -> None:
    state = build_query_state(
        table_spec={
            "filters": {"party": "Alpha Dist", "city": "Lahore"},
            "period_phrase": "July",
            "row_dimensions": ["party"],
            "metrics": ["volume", "ams"],
        },
        result_mode="party_profile",
    )
    assert state is not None
    assert state["party_scope"]["party"] == "Alpha Dist"
    assert state["operation"] == "party_profile"
    # Compat with prior_context_payload party_scope
    prior = prior_context_payload(
        table_spec={"filters": {"party": "Alpha Dist"}}
    )
    assert prior and prior.get("party_scope", {}).get("party") == "Alpha Dist"


def test_prompt_teaches_party_profile() -> None:
    text = system_prompt()
    assert "party_profile" in text
