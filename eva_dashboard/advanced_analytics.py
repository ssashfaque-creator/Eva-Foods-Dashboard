"""Advanced party/sales analytics: compares, silent, dumping, concentration, etc."""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Any

import pandas as pd

from eva_dashboard.client_language import (
    extract_client_type_from_text,
    lookup_party,
    normalize_client_type,
    normalize_oil_type,
    normalize_packing_category,
)
from eva_dashboard.client_type_map import (
    classify_client_type_filter,
    sql_client_type_values,
)
from eva_dashboard.data import _prior_three_month_ranges, pct_change
from eva_dashboard.db import connect, init_db
from eva_dashboard.fmt import mt_round, mt_str, pct_round
from eva_dashboard.party_analytics import (
    _ams_by_party,
    _fetch_party_lines,
    _first_sale_dates,
    _party_meta,
    extract_city_from_text,
)
from eva_dashboard.sales_query import (
    _attach_client_dims,
    _normalize_business_unit,
    _parties_matching,
    _sales_date_bounds,
    resolve_period,
)

_MT_SQL = """
CASE
  WHEN COALESCE(s.mt_qty, 0) <> 0 THEN s.mt_qty
  WHEN lower(trim(COALESCE(s.unit,''))) IN ('kg','kgs')
    THEN COALESCE(s.qty,0)/1000.0
  WHEN lower(trim(COALESCE(s.unit,''))) IN
       ('mt','m.t','m.t.','ton','tons','tonne','tonnes')
    THEN COALESCE(s.qty,0)
  ELSE 0
END
"""


def _fetch_filtered_mt(
    *,
    date_from: str,
    date_to: str,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    brand_prefix: str | None = None,
    product: str | None = None,
    exclude_client_types: list[str] | None = None,
    exclude_cities: list[str] | None = None,
) -> float:
    frame = _fetch_filtered_lines(
        date_from=date_from,
        date_to=date_to,
        city=city,
        client_type=client_type,
        business_unit=business_unit,
        oil_type=oil_type,
        packing_category=packing_category,
        brand_prefix=brand_prefix,
        product=product,
        exclude_client_types=exclude_client_types,
        exclude_cities=exclude_cities,
    )
    if frame.empty:
        return 0.0
    return float(frame["mt"].sum())


def _fetch_filtered_lines(
    *,
    date_from: str,
    date_to: str,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    brand_prefix: str | None = None,
    product: str | None = None,
    exclude_client_types: list[str] | None = None,
    exclude_cities: list[str] | None = None,
) -> pd.DataFrame:
    """Sales lines with taxonomy + geography (fast in-memory client map)."""
    init_db()
    params: list[Any] = [date_from, date_to]
    where = ["s.date >= ?", "s.date <= ?"]

    if city or client_type:
        matched = _parties_matching(city=city, client_type=client_type) or []
        if client_type and not city:
            raw_types = sql_client_type_values(client_type) or [client_type]
            type_placeholders = ",".join("?" for _ in raw_types)
            where.append(
                "("
                + (
                    f"s.party IN ({','.join('?' for _ in matched)}) OR "
                    if matched
                    else "0 OR "
                )
                + f"lower(trim(COALESCE(s.client_type, ''))) IN ({type_placeholders})"
                + ")"
            )
            if matched:
                params.extend(matched)
            params.extend(t.lower().strip() for t in raw_types)
        elif matched:
            placeholders = ",".join("?" for _ in matched)
            where.append(f"s.party IN ({placeholders})")
            params.extend(matched)
        else:
            return pd.DataFrame(
                columns=[
                    "date",
                    "party",
                    "inv_no",
                    "product",
                    "client_type",
                    "city",
                    "business_unit",
                    "oil_type",
                    "packing_category",
                    "mt",
                ]
            )

    if business_unit:
        where.append("lower(trim(COALESCE(c.category_1, ''))) = lower(trim(?))")
        params.append(business_unit)
    if brand_prefix:
        where.append("lower(trim(COALESCE(c.category_1, ''))) LIKE lower(?)")
        params.append(f"{brand_prefix}%")
    if oil_type:
        where.append("lower(trim(COALESCE(c.category_2, ''))) = lower(trim(?))")
        params.append(oil_type)
    if packing_category:
        where.append("lower(trim(COALESCE(c.packing_category, ''))) = lower(trim(?))")
        params.append(packing_category)
    if product:
        where.append("lower(trim(s.product)) = lower(trim(?))")
        params.append(product)

    sql = f"""
    SELECT
      s.date, s.party, s.inv_no, s.product, s.rate,
      COALESCE(NULLIF(trim(c.category_1), ''), '(unmapped)') AS business_unit,
      COALESCE(NULLIF(trim(c.category_2), ''), '(unmapped)') AS oil_type,
      COALESCE(NULLIF(trim(c.packing_category), ''), '(unmapped)') AS packing_category,
      COALESCE(NULLIF(trim(s.client_type), ''), '') AS sales_client_type,
      {_MT_SQL} AS mt
    FROM sales s
    LEFT JOIN category c ON c.product = s.product
    WHERE {' AND '.join(where)}
    """
    with connect() as conn:
        frame = pd.read_sql_query(sql, conn, params=params)
    frame = _attach_client_dims(frame)
    if not frame.empty and "packing_category" in frame.columns:
        frame = frame.copy()
        frame["packing_category"] = frame["packing_category"].map(
            lambda v: normalize_packing_category(str(v))
            if str(v).strip() and str(v).strip() != "(unmapped)"
            else str(v)
        )
    if city:
        ck = city.strip().lower()
        frame = frame[frame["city"].astype(str).str.strip().str.lower() == ck]
    if client_type:
        classified = classify_client_type_filter(client_type)
        if classified:
            mode, label = classified
            tk = label.strip().lower()
            col = "client_type_raw" if mode == "raw" else "client_type"
            if col not in frame.columns:
                col = "client_type"
            frame = frame[frame[col].astype(str).str.strip().str.lower() == tk]
    if exclude_client_types:
        for x in exclude_client_types:
            if not x:
                continue
            classified = classify_client_type_filter(str(x))
            if not classified:
                continue
            mode, label = classified
            tk = label.strip().lower()
            col = "client_type_raw" if mode == "raw" else "client_type"
            if col not in frame.columns:
                col = "client_type"
            frame = frame[
                ~frame[col].astype(str).str.strip().str.lower().eq(tk)
            ]
    if exclude_cities:
        ex = {str(x).strip().lower() for x in exclude_cities if x}
        frame = frame[~frame["city"].astype(str).str.strip().str.lower().isin(ex)]
    return frame.reset_index(drop=True)


def _analysis(lines: list[str], tips: list[str]) -> str:
    out = list(lines)
    if tips:
        out.append("")
        out.append("### Analysis")
        out.extend(f"- {t}" for t in tips)
    return "\n".join(out).strip() + "\n"


def _scope_bits(filters: dict[str, Any]) -> str:
    bits = []
    for k, label in (
        ("city", "city"),
        ("client_type", "Client Type"),
        ("business_unit", "BU"),
        ("oil_type", "Oil"),
        ("packing_category", "Packing"),
        ("product", "SKU"),
    ):
        if filters.get(k):
            bits.append(f"{label} **{filters[k]}**")
    if filters.get("exclude_client_types"):
        bits.append("excl. " + ", ".join(filters["exclude_client_types"]))
    return " · ".join(bits) if bits else "all sales"


# ---------------------------------------------------------------------------
# Compare cities / client types (volume or growth)
# ---------------------------------------------------------------------------

