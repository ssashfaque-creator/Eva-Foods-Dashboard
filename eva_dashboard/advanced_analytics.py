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
    _PARTY_JOIN,
    _normalize_business_unit,
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

_CLIENT_TYPE_EXPR = """
COALESCE(
  NULLIF(trim(cl.type), ''),
  NULLIF(trim(s.client_type), ''),
  'Unmapped'
)
"""
_CITY_EXPR = "COALESCE(NULLIF(trim(cl.city_filter), ''), 'Unmapped')"


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
    init_db()
    params: list[Any] = [date_from, date_to]
    where = ["s.date >= ?", "s.date <= ?"]
    if city:
        where.append(f"lower(trim({_CITY_EXPR})) = lower(trim(?))")
        params.append(city)
    if client_type:
        where.append(f"lower(trim({_CLIENT_TYPE_EXPR})) = lower(trim(?))")
        params.append(client_type)
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
    for ex in exclude_client_types or []:
        where.append(f"lower(trim({_CLIENT_TYPE_EXPR})) != lower(trim(?))")
        params.append(ex)
    for ex in exclude_cities or []:
        where.append(f"lower(trim({_CITY_EXPR})) != lower(trim(?))")
        params.append(ex)

    sql = f"""
    SELECT
      s.date, s.party, s.inv_no, s.product,
      {_CLIENT_TYPE_EXPR} AS client_type,
      {_CITY_EXPR} AS city,
      COALESCE(NULLIF(trim(c.category_1), ''), '(unmapped)') AS business_unit,
      COALESCE(NULLIF(trim(c.category_2), ''), '(unmapped)') AS oil_type,
      COALESCE(NULLIF(trim(c.packing_category), ''), '(unmapped)') AS packing_category,
      {_MT_SQL} AS mt
    FROM sales s
    {_PARTY_JOIN}
    WHERE {' AND '.join(where)}
    """
    with connect() as conn:
        return pd.read_sql_query(sql, conn, params=params)


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
    left: str,
    right: str,
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
    """Side-by-side city or client-type compare (volume and/or YoY growth)."""
    period_info = resolve_period(period or "this month", date_from=date_from, date_to=date_to)
    if period_info.get("ok") is False:
        return {"ok": False, "error": period_info.get("error")}
    d0, d1 = period_info["date_from"], period_info["date_to"]
    bu = _normalize_business_unit(business_unit)
    oil = normalize_oil_type(oil_type)
    pack = normalize_packing_category(packing_category)
    ctype = normalize_client_type(client_type)
    seg = "city" if segment == "city" else "client_type"

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

    left_v = _vol(left, d0, d1)
    right_v = _vol(right, d0, d1)
    left_p = _vol(left, p0, p1)
    right_p = _vol(right, p0, p1)
    left_g = pct_change(left_v, left_p)
    right_g = pct_change(right_v, right_p)

    filters = {
        "business_unit": bu,
        "oil_type": oil,
        "packing_category": pack,
        "client_type": ctype if seg == "city" else None,
        "city": city if seg != "city" else None,
        "exclude_client_types": exclude_client_types,
    }
    title = f"{left} vs {right}"
    lines = [
        f"Compare {seg.replace('_', ' ')} — **{title}** · {period_info.get('label')} "
        f"({_scope_bits(filters)}).\n",
        f"| {seg.replace('_', ' ').title()} | Volume (MT) | Prior YoY (MT) | YoY % |",
        "| --- | --- | --- | --- |",
    ]
    for name, vol, prior, growth in (
        (left, left_v, left_p, left_g),
        (right, right_v, right_p, right_g),
    ):
        gs = f"{growth:+.1f}%" if growth is not None else "—"
        lines.append(f"| {name} | {mt_round(vol)} | {mt_round(prior)} | {gs} |")
    lines.append(f"| **Gap ({left} − {right})** | **{mt_round(left_v - right_v)}** | | |")
    tips = []
    if left_v >= right_v:
        tips.append(
            f"**{left}** leads **{right}** by {mt_round(left_v - right_v)} MT this period."
        )
    else:
        tips.append(
            f"**{right}** leads **{left}** by {mt_round(right_v - left_v)} MT this period."
        )
    if left_g is not None and right_g is not None:
        if left_g > right_g:
            tips.append(
                f"**{left}** is growing faster YoY ({left_g:+.1f}% vs {right_g:+.1f}%)."
            )
        elif right_g > left_g:
            tips.append(
                f"**{right}** is growing faster YoY ({right_g:+.1f}% vs {left_g:+.1f}%)."
            )
        else:
            tips.append("YoY growth rates are similar.")

    return {
        "ok": True,
        "mode": "compare_segments",
        "segment": seg,
        "metric": metric,
        "period": period_info,
        "filters": filters,
        "left": {"name": left, "volume_mt": mt_round(left_v), "prior_mt": mt_round(left_p),
                 "yoy_pct": pct_round(left_g)},
        "right": {"name": right, "volume_mt": mt_round(right_v), "prior_mt": mt_round(right_p),
                  "yoy_pct": pct_round(right_g)},
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
