"""Parse product category workbooks (Product / Category 1 / Category 2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

# Normalize truncated / alternate Category 1 labels to report names
CATEGORY1_ALIASES = {
    "maan consum": "Maan Consumer",
    "maan consumer": "Maan Consumer",
    "cusine king": "Cusine King",
    "cuisine king": "Cusine King",
}


def _norm_header(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "")


def _find_header_row(raw: pd.DataFrame) -> int:
    for i in range(min(15, len(raw))):
        vals = [_norm_header(v) for v in raw.iloc[i].tolist()]
        if "product" in vals and (
            "category1" in vals or "category 1".replace(" ", "") in vals
        ):
            # "category1" after removing spaces from "Category 1"
            return i
        if "product" in vals and any(v.startswith("category") for v in vals):
            return i
    raise ValueError(
        "Category file must include a header row with Product, Category 1, Category 2"
    )


def _normalize_category1(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none"}:
        return ""
    return CATEGORY1_ALIASES.get(text.lower(), text)


def parse_category_file(path: Path | str) -> pd.DataFrame:
    """Load a category Excel/CSV: columns Product, Category 1, Category 2."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {".csv", ".txt"}:
        raw = pd.read_csv(path, header=None)
    else:
        # Try first sheet; header may not be row 0
        raw = pd.read_excel(path, sheet_name=0, header=None, engine="openpyxl")

    raw = raw.dropna(how="all")
    if raw.empty:
        raise ValueError("Category file is empty")

    header_row = _find_header_row(raw)
    headers = [
        str(v).strip() if v is not None and not (isinstance(v, float) and pd.isna(v)) else f"col_{i}"
        for i, v in enumerate(raw.iloc[header_row].tolist())
    ]
    body = raw.iloc[header_row + 1 :].copy()
    body.columns = headers
    body = body.dropna(how="all")

    col_map = {_norm_header(c): c for c in body.columns}
    product_col = col_map.get("product")
    c1_col = col_map.get("category1")
    c2_col = col_map.get("category2")
    if product_col is None or c1_col is None or c2_col is None:
        raise ValueError(
            "Category file must have columns: Product, Category 1, Category 2"
        )

    frame = pd.DataFrame(
        {
            "product": body[product_col].astype(str).str.strip(),
            "category1": body[c1_col].map(_normalize_category1),
            "category2": body[c2_col].astype(str).str.strip(),
        }
    )
    frame = frame[frame["product"].ne("") & frame["product"].str.lower().ne("nan")]
    frame.loc[frame["category2"].str.lower().isin({"nan", "none"}), "category2"] = ""
    frame = frame.dropna(subset=["product"])
    duplicates = frame["product"][frame["product"].duplicated()].tolist()
    if duplicates:
        raise ValueError(
            "Duplicate products in category file: " + ", ".join(duplicates[:20])
        )
    return frame.reset_index(drop=True)
