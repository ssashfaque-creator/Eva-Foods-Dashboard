"""Parse product category workbooks (Business Unit / Oil Type / Packing)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

# Normalize truncated / alternate Business Unit labels to report names
BUSINESS_UNIT_ALIASES = {
    "maan consum": "Maan Consumer",
    "maan consumer": "Maan Consumer",
    # Reports / PDF historically use Excel spelling "Cusine King"
    "cusine king": "Cusine King",
    "cuisine king": "Cusine King",
}

# Legacy header aliases → canonical internal keys
_HEADER_ALIASES = {
    "product": "product",
    "businessunit": "business_unit",
    "businessunits": "business_unit",
    "category1": "business_unit",  # legacy
    "oiltype": "oil_type",
    "oiltypes": "oil_type",
    "category2": "oil_type",  # legacy
    "packingcategory": "packing_category",
    "packing": "packing_category",
    "packcategory": "packing_category",
    "packtype": "packing_category",
    "category3": "packing_category",  # optional legacy
}


def _norm_header(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("_", "")


def _canonical_header(value: Any) -> str | None:
    return _HEADER_ALIASES.get(_norm_header(value))


def _find_header_row(raw: pd.DataFrame) -> int:
    for i in range(min(20, len(raw))):
        vals = [_norm_header(v) for v in raw.iloc[i].tolist()]
        canonical = {_HEADER_ALIASES.get(v) for v in vals}
        has_product = "product" in canonical
        has_bu = "business_unit" in canonical
        has_oil = "oil_type" in canonical
        if has_product and has_bu and has_oil:
            return i
        # Legacy: Product + Category 1 + Category 2
        if "product" in vals and "category1" in vals and "category2" in vals:
            return i
    raise ValueError(
        "Category file must include a header row with "
        "Product, Business Unit, Oil Type, Packing Category "
        "(legacy: Product, Category 1, Category 2)"
    )


def _normalize_business_unit(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none"}:
        return ""
    return BUSINESS_UNIT_ALIASES.get(text.lower(), text)


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none"}:
        return ""
    return text


def parse_category_file(path: Path | str) -> pd.DataFrame:
    """Load a category Excel/CSV.

    Preferred columns:
      Product | Business Unit | Oil Type | Packing Category

    Legacy still accepted:
      Product | Category 1 | Category 2
      (mapped to Business Unit / Oil Type; Packing Category blank)
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {".csv", ".txt"}:
        raw = pd.read_csv(path, header=None)
    else:
        raw = pd.read_excel(path, sheet_name=0, header=None, engine="openpyxl")

    raw = raw.dropna(how="all")
    if raw.empty:
        raise ValueError("Category file is empty")

    header_row = _find_header_row(raw)
    headers = [
        str(v).strip()
        if v is not None and not (isinstance(v, float) and pd.isna(v))
        else f"col_{i}"
        for i, v in enumerate(raw.iloc[header_row].tolist())
    ]
    body = raw.iloc[header_row + 1 :].copy()
    body.columns = headers
    body = body.dropna(how="all")

    resolved: dict[str, str] = {}
    for col in body.columns:
        key = _canonical_header(col)
        if key and key not in resolved:
            resolved[key] = col

    product_col = resolved.get("product")
    bu_col = resolved.get("business_unit")
    oil_col = resolved.get("oil_type")
    pack_col = resolved.get("packing_category")

    if product_col is None or bu_col is None or oil_col is None:
        raise ValueError(
            "Category file must have columns: Product, Business Unit, Oil Type, "
            "Packing Category (legacy: Product, Category 1, Category 2)"
        )

    packing_series = (
        body[pack_col].map(_clean_text)
        if pack_col is not None
        else pd.Series([""] * len(body), index=body.index)
    )

    frame = pd.DataFrame(
        {
            "product": body[product_col].astype(str).str.strip(),
            # Keep category1/category2 aliases for report/PDF joins
            "category1": body[bu_col].map(_normalize_business_unit),
            "category2": body[oil_col].map(_clean_text),
            "packing_category": packing_series,
            "business_unit": body[bu_col].map(_normalize_business_unit),
            "oil_type": body[oil_col].map(_clean_text),
        }
    )
    frame = frame[frame["product"].ne("") & frame["product"].str.lower().ne("nan")]
    frame = frame.dropna(subset=["product"])
    duplicates = frame["product"][frame["product"].duplicated()].tolist()
    if duplicates:
        raise ValueError(
            "Duplicate products in category file: " + ", ".join(duplicates[:20])
        )
    return frame.reset_index(drop=True)


# Back-compat name used by older call sites
_normalize_category1 = _normalize_business_unit
CATEGORY1_ALIASES = BUSINESS_UNIT_ALIASES
