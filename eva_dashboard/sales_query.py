"""Structured sales query engine for the chatbot (pivots + analytical AMS)."""

from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Any

import pandas as pd

from eva_dashboard.categories import BUSINESS_UNIT_ALIASES
from eva_dashboard.data import CATEGORY1_ORDER, _prior_three_month_ranges, pct_change
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

    # this month / so far / MTD
    so_far = any(
        p in text
        for p in ("so far", "mtd", "month to date", "to date", "till date", "until now")
    )
    this_month = "this month" in text or so_far

    # Named month
    year = max_d.year
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
) -> str:
    """Drill-down: unspecified → BU; BU set → Oil Type; Oil Type set → Packing."""
    if packing_category:
        return "product"
    if oil_type:
        return "packing_category"
    if business_unit:
        return "oil_type"
    return "business_unit"


def _fetch_lines(
    *,
    date_from: str,
    date_to: str,
    city: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
) -> pd.DataFrame:
    """Pull line-level MT with taxonomy + geography + client type."""
    init_db()
    params: list[Any] = [date_from, date_to]
    where = ["s.date >= ?", "s.date <= ?"]
    if city:
        where.append("lower(trim(COALESCE(cl.city_filter, ''))) = lower(trim(?))")
        params.append(city)
    if business_unit:
        where.append("lower(trim(COALESCE(c.category_1, ''))) = lower(trim(?))")
        params.append(business_unit)
    if oil_type:
        where.append("lower(trim(COALESCE(c.category_2, ''))) = lower(trim(?))")
        params.append(oil_type)
    if packing_category:
        where.append("lower(trim(COALESCE(c.packing_category, ''))) = lower(trim(?))")
        params.append(packing_category)

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

    return {
        "row_dimension": row_dim,
        "column_dimension": col_dim,
        "columns": [str(c) for c in columns],
        "rows": rows,
        "column_totals": {
            str(c): round(float(col_totals.get(c, 0.0)), 3) for c in col_totals.index
        },
        "grand_total_mt": round(float(pivot["Total"].sum()), 3),
        "markdown_hint": (
            f"Markdown table: rows = {row_dim}, columns = {col_dim} "
            "(highest column totals first), plus Total."
        ),
    }


def _ams_by_row(
    *,
    row_dim: str,
    as_of: date,
    city: str | None,
    business_unit: str | None,
    oil_type: str | None,
    packing_category: str | None,
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
            oil_type=oil_type,
            packing_category=packing_category,
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
    oil_type: str | None,
    packing_category: str | None,
) -> dict[str, Any]:
    as_of = date.fromisoformat(period["date_to"])
    ams = _ams_by_row(
        row_dim=row_dim,
        as_of=as_of.replace(day=1),  # prior months relative to this month
        city=city,
        business_unit=business_unit,
        oil_type=oil_type,
        packing_category=packing_category,
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
            # Full month: AMS is the expected baseline
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

    return {
        "row_dimension": row_dim,
        "partial_month": partial,
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "ams_definition": (
            "AMS = average of the three full calendar months before this month, "
            f"same filters (city={city!r}, business_unit={business_unit!r})"
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
        ),
    }


def query_sales(
    *,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    city: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    columns: str = "client_type",
    mode: str = "auto",
) -> dict[str, Any]:
    """One-shot sales answer builder for the chatbot.

    Row drill-down (automatic):
      no BU → Business Unit rows
      BU set → Oil Type rows
      Oil Type set → Packing Category rows
      Packing set → Product rows

    Column default: client_type (Eva Distributors, …), highest total first.
    Pass columns='city' for a city-wise breakdown.

    mode:
      auto — analytical when Business Unit (or Oil Type) is set; else matrix
      analytical — city + client + AMS trend tables (all must be shown)
      matrix — single pivot only
    """
    bu = _normalize_business_unit(business_unit)
    oil = (oil_type or "").strip() or None
    pack = (packing_category or "").strip() or None
    city_f = (city or "").strip() or None

    period_info = resolve_period(period, date_from=date_from, date_to=date_to)
    if period_info.get("ok") is False or not period_info.get("date_from"):
        return {"ok": False, "error": period_info.get("error") or "Bad period", "period": period_info}

    d0 = period_info["date_from"]
    d1 = period_info["date_to"]
    row_dim = _auto_row_dimension(bu, oil, pack)

    col = (columns or "client_type").strip().lower().replace(" ", "_")
    if col in {"client", "clients", "clienttype", "type"}:
        col = "client_type"
    if col in {"cities"}:
        col = "city"
    if col not in {"client_type", "city"}:
        col = "client_type"

    frame = _fetch_lines(
        date_from=d0,
        date_to=d1,
        city=city_f,
        business_unit=bu,
        oil_type=oil,
        packing_category=pack,
    )

    primary = _pivot_mt(frame, row_dim, col)

    mode_norm = (mode or "auto").strip().lower()
    if mode_norm in {"auto", "", "default"}:
        # Business Unit / Oil Type scoped questions get full analytical pack
        mode_norm = "analytical" if (bu or oil) else "matrix"

    result: dict[str, Any] = {
        "ok": True,
        "metric": "mt",
        "period": period_info,
        "filters": {
            "city": city_f,
            "business_unit": bu,
            "oil_type": oil,
            "packing_category": pack,
        },
        "row_dimension": row_dim,
        "column_dimension": col,
        "matrix": primary,
    }

    if mode_norm in {"analytical", "analysis", "how_are", "performance"}:
        city_matrix = _pivot_mt(frame, row_dim, "city")
        client_matrix = _pivot_mt(frame, row_dim, "client_type")
        trend = _trend_table(
            frame,
            row_dim=row_dim,
            period=period_info,
            city=city_f,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
        )
        result["mode"] = "analytical"
        result["city_matrix"] = city_matrix
        result["client_matrix"] = client_matrix
        result["trend"] = trend
        # Ordered list the model must render completely
        trend_cols = "Volume | AMS | " + (
            "Expected | % vs Expected"
            if period_info.get("partial_month")
            else "% vs AMS (no Expected — full month)"
        )
        result["tables"] = [
            {
                "index": 1,
                "title": "City-wise breakdown",
                "source": "city_matrix",
                "data": city_matrix,
            },
            {
                "index": 2,
                "title": "Client-type breakdown",
                "source": "client_matrix",
                "data": client_matrix,
            },
            {
                "index": 3,
                "title": f"Trend vs AMS ({trend_cols})",
                "source": "trend",
                "data": trend,
            },
        ]
        result["response_instructions"] = (
            "CRITICAL: You MUST render ALL tables in `tables` (usually 3) as markdown, "
            "in order, with their titles as headings. Do NOT stop after the first table.\n"
            "1) City-wise breakdown\n"
            "2) Client-type breakdown\n"
            f"3) Trend: {trend_cols}\n"
            "Then add 2–4 short insight bullets (leaders, behind AMS/expected). "
            "State the period label once. Do not invent numbers."
        )
        result["required_table_count"] = 3
    else:
        result["mode"] = "matrix"
        result["tables"] = [
            {
                "index": 1,
                "title": f"{row_dim} × {col}",
                "source": "matrix",
                "data": primary,
            }
        ]
        result["required_table_count"] = 1
        result["response_instructions"] = (
            "Present the single matrix as a markdown table. "
            "State the period label and filters in one short sentence. "
            "Do not invent numbers."
        )

    return result
