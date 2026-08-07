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


def test_compose_tables_plus_gpt_analysis() -> None:
    from eva_dashboard.chatbot import (
        _compose_tables_plus_analysis,
        _strip_analysis_section,
    )

    tool_md = (
        "Sales for Jul 2026 · city **Lahore** (MT).\n\n"
        "### Business Unit × Client Type\n"
        '<table class="eva-mtx"><tr><td>42</td></tr></table>\n\n'
        "### Analysis\n"
        "- Canned insight from the tool.\n"
    )
    tables = _strip_analysis_section(tool_md)
    assert "eva-mtx" in tables
    assert "### Analysis" not in tables
    assert "Canned" not in tables

    composed = _compose_tables_plus_analysis(
        tool_md,
        "### Analysis\n- Lahore is led by distributors at 42 MT.\n- Watch AMS vs July volume.\n",
    )
    assert '<table class="eva-mtx">' in composed
    assert "### Analysis" in composed
    assert "Lahore is led by distributors" in composed
    assert "Canned insight" not in composed

    fallback = _compose_tables_plus_analysis(tool_md, "Thanks!")
    assert "Canned insight from the tool." in fallback


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
            assert "DATA MODEL" in text
            # v0.4.1: static rules stay short; live/glossary/catalog may be large
            static_marker = "SPEED & TOOL RULES"
            start = text.find(static_marker)
            end = text.find("=== PRODUCT LANGUAGE")
            assert start >= 0 and end > start
            assert (end - start) < 4500, "static prompt rules grew too large"
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


def test_export_chat_training_csv_pairs_turns() -> None:
    from eva_dashboard.chatbot import FOLLOWUP_MARKER, export_chat_training_csv

    messages = [
        {"role": "user", "content": "Show me Lahore sales"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "query_sales", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "1",
            "name": "query_sales",
            "content": '{"ok": true}',
        },
        {
            "role": "assistant",
            "content": '<table class="eva-mtx"><tr><td>42</td></tr></table>\n### Analysis\n- Strong.',
            "_eva_followup": {
                "table_spec": {
                    "filters": {"city": "Lahore"},
                    "column_dimension": "month",
                    "months_back": 6,
                }
            },
        },
        {
            "role": "user",
            "content": f"{FOLLOWUP_MARKER}\n\ngroup by city",
        },
        {
            "role": "assistant",
            "content": "City breakdown table",
            "_eva_followup": {
                "table_spec": {
                    "filters": {"city": None},
                    "column_dimension": "month",
                    "row_dimension": "city",
                }
            },
        },
    ]
    csv_text = export_chat_training_csv(messages, model="gpt-4o-mini")
    assert "user_question" in csv_text
    assert "comment" in csv_text
    assert "rating_1_to_5" in csv_text
    assert "expected_answer_notes" in csv_text
    assert "suggested_tool" in csv_text
    assert "forced_tool_hint" in csv_text
    assert "Show me Lahore sales" in csv_text
    assert "group by city" in csv_text
    assert "query_sales" in csv_text
    assert "yes" in csv_text  # follow-up flag
    assert "42" in csv_text
    assert "<table" not in csv_text.split("\n")[2] or "assistant_answer_plain" in csv_text
    # plain column should strip tags
    assert "Strong." in csv_text
