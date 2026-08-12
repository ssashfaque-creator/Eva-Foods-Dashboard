"""Phase 2: MemoryContext, state_action, golden RAG, self-correction feedback."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from eva_dashboard.agent_loop import build_correction_feedback
from eva_dashboard.golden_rag import (
    clear_golden_cache,
    format_goldens_for_prompt,
    retrieve_golden_queries,
)
from eva_dashboard.memory_context import (
    MemoryContext,
    merge_memory_into_spec,
    resolve_state_action,
)
from eva_dashboard.query_executor import execute_query_spec
from eva_dashboard.query_spec import normalize_query_spec, prior_context_for_prompt
from eva_dashboard.db import connect, init_db


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
        conn.execute(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, "
            "payload_json, updated_at) "
            "VALUES ('1', 'Alpha Dist', 'Eva Distributors', 'Lahore', 'Lahore', "
            "'', '{}', datetime('now'))"
        )
        conn.execute(
            """
            INSERT INTO sales (
              source_file_id, row_hash, imported_at, date, party, product,
              qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type, payload_json
            ) VALUES (NULL, 'mc-1', datetime('now'), '2026-07-05', 'Alpha Dist', 'P1',
                      10, 'MT', 10, 100, 1000, 'Eva Distributors', '{}')
            """
        )
        conn.commit()


def test_normalize_state_action_maps_to_base():
    n = normalize_query_spec(
        {
            "state_action": "keep",
            "row_dimensions": ["party"],
            "metrics": ["avg_price"],
            "period_type": "MTD",
        }
    )
    assert n["state_action"] == "keep"
    assert n["base"] == "prior"
    assert n["_state_action_explicit"] is True

    n2 = normalize_query_spec(
        {
            "state_action": "clear",
            "row_dimensions": ["business_unit"],
            "metrics": ["volume"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 6,
        }
    )
    assert n2["state_action"] == "clear"
    assert n2["base"] == "none"


def test_memory_context_prompt_is_strict_json():
    mem = MemoryContext(
        filters={"city": "Lahore", "party": "Alpha Dist"},
        party_scope={"party": "Alpha Dist"},
        row_dimensions=["party"],
        metrics=["volume", "ams"],
        source="query_state",
    )
    block = mem.to_prompt_block()
    assert "MEMORY_CONTEXT" in block
    assert "state_action" in block
    assert "Lahore" in block
    assert "<table" not in block.lower()
    assert "html" not in block.lower()


def test_merge_memory_clear_ignores_prior():
    mem = MemoryContext(
        filters={"city": "Lahore", "party": "Alpha Dist"},
        party_scope={"party": "Alpha Dist"},
    )
    spec = {
        "state_action": "clear",
        "row_dimensions": ["business_unit"],
        "metrics": ["volume"],
        "period_type": "LAST_N_MONTHS",
        "months_back": 6,
        "filters": {},
        "base": "none",
        "clear": [],
    }
    out = merge_memory_into_spec(spec, mem)
    assert out["base"] == "none"
    assert out["state_action"] == "clear"
    assert not (out.get("filters") or {}).get("city")
    assert not (out.get("filters") or {}).get("party")


def test_merge_memory_keep_restores_party():
    mem = MemoryContext(
        filters={"party": "Alpha Dist", "city": "Lahore"},
        party_scope={"party": "Alpha Dist"},
        period={"date_from": "2026-07-01", "date_to": "2026-07-31"},
        period_phrase="July 2026",
    )
    spec = normalize_query_spec(
        {
            "state_action": "keep",
            "row_dimensions": ["product"],
            "metrics": ["avg_price"],
            "period_type": "SPECIFIC_MONTH",
            "target_month": "2026-07",
            "clear_filters": [],
            "filters": {},
        }
    )
    out = merge_memory_into_spec(spec, mem)
    assert out["base"] == "prior"
    assert out["filters"].get("party") == "Alpha Dist"
    assert out["filters"].get("city") == "Lahore"


def test_explicit_clear_does_not_soft_stick_party():
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        _seed()
        prior = {
            "filters": {"party": "Alpha Dist"},
            "party_scope": {"party": "Alpha Dist"},
            "period": {
                "date_from": "2026-07-01",
                "date_to": "2026-07-31",
                "label": "July 2026",
            },
        }
        # Fresh ask with explicit clear — must NOT revive Alpha Dist
        result = execute_query_spec(
            {
                "state_action": "clear",
                "context_handling": "none",
                "row_dimensions": ["business_unit"],
                "column_dimensions": ["month"],
                "metrics": ["volume", "ams"],
                "period_type": "LAST_N_MONTHS",
                "months_back": 6,
                "filters": {},
            },
            prior=prior,
            user_text="show me sales",
        )
        assert result.get("ok") is True
        qs = result.get("query_spec") or {}
        filters = qs.get("filters") or result.get("filters") or {}
        assert filters.get("party") in (None, "", [])


def test_legacy_price_followup_still_sticks_without_state_action():
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        _seed()
        prior = {
            "filters": {"party": "Alpha Dist"},
            "party_scope": {"party": "Alpha Dist"},
            "period": {
                "date_from": "2026-07-01",
                "date_to": "2026-07-31",
                "label": "July 2026",
            },
        }
        result = execute_query_spec(
            {
                # No state_action — legacy soft stick must keep party
                "context_handling": "none",
                "row_dimensions": ["product"],
                "metrics": [],
                "period_type": "SPECIFIC_MONTH",
                "target_month": "2026-07",
            },
            prior=prior,
            user_text="what's the price?",
        )
        assert result.get("ok") is True
        qs = result.get("query_spec") or {}
        filters = qs.get("filters") or result.get("filters") or {}
        assert filters.get("party") == "Alpha Dist"


def test_correction_feedback_on_validation_failure():
    fb = build_correction_feedback(
        {
            "ok": False,
            "error": "Empty result — revise the QuerySpec.",
            "plan_errors": [
                "Query returned no rows for city=Atlantis.",
            ],
            "query_spec": {
                "filters": {"city": "Atlantis"},
                "row_dimensions": ["business_unit"],
                "metrics": ["volume"],
            },
        },
        attempt=1,
        max_attempts=3,
    )
    assert fb["kind"] in {"validation_error", "execution_error", "empty_result"}
    assert fb["show_to_user"] is False
    assert fb["suggested_fixes"]
    assert "failed_query_spec" in fb


def test_execute_attaches_feedback_ok():
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        _seed()
        result = execute_query_spec(
            {
                "state_action": "clear",
                "row_dimensions": ["party"],
                "metrics": ["volume", "ams"],
                "period_type": "SPECIFIC_MONTH",
                "target_month": "2026-07",
                "filters": {"party": "Alpha Dist"},
            },
            user_text="Alpha Dist sales in July",
        )
        assert result.get("ok") is True
        assert "feedback" in result
        assert result["feedback"]["kind"] == "ok"


def test_golden_rag_retrieves_channel_sku():
    clear_golden_cache()
    hits = retrieve_golden_queries(
        "last price for all SKUs across all channels",
        k=3,
    )
    assert hits
    assert any(
        "client_type" in str((h.get("plan") or {}).get("row_dimensions"))
        for h in hits
    )
    block = format_goldens_for_prompt("remove al shaheer from this data")
    assert "GOLDEN_QUERY_EXAMPLES" in block
    assert "QuerySpec" in block or "state_action" in block


def test_prior_context_for_prompt_uses_memory_block():
    text = prior_context_for_prompt(
        {"filters": {"city": "Lahore"}, "row_dimensions": ["party"]}
    )
    assert "MEMORY_CONTEXT" in text
    assert "Lahore" in text


def test_resolve_state_action_legacy():
    assert resolve_state_action({"base": "prior", "filters": {}}) == "keep"
    assert (
        resolve_state_action(
            {"base": "prior", "clear": ["city"], "filters": {}}
        )
        == "modify"
    )
    assert resolve_state_action({"base": "none"}) == "clear"


def test_version_is_at_least_1_3():
    from eva_dashboard import __version__
    from eva_dashboard.update import _version_tuple

    assert _version_tuple(__version__) >= _version_tuple("1.3.0")
