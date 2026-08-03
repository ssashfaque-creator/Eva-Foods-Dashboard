"""Load sales/client Excel workbooks and aggregate report metrics."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd


SALES_SHEET = "Sales"
CATEGORY_SHEET = "Category"
CLIENT_SHEET = "ClientListReport"
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

CITY_BRAND_COLUMNS = (
    "Eva Consumer",
    "Eva Bulk",
    "Maan Consumer",
    "Maan Bulk",
)

# Summary + detail section order (Excel spelling: Cusine King)
CATEGORY1_ORDER = (
    "Eva Consumer",
    "Eva Bulk",
    "Maan Consumer",
    "Maan Bulk",
    "Cusine King",
    "Shortening",
    "Bulk Oil",
    "Meal",
    "Byproducts",
)

# These product types get City-Filter subsections in sales detail
CITY_DETAIL_CATEGORIES = (
    "Eva Consumer",
    "Eva Bulk",
    "Maan Consumer",
    "Maan Bulk",
    "Cusine King",
)


@dataclass(frozen=True)
class CategorySummaryRow:
    category1: str
    daily_mt: float
    avg_30d_mt: float
    mtd_mt: float
    ams_mt: float


@dataclass(frozen=True)
class SalesReportData:
    """Prepared data for the sales PDF report."""

    source_path: Path
    clients_path: Path | None
    report_date: date
    month_start: date
    month_end: date
    trailing_30_start: date
    ams_months: tuple[str, ...]
    category_summary: list[CategorySummaryRow]
    city_daily: pd.DataFrame
    city_mtd: pd.DataFrame
    city_daily_ads: dict[str, float]
    city_mtd_ams: dict[str, float]
    daily_sales: pd.DataFrame
    total_daily_mt: float
    total_mtd_mt: float
    total_avg_30d_mt: float
    total_ams_mt: float


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


def normalize_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"\s+", " ", text)


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


def resolve_credit_days(cr_days: Any, payment_type: Any) -> float:
    """Use CrDays when set; else Cash→0, Credit/blank→30."""
    if cr_days is not None and not (isinstance(cr_days, float) and pd.isna(cr_days)):
        text = str(cr_days).strip()
        if text != "":
            try:
                return float(cr_days)
            except (TypeError, ValueError):
                pass

    payment = str(payment_type or "").strip().lower()
    if payment == "cash":
        return 0.0
    return 30.0


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
    sales["party_key"] = sales["party"].map(normalize_name)
    return sales.reset_index(drop=True)


def load_clients(path: Path | str) -> pd.DataFrame:
    """Load client master. Geographic city for reports comes from City-Filter only."""
    path = Path(path)
    raw = pd.read_excel(
        path,
        sheet_name=CLIENT_SHEET,
        header=HEADER_ROW,
        engine="openpyxl",
    )

    rename = {
        "Locality": "locality",
        "Region": "region",
        "Zone": "zone",
        "Area": "area",
        "Territory": "territory",
        "Type": "client_type",
        "ClientID": "client_id",
        "Client": "client",
        "PaymentType": "payment_type",
        "CrDays": "cr_days_raw",
        "CrLimit": "cr_limit",
        "InActive": "inactive",
        "City-Filter": "city",
    }
    if "City-Filter" not in raw.columns:
        raise ValueError(
            "Clients sheet missing required City-Filter column "
            "(do not use City; City-Filter is the report geography)"
        )

    missing = [col for col in rename if col not in raw.columns]
    if missing:
        raise ValueError(f"Clients sheet missing columns: {missing}")

    clients = raw[list(rename.keys())].rename(columns=rename).copy()
    clients["client"] = clients["client"].fillna("").astype(str).str.strip()
    clients = clients[clients["client"] != ""].copy()
    clients["party_key"] = clients["client"].map(normalize_name)

    for col in ("locality", "zone", "area", "territory", "client_type", "payment_type", "city"):
        clients[col] = clients[col].fillna("").astype(str).str.strip()
        clients.loc[clients[col].str.lower().isin({"nan", "none"}), col] = ""

    clients["cr_limit"] = pd.to_numeric(clients["cr_limit"], errors="coerce")
    clients["credit_days"] = [
        resolve_credit_days(days, pay)
        for days, pay in zip(clients["cr_days_raw"], clients["payment_type"], strict=True)
    ]

    inactive = clients["inactive"].astype(str).str.strip().str.upper()
    clients["_inactive"] = inactive.eq("Y")
    # Prefer rows with a real City-Filter over blank/Undefined when deduping
    clients["_city_missing"] = clients["city"].eq("") | clients["city"].str.lower().eq(
        "undefined"
    )
    clients = clients.sort_values(
        ["_inactive", "_city_missing", "client_id"],
        kind="mergesort",
    )
    clients = clients.drop_duplicates("party_key", keep="first")

    return clients[
        [
            "party_key",
            "client_id",
            "client",
            "locality",
            "zone",
            "area",
            "territory",
            "client_type",
            "city",
            "payment_type",
            "credit_days",
            "cr_limit",
        ]
    ].reset_index(drop=True)


def _category1_sort_key(name: str) -> tuple[int, str]:
    try:
        return (CATEGORY1_ORDER.index(name), name)
    except ValueError:
        return (len(CATEGORY1_ORDER), name)


def _prior_three_month_ranges(current: date) -> list[tuple[date, date]]:
    """Full calendar months immediately before the report month (oldest → newest)."""
    year, month = current.year, current.month
    ranges: list[tuple[date, date]] = []
    for _ in range(3):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        ranges.append((start, end))
    ranges.reverse()
    return ranges


def pct_change(current: float, baseline: float) -> float | None:
    if baseline == 0:
        return None
    return (current - baseline) / baseline * 100.0


def _sum_by_category(frame: pd.DataFrame) -> dict[str, float]:
    if frame.empty:
        return {}
    grouped = frame.groupby("category1")["effective_mt"].sum()
    return {str(k): float(v) for k, v in grouped.items()}


def _city_brand_pivot(frame: pd.DataFrame) -> pd.DataFrame:
    brands = list(CITY_BRAND_COLUMNS)
    scoped = frame[frame["category1"].isin(brands)].copy()
    if scoped.empty:
        empty = pd.DataFrame(columns=["city", *brands, "total"])
        return empty

    scoped["city"] = scoped["city"].replace("", "Unmapped").fillna("Unmapped")
    pivot = (
        scoped.groupby(["city", "category1"], as_index=False)["effective_mt"]
        .sum()
        .pivot(index="city", columns="category1", values="effective_mt")
        .reindex(columns=brands)
        .fillna(0.0)
        .reset_index()
    )
    pivot["total"] = pivot[brands].sum(axis=1)
    pivot = pivot.sort_values(
        ["total", "city"], ascending=[False, True], kind="mergesort"
    ).reset_index(drop=True)
    return pivot


def prepare_report_data(
    path: Path | str,
    clients_path: Path | str | None = None,
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

    clients_file: Path | None = None
    if clients_path is not None:
        clients_file = Path(clients_path)
        clients = load_clients(clients_file)
        merged = merged.merge(clients, on="party_key", how="left")
    else:
        for col, default in (
            ("city", ""),
            ("client_type", ""),
            ("locality", ""),
            ("zone", ""),
            ("area", ""),
            ("territory", ""),
            ("payment_type", ""),
            ("credit_days", None),
            ("cr_limit", None),
            ("client_id", None),
        ):
            merged[col] = default

    merged["city"] = merged["city"].fillna("").astype(str).str.strip()
    merged["client_type"] = merged["client_type"].fillna("").astype(str).str.strip()
    merged["detail_category"] = merged["client_type"].where(
        merged["client_type"] != "", "Unmapped"
    )
    merged["city_display"] = merged["city"].where(merged["city"] != "", "Unmapped")

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
    trailing_30_start = current - timedelta(days=29)
    prior_months = _prior_three_month_ranges(current)
    ams_labels = tuple(start.strftime("%b %Y") for start, _ in prior_months)

    month_mask = (merged["date"] >= month_start) & (merged["date"] <= current)
    daily_mask = merged["date"] == current
    trailing_mask = (merged["date"] >= trailing_30_start) & (merged["date"] <= current)

    mtd = merged.loc[month_mask]
    daily = merged.loc[daily_mask].copy()
    trailing_30 = merged.loc[trailing_mask]

    # Prior 3 full months (often empty until history is loaded)
    prior_frames = []
    for start, end in prior_months:
        prior_frames.append(
            merged.loc[(merged["date"] >= start) & (merged["date"] <= end)]
        )
    prior_all = (
        pd.concat(prior_frames, ignore_index=True) if prior_frames else merged.iloc[0:0]
    )

    mtd_by_cat = _sum_by_category(mtd)
    daily_by_cat = _sum_by_category(daily)
    # Average daily MT over a fixed 30-day calendar window (zeros for missing days)
    trailing_by_cat = {
        key: value / 30.0 for key, value in _sum_by_category(trailing_30).items()
    }
    # AMS = mean of the three prior monthly totals
    ams_by_cat: dict[str, float] = {}
    monthly_cat_totals: list[dict[str, float]] = [
        _sum_by_category(frame) for frame in prior_frames
    ]
    all_cats = set(mtd_by_cat) | set(daily_by_cat) | set(trailing_by_cat)
    for month_totals in monthly_cat_totals:
        all_cats.update(month_totals)
    for cat in all_cats:
        ams_by_cat[cat] = (
            sum(month_totals.get(cat, 0.0) for month_totals in monthly_cat_totals) / 3.0
        )

    summary_names = sorted(all_cats, key=_category1_sort_key)
    category_summary = [
        CategorySummaryRow(
            category1=name,
            daily_mt=float(daily_by_cat.get(name, 0.0)),
            avg_30d_mt=float(trailing_by_cat.get(name, 0.0)),
            mtd_mt=float(mtd_by_cat.get(name, 0.0)),
            ams_mt=float(ams_by_cat.get(name, 0.0)),
        )
        for name in summary_names
        if name in CATEGORY1_ORDER or name in daily_by_cat or name in mtd_by_cat
    ]
    # Keep configured order, then any extras
    ordered = [row for name in CATEGORY1_ORDER for row in category_summary if row.category1 == name]
    extras = [row for row in category_summary if row.category1 not in CATEGORY1_ORDER]
    category_summary = ordered + extras

    city_daily = _city_brand_pivot(daily)
    city_mtd = _city_brand_pivot(mtd)

    # City-level trailing 30-day average of daily TOTAL sales
    trailing_city = _city_brand_pivot(trailing_30)
    trailing_city_avg = {
        str(row["city"]): float(row["total"]) / 30.0
        for _, row in trailing_city.iterrows()
    }
    city_daily["avg_30d"] = city_daily["city"].map(
        lambda c: float(trailing_city_avg.get(str(c), 0.0))
    )

    # ADS row: brand + total average daily sales over last 30 days
    brands = list(CITY_BRAND_COLUMNS)
    city_daily_ads = {
        brand: float(trailing_by_cat.get(brand, 0.0)) for brand in brands
    }
    city_daily_ads["total"] = float(sum(city_daily_ads.values()))

    # City-level AMS for prior 3 months (mean monthly total for that city)
    prior_city_months = [_city_brand_pivot(frame) for frame in prior_frames]
    city_ams_map: dict[str, float] = {}
    all_cities = set(city_mtd["city"].astype(str)) if len(city_mtd) else set()
    for pivot in prior_city_months:
        all_cities.update(pivot["city"].astype(str).tolist())
    for city_name in all_cities:
        month_totals = []
        for pivot in prior_city_months:
            match = pivot.loc[pivot["city"].astype(str) == city_name, "total"]
            month_totals.append(float(match.iloc[0]) if len(match) else 0.0)
        city_ams_map[city_name] = sum(month_totals) / 3.0
    city_mtd["ams"] = city_mtd["city"].map(
        lambda c: float(city_ams_map.get(str(c), 0.0))
    )

    # AMS footer row for brand columns
    city_mtd_ams = {brand: float(ams_by_cat.get(brand, 0.0)) for brand in brands}
    city_mtd_ams["total"] = float(sum(city_mtd_ams.values()))

    daily_sales = daily[
        [
            "category1",
            "detail_category",
            "city_display",
            "party",
            "product",
            "qty",
            "unit",
            "effective_mt",
            "rate",
            "basic_amount",
            "incl_gst_fed",
        ]
    ].rename(
        columns={
            "category1": "product_type",
            "detail_category": "category",
            "city_display": "city",
            "effective_mt": "mt_qty",
        }
    )
    kg = daily_sales["mt_qty"] * 1000.0
    daily_sales["amount_per_kg"] = [
        (float(incl) / float(k) if float(k) else 0.0)
        for incl, k in zip(daily_sales["incl_gst_fed"], kg, strict=True)
    ]
    daily_sales["_ptype_ord"] = daily_sales["product_type"].map(
        lambda n: _category1_sort_key(n)[0]
    )
    daily_sales = daily_sales.sort_values(
        ["_ptype_ord", "city", "party", "product"], kind="mergesort"
    ).drop(columns=["_ptype_ord"]).reset_index(drop=True)

    total_daily = float(daily_sales["mt_qty"].sum())
    total_mtd = float(sum(row.mtd_mt for row in category_summary))
    total_avg_30d = float(sum(row.avg_30d_mt for row in category_summary))
    total_ams = float(sum(row.ams_mt for row in category_summary))

    return SalesReportData(
        source_path=path,
        clients_path=clients_file,
        report_date=current,
        month_start=month_start,
        month_end=month_end,
        trailing_30_start=trailing_30_start,
        ams_months=ams_labels,
        category_summary=category_summary,
        city_daily=city_daily,
        city_mtd=city_mtd,
        city_daily_ads=city_daily_ads,
        city_mtd_ams=city_mtd_ams,
        daily_sales=daily_sales,
        total_daily_mt=total_daily,
        total_mtd_mt=total_mtd,
        total_avg_30d_mt=total_avg_30d,
        total_ams_mt=total_ams,
    )
