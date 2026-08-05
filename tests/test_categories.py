"""Tests for category file parsing and replace-on-upload ingest."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from eva_dashboard.categories import parse_category_file
from eva_dashboard.data import load_category_map
from eva_dashboard.ingest import (
    category_count,
    ingest_categories,
    load_category_map_from_db,
)


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_SALES = ROOT / "data" / "sales.xlsx"


def _with_temp_data(fn):
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")
        try:
            fn()
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def _write_category_xlsx(path: Path, rows: list[tuple[str, str, str]]) -> Path:
    df = pd.DataFrame(rows, columns=["Product", "Category 1", "Category 2"])
    df.to_excel(path, index=False)
    return path


def test_parse_category_file_normalizes_aliases(tmp_path: Path) -> None:
    path = _write_category_xlsx(
        tmp_path / "cats.xlsx",
        [
            ("Eva Oil 1x16Ltr", "Eva Bulk", "Oil"),
            ("Maan Oil 1x16Ltr", "Maan Consum", "Oil"),
            ("  ", "ignore", "me"),
        ],
    )
    parsed = parse_category_file(path)
    assert list(parsed["product"]) == ["Eva Oil 1x16Ltr", "Maan Oil 1x16Ltr"]
    assert list(parsed["category1"]) == ["Eva Bulk", "Maan Consumer"]
    assert list(parsed["category2"]) == ["Oil", "Oil"]


def test_parse_category_file_requires_columns(tmp_path: Path) -> None:
    path = tmp_path / "bad.xlsx"
    pd.DataFrame({"Product": ["A"], "Category": ["B"]}).to_excel(path, index=False)
    with pytest.raises(ValueError, match="Category 1"):
        parse_category_file(path)


def test_ingest_categories_replaces_previous(tmp_path: Path) -> None:
    first = _write_category_xlsx(
        tmp_path / "c1.xlsx",
        [("Product A", "Eva Bulk", "Oil")],
    )
    second = _write_category_xlsx(
        tmp_path / "c2.xlsx",
        [
            ("Product B", "Eva Consumer", "Ghee"),
            ("Product C", "Maan Bulk", "Oil"),
        ],
    )

    def run() -> None:
        r1 = ingest_categories(first, original_name="c1.xlsx")
        assert r1["replaced"] == 1
        assert category_count() == 1
        assert len(load_category_map_from_db()) == 1

        r2 = ingest_categories(second, original_name="c2.xlsx")
        assert r2["replaced"] == 2
        loaded = load_category_map_from_db()
        assert set(loaded["product"]) == {"Product B", "Product C"}
        assert "Product A" not in set(loaded["product"])
        assert category_count() == 2

    _with_temp_data(run)


def test_load_category_map_from_file(tmp_path: Path) -> None:
    path = _write_category_xlsx(
        tmp_path / "cats.xlsx",
        [("Eva Oil 1x16Ltr", "Eva Bulk", "Oil")],
    )
    cats = load_category_map(path)
    assert cats.loc[0, "category1"] == "Eva Bulk"
    assert cats.loc[0, "category2"] == "Oil"


def test_parse_sample_sales_category_sheet() -> None:
    """If data/sales.xlsx has a Category sheet, ensure parse still works via load_category_map."""
    if not SAMPLE_SALES.exists():
        return
    cats = load_category_map(SAMPLE_SALES)
    assert not cats.empty
    assert {"product", "category1", "category2"} <= set(cats.columns)
