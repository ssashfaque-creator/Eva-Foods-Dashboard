"""Load product + packing cost factor workbooks and compute total factor costs."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd

PRODUCT_COST_SHEET = "ProductCostFactors"
PRODUCT_COST_HEADER_ROW = 4  # 0-indexed; Excel row 5

# Canonical output columns
OUTPUT_COLUMNS = (
    "ClientType",
    "ProdID",
    "Product",
    "Unit",
    "ProductCost",
    "PackingCost",
    "TotalFactorCost",
    "ProductCostDate",
    "PackingCostDate",
    "PCFID",
)


@dataclass(frozen=True)
class FactorCostResult:
    """Computed total factor costs ready to persist."""

    frame: pd.DataFrame
    product_cost_path: Path
    packing_cost_path: Path
    product_rows_read: int
    packing_rows_read: int
    products_without_packing: int


def _to_date(value: Any) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value.date()
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _norm_header(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def _find_col(columns: list[str], *candidates: str) -> str | None:
    lookup = {_norm_header(c): c for c in columns}
    for name in candidates:
        key = _norm_header(name)
        if key in lookup:
            return lookup[key]
    return None


def _read_xlsx_via_openpyxl(path: Path, sheet_name: str | None = None) -> pd.DataFrame:
    return pd.read_excel(path, sheet_name=sheet_name or 0, header=None, engine="openpyxl")


def _col_letter_to_index(letter: str) -> int:
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch.upper()) - ord("A") + 1)
    return idx - 1


def _read_xlsx_via_zip(path: Path, sheet_index: int = 0) -> pd.DataFrame:
    """Fallback reader for Google-exported xlsx files that openpyxl rejects."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", ns):
                texts = [t.text or "" for t in si.findall(".//m:t", ns)]
                shared.append("".join(texts))

        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        sheets = workbook.findall("m:sheets/m:sheet", ns)
        if not sheets:
            raise ValueError(f"No sheets found in {path}")
        if sheet_index >= len(sheets):
            raise ValueError(f"Sheet index {sheet_index} out of range for {path}")

        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_ns = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
        rid = sheets[sheet_index].attrib[
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        ]
        target = None
        for rel in rels.findall("r:Relationship", rel_ns):
            if rel.attrib.get("Id") == rid:
                target = rel.attrib["Target"]
                break
        if not target:
            target = f"worksheets/sheet{sheet_index + 1}.xml"
        sheet_path = target if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
        sheet_root = ET.fromstring(zf.read(sheet_path))

        rows: dict[int, dict[int, Any]] = {}
        max_col = 0
        for row_el in sheet_root.findall("m:sheetData/m:row", ns):
            r_idx = int(row_el.attrib["r"]) - 1
            row_vals: dict[int, Any] = {}
            for cell in row_el.findall("m:c", ns):
                ref = cell.attrib.get("r", "")
                m = re.match(r"([A-Z]+)", ref)
                if not m:
                    continue
                c_idx = _col_letter_to_index(m.group(1))
                max_col = max(max_col, c_idx)
                v_el = cell.find("m:v", ns)
                if v_el is None or v_el.text is None:
                    continue
                raw = v_el.text
                if cell.attrib.get("t") == "s":
                    row_vals[c_idx] = shared[int(raw)]
                else:
                    try:
                        num = float(raw)
                        row_vals[c_idx] = int(num) if num.is_integer() else num
                    except ValueError:
                        row_vals[c_idx] = raw
            rows[r_idx] = row_vals

    if not rows:
        return pd.DataFrame()

    max_row = max(rows)
    data = []
    for r in range(max_row + 1):
        row_vals = rows.get(r, {})
        data.append([row_vals.get(c) for c in range(max_col + 1)])
    return pd.DataFrame(data)


def read_excel_raw(path: Path, sheet_name: str | int | None = None) -> pd.DataFrame:
    """Read an Excel sheet as a headerless DataFrame, with zip XML fallback."""
    path = Path(path)
    try:
        if isinstance(sheet_name, str):
            return _read_xlsx_via_openpyxl(path, sheet_name=sheet_name)
        return _read_xlsx_via_openpyxl(path, sheet_name=None if sheet_name is None else sheet_name)
    except Exception:
        idx = 0 if not isinstance(sheet_name, int) else sheet_name
        return _read_xlsx_via_zip(path, sheet_index=idx)


