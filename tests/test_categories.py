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


def _write_category_xlsx(
    path: Path,
    rows: list[tuple],
    columns: list[str] | None = None,
) -> Path:
    cols = columns or [
        "Product",
        "Business Unit",
        "Oil Type",
        "Packing Category",
    ]
    df = pd.DataFrame(rows, columns=cols)
    df.to_excel(path, index=False)
    return path


def test_parse_new_taxonomy_headers(tmp_path: Path) -> None:
    path = _write_category_xlsx(
        tmp_path / "cats.xlsx",
        [
            (
                "Eva Canola Oil (StandUpPouch)",
                "Eva Consumer",
                "Eva Canola",
                "Stand up",
            ),
            (
                "Eva VTF Banaspati 16 Kg Tin",
                "Eva Bulk",
                "Eva VTF Bulk",
                "Tin",
            ),
            ("Cuisine King (16 Ltr Tin)", "Cuisine King", "Cuisine King", "Tin"),
            ("  ", "ignore", "me", "x"),
        ],
    )
    parsed = parse_category_file(path)
    assert list(parsed["product"]) == [
        "Eva Canola Oil (StandUpPouch)",
        "Eva VTF Banaspati 16 Kg Tin",
        "Cuisine King (16 Ltr Tin)",
    ]
    assert list(parsed["business_unit"]) == [
        "Eva Consumer",
        "Eva Bulk",
        "Cusine King",
    ]
    assert list(parsed["oil_type"]) == ["Eva Canola", "Eva VTF Bulk", "Cuisine King"]
    assert list(parsed["packing_category"]) == ["Stand up", "Tin", "Tin"]
    # Report aliases still present
    assert list(parsed["category1"]) == list(parsed["business_unit"])
    assert list(parsed["category2"]) == list(parsed["oil_type"])


def test_parse_legacy_category1_category2(tmp_path: Path) -> None:
    path = _write_category_xlsx(
        tmp_path / "legacy.xlsx",
        [
            ("Eva Oil 1x16Ltr", "Eva Bulk", "Oil"),
            ("Maan Oil 1x16Ltr", "Maan Consum", "Oil"),
        ],
        columns=["Product", "Category 1", "Category 2"],
    )
    parsed = parse_category_file(path)
    assert list(parsed["business_unit"]) == ["Eva Bulk", "Maan Consumer"]
    assert list(parsed["oil_type"]) == ["Oil", "Oil"]
    assert list(parsed["packing_category"]) == ["", ""]


def test_parse_category_file_requires_columns(tmp_path: Path) -> None:
    path = tmp_path / "bad.xlsx"
    pd.DataFrame({"Product": ["A"], "Category": ["B"]}).to_excel(path, index=False)
    with pytest.raises(ValueError, match="Business Unit|Category 1"):
        parse_category_file(path)


def test_ingest_categories_replaces_previous(tmp_path: Path) -> None:
    first = _write_category_xlsx(
        tmp_path / "c1.xlsx",
        [("Product A", "Eva Bulk", "Eva Bulk", "Tin")],
    )
    second = _write_category_xlsx(
        tmp_path / "c2.xlsx",
        [
            ("Product B", "Eva Consumer", "Eva Canola", "Stand up"),
            ("Product C", "Maan Bulk", "Maan Bulk", "Bucket"),
        ],
    )

    def run() -> None:
        r1 = ingest_categories(first, original_name="c1.xlsx")
        assert r1["replaced"] == 1
        assert category_count() == 1
        loaded1 = load_category_map_from_db()
        assert loaded1.loc[0, "packing_category"] == "Tin"

        r2 = ingest_categories(second, original_name="c2.xlsx")
        assert r2["replaced"] == 2
        loaded = load_category_map_from_db()
        assert set(loaded["product"]) == {"Product B", "Product C"}
        assert "Product A" not in set(loaded["product"])
        assert category_count() == 2
        row_b = loaded[loaded["product"] == "Product B"].iloc[0]
        assert row_b["business_unit"] == "Eva Consumer"
        assert row_b["oil_type"] == "Eva Canola"
        assert row_b["packing_category"] == "Stand up"

    _with_temp_data(run)


def test_load_category_map_from_file(tmp_path: Path) -> None:
    path = _write_category_xlsx(
        tmp_path / "cats.xlsx",
        [("Eva Oil 1x16Ltr", "Eva Bulk", "Eva Bulk", "Tin")],
    )
    cats = load_category_map(path)
    assert cats.loc[0, "category1"] == "Eva Bulk"
    assert cats.loc[0, "oil_type"] == "Eva Bulk"
    assert cats.loc[0, "packing_category"] == "Tin"


def test_parse_sample_sales_category_sheet() -> None:
    """If data/sales.xlsx has a Category sheet, ensure parse still works via load_category_map."""
    if not SAMPLE_SALES.exists():
        return
    cats = load_category_map(SAMPLE_SALES)
    assert not cats.empty
    assert {"product", "category1", "category2"} <= set(cats.columns)
