"""Load sales Excel workbooks and aggregate Category 1 metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd


SALES_SHEET = "Sales"
CATEGORY_SHEET = "Category"
HEADER_ROW = 4  # 0-indexed; Excel row 5

SALES_COLUMNS = {
    "Date": "date",
    "Party": "party",
    "Product": "product",
    "Qty": "qty",
    "Unit": "unit",
    "M.T Qty": "mt_qty",
    "Rate": "rate",
    "Basic Amount": "basic_amount",
    "Incl GST/FED Amount": "incl_gst_fed",
}


@dataclass(frozen=True)
class CategorySummaryRow:
    category1: str
    daily_mt: float
    mtd_mt: float


@dataclass(frozen=True)
class SalesReportData:
    """Prepared data for the sales PDF report."""

    source_path: Path
    report_date: date
    month_start: date
    month_end: date
    category_summary: list[CategorySummaryRow]
    daily_sales: pd.DataFrame
    total_daily_mt: float
    total_mtd_mt: float


def _to_date(value: Any) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() == "totals":
            return None
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    return None


def effective_mt_qty(qty: Any, unit: Any, mt_qty: Any) -> float:
    """Prefer Excel M.T Qty; for bulk Kgs rows where it is blank/0, use Qty/1000."""
    try:
        mt = float(mt_qty) if mt_qty is not None and not pd.isna(mt_qty) else 0.0
    except (TypeError, ValueError):
        mt = 0.0

    if mt != 0.0:
        return mt

    unit_text = str(unit or "").strip().lower()
    try:
        quantity = float(qty) if qty is not None and not pd.isna(qty) else 0.0
    except (TypeError, ValueError):
        quantity = 0.0

    if unit_text in {"kgs", "kg"}:
        return quantity / 1000.0
    if unit_text in {"mt", "m.t", "m.t.", "ton", "tons", "tonne", "tonnes"}:
        return quantity
    return mt


def load_category_map(path: Path | str) -> pd.DataFrame:
    path = Path(path)
    mapping = pd.read_excel(path, sheet_name=CATEGORY_SHEET, engine="openpyxl")
    mapping = mapping.rename(
        columns={
            "Product": "product",
            "Category 1": "category1",
            "Category 2": "category2",
        }
    )
    mapping["product"] = mapping["product"].astype(str).str.strip()
    mapping["category1"] = mapping["category1"].astype(str).str.strip()
    mapping["category2"] = mapping["category2"].astype(str).str.strip()
    mapping = mapping.dropna(subset=["product"])
    duplicates = mapping["product"][mapping["product"].duplicated()].tolist()
    if duplicates:
        raise ValueError(f"Duplicate products in Category sheet: {duplicates}")
    return mapping[["product", "category1", "category2"]]


def load_sales(path: Path | str) -> pd.DataFrame:
    path = Path(path)
    raw = pd.read_excel(
        path,
        sheet_name=SALES_SHEET,
        header=HEADER_ROW,
        engine="openpyxl",
    )
    missing = [col for col in SALES_COLUMNS if col not in raw.columns]
    if missing:
        raise ValueError(f"Sales sheet missing columns: {missing}")

    sales = raw[list(SALES_COLUMNS.keys())].rename(columns=SALES_COLUMNS)
    sales["date"] = sales["date"].map(_to_date)
    sales = sales.dropna(subset=["date", "product"]).copy()
    sales["product"] = sales["product"].astype(str).str.strip()
    sales["party"] = sales["party"].fillna("").astype(str).str.strip()
    for col in ("qty", "mt_qty", "rate", "basic_amount", "incl_gst_fed"):
        sales[col] = pd.to_numeric(sales[col], errors="coerce").fillna(0.0)
    sales["unit"] = sales["unit"].fillna("").astype(str).str.strip()
    sales["effective_mt"] = [
        effective_mt_qty(q, u, m)
        for q, u, m in zip(sales["qty"], sales["unit"], sales["mt_qty"], strict=True)
    ]
    return sales.reset_index(drop=True)


def prepare_report_data(
    path: Path | str,
    report_date: date | None = None,
) -> SalesReportData:
    path = Path(path)
    sales = load_sales(path)
    categories = load_category_map(path)
    merged = sales.merge(categories, on="product", how="left")
    unmatched = sorted(merged.loc[merged["category1"].isna(), "product"].unique())
    if unmatched:
        raise ValueError(
            "Products missing from Category sheet: " + ", ".join(unmatched)
        )

    available_dates = sorted(merged["date"].unique())
    if not available_dates:
        raise ValueError("No dated sales rows found in workbook")

    current = report_date or available_dates[-1]
    if current not in set(available_dates):
        raise ValueError(
            f"Report date {current.isoformat()} not found in sales data "
            f"({available_dates[0]} to {available_dates[-1]})"
        )

    month_start = current.replace(day=1)
    month_end = available_dates[-1]
    month_mask = (merged["date"] >= month_start) & (merged["date"] <= current)
    daily_mask = merged["date"] == current

    mtd = merged.loc[month_mask]
    daily = merged.loc[daily_mask].copy()

    mtd_by_cat = mtd.groupby("category1", as_index=False)["effective_mt"].sum()
    daily_by_cat = daily.groupby("category1", as_index=False)["effective_mt"].sum()
    summary = mtd_by_cat.merge(
        daily_by_cat,
        on="category1",
        how="outer",
        suffixes=("_mtd", "_daily"),
    ).fillna(0.0)
    summary = summary.sort_values("category1").reset_index(drop=True)

    category_summary = [
        CategorySummaryRow(
            category1=row["category1"],
            daily_mt=float(row["effective_mt_daily"]),
            mtd_mt=float(row["effective_mt_mtd"]),
        )
        for _, row in summary.iterrows()
    ]

    daily_sales = daily[
        [
            "category2",
            "party",
            "product",
            "qty",
            "unit",
            "effective_mt",
            "rate",
            "basic_amount",
            "incl_gst_fed",
        ]
    ].rename(columns={"category2": "category", "effective_mt": "mt_qty"})
    kg = daily_sales["mt_qty"] * 1000.0
    daily_sales["amount_per_kg"] = [
        (float(incl) / float(k) if float(k) else 0.0)
        for incl, k in zip(daily_sales["incl_gst_fed"], kg, strict=True)
    ]
    daily_sales = daily_sales.sort_values(
        ["category", "party", "product"], kind="mergesort"
    ).reset_index(drop=True)

    return SalesReportData(
        source_path=path,
        report_date=current,
        month_start=month_start,
        month_end=month_end,
        category_summary=category_summary,
        daily_sales=daily_sales,
        total_daily_mt=float(daily_sales["mt_qty"].sum()),
        total_mtd_mt=float(sum(row.mtd_mt for row in category_summary)),
    )
