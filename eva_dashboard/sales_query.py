"""Structured sales query engine for the chatbot (pivots + analytical AMS)."""

from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Any

import pandas as pd

from eva_dashboard.categories import BUSINESS_UNIT_ALIASES
from eva_dashboard.client_language import (
    normalize_client_type,
    normalize_oil_type,
    normalize_packing_category,
)
from eva_dashboard.data import (
    CATEGORY1_ORDER,
    LTR_TO_KG,
    MAUND_FACTOR_PRICE_FETCH,
    _prior_three_month_ranges,
    cost_factor_per_kg,
    pct_change,
    price_fetch_per_maund,
    weighted_avg,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.fmt import mt_round, mt_str

_PARTY_JOIN = """
LEFT JOIN clients cl
  ON lower(trim(replace(replace(cl.client, '  ', ' '), '  ', ' ')))
   = lower(trim(replace(replace(s.party, '  ', ' '), '  ', ' ')))
LEFT JOIN category c ON c.product = s.product
"""

MONTH_NAMES = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


def _sales_date_bounds() -> tuple[date | None, date | None]:
    init_db()
    with connect() as conn:
        row = conn.execute(
            "SELECT MIN(date) AS min_d, MAX(date) AS max_d FROM sales WHERE date IS NOT NULL"
        ).fetchone()
    if not row or not row["max_d"]:
        return None, None
    return (
        date.fromisoformat(str(row["min_d"])[:10]) if row["min_d"] else None,
        date.fromisoformat(str(row["max_d"])[:10]),
    )


def _parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _normalize_business_unit(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    return BUSINESS_UNIT_ALIASES.get(text.lower(), text)


def resolve_period(
    period: str | None = None,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Resolve natural-language or ISO period against live sales dates."""
    explicit_from = _parse_iso(date_from)
    explicit_to = _parse_iso(date_to)
    min_d, max_d = _sales_date_bounds()
    if explicit_from and explicit_to:
        partial = explicit_to.day < calendar.monthrange(explicit_to.year, explicit_to.month)[1]
        # Treat as partial only when end is before month-end AND matches max sales in that month
        return {
            "date_from": explicit_from.isoformat(),
            "date_to": explicit_to.isoformat(),
            "label": f"{explicit_from.isoformat()} → {explicit_to.isoformat()}",
            "partial_month": bool(
                explicit_from.day == 1
                and explicit_from.month == explicit_to.month
                and explicit_from.year == explicit_to.year
                and partial
            ),
            "days_elapsed": (explicit_to - explicit_from).days + 1,
            "days_in_month": calendar.monthrange(explicit_to.year, explicit_to.month)[1],
            "anchor_max_sales_date": max_d.isoformat() if max_d else None,
        }

    if max_d is None:
        return {
            "ok": False,
            "error": "No sales dates in database",
            "date_from": None,
            "date_to": None,
        }

    text = (period or "").strip().lower()
    text = re.sub(r"\s+", " ", text)

    # YYYY-MM
    m = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        # Cap to available data if asking for current partial month
        partial = False
        if max_d.year == year and max_d.month == month and max_d < end:
            end = max_d
            partial = True
        return _period_result(start, end, partial, max_d, label=start.strftime("%b %Y"))

    # last month / previous month
    if "last month" in text or "previous month" in text or text in {"prior month"}:
        y, mth = max_d.year, max_d.month
        mth -= 1
        if mth == 0:
            mth = 12
            y -= 1
        start = date(y, mth, 1)
        end = date(y, mth, calendar.monthrange(y, mth)[1])
        return _period_result(start, end, False, max_d, label=start.strftime("%b %Y"))

    # last week / past week / previous week (7 days ending at max sales date)
    if re.search(r"\b(last|past|previous)\s+weeks?\b", text) or text in {
        "last week",
        "past week",
        "previous week",
    }:
        from datetime import timedelta

        end = max_d
        start = max_d - timedelta(days=6)
        if min_d and start < min_d:
            start = min_d
        return _period_result(
            start,
            end,
            False,
            max_d,
            label=f"Last 7 days ({start.isoformat()} → {end.isoformat()})",
        )

    # this week (Mon–max_d or last 7 days of current week ending max_d)
    if "this week" in text:
        from datetime import timedelta

        start = max_d - timedelta(days=max_d.weekday())  # Monday
        if min_d and start < min_d:
            start = min_d
        return _period_result(
            start,
            max_d,
            True,
            max_d,
            label=f"This week ({start.isoformat()} → {max_d.isoformat()})",
        )

    # last 3 months / past 3 months (calendar months ending at max_d month)
    m_n = re.search(r"\b(last|past|previous)\s+(\d{1,2})\s+months?\b", text)
    if m_n or "last quarter" in text or "previous quarter" in text or "past quarter" in text:
        from datetime import timedelta

        n_months = 3
        if m_n:
            n_months = max(1, min(24, int(m_n.group(2))))
        elif "quarter" in text:
            n_months = 3
        # Inclusive: n full/partial months ending at max_d
        end = max_d
        y, mth = max_d.year, max_d.month
        for _ in range(n_months - 1):
            mth -= 1
            if mth == 0:
                mth = 12
                y -= 1
        start = date(y, mth, 1)
        if min_d and start < min_d:
            start = min_d
        label = (
            f"Last quarter ({start.isoformat()} → {end.isoformat()})"
            if "quarter" in text
            else f"Last {n_months} months ({start.isoformat()} → {end.isoformat()})"
        )
        return _period_result(start, end, True, max_d, label=label)

    # this month / so far / MTD
    so_far = any(
        p in text
        for p in ("so far", "mtd", "month to date", "to date", "till date", "until now")
    )
    this_month = "this month" in text or so_far

    # Named month
    year = max_d.year
    if re.search(r"\b(last|previous)\s+year\b", text) or re.search(
        r"\b(year\s+ago|prior\s+year)\b", text
    ):
        year = max_d.year - 1
    year_m = re.search(r"(20\d{2})", text)
    if year_m:
        year = int(year_m.group(1))
    month_num = None
    for name, num in MONTH_NAMES.items():
        if re.search(rf"\b{name}\b", text):
            month_num = num
            break

    if month_num is None and this_month:
        month_num = max_d.month
        year = max_d.year

    if month_num is not None:
        start = date(year, month_num, 1)
        month_end = date(year, month_num, calendar.monthrange(year, month_num)[1])
        # Partial if "so far" OR asking about the month that contains max_d and max_d < month_end
        if so_far or (max_d.year == year and max_d.month == month_num and max_d < month_end):
            end = min(max_d, month_end) if max_d.year == year and max_d.month == month_num else month_end
            # If so far but max_d is not in that month, use month_end (completed) unless future
            if so_far and max_d.year == year and max_d.month == month_num:
                end = max_d
                partial = True
            elif so_far and (year, month_num) > (max_d.year, max_d.month):
                end = max_d
                partial = True
                start = date(max_d.year, max_d.month, 1)
            else:
                partial = end < month_end and start.month == end.month
        else:
            end = month_end
            # If this is the current data month and data hasn't reached month-end, still partial
            if max_d.year == year and max_d.month == month_num and max_d < month_end:
                end = max_d
                partial = True
            else:
                partial = False
        label = start.strftime("%b %Y")
        if partial:
            label = f"{label} (through {end.isoformat()})"
        return _period_result(start, end, partial, max_d, label=label)

    # Fallback: last full calendar month before max_d's month if max_d is early, else max_d month MTD
    if not text:
        start = max_d.replace(day=1)
        month_end = date(max_d.year, max_d.month, calendar.monthrange(max_d.year, max_d.month)[1])
        partial = max_d < month_end
        return _period_result(
            start,
            max_d,
            partial,
            max_d,
            label=start.strftime("%b %Y") + (" MTD" if partial else ""),
        )

    return {
        "ok": False,
        "error": f"Could not parse period: {period!r}. Use ISO dates or e.g. 'July 2026', 'last month'.",
        "date_from": None,
        "date_to": None,
        "anchor_max_sales_date": max_d.isoformat(),
    }


def _period_result(
    start: date,
    end: date,
    partial: bool,
    max_d: date,
    *,
    label: str,
) -> dict[str, Any]:
    days_in_month = calendar.monthrange(start.year, start.month)[1]
    days_elapsed = (end - start).days + 1
    return {
        "ok": True,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "label": label,
        "partial_month": partial,
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "anchor_max_sales_date": max_d.isoformat(),
    }


def _auto_row_dimension(
    business_unit: str | None,
    oil_type: str | None,
    packing_category: str | None,
    *,
    business_units: list[str] | None = None,
) -> str:
    """Drill-down for rows.

    - Multiple business units → business_unit rows (comparison)
    - Packing set → product
    - Oil Type set → packing_category
    - Single Business Unit → packing_category (not oil type)
    - Else → business_unit
    """
    units = [u for u in (business_units or []) if u] or (
        [business_unit] if business_unit else []
    )
    if len(units) > 1:
        return "business_unit"
    if packing_category:
        return "product"
    if oil_type:
        return "packing_category"
    if units:
        return "packing_category"
    return "business_unit"


_VALID_ROW_DIMS = {
    "business_unit",
    "oil_type",
    "packing_category",
    "product",
    "city",
    "client_type",
}


def normalize_row_dimension(value: str | None) -> str | None:
    """Map spoken / tool row labels to a pivot row dimension."""
    if not value:
        return None
    raw = str(value).strip().lower().replace("-", " ").replace("_", " ")
    raw = re.sub(r"\s+", " ", raw).strip()
    aliases = {
        "business unit": "business_unit",
        "business units": "business_unit",
        "bu": "business_unit",
        "bus": "business_unit",
        "category 1": "business_unit",
        "oil type": "oil_type",
        "oil types": "oil_type",
        "oil": "oil_type",
        "category 2": "oil_type",
        "packing": "packing_category",
        "packing category": "packing_category",
        "packing categories": "packing_category",
        "pack": "packing_category",
        "pack type": "packing_category",
        "product category": "packing_category",
        "product categories": "packing_category",
        "pack category": "packing_category",
        "category 3": "packing_category",
        "product": "product",
        "products": "product",
        "sku": "product",
        "skus": "product",
        "sku wise": "product",
        "item": "product",
        "items": "product",
        "city": "city",
        "cities": "city",
        "client type": "client_type",
        "client": "client_type",
        "clients": "client_type",
    }
    if raw in aliases:
        return aliases[raw]
    compact = raw.replace(" ", "_")
    if compact in _VALID_ROW_DIMS:
        return compact
    return None


def _fetch_lines(
    *,
    date_from: str,
    date_to: str,
    city: str | None = None,
    business_unit: str | None = None,
    business_units: list[str] | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    client_type: str | None = None,
    party: str | None = None,
    excludes: dict[str, list[str]] | None = None,
) -> pd.DataFrame:
    """Pull line-level MT with taxonomy + geography + client type."""
    init_db()
    params: list[Any] = [date_from, date_to]
    where = ["s.date >= ?", "s.date <= ?"]
    if city:
        where.append("lower(trim(COALESCE(cl.city_filter, ''))) = lower(trim(?))")
        params.append(city)

    units = [u for u in (business_units or []) if u]
    if not units and business_unit:
        units = [business_unit]
    if len(units) == 1:
        where.append("lower(trim(COALESCE(c.category_1, ''))) = lower(trim(?))")
        params.append(units[0])
    elif len(units) > 1:
        placeholders = ",".join("?" for _ in units)
        where.append(
            f"lower(trim(COALESCE(c.category_1, ''))) IN ({placeholders})"
        )
        params.extend(u.lower().strip() for u in units)

    if oil_type:
        where.append("lower(trim(COALESCE(c.category_2, ''))) = lower(trim(?))")
        params.append(oil_type)
    if packing_category:
        where.append("lower(trim(COALESCE(c.packing_category, ''))) = lower(trim(?))")
        params.append(packing_category)
    if client_type:
        where.append(
            """
            lower(trim(COALESCE(
              NULLIF(trim(cl.type), ''),
              NULLIF(trim(s.client_type), ''),
              'Unmapped'
            ))) = lower(trim(?))
            """
        )
        params.append(client_type)
    if party:
        where.append(
            "lower(trim(replace(replace(s.party, '  ', ' '), '  ', ' '))) "
            "= lower(trim(replace(replace(?, '  ', ' '), '  ', ' ')))"
        )
        params.append(party)

    ex = excludes or {}
    if ex.get("city"):
        placeholders = ",".join("?" for _ in ex["city"])
        where.append(
            f"lower(trim(COALESCE(cl.city_filter, ''))) NOT IN ({placeholders})"
        )
        params.extend(v.lower().strip() for v in ex["city"])
    if ex.get("client_type"):
        placeholders = ",".join("?" for _ in ex["client_type"])
        where.append(
            f"""
            lower(trim(COALESCE(
              NULLIF(trim(cl.type), ''),
              NULLIF(trim(s.client_type), ''),
              'Unmapped'
            ))) NOT IN ({placeholders})
            """
        )
        params.extend(v.lower().strip() for v in ex["client_type"])
    if ex.get("business_unit"):
        placeholders = ",".join("?" for _ in ex["business_unit"])
        where.append(
            f"lower(trim(COALESCE(c.category_1, ''))) NOT IN ({placeholders})"
        )
        params.extend(v.lower().strip() for v in ex["business_unit"])
    if ex.get("oil_type"):
        placeholders = ",".join("?" for _ in ex["oil_type"])
        where.append(
            f"lower(trim(COALESCE(c.category_2, ''))) NOT IN ({placeholders})"
        )
        params.extend(v.lower().strip() for v in ex["oil_type"])
    if ex.get("packing_category"):
        placeholders = ",".join("?" for _ in ex["packing_category"])
        where.append(
            f"lower(trim(COALESCE(c.packing_category, ''))) NOT IN ({placeholders})"
        )
        params.extend(v.lower().strip() for v in ex["packing_category"])
    if ex.get("product"):
        placeholders = ",".join("?" for _ in ex["product"])
        where.append(f"lower(trim(COALESCE(s.product, ''))) NOT IN ({placeholders})")
        params.extend(v.lower().strip() for v in ex["product"])

    sql = f"""
    SELECT
      s.date,
      s.product,
      s.party,
      COALESCE(NULLIF(trim(c.category_1), ''), '(unmapped)') AS business_unit,
      COALESCE(NULLIF(trim(c.category_2), ''), '(unmapped)') AS oil_type,
      COALESCE(NULLIF(trim(c.packing_category), ''), '(unmapped)') AS packing_category,
      COALESCE(NULLIF(trim(cl.city_filter), ''), 'Unmapped') AS city,
      COALESCE(
        NULLIF(trim(cl.type), ''),
        NULLIF(trim(s.client_type), ''),
        'Unmapped'
      ) AS client_type,
      CASE
        WHEN COALESCE(s.mt_qty, 0) <> 0 THEN s.mt_qty
        WHEN lower(trim(COALESCE(s.unit,''))) IN ('kg','kgs')
          THEN COALESCE(s.qty,0)/1000.0
        WHEN lower(trim(COALESCE(s.unit,''))) IN
             ('mt','m.t','m.t.','ton','tons','tonne','tonnes')
          THEN COALESCE(s.qty,0)
        ELSE 0
      END AS mt
    FROM sales s
    {_PARTY_JOIN}
    WHERE {' AND '.join(where)}
    """
    with connect() as conn:
        return pd.read_sql_query(sql, conn, params=params)


def _pivot_mt(
    frame: pd.DataFrame,
    row_dim: str,
    col_dim: str,
) -> dict[str, Any]:
    if frame.empty:
        return {
            "row_dimension": row_dim,
            "column_dimension": col_dim,
            "columns": [],
            "rows": [],
            "column_totals": {},
            "grand_total_mt": 0.0,
        }

    pivot = (
        frame.groupby([row_dim, col_dim], as_index=False)["mt"]
        .sum()
        .pivot(index=row_dim, columns=col_dim, values="mt")
        .fillna(0.0)
    )
    # Column order: highest total first
    col_totals = pivot.sum(axis=0).sort_values(ascending=False)
    pivot = pivot.reindex(columns=list(col_totals.index))
    # Row order: highest total first (BU uses CATEGORY1_ORDER when applicable)
    row_totals = pivot.sum(axis=1)
    if row_dim == "business_unit":

        def _row_key(name: str) -> tuple:
            try:
                return (0, CATEGORY1_ORDER.index(name), -float(row_totals.get(name, 0)))
            except ValueError:
                return (1, 0, -float(row_totals.get(name, 0)))

        pivot = pivot.reindex(sorted(pivot.index, key=_row_key))
    else:
        pivot = pivot.reindex(row_totals.sort_values(ascending=False).index)

    pivot["Total"] = pivot.sum(axis=1)
    columns = [c for c in pivot.columns if c != "Total"] + ["Total"]
    rows = []
    for idx, row in pivot.iterrows():
        if float(mt_round(row["Total"])) == 0:
            continue
        entry: dict[str, Any] = {row_dim: str(idx)}
        for c in columns:
            entry[str(c)] = mt_round(row[c])
        rows.append(entry)

    # Column totals footer row (from remaining rows only)
    col_tot_map: dict[str, float] = {str(c): 0.0 for c in columns}
    for entry in rows:
        for c in columns:
            col_tot_map[str(c)] = float(col_tot_map[str(c)]) + float(entry.get(str(c)) or 0)
    col_tot_map = {k: mt_round(v) for k, v in col_tot_map.items()}
    total_row: dict[str, Any] = {row_dim: "Total", "row_kind": "total"}
    for c in columns:
        total_row[str(c)] = col_tot_map.get(str(c), 0.0)
    rows.append(total_row)

    return {
        "row_dimension": row_dim,
        "column_dimension": col_dim,
        "columns": [str(c) for c in columns],
        "rows": rows,
        "column_totals": col_tot_map,
        "grand_total_mt": col_tot_map.get("Total", 0.0),
        "markdown_hint": (
            f"HTML table: rows = {row_dim}, columns = {col_dim} "
            "(highest column totals first), row Total + column Total footer; "
            "zero-volume rows omitted."
        ),
    }


def _month_labels(end: date, months_back: int) -> list[str]:
    """Oldest → newest YYYY-MM labels ending at end's month."""
    labels: list[str] = []
    y, m = end.year, end.month
    for _ in range(max(1, months_back)):
        labels.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    labels.reverse()
    return labels


def _pivot_months(
    frame: pd.DataFrame,
    row_dim: str,
    month_labels: list[str],
) -> dict[str, Any]:
    """Rows × calendar months + Average column + Total footer."""
    if frame.empty:
        cols = list(month_labels) + ["Average", "Total"]
        return {
            "row_dimension": row_dim,
            "column_dimension": "month",
            "columns": cols,
            "rows": [],
            "column_totals": {c: 0.0 for c in cols},
            "grand_total_mt": 0.0,
            "month_labels": month_labels,
        }

    work = frame.copy()
    work["month"] = work["date"].astype(str).str.slice(0, 7)
    work = work[work["month"].isin(month_labels)]
    pivot = (
        work.groupby([row_dim, "month"], as_index=False)["mt"]
        .sum()
        .pivot(index=row_dim, columns="month", values="mt")
        .fillna(0.0)
    )
    for lab in month_labels:
        if lab not in pivot.columns:
            pivot[lab] = 0.0
    pivot = pivot.reindex(columns=month_labels)
    # Row order by total across months
    pivot["_sum"] = pivot.sum(axis=1)
    if row_dim == "business_unit":

        def _row_key(name: str) -> tuple:
            try:
                return (0, CATEGORY1_ORDER.index(name), -float(pivot.loc[name, "_sum"]))
            except ValueError:
                return (1, 0, -float(pivot.loc[name, "_sum"]))

        pivot = pivot.reindex(sorted(pivot.index, key=_row_key))
    else:
        pivot = pivot.sort_values("_sum", ascending=False)
    pivot = pivot.drop(columns=["_sum"])

    n = max(len(month_labels), 1)
    pivot["Average"] = pivot[month_labels].sum(axis=1) / n
    pivot["Total"] = pivot[month_labels].sum(axis=1)
    columns = list(month_labels) + ["Average", "Total"]

    rows = []
    for idx, row in pivot.iterrows():
        if float(mt_round(row["Total"])) == 0:
            continue
        entry: dict[str, Any] = {row_dim: str(idx)}
        for c in columns:
            entry[str(c)] = mt_round(row[c])
        rows.append(entry)

    col_tot_map: dict[str, float] = {c: 0.0 for c in columns}
    for entry in rows:
        for c in columns:
            col_tot_map[c] = float(col_tot_map[c]) + float(entry.get(c) or 0)
    n = max(len(month_labels), 1)
    if month_labels:
        col_tot_map["Average"] = round(
            sum(col_tot_map[c] for c in month_labels) / n, 3
        )
    col_tot_map = {k: mt_round(v) if k != "Average" else round(v, 3) for k, v in col_tot_map.items()}
    total_row: dict[str, Any] = {row_dim: "Total", "row_kind": "total"}
    for c in columns:
        total_row[c] = col_tot_map[c]
    rows.append(total_row)

    return {
        "row_dimension": row_dim,
        "column_dimension": "month",
        "columns": columns,
        "rows": rows,
        "column_totals": col_tot_map,
        "grand_total_mt": col_tot_map["Total"],
        "month_labels": month_labels,
        "markdown_hint": (
            f"Month-wise MT: rows={row_dim}, columns=months + Average + Total, "
            "with column totals footer; zero-volume rows omitted."
        ),
    }


# Leaf row dim → parent layers shown as leading columns (markdown "merge" via blanks)
_ROW_HIERARCHY: dict[str, list[str]] = {
    "packing_category": ["business_unit", "packing_category"],
    "product": ["business_unit", "packing_category", "product"],
}

_ROW_HEADER_LABELS = {
    "business_unit": "Business Unit",
    "packing_category": "Packing",
    "product": "SKU",
    "oil_type": "Oil Type",
    "city": "City",
    "client_type": "Client Type",
}


def _row_hierarchy(row_dim: str) -> list[str] | None:
    levels = _ROW_HIERARCHY.get(row_dim)
    return list(levels) if levels else None


def _bu_order_key(name: str, total: float) -> tuple:
    try:
        return (0, CATEGORY1_ORDER.index(name), -float(total))
    except ValueError:
        return (1, 0, -float(total))


def _ensure_level_cols(frame: pd.DataFrame, levels: list[str]) -> pd.DataFrame:
    work = frame.copy()
    for lv in levels:
        if lv not in work.columns:
            work[lv] = "(unmapped)"
        work[lv] = work[lv].fillna("(unmapped)").astype(str)
        work.loc[work[lv].str.strip() == "", lv] = "(unmapped)"
    return work


def _value_cells(
    grouped: pd.DataFrame,
    levels: list[str],
    key: tuple,
    ordered_cols: list[str],
    *,
    month_mode: bool,
) -> dict[str, float]:
    """Sum MT into display columns for one level-key combination."""
    mask = pd.Series(True, index=grouped.index)
    for lv, val in zip(levels, key):
        mask &= grouped[lv] == val
    subset = grouped.loc[mask]
    cells: dict[str, float] = {}
    for c in ordered_cols:
        cells[str(c)] = float(subset.loc[subset["__col"] == c, "mt"].sum()) if not subset.empty else 0.0
    total = sum(cells.values())
    if month_mode:
        n = max(len(ordered_cols), 1)
        cells["Average"] = total / n
    cells["Total"] = total
    return {k: mt_round(v) for k, v in cells.items()}


def _level_name_order(level: str, names: list[Any], totals: dict[Any, float]) -> list[str]:
    str_names = [str(n) for n in names]
    if level == "business_unit":
        return sorted(str_names, key=lambda n: _bu_order_key(n, float(totals.get(n, 0))))
    return sorted(str_names, key=lambda n: (-float(totals.get(n, 0)), n.lower()))


def _pivot_hierarchy(
    frame: pd.DataFrame,
    levels: list[str],
    *,
    col_dim: str,
    month_labels: list[str] | None = None,
) -> dict[str, Any]:
    """Pivot with parent layers as leading columns + group subtotals.

    Markdown cannot rowspan, so parent labels appear once then blank cells
    (visual merge). Subtotal rows after each non-leaf group.
    """
    levels = [lv for lv in levels if lv]
    leaf = levels[-1] if levels else "row"
    month_mode = month_labels is not None
    display_cols: list[str]
    if month_mode:
        display_cols = list(month_labels or []) + ["Average", "Total"]
    else:
        display_cols = []

    empty = {
        "row_dimension": leaf,
        "row_headers": levels,
        "hierarchical": True,
        "column_dimension": "month" if month_mode else col_dim,
        "columns": display_cols,
        "rows": [],
        "column_totals": {c: 0.0 for c in display_cols},
        "grand_total_mt": 0.0,
        "month_labels": month_labels,
        "markdown_hint": (
            f"Hierarchical MT: {' → '.join(levels)} × "
            f"{'month' if month_mode else col_dim}, with group subtotals."
        ),
    }
    if frame.empty or not levels:
        return empty

    work = _ensure_level_cols(frame, levels)
    if month_mode:
        work["__col"] = work["date"].astype(str).str.slice(0, 7)
        work = work[work["__col"].isin(month_labels or [])]
        ordered_cols = list(month_labels or [])
    else:
        work["__col"] = work[col_dim].fillna("Unmapped").astype(str)
        ordered_cols = list(
            work.groupby("__col")["mt"].sum().sort_values(ascending=False).index
        )
        display_cols = ordered_cols + ["Total"]
        empty["columns"] = display_cols

    if work.empty:
        empty["columns"] = display_cols
        return empty

    grouped = work.groupby(levels + ["__col"], as_index=False)["mt"].sum()
    combo = (
        grouped.groupby(levels, as_index=False)["mt"]
        .sum()
        .rename(columns={"mt": "_tot"})
    )

    def _ordered_leaves(prefix: list[str], depth: int) -> list[tuple[str, ...]]:
        if depth >= len(levels):
            return [tuple(prefix)]
        level = levels[depth]
        sub = combo
        for lv, val in zip(levels[:depth], prefix):
            sub = sub[sub[lv] == val]
        if sub.empty:
            return []
        totals = sub.groupby(level)["_tot"].sum()
        out: list[tuple[str, ...]] = []
        for name in _level_name_order(level, list(totals.index), totals.to_dict()):
            out.extend(_ordered_leaves(prefix + [name], depth + 1))
        return out

    leaves = _ordered_leaves([], 0)
    # Drop leaf combos with zero volume so tables never show empty rows
    nonzero_leaves: list[tuple[str, ...]] = []
    leaf_cells: dict[tuple[str, ...], dict[str, Any]] = {}
    for key in leaves:
        cells = _value_cells(grouped, levels, key, ordered_cols, month_mode=month_mode)
        if float(cells.get("Total") or 0) == 0:
            continue
        nonzero_leaves.append(key)
        leaf_cells[key] = cells
    leaves = nonzero_leaves
    rows_out: list[dict[str, Any]] = []

    def _prefix_cells(prefix: tuple[str, ...]) -> dict[str, float]:
        mask = pd.Series(True, index=grouped.index)
        for lv, val in zip(levels, prefix):
            mask &= grouped[lv] == val
        cells: dict[str, float] = {}
        for c in ordered_cols:
            cells[str(c)] = float(
                grouped.loc[mask & (grouped["__col"] == c), "mt"].sum()
            )
        total = sum(cells.values())
        if month_mode:
            cells["Average"] = total / max(len(ordered_cols), 1)
        cells["Total"] = total
        return {k: mt_round(v) for k, v in cells.items()}

    def _emit_subtotal(prev_key: tuple[str, ...], depth: int) -> None:
        """Subtotal for levels[depth] group (not for the leaf level)."""
        if depth >= len(levels) - 1:
            return
        prefix = prev_key[: depth + 1]
        cells = _prefix_cells(prefix)
        if float(cells.get("Total") or 0) == 0:
            return
        entry: dict[str, Any] = {lv: "" for lv in levels}
        entry[levels[depth]] = f"{prefix[depth]} Total"
        entry["row_kind"] = f"subtotal_{levels[depth]}"
        entry.update(cells)
        rows_out.append(entry)

    for i, key in enumerate(leaves):
        if i > 0:
            prev = leaves[i - 1]
            diff_d = next(
                (d for d in range(len(levels)) if key[d] != prev[d]),
                None,
            )
            if diff_d is not None:
                for close_d in range(len(levels) - 2, diff_d - 1, -1):
                    _emit_subtotal(prev, close_d)

        cells = leaf_cells[key]
        entry = {lv: "" for lv in levels}
        for d, lv in enumerate(levels):
            show = i == 0 or key[: d + 1] != leaves[i - 1][: d + 1]
            entry[lv] = key[d] if show else ""
        entry["row_kind"] = "leaf"
        entry.update(cells)
        rows_out.append(entry)

    if leaves:
        for close_d in range(len(levels) - 2, -1, -1):
            _emit_subtotal(leaves[-1], close_d)

    grand_cells: dict[str, float] = {}
    for c in ordered_cols:
        grand_cells[str(c)] = float(grouped.loc[grouped["__col"] == c, "mt"].sum())
    grand_tot = sum(grand_cells.values())
    if month_mode:
        grand_cells["Average"] = grand_tot / max(len(ordered_cols), 1)
    grand_cells["Total"] = grand_tot
    total_row: dict[str, Any] = {
        levels[0]: "Total",
        "row_kind": "total",
        **{k: mt_round(v) for k, v in grand_cells.items()},
    }
    for lv in levels[1:]:
        total_row[lv] = ""
    rows_out.append(total_row)

    col_tot_map = {k: mt_round(v) for k, v in grand_cells.items()}
    return {
        "row_dimension": leaf,
        "row_headers": levels,
        "hierarchical": True,
        "column_dimension": "month" if month_mode else col_dim,
        "columns": display_cols if month_mode else (ordered_cols + ["Total"]),
        "rows": rows_out,
        "column_totals": col_tot_map,
        "grand_total_mt": mt_round(grand_tot),
        "month_labels": month_labels,
        "markdown_hint": (
            f"Hierarchical MT: {' → '.join(levels)}; parent cells blank after first "
            "row of each group; subtotals per parent group."
        ),
    }


def _resolve_row_levels(
    row_dim: str,
    *,
    row_groups: list[str] | None = None,
) -> list[str]:
    """Leading group dims + base hierarchy for the leaf row dimension."""
    levels: list[str] = []
    for g in row_groups or []:
        g_n = normalize_row_dimension(g) or str(g).strip()
        if g_n and g_n not in levels:
            levels.append(g_n)
    base = _ROW_HIERARCHY.get(row_dim)
    if base:
        for b in base:
            if b not in levels:
                levels.append(b)
    elif row_dim and row_dim not in levels:
        levels.append(row_dim)
    return levels


def _build_pivot(
    frame: pd.DataFrame,
    row_dim: str,
    col_dim: str,
    *,
    month_labels: list[str] | None = None,
    row_groups: list[str] | None = None,
) -> dict[str, Any]:
    """Flat or hierarchical pivot depending on leaf row dimension / groups."""
    levels = _resolve_row_levels(row_dim, row_groups=row_groups)
    if len(levels) > 1:
        return _pivot_hierarchy(
            frame, levels, col_dim=col_dim, month_labels=month_labels
        )
    # Single level — use classic flat pivots (city / client_type / BU alone)
    leaf = levels[0] if levels else row_dim
    if month_labels is not None:
        return _pivot_months(frame, leaf, month_labels)
    return _pivot_mt(frame, leaf, col_dim)


def _ams_by_row(
    *,
    row_dim: str,
    as_of: date,
    city: str | None,
    business_unit: str | None,
    business_units: list[str] | None = None,
    oil_type: str | None,
    packing_category: str | None,
    client_type: str | None = None,
) -> dict[str, float]:
    """Mean of the three prior full calendar months' MT by row dimension."""
    ranges = _prior_three_month_ranges(as_of)
    monthly: list[dict[str, float]] = []
    keys: set[str] = set()
    for start, end in ranges:
        frame = _fetch_lines(
            date_from=start.isoformat(),
            date_to=end.isoformat(),
            city=city,
            business_unit=business_unit,
            business_units=business_units,
            oil_type=oil_type,
            packing_category=packing_category,
            client_type=client_type,
        )
        if frame.empty:
            monthly.append({})
            continue
        grouped = frame.groupby(row_dim)["mt"].sum()
        totals = {str(k): float(v) for k, v in grouped.items()}
        keys.update(totals)
        monthly.append(totals)
    return {
        key: sum(m.get(key, 0.0) for m in monthly) / 3.0 for key in keys
    }


def _trend_table(
    period_frame: pd.DataFrame,
    *,
    row_dim: str,
    period: dict[str, Any],
    city: str | None,
    business_unit: str | None,
    business_units: list[str] | None = None,
    oil_type: str | None,
    packing_category: str | None,
    client_type: str | None = None,
) -> dict[str, Any]:
    as_of = date.fromisoformat(period["date_to"])
    ams = _ams_by_row(
        row_dim=row_dim,
        as_of=as_of.replace(day=1),
        city=city,
        business_unit=business_unit,
        business_units=business_units,
        oil_type=oil_type,
        packing_category=packing_category,
        client_type=client_type,
    )
    volume = (
        period_frame.groupby(row_dim)["mt"].sum().to_dict()
        if not period_frame.empty
        else {}
    )
    keys = sorted(
        set(volume) | set(ams),
        key=lambda k: (-float(volume.get(k, 0.0)), str(k)),
    )
    partial = bool(period.get("partial_month"))
    days_elapsed = int(period.get("days_elapsed") or 0)
    days_in_month = int(period.get("days_in_month") or 30)
    rows = []
    for key in keys:
        vol = float(volume.get(key, 0.0))
        ams_v = float(ams.get(key, 0.0))
        entry: dict[str, Any] = {
            row_dim: str(key),
            "volume_mt": round(vol, 3),
            "ams_mt": round(ams_v, 3),
        }
        if partial:
            expected = (days_elapsed / days_in_month) * ams_v if days_in_month else None
            entry["expected_mt"] = (
                round(expected, 3) if expected is not None else None
            )
            entry["pct_vs_expected"] = (
                round(pct_change(vol, expected), 1)
                if expected not in (None, 0)
                else None
            )
            entry["note"] = (
                f"Expected = {days_elapsed}/{days_in_month} × AMS"
            )
        else:
            entry["pct_vs_ams"] = (
                round(pct_change(vol, ams_v), 1) if ams_v else None
            )
            entry["note"] = "Full month — AMS is the expected sale"
        rows.append(entry)

    columns = [row_dim, "volume_mt", "ams_mt"]
    if partial:
        columns.extend(["expected_mt", "pct_vs_expected"])
    else:
        columns.append("pct_vs_ams")

    # Column totals footer
    tot: dict[str, Any] = {row_dim: "Total"}
    tot["volume_mt"] = round(sum(float(r["volume_mt"]) for r in rows), 3)
    tot["ams_mt"] = round(sum(float(r["ams_mt"]) for r in rows), 3)
    if partial:
        exp_vals = [float(r["expected_mt"]) for r in rows if r.get("expected_mt") is not None]
        tot["expected_mt"] = round(sum(exp_vals), 3) if exp_vals else None
        tot["pct_vs_expected"] = (
            round(pct_change(tot["volume_mt"], tot["expected_mt"]), 1)
            if tot.get("expected_mt")
            else None
        )
    else:
        tot["pct_vs_ams"] = (
            round(pct_change(tot["volume_mt"], tot["ams_mt"]), 1)
            if tot["ams_mt"]
            else None
        )
    rows.append(tot)

    return {
        "row_dimension": row_dim,
        "partial_month": partial,
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "ams_definition": (
            "AMS = average of the three full calendar months before this month, "
            f"same filters (city={city!r}, business_unit={business_unit!r}, "
            f"business_units={business_units!r}, client_type={client_type!r})"
        ),
        "columns": columns,
        "rows": rows,
        "markdown_hint": (
            "Trend table: Volume (MT), AMS, "
            + (
                "Expected (= days_elapsed/days_in_month × AMS), % vs Expected"
                if partial
                else "% vs AMS (no Expected column — AMS is expected for a full month)"
            )
            + "; includes Total footer row."
        ),
    }


def _shift_date_year(d: date, years: int = -1) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        # Feb 29 → Feb 28
        return d.replace(year=d.year + years, day=28)


def _yoy_period_from(period_info: dict[str, Any]) -> dict[str, Any]:
    """Same calendar span one year earlier (preserves partial-month day range)."""
    d0 = date.fromisoformat(str(period_info["date_from"])[:10])
    d1 = date.fromisoformat(str(period_info["date_to"])[:10])
    c0, c1 = _shift_date_year(d0, -1), _shift_date_year(d1, -1)
    return {
        "ok": True,
        "date_from": c0.isoformat(),
        "date_to": c1.isoformat(),
        "label": f"{c0.strftime('%b %Y')} ({c0.isoformat()} → {c1.isoformat()})",
        "partial_month": period_info.get("partial_month"),
        "days_elapsed": period_info.get("days_elapsed"),
        "days_in_month": calendar.monthrange(c1.year, c1.month)[1],
    }


def _is_matrix_leaf_row(row: dict[str, Any], row_key: str) -> bool:
    kind = row.get("row_kind")
    if kind:
        return kind == "leaf"
    label = str(row.get(row_key) or "").strip().lower()
    return bool(label) and label != "total"


def _is_matrix_bold_row(row: dict[str, Any], row_key: str) -> bool:
    kind = str(row.get("row_kind") or "")
    if kind == "total" or kind.startswith("subtotal_"):
        return True
    return str(row.get(row_key) or "").strip().lower() == "total"


def _matrix_total_mt(matrix: dict[str, Any]) -> float:
    if "grand_total_mt" in matrix and matrix["grand_total_mt"] is not None:
        return float(matrix["grand_total_mt"])
    row_key = str(matrix.get("row_dimension") or "")
    headers = list(matrix.get("row_headers") or [])
    for row in matrix.get("rows") or []:
        if row.get("row_kind") == "total":
            return float(row.get("Total") or 0.0)
        if str(row.get(row_key) or "").strip().lower() == "total":
            return float(row.get("Total") or 0.0)
        if headers and str(row.get(headers[0]) or "").strip().lower() == "total":
            return float(row.get("Total") or 0.0)
    return 0.0


def _yoy_pct(current: float, prior: float) -> float | None:
    raw = pct_change(current, prior)
    if raw is None:
        if current and not prior:
            return None  # new vs zero prior — leave blank
        return None
    return round(float(raw), 1)


def _yoy_breakdown_table(
    current: dict[str, Any],
    prior: dict[str, Any],
    *,
    axis: str,
) -> dict[str, Any]:
    """Build Current / Prior / YoY % table by row labels or by column labels."""
    row_key = str(current.get("row_dimension") or prior.get("row_dimension") or "row")
    if axis == "row":
        cur_map: dict[str, float] = {}
        pri_map: dict[str, float] = {}
        for src, dest in ((current, cur_map), (prior, pri_map)):
            for row in src.get("rows") or []:
                if not _is_matrix_leaf_row(row, row_key):
                    continue
                label = str(row.get(row_key) or "").strip()
                if not label:
                    continue
                dest[label] = float(row.get("Total") or 0.0)
        labels = list(dict.fromkeys([*cur_map.keys(), *pri_map.keys()]))
        labels.sort(
            key=lambda x: (-(cur_map.get(x, 0.0) + pri_map.get(x, 0.0)), x.lower())
        )
        rows = []
        for lab in labels:
            c = cur_map.get(lab, 0.0)
            p = pri_map.get(lab, 0.0)
            rows.append(
                {
                    "segment": lab,
                    "current_mt": mt_round(c),
                    "prior_mt": mt_round(p),
                    "yoy_pct": _yoy_pct(c, p),
                }
            )
        c_tot = sum(cur_map.values())
        p_tot = sum(pri_map.values())
        rows.append(
            {
                "segment": "Total",
                "current_mt": mt_round(c_tot),
                "prior_mt": mt_round(p_tot),
                "yoy_pct": _yoy_pct(c_tot, p_tot),
            }
        )
        return {
            "row_dimension": "segment",
            "columns": ["segment", "current_mt", "prior_mt", "yoy_pct"],
            "rows": rows,
        }

    # axis == column
    cur_cols = [c for c in (current.get("columns") or []) if c != "Total"]
    pri_cols = [c for c in (prior.get("columns") or []) if c != "Total"]
    labels = list(dict.fromkeys([*cur_cols, *pri_cols]))

    def _col_total(matrix: dict[str, Any], col: str) -> float:
        headers = list(matrix.get("row_headers") or [])
        for row in matrix.get("rows") or []:
            if row.get("row_kind") == "total":
                return float(row.get(col) or 0.0)
            lab = str(row.get(row_key) or "").strip().lower()
            if lab == "total":
                return float(row.get(col) or 0.0)
            if headers and str(row.get(headers[0]) or "").strip().lower() == "total":
                return float(row.get(col) or 0.0)
        return sum(
            float(r.get(col) or 0.0)
            for r in (matrix.get("rows") or [])
            if _is_matrix_leaf_row(r, row_key)
        )

    rows = []
    for lab in labels:
        c = _col_total(current, lab)
        p = _col_total(prior, lab)
        rows.append(
            {
                "segment": lab,
                "current_mt": mt_round(c),
                "prior_mt": mt_round(p),
                "yoy_pct": _yoy_pct(c, p),
            }
        )
    rows.sort(key=lambda r: (-(r["current_mt"] + r["prior_mt"]), str(r["segment"]).lower()))
    c_tot = sum(r["current_mt"] for r in rows)
    p_tot = sum(r["prior_mt"] for r in rows)
    rows.append(
        {
            "segment": "Total",
            "current_mt": mt_round(c_tot),
            "prior_mt": mt_round(p_tot),
            "yoy_pct": _yoy_pct(c_tot, p_tot),
        }
    )
    return {
        "row_dimension": "segment",
        "columns": ["segment", "current_mt", "prior_mt", "yoy_pct"],
        "rows": rows,
    }


def _yoy_table_to_markdown(table: dict[str, Any], title_segment: str) -> str:
    lines = [
        f"| {title_segment} | Current (MT) | Prior (MT) | YoY % |",
        "| --- | --- | --- | --- |",
    ]
    for row in table.get("rows") or []:
        seg = str(row.get("segment") or "").replace("|", "/")
        yoy = row.get("yoy_pct")
        yoy_s = f"{yoy:+.1f}%" if isinstance(yoy, (int, float)) else "—"
        is_tot = seg.strip().lower() == "total"
        if is_tot:
            lines.append(
                f"| **{seg}** | **{row.get('current_mt')}** | "
                f"**{row.get('prior_mt')}** | **{yoy_s}** |"
            )
        else:
            lines.append(
                f"| {seg} | {row.get('current_mt')} | {row.get('prior_mt')} | {yoy_s} |"
            )
    return "\n".join(lines) + "\n"


def query_sales(
    *,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    city: str | None = None,
    business_unit: str | None = None,
    business_units: list[str] | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    client_type: str | None = None,
    party: str | None = None,
    columns: str = "client_type",
    months_back: int = 6,
    mode: str = "matrix",
    row_dimension: str | None = None,
    row_groups: list[str] | None = None,
    clear_filters: list[str] | None = None,
    lock_columns: bool = False,
    excludes: dict[str, list[str]] | None = None,
    prior_spec: dict[str, Any] | None = None,
    compare: str | None = None,
) -> dict[str, Any]:
    """One-shot sales answer builder for the chatbot.

    Row drill-down (auto, unless ``row_dimension`` override):
      no BU → Business Unit
      one BU → Packing Category
      multiple BUs → Business Unit (comparison)
      Oil Type set → Packing; Packing set → Product

    Explicit ``row_dimension`` (follow-ups): business_unit | oil_type |
    packing_category | product | city | client_type — keeps prior filters/columns.

    ``row_groups``: optional leading group dims (e.g. city above packing).

    ``clear_filters``: drop inherited filters when a follow-up promotes that
    dimension to rows/columns (e.g. group by city clears city=Lahore).

    ``excludes``: drop specific values from the result (e.g. city=Lahore,
    client_type=Eva Distributors) while keeping the same table grain.

    ``party``: filter to one sales party / client name (exact match after resolve).
    When set, prior city/client_type filters are not inherited.

    columns: client_type | city | month
      month → last ``months_back`` months as columns + Average

    client_type: filter to one Client Type (aliases resolved), e.g. Imtiaz Store.

    prior_spec: previous table_spec for follow-ups like "add Eva Bulk" /
    "show by product" / "SKU wise" / "group by city" / "remove Lahore".

    compare: ``yoy`` / ``same_period_last_year`` — same filters & grain vs
    the same calendar span one year earlier (partial months keep day range).
    """
    # Merge follow-up additions into prior filters
    units: list[str] = []
    for u in business_units or []:
        nu = _normalize_business_unit(u)
        if nu and nu not in units:
            units.append(nu)
    bu_one = _normalize_business_unit(business_unit)
    if bu_one and bu_one not in units:
        units.append(bu_one)

    oil = normalize_oil_type((oil_type or "").strip() or None)
    pack = normalize_packing_category((packing_category or "").strip() or None)
    city_f = (city or "").strip() or None
    ctype = normalize_client_type((client_type or "").strip() or None)
    party_f = (party or "").strip() or None
    col = (columns or "client_type").strip().lower().replace(" ", "_")
    mb = int(months_back or 6)
    row_override = normalize_row_dimension(row_dimension)
    groups: list[str] = []
    for g in row_groups or []:
        ng = normalize_row_dimension(g) or str(g).strip()
        if ng and ng not in groups:
            groups.append(ng)
    clear = {
        (normalize_row_dimension(c) or str(c).strip().lower().replace(" ", "_"))
        for c in (clear_filters or [])
        if c
    }
    ex_map: dict[str, list[str]] = {}
    for dim, vals in (excludes or {}).items():
        key = normalize_row_dimension(dim) or str(dim).strip()
        if not key:
            continue
        bucket = ex_map.setdefault(key, [])
        for v in vals or []:
            vs = str(v).strip()
            if vs and vs not in bucket:
                bucket.append(vs)

    if prior_spec and not party_f:
        # Carry forward dimensions; merge new business units
        # (skip when a specific party is requested — prior city/type must not stick)
        prior_filters = prior_spec.get("filters") or {}
        prior_units = list(prior_spec.get("business_units") or [])
        if prior_filters.get("business_unit") and prior_filters["business_unit"] not in prior_units:
            prior_units.append(prior_filters["business_unit"])
        if "business_unit" not in clear:
            for u in prior_units:
                nu = _normalize_business_unit(u)
                if nu and nu not in units:
                    units.insert(0, nu)
        if not city_f and "city" not in clear:
            city_f = prior_filters.get("city")
        if not oil and "oil_type" not in clear:
            oil = prior_filters.get("oil_type") or None
        if not pack and "packing_category" not in clear:
            pack = prior_filters.get("packing_category") or None
        if not ctype and "client_type" not in clear:
            ctype = prior_filters.get("client_type") or None
        # Explicit columns from regroup / caller win over prior default
        if (
            not lock_columns
            and col in {"client_type", "auto", ""}
            and prior_spec.get("column_dimension")
        ):
            col = str(prior_spec["column_dimension"])
        if prior_spec.get("months_back"):
            mb = int(prior_spec["months_back"])
        if not period and not date_from and prior_spec.get("period_phrase"):
            period = prior_spec.get("period_phrase")
        if not period and not date_from and prior_spec.get("period"):
            date_from = (prior_spec["period"] or {}).get("date_from")
            date_to = (prior_spec["period"] or {}).get("date_to")
        if not groups and prior_spec.get("row_groups"):
            for g in prior_spec.get("row_groups") or []:
                ng = normalize_row_dimension(g) or str(g).strip()
                if ng and ng not in groups:
                    groups.append(ng)
        if not row_override and prior_spec.get("row_dimension"):
            # Keep prior row dim only when caller did not request a new one —
            # follow-ups that only add a BU should preserve rows; drill-downs
            # pass an explicit override.
            pass
        # Cumulative excludes (remove Lahore, remove distributors, …)
        for dim, vals in (prior_spec.get("excludes") or {}).items():
            key = normalize_row_dimension(dim) or str(dim).strip()
            if not key:
                continue
            bucket = ex_map.setdefault(key, [])
            for v in vals or []:
                vs = str(v).strip()
                if vs and vs not in bucket:
                    bucket.append(vs)

    elif prior_spec and party_f:
        # Named-party query: only reuse period from prior if caller omitted one
        if not period and not date_from and prior_spec.get("period_phrase"):
            period = prior_spec.get("period_phrase")
        if not period and not date_from and prior_spec.get("period"):
            date_from = (prior_spec["period"] or {}).get("date_from")
            date_to = (prior_spec["period"] or {}).get("date_to")

    # Apply clears after inherit (regroup promoted a filter to a dimension)
    if "city" in clear:
        city_f = None
    if "client_type" in clear:
        ctype = None
    if "oil_type" in clear:
        oil = None
    if "packing_category" in clear:
        pack = None
    if "business_unit" in clear:
        units = []
    if col in {"client", "clients", "clienttype", "type"}:
        col = "client_type"
    if col in {"cities"}:
        col = "city"
    if col in {"months", "monthly", "month_wise", "monthwise"}:
        col = "month"
    if col not in {"client_type", "city", "month"}:
        col = "client_type"

    # When filtering to one client type, client_type columns are useless → city
    if ctype and col == "client_type":
        col = "city"

    # Month-wise: date range = last N months ending at max sales date
    if col == "month":
        _, max_d = _sales_date_bounds()
        if max_d is None:
            return {"ok": False, "error": "No sales dates in database"}
        labels = _month_labels(max_d, mb)
        start_y, start_m = map(int, labels[0].split("-"))
        d0 = date(start_y, start_m, 1).isoformat()
        d1 = max_d.isoformat()
        period_info = {
            "ok": True,
            "date_from": d0,
            "date_to": d1,
            "label": f"Last {mb} months through {max_d.strftime('%b %Y')}",
            "partial_month": max_d.day
            < calendar.monthrange(max_d.year, max_d.month)[1],
            "days_elapsed": max_d.day,
            "days_in_month": calendar.monthrange(max_d.year, max_d.month)[1],
            "anchor_max_sales_date": max_d.isoformat(),
            "months_back": mb,
            "month_labels": labels,
        }
    else:
        period_info = resolve_period(period, date_from=date_from, date_to=date_to)
        if period_info.get("ok") is False or not period_info.get("date_from"):
            return {
                "ok": False,
                "error": period_info.get("error") or "Bad period",
                "period": period_info,
            }
        d0 = period_info["date_from"]
        d1 = period_info["date_to"]
        labels = []

    bu = units[0] if len(units) == 1 else None
    if row_override and row_override in _VALID_ROW_DIMS:
        row_dim = row_override
    else:
        row_dim = _auto_row_dimension(
            bu, oil, pack, business_units=units if len(units) > 1 else None
        )

    # Single-party views: client_type columns collapse → use city
    if party_f and col == "client_type":
        col = "city"

    frame = _fetch_lines(
        date_from=d0,
        date_to=d1,
        city=city_f,
        business_unit=bu,
        business_units=units if len(units) > 1 else None,
        oil_type=oil,
        packing_category=pack,
        client_type=ctype,
        party=party_f,
        excludes=ex_map or None,
    )

    if col == "month":
        primary = _build_pivot(
            frame, row_dim, "month", month_labels=labels, row_groups=groups or None
        )
    else:
        primary = _build_pivot(frame, row_dim, col, row_groups=groups or None)

    mode_norm = (mode or "matrix").strip().lower()
    if mode_norm in {"auto", "default"}:
        mode_norm = "matrix"
    # Month-wise custom tables stay matrix (not the 3-pack analytical)
    if col == "month":
        mode_norm = "matrix"

    compare_n = (compare or "").strip().lower().replace("-", "_").replace(" ", "_")
    want_yoy = compare_n in {
        "yoy",
        "year_over_year",
        "same_period_last_year",
        "last_year",
        "vs_last_year",
    }
    # Month-wise YoY is not supported as a dual month grid — fall back to totals only
    if want_yoy and col == "month":
        want_yoy = True  # still do headline + row YoY on month totals if needed later
        # For now: treat as packing/client style on full fetched frame totals via matrix of months is awkward
        # Skip dual month matrices; build YoY on row totals from current month pivot Average column isn't ideal.
        # Simpler: disable month-col YoY and just compare overall period totals via a single-column pivot.
        pass

    table_spec = {
        "period_phrase": period,
        "period": {
            "date_from": d0,
            "date_to": d1,
            "label": period_info.get("label"),
        },
        "filters": {
            "city": city_f,
            "business_unit": bu,
            "oil_type": oil,
            "packing_category": pack,
            "client_type": ctype,
            "party": party_f,
        },
        "business_units": units,
        "column_dimension": col,
        "row_dimension": row_dim,
        "row_groups": groups or None,
        "excludes": ex_map or None,
        "months_back": mb if col == "month" else None,
        "compare": "yoy" if want_yoy else None,
    }

    result: dict[str, Any] = {
        "ok": True,
        "metric": "mt",
        "period": period_info,
        "filters": table_spec["filters"],
        "business_units": units,
        "row_dimension": row_dim,
        "column_dimension": col,
        "excludes": ex_map or None,
        "matrix": primary,
        "table_spec": table_spec,
    }

    if want_yoy:
        compare_info = _yoy_period_from(period_info)
        prior_frame = _fetch_lines(
            date_from=compare_info["date_from"],
            date_to=compare_info["date_to"],
            city=city_f,
            business_unit=bu,
            business_units=units if len(units) > 1 else None,
            oil_type=oil,
            packing_category=pack,
            client_type=ctype,
            party=party_f,
            excludes=ex_map or None,
        )
        if col == "month":
            # Collapse to row totals vs prior-year same span (not month columns)
            prior_primary = _build_pivot(
                prior_frame, row_dim, "client_type", row_groups=groups or None
            )
            # Rebuild current as client_type for apples-to-apples when month was requested
            current_cmp = _build_pivot(
                frame, row_dim, "client_type", row_groups=groups or None
            )
            col_cmp = "client_type"
        else:
            prior_primary = _build_pivot(
                prior_frame, row_dim, col, row_groups=groups or None
            )
            current_cmp = primary
            col_cmp = col

        # Attach row_dimension for total helpers
        current_cmp = {**current_cmp, "row_dimension": row_dim}
        prior_primary = {**prior_primary, "row_dimension": row_dim}

        cur_tot = _matrix_total_mt(current_cmp)
        pri_tot = _matrix_total_mt(prior_primary)
        yoy_tot = _yoy_pct(cur_tot, pri_tot)

        yoy_by_row = _yoy_breakdown_table(current_cmp, prior_primary, axis="row")
        yoy_by_col = _yoy_breakdown_table(current_cmp, prior_primary, axis="column")

        result["mode"] = "yoy"
        result["compare_period"] = compare_info
        result["prior_matrix"] = prior_primary
        result["matrix"] = current_cmp
        result["column_dimension"] = col_cmp
        result["yoy_by_row"] = yoy_by_row
        result["yoy_by_col"] = yoy_by_col
        result["current_total_mt"] = mt_round(cur_tot)
        result["prior_total_mt"] = mt_round(pri_tot)
        result["yoy_pct"] = yoy_tot
        result["tables"] = [
            {
                "index": 1,
                "title": f"YoY by {row_dim}",
                "source": "yoy_by_row",
                "data": yoy_by_row,
            },
            {
                "index": 2,
                "title": f"YoY by {col_cmp}",
                "source": "yoy_by_col",
                "data": yoy_by_col,
            },
            {
                "index": 3,
                "title": f"Current {row_dim} × {col_cmp}",
                "source": "matrix",
                "data": current_cmp,
            },
            {
                "index": 4,
                "title": f"Prior year {row_dim} × {col_cmp}",
                "source": "prior_matrix",
                "data": prior_primary,
            },
        ]
        result["required_table_count"] = 4
        result["answer_markdown"] = render_sales_markdown(result)
        result["response_instructions"] = (
            "REQUIRED: Your entire reply MUST be the `answer_markdown` field verbatim. "
            "Do not rebuild or omit tables. Keep `table_spec` for follow-ups."
        )
        return result

    if mode_norm in {"analytical", "analysis", "how_are", "performance"} and col != "month":
        city_matrix = _build_pivot(frame, row_dim, "city")
        trend = _trend_table(
            frame,
            row_dim=row_dim,
            period=period_info,
            city=city_f,
            business_unit=bu,
            business_units=units if len(units) > 1 else None,
            oil_type=oil,
            packing_category=pack,
            client_type=ctype,
        )
        result["mode"] = "analytical"
        result["city_matrix"] = city_matrix
        result["trend"] = trend
        trend_cols = "Volume | AMS | " + (
            "Expected | % vs Expected"
            if period_info.get("partial_month")
            else "% vs AMS (no Expected — full month)"
        )
        tables = [
            {
                "index": 1,
                "title": "City-wise breakdown",
                "source": "city_matrix",
                "data": city_matrix,
            },
        ]
        if ctype:
            # Already filtered to one client type — show packing breakdown by city
            pack_matrix = _build_pivot(frame, "packing_category", "city")
            result["client_matrix"] = pack_matrix
            tables.append(
                {
                    "index": 2,
                    "title": f"Packing × city ({ctype})",
                    "source": "client_matrix",
                    "data": pack_matrix,
                }
            )
        else:
            client_matrix = _build_pivot(frame, row_dim, "client_type")
            result["client_matrix"] = client_matrix
            tables.append(
                {
                    "index": 2,
                    "title": "Client-type breakdown",
                    "source": "client_matrix",
                    "data": client_matrix,
                }
            )
        tables.append(
            {
                "index": 3,
                "title": f"Trend vs AMS ({trend_cols})",
                "source": "trend",
                "data": trend,
            }
        )
        result["tables"] = tables
        result["required_table_count"] = 3
    else:
        result["mode"] = "matrix"
        title = (
            f"{row_dim} × months (last {mb}) + Average"
            if col == "month"
            else f"{row_dim} × {col}"
        )
        result["tables"] = [
            {
                "index": 1,
                "title": title,
                "source": "matrix",
                "data": primary,
            }
        ]
        result["required_table_count"] = 1

    result["answer_markdown"] = render_sales_markdown(result)
    result["response_instructions"] = (
        "REQUIRED: Your entire reply MUST be the `answer_markdown` field verbatim. "
        "Do not rebuild or omit tables. Keep `table_spec` for follow-ups "
        "(e.g. user says 'add Eva Bulk to this table')."
    )
    return result


def prior_business_units(prior_spec: dict[str, Any] | None) -> list[str]:
    """Business units active on a previous sales table (empty = all BUs)."""
    if not prior_spec:
        return []
    units: list[str] = []
    for u in prior_spec.get("business_units") or []:
        nu = _normalize_business_unit(u)
        if nu and nu not in units:
            units.append(nu)
    pf = prior_spec.get("filters") or {}
    one = _normalize_business_unit(pf.get("business_unit"))
    if one and one not in units:
        units.append(one)
    return units


def check_segment_inclusion(
    *,
    prior_spec: dict[str, Any],
    segment: str,
    mode: str = "matrix",
) -> dict[str, Any]:
    """Answer 'does this include Bulk?' against the previous sales table.

    - If ``segment`` was already in the prior BU filter (or prior had no BU
      filter = all BUs), show that segment alone and say it **was included**.
    - Otherwise show the same city/client/period scope for ``segment`` and say
      it **was excluded** (so the user can then combine / include it).
    """
    target = _normalize_business_unit(segment)
    if not target:
        return {"ok": False, "error": "Could not resolve which segment to check"}

    prior_units = prior_business_units(prior_spec)
    # Empty prior BU list means the previous table covered all business units
    included = (not prior_units) or (target in prior_units)

    pf = prior_spec.get("filters") or {}
    col = prior_spec.get("column_dimension") or "client_type"
    mb = int(prior_spec.get("months_back") or 6)
    period = prior_spec.get("period_phrase")
    date_from = None
    date_to = None
    if not period and prior_spec.get("period"):
        date_from = (prior_spec["period"] or {}).get("date_from")
        date_to = (prior_spec["period"] or {}).get("date_to")

    sliced = query_sales(
        period=period,
        date_from=date_from,
        date_to=date_to,
        city=pf.get("city"),
        business_unit=target,
        oil_type=pf.get("oil_type"),
        packing_category=pf.get("packing_category"),
        client_type=pf.get("client_type"),
        columns=col,
        months_back=mb,
        mode=mode if mode in {"matrix", "analytical"} else "matrix",
    )
    if not sliced.get("ok"):
        return sliced

    total = mt_round(_matrix_total_mt(sliced.get("matrix") or {}))
    prior_label = (prior_spec.get("period") or {}).get("label") or period or "prior table"
    prior_desc = ", ".join(
        p
        for p in [
            ", ".join(prior_units) if prior_units else "all business units",
            pf.get("client_type"),
            pf.get("city"),
            prior_label,
        ]
        if p
    )

    if included:
        headline = (
            f"**Yes — {target} is included** in the previous answer "
            f"({prior_desc}).\n\n"
            f"Here is **{target} only** from that same scope "
            f"(**{total} MT** total):\n"
        )
        tip = (
            f"Say **combine / include {target}** if you want it merged with "
            "other units in one table."
        )
    else:
        headline = (
            f"**No — {target} was not included** in the previous answer "
            f"({prior_desc}).\n\n"
            f"Here are **{target}** sales for the **same filters** that were "
            f"excluded (**{total} MT** total):\n"
        )
        tip = (
            f"Say **combine the tables**, **add {target}**, or **include bulk** "
            "to merge this with the previous table."
        )

    body = sliced.get("answer_markdown") or ""
    # Drop the original one-liner context if present; keep tables + analysis
    lines = [headline, body.strip(), "", "### Next step", f"- {tip}", ""]
    out = dict(sliced)
    out["mode"] = "include_check"
    out["included"] = included
    out["checked_segment"] = target
    out["prior_business_units"] = prior_units
    out["answer_markdown"] = "\n".join(lines)
    # Keep a table_spec pointing at the *checked* segment so a later
    # "combine" can merge prior units + this segment.
    spec = dict(out.get("table_spec") or {})
    spec["include_check"] = {
        "included": included,
        "segment": target,
        "prior_business_units": prior_units,
        "prior_spec": {
            "period_phrase": prior_spec.get("period_phrase"),
            "period": prior_spec.get("period"),
            "filters": pf,
            "business_units": prior_units,
            "column_dimension": col,
            "row_dimension": prior_spec.get("row_dimension"),
            "months_back": prior_spec.get("months_back"),
        },
    }
    out["table_spec"] = spec
    out["response_instructions"] = (
        "REQUIRED: Use answer_markdown verbatim. "
        "If the user then says combine / add / include, merge prior + segment."
    )
    return out


def _md_escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", " ")


def _html_escape(value: object) -> str:
    text = str(value if value is not None else "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _rowspan_map(
    rows: list[dict[str, Any]], headers: list[str]
) -> list[dict[str, int]]:
    """rowspan per header cell; 0 means covered by a prior rowspan (omit <td>).

    Subtotal/total rows break a span so parent merges never swallow summary rows.
    """
    n = len(rows)
    spans: list[dict[str, int]] = [{h: 1 for h in headers} for _ in range(n)]

    def _summary(row: dict[str, Any]) -> bool:
        kind = str(row.get("row_kind") or "")
        if kind == "total" or kind.startswith("subtotal_"):
            return True
        return False

    for h in headers:
        i = 0
        while i < n:
            cell = str(rows[i].get(h) or "").strip()
            if not cell:
                # Empty summary cells stay as blank <td>; leaf empties are covered
                spans[i][h] = 1 if _summary(rows[i]) else 0
                i += 1
                continue
            j = i + 1
            while j < n and not str(rows[j].get(h) or "").strip():
                if _summary(rows[j]):
                    break
                j += 1
            spans[i][h] = j - i
            for k in range(i + 1, j):
                spans[k][h] = 0
            i = j
    return spans


def _matrix_row_css_class(row: dict[str, Any], row_key: str) -> str:
    kind = str(row.get("row_kind") or "")
    if kind == "total":
        return "eva-total"
    if kind.startswith("subtotal_"):
        return "eva-subtotal"
    label = str(row.get(row_key) or "").strip().lower()
    if label == "total":
        return "eva-total"
    for h in (
        "business_unit",
        "packing_category",
        "product",
        "city",
        "client_type",
        "oil_type",
    ):
        v = str(row.get(h) or "").strip()
        if v.lower() == "total":
            return "eva-total"
        if v.lower().endswith(" total"):
            return "eva-subtotal"
    return ""


def _matrix_to_markdown(matrix: dict[str, Any], row_key: str) -> str:
    """Render matrix as an HTML table (rowspan merges + bold subtotals/totals)."""
    columns = list(matrix.get("columns") or [])
    rows = list(matrix.get("rows") or [])
    if not columns:
        return "_No data._\n"

    headers = list(matrix.get("row_headers") or [])
    if matrix.get("hierarchical") and headers:
        head_labels = [
            _ROW_HEADER_LABELS.get(h, h.replace("_", " ").title()) for h in headers
        ]
        spans = _rowspan_map(rows, headers)
        lines = [
            '<div class="eva-mtx-wrap">',
            '<table class="eva-mtx">',
            "<thead><tr>",
        ]
        for lab in head_labels:
            lines.append(f"<th>{_html_escape(lab)}</th>")
        for c in columns:
            cls = ' class="num total-col"' if c == "Total" else ' class="num"'
            lines.append(f"<th{cls}>{_html_escape(c)}</th>")
        lines.append("</tr></thead><tbody>")
        for i, row in enumerate(rows):
            css = _matrix_row_css_class(row, row_key)
            lines.append(f'<tr class="{css}">' if css else "<tr>")
            for h in headers:
                sp = spans[i].get(h, 1)
                if sp == 0:
                    continue
                text = str(row.get(h, "") or "").strip()
                rs = f' rowspan="{sp}"' if sp > 1 else ""
                cls = ' class="dim"' if text else ""
                lines.append(f"<td{cls}{rs}>{_html_escape(text)}</td>")
            for key in columns:
                val = row.get(key, 0)
                if val is None or val == "":
                    text = ""
                elif isinstance(val, (int, float)):
                    text = mt_str(val)
                else:
                    text = _html_escape(val)
                cls = "num total-col" if key == "Total" else "num"
                lines.append(f'<td class="{cls}">{text}</td>')
            lines.append("</tr>")
        lines.append("</tbody></table></div>")
        return "\n".join(lines) + "\n"

    label_h = _ROW_HEADER_LABELS.get(row_key, row_key.replace("_", " ").title())
    lines = [
        '<div class="eva-mtx-wrap">',
        '<table class="eva-mtx">',
        "<thead><tr>",
        f"<th>{_html_escape(label_h)}</th>",
    ]
    for c in columns:
        cls = ' class="num total-col"' if c == "Total" else ' class="num"'
        lines.append(f"<th{cls}>{_html_escape(c)}</th>")
    lines.append("</tr></thead><tbody>")
    for row in rows:
        css = _matrix_row_css_class(row, row_key)
        lines.append(f'<tr class="{css}">' if css else "<tr>")
        lab = _html_escape(row.get(row_key, ""))
        lines.append(f'<td class="dim">{lab}</td>')
        for key in columns:
            val = row.get(key, 0)
            if isinstance(val, (int, float)):
                text = mt_str(val)
            elif val is None:
                text = "—"
            else:
                text = _html_escape(val)
            cls = "num total-col" if key == "Total" else "num"
            lines.append(f'<td class="{cls}">{text}</td>')
        lines.append("</tr>")
    lines.append("</tbody></table></div>")
    return "\n".join(lines) + "\n"



def _trend_to_markdown(trend: dict[str, Any]) -> str:
    row_key = str(trend.get("row_dimension") or "row")
    columns = list(trend.get("columns") or [])
    rows = list(trend.get("rows") or [])
    if not columns:
        return "_No trend data._\n"
    # Prefer friendly headers
    label = {
        row_key: row_key.replace("_", " ").title(),
        "volume_mt": "Volume (MT)",
        "ams_mt": "AMS (MT)",
        "expected_mt": "Expected (MT)",
        "pct_vs_expected": "% vs Expected",
        "pct_vs_ams": "% vs AMS",
    }
    header_cols = [label.get(c, c) for c in columns]
    header = "| " + " | ".join(_md_escape(c) for c in header_cols) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    lines = [header, sep]
    for row in rows:
        cells = []
        for c in columns:
            val = row.get(c)
            if val is None:
                cells.append("—")
            elif isinstance(val, float):
                if c.startswith("pct_"):
                    cells.append(f"{val:+.1f}%")
                elif c.endswith("_mt") or c in {"volume_mt", "ams_mt", "expected_mt"}:
                    cells.append(mt_str(val))
                else:
                    cells.append(mt_str(val) if abs(val) >= 1 or val == 0 else f"{val:.3f}")
            elif isinstance(val, int):
                cells.append(str(val))
            else:
                cells.append(_md_escape(val))
        lines.append("| " + " | ".join(cells) + " |")
    note = ""
    if trend.get("partial_month"):
        note = (
            f"\n_Partial month: Expected = "
            f"{trend.get('days_elapsed')}/{trend.get('days_in_month')} × AMS. "
            f"{trend.get('ams_definition', '')}_"
        )
    else:
        note = f"\n_Full month — AMS is the expected baseline. {trend.get('ams_definition', '')}_"
    return "\n".join(lines) + note + "\n"


def _filter_blurb(filters: dict[str, Any], period: dict[str, Any], units: list[str] | None = None, excludes: dict[str, list[str]] | None = None) -> str:
    bits = [str(period.get("label") or f"{period.get('date_from')} → {period.get('date_to')}")]
    if filters.get("party"):
        bits.append(f"party **{filters['party']}**")
    if filters.get("city"):
        bits.append(f"city **{filters['city']}**")
    if filters.get("client_type"):
        bits.append(f"Client Type **{filters['client_type']}**")
    show_units = units or ([filters["business_unit"]] if filters.get("business_unit") else [])
    if len(show_units) > 1:
        bits.append("Business Units **" + "**, **".join(show_units) + "**")
    elif show_units:
        bits.append(f"Business Unit **{show_units[0]}**")
    if filters.get("oil_type"):
        bits.append(f"Oil Type **{filters['oil_type']}**")
    if filters.get("packing_category"):
        bits.append(f"Packing **{filters['packing_category']}**")
    if excludes:
        for dim, vals in excludes.items():
            if vals:
                label = dim.replace("_", " ")
                bits.append(f"excl. {label} **" + "**, **".join(vals) + "**")
    return " · ".join(bits)


def _auto_insights(result: dict[str, Any]) -> list[str]:
    """Short analytical bullets — interpretation, not a restatement of the table."""
    tips: list[str] = []
    mode = result.get("mode")
    row_dim = str(result.get("row_dimension") or "row")
    col_dim = str(result.get("column_dimension") or "column")
    filters = result.get("filters") or {}

    if mode == "yoy":
        yoy = result.get("yoy_pct")
        cur = float(result.get("current_total_mt") or 0)
        pri = float(result.get("prior_total_mt") or 0)
        if isinstance(yoy, (int, float)):
            if yoy <= -20:
                tips.append(
                    f"Overall volume is sharply down YoY ({yoy:+.1f}%) — "
                    f"{mt_round(cur)} vs {mt_round(pri)} MT prior."
                )
            elif yoy < 0:
                tips.append(
                    f"Overall volume is soft YoY ({yoy:+.1f}%); "
                    "check whether the drop is concentrated in one packing or client type."
                )
            elif yoy >= 20:
                tips.append(
                    f"Strong YoY expansion ({yoy:+.1f}%) — "
                    "confirm it is broad-based, not one client spike."
                )
            else:
                tips.append(f"Overall YoY is roughly flat-to-up ({yoy:+.1f}%).")
        # Best / worst row movers
        row_yoy = result.get("yoy_by_row") or {}
        scored = [
            r
            for r in (row_yoy.get("rows") or [])
            if str(r.get("segment") or "").lower() != "total"
            and isinstance(r.get("yoy_pct"), (int, float))
            and (float(r.get("current_mt") or 0) + float(r.get("prior_mt") or 0)) > 0
        ]
        if scored:
            best = max(scored, key=lambda r: float(r["yoy_pct"]))
            worst = min(scored, key=lambda r: float(r["yoy_pct"]))
            tips.append(
                f"**{best['segment']}** leads YoY among {row_dim.replace('_', ' ')}s "
                f"({best['yoy_pct']:+.1f}%)."
            )
            if worst["segment"] != best["segment"]:
                tips.append(
                    f"**{worst['segment']}** is the main drag "
                    f"({worst['yoy_pct']:+.1f}% YoY)."
                )
        col_yoy = result.get("yoy_by_col") or {}
        cscored = [
            r
            for r in (col_yoy.get("rows") or [])
            if str(r.get("segment") or "").lower() != "total"
            and isinstance(r.get("yoy_pct"), (int, float))
        ]
        if cscored:
            top_c = max(cscored, key=lambda r: float(r["current_mt"] or 0))
            tips.append(
                f"**{top_c['segment']}** is still the largest {col_dim.replace('_', ' ')} "
                f"this period ({top_c['current_mt']} MT"
                + (
                    f", {top_c['yoy_pct']:+.1f}% YoY)."
                    if isinstance(top_c.get("yoy_pct"), (int, float))
                    else ")."
                )
            )
        return tips[:4]

    # Analytical trend insights
    trend = result.get("trend") or {}
    rows = list(trend.get("rows") or [])
    partial = bool(trend.get("partial_month"))
    pct_key = "pct_vs_expected" if partial else "pct_vs_ams"
    scored = [
        (r, r.get(pct_key))
        for r in rows
        if isinstance(r.get(pct_key), (int, float))
        and str(r.get(row_dim) or "").lower() != "total"
    ]
    if scored:
        best = max(scored, key=lambda x: float(x[1]))
        worst = min(scored, key=lambda x: float(x[1]))
        baseline = "expected (MTD×AMS)" if partial else "AMS"
        tips.append(
            f"**{best[0].get(row_dim)}** is furthest ahead of {baseline} "
            f"({best[1]:+.1f}%)."
        )
        if worst[0].get(row_dim) != best[0].get(row_dim):
            tips.append(
                f"**{worst[0].get(row_dim)}** is furthest behind {baseline} "
                f"({worst[1]:+.1f}%)."
            )

    # Matrix / client concentration
    matrix = result.get("matrix") or result.get("client_matrix") or {}
    mrows = [
        r
        for r in (matrix.get("rows") or [])
        if _is_matrix_leaf_row(r, str(matrix.get("row_dimension") or row_dim))
    ]
    grand = float(matrix.get("grand_total_mt") or 0)
    if not grand and mrows:
        grand = sum(float(r.get("Total") or 0) for r in mrows)
    if mrows and grand > 0:
        top = max(mrows, key=lambda r: float(r.get("Total") or 0))
        top_key = matrix.get("row_dimension") or row_dim
        top_name = top.get(top_key)
        top_mt = float(top.get("Total") or 0)
        share = 100.0 * top_mt / grand
        tips.append(
            f"**{top_name}** is {share:.0f}% of this view ({mt_round(top_mt)} of {mt_round(grand)} MT) "
            + ("— concentration risk if it slips." if share >= 50 else "— still the lead line.")
        )
        cols = [c for c in (matrix.get("columns") or []) if c not in {"Total", "Average"}]
        if cols and not filters.get("client_type"):
            # Column share of grand total from footer
            headers = list(matrix.get("row_headers") or [])
            footer = next(
                (
                    r
                    for r in (matrix.get("rows") or [])
                    if r.get("row_kind") == "total"
                    or str(r.get(top_key) or "").lower() == "total"
                    or (headers and str(r.get(headers[0]) or "").lower() == "total")
                ),
                None,
            )
            if footer:
                top_col = max(cols, key=lambda c: float(footer.get(c) or 0))
                col_mt = float(footer.get(top_col) or 0)
                if col_mt > 0:
                    tips.append(
                        f"**{top_col}** is the dominant {col_dim.replace('_', ' ')} "
                        f"({100.0 * col_mt / grand:.0f}% of total)."
                    )
        # Second row vs first — mix breadth
        if len(mrows) >= 2:
            second = sorted(mrows, key=lambda r: float(r.get("Total") or 0), reverse=True)[1]
            gap = top_mt - float(second.get("Total") or 0)
            if top_mt > 0 and gap / top_mt >= 0.4:
                tips.append(
                    f"Lead over **{second.get(top_key)}** is wide "
                    f"({mt_round(gap)} MT) — mix is top-heavy."
                )

    # Deduplicate while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for tip in tips:
        if tip not in seen:
            seen.add(tip)
            uniq.append(tip)
    return uniq[:4]


def render_sales_markdown(result: dict[str, Any]) -> str:
    """Deterministic markdown so the UI always shows every required table."""
    if not result.get("ok"):
        return f"Could not build sales answer: {result.get('error')}"

    period = result.get("period") or {}
    filters = result.get("filters") or {}
    row_dim = str(result.get("row_dimension") or "row")
    blurb = _filter_blurb(
        filters,
        period,
        result.get("business_units"),
        (result.get("table_spec") or {}).get("excludes") or result.get("excludes"),
    )
    parts = [f"Sales for {blurb} (MT).\n"]

    if result.get("mode") == "yoy":
        cmp = result.get("compare_period") or {}
        yoy = result.get("yoy_pct")
        yoy_s = f"{yoy:+.1f}%" if isinstance(yoy, (int, float)) else "—"
        parts = [
            f"YoY comparison — {blurb} vs **{cmp.get('label') or 'same period last year'}** (MT).\n",
            f"**Total:** {result.get('current_total_mt')} MT now vs "
            f"{result.get('prior_total_mt')} MT prior → **{yoy_s}**.\n",
            f"### 1. YoY by {row_dim.replace('_', ' ').title()}\n",
            _yoy_table_to_markdown(
                result.get("yoy_by_row") or {},
                row_dim.replace("_", " ").title(),
            ),
            f"### 2. YoY by {str(result.get('column_dimension') or 'column').replace('_', ' ').title()}\n",
            _yoy_table_to_markdown(
                result.get("yoy_by_col") or {},
                str(result.get("column_dimension") or "column").replace("_", " ").title(),
            ),
            f"### 3. Current — {row_dim.replace('_', ' ').title()} × "
            f"{str(result.get('column_dimension') or '').replace('_', ' ').title()}\n",
            _matrix_to_markdown(result.get("matrix") or {}, row_dim),
            f"### 4. Prior year — {row_dim.replace('_', ' ').title()} × "
            f"{str(result.get('column_dimension') or '').replace('_', ' ').title()}\n",
            _matrix_to_markdown(result.get("prior_matrix") or {}, row_dim),
        ]
        insights = _auto_insights(result)
        if insights:
            parts.append("### Analysis\n")
            parts.extend(f"- {t}" for t in insights)
            parts.append("")
        return "\n".join(parts).strip() + "\n"

    if result.get("mode") == "analytical":
        parts.append("### 1. City-wise breakdown\n")
        parts.append(
            _matrix_to_markdown(result.get("city_matrix") or {}, row_dim)
        )
        second_title = "Client-type breakdown"
        second_row = row_dim
        if filters.get("client_type"):
            second_title = f"Packing × city ({filters['client_type']})"
            second_row = "packing_category"
        parts.append(f"### 2. {second_title}\n")
        parts.append(
            _matrix_to_markdown(result.get("client_matrix") or {}, second_row)
        )
        parts.append("### 3. Trend vs AMS\n")
        parts.append(_trend_to_markdown(result.get("trend") or {}))
        insights = _auto_insights(result)
        if insights:
            parts.append("### Analysis\n")
            parts.extend(f"- {t}" for t in insights)
            parts.append("")
    else:
        col = str(result.get("column_dimension") or "column")
        matrix = result.get("matrix") or {}
        if matrix.get("hierarchical") and matrix.get("row_headers"):
            chain = " → ".join(
                _ROW_HEADER_LABELS.get(h, h.replace("_", " ").title())
                for h in matrix["row_headers"]
            )
            parts.append(
                f"### {chain} × {col.replace('_', ' ').title()}\n"
            )
        else:
            parts.append(
                f"### {row_dim.replace('_', ' ').title()} × {col.replace('_', ' ').title()}\n"
            )
        parts.append(_matrix_to_markdown(matrix, row_dim))
        insights = _auto_insights(result)
        if insights:
            parts.append("### Analysis\n")
            parts.extend(f"- {t}" for t in insights)
            parts.append("")

    return "\n".join(parts).strip() + "\n"


def query_price(
    *,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    city: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    client_type: str | None = None,
    product: str | None = None,
    product_query: str | None = None,
    include_price_fetch: bool = False,
    prior_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Average Rate (and optional Price Fetch) from sales lines.

    Rate = MT-weighted average of ``sales.rate``.
    Amount/kg = Incl GST/FED ÷ (MT × 1000).
    Price Fetch = (amount/kg − cost factor/kg) × maund factor, when factor_costs match.
    """
    oil = normalize_oil_type((oil_type or "").strip() or None)
    pack = normalize_packing_category((packing_category or "").strip() or None)
    city_f = (city or "").strip() or None
    ctype = normalize_client_type((client_type or "").strip() or None)
    bu = _normalize_business_unit(business_unit)
    exact_product = (product or "").strip() or None
    resolution = None

    if prior_spec:
        prior_filters = prior_spec.get("filters") or {}
        if not city_f:
            city_f = prior_filters.get("city")
        if not oil:
            oil = prior_filters.get("oil_type") or None
        if not pack:
            pack = prior_filters.get("packing_category") or None
        if not ctype:
            ctype = prior_filters.get("client_type") or None
        if not bu:
            bu = prior_filters.get("business_unit") or None
        if not exact_product:
            exact_product = prior_filters.get("product") or None
        if not period and not date_from and prior_spec.get("period_phrase"):
            period = prior_spec.get("period_phrase")
        if not period and not date_from and prior_spec.get("period"):
            date_from = (prior_spec["period"] or {}).get("date_from")
            date_to = (prior_spec["period"] or {}).get("date_to")
        if prior_spec.get("include_price_fetch"):
            include_price_fetch = True

    if not exact_product and product_query:
        from eva_dashboard.product_language import resolve_product_language

        resolution = resolve_product_language(product_query, limit=5)
        exact_product = resolution.get("top_product") or None
        # Soft fill oil/packing from resolution when not set
        top = (resolution.get("matches") or [None])[0] or {}
        if not oil and top.get("oil_type"):
            oil = top["oil_type"]
        if not pack and top.get("packing_category"):
            pack = top["packing_category"]
        if not bu and top.get("business_unit"):
            bu = top["business_unit"]

    period_info = resolve_period(period, date_from=date_from, date_to=date_to)
    if period_info.get("ok") is False or not period_info.get("date_from"):
        return {
            "ok": False,
            "error": period_info.get("error") or "Bad period",
            "period": period_info,
        }
    d0 = period_info["date_from"]
    d1 = period_info["date_to"]

    params: list[Any] = [d0, d1]
    where = ["s.date >= ?", "s.date <= ?"]
    if city_f:
        where.append("lower(trim(COALESCE(cl.city_filter, ''))) = lower(trim(?))")
        params.append(city_f)
    if bu:
        where.append("lower(trim(COALESCE(c.category_1, ''))) = lower(trim(?))")
        params.append(bu)
    if oil:
        where.append("lower(trim(COALESCE(c.category_2, ''))) = lower(trim(?))")
        params.append(oil)
    if pack:
        where.append("lower(trim(COALESCE(c.packing_category, ''))) = lower(trim(?))")
        params.append(pack)
    if ctype:
        where.append(
            """
            lower(trim(COALESCE(
              NULLIF(trim(cl.type), ''),
              NULLIF(trim(s.client_type), ''),
              'Unmapped'
            ))) = lower(trim(?))
            """
        )
        params.append(ctype)
    if exact_product:
        where.append("s.product = ?")
        params.append(exact_product)

    sql = f"""
    SELECT
      s.product,
      s.rate,
      s.mes_qty,
      s.incl_gst_fed_amount,
      s.basic_amount,
      COALESCE(NULLIF(trim(c.category_1), ''), '(unmapped)') AS business_unit,
      COALESCE(NULLIF(trim(c.category_2), ''), '(unmapped)') AS oil_type,
      COALESCE(NULLIF(trim(c.packing_category), ''), '(unmapped)') AS packing_category,
      COALESCE(
        NULLIF(trim(cl.type), ''),
        NULLIF(trim(s.client_type), ''),
        'Unmapped'
      ) AS client_type,
      CASE
        WHEN COALESCE(s.mt_qty, 0) <> 0 THEN s.mt_qty
        WHEN lower(trim(COALESCE(s.unit,''))) IN ('kg','kgs')
          THEN COALESCE(s.qty,0)/1000.0
        WHEN lower(trim(COALESCE(s.unit,''))) IN
             ('mt','m.t','m.t.','ton','tons','tonne','tonnes')
          THEN COALESCE(s.qty,0)
        ELSE 0
      END AS mt,
      fc.total_factor_cost,
      fc.unit AS cost_unit
    FROM sales s
    {_PARTY_JOIN}
    LEFT JOIN factor_costs fc
      ON lower(trim(fc.client_type)) = lower(trim(COALESCE(
           NULLIF(trim(cl.type), ''),
           NULLIF(trim(s.client_type), ''),
           ''
         )))
     AND lower(trim(fc.product)) = lower(trim(s.product))
    WHERE {' AND '.join(where)}
    """
    init_db()
    with connect() as conn:
        frame = pd.read_sql_query(sql, conn, params=params)

    if frame.empty:
        filters = {
            "city": city_f,
            "business_unit": bu,
            "oil_type": oil,
            "packing_category": pack,
            "client_type": ctype,
            "product": exact_product,
        }
        return {
            "ok": True,
            "period": period_info,
            "filters": filters,
            "lines": 0,
            "mt": 0.0,
            "avg_rate": None,
            "amount_per_kg": None,
            "price_fetch": None,
            "by_product": [],
            "resolution": resolution,
            "answer_markdown": (
                f"No sales lines for {_filter_blurb(filters, period_info)} "
                "— cannot compute Rate / Price Fetch.\n"
            ),
            "price_spec": {
                "period_phrase": period,
                "period": {"date_from": d0, "date_to": d1, "label": period_info.get("label")},
                "filters": filters,
                "include_price_fetch": include_price_fetch,
            },
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    mt_w = frame["mt"].fillna(0).astype(float)
    rate_w = frame["rate"]
    # Prefer MT weights; fall back to mes_qty for rate when MT is zero
    mes_w = frame["mes_qty"].fillna(0).astype(float) if "mes_qty" in frame.columns else mt_w
    rate_weights = mt_w.where(mt_w > 0, mes_w)
    avg_rate = weighted_avg(rate_w, rate_weights)

    incl = frame["incl_gst_fed_amount"].fillna(0).astype(float)
    total_incl = float(incl.sum())
    total_mt = float(mt_w.sum())
    total_kg = total_mt * 1000.0
    amount_per_kg = (total_incl / total_kg) if total_kg else None

    # Line-level price fetch then MT-weighted average
    pf_vals: list[float] = []
    pf_weights: list[float] = []
    for _, row in frame.iterrows():
        mt = float(row.get("mt") or 0)
        incl_v = float(row.get("incl_gst_fed_amount") or 0)
        kg = mt * 1000.0
        apk = (incl_v / kg) if kg else None
        cf = cost_factor_per_kg(row.get("total_factor_cost"), row.get("cost_unit"))
        if apk is None or cf is None:
            continue
        pf = price_fetch_per_maund(apk, cf)
        if pf is None:
            continue
        w = mt if mt > 0 else float(row.get("mes_qty") or 0)
        if w <= 0:
            continue
        pf_vals.append(float(pf))
        pf_weights.append(w)

    blended_pf = None
    if pf_vals:
        blended_pf = weighted_avg(
            pd.Series(pf_vals),
            pd.Series(pf_weights),
            allow_unweighted_fallback=False,
        )

    # By product breakdown
    by_product: list[dict[str, Any]] = []
    for product_name, grp in frame.groupby("product", sort=False):
        g_mt = float(grp["mt"].fillna(0).sum())
        g_weights = grp["mt"].fillna(0).astype(float)
        g_weights = g_weights.where(g_weights > 0, grp["mes_qty"].fillna(0).astype(float))
        g_rate = weighted_avg(grp["rate"], g_weights)
        g_incl = float(grp["incl_gst_fed_amount"].fillna(0).sum())
        g_kg = g_mt * 1000.0
        g_apk = (g_incl / g_kg) if g_kg else None
        entry: dict[str, Any] = {
            "product": str(product_name),
            "business_unit": str(grp["business_unit"].iloc[0]),
            "oil_type": str(grp["oil_type"].iloc[0]),
            "packing_category": str(grp["packing_category"].iloc[0]),
            "lines": int(len(grp)),
            "mt": round(g_mt, 3),
            "avg_rate": round(float(g_rate), 2) if g_rate is not None else None,
            "amount_per_kg": round(float(g_apk), 4) if g_apk is not None else None,
        }
        # product-level PF
        p_vals: list[float] = []
        p_w: list[float] = []
        for _, r in grp.iterrows():
            mt = float(r.get("mt") or 0)
            incl_v = float(r.get("incl_gst_fed_amount") or 0)
            kg = mt * 1000.0
            apk = (incl_v / kg) if kg else None
            cf = cost_factor_per_kg(r.get("total_factor_cost"), r.get("cost_unit"))
            if apk is None or cf is None:
                continue
            pf = price_fetch_per_maund(apk, cf)
            if pf is None:
                continue
            w = mt if mt > 0 else float(r.get("mes_qty") or 0)
            if w <= 0:
                continue
            p_vals.append(float(pf))
            p_w.append(w)
        if p_vals:
            entry["price_fetch"] = round(
                float(
                    weighted_avg(
                        pd.Series(p_vals),
                        pd.Series(p_w),
                        allow_unweighted_fallback=False,
                    )
                    or 0
                ),
                2,
            )
        else:
            entry["price_fetch"] = None
        by_product.append(entry)

    by_product.sort(key=lambda r: -float(r.get("mt") or 0))

    filters = {
        "city": city_f,
        "business_unit": bu,
        "oil_type": oil,
        "packing_category": pack,
        "client_type": ctype,
        "product": exact_product,
    }
    price_spec = {
        "period_phrase": period,
        "period": {"date_from": d0, "date_to": d1, "label": period_info.get("label")},
        "filters": filters,
        "include_price_fetch": include_price_fetch,
    }

    blurb = _filter_blurb(filters, period_info)
    lines_md = [
        f"Prices for {blurb}.\n",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Lines | {len(frame)} |",
        f"| Volume (MT) | {round(total_mt, 3)} |",
        f"| **Avg Rate** | "
        f"**{round(float(avg_rate), 2) if avg_rate is not None else '—'}** |",
        f"| Amount / kg (Incl GST/FED) | "
        f"{round(float(amount_per_kg), 4) if amount_per_kg is not None else '—'} |",
    ]
    if include_price_fetch:
        pf_txt = (
            round(float(blended_pf), 2) if blended_pf is not None else "— (no factor cost match)"
        )
        lines_md.append(f"| **Price Fetch** (per maund) | **{pf_txt}** |")
        lines_md.append(
            "\n_Price Fetch = (Incl GST/FED per kg − cost factor per kg) × "
            f"{MAUND_FACTOR_PRICE_FETCH:.4f} kg/maund "
            f"(Ltrs costs ÷ {LTR_TO_KG})._"
        )
    else:
        lines_md.append(
            "\n_Ask “what’s the Price Fetch?” for the recovery figure on the same scope._"
        )

    if len(by_product) > 1:
        lines_md.append("\n### By product\n")
        hdr = "| Product | Packing | MT | Avg Rate | Amount/kg |"
        if include_price_fetch:
            hdr += " Price Fetch |"
        lines_md.append(hdr)
        lines_md.append(
            "| --- | --- | --- | --- | --- |"
            + (" --- |" if include_price_fetch else "")
        )
        for row in by_product[:15]:
            cells = [
                str(row["product"]).replace("|", "/"),
                str(row["packing_category"]).replace("|", "/"),
                str(row["mt"]),
                str(row["avg_rate"] if row["avg_rate"] is not None else "—"),
                str(row["amount_per_kg"] if row["amount_per_kg"] is not None else "—"),
            ]
            if include_price_fetch:
                cells.append(
                    str(row["price_fetch"] if row["price_fetch"] is not None else "—")
                )
            lines_md.append("| " + " | ".join(cells) + " |")

    return {
        "ok": True,
        "period": period_info,
        "filters": filters,
        "lines": int(len(frame)),
        "mt": round(total_mt, 3),
        "avg_rate": round(float(avg_rate), 2) if avg_rate is not None else None,
        "amount_per_kg": round(float(amount_per_kg), 4) if amount_per_kg is not None else None,
        "price_fetch": round(float(blended_pf), 2) if blended_pf is not None else None,
        "include_price_fetch": include_price_fetch,
        "by_product": by_product,
        "resolution": resolution,
        "price_spec": price_spec,
        "answer_markdown": "\n".join(lines_md).strip() + "\n",
        "response_instructions": (
            "REQUIRED: Reply with `answer_markdown` verbatim. "
            "Keep `price_spec` for follow-ups like 'what's the Price Fetch?'."
        ),
    }