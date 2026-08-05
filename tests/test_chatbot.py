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
            assert "ANTI-HALLUCINATION" in text
            assert "knowledge cutoff" in text.lower()
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
