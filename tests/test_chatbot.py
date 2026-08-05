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


def test_run_sql_readonly_on_temp_db() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")
        try:
            init_db()
            result = run_sql("SELECT name FROM sqlite_master WHERE type='table'")
            assert result["ok"] is True
            assert result["row_count"] >= 1
            overview = sales_overview()
            assert overview["sales_rows"] == 0
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
