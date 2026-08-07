"""Tests for chatbot SQL safety and helpers."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from eva_dashboard.chatbot import (
    _validate_select,
    load_data_catalog,
    run_sql,
    sales_overview,
)
from eva_dashboard.db import init_db


def test_catalog_file_loads() -> None:
    text = load_data_catalog()
    assert "sales" in text.lower()
    assert "Price Fetch" in text or "price fetch" in text.lower()


def test_validate_select_blocks_writes() -> None:
    with pytest.raises(ValueError):
        _validate_select("DELETE FROM sales")
    with pytest.raises(ValueError):
        _validate_select("SELECT 1; DROP TABLE sales")
    with pytest.raises(ValueError):
        _validate_select("ATTACH DATABASE 'x' AS evil")
    ok = _validate_select("SELECT count(*) FROM sales")
    assert ok.lower().startswith("select")


def test_looks_factual_detects_data_questions() -> None:
    from eva_dashboard.chatbot import _looks_factual

    assert _looks_factual("How much were Eva consumer sales in Lahore for July")
    assert _looks_factual("Price Fetch for 2026-06-30")
    assert not _looks_factual("Thanks!")


def test_system_prompt_includes_live_briefing() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")
        try:
            init_db()
            from eva_dashboard.chatbot import system_prompt

            text = system_prompt()
            assert "LIVE DATABASE STATE" in text
            assert "query_sales" in text
            assert "knowledge cutoff" in text.lower() or "OpenAI knowledge cutoff" in text
            assert "SPEED" in text or "TOOL RULES" in text
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_followup_and_include_routing_helpers() -> None:
    from eva_dashboard.chatbot import (
        FOLLOWUP_MARKER,
        _companion_business_units,
        _is_explicit_followup,
        _looks_combine_tables,
        _looks_include_check,
        _looks_table_followup,
        _resolve_include_segment,
        _api_history_message,
    )

    assert _looks_include_check("Does this include bulk?")
    assert _looks_include_check("was Eva Bulk included in this?")
    assert not _looks_include_check("include bulk")  # combine, not check

    assert _looks_combine_tables("combine the tables")
    assert _looks_combine_tables("add bulk sales")
    assert _looks_combine_tables("include bulk")
    assert _looks_table_followup("combine the tables")

    prior = {
        "business_units": ["Eva Consumer"],
        "filters": {"business_unit": "Eva Consumer", "city": "Karachi"},
    }
    assert _companion_business_units("does this include bulk?", prior) == ["Eva Bulk"]
    assert _resolve_include_segment("does this include bulk?", prior) == "Eva Bulk"

    marked = f"{FOLLOWUP_MARKER}\n\ndoes this include bulk?"
    assert _is_explicit_followup(marked)
    assert _looks_include_check(marked)

    raw = {
        "role": "assistant",
        "content": "hello",
        "_eva_followup": {"table_spec": {"x": 1}},
        "tool_calls": [{"id": "1"}],
    }
    clean = _api_history_message(raw)
    assert "_eva_followup" not in clean
    assert clean["content"] == "hello"
    assert clean["tool_calls"]