def _frame_from_header_row(raw: pd.DataFrame, header_row: int) -> pd.DataFrame:
    header = [str(v).strip() if v is not None and not (isinstance(v, float) and pd.isna(v)) else f"col_{i}"
              for i, v in enumerate(raw.iloc[header_row].tolist())]
    body = raw.iloc[header_row + 1 :].copy()
    body.columns = header
    body = body.dropna(how="all")
    return body.reset_index(drop=True)


def load_product_cost_factors(path: Path | str) -> pd.DataFrame:
    """
    Load product cost factors and collapse to one row per ClientType + ProdID.

    For each client type and product, keep the most recent Date (tie-break: highest
    PCFID). Sum Cost across ProductCostCenter lines in that snapshot.
    """
    path = Path(path)
    raw = read_excel_raw(path, sheet_name=PRODUCT_COST_SHEET)
    # Detect header row if title rows precede it
    header_row = PRODUCT_COST_HEADER_ROW
    for i in range(min(10, len(raw))):
        vals = [_norm_header(v) for v in raw.iloc[i].tolist()]
        if "clienttype" in vals and "cost" in vals and "prodid" in vals:
            header_row = i
            break

    df = _frame_from_header_row(raw, header_row)
    col_client = _find_col(list(df.columns), "ClientType")
    col_prodid = _find_col(list(df.columns), "ProdID")
    col_product = _find_col(list(df.columns), "Product")
    col_unit = _find_col(list(df.columns), "Unit")
    col_cost = _find_col(list(df.columns), "Cost")
    col_date = _find_col(list(df.columns), "Date")
    col_pcfid = _find_col(list(df.columns), "PCFID")

    required = {
        "ClientType": col_client,
        "ProdID": col_prodid,
        "Product": col_product,
        "Unit": col_unit,
        "Cost": col_cost,
        "Date": col_date,
    }
    missing = [name for name, col in required.items() if col is None]
    if missing:
        raise ValueError(f"Product cost file missing columns: {', '.join(missing)}")

    out = pd.DataFrame(
        {
            "ClientType": df[col_client].astype(str).str.strip(),
            "ProdID": pd.to_numeric(df[col_prodid], errors="coerce"),
            "Product": df[col_product].astype(str).str.strip(),
            "Unit": df[col_unit].astype(str).str.strip(),
            "Cost": pd.to_numeric(df[col_cost], errors="coerce").fillna(0.0),
            "Date": df[col_date].map(_to_date),
            "PCFID": pd.to_numeric(df[col_pcfid], errors="coerce") if col_pcfid else pd.NA,
        }
    )
    out = out.dropna(subset=["ClientType", "ProdID", "Date"])
    out = out[out["ClientType"].str.lower() != "nan"]
    out["ProdID"] = out["ProdID"].astype(int)

    # Most recent Date per client + product; tie-break on highest PCFID
    latest_keys = (
        out.sort_values(["Date", "PCFID"], ascending=[False, False])
        .groupby(["ClientType", "ProdID"], as_index=False)
        .first()[["ClientType", "ProdID", "Date", "PCFID", "Product", "Unit"]]
    )

    merged = out.merge(
        latest_keys[["ClientType", "ProdID", "Date", "PCFID"]],
        on=["ClientType", "ProdID", "Date", "PCFID"],
        how="inner",
    )
    summed = (
        merged.groupby(["ClientType", "ProdID", "Product", "Unit", "Date", "PCFID"], as_index=False)
        .agg(ProductCost=("Cost", "sum"))
    )
    summed = (
        summed.sort_values(["ClientType", "ProdID", "Date", "PCFID"])
        .groupby(["ClientType", "ProdID"], as_index=False)
        .last()
    )
    summed = summed.rename(columns={"Date": "ProductCostDate"})
    return summed[
        ["ClientType", "ProdID", "Product", "Unit", "ProductCost", "ProductCostDate", "PCFID"]
    ].reset_index(drop=True)