def compare_segments(
    *,
    segment: str = "city",  # city | client_type
    left: str | None = None,
    right: str | None = None,
    entities: list[str] | None = None,
    metric: str = "volume",  # volume | growth | yoy
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    client_type: str | None = None,
    city: str | None = None,
    exclude_client_types: list[str] | None = None,
) -> dict[str, Any]:
    """Compare 2+ cities or client types (volume + YoY growth).

    Pass ``entities`` for multi-way compares (Lahore vs Karachi vs Islamabad,
    Imtiaz vs Metro vs Chase Up). ``left``/``right`` remain supported for
    pairwise asks.
    """
    period_info = resolve_period(period or "this month", date_from=date_from, date_to=date_to)
    if period_info.get("ok") is False:
        return {"ok": False, "error": period_info.get("error")}
    d0, d1 = period_info["date_from"], period_info["date_to"]
    bu = _normalize_business_unit(business_unit)
    oil = normalize_oil_type(oil_type)
    pack = normalize_packing_category(packing_category)
    ctype = normalize_client_type(client_type)
    seg = "city" if segment == "city" else "client_type"

    names: list[str] = []
    for raw in list(entities or []) + ([left] if left else []) + ([right] if right else []):
        if not raw:
            continue
        label = str(raw).strip()
        if seg == "client_type":
            label = normalize_client_type(label) or label
        elif seg == "city":
            label = label.title() if label == label.lower() else label
        if label and label not in names:
            names.append(label)
    if len(names) < 2:
        return {
            "ok": False,
            "error": "Compare needs at least two cities or client types.",
        }

    def _vol(value: str, p0: str, p1: str) -> float:
        kw: dict[str, Any] = dict(
            date_from=p0,
            date_to=p1,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            exclude_client_types=exclude_client_types,
        )
        if seg == "city":
            kw["city"] = value
            kw["client_type"] = ctype
        else:
            kw["client_type"] = normalize_client_type(value) or value
            kw["city"] = city
        return _fetch_filtered_mt(**kw)

    # Prior year same span
    c0 = date.fromisoformat(d0)
    c1 = date.fromisoformat(d1)
    try:
        p0 = c0.replace(year=c0.year - 1).isoformat()
    except ValueError:
        p0 = c0.replace(year=c0.year - 1, day=28).isoformat()
    try:
        p1 = c1.replace(year=c1.year - 1).isoformat()
    except ValueError:
        p1 = c1.replace(year=c1.year - 1, day=28).isoformat()

    rows_data: list[dict[str, Any]] = []
    for name in names:
        vol = _vol(name, d0, d1)
        prior = _vol(name, p0, p1)
        growth = pct_change(vol, prior)
        rows_data.append(
            {
                "name": name,
                "volume_mt": mt_round(vol),
                "prior_mt": mt_round(prior),
                "yoy_pct": pct_round(growth),
                "_vol": vol,
                "_prior": prior,
                "_growth": growth,
            }
        )
    # Rank by current volume for the table / tips (keep mention order in entities)
    ranked = sorted(rows_data, key=lambda r: (-float(r["_vol"]), str(r["name"])))

    filters = {
        "business_unit": bu,
        "oil_type": oil,
        "packing_category": pack,
        "client_type": ctype if seg == "city" else None,
        "city": city if seg != "city" else None,
        "exclude_client_types": exclude_client_types,
    }
    title = " vs ".join(names)
    dim_label = seg.replace("_", " ").title()
    lines = [
        f"Compare {seg.replace('_', ' ')} — **{title}** · {period_info.get('label')} "
        f"({_scope_bits(filters)}).\n",
        f"| # | {dim_label} | Volume (MT) | Prior YoY (MT) | YoY % | Share |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    total_v = sum(float(r["_vol"]) for r in ranked) or 0.0
    for i, r in enumerate(ranked, 1):
        gs = f"{r['_growth']:+.1f}%" if r["_growth"] is not None else "—"
        share = (100.0 * float(r["_vol"]) / total_v) if total_v else 0.0
        lines.append(
            f"| {i} | {r['name']} | {r['volume_mt']} | {r['prior_mt']} | {gs} | "
            f"{share:.1f}% |"
        )
    lines.append(f"| | **Total** | **{mt_round(total_v)}** | | | **100%** |")

    tips: list[str] = []
    leader, second = ranked[0], ranked[1] if len(ranked) > 1 else ranked[0]
    gap = float(leader["_vol"]) - float(second["_vol"])
    tips.append(
        f"**{leader['name']}** leads with {leader['volume_mt']} MT "
        f"({(100.0 * float(leader['_vol']) / total_v) if total_v else 0:.1f}% share)"
        + (
            f" — ahead of **{second['name']}** by {mt_round(gap)} MT."
            if leader["name"] != second["name"]
            else "."
        )
    )
    growth_rows = [r for r in ranked if r["_growth"] is not None]
    if len(growth_rows) >= 2:
        fastest = max(growth_rows, key=lambda r: float(r["_growth"]))
        slowest = min(growth_rows, key=lambda r: float(r["_growth"]))
        if fastest["name"] != slowest["name"]:
            tips.append(
                f"**{fastest['name']}** is growing fastest YoY "
                f"({fastest['_growth']:+.1f}%); **{slowest['name']}** "
                f"slowest ({slowest['_growth']:+.1f}%)."
            )
        else:
            tips.append("YoY growth rates are similar across the set.")

    left_row = next((r for r in rows_data if r["name"] == names[0]), ranked[0])
    right_row = next((r for r in rows_data if r["name"] == names[1]), ranked[1])

    return {
        "ok": True,
        "mode": "compare_segments",
        "segment": seg,
        "metric": metric,
        "period": period_info,
        "filters": filters,
        "entities": [
            {
                "name": r["name"],
                "volume_mt": r["volume_mt"],
                "prior_mt": r["prior_mt"],
                "yoy_pct": r["yoy_pct"],
            }
            for r in ranked
        ],
        "left": {
            "name": left_row["name"],
            "volume_mt": left_row["volume_mt"],
            "prior_mt": left_row["prior_mt"],
            "yoy_pct": left_row["yoy_pct"],
        },
        "right": {
            "name": right_row["name"],
            "volume_mt": right_row["volume_mt"],
            "prior_mt": right_row["prior_mt"],
            "yoy_pct": right_row["yoy_pct"],
        },
        "answer_markdown": _analysis(lines, tips),
        "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
    }


# ---------------------------------------------------------------------------
# Week-over-week
# ---------------------------------------------------------------------------

def week_over_week(
    *,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    exclude_client_types: list[str] | None = None,
    row_dimension: str = "packing_category",
) -> dict[str, Any]:
    this_w = resolve_period("this week")
    last_w = resolve_period("last week")
    if this_w.get("ok") is False:
        return {"ok": False, "error": this_w.get("error")}
    bu = _normalize_business_unit(business_unit)
    oil = normalize_oil_type(oil_type)
    pack = normalize_packing_category(packing_category)
    ctype = normalize_client_type(client_type)
    dim = row_dimension if row_dimension in {
        "packing_category", "oil_type", "business_unit", "client_type", "city", "product"
    } else "packing_category"

    cur = _fetch_filtered_lines(
        date_from=this_w["date_from"], date_to=this_w["date_to"],
        city=city, client_type=ctype, business_unit=bu, oil_type=oil,
        packing_category=pack, exclude_client_types=exclude_client_types,
    )
    pri = _fetch_filtered_lines(
        date_from=last_w["date_from"], date_to=last_w["date_to"],
        city=city, client_type=ctype, business_unit=bu, oil_type=oil,
        packing_category=pack, exclude_client_types=exclude_client_types,
    )
    cur_g = cur.groupby(dim)["mt"].sum() if not cur.empty else pd.Series(dtype=float)
    pri_g = pri.groupby(dim)["mt"].sum() if not pri.empty else pd.Series(dtype=float)
    keys = sorted(set(cur_g.index) | set(pri_g.index),
                  key=lambda k: -(float(cur_g.get(k, 0)) + float(pri_g.get(k, 0))))
    rows = []
    for k in keys:
        c = float(cur_g.get(k, 0))
        p = float(pri_g.get(k, 0))
        rows.append({
            "segment": str(k),
            "this_week_mt": mt_round(c),
            "last_week_mt": mt_round(p),
            "wow_pct": pct_round(pct_change(c, p)),
            "delta_mt": mt_round(c - p),
        })
    c_tot = float(cur["mt"].sum()) if not cur.empty else 0.0
    p_tot = float(pri["mt"].sum()) if not pri.empty else 0.0
    rows.append({
        "segment": "Total",
        "this_week_mt": mt_round(c_tot),
        "last_week_mt": mt_round(p_tot),
        "wow_pct": pct_round(pct_change(c_tot, p_tot)),
        "delta_mt": mt_round(c_tot - p_tot),
    })
    filters = {
        "city": city, "client_type": ctype, "business_unit": bu,
        "oil_type": oil, "packing_category": pack,
        "exclude_client_types": exclude_client_types,
    }
    lines = [
        f"Week-over-week — this week ({this_w['date_from']}→{this_w['date_to']}) vs "
        f"last 7 days ({last_w['date_from']}→{last_w['date_to']}) · {_scope_bits(filters)}.\n",
        f"| {dim.replace('_', ' ').title()} | This week | Last week | Δ MT | WoW % |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        wow = r["wow_pct"]
        wow_s = f"{wow:+.1f}%" if wow is not None else "—"
        bold = r["segment"] == "Total"
        seg = f"**{r['segment']}**" if bold else r["segment"]
        a, b, d = r["this_week_mt"], r["last_week_mt"], r["delta_mt"]
        if bold:
            lines.append(f"| {seg} | **{a}** | **{b}** | **{d}** | **{wow_s}** |")
        else:
            lines.append(f"| {seg} | {a} | {b} | {d} | {wow_s} |")
    tips = []
    body = [r for r in rows if r["segment"] != "Total"]
    if body:
        best = max(body, key=lambda r: r["delta_mt"])
        worst = min(body, key=lambda r: r["delta_mt"])
        tips.append(
            f"**{best['segment']}** added the most week-on-week (+{best['delta_mt']} MT)."
        )
        if worst["segment"] != best["segment"]:
            tips.append(
                f"**{worst['segment']}** lost the most ({worst['delta_mt']} MT)."
            )
    tot_wow = pct_round(pct_change(c_tot, p_tot))
    if tot_wow is not None:
        tips.append(f"Overall WoW **{tot_wow:+.1f}%** ({mt_round(c_tot)} vs {mt_round(p_tot)} MT).")
    return {
        "ok": True,
        "mode": "week_over_week",
        "period": this_w,
        "compare_period": last_w,
        "filters": filters,
        "rows": rows,
        "answer_markdown": _analysis(lines, tips),
        "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
    }


# ---------------------------------------------------------------------------
# Packing / oil growth ranks
# ---------------------------------------------------------------------------

def rank_dimension_growth(
    *,
    dimension: str = "packing_category",  # packing_category | oil_type | product
    period: str | None = None,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    exclude_client_types: list[str] | None = None,
    limit: int = 10,
    sort: str = "desc",
) -> dict[str, Any]:
    period_info = resolve_period(period or "this month")
    if period_info.get("ok") is False:
        return {"ok": False, "error": period_info.get("error")}
    d0, d1 = period_info["date_from"], period_info["date_to"]
    c0 = date.fromisoformat(d0)
    c1 = date.fromisoformat(d1)
    try:
        p0 = c0.replace(year=c0.year - 1).isoformat()
        p1 = c1.replace(year=c1.year - 1).isoformat()
    except ValueError:
        p0 = c0.replace(year=c0.year - 1, day=28).isoformat()
        p1 = c1.replace(year=c1.year - 1, day=28).isoformat()

    dim = dimension if dimension in {"packing_category", "oil_type", "product", "business_unit"} else "packing_category"
    bu = _normalize_business_unit(business_unit)
    oil = normalize_oil_type(oil_type)
    pack = normalize_packing_category(packing_category)
    ctype = normalize_client_type(client_type)

    cur = _fetch_filtered_lines(
        date_from=d0, date_to=d1, city=city, client_type=ctype,
        business_unit=bu, oil_type=oil, packing_category=pack,
        exclude_client_types=exclude_client_types,
    )
    pri = _fetch_filtered_lines(
        date_from=p0, date_to=p1, city=city, client_type=ctype,
        business_unit=bu, oil_type=oil, packing_category=pack,
        exclude_client_types=exclude_client_types,
    )
    cur_g = cur.groupby(dim)["mt"].sum() if not cur.empty else pd.Series(dtype=float)
    pri_g = pri.groupby(dim)["mt"].sum() if not pri.empty else pd.Series(dtype=float)
    rows = []
    for k in set(cur_g.index) | set(pri_g.index):
        c = float(cur_g.get(k, 0))
        p = float(pri_g.get(k, 0))
        rows.append({
            "segment": str(k),
            "volume_mt": mt_round(c),
            "prior_mt": mt_round(p),
            "yoy_pct": pct_round(pct_change(c, p)),
            "delta_mt": mt_round(c - p),
        })
    rows = [r for r in rows if r["volume_mt"] or r["prior_mt"]]
    reverse = sort != "asc"
    rows.sort(
        key=lambda r: (
            r["yoy_pct"] is None,
            -(r["yoy_pct"] if r["yoy_pct"] is not None else 0) if reverse
            else (r["yoy_pct"] if r["yoy_pct"] is not None else 0),
            -r["volume_mt"],
        )
    )
    rows = rows[: max(1, min(limit, 100))]
    filters = {
        "city": city, "client_type": ctype, "business_unit": bu,
        "oil_type": oil, "packing_category": pack,
        "exclude_client_types": exclude_client_types,
    }
    lines = [
        f"YoY growth by {dim.replace('_', ' ')} — {period_info.get('label')} "
        f"· {_scope_bits(filters)}.\n",
        f"| # | {dim.replace('_', ' ').title()} | Volume | Prior | YoY % | Δ MT |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(rows, 1):
        y = r["yoy_pct"]
        ys = f"{y:+.1f}%" if y is not None else "—"
        lines.append(
            f"| {i} | {r['segment']} | {r['volume_mt']} | {r['prior_mt']} | {ys} | {r['delta_mt']} |"
        )
    tips = []
    if rows and rows[0].get("yoy_pct") is not None:
        tips.append(
            f"**{rows[0]['segment']}** ranks #1 by YoY ({rows[0]['yoy_pct']:+.1f}%)."
        )
    return {
        "ok": True,
        "mode": "dimension_growth",
        "dimension": dim,
        "period": period_info,
        "filters": filters,
        "rows": rows,
        "answer_markdown": _analysis(lines, tips),
        "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
    }


# ---------------------------------------------------------------------------
# Oil-type mix / packing contribution / concentration
# ---------------------------------------------------------------------------

def mix_or_share(
    *,
    mode: str = "oil_mix",  # oil_mix | packing_contribution | concentration | packing_share_of_party
    period: str | None = None,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    party: str | None = None,
    exclude_client_types: list[str] | None = None,
    group_by: str = "party",
    metric: str = "volume",  # volume | growth
    limit: int = 20,
) -> dict[str, Any]:
    period_info = resolve_period(period or "this month")
    if period_info.get("ok") is False:
        return {"ok": False, "error": period_info.get("error")}
    d0, d1 = period_info["date_from"], period_info["date_to"]
    bu = _normalize_business_unit(business_unit)
    oil = normalize_oil_type(oil_type)
    pack = normalize_packing_category(packing_category)
    ctype = normalize_client_type(client_type)

    frame = _fetch_filtered_lines(
        date_from=d0, date_to=d1, city=city, client_type=ctype,
        business_unit=bu, oil_type=oil, packing_category=pack,
        exclude_client_types=exclude_client_types,
    )
    if party:
        frame = frame[frame["party"].astype(str).str.lower() == party.lower()] if not frame.empty else frame

    filters = {
        "city": city, "client_type": ctype, "business_unit": bu,
        "oil_type": oil, "packing_category": pack, "party": party,
        "exclude_client_types": exclude_client_types,
    }

    if mode == "oil_mix":
        dim = "oil_type"
        total = float(frame["mt"].sum()) if not frame.empty else 0.0
        g = frame.groupby(dim)["mt"].sum().sort_values(ascending=False) if not frame.empty else pd.Series(dtype=float)
        rows = [
            {"segment": str(k), "volume_mt": mt_round(v),
             "share_pct": pct_round(100.0 * float(v) / total if total else 0)}
            for k, v in g.items()
        ]
        lines = [
            f"Oil-type mix — {period_info.get('label')} · {_scope_bits(filters)}.\n",
            "| Oil Type | Volume (MT) | Share % |",
            "| --- | --- | --- |",
        ]
        for r in rows:
            lines.append(f"| {r['segment']} | {r['volume_mt']} | {r['share_pct']}% |")
        lines.append(f"| **Total** | **{mt_round(total)}** | **100%** |")
        tips = []
        if rows:
            tips.append(
                f"**{rows[0]['segment']}** is {rows[0]['share_pct']:.0f}% of oil mix."
            )
        return {
            "ok": True, "mode": mode, "period": period_info, "filters": filters,
            "rows": rows,
            "answer_markdown": _analysis(lines, tips),
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    if mode == "packing_contribution":
        # Packing share of BU (or scoped) volume
        dim = "packing_category"
        total = float(frame["mt"].sum()) if not frame.empty else 0.0
        g = frame.groupby(dim)["mt"].sum().sort_values(ascending=False) if not frame.empty else pd.Series(dtype=float)
        rows = [
            {"segment": str(k), "volume_mt": mt_round(v),
             "share_pct": pct_round(100.0 * float(v) / total if total else 0)}
            for k, v in g.items()
        ]
        lines = [
            f"Packing contribution — {period_info.get('label')} · {_scope_bits(filters)}.\n",
            "| Packing | Volume (MT) | Share of scope % |",
            "| --- | --- | --- |",
        ]
        for r in rows:
            lines.append(f"| {r['segment']} | {r['volume_mt']} | {r['share_pct']}% |")
        lines.append(f"| **Total** | **{mt_round(total)}** | **100%** |")
        tips = []
        if rows:
            tips.append(
                f"**{rows[0]['segment']}** contributes {rows[0]['share_pct']:.0f}% of this scope."
            )
        return {
            "ok": True, "mode": mode, "period": period_info, "filters": filters,
            "rows": rows,
            "answer_markdown": _analysis(lines, tips),
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    if mode == "packing_share_of_party":
        # For each party (or one party): packing share of their volume
        if frame.empty:
            return {
                "ok": True, "mode": mode, "period": period_info, "filters": filters,
                "rows": [],
                "answer_markdown": "No sales for packing share of party.\n",
                "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
            }
        party_tot = frame.groupby("party")["mt"].sum()
        pack_party = frame.groupby(["party", "packing_category"])["mt"].sum().reset_index()
        pack_party["party_total"] = pack_party["party"].map(party_tot)
        pack_party["share_pct"] = pack_party.apply(
            lambda r: 100.0 * float(r["mt"]) / float(r["party_total"]) if r["party_total"] else 0,
            axis=1,
        )
        if pack:
            pack_party = pack_party[
                pack_party["packing_category"].astype(str).str.lower() == pack.lower()
            ]
        pack_party = pack_party.sort_values(["mt"], ascending=False).head(limit)
        rows = [
            {
                "party": str(r["party"]),
                "packing_category": str(r["packing_category"]),
                "volume_mt": mt_round(r["mt"]),
                "party_total_mt": mt_round(r["party_total"]),
                "share_pct": pct_round(r["share_pct"]),
            }
            for _, r in pack_party.iterrows()
        ]
        lines = [
            f"Packing share of customer volume — {period_info.get('label')} · {_scope_bits(filters)}.\n",
            "| Party | Packing | Packing MT | Party total | Share % |",
            "| --- | --- | --- | --- | --- |",
        ]
        for r in rows:
            lines.append(
                f"| {r['party']} | {r['packing_category']} | {r['volume_mt']} | "
                f"{r['party_total_mt']} | {r['share_pct']}% |"
            )
        tips = []
        if rows:
            tips.append(
                f"**{rows[0]['party']}** has {rows[0]['share_pct']:.0f}% of its volume in "
                f"**{rows[0]['packing_category']}**."
            )
        return {
            "ok": True, "mode": mode, "period": period_info, "filters": filters,
            "rows": rows,
            "answer_markdown": _analysis(lines, tips),
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    # concentration — share of parties (or growth of Imtiaz stores)
    entity = "party"
    if metric == "growth":
        # YoY by party within filters
        c0 = date.fromisoformat(d0)
        c1 = date.fromisoformat(d1)
        try:
            p0 = c0.replace(year=c0.year - 1).isoformat()
            p1 = c1.replace(year=c1.year - 1).isoformat()
        except ValueError:
            p0 = c0.replace(year=c0.year - 1, day=28).isoformat()
            p1 = c1.replace(year=c1.year - 1, day=28).isoformat()
        pri = _fetch_filtered_lines(
            date_from=p0, date_to=p1, city=city, client_type=ctype,
            business_unit=bu, oil_type=oil, packing_category=pack,
            exclude_client_types=exclude_client_types,
        )
        cur_g = frame.groupby("party")["mt"].sum() if not frame.empty else pd.Series(dtype=float)
        pri_g = pri.groupby("party")["mt"].sum() if not pri.empty else pd.Series(dtype=float)
        rows = []
        for k in set(cur_g.index) | set(pri_g.index):
            c = float(cur_g.get(k, 0))
            p = float(pri_g.get(k, 0))
            meta = _party_meta(frame if not frame.empty else pri, str(k))
            rows.append({
                "party": str(k),
                **meta,
                "volume_mt": mt_round(c),
                "prior_mt": mt_round(p),
                "yoy_pct": pct_round(pct_change(c, p)),
                "share_pct": None,
            })
        rows.sort(key=lambda r: (-(r["yoy_pct"] if r["yoy_pct"] is not None else -1e9), -r["volume_mt"]))
        rows = rows[:limit]
        lines = [
            f"Growth by party — {period_info.get('label')} · {_scope_bits(filters)}.\n",
            "| # | Party | Client Type | City | Volume | Prior | YoY % |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for i, r in enumerate(rows, 1):
            y = r["yoy_pct"]
            ys = f"{y:+.1f}%" if y is not None else "—"
            lines.append(
                f"| {i} | {r['party']} | {r.get('client_type') or '—'} | "
                f"{r.get('city') or '—'} | {r['volume_mt']} | {r['prior_mt']} | {ys} |"
            )
        tips = []
        if rows and rows[0].get("yoy_pct") is not None:
            tips.append(f"**{rows[0]['party']}** leads growth at {rows[0]['yoy_pct']:+.1f}%.")
        return {
            "ok": True, "mode": "concentration_growth", "period": period_info,
            "filters": filters, "rows": rows,
            "answer_markdown": _analysis(lines, tips),
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    total = float(frame["mt"].sum()) if not frame.empty else 0.0
    g = frame.groupby("party")["mt"].sum().sort_values(ascending=False) if not frame.empty else pd.Series(dtype=float)
    rows = []
    for k, v in g.head(limit).items():
        meta = _party_meta(frame, str(k))
        rows.append({
            "party": str(k),
            **meta,
            "volume_mt": mt_round(v),
            "share_pct": pct_round(100.0 * float(v) / total if total else 0),
        })
    # Herfindahl-ish top share
    top_share = float(rows[0]["share_pct"] or 0) if rows else 0
    lines = [
        f"Party shares — {period_info.get('label')} · {_scope_bits(filters)} "
        f"(total {mt_round(total)} MT).\n",
        "| # | Party | Client Type | City | Volume (MT) | Share % |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {r['party']} | {r.get('client_type') or '—'} | "
            f"{r.get('city') or '—'} | {r['volume_mt']} | {r['share_pct']}% |"
        )
    tips = []
    if rows:
        tips.append(
            f"**{rows[0]['party']}** holds {top_share:.0f}% of this scope"
            + (" — high dependency." if top_share >= 25 else ".")
        )
        top3 = sum(float(r["share_pct"] or 0) for r in rows[:3])
        tips.append(f"Top 3 parties = {top3:.0f}% combined.")
    return {
        "ok": True, "mode": "concentration", "period": period_info,
        "filters": filters, "total_mt": mt_round(total), "rows": rows,
        "answer_markdown": _analysis(lines, tips),
        "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
    }


# ---------------------------------------------------------------------------
# Silent this week / not ordered / reactivated / days since invoice
# ---------------------------------------------------------------------------

def silent_parties(
    *,
    grain: str = "week",  # week | month
    period: str | None = None,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    exclude_client_types: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """AMS > 0 but zero volume in this week/month."""
    if grain == "week" and not period:
        period_info = resolve_period("this week")
    else:
        period_info = resolve_period(period or "this month")
    if period_info.get("ok") is False:
        return {"ok": False, "error": period_info.get("error")}
    d0, d1 = period_info["date_from"], period_info["date_to"]
    as_of = date.fromisoformat(d1)
    bu = _normalize_business_unit(business_unit)
    oil = normalize_oil_type(oil_type)
    pack = normalize_packing_category(packing_category)
    ctype = normalize_client_type(client_type)

    ams = _ams_by_party(
        as_of=as_of.replace(day=1),
        city=city, client_type=ctype, business_unit=bu,
        oil_type=oil, packing_category=pack, brand_prefix=None,
    )
    # Apply excludes by re-fetching meta
    frame = _fetch_filtered_lines(
        date_from=d0, date_to=d1, city=city, client_type=ctype,
        business_unit=bu, oil_type=oil, packing_category=pack,
        exclude_client_types=exclude_client_types,
    )
    vol = frame.groupby("party")["mt"].sum().to_dict() if not frame.empty else {}

    # Meta from AMS window
    ranges = _prior_three_month_ranges(as_of.replace(day=1))
    meta_frame = _fetch_filtered_lines(
        date_from=ranges[0][0].isoformat(), date_to=d1,
        city=city, client_type=ctype, business_unit=bu,
        oil_type=oil, packing_category=pack,
        exclude_client_types=exclude_client_types,
    )
    exclude_set = {e.lower() for e in (exclude_client_types or [])}
    rows = []
    for party, ams_v in ams.items():
        if ams_v <= 0:
            continue
        if float(vol.get(party, 0)) > 0:
            continue
        meta = _party_meta(meta_frame, party) if not meta_frame.empty else {}
        if ctype and (meta.get("client_type") or "").lower() != ctype.lower():
            # ams was filtered; keep
            pass
        if exclude_set and (meta.get("client_type") or "").lower() in exclude_set:
            continue
        rows.append({
            "party": party,
            **meta,
            "volume_mt": 0,
            "ams_mt": mt_round(ams_v),
        })
    rows.sort(key=lambda r: (-(r["ams_mt"] or 0), str(r["party"])))
    rows = rows[:limit]
    filters = {
        "city": city, "client_type": ctype, "business_unit": bu,
        "oil_type": oil, "packing_category": pack,
        "exclude_client_types": exclude_client_types,
    }
    grain_label = "week" if "week" in (period_info.get("label") or "").lower() or grain == "week" else "period"
    lines = [
        f"No sales this {grain_label} (AMS > 0) — {period_info.get('label')} · {_scope_bits(filters)}.\n",
        "| # | Party | Client Type | City | AMS (MT) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {r['party']} | {r.get('client_type') or '—'} | "
            f"{r.get('city') or '—'} | {r['ams_mt']} |"
        )
    if not rows:
        lines = [f"No silent parties for {period_info.get('label')} · {_scope_bits(filters)}.\n"]
    tips = []
    if rows:
        tips.append(
            f"{len(rows)} parties with AMS but **zero** this {grain_label}; "
            f"largest AMS silent: **{rows[0]['party']}** ({rows[0]['ams_mt']} MT)."
        )
    return {
        "ok": True, "mode": "silent_parties", "period": period_info,
        "filters": filters, "parties": rows,
        "answer_markdown": _analysis(lines, tips),
        "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
    }


def not_ordered(
    *,
    period: str | None = None,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    product: str | None = None,
    exclude_client_types: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Parties with AMS for the scoped product/packing but zero in period — sort by AMS desc."""
    period_info = resolve_period(period or "this month")
    if period_info.get("ok") is False:
        return {"ok": False, "error": period_info.get("error")}
    if not packing_category and not product and not oil_type:
        return {
            "ok": False,
            "error": "Specify a packing, product, or oil type (e.g. Stand up / Pillow).",
        }
    d0, d1 = period_info["date_from"], period_info["date_to"]
    as_of = date.fromisoformat(d1)
    bu = _normalize_business_unit(business_unit)
    oil = normalize_oil_type(oil_type)
    pack = normalize_packing_category(packing_category)
    ctype = normalize_client_type(client_type)

    ams = _ams_by_party(
        as_of=as_of.replace(day=1),
        city=city, client_type=ctype, business_unit=bu,
        oil_type=oil, packing_category=pack, brand_prefix=None,
    )
    # If product filter, recompute AMS for that product only
    if product:
        ranges = _prior_three_month_ranges(as_of.replace(day=1))
        monthly = []
        keys: set[str] = set()
        for start, end in ranges:
            f = _fetch_filtered_lines(
                date_from=start.isoformat(), date_to=end.isoformat(),
                city=city, client_type=ctype, business_unit=bu,
                oil_type=oil, packing_category=pack, product=product,
                exclude_client_types=exclude_client_types,
            )
            if f.empty:
                monthly.append({})
                continue
            totals = {str(k): float(v) for k, v in f.groupby("party")["mt"].sum().items()}
            keys.update(totals)
            monthly.append(totals)
        ams = {k: sum(m.get(k, 0.0) for m in monthly) / 3.0 for k in keys}

    frame = _fetch_filtered_lines(
        date_from=d0, date_to=d1, city=city, client_type=ctype,
        business_unit=bu, oil_type=oil, packing_category=pack, product=product,
        exclude_client_types=exclude_client_types,
    )
    vol = frame.groupby("party")["mt"].sum().to_dict() if not frame.empty else {}
    ranges = _prior_three_month_ranges(as_of.replace(day=1))
    meta_frame = _fetch_filtered_lines(
        date_from=ranges[0][0].isoformat(), date_to=d1,
        city=city, client_type=ctype, business_unit=bu,
        oil_type=oil, packing_category=pack, product=product,
        exclude_client_types=exclude_client_types,
    )
    rows = []
    for party, ams_v in ams.items():
        if ams_v <= 0:
            continue
        if float(vol.get(party, 0)) > 0:
            continue
        meta = _party_meta(meta_frame, party) if not meta_frame.empty else {}
        rows.append({
            "party": party, **meta,
            "volume_mt": 0, "ams_mt": mt_round(ams_v),
        })
    rows.sort(key=lambda r: (-(r["ams_mt"] or 0), str(r["party"])))
    rows = rows[:limit]
    scope = pack or product or oil or "item"
    filters = {
        "city": city, "client_type": ctype, "business_unit": bu,
        "oil_type": oil, "packing_category": pack, "product": product,
        "exclude_client_types": exclude_client_types,
    }
    lines = [
        f"Not ordered **{scope}** this period (AMS > 0, zero volume) — "
        f"{period_info.get('label')} · {_scope_bits(filters)}.\n",
        "| # | Party | Client Type | City | AMS for item (MT) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {r['party']} | {r.get('client_type') or '—'} | "
            f"{r.get('city') or '—'} | {r['ams_mt']} |"
        )
    if not rows:
        lines = [f"Everyone with AMS for {scope} has ordered this period.\n"]
    tips = []
    if rows:
        tips.append(
            f"{len(rows)} parties missing **{scope}**; "
            f"highest AMS at risk: **{rows[0]['party']}** ({rows[0]['ams_mt']} MT AMS)."
        )
    return {
        "ok": True, "mode": "not_ordered", "period": period_info,
        "filters": filters, "parties": rows,
        "answer_markdown": _analysis(lines, tips),
        "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
    }


def reactivated_parties(
    *,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    exclude_client_types: list[str] | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """Silent last quarter, buying in current period."""
    last_q = resolve_period("last quarter")
    this_m = resolve_period("this month")
    if last_q.get("ok") is False:
        return {"ok": False, "error": last_q.get("error")}
    bu = _normalize_business_unit(business_unit)
    ctype = normalize_client_type(client_type)
    q_frame = _fetch_filtered_lines(
        date_from=last_q["date_from"], date_to=last_q["date_to"],
        city=city, client_type=ctype, business_unit=bu,
        exclude_client_types=exclude_client_types,
    )
    m_frame = _fetch_filtered_lines(
        date_from=this_m["date_from"], date_to=this_m["date_to"],
        city=city, client_type=ctype, business_unit=bu,
        exclude_client_types=exclude_client_types,
    )
    q_parties = set(q_frame["party"].unique()) if not q_frame.empty else set()
    m_vol = m_frame.groupby("party")["mt"].sum().to_dict() if not m_frame.empty else {}

    # Need AMS before last quarter to know they were real customers
    as_of = date.fromisoformat(last_q["date_from"])
    ams = _ams_by_party(
        as_of=as_of.replace(day=1),
        city=city, client_type=ctype, business_unit=bu,
        oil_type=None, packing_category=None, brand_prefix=None,
    )
    rows = []
    for party, vol in m_vol.items():
        if vol <= 0:
            continue
        if party in q_parties:
            continue
        if float(ams.get(party, 0)) <= 0:
            continue
        meta = _party_meta(m_frame, party)
        rows.append({
            "party": party, **meta,
            "volume_mt": mt_round(vol),
            "ams_mt": mt_round(ams.get(party, 0)),
        })
    rows.sort(key=lambda r: (-r["volume_mt"], -r["ams_mt"]))
    rows = rows[:limit]
    filters = {"city": city, "client_type": ctype, "business_unit": bu,
               "exclude_client_types": exclude_client_types}
    lines = [
        f"Reactivated parties (silent last quarter, buying this month) — {_scope_bits(filters)}.\n",
        "| # | Party | Client Type | City | This month MT | Prior AMS |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {r['party']} | {r.get('client_type') or '—'} | "
            f"{r.get('city') or '—'} | {r['volume_mt']} | {r['ams_mt']} |"
        )
    if not rows:
        lines = ["No reactivated parties in this cut.\n"]
    tips = []
    if rows:
        tips.append(
            f"{len(rows)} reactivations; largest return: **{rows[0]['party']}** "
            f"({rows[0]['volume_mt']} MT)."
        )
    return {
        "ok": True, "mode": "reactivated", "filters": filters, "parties": rows,
        "answer_markdown": _analysis(lines, tips),
        "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
    }


def days_since_last_invoice(
    *,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    packing_category: str | None = None,
    exclude_client_types: list[str] | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    init_db()
    bu = _normalize_business_unit(business_unit)
    ctype = normalize_client_type(client_type)
    pack = normalize_packing_category(packing_category)
    _, max_d = _sales_date_bounds()
    if not max_d:
        return {"ok": False, "error": "No sales dates"}
    # Last 12 months of activity to find last invoice date per party
    start = (max_d.replace(day=1) - timedelta(days=365)).isoformat()
    frame = _fetch_filtered_lines(
        date_from=start, date_to=max_d.isoformat(),
        city=city, client_type=ctype, business_unit=bu,
        packing_category=pack, exclude_client_types=exclude_client_types,
    )
    if frame.empty:
        return {
            "ok": True, "mode": "days_since_invoice", "parties": [],
            "answer_markdown": "No parties in scope.\n",
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    last = frame.groupby("party")["date"].max()
    ams = _ams_by_party(
        as_of=max_d.replace(day=1),
        city=city, client_type=ctype, business_unit=bu,
        oil_type=None, packing_category=pack, brand_prefix=None,
    )
    rows = []
    for party, last_dt in last.items():
        if pd.isna(last_dt):
            continue
        days = (max_d - last_dt.date()).days
        meta = _party_meta(frame, str(party))
        rows.append({
            "party": str(party), **meta,
            "last_sale": last_dt.date().isoformat(),
            "days_since": int(days),
            "ams_mt": mt_round(ams.get(str(party), 0)),
        })
    rows.sort(key=lambda r: (-r["days_since"], -r["ams_mt"]))
    rows = rows[:limit]
    filters = {
        "city": city, "client_type": ctype, "business_unit": bu,
        "packing_category": pack, "exclude_client_types": exclude_client_types,
    }
    lines = [
        f"Days since last invoice (as of {max_d.isoformat()}) · {_scope_bits(filters)}.\n",
        "| # | Party | Client Type | City | Last sale | Days | AMS |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {r['party']} | {r.get('client_type') or '—'} | "
            f"{r.get('city') or '—'} | {r['last_sale']} | {r['days_since']} | {r['ams_mt']} |"
        )
    tips = []
    if rows:
        tips.append(
            f"Longest gap: **{rows[0]['party']}** — {rows[0]['days_since']} days "
            f"(AMS {rows[0]['ams_mt']} MT)."
        )
        stale = [r for r in rows if r["days_since"] >= 14]
        if stale:
            tips.append(f"{len(stale)} parties with ≥14 days since last invoice.")
    return {
        "ok": True, "mode": "days_since_invoice", "filters": filters, "parties": rows,
        "answer_markdown": _analysis(lines, tips),
        "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
    }


# ---------------------------------------------------------------------------
# Single party profile (search then analyze)
# ---------------------------------------------------------------------------

def party_profile(
    *,
    query: str,
    period: str | None = None,
    months_back: int = 6,
) -> dict[str, Any]:
    """Fuzzy party search: 1 exact/unique match → profile; else list picks."""
    looked = lookup_party(query, limit=10)
    matches = list(looked.get("matches") or [])
    if not matches:
        return {
            "ok": True,
            "mode": "party_pick",
            "matches": [],
            "answer_markdown": f"No party matched **{query}**.\n",
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }
    # Exact name match (case-insensitive) or single high-confidence match
    qn = re.sub(r"\s+", " ", (query or "").strip().lower())
    exact = [m for m in matches if str(m.get("client") or "").strip().lower() == qn]
    if len(exact) == 1:
        chosen = exact[0]
    elif len(matches) == 1 and float(matches[0].get("match_score") or 0) >= 0.72:
        chosen = matches[0]
    elif (
        len(matches) >= 1
        and float(matches[0].get("match_score") or 0) >= 0.92
        and (
            len(matches) == 1
            or float(matches[0].get("match_score") or 0)
            - float(matches[1].get("match_score") or 0)
            >= 0.08
        )
    ):
        chosen = matches[0]
    else:
        lines = [
            f"Multiple parties match **{query}** — reply with the exact name:\n",
            "| # | Party | Client Type | City | Score |",
            "| --- | --- | --- | --- | --- |",
        ]
        for i, m in enumerate(matches[:10], 1):
            lines.append(
                f"| {i} | {m.get('client')} | {m.get('client_type') or '—'} | "
                f"{m.get('city_filter') or m.get('city') or '—'} | "
                f"{m.get('match_score')} |"
            )
        return {
            "ok": True,
            "mode": "party_pick",
            "matches": matches,
            "answer_markdown": "\n".join(lines) + "\n",
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    name = str(chosen.get("client"))
    period_info = resolve_period(period) if period else None
    # Month-wise last N months + AMS
    _, max_d = _sales_date_bounds()
    if not max_d:
        return {"ok": False, "error": "No sales"}
    labels = []
    y, m = max_d.year, max_d.month
    for _ in range(months_back):
        labels.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    labels.reverse()
    start = f"{labels[0]}-01"
    frame = _fetch_filtered_lines(date_from=start, date_to=max_d.isoformat())
    frame = frame[frame["party"].astype(str) == name] if not frame.empty else frame
    ams = _ams_by_party(
        as_of=max_d.replace(day=1),
        city=None, client_type=None, business_unit=None,
        oil_type=None, packing_category=None, brand_prefix=None,
    ).get(name, 0.0)

    month_rows = []
    if not frame.empty:
        frame = frame.copy()
        frame["ym"] = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m")
        by_m = frame.groupby("ym")["mt"].sum().to_dict()
        for lab in labels:
            month_rows.append({"month": lab, "volume_mt": mt_round(by_m.get(lab, 0))})
        pack_mix = (
            frame.groupby("packing_category")["mt"].sum().sort_values(ascending=False)
        )
        oil_mix = frame.groupby("oil_type")["mt"].sum().sort_values(ascending=False)
    else:
        pack_mix = pd.Series(dtype=float)
        oil_mix = pd.Series(dtype=float)

    total = sum(r["volume_mt"] for r in month_rows)
    lines = [
        f"**{name}** · {chosen.get('client_type') or '—'} · "
        f"{chosen.get('city_filter') or chosen.get('city') or '—'} "
        f"(AMS {mt_round(ams)} MT).\n",
        "### Monthly volume\n",
        "| Month | MT |",
        "| --- | --- |",
    ]
    for r in month_rows:
        lines.append(f"| {r['month']} | {r['volume_mt']} |")
    lines.append(f"| **Total ({months_back}m)** | **{total}** |")
    if not pack_mix.empty:
        lines += ["\n### Packing mix (same window)\n", "| Packing | MT |", "| --- | --- |"]
        for k, v in pack_mix.head(8).items():
            lines.append(f"| {k} | {mt_round(v)} |")
    if not oil_mix.empty:
        lines += ["\n### Oil mix\n", "| Oil | MT |", "| --- | --- |"]
        for k, v in oil_mix.head(6).items():
            lines.append(f"| {k} | {mt_round(v)} |")
    tips = [
        f"AMS baseline is **{mt_round(ams)} MT**/month.",
    ]
    if month_rows:
        last = month_rows[-1]
        tips.append(
            f"Latest month **{last['month']}**: {last['volume_mt']} MT "
            + (
                f"({(last['volume_mt'] - ams) / ams * 100:+.0f}% vs AMS)."
                if ams
                else "."
            )
        )
    return {
        "ok": True,
        "mode": "party_profile",
        "party": name,
        "match": chosen,
        "ams_mt": mt_round(ams),
        "months": month_rows,
        "answer_markdown": _analysis(lines, tips),
        "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
    }


# ---------------------------------------------------------------------------
# Dumping / excessive sales
# ---------------------------------------------------------------------------

def detect_dumping(
    *,
    period: str | None = None,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    exclude_client_types: list[str] | None = None,
    group_by: str | None = None,  # None = overall party; or bu/pack/oil/city/client_type
    limit: int = 40,
) -> dict[str, Any]:
    """Invoice line MT > party's AMS for that product → excessive / dumping flag."""
    period_info = resolve_period(period or "this month")
    if period_info.get("ok") is False:
        return {"ok": False, "error": period_info.get("error")}
    d0, d1 = period_info["date_from"], period_info["date_to"]
    as_of = date.fromisoformat(d1)
    bu = _normalize_business_unit(business_unit)
    oil = normalize_oil_type(oil_type)
    pack = normalize_packing_category(packing_category)
    ctype = normalize_client_type(client_type)

    # AMS by party×product over prior 3 months
    ranges = _prior_three_month_ranges(as_of.replace(day=1))
    monthly: list[dict[tuple[str, str], float]] = []
    keys: set[tuple[str, str]] = set()
    for start, end in ranges:
        f = _fetch_filtered_lines(
            date_from=start.isoformat(), date_to=end.isoformat(),
            # AMS universe: do not apply city/client filters unless user asked overall
            # User said: first show overall unless prior filter — we apply filters if present
            city=city, client_type=ctype, business_unit=bu,
            oil_type=oil, packing_category=pack,
            exclude_client_types=exclude_client_types,
        )
        if f.empty:
            monthly.append({})
            continue
        totals = {
            (str(r.party), str(r.product)): float(r.mt)
            for r in f.groupby(["party", "product"], as_index=False)["mt"].sum().itertuples()
        }
        keys.update(totals)
        monthly.append(totals)
    ams_pp = {
        k: sum(m.get(k, 0.0) for m in monthly) / 3.0 for k in keys
    }

    cur = _fetch_filtered_lines(
        date_from=d0, date_to=d1,
        city=city, client_type=ctype, business_unit=bu,
        oil_type=oil, packing_category=pack,
        exclude_client_types=exclude_client_types,
    )
    if cur.empty:
        return {
            "ok": True, "mode": "dumping", "period": period_info,
            "cases": [],
            "answer_markdown": "No sales in period to check for dumping.\n",
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    # Aggregate by invoice×party×product
    cur = cur.copy()
    cur["inv_key"] = cur["inv_no"].fillna("").astype(str).str.strip()
    missing = cur["inv_key"] == ""
    cur.loc[missing, "inv_key"] = (
        cur.loc[missing, "date"].astype(str) + "|" + cur.loc[missing, "party"].astype(str)
    )
    inv = (
        cur.groupby(
            ["inv_key", "date", "party", "product", "client_type", "city",
             "business_unit", "oil_type", "packing_category"],
            as_index=False,
        )["mt"].sum()
    )
    cases = []
    for _, r in inv.iterrows():
        key = (str(r["party"]), str(r["product"]))
        ams_v = float(ams_pp.get(key, 0.0))
        mt = float(r["mt"])
        if ams_v <= 0:
            continue
        if mt <= ams_v:
            continue
        cases.append({
            "party": str(r["party"]),
            "product": str(r["product"]),
            "inv_no": str(r["inv_key"]),
            "date": str(r["date"])[:10],
            "client_type": str(r["client_type"]),
            "city": str(r["city"]),
            "business_unit": str(r["business_unit"]),
            "oil_type": str(r["oil_type"]),
            "packing_category": str(r["packing_category"]),
            "line_mt": mt_round(mt),
            "ams_mt": mt_round(ams_v),
            "multiple": pct_round(mt / ams_v) if ams_v else None,
            "excess_mt": mt_round(mt - ams_v),
        })
    cases.sort(key=lambda c: (-(c["excess_mt"] or 0), -(c["line_mt"] or 0)))

    filters = {
        "city": city, "client_type": ctype, "business_unit": bu,
        "oil_type": oil, "packing_category": pack,
        "exclude_client_types": exclude_client_types,
    }

    if group_by in {"business_unit", "packing_category", "oil_type", "client_type", "city"}:
        g = {}
        for c in cases:
            k = c.get(group_by) or "—"
            g.setdefault(k, {"cases": 0, "excess_mt": 0, "line_mt": 0})
            g[k]["cases"] += 1
            g[k]["excess_mt"] += c["excess_mt"]
            g[k]["line_mt"] += c["line_mt"]
        rows = sorted(g.items(), key=lambda x: -x[1]["excess_mt"])
        lines = [
            f"Dumping / excessive sales by **{group_by.replace('_', ' ')}** — "
            f"{period_info.get('label')} · {_scope_bits(filters)}.\n",
            f"| {group_by.replace('_', ' ').title()} | Cases | Excess MT | Line MT |",
            "| --- | --- | --- | --- |",
        ]
        for k, v in rows:
            lines.append(
                f"| {k} | {v['cases']} | {mt_round(v['excess_mt'])} | {mt_round(v['line_mt'])} |"
            )
        tips = [
            f"{len(cases)} invoice lines exceed party×product AMS.",
        ]
        if rows:
            tips.append(
                f"**{rows[0][0]}** has the most excess MT ({mt_round(rows[0][1]['excess_mt'])})."
            )
        return {
            "ok": True, "mode": "dumping_grouped", "group_by": group_by,
            "period": period_info, "filters": filters, "cases": cases[:limit],
            "answer_markdown": _analysis(lines, tips),
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    show = cases[:limit]
    lines = [
        f"Dumping / excessive sales (invoice line MT > party×product AMS) — "
        f"{period_info.get('label')} · {_scope_bits(filters)}.\n",
        "| # | Party | Product | Inv | Date | Line MT | AMS | ×AMS | Excess |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, c in enumerate(show, 1):
        mult = f"{c['multiple']:.1f}×" if c.get("multiple") is not None else "—"
        lines.append(
            f"| {i} | {c['party']} | {c['product'][:40]} | {c['inv_no'][:16]} | "
            f"{c['date']} | {c['line_mt']} | {c['ams_mt']} | {mult} | {c['excess_mt']} |"
        )
    if not show:
        lines = [
            f"No dumping cases for {period_info.get('label')} · {_scope_bits(filters)}.\n"
        ]
    tips = []
    if show:
        tips.append(
            f"{len(cases)} cases; largest excess: **{show[0]['party']}** / "
            f"{show[0]['product'][:30]} (+{show[0]['excess_mt']} MT over AMS)."
        )
        tips.append(
            "Ask to break down by BU, packing, oil type, client type, or city."
        )
    return {
        "ok": True, "mode": "dumping", "period": period_info, "filters": filters,
        "case_count": len(cases), "cases": show,
        "answer_markdown": _analysis(lines, tips),
        "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
    }


def detect_price_dispersion(
    *,
    period: str | None = None,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    exclude_client_types: list[str] | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    """Find same-date (+product) cases where distributors paid different rates."""
    period_info = resolve_period(period or "this month")
    if period_info.get("ok") is False:
        return {"ok": False, "error": period_info.get("error")}
    d0, d1 = period_info["date_from"], period_info["date_to"]
    bu = _normalize_business_unit(business_unit)
    oil = normalize_oil_type(oil_type)
    pack = normalize_packing_category(packing_category)
    ctype = normalize_client_type(client_type) or "Eva Distributors"

    cur = _fetch_filtered_lines(
        date_from=d0,
        date_to=d1,
        city=city,
        client_type=ctype,
        business_unit=bu,
        oil_type=oil,
        packing_category=pack,
        exclude_client_types=exclude_client_types,
    )
    filters = {
        "city": city,
        "client_type": ctype,
        "business_unit": bu,
        "oil_type": oil,
        "packing_category": pack,
        "exclude_client_types": exclude_client_types,
    }
    if cur.empty:
        return {
            "ok": True,
            "mode": "price_dispersion",
            "period": period_info,
            "filters": filters,
            "cases": [],
            "answer_markdown": (
                f"No sales with rates for {period_info.get('label')} · "
                f"{_scope_bits(filters)}.\n"
            ),
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    frame = cur.copy()
    frame["rate"] = pd.to_numeric(frame.get("rate"), errors="coerce")
    frame["mt"] = pd.to_numeric(frame.get("mt"), errors="coerce").fillna(0.0)
    frame = frame[frame["rate"].notna() & (frame["rate"] > 0)]
    if frame.empty:
        return {
            "ok": True,
            "mode": "price_dispersion",
            "period": period_info,
            "filters": filters,
            "cases": [],
            "answer_markdown": (
                f"No usable rate values for {period_info.get('label')} · "
                f"{_scope_bits(filters)}.\n"
            ),
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    frame["date_key"] = frame["date"].astype(str).str.slice(0, 10)
    # Party-level weighted average rate per date×product
    frame["rate_mt"] = frame["rate"] * frame["mt"]
    party_rates = (
        frame.groupby(
            ["date_key", "product", "party", "city", "client_type"],
            as_index=False,
        )
        .agg(mt=("mt", "sum"), rate_mt=("rate_mt", "sum"))
    )
    party_rates["rate"] = party_rates.apply(
        lambda r: (float(r["rate_mt"]) / float(r["mt"])) if float(r["mt"]) else None,
        axis=1,
    )
    party_rates = party_rates[party_rates["rate"].notna()]

    cases: list[dict[str, Any]] = []
    for (dt, prod), grp in party_rates.groupby(["date_key", "product"]):
        rates = sorted({round(float(x), 4) for x in grp["rate"].tolist()})
        if len(rates) < 2:
            continue
        parties = sorted(
            {
                (
                    str(r["party"]),
                    round(float(r["rate"]), 2),
                    round(float(r["mt"]), 3),
                    str(r.get("city") or ""),
                )
                for _, r in grp.iterrows()
            },
            key=lambda x: (-x[1], -x[2], x[0]),
        )
        spread = rates[-1] - rates[0]
        cases.append(
            {
                "date": str(dt),
                "product": str(prod),
                "party_count": len(parties),
                "min_rate": round(rates[0], 2),
                "max_rate": round(rates[-1], 2),
                "spread": round(spread, 2),
                "parties": parties[:8],
            }
        )
    cases.sort(key=lambda c: (-(c["spread"] or 0), -c["party_count"], c["date"]))
    show = cases[: max(1, min(int(limit or 40), 100))]

    lines = [
        "Same-date price differences across distributors "
        f"(date × product) — {period_info.get('label')} · {_scope_bits(filters)}.\n",
        "| # | Date | Product | Parties | Min rate | Max rate | Spread | Examples |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, c in enumerate(show, 1):
        examples = "; ".join(
            f"{p[0][:28]}@{p[1]}" for p in (c.get("parties") or [])[:3]
        )
        prod = str(c["product"])[:36]
        lines.append(
            f"| {i} | {c['date']} | {prod} | {c['party_count']} | "
            f"{c['min_rate']} | {c['max_rate']} | {c['spread']} | {examples} |"
        )
    tips: list[str] = []
    if show:
        tips.append(
            f"{len(cases)} date×product groups have different distributor rates."
        )
        top = show[0]
        tips.append(
            f"Largest spread **{top['spread']}** on **{top['date']}** / "
            f"{str(top['product'])[:40]} "
            f"(min {top['min_rate']} → max {top['max_rate']})."
        )
    else:
        lines = [
            "No same-date price differences across distributors for "
            f"{period_info.get('label')} · {_scope_bits(filters)}.\n"
        ]
        tips.append(
            "All distributors with rates paid the same rate per product on each date "
            "in this scope."
        )
    return {
        "ok": True,
        "mode": "price_dispersion",
        "period": period_info,
        "filters": filters,
        "case_count": len(cases),
        "cases": show,
        "answer_markdown": _analysis(lines, tips),
        "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
    }


# ---------------------------------------------------------------------------
# Top SKUs (product volume table)
# ---------------------------------------------------------------------------

def top_skus(
    *,
    period: str | None = None,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    exclude_client_types: list[str] | None = None,
    limit: int = 10,
    sort: str = "desc",
) -> dict[str, Any]:
    period_info = resolve_period(period or "this month")
    if period_info.get("ok") is False:
        return {"ok": False, "error": period_info.get("error")}
    frame = _fetch_filtered_lines(
        date_from=period_info["date_from"], date_to=period_info["date_to"],
        city=city, client_type=normalize_client_type(client_type),
        business_unit=_normalize_business_unit(business_unit),
        oil_type=normalize_oil_type(oil_type),
        packing_category=normalize_packing_category(packing_category),
        exclude_client_types=exclude_client_types,
    )
    if frame.empty:
        return {
            "ok": True, "mode": "top_skus", "period": period_info, "rows": [],
            "answer_markdown": "No SKU sales in scope.\n",
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }
    g = frame.groupby("product")["mt"].sum().sort_values(ascending=(sort == "asc"))
    total = float(g.sum())
    rows = []
    for k, v in g.head(limit).items():
        rows.append({
            "product": str(k),
            "volume_mt": mt_round(v),
            "share_pct": pct_round(100.0 * float(v) / total if total else 0),
        })
    filters = {
        "city": city, "client_type": client_type, "business_unit": business_unit,
        "oil_type": oil_type, "packing_category": packing_category,
        "exclude_client_types": exclude_client_types,
    }
    lines = [
        f"{'Bottom' if sort == 'asc' else 'Top'} SKUs — {period_info.get('label')} · "
        f"{_scope_bits(filters)}.\n",
        "| # | Product | Volume (MT) | Share % |",
        "| --- | --- | --- | --- |",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {r['product']} | {r['volume_mt']} | {r['share_pct']}% |"
        )
    tips = []
    if rows:
        tips.append(
            f"**{rows[0]['product']}** is {rows[0]['share_pct']:.0f}% of SKU volume in this cut."
        )
    return {
        "ok": True, "mode": "top_skus", "period": period_info, "filters": filters,
        "rows": rows,
        "answer_markdown": _analysis(lines, tips),
        "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
    }


# ---------------------------------------------------------------------------
# Filter entities by volume / YoY / MoM conditions
# ---------------------------------------------------------------------------

_ENTITY_DIMS = {
    "party": "party",
    "parties": "party",
    "distributor": "party",
    "distributors": "party",
    "customer": "party",
    "customers": "party",
    "client": "party",
    "clients": "party",
    "product": "product",
    "products": "product",
    "sku": "product",
    "skus": "product",
    "packing": "packing_category",
    "packing_category": "packing_category",
    "oil": "oil_type",
    "oil_type": "oil_type",
    "business_unit": "business_unit",
    "bu": "business_unit",
    "city": "city",
    "cities": "city",
    "channel": "client_type",
    "channels": "client_type",
    "client_type": "client_type",
    "client_types": "client_type",
    "trade_channel": "client_type",
    "trade_channels": "client_type",
    "client_type": "client_type",
}


def _yoy_prior_dates(d0: str, d1: str) -> tuple[str, str]:
    c0 = date.fromisoformat(d0)
    c1 = date.fromisoformat(d1)
    try:
        return (
            c0.replace(year=c0.year - 1).isoformat(),
            c1.replace(year=c1.year - 1).isoformat(),
        )
    except ValueError:
        return (
            c0.replace(year=c0.year - 1, day=28).isoformat(),
            c1.replace(year=c1.year - 1, day=28).isoformat(),
        )


def _mom_prior_dates(d0: str, d1: str) -> tuple[str, str]:
    """Previous calendar month (full month) for 'vs last month' filters."""
    c0 = date.fromisoformat(d0)
    if c0.month == 1:
        p_start = date(c0.year - 1, 12, 1)
    else:
        p_start = date(c0.year, c0.month - 1, 1)
    last_day = calendar.monthrange(p_start.year, p_start.month)[1]
    p_end = date(p_start.year, p_start.month, last_day)
    return p_start.isoformat(), p_end.isoformat()


def _passes_condition(
    value: float | None,
    *,
    op: str,
    threshold: float | None,
    metric: str,
) -> bool:
    thr = 0.0 if threshold is None else float(threshold)
    if metric in {"yoy", "mom"}:
        if value is None:
            return False
        if op in {"grown", "gt"}:
            return value > thr
        if op == "gte":
            return value >= thr
        if op in {"declined", "lt"}:
            # "declined more than 10%" → value < -10
            cut = -abs(thr) if op == "declined" else thr
            return value < cut
        if op == "lte":
            return value <= thr
        if op == "eq":
            return abs(value - thr) < 1e-9
        return False
    # volume
    v = 0.0 if value is None else float(value)
    if op in {"grown", "gt"}:
        return v > thr
    if op == "gte":
        return v >= thr
    if op in {"declined", "lt"}:
        return v < thr
    if op == "lte":
        return v <= thr
    if op == "eq":
        return abs(v - thr) < 1e-9
    return False


def _group_volumes(frame: pd.DataFrame, dim: str) -> pd.DataFrame:
    if frame.empty:
        cols = [dim, "mt"]
        if dim == "party":
            cols += ["city", "client_type"]
        return pd.DataFrame(columns=cols)
    if dim == "party":
        return (
            frame.groupby("party", as_index=False)
            .agg(
                mt=("mt", "sum"),
                city=("city", "first"),
                client_type=("client_type", "first"),
            )
        )
    g = frame.groupby(dim, as_index=False)["mt"].sum()
    return g


def filter_entities(
    *,
    entity: str = "party",
    metric: str = "volume",  # volume | yoy | mom
    op: str = "gt",  # gt | gte | lt | lte | eq | grown | declined
    threshold: float | None = None,
    period: str | None = None,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    exclude_client_types: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List entities matching a volume or growth condition.

    Examples:
      volume > 10 MT; YoY declined (or declined more than 10%);
      MoM more this month than last month.
    """
    dim_key = (entity or "party").strip().lower().replace(" ", "_").replace("-", "_")
    dim = _ENTITY_DIMS.get(dim_key, dim_key)
    if dim not in {
        "party", "product", "packing_category", "oil_type",
        "business_unit", "city", "client_type",
    }:
        dim = "party"

    met = (metric or "volume").strip().lower()
    if met in {"sales", "mt", "tons", "tonnes", "absolute"}:
        met = "volume"
    if met in {"yoy_pct", "year_over_year", "growth", "yoy_growth"}:
        met = "yoy"
    if met in {"month_over_month", "mom_pct", "vs_last_month"}:
        met = "mom"
    if met not in {"volume", "yoy", "mom"}:
        met = "volume"

    op_n = (op or "gt").strip().lower()
    if op_n in {"greater", "greater_than", "above", "over", "more", "more_than", ">"}:
        op_n = "gt"
    if op_n in {"less", "less_than", "below", "under", "<"}:
        op_n = "lt"
    if op_n in {"grow", "grew", "increased", "increase", "up", "positive"}:
        op_n = "grown"
    if op_n in {"decline", "dropped", "drop", "fallen", "fall", "decreased", "decrease", "down", "negative"}:
        op_n = "declined"
    if op_n not in {"gt", "gte", "lt", "lte", "eq", "grown", "declined"}:
        op_n = "gt"

    period_info = resolve_period(period or ("this month" if met == "mom" else "this month"))
    if period_info.get("ok") is False:
        return {"ok": False, "error": period_info.get("error")}
    d0, d1 = period_info["date_from"], period_info["date_to"]
    bu = _normalize_business_unit(business_unit)
    oil = normalize_oil_type(oil_type)
    pack = normalize_packing_category(packing_category)
    ctype = normalize_client_type(client_type)

    fetch_kw = dict(
        city=city, client_type=ctype, business_unit=bu, oil_type=oil,
        packing_category=pack, exclude_client_types=exclude_client_types,
    )
    cur = _fetch_filtered_lines(date_from=d0, date_to=d1, **fetch_kw)
    cur_g = _group_volumes(cur, dim)

    prior_label = None
    pri_g = None
    if met in {"yoy", "mom"}:
        if met == "yoy":
            p0, p1 = _yoy_prior_dates(d0, d1)
            prior_label = f"same period last year ({p0} → {p1})"
        else:
            p0, p1 = _mom_prior_dates(d0, d1)
            prior_label = f"prior month ({p0} → {p1})"
        pri = _fetch_filtered_lines(date_from=p0, date_to=p1, **fetch_kw)
        pri_g = _group_volumes(pri, dim)

    # Build candidate keys
    keys: set[str] = set()
    if not cur_g.empty:
        keys |= set(cur_g[dim].astype(str))
    if pri_g is not None and not pri_g.empty:
        keys |= set(pri_g[dim].astype(str))

    cur_map = {}
    if not cur_g.empty:
        for _, r in cur_g.iterrows():
            cur_map[str(r[dim])] = r
    pri_map = {}
    if pri_g is not None and not pri_g.empty:
        for _, r in pri_g.iterrows():
            pri_map[str(r[dim])] = r

    rows: list[dict[str, Any]] = []
    for k in keys:
        c_row = cur_map.get(k)
        p_row = pri_map.get(k)
        c_mt = float(c_row["mt"]) if c_row is not None else 0.0
        p_mt = float(p_row["mt"]) if p_row is not None else 0.0
        if met == "volume":
            score = c_mt
            change_pct = None
        else:
            change_pct = pct_change(c_mt, p_mt)
            score = change_pct
        if not _passes_condition(score, op=op_n, threshold=threshold, metric=met):
            continue
        item: dict[str, Any] = {
            "entity": k,
            "volume_mt": mt_round(c_mt),
        }
        if met != "volume":
            item["prior_mt"] = mt_round(p_mt)
            item["delta_mt"] = mt_round(c_mt - p_mt)
            item["change_pct"] = pct_round(change_pct) if change_pct is not None else None
        if dim == "party":
            src = c_row if c_row is not None else p_row
            if src is not None:
                item["city"] = str(src.get("city") or "")
                item["client_type"] = str(src.get("client_type") or "")
        rows.append(item)

    # Sort: declined → most negative first; grown/gt volume → highest first; lt volume → lowest
    reverse = op_n in {"grown", "gt", "gte"} or (
        met == "volume" and op_n not in {"lt", "lte", "declined"}
    )
    if met == "volume":
        rows.sort(key=lambda r: r["volume_mt"], reverse=reverse)
    else:
        rows.sort(
            key=lambda r: (
                r["change_pct"] is None,
                -(r["change_pct"] if r["change_pct"] is not None else 0)
                if reverse
                else (r["change_pct"] if r["change_pct"] is not None else 0),
                -r["volume_mt"],
            )
        )

    lim = max(1, min(int(limit or 50), 200))
    rows = rows[:lim]

    filters = {
        "city": city, "client_type": ctype, "business_unit": bu,
        "oil_type": oil, "packing_category": pack,
        "exclude_client_types": exclude_client_types,
    }

    # Condition label
    thr_s = f"{threshold:g}" if threshold is not None else None
    if met == "volume":
        op_words = {
            "gt": f"sales > {thr_s} MT",
            "gte": f"sales ≥ {thr_s} MT",
            "lt": f"sales < {thr_s} MT",
            "lte": f"sales ≤ {thr_s} MT",
            "eq": f"sales = {thr_s} MT",
            "grown": f"sales > {thr_s or '0'} MT",
            "declined": f"sales < {thr_s or '0'} MT",
        }
        cond = op_words.get(op_n, f"sales filter ({op_n})")
        metric_title = "Volume"
    elif met == "yoy":
        if op_n == "declined":
            cond = (
                f"YoY declined more than {thr_s}%"
                if thr_s and float(threshold or 0) > 0
                else "YoY declined"
            )
        elif op_n == "grown":
            cond = (
                f"YoY grown more than {thr_s}%"
                if thr_s and float(threshold or 0) > 0
                else "YoY grown"
            )
        else:
            cond = f"YoY {op_n} {thr_s}%"
        metric_title = "YoY %"
    else:
        if op_n == "declined":
            cond = (
                f"MoM declined more than {thr_s}%"
                if thr_s and float(threshold or 0) > 0
                else "MoM declined (less than last month)"
            )
        elif op_n == "grown":
            cond = (
                f"MoM grown more than {thr_s}%"
                if thr_s and float(threshold or 0) > 0
                else "more sales this month than last month"
            )
        else:
            cond = f"MoM {op_n} {thr_s}%"
        metric_title = "MoM %"

    dim_title = dim.replace("_", " ").title()
    if dim == "party":
        dim_title = "Party"
    list_title = "Parties" if dim == "party" else f"{dim_title}s"

    lines = [
        f"{list_title} where {cond} — {period_info.get('label')} · {_scope_bits(filters)}.",
    ]
    if prior_label:
        lines.append(f"Compare vs {prior_label}.")
    lines.append("")

    if dim == "party":
        if met == "volume":
            lines += [
                "| # | Party | City | Client Type | Volume (MT) |",
                "| --- | --- | --- | --- | --- |",
            ]
            for i, r in enumerate(rows, 1):
                lines.append(
                    f"| {i} | {r['entity']} | {r.get('city', '')} | "
                    f"{r.get('client_type', '')} | {r['volume_mt']} |"
                )
        else:
            lines += [
                f"| # | Party | City | Client Type | Volume | Prior | {metric_title} | Δ MT |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
            for i, r in enumerate(rows, 1):
                ch = r.get("change_pct")
                cs = f"{ch:+.1f}%" if ch is not None else "—"
                lines.append(
                    f"| {i} | {r['entity']} | {r.get('city', '')} | "
                    f"{r.get('client_type', '')} | {r['volume_mt']} | "
                    f"{r.get('prior_mt', 0)} | {cs} | {r.get('delta_mt', 0)} |"
                )
    else:
        if met == "volume":
            lines += [
                f"| # | {dim_title} | Volume (MT) |",
                "| --- | --- | --- |",
            ]
            for i, r in enumerate(rows, 1):
                lines.append(f"| {i} | {r['entity']} | {r['volume_mt']} |")
        else:
            lines += [
                f"| # | {dim_title} | Volume | Prior | {metric_title} | Δ MT |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
            for i, r in enumerate(rows, 1):
                ch = r.get("change_pct")
                cs = f"{ch:+.1f}%" if ch is not None else "—"
                lines.append(
                    f"| {i} | {r['entity']} | {r['volume_mt']} | "
                    f"{r.get('prior_mt', 0)} | {cs} | {r.get('delta_mt', 0)} |"
                )

    if not rows:
        lines.append("_No matching entities._")

    tips = []
    if rows:
        tip_name = rows[0]["entity"]
        if met == "volume":
            tips.append(f"**{tip_name}** leads this filter at **{rows[0]['volume_mt']} MT**.")
        else:
            ch = rows[0].get("change_pct")
            if ch is not None:
                tips.append(f"**{tip_name}** is extreme in this cut at **{ch:+.1f}%**.")
        tips.append(f"Showing {len(rows)} match(es) (limit {lim}).")

    return {
        "ok": True,
        "mode": "filter_entities",
        "entity": dim,
        "metric": met,
        "op": op_n,
        "threshold": threshold,
        "period": period_info,
        "filters": filters,
        "rows": rows,
        "answer_markdown": _analysis(lines, tips),
        "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
    }
