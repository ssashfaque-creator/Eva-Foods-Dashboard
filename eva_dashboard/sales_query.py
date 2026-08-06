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
        entry: dict[str, Any] = {row_dim: str(idx)}
        for c in columns:
            entry[str(c)] = round(float(row[c]), 3)
        rows.append(entry)

    # Column totals footer row
    col_tot_map = {str(c): round(float(col_totals.get(c, 0.0)), 3) for c in col_totals.index}
    col_tot_map["Total"] = round(float(pivot["Total"].sum()), 3)
    total_row: dict[str, Any] = {row_dim: "Total"}
    for c in columns:
        total_row[str(c)] = col_tot_map.get(str(c), 0.0)
    rows.append(total_row)

    return {
        "row_dimension": row_dim,
        "column_dimension": col_dim,
        "columns": [str(c) for c in columns],
        "rows": rows,
        "column_totals": col_tot_map,
        "grand_total_mt": col_tot_map["Total"],
        "markdown_hint": (
            f"Markdown table: rows = {row_dim}, columns = {col_dim} "
            "(highest column totals first), row Total + column Total footer."
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
        entry: dict[str, Any] = {row_dim: str(idx)}
        for c in columns:
            entry[str(c)] = round(float(row[c]), 3)
        rows.append(entry)

    col_tot_map: dict[str, float] = {}
    for c in month_labels:
        col_tot_map[c] = round(float(pivot[c].sum()), 3)
    col_tot_map["Average"] = round(sum(col_tot_map[c] for c in month_labels) / n, 3)
    col_tot_map["Total"] = round(float(pivot["Total"].sum()), 3)
    total_row: dict[str, Any] = {row_dim: "Total"}
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
            "with column totals footer."
        ),
    }


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
    columns: str = "client_type",
    months_back: int = 6,
    mode: str = "matrix",
    row_dimension: str | None = None,
    prior_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One-shot sales answer builder for the chatbot.

    Row drill-down (auto, unless ``row_dimension`` override):
      no BU → Business Unit
      one BU → Packing Category
      multiple BUs → Business Unit (comparison)
      Oil Type set → Packing; Packing set → Product

    Explicit ``row_dimension`` (follow-ups): business_unit | oil_type |
    packing_category | product — keeps prior filters/columns.

    columns: client_type | city | month
      month → last ``months_back`` months as columns + Average

    client_type: filter to one Client Type (aliases resolved), e.g. Imtiaz Store.

    prior_spec: previous table_spec for follow-ups like "add Eva Bulk" /
    "show by product" / "SKU wise".
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
    col = (columns or "client_type").strip().lower().replace(" ", "_")
    mb = int(months_back or 6)
    row_override = normalize_row_dimension(row_dimension)

    if prior_spec:
        # Carry forward dimensions; merge new business units
        prior_filters = prior_spec.get("filters") or {}
        prior_units = list(prior_spec.get("business_units") or [])
        if prior_filters.get("business_unit") and prior_filters["business_unit"] not in prior_units:
            prior_units.append(prior_filters["business_unit"])
        for u in prior_units:
            nu = _normalize_business_unit(u)
            if nu and nu not in units:
                units.insert(0, nu)
        if not city_f:
            city_f = prior_filters.get("city")
        if not oil:
            oil = prior_filters.get("oil_type") or None
        if not pack:
            pack = prior_filters.get("packing_category") or None
        if not ctype:
            ctype = prior_filters.get("client_type") or None
        if col in {"client_type", "auto", ""} and prior_spec.get("column_dimension"):
            col = str(prior_spec["column_dimension"])
        if prior_spec.get("months_back"):
            mb = int(prior_spec["months_back"])
        if not period and not date_from and prior_spec.get("period_phrase"):
            period = prior_spec.get("period_phrase")
        if not period and not date_from and prior_spec.get("period"):
            date_from = (prior_spec["period"] or {}).get("date_from")
            date_to = (prior_spec["period"] or {}).get("date_to")
        if not row_override and prior_spec.get("row_dimension"):
            # Keep prior row dim only when caller did not request a new one —
            # follow-ups that only add a BU should preserve rows; drill-downs
            # pass an explicit override.
            pass

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

    frame = _fetch_lines(
        date_from=d0,
        date_to=d1,
        city=city_f,
        business_unit=bu,
        business_units=units if len(units) > 1 else None,
        oil_type=oil,
        packing_category=pack,
        client_type=ctype,
    )

    if col == "month":
        primary = _pivot_months(frame, row_dim, labels)
    else:
        primary = _pivot_mt(frame, row_dim, col)

    mode_norm = (mode or "matrix").strip().lower()
    if mode_norm in {"auto", "default"}:
        mode_norm = "matrix"
    # Month-wise custom tables stay matrix (not the 3-pack analytical)
    if col == "month":
        mode_norm = "matrix"

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
        },
        "business_units": units,
        "column_dimension": col,
        "row_dimension": row_dim,
        "months_back": mb if col == "month" else None,
    }

    result: dict[str, Any] = {
        "ok": True,
        "metric": "mt",
        "period": period_info,
        "filters": table_spec["filters"],
        "business_units": units,
        "row_dimension": row_dim,
        "column_dimension": col,
        "matrix": primary,
        "table_spec": table_spec,
    }

    if mode_norm in {"analytical", "analysis", "how_are", "performance"} and col != "month":
        city_matrix = _pivot_mt(frame, row_dim, "city")
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
            pack_matrix = _pivot_mt(frame, "packing_category", "city")
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
            client_matrix = _pivot_mt(frame, row_dim, "client_type")
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