def load_packing_costs(path: Path | str) -> pd.DataFrame:
    """
    Load packing costs — one row per ProdID (most recent).

    Prefer a Date column when present. Otherwise treat the file as append-only and
    keep the last row for each product (file order).
    """
    path = Path(path)
    raw = read_excel_raw(path, sheet_name=0)
    if raw.empty:
        return pd.DataFrame(
            columns=["ProdID", "Product", "PackingCost", "Unit", "PackingCostDate"]
        )

    # Detect header vs headerless Google export
    first = [_norm_header(v) for v in raw.iloc[0].tolist()]
    has_header = any(h in first for h in ("prodid", "product", "cost", "unit", "date"))

    if has_header:
        df = _frame_from_header_row(raw, 0)
        col_prodid = _find_col(list(df.columns), "ProdID", "ProductID", "ID")
        col_product = _find_col(list(df.columns), "Product")
        col_cost = _find_col(list(df.columns), "Cost", "PackingCost")
        col_unit = _find_col(list(df.columns), "Unit")
        col_date = _find_col(list(df.columns), "Date")
        row_order = list(range(len(df)))
    else:
        # ProdID, Product, Cost, Unit [, Date]
        cols = list(raw.columns)
        rename = {}
        if len(cols) >= 1:
            rename[cols[0]] = "ProdID"
        if len(cols) >= 2:
            rename[cols[1]] = "Product"
        if len(cols) >= 3:
            rename[cols[2]] = "Cost"
        if len(cols) >= 4:
            rename[cols[3]] = "Unit"
        if len(cols) >= 5:
            rename[cols[4]] = "Date"
        df = raw.rename(columns=rename)
        col_prodid, col_product, col_cost, col_unit = "ProdID", "Product", "Cost", "Unit"
        col_date = "Date" if "Date" in df.columns else None
        row_order = list(range(len(df)))

    if col_prodid is None or col_cost is None:
        raise ValueError("Packing cost file must include ProdID and Cost columns")

    out = pd.DataFrame(
        {
            "ProdID": pd.to_numeric(df[col_prodid], errors="coerce"),
            "Product": df[col_product].astype(str).str.strip() if col_product else "",
            "PackingCost": pd.to_numeric(df[col_cost], errors="coerce"),
            "Unit": df[col_unit].astype(str).str.strip() if col_unit else "",
            "PackingCostDate": df[col_date].map(_to_date) if col_date else None,
            "_row": row_order,
        }
    )
    out = out.dropna(subset=["ProdID", "PackingCost"])
    out["ProdID"] = out["ProdID"].astype(int)

    if out["PackingCostDate"].notna().any():
        latest = (
            out.sort_values(["PackingCostDate", "_row"], ascending=[True, True])
            .groupby("ProdID", as_index=False)
            .last()
        )
    else:
        latest = out.sort_values("_row").groupby("ProdID", as_index=False).last()
        latest["PackingCostDate"] = pd.NaT

    return latest[["ProdID", "Product", "PackingCost", "Unit", "PackingCostDate"]].reset_index(
        drop=True
    )


def compute_total_factor_costs(
    product_cost_path: Path | str,
    packing_cost_path: Path | str,
) -> FactorCostResult:
    """
    Total factor cost per product per client type:

        ProductCost (sum of latest cost centers) + PackingCost (latest for product)

    Unit (Ltrs / Kgs) is taken from the product cost factors file.
    """
    product_cost_path = Path(product_cost_path)
    packing_cost_path = Path(packing_cost_path)

    products = load_product_cost_factors(product_cost_path)
    packing = load_packing_costs(packing_cost_path)

    pack_lookup = packing[["ProdID", "PackingCost", "PackingCostDate"]].rename(
        columns={"PackingCostDate": "PackingCostDate"}
    )

    merged = products.merge(pack_lookup, on="ProdID", how="left")
    missing_pack = int(merged["PackingCost"].isna().sum())
    merged["PackingCost"] = merged["PackingCost"].fillna(0.0)
    merged["TotalFactorCost"] = merged["ProductCost"] + merged["PackingCost"]

    frame = merged[
        [
            "ClientType",
            "ProdID",
            "Product",
            "Unit",
            "ProductCost",
            "PackingCost",
            "TotalFactorCost",
            "ProductCostDate",
            "PackingCostDate",
            "PCFID",
        ]
    ].sort_values(["ClientType", "ProdID"]).reset_index(drop=True)

    return FactorCostResult(
        frame=frame,
        product_cost_path=product_cost_path,
        packing_cost_path=packing_cost_path,
        product_rows_read=len(products),
        packing_rows_read=len(packing),
        products_without_packing=missing_pack,
    )


def save_factor_costs(result: FactorCostResult, output: Path | str) -> Path:
    """Save total factor costs to CSV or Excel based on the output suffix."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    frame = result.frame[list(OUTPUT_COLUMNS)].copy()
    for col in ("ProductCost", "PackingCost", "TotalFactorCost"):
        frame[col] = frame[col].astype(float).round(6)
    suffix = output.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        frame.to_excel(output, index=False)
    else:
        if suffix != ".csv":
            output = output.with_suffix(".csv")
        frame.to_csv(output, index=False)
    return output
