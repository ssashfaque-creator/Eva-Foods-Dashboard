"""Who-is fuzzy lookup + exclude/include identification preview."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.client_language import _score_name, lookup_party
from eva_dashboard.db import connect, init_db
from eva_dashboard.metric_filters import parse_metric_filters
from eva_dashboard.query_executor import execute_query_spec


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed() -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, payload_json, "
            "updated_at) VALUES "
            "('Prod A', 'Eva Consumer', 'Eva Canola', 'Stand up', '{}', "
            "datetime('now'))"
        )
        conn.execute(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, "
            "payload_json, updated_at) VALUES "
            "('1', 'AL SHAHEER CORPORATION LIMITED', 'Eva Distributors', "
            "'Lahore', 'Lahore', '', '{}', datetime('now'))"
        )
        conn.execute(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, "
            "payload_json, updated_at) VALUES "
            "('2', 'Other Trader', 'Direct Customers', "
            "'Lahore', 'Lahore', '', '{}', datetime('now'))"
        )
        for i, (party, mt) in enumerate(
            [
                ("AL SHAHEER CORPORATION LIMITED", 20.0),
                ("Other Trader", 5.0),
            ]
        ):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type,
                  payload_json
                ) VALUES (NULL, ?, datetime('now'), '2026-07-15', ?, 'Prod A',
                  ?, 'MT', ?, 100, ?, 'Eva Distributors', '{}')
                """,
                (f"w-{i}", party, mt, mt, mt * 100),
            )
        conn.commit()


def test_score_token_overlap_al_shaheer() -> None:
    score = _score_name("al shaheer", "AL SHAHEER CORPORATION LIMITED")
    assert score >= 0.80


def test_who_is_fuzzy_not_exact() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            import eva_dashboard.sales_query as sq

            sq._CLIENTS_CACHE = None
            _seed()
            out = lookup_party("al shaheer", limit=5)
            assert out.get("ok")
            assert out.get("mode") == "party_lookup"
            names = [m.get("client") for m in (out.get("matches") or [])]
            assert any("SHAHEER" in str(n).upper() for n in names)
            assert "Client search" in (out.get("answer_markdown") or "")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_execute_who_is_short_circuits() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            import eva_dashboard.sales_query as sq

            sq._CLIENTS_CACHE = None
            _seed()
            out = execute_query_spec(
                {
                    "operation": "pivot",
                    "row_dimensions": ["business_unit"],
                    "metrics": ["volume"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                },
                user_text="who is al shaheer?",
            )
            assert out.get("ok"), out.get("error")
            assert out.get("mode") == "party_lookup" or "Client search" in (
                out.get("answer_markdown") or ""
            )
            assert "SHAHEER" in (out.get("answer_markdown") or "").upper()
            # Must not force a planner retry loop
            assert not out.get("plan_errors")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_exclude_strips_from_this_data_tail() -> None:
    from eva_dashboard.spoken_constraints import extract_exclude_phrases
    from eva_dashboard.chatbot import (
        _looks_short_exclude_followup,
        _plan_from_prior_for_exclude,
    )

    assert extract_exclude_phrases("remove al shaheer from this data") == [
        "al shaheer"
    ]
    assert _looks_short_exclude_followup("remove al shaheer from this data")
    plan = _plan_from_prior_for_exclude(
        {
            "filters": {"city": "Lahore"},
            "business_units": ["Eva Consumer", "Eva Bulk"],
            "row_dimensions": ["business_unit"],
            "column_dimensions": ["month"],
            "metrics": ["volume", "ams"],
            "months_back": 6,
        },
        "remove al shaheer from this data",
    )
    assert plan is not None
    assert (plan.get("excludes") or {}).get("party_like") == ["al shaheer"]
    assert plan.get("base") == "prior"


def test_exclude_shows_identification_preview() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            import eva_dashboard.sales_query as sq

            sq._CLIENTS_CACHE = None
            _seed()
            out = execute_query_spec(
                {
                    "operation": "pivot",
                    "row_dimensions": ["party"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "filters": {"city": "Lahore"},
                    "excludes": {"party_like": ["al shaheer"]},
                },
                user_text="exclude al shaheer from this table",
            )
            assert out.get("ok"), out.get("error")
            md = out.get("answer_markdown") or ""
            assert "Clients identified" in md
            assert "excluded" in md.lower()
            assert "SHAHEER" in md.upper()
            assert out.get("exclude_preview") is True
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_parse_yoy_and_mom_filters() -> None:
    assert parse_metric_filters("yoy less than -20") == [
        {"metric": "yoy", "op": "lt", "value": -20.0}
    ]
    assert parse_metric_filters("mom more than 10%") == [
        {"metric": "mom", "op": "gt", "value": 10.0}
    ]
    assert parse_metric_filters("yoy less than 20%") == [
        {"metric": "yoy", "op": "lt", "value": 20.0}
    ]