def _md_escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", " ")


def _matrix_to_markdown(matrix: dict[str, Any], row_key: str) -> str:
    columns = list(matrix.get("columns") or [])
    rows = list(matrix.get("rows") or [])
    if not columns:
        return "_No data._\n"
    header = "| " + " | ".join([row_key] + [_md_escape(c) for c in columns]) + " |"
    sep = "| " + " | ".join(["---"] * (len(columns) + 1)) + " |"
    lines = [header, sep]
    for row in rows:
        is_total = str(row.get(row_key, "")).strip().lower() == "total"
        cells = []
        for i, key in enumerate([row_key] + columns):
            if i == 0:
                val = row.get(row_key, "")
                text = _md_escape(val)
            else:
                val = row.get(key, 0)
                if isinstance(val, float):
                    text = f"{val:.3f}".rstrip("0").rstrip(".") if val else "0"
                elif val is None:
                    text = "—"
                else:
                    text = _md_escape(val)
            cells.append(f"**{text}**" if is_total else text)
        lines.append("| " + " | ".join(cells) + " |")
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
                else:
                    cells.append(f"{val:.3f}".rstrip("0").rstrip(".") if val else "0")
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


def _filter_blurb(filters: dict[str, Any], period: dict[str, Any], units: list[str] | None = None) -> str:
    bits = [str(period.get("label") or f"{period.get('date_from')} → {period.get('date_to')}")]
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
    return " · ".join(bits)


def _auto_insights(result: dict[str, Any]) -> list[str]:
    tips: list[str] = []
    trend = result.get("trend") or {}
    rows = list(trend.get("rows") or [])
    partial = bool(trend.get("partial_month"))
    pct_key = "pct_vs_expected" if partial else "pct_vs_ams"
    scored = [
        (r, r.get(pct_key))
        for r in rows
        if isinstance(r.get(pct_key), (int, float))
    ]
    if scored:
        best = max(scored, key=lambda x: float(x[1]))
        worst = min(scored, key=lambda x: float(x[1]))
        dim = trend.get("row_dimension", "line")
        tips.append(
            f"**{best[0].get(dim)}** is furthest ahead of "
            f"{'expected' if partial else 'AMS'} ({best[1]:+.1f}%)."
        )
        if worst[0].get(dim) != best[0].get(dim):
            tips.append(
                f"**{worst[0].get(dim)}** is furthest behind "
                f"({'expected' if partial else 'AMS'} {worst[1]:+.1f}%)."
            )
    client = result.get("client_matrix") or {}
    crow = (client.get("rows") or [None])[0]
    if crow:
        # largest non-total column
        cols = [c for c in (client.get("columns") or []) if c != "Total"]
        if cols:
            top_col = cols[0]
            tips.append(
                f"Largest client-type column: **{top_col}** "
                f"(sorted highest-first)."
            )
    return tips[:4]


def render_sales_markdown(result: dict[str, Any]) -> str:
    """Deterministic markdown so the UI always shows every required table."""
    if not result.get("ok"):
        return f"Could not build sales answer: {result.get('error')}"

    period = result.get("period") or {}
    filters = result.get("filters") or {}
    row_dim = str(result.get("row_dimension") or "row")
    blurb = _filter_blurb(filters, period, result.get("business_units"))
    parts = [f"Sales for {blurb} (MT).\n"]

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
            parts.append("### Insights\n")
            parts.extend(f"- {t}" for t in insights)
            parts.append("")
    else:
        col = str(result.get("column_dimension") or "column")
        parts.append(f"### {row_dim.replace('_', ' ').title()} × {col.replace('_', ' ').title()}\n")
        parts.append(_matrix_to_markdown(result.get("matrix") or {}, row_dim))

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