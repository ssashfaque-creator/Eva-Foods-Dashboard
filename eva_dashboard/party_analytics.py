"""Client lists and party-level analytics (AMS, share, YoY) for the chatbot."""

from __future__ import annotations

import calendar
import re
from datetime import date
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
from eva_dashboard.fmt import mt_round
from eva_dashboard.sales_query import (
    _PARTY_JOIN,
    _normalize_business_unit,
    _sales_date_bounds,
    query_sales,
    resolve_period,
)

_MT_EXPR = """
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


def list_known_cities() -> list[str]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT city_filter AS city
            FROM clients
            WHERE city_filter IS NOT NULL
              AND trim(city_filter) != ''
              AND lower(trim(city_filter)) != 'undefined'
            ORDER BY city_filter
            """
        ).fetchall()
    return [str(r["city"]).strip() for r in rows if r["city"]]


def extract_city_from_text(text: str) -> str | None:
    t = (text or "").strip()
    if not t:
        return None
    lower = t.lower()
    # Prefer longer city names first
    cities = sorted(list_known_cities(), key=len, reverse=True)
    for city in cities:
        c = city.lower()
        if re.search(r"(?<!\w)" + re.escape(c) + r"(?!\w)", lower):
            return city
    # Common fallbacks even if not yet in clients
    for city in (
        "Lahore",
        "Karachi",
        "Islamabad",
        "Rawalpindi",
        "Faisalabad",
        "Multan",
        "Peshawar",
        "Quetta",
        "Hyderabad",
        "Sialkot",
        "Gujranwala",
    ):
        if re.search(r"(?<!\w)" + re.escape(city.lower()) + r"(?!\w)", lower):
            return city
    return None


def _fetch_party_lines(
    *,
    date_from: str,
    date_to: str,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    brand_prefix: str | None = None,
) -> pd.DataFrame:
    """Line-level MT with party + taxonomy for analytics."""
    init_db()
    params: list[Any] = [date_from, date_to]
    where = ["s.date >= ?", "s.date <= ?", "s.party IS NOT NULL", "trim(s.party) != ''"]
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

    sql = f"""
    SELECT
      s.date,
      s.party,
      s.inv_no,
      s.product,
      {_CLIENT_TYPE_EXPR} AS client_type,
      {_CITY_EXPR} AS city,
      COALESCE(NULLIF(trim(c.category_1), ''), '(unmapped)') AS business_unit,
      COALESCE(NULLIF(trim(c.category_2), ''), '(unmapped)') AS oil_type,
      COALESCE(NULLIF(trim(c.packing_category), ''), '(unmapped)') AS packing_category,
      {_MT_EXPR} AS mt
    FROM sales s
    {_PARTY_JOIN}
    WHERE {' AND '.join(where)}
    """
    with connect() as conn:
        return pd.read_sql_query(sql, conn, params=params)


def _first_sale_dates(
    *,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    brand_prefix: str | None = None,
) -> dict[str, str]:
    """Earliest sale date per party (optional filters)."""
    init_db()
    params: list[Any] = []
    where = ["s.party IS NOT NULL", "trim(s.party) != ''", "s.date IS NOT NULL"]
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
    sql = f"""
    SELECT s.party, MIN(s.date) AS first_sale
    FROM sales s
    {_PARTY_JOIN}
    WHERE {' AND '.join(where)}
    GROUP BY s.party
    """
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {str(r["party"]): str(r["first_sale"])[:10] for r in rows}


def _ams_by_party(
    *,
    as_of: date,
    city: str | None,
    client_type: str | None,
    business_unit: str | None,
    oil_type: str | None,
    packing_category: str | None,
    brand_prefix: str | None,
) -> dict[str, float]:
    ranges = _prior_three_month_ranges(as_of)
    monthly: list[dict[str, float]] = []
    keys: set[str] = set()
    for start, end in ranges:
        frame = _fetch_party_lines(
            date_from=start.isoformat(),
            date_to=end.isoformat(),
            city=city,
            client_type=client_type,
            business_unit=business_unit,
            oil_type=oil_type,
            packing_category=packing_category,
            brand_prefix=brand_prefix,
        )
        if frame.empty:
            monthly.append({})
            continue
        totals = {
            str(k): float(v)
            for k, v in frame.groupby("party")["mt"].sum().items()
        }
        keys.update(totals)
        monthly.append(totals)
    return {key: sum(m.get(key, 0.0) for m in monthly) / 3.0 for key in keys}


def list_clients(
    *,
    city: str | None = None,
    client_type: str | None = None,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 200,
    include_zero: bool = False,
) -> dict[str, Any]:
    """List clients matching city (City-Filter) and/or client type — not fuzzy name search."""
    city_f = (city or "").strip() or None
    ctype = normalize_client_type((client_type or "").strip() or None)
    if not city_f and not ctype:
        return {
            "ok": False,
            "error": "Pass city and/or client_type (e.g. Lahore + Eva Distributors).",
        }

    period_info = None
    d0 = d1 = None
    if period or date_from or date_to:
        period_info = resolve_period(period, date_from=date_from, date_to=date_to)
        if period_info.get("ok") is False:
            return {"ok": False, "error": period_info.get("error"), "period": period_info}
        d0, d1 = period_info["date_from"], period_info["date_to"]
    else:
        # Default: all-time sales MT for ranking within the list
        min_d, max_d = _sales_date_bounds()
        if min_d and max_d:
            d0, d1 = min_d.isoformat(), max_d.isoformat()
            period_info = {
                "ok": True,
                "date_from": d0,
                "date_to": d1,
                "label": f"All sales ({d0} → {d1})",
            }

    init_db()
    params: list[Any] = []
    where = ["cl.client IS NOT NULL", "trim(cl.client) != ''"]
    if city_f:
        where.append("lower(trim(COALESCE(cl.city_filter, ''))) = lower(trim(?))")
        params.append(city_f)
    if ctype:
        where.append("lower(trim(COALESCE(cl.type, ''))) = lower(trim(?))")
        params.append(ctype)

    sql = f"""
    SELECT
      cl.client AS client,
      COALESCE(NULLIF(trim(cl.type), ''), 'Unmapped') AS client_type,
      COALESCE(NULLIF(trim(cl.city_filter), ''), '') AS city_filter,
      COALESCE(NULLIF(trim(cl.city), ''), '') AS city,
      COALESCE(NULLIF(trim(cl.inactive), ''), '') AS inactive
    FROM clients cl
    WHERE {' AND '.join(where)}
    ORDER BY cl.client
    """
    with connect() as conn:
        clients = pd.read_sql_query(sql, conn, params=params)

    mt_map: dict[str, float] = {}
    if d0 and d1 and not clients.empty:
        sales = _fetch_party_lines(
            date_from=d0,
            date_to=d1,
            city=city_f,
            client_type=ctype,
        )
        if not sales.empty:
            mt_map = {
                str(k): float(v)
                for k, v in sales.groupby("party")["mt"].sum().items()
            }

    rows: list[dict[str, Any]] = []
    for _, r in clients.iterrows():
        name = str(r["client"])
        mt = round(mt_map.get(name, 0.0), 3)
        if not include_zero and mt <= 0:
            continue
        rows.append(
            {
                "client": name,
                "client_type": r["client_type"],
                "city_filter": r["city_filter"] or None,
                "city": r["city"] or None,
                "inactive": r["inactive"] or None,
                "mt": mt,
            }
        )
    rows.sort(key=lambda x: (-float(x["mt"]), str(x["client"]).lower()))
    lim = max(1, min(int(limit or 200), 500))
    rows = rows[:lim]
    total_mt = round(sum(float(r["mt"]) for r in rows), 3)

    scope_bits = []
    if ctype:
        scope_bits.append(f"Client Type **{ctype}**")
    if city_f:
        scope_bits.append(f"City-Filter **{city_f}**")
    scope = " · ".join(scope_bits) or "clients"
    period_label = (period_info or {}).get("label") or "all time"

    # Omit constant filter columns when every row shares the same value
    show_type = not (ctype and all(str(r["client_type"]) == ctype for r in rows))
    show_cf = not (
        city_f
        and all(str(r.get("city_filter") or "") == city_f for r in rows)
    )
    show_city = not (
        city_f
        and all(str(r.get("city") or "") in {city_f, ""} for r in rows)
        and all(str(r.get("city_filter") or "") == city_f for r in rows)
    )

    headers = ["#", "Client"]
    if show_type:
        headers.append("Client Type")
    if show_cf:
        headers.append("City-Filter")
    if show_city:
        headers.append("City")
    headers.append("MT")

    lines = [
        f"**{len(rows)}** clients — {scope} · volume period: {period_label}.\n",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for i, r in enumerate(rows, 1):
        cells = [str(i), str(r["client"]).replace("|", "/")]
        if show_type:
            cells.append(str(r["client_type"]).replace("|", "/"))
        if show_cf:
            cells.append(str(r["city_filter"] or "—").replace("|", "/"))
        if show_city:
            cells.append(str(r["city"] or "—").replace("|", "/"))
        cells.append(str(r["mt"]))
        lines.append("| " + " | ".join(cells) + " |")
    lines.append(f"\n_Listed MT total: **{total_mt}**"
                 + (" (zero-volume clients omitted)." if not include_zero else ".")
                 + "_")

    party_spec = {
        "kind": "list_clients",
        "filters": {"city": city_f, "client_type": ctype},
        "period_phrase": period,
        "period": {
            "date_from": (period_info or {}).get("date_from"),
            "date_to": (period_info or {}).get("date_to"),
            "label": (period_info or {}).get("label"),
        },
        "limit": lim,
        "include_zero": include_zero,
    }

    return {
        "ok": True,
        "mode": "list_clients",
        "filters": {"city": city_f, "client_type": ctype},
        "period": period_info,
        "count": len(rows),
        "total_mt": total_mt,
        "clients": rows,
        "party_spec": party_spec,
        "answer_markdown": "\n".join(lines) + "\n",
        "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
    }


def analyze_parties(
    *,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    brand: str | None = None,
    metric: str = "ams",
    compare_period: str | None = None,
    share_city: str | None = None,
    group_by: str = "party",
    mix_dimension: str | None = None,
    sort: str = "desc",
    limit: int = 10,
    per_party_mix: bool = False,
) -> dict[str, Any]:
    """Rank / summarize parties or cities.

    metric:
      volume | ams | vs_ams | underperformers | yoy | share_of_segment |
      segment_mix | geo_share | doing_well | new_parties | lost_parties |
      packing_mix | product_mix | invoices | invoice_mt

    group_by: party (default) | city
    mix_dimension: packing_category | product (for mix metrics)
    per_party_mix: when True with packing/product_mix, one mix table per party
    sort: desc (default) | asc  — underperformers force asc on % vs AMS
    Default ranking metric is AMS unless the user asks for volume/growth.
    """
    city_f = (city or "").strip() or None
    ctype = normalize_client_type((client_type or "").strip() or None)
    bu = _normalize_business_unit(business_unit)
    oil = normalize_oil_type((oil_type or "").strip() or None)
    pack = normalize_packing_category((packing_category or "").strip() or None)
    brand_prefix = None
    brand_n = (brand or "").strip().lower()
    if brand_n in {"eva", "eva foods"}:
        brand_prefix = "Eva"
    elif brand_n in {"maan"}:
        brand_prefix = "Maan"

    group = (group_by or "party").strip().lower()
    if group in {"cities", "city_filter"}:
        group = "city"
    if group not in {"party", "city"}:
        group = "party"

    mix_dim = (mix_dimension or "").strip().lower().replace("-", "_").replace(" ", "_")
    if mix_dim in {"packing", "pack", "product_category", "pack_category", "category"}:
        mix_dim = "packing_category"
    if mix_dim in {"sku", "skus", "products", "item", "items"}:
        mix_dim = "product"

    metric_n = (metric or "ams").strip().lower().replace("-", "_").replace(" ", "_")
    if metric_n in {"avg", "average", "average_sale", "avg_volume", "mt", "sales", "sale"}:
        metric_n = "volume"
    if metric_n in {
        "vsams",
        "pct_ams",
        "against_ams",
        "doing_well_rank",
        "behind",
        "poor",
        "poorly",
        "falling",
        "underperform",
        "underperforming",
        "underperformers",
        "not_doing_well",
        "falling_behind",
        "falling_in_sales",
        "below_ams",
        "behind_ams",
        "behind_average",
    }:
        # "underperformers" sorts ascending; plain vs_ams can be either
        if metric_n in {
            "behind",
            "poor",
            "poorly",
            "falling",
            "underperform",
            "underperforming",
            "underperformers",
            "not_doing_well",
            "falling_behind",
            "falling_in_sales",
            "below_ams",
            "behind_ams",
            "behind_average",
        }:
            sort = "asc"
        metric_n = "vs_ams"
    if metric_n in {"year_over_year", "yoy_growth", "growth", "sales_growth"}:
        metric_n = "yoy"
    if metric_n in {"share", "vtf_share", "segment_share"}:
        metric_n = "share_of_segment"
    if metric_n in {"mix", "vtf_mix"}:
        metric_n = "segment_mix"
    if metric_n in {"percent_in_city", "city_share", "geo"}:
        metric_n = "geo_share"
    if metric_n in {"well", "performing"}:
        metric_n = "doing_well"
    if metric_n in {"new", "new_client", "new_clients", "new_party", "first_sale"}:
        metric_n = "new_parties"
    if metric_n in {
        "lost",
        "lost_client",
        "lost_clients",
        "lost_party",
        "inactive",
        "zero_sales",
        "dropped",
    }:
        metric_n = "lost_parties"
    if metric_n in {"packing_mix", "product_breakdown", "pack_mix", "category_mix"}:
        metric_n = "packing_mix"
        if not mix_dim:
            mix_dim = "packing_category"
    if metric_n in {"product_mix", "sku_mix", "sku_breakdown", "sku_wise"}:
        metric_n = "product_mix"
        mix_dim = "product"
    if metric_n in {"invoice", "invoices", "invoice_count", "frequency", "invoice_frequency"}:
        metric_n = "invoices"
    if metric_n in {"invoice_mt", "avg_invoice", "average_invoice", "mt_per_invoice"}:
        metric_n = "invoice_mt"

    sort_n = (sort or "desc").strip().lower()
    if sort_n not in {"asc", "desc"}:
        sort_n = "desc"

    lim = max(1, min(int(limit or 10), 200))

    # Default period: current data month (MTD if partial)
    if not period and not date_from:
        period = "this month"
    period_info = resolve_period(period, date_from=date_from, date_to=date_to)
    if period_info.get("ok") is False or not period_info.get("date_from"):
        return {
            "ok": False,
            "error": period_info.get("error") or "Bad period",
            "period": period_info,
        }
    d0, d1 = period_info["date_from"], period_info["date_to"]
    as_of = date.fromisoformat(d1)

    filters = {
        "city": city_f,
        "client_type": ctype,
        "business_unit": bu,
        "oil_type": oil,
        "packing_category": pack,
        "brand": brand_prefix,
        "group_by": group,
    }

    # --- Geography share of a segment (e.g. % of VTF in Lahore) ---
    if metric_n == "geo_share":
        target_city = (share_city or city_f or "").strip() or None
        if not target_city:
            return {
                "ok": False,
                "error": "geo_share needs a city (e.g. Lahore).",
            }
        universe = _fetch_party_lines(
            date_from=d0,
            date_to=d1,
            city=None,
            client_type=ctype,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            brand_prefix=brand_prefix,
        )
        total = float(universe["mt"].sum()) if not universe.empty else 0.0
        in_city = (
            float(
                universe.loc[
                    universe["city"].astype(str).str.lower() == target_city.lower(),
                    "mt",
                ].sum()
            )
            if not universe.empty
            else 0.0
        )
        share_pct = (in_city / total * 100.0) if total else None
        scope = _scope_blurb(filters, period_info)
        md = [
            f"Geography share for {scope}.\n",
            "| Scope | MT |",
            "| --- | --- |",
            f"| **{target_city}** | {round(in_city, 3)} |",
            f"| All cities (same filters) | {round(total, 3)} |",
            f"| **% in {target_city}** | "
            f"**{share_pct:.1f}%** |" if share_pct is not None else f"| **% in {target_city}** | — |",
        ]
        return {
            "ok": True,
            "mode": "geo_share",
            "metric": metric_n,
            "period": period_info,
            "filters": filters,
            "city": target_city,
            "city_mt": mt_round(in_city),
            "total_mt": mt_round(total),
            "share_pct": round(share_pct, 1) if share_pct is not None else None,
            "answer_markdown": "\n".join(md) + "\n",
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    # --- Packing / product (SKU) mix ---
    if metric_n in {"packing_mix", "product_mix"}:
        dim = mix_dim or ("product" if metric_n == "product_mix" else "packing_category")
        if dim not in {"packing_category", "product", "oil_type", "business_unit"}:
            dim = "packing_category"
        frame = _fetch_party_lines(
            date_from=d0,
            date_to=d1,
            city=city_f,
            client_type=ctype,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            brand_prefix=brand_prefix,
        )
        if frame.empty:
            return {
                "ok": True,
                "mode": metric_n,
                "period": period_info,
                "filters": filters,
                "rows": [],
                "answer_markdown": (
                    f"No sales for mix — {_scope_blurb(filters, period_info)}.\n"
                ),
                "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
            }
        label = {
            "packing_category": "Packing Category",
            "product": "Product (SKU)",
            "oil_type": "Oil Type",
            "business_unit": "Business Unit",
        }.get(dim, dim)

        # Per-distributor / per-party mix tables
        if per_party_mix and "party" in frame.columns:
            party_totals = (
                frame.groupby("party", as_index=False)["mt"]
                .sum()
                .sort_values("mt", ascending=False)
            )
            party_totals = party_totals.head(lim)
            lines = [
                f"{label} mix by party — {_scope_blurb(filters, period_info)}.\n"
            ]
            all_rows: list[dict[str, Any]] = []
            parties_out: list[dict[str, Any]] = []
            for _, pt in party_totals.iterrows():
                pname = str(pt["party"])
                p_mt = float(pt["mt"])
                part = frame[frame["party"] == pname]
                grouped = (
                    part.groupby(dim, as_index=False)["mt"]
                    .sum()
                    .sort_values("mt", ascending=False)
                )
                meta = _party_meta(frame, pname)
                parties_out.append(
                    {
                        "party": pname,
                        "volume_mt": mt_round(p_mt),
                        "client_type": meta.get("client_type"),
                        "city": meta.get("city"),
                    }
                )
                lines.append(f"### {pname.replace('|', '/')} — {round(p_mt, 3)} MT\n")
                lines.append(f"| {label} | Volume (MT) | Share % |")
                lines.append("| --- | --- | --- |")
                for _, r in grouped.iterrows():
                    mt = float(r["mt"])
                    share = round(mt / p_mt * 100.0, 1) if p_mt else None
                    row = {
                        "party": pname,
                        dim: str(r[dim]),
                        "volume_mt": mt_round(mt),
                        "share_pct": share,
                    }
                    all_rows.append(row)
                    lines.append(
                        f"| {str(r[dim]).replace('|', '/')} | {mt_round(mt)} | "
                        f"{share}% |"
                    )
                lines.append(f"| **Total** | **{round(p_mt, 3)}** | **100%** |")
                lines.append("")
            tips = _party_analysis_bullets(
                rows=parties_out,
                metric="volume",
                filters=filters,
                entity_key="party",
            )
            lines = _append_analysis(lines, tips)
            return {
                "ok": True,
                "mode": metric_n,
                "metric": metric_n,
                "mix_dimension": dim,
                "per_party_mix": True,
                "period": period_info,
                "filters": filters,
                "total_mt": mt_round(float(party_totals["mt"].sum())),
                "parties": parties_out,
                "rows": all_rows,
                "answer_markdown": "\n".join(lines).strip() + "\n",
                "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
            }

        total = float(frame["mt"].sum())
        grouped = (
            frame.groupby(dim, as_index=False)["mt"]
            .sum()
            .sort_values("mt", ascending=False)
        )
        rows = []
        for _, r in grouped.iterrows():
            mt = float(r["mt"])
            rows.append(
                {
                    dim: str(r[dim]),
                    "volume_mt": mt_round(mt),
                    "share_pct": round(mt / total * 100.0, 1) if total else None,
                }
            )
        rows = rows[:lim]
        lines = [
            f"{label} mix — {_scope_blurb(filters, period_info)}.\n",
            f"| {label} | Volume (MT) | Share % |",
            "| --- | --- | --- |",
        ]
        for r in rows:
            lines.append(
                f"| {str(r[dim]).replace('|', '/')} | {r['volume_mt']} | "
                f"{r['share_pct']}% |"
            )
        lines.append(f"| **Total** | **{round(total, 3)}** | **100%** |")
        tips = _party_analysis_bullets(
            rows=rows, metric=metric_n, filters=filters, entity_key="party"
        )
        lines = _append_analysis(lines, tips)
        return {
            "ok": True,
            "mode": metric_n,
            "metric": metric_n,
            "mix_dimension": dim,
            "period": period_info,
            "filters": filters,
            "total_mt": mt_round(total),
            "rows": rows,
            "answer_markdown": "\n".join(lines).strip() + "\n",
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    # --- New parties (first sale in period) ---
    if metric_n == "new_parties":
        firsts = _first_sale_dates(
            city=city_f,
            client_type=ctype,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            brand_prefix=brand_prefix,
        )
        frame = _fetch_party_lines(
            date_from=d0,
            date_to=d1,
            city=city_f,
            client_type=ctype,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            brand_prefix=brand_prefix,
        )
        vol = (
            frame.groupby("party")["mt"].sum().to_dict()
            if not frame.empty
            else {}
        )
        rows = []
        for party, first in firsts.items():
            if d0 <= first <= d1:
                meta = _party_meta(frame, party) if not frame.empty else {
                    "client_type": ctype,
                    "city": city_f,
                }
                # Prefer client master type/city when no period sales yet
                if not meta.get("client_type") or not meta.get("city"):
                    init_db()
                    with connect() as conn:
                        crow = conn.execute(
                            """
                            SELECT type, city_filter FROM clients
                            WHERE lower(trim(client)) = lower(trim(?))
                            """,
                            (party,),
                        ).fetchone()
                    if crow:
                        meta = {
                            "client_type": meta.get("client_type")
                            or (str(crow["type"] or "").strip() or None),
                            "city": meta.get("city")
                            or (str(crow["city_filter"] or "").strip() or None),
                        }
                rows.append(
                    {
                        "party": party,
                        **meta,
                        "first_sale": first,
                        "volume_mt": round(float(vol.get(party, 0.0)), 3),
                        "score": round(float(vol.get(party, 0.0)), 3),
                    }
                )
        rows.sort(key=lambda r: (-(r["volume_mt"] or 0), r["first_sale"]))
        rows = rows[:lim]
        return _party_table_result(
            rows=rows,
            period_info=period_info,
            filters=filters,
            metric=metric_n,
            score_label="Volume (MT)",
            extra_cols=["first_sale", "volume_mt"],
            blurb=f"New parties (first sale in period) — {_scope_blurb(filters, period_info)}",
        )

    # --- Lost parties (AMS > 0 but zero volume in period) ---
    if metric_n == "lost_parties":
        ams = _ams_by_party(
            as_of=as_of.replace(day=1),
            city=city_f,
            client_type=ctype,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            brand_prefix=brand_prefix,
        )
        frame = _fetch_party_lines(
            date_from=d0,
            date_to=d1,
            city=city_f,
            client_type=ctype,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            brand_prefix=brand_prefix,
        )
        vol = (
            frame.groupby("party")["mt"].sum().to_dict()
            if not frame.empty
            else {}
        )
        # Also pull meta from AMS window sales
        ams_start = _prior_three_month_ranges(as_of.replace(day=1))[0][0]
        meta_frame = _fetch_party_lines(
            date_from=ams_start.isoformat(),
            date_to=d1,
            city=city_f,
            client_type=ctype,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            brand_prefix=brand_prefix,
        )
        rows = []
        for party, ams_v in ams.items():
            if ams_v <= 0:
                continue
            if float(vol.get(party, 0.0)) > 0:
                continue
            meta = _party_meta(meta_frame, party) if not meta_frame.empty else {
                "client_type": ctype,
                "city": city_f,
            }
            rows.append(
                {
                    "party": party,
                    **meta,
                    "volume_mt": 0.0,
                    "ams_mt": mt_round(float(ams_v)),
                    "score": mt_round(ams_v),
                }
            )
        rows.sort(key=lambda r: (-(r["ams_mt"] or 0), str(r["party"])))
        rows = rows[:lim]
        return _party_table_result(
            rows=rows,
            period_info=period_info,
            filters=filters,
            metric=metric_n,
            score_label="AMS (MT)",
            extra_cols=["volume_mt", "ams_mt"],
            blurb=(
                f"Lost / silent parties (AMS > 0, zero volume in period) — "
                f"{_scope_blurb(filters, period_info)}"
            ),
        )

    # Segment for share_of_segment / segment_mix (e.g. VTF)
    segment_oil = oil
    segment_bu = bu
    segment_pack = pack
    # For share metrics, party universe may be broader (client_type/city only)
    party_frame = _fetch_party_lines(
        date_from=d0,
        date_to=d1,
        city=city_f,
        client_type=ctype,
        business_unit=bu if metric_n not in {"share_of_segment", "segment_mix"} else None,
        oil_type=oil if metric_n not in {"share_of_segment", "segment_mix"} else None,
        packing_category=pack if metric_n not in {"share_of_segment", "segment_mix"} else None,
        brand_prefix=brand_prefix if metric_n not in {"share_of_segment", "segment_mix"} else None,
    )

    if metric_n in {"share_of_segment", "segment_mix"}:
        # Need a segment definition
        if not (segment_oil or segment_bu or segment_pack or brand_prefix):
            return {
                "ok": False,
                "error": "Share metrics need oil_type / business_unit / packing (e.g. VTF).",
            }
        seg_frame = _fetch_party_lines(
            date_from=d0,
            date_to=d1,
            city=city_f,
            client_type=ctype,
            business_unit=segment_bu,
            oil_type=segment_oil,
            packing_category=segment_pack,
            brand_prefix=brand_prefix,
        )
        party_total = (
            party_frame.groupby("party")["mt"].sum()
            if not party_frame.empty
            else pd.Series(dtype=float)
        )
        seg_total = (
            seg_frame.groupby("party")["mt"].sum()
            if not seg_frame.empty
            else pd.Series(dtype=float)
        )
        all_seg_mt = float(seg_total.sum()) if not seg_total.empty else 0.0
        rows = []
        parties = set(party_total.index) | set(seg_total.index)
        for party in parties:
            p_mt = float(party_total.get(party, 0.0))
            s_mt = float(seg_total.get(party, 0.0))
            if metric_n == "share_of_segment":
                score = (s_mt / all_seg_mt * 100.0) if all_seg_mt else None
            else:
                score = (s_mt / p_mt * 100.0) if p_mt else None
            meta = _party_meta(party_frame if not party_frame.empty else seg_frame, party)
            rows.append(
                {
                    "party": party,
                    **meta,
                    "volume_mt": mt_round(p_mt),
                    "segment_mt": mt_round(s_mt),
                    "score": round(score, 1) if score is not None else None,
                }
            )
        rows = [r for r in rows if (r.get("segment_mt") or 0) > 0]
        rows.sort(key=lambda r: (-(r["score"] or -1e18), -(r["segment_mt"] or 0)))
        rows = rows[:lim]
        score_label = (
            "% of segment"
            if metric_n == "share_of_segment"
            else "% of party volume"
        )
        return _party_table_result(
            rows=rows,
            period_info=period_info,
            filters={**filters, "segment_oil": segment_oil},
            metric=metric_n,
            score_label=score_label,
            extra_cols=["volume_mt", "segment_mt"],
            blurb=(
                f"Party share of "
                f"{segment_oil or segment_bu or segment_pack or brand_prefix} "
                f"({_scope_blurb(filters, period_info)})"
            ),
        )

    # Volume / AMS / vs AMS / doing well / YoY / invoices (party or city)
    entity_key = "city" if group == "city" else "party"
    if party_frame.empty and metric_n in {"volume", "invoices", "invoice_mt"}:
        return {
            "ok": True,
            "mode": metric_n,
            "period": period_info,
            "filters": filters,
            "parties": [],
            "answer_markdown": (
                f"No {entity_key} sales for {_scope_blurb(filters, period_info)}.\n"
            ),
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    volume = (
        party_frame.groupby(entity_key)["mt"].sum().to_dict()
        if not party_frame.empty
        else {}
    )

    # Invoice stats
    invoice_counts: dict[str, int] = {}
    if metric_n in {"invoices", "invoice_mt"} and not party_frame.empty:
        inv = party_frame.copy()
        inv["inv_key"] = inv["inv_no"].fillna("").astype(str).str.strip()
        # Fall back to date+party when inv_no missing
        missing = inv["inv_key"] == ""
        inv.loc[missing, "inv_key"] = (
            inv.loc[missing, "date"].astype(str)
            + "|"
            + inv.loc[missing, "party"].astype(str)
        )
        invoice_counts = (
            inv.groupby(entity_key)["inv_key"].nunique().astype(int).to_dict()
        )

    if group == "city":
        # AMS by city = mean of prior 3 months' city totals
        ranges = _prior_three_month_ranges(as_of.replace(day=1))
        monthly_city: list[dict[str, float]] = []
        keys: set[str] = set()
        for start, end in ranges:
            cf = _fetch_party_lines(
                date_from=start.isoformat(),
                date_to=end.isoformat(),
                city=city_f,
                client_type=ctype,
                business_unit=bu,
                oil_type=oil,
                packing_category=pack,
                brand_prefix=brand_prefix,
            )
            if cf.empty:
                monthly_city.append({})
                continue
            totals = {
                str(k): float(v) for k, v in cf.groupby("city")["mt"].sum().items()
            }
            keys.update(totals)
            monthly_city.append(totals)
        ams = {k: sum(m.get(k, 0.0) for m in monthly_city) / 3.0 for k in keys}
    else:
        ams = _ams_by_party(
            as_of=as_of.replace(day=1),
            city=city_f,
            client_type=ctype,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            brand_prefix=brand_prefix,
        )

    compare_info = None
    compare_vol: dict[str, float] = {}
    if metric_n == "yoy":
        if compare_period:
            compare_info = resolve_period(compare_period)
        else:
            start = date.fromisoformat(d0)
            end = date.fromisoformat(d1)
            try:
                c_start = start.replace(year=start.year - 1)
            except ValueError:
                c_start = start.replace(year=start.year - 1, day=28)
            try:
                c_end = end.replace(year=end.year - 1)
            except ValueError:
                c_end = end.replace(year=end.year - 1, day=28)
            c_end = min(
                c_end,
                date(c_end.year, c_end.month, calendar.monthrange(c_end.year, c_end.month)[1]),
            )
            compare_info = {
                "ok": True,
                "date_from": c_start.isoformat(),
                "date_to": c_end.isoformat(),
                "label": f"{c_start.strftime('%b %Y')} (YoY)",
            }
        if compare_info.get("ok") is False:
            return {"ok": False, "error": compare_info.get("error"), "period": compare_info}
        cmp_frame = _fetch_party_lines(
            date_from=compare_info["date_from"],
            date_to=compare_info["date_to"],
            city=city_f,
            client_type=ctype,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            brand_prefix=brand_prefix,
        )
        compare_vol = (
            cmp_frame.groupby(entity_key)["mt"].sum().to_dict()
            if not cmp_frame.empty
            else {}
        )

    partial = bool(period_info.get("partial_month"))
    days_elapsed = int(period_info.get("days_elapsed") or 0)
    days_in_month = int(period_info.get("days_in_month") or 30)

    rows = []
    entities = set(volume) | set(ams) | set(compare_vol) | set(invoice_counts)
    for ent in entities:
        vol = float(volume.get(ent, 0.0))
        ams_v = float(ams.get(ent, 0.0))
        expected = None
        if partial and days_in_month:
            expected = (days_elapsed / days_in_month) * ams_v
        baseline = expected if (partial and expected is not None) else ams_v
        vs = pct_change(vol, baseline) if baseline else None
        prior = float(compare_vol.get(ent, 0.0))
        yoy = pct_change(vol, prior) if metric_n == "yoy" else None
        inv_n = int(invoice_counts.get(ent, 0))
        avg_inv = (vol / inv_n) if inv_n else None
        if group == "city":
            meta = {"client_type": ctype, "city": ent}
            name_field = {"party": ent, "city": ent}
        else:
            meta = _party_meta(party_frame, ent) if not party_frame.empty else {
                "client_type": ctype,
                "city": city_f,
            }
            name_field = {"party": ent}
        entry = {
            **name_field,
            **meta,
            "volume_mt": mt_round(vol),
            "ams_mt": mt_round(ams_v),
            "expected_mt": mt_round(expected) if expected is not None else None,
            "pct_vs_ams": round(vs, 1) if vs is not None else None,
            "prior_mt": mt_round(prior) if metric_n == "yoy" else None,
            "yoy_pct": round(yoy, 1) if yoy is not None else None,
            "invoices": inv_n,
            "avg_invoice_mt": mt_round(avg_inv) if avg_inv is not None else None,
            "doing_well": bool(vs is not None and vs >= 0),
        }
        rows.append(entry)

    if metric_n == "doing_well":
        well = [r for r in rows if r["doing_well"] and (r["volume_mt"] or 0) > 0]
        not_well = [r for r in rows if not r["doing_well"] and (r["volume_mt"] or 0) > 0]
        well.sort(key=lambda r: (-(r["pct_vs_ams"] if r["pct_vs_ams"] is not None else -1e18)))
        total_parties = len(well) + len(not_well)
        pct_well = (len(well) / total_parties * 100.0) if total_parties else None
        well_mt = sum(r["volume_mt"] for r in well)
        all_mt = well_mt + sum(r["volume_mt"] for r in not_well)
        mt_pct = (well_mt / all_mt * 100.0) if all_mt else None
        baseline_name = "Expected (partial × AMS)" if partial else "AMS"
        entity_label = "City" if group == "city" else "Party"
        lines = [
            f"Doing well vs {baseline_name} — {_scope_blurb(filters, period_info)}.\n",
            f"**{len(well)}** of **{total_parties}** "
            f"{'cities' if group == 'city' else 'parties'} "
            f"({pct_well:.1f}%) are at/above {baseline_name}"
            if pct_well is not None
            else "No parties in scope.",
            f"; they account for **{mt_pct:.1f}%** of volume.\n"
            if mt_pct is not None
            else "\n",
            f"| {entity_label} | City | Volume | {baseline_name} | % vs |",
            "| --- | --- | --- | --- | --- |",
        ]
        for r in well[:lim]:
            base = r["expected_mt"] if partial else r["ams_mt"]
            name = r.get("party") or r.get("city") or "—"
            lines.append(
                f"| {name} | {r.get('city') or '—'} | {r['volume_mt']} | "
                f"{base if base is not None else '—'} | "
                f"{r['pct_vs_ams']:+.1f}% |"
                if r["pct_vs_ams"] is not None
                else f"| {name} | {r.get('city') or '—'} | {r['volume_mt']} | "
                f"{base if base is not None else '—'} | — |"
            )
        tips = []
        if pct_well is not None:
            tips.append(
                f"{pct_well:.0f}% of {entity_label.lower()}s are at/above {baseline_name}."
            )
        if mt_pct is not None:
            tips.append(
                f"Those names already deliver {mt_pct:.0f}% of volume — "
                + (
                    "healthy breadth."
                    if pct_well and pct_well >= 50
                    else "volume still leans on a minority of names."
                )
            )
        lines = _append_analysis(lines, tips)
        return {
            "ok": True,
            "mode": "doing_well",
            "metric": metric_n,
            "period": period_info,
            "filters": filters,
            "parties_doing_well": len(well),
            "parties_total": total_parties,
            "pct_parties_doing_well": round(pct_well, 1) if pct_well is not None else None,
            "pct_volume_doing_well": round(mt_pct, 1) if mt_pct is not None else None,
            "parties": well[:lim],
            "answer_markdown": "\n".join(lines).strip() + "\n",
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    # Sort by metric (asc for underperformers / behind AMS)
    def _sort_key_desc(val, secondary=0.0):
        return (-(val if val is not None else -1e18), -secondary)

    def _sort_key_asc(val, secondary=0.0):
        return ((val if val is not None else 1e18), -secondary)

    sk = _sort_key_asc if sort_n == "asc" else _sort_key_desc

    if metric_n == "ams":
        rows.sort(key=lambda r: sk(r["ams_mt"], r["volume_mt"] or 0))
        score_key, score_label = "ams_mt", "AMS (MT)"
    elif metric_n == "vs_ams":
        rows = [r for r in rows if (r["volume_mt"] or 0) > 0 or (r["ams_mt"] or 0) > 0]
        rows.sort(key=lambda r: sk(r["pct_vs_ams"], r["volume_mt"] or 0))
        score_key, score_label = "pct_vs_ams", "% vs AMS/Expected"
    elif metric_n == "yoy":
        rows = [r for r in rows if (r["volume_mt"] or 0) > 0 or (r["prior_mt"] or 0) > 0]
        rows.sort(key=lambda r: sk(r["yoy_pct"], r["volume_mt"] or 0))
        score_key, score_label = "yoy_pct", "YoY %"
    elif metric_n == "invoices":
        rows = [r for r in rows if (r["invoices"] or 0) > 0]
        rows.sort(key=lambda r: sk(float(r["invoices"]), r["volume_mt"] or 0))
        score_key, score_label = "invoices", "Invoices"
    elif metric_n == "invoice_mt":
        rows = [r for r in rows if r.get("avg_invoice_mt") is not None]
        rows.sort(key=lambda r: sk(r["avg_invoice_mt"], r["volume_mt"] or 0))
        score_key, score_label = "avg_invoice_mt", "Avg MT / invoice"
    else:
        rows.sort(key=lambda r: sk(r["volume_mt"], 0))
        score_key, score_label = "volume_mt", "Volume (MT)"
        rows = [r for r in rows if (r.get("volume_mt") or 0) > 0]

    rows = rows[:lim]
    for r in rows:
        r["score"] = r.get(score_key)

    extra = ["volume_mt", "ams_mt"]
    if metric_n == "vs_ams" and partial:
        extra.append("expected_mt")
    if metric_n == "vs_ams":
        extra.append("pct_vs_ams")
    if metric_n == "yoy":
        extra = ["volume_mt", "prior_mt", "yoy_pct"]
    if metric_n == "invoices":
        extra = ["invoices", "volume_mt", "avg_invoice_mt"]
    if metric_n == "invoice_mt":
        extra = ["avg_invoice_mt", "invoices", "volume_mt"]

    blurb = _scope_blurb(filters, period_info)
    entity_word = "cities" if group == "city" else "parties"
    if metric_n == "yoy" and compare_info:
        blurb += f" vs {compare_info.get('label')}"
    if metric_n == "vs_ams":
        direction = "furthest behind" if sort_n == "asc" else "furthest ahead of"
        blurb += (
            f" · {entity_word} {direction} "
            + ("Expected (days/month × AMS)" if partial else "AMS (3 prior months)")
        )
    else:
        blurb = f"Top {entity_word} by {score_label} — {blurb}"

    return _party_table_result(
        rows=rows,
        period_info=period_info,
        filters=filters,
        metric=metric_n,
        score_label=score_label,
        extra_cols=extra,
        blurb=blurb if metric_n == "vs_ams" else blurb,
        compare_period=compare_info,
        entity_key=entity_key,
    )


def _party_meta(frame: pd.DataFrame, party: str) -> dict[str, Any]:
    part = frame[frame["party"] == party]
    if part.empty:
        return {"client_type": None, "city": None}
    return {
        "client_type": str(part["client_type"].iloc[0]) if "client_type" in part else None,
        "city": str(part["city"].iloc[0]) if "city" in part else None,
    }


def _party_analysis_bullets(
    *,
    rows: list[dict[str, Any]],
    metric: str,
    filters: dict[str, Any],
    entity_key: str = "party",
) -> list[str]:
    """Short interpretation bullets for party/city analytics answers."""
    tips: list[str] = []
    if not rows:
        return tips
    name = entity_key
    scope = filters.get("client_type") or filters.get("city") or "this set"

    if metric in {"packing_mix", "product_mix"}:
        # rows may use packing_category / product keys
        total = sum(float(r.get("volume_mt") or 0) for r in rows)
        if total > 0 and rows:
            top = rows[0]
            key = next(
                (k for k in ("packing_category", "product", "segment") if k in top),
                None,
            )
            if key:
                share = float(top.get("share_pct") or 0)
                tips.append(
                    f"**{top.get(key)}** is {share:.0f}% of the mix — "
                    + (
                        "highly concentrated; a slip here moves the total."
                        if share >= 45
                        else "the lead line in this mix."
                    )
                )
            if len(rows) >= 2:
                tips.append(
                    f"Top 2 lines are "
                    f"{float(rows[0].get('share_pct') or 0) + float(rows[1].get('share_pct') or 0):.0f}% "
                    f"combined."
                )
        return tips[:3]

    if metric == "lost_parties":
        tips.append(
            f"{len(rows)} {name}(s) had AMS > 0 but **zero** volume in this period — "
            "priority follow-ups."
        )
        if rows and rows[0].get("ams_mt"):
            tips.append(
                f"Largest silent AMS: **{rows[0].get('party') or rows[0].get('city')}** "
                f"({rows[0]['ams_mt']} MT AMS)."
            )
        return tips[:3]

    if metric == "new_parties":
        tips.append(
            f"{len(rows)} new {name}(s) with first sale in this window."
        )
        if rows and rows[0].get("volume_mt"):
            tips.append(
                f"Largest new volume: **{rows[0].get('party')}** "
                f"({rows[0]['volume_mt']} MT)."
            )
        return tips[:3]

    if metric == "yoy":
        scored = [r for r in rows if isinstance(r.get("yoy_pct"), (int, float))]
        if scored:
            best = scored[0] if True else scored[0]
            # rows already sorted by metric
            tips.append(
                f"**{best.get('party') or best.get('city')}** leads YoY "
                f"({best['yoy_pct']:+.1f}%; {best.get('volume_mt')} vs "
                f"{best.get('prior_mt')} MT prior)."
            )
            neg = [r for r in scored if float(r["yoy_pct"]) < 0]
            if neg:
                tips.append(
                    f"{len(neg)} of {len(scored)} listed are still down YoY."
                )
        return tips[:3]

    if metric == "vs_ams":
        scored = [r for r in rows if isinstance(r.get("pct_vs_ams"), (int, float))]
        if scored:
            lead = scored[0]
            tips.append(
                f"**{lead.get('party') or lead.get('city')}** is "
                f"{lead['pct_vs_ams']:+.1f}% vs AMS/Expected "
                f"({lead.get('volume_mt')} MT)."
            )
            behind = [r for r in scored if float(r["pct_vs_ams"]) < 0]
            if behind:
                tips.append(
                    f"{len(behind)} of {len(scored)} shown are below AMS — "
                    f"focus recovery on {scope}."
                )
            elif scored:
                tips.append("All listed names are at/above AMS in this cut.")
        return tips[:3]

    if metric in {"ams", "volume"}:
        top = rows[0]
        top_name = top.get("party") or top.get("city")
        tips.append(
            f"**{top_name}** ranks #1 "
            f"({top.get('ams_mt') if metric == 'ams' else top.get('volume_mt')} MT "
            f"{'AMS' if metric == 'ams' else 'volume'})."
        )
        if len(rows) >= 3:
            top3 = sum(
                float(r.get("ams_mt" if metric == "ams" else "volume_mt") or 0)
                for r in rows[:3]
            )
            allv = sum(
                float(r.get("ams_mt" if metric == "ams" else "volume_mt") or 0)
                for r in rows
            )
            if allv > 0:
                tips.append(
                    f"Top 3 are {100.0 * top3 / allv:.0f}% of this ranked list — "
                    + ("concentrated." if top3 / allv >= 0.6 else "reasonably spread.")
                )
        return tips[:3]

    if metric == "invoices":
        top = rows[0]
        tips.append(
            f"**{top.get('party') or top.get('city')}** has the most invoices "
            f"({top.get('invoices')})"
            + (
                f" at ~{top.get('avg_invoice_mt')} MT each."
                if top.get("avg_invoice_mt") is not None
                else "."
            )
        )
        return tips[:3]

    if metric in {"share_of_segment", "segment_mix"}:
        top = rows[0]
        tips.append(
            f"**{top.get('party')}** holds the largest share "
            f"({top.get('score')}%; segment {top.get('segment_mt')} MT)."
        )
        return tips[:3]

    return tips


def _append_analysis(lines: list[str], tips: list[str]) -> list[str]:
    if not tips:
        return lines
    out = list(lines)
    if out and out[-1].strip():
        out.append("")
    out.append("### Analysis")
    out.extend(f"- {t}" for t in tips)
    return out


def _scope_blurb(filters: dict[str, Any], period: dict[str, Any]) -> str:
    bits = [str(period.get("label") or "")]
    if filters.get("client_type"):
        bits.append(f"**{filters['client_type']}**")
    if filters.get("city"):
        bits.append(f"city **{filters['city']}**")
    if filters.get("brand"):
        bits.append(f"brand **{filters['brand']}**")
    if filters.get("business_unit"):
        bits.append(f"BU **{filters['business_unit']}**")
    if filters.get("oil_type"):
        bits.append(f"Oil **{filters['oil_type']}**")
    if filters.get("packing_category"):
        bits.append(f"Packing **{filters['packing_category']}**")
    return " · ".join(b for b in bits if b)


def _party_table_result(
    *,
    rows: list[dict[str, Any]],
    period_info: dict[str, Any],
    filters: dict[str, Any],
    metric: str,
    score_label: str,
    extra_cols: list[str],
    blurb: str,
    compare_period: dict[str, Any] | None = None,
    entity_key: str = "party",
) -> dict[str, Any]:
    col_labels = {
        "volume_mt": "Volume (MT)",
        "ams_mt": "AMS (MT)",
        "expected_mt": "Expected (MT)",
        "segment_mt": "Segment (MT)",
        "prior_mt": "Prior (MT)",
        "pct_vs_ams": "% vs AMS",
        "yoy_pct": "YoY %",
        "first_sale": "First sale",
        "invoices": "Invoices",
        "avg_invoice_mt": "Avg MT / invoice",
    }
    name_header = "City" if entity_key == "city" else "Party"
    # Omit constant filter columns (same value on every row / matches filter)
    show_type = True
    show_city = entity_key != "city"
    if rows and entity_key != "city":
        ftype = filters.get("client_type")
        fcity = filters.get("city")
        types = {str(r.get("client_type") or "") for r in rows}
        cities = {str(r.get("city") or "") for r in rows}
        if ftype and len(types) <= 1 and (not types or next(iter(types)) == ftype):
            show_type = False
        if fcity and len(cities) <= 1 and (
            not cities or next(iter(cities)) in {fcity, ""}
        ):
            show_city = False
    headers = ["#", name_header]
    if entity_key == "city":
        if show_type:
            headers.append("Client Type")
    else:
        if show_type:
            headers.append("Client Type")
        if show_city:
            headers.append("City")
    headers = headers + [col_labels.get(c, c) for c in extra_cols]
    # Drop duplicate City column when entity is city — already handled above
    if score_label and score_label not in headers:
        if not any(col_labels.get(c) == score_label or c == "score" for c in extra_cols):
            headers.append(score_label)
            show_score = True
        else:
            show_score = False
    else:
        show_score = False

    lines = [f"{blurb}.\n", "| " + " | ".join(headers) + " |"]
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for i, r in enumerate(rows, 1):
        if entity_key == "city":
            cells = [
                str(i),
                str(r.get("city") or r.get("party") or "").replace("|", "/"),
            ]
            if show_type:
                cells.append(str(r.get("client_type") or "—").replace("|", "/"))
        else:
            cells = [
                str(i),
                str(r.get("party") or "").replace("|", "/"),
            ]
            if show_type:
                cells.append(str(r.get("client_type") or "—").replace("|", "/"))
            if show_city:
                cells.append(str(r.get("city") or "—").replace("|", "/"))
        for c in extra_cols:
            val = r.get(c)
            if val is None:
                cells.append("—")
            elif isinstance(val, float) and (c.endswith("_pct") or c == "pct_vs_ams"):
                cells.append(f"{val:+.1f}%")
            else:
                cells.append(str(val))
        if show_score:
            sc = r.get("score")
            if sc is None:
                cells.append("—")
            elif isinstance(sc, float) and (
                " %" in score_label or score_label.startswith("%") or "YoY" in score_label
            ):
                cells.append(f"{sc:+.1f}%")
            else:
                cells.append(str(sc))
        lines.append("| " + " | ".join(cells) + " |")

    if not rows:
        lines = [f"No results for {blurb}.\n"]
    else:
        tips = _party_analysis_bullets(
            rows=rows, metric=metric, filters=filters, entity_key=entity_key
        )
        lines = _append_analysis(lines, tips)

    return {
        "ok": True,
        "mode": metric,
        "metric": metric,
        "period": period_info,
        "compare_period": compare_period,
        "filters": filters,
        "parties": rows,
        "party_spec": {
            "kind": "analyze_parties",
            "filters": {
                "city": filters.get("city"),
                "client_type": filters.get("client_type"),
                "business_unit": filters.get("business_unit"),
                "oil_type": filters.get("oil_type"),
                "packing_category": filters.get("packing_category"),
            },
            "period_phrase": None,
            "period": {
                "date_from": period_info.get("date_from"),
                "date_to": period_info.get("date_to"),
                "label": period_info.get("label"),
            },
            "metric": metric,
            "limit": len(rows) if rows else 10,
            "group_by": entity_key if entity_key in {"party", "city"} else "party",
        },
        "answer_markdown": "\n".join(lines).strip() + "\n",
        "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
    }


def party_sales(
    *,
    query: str,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    columns: str = "city",
    mode: str = "matrix",
) -> dict[str, Any]:
    """Resolve a named client/party then show their sales for a period.

    Does not inherit city/client_type from chat context — only the named party
    (plus optional period). Zero matches → clear not-found + ask to elaborate.
    """
    q = (query or "").strip()
    if not q:
        return {
            "ok": False,
            "error": "Empty party query",
            "answer_markdown": "Please give a client / distributor name to look up.\n",
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    looked = lookup_party(q, limit=10)
    matches = list(looked.get("matches") or [])
    if not matches:
        cleaned = looked.get("query") or q
        md = (
            f"Could not find **{cleaned}** in clients or sales data.\n\n"
            "Is this a **client / distributor** name? Please check the spelling "
            "or give a fuller name (a city suffix helps, e.g. "
            "`Rubina Shaheen (LHR)`). You can also name the city or client type "
            "to help me search."
        )
        return {
            "ok": True,
            "mode": "party_not_found",
            "query": cleaned,
            "matches": [],
            "answer_markdown": md + "\n",
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    qn = re.sub(r"\s+", " ", q.strip().lower())
    exact = [m for m in matches if str(m.get("client") or "").strip().lower() == qn]
    # Also treat query as substring of full client name (Rubina Shaheen vs Rubina Shaheen (LHR))
    partial = [
        m
        for m in matches
        if qn in str(m.get("client") or "").strip().lower()
        or str(m.get("client") or "").strip().lower().startswith(qn)
    ]
    if len(exact) == 1:
        chosen = exact[0]
    elif len(partial) == 1 and float(partial[0].get("match_score") or 0) >= 0.55:
        chosen = partial[0]
    elif len(matches) == 1 and float(matches[0].get("match_score") or 0) >= 0.65:
        chosen = matches[0]
    elif (
        len(matches) >= 1
        and float(matches[0].get("match_score") or 0) >= 0.88
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
            f"Multiple parties match **{q}** — reply with the exact name:\n",
            "| # | Party | Client Type | City | Score |",
            "| --- | --- | --- | --- | --- |",
        ]
        for i, m in enumerate(matches[:10], 1):
            lines.append(
                f"| {i} | {m.get('client')} | {m.get('client_type') or '—'} | "
                f"{m.get('city_filter') or m.get('city') or '—'} | "
                f"{m.get('match_score')} |"
            )
        lines.append(
            "\n_Tell me which one (exact name), or add city / client type._"
        )
        return {
            "ok": True,
            "mode": "party_pick",
            "query": q,
            "matches": matches,
            "answer_markdown": "\n".join(lines) + "\n",
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    name = str(chosen.get("client"))
    out = query_sales(
        period=period,
        date_from=date_from,
        date_to=date_to,
        party=name,
        columns=columns or "city",
        mode=mode or "matrix",
        prior_spec=None,
    )
    if not out.get("ok"):
        return out

    # Empty sales in period → still report the resolved party, not a blank matrix only
    matrix = out.get("matrix") or {}
    grand = float(matrix.get("grand_total_mt") or 0)
    if grand <= 0 and not any(
        r.get("row_kind") == "leaf" for r in (matrix.get("rows") or [])
    ):
        period_label = (out.get("period") or {}).get("label") or period or "that period"
        md = (
            f"Found **{name}** "
            f"({chosen.get('client_type') or '—'} · "
            f"{chosen.get('city_filter') or chosen.get('city') or '—'}), "
            f"but **no sales** in **{period_label}**.\n\n"
            "Try another month, or ask for all-time / last 6 months for this party."
        )
        return {
            "ok": True,
            "mode": "party_sales_empty",
            "party": name,
            "match": chosen,
            "period": out.get("period"),
            "filters": {"party": name},
            "answer_markdown": md + "\n",
            "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
        }

    # Prefixed identity line on the matrix answer
    prefix = (
        f"**{name}** · {chosen.get('client_type') or '—'} · "
        f"{chosen.get('city_filter') or chosen.get('city') or '—'}.\n\n"
    )
    out["mode"] = "party_sales"
    out["party"] = name
    out["match"] = chosen
    out["answer_markdown"] = prefix + str(out.get("answer_markdown") or "")
    out["party_spec"] = {
        "kind": "party_sales",
        "filters": {"party": name},
        "period_phrase": period,
        "period": {
            "date_from": (out.get("period") or {}).get("date_from"),
            "date_to": (out.get("period") or {}).get("date_to"),
            "label": (out.get("period") or {}).get("label"),
        },
        "party": name,
    }
    return out


def infer_party_analytics_from_text(text: str) -> dict[str, Any]:
    """Heuristic argument extraction for list_clients / analyze_parties."""
    t = (text or "").lower()
    out: dict[str, Any] = {
        "city": extract_city_from_text(text),
        "client_type": extract_client_type_from_text(text),
        "oil_type": None,
        "business_unit": None,
        "brand": None,
        "packing_category": None,
        "metric": "ams",  # default ranking metric
        "period": None,
        "compare_period": None,
        "limit": 10,
        "mode": "analyze",
        "group_by": "party",
        "mix_dimension": None,
        "sort": "desc",
        "per_party_mix": False,
    }

    # Oil / VTF / packing from language
    if re.search(r"\bvtf\b|\bbanaspati\b", t):
        out["oil_type"] = "Eva VTF"
    else:
        out["oil_type"] = normalize_oil_type(
            next(
                (
                    a
                    for a in ("canola", "cooking", "sunflower", "eva canola")
                    if a in t
                ),
                None,
            )
        )
    out["packing_category"] = normalize_packing_category(
        next(
            (
                a
                for a in (
                    "pillow",
                    "standup",
                    "stand up",
                    "pet bottle",
                    "jerry can",
                    "jerry",
                    "pouch",
                    "tin",
                    "bucket",
                )
                if a in t
            ),
            None,
        )
    )

    if re.search(r"\beva sales\b|\bfor eva\b|\beva vtf\b", t) and not re.search(
        r"\beva distributors?\b", t
    ):
        if not out["oil_type"]:
            out["brand"] = "Eva"
    if re.search(r"\btop\b.+\bfor eva\b|\bdistributors? for eva\b", t):
        out["brand"] = out.get("brand") or "Eva"

    m_lim = re.search(r"\b(?:top|bottom)\s+(\d{1,3})\b", t)
    if m_lim:
        out["limit"] = int(m_lim.group(1))
    elif re.search(r"\b(highest|who\s+(are|were)\s+the\s+top|top\s+distributors?)\b", t):
        out["limit"] = 10
    elif re.search(r"\bwhich\b", t) and not re.search(r"\btop\s+\d", t):
        out["limit"] = 10

    # City league
    if re.search(
        r"\b(cities|city league|rank(ed)? cities|top\s+\d+\s+cities|top cities)\b",
        t,
    ):
        out["group_by"] = "city"

    # List mode
    if re.search(
        r"\b(who are|list|show( me)?)\b.+\b(distributors?|clients?|parties|imtiaz)\b|"
        r"\b(individual\s+distributors?|by\s+(individual\s+)?"
        r"(distributors?|parties|clients?)|(distributors?|parties)[- ]wise)\b",
        t,
    ) and not re.search(
        r"\b(top|highest|grow|doing well|poor|behind|share|percent|%|ams|average|"
        r"new|lost|mix|breakdown|invoice|frequency|silent|bottom)\b",
        t,
    ):
        out["mode"] = "list"
        out["limit"] = 200
        out["metric"] = "volume"

    # Metrics (order matters)
    context_ref = bool(
        re.search(
            r"\b(in this|from this|in that|from that|this table|that table|"
            r"above|these sales|those sales|for this)\b",
            t,
        )
    )
    if re.search(
        r"\b(new\s+(parties|clients|distributors|imtiaz)|new in\b|first sale|"
        r"newly\s+added)\b",
        t,
    ):
        out["metric"] = "new_parties"
    elif re.search(
        r"\b(lost\s+(parties|clients|distributors)|silent|"
        r"zero sales|no sales this|dropped off|inactive parties)\b",
        t,
    ):
        out["metric"] = "lost_parties"
    elif re.search(
        r"\b(sku[- ]?wise|by sku|show by sku|sku break|product[- ]?wise)\b",
        t,
    ) or (
        re.search(r"\b(mix|breakdown|break\s*down)\b", t)
        and re.search(r"\bsku\b", t)
    ):
        out["metric"] = "product_mix"
        out["mix_dimension"] = "product"
        if re.search(
            r"\b(for\s+each|each\s+(distributor|party|client)|"
            r"per\s+(distributor|party|client)|"
            r"(distributors?|parties|clients?)[- ]wise)\b",
            t,
        ):
            out["per_party_mix"] = True
            out["limit"] = out.get("limit") or 16
    elif re.search(
        r"\b(product mix|product break|packing mix|pack(ing)? break|"
        r"pack mix|category mix|mix for)\b",
        t,
    ) or (
        re.search(r"\b(product|packing|pack)\s+break(\s*down|down)?\b", t)
        and re.search(r"\b(imtiaz|distributors?|clients?|each|per)\b", t)
    ):
        out["metric"] = "packing_mix"
        out["mix_dimension"] = "packing_category"
        if re.search(
            r"\b(for\s+each|each\s+(distributor|party|client)|"
            r"per\s+(distributor|party|client)|"
            r"(distributors?|parties|clients?)[- ]wise)\b",
            t,
        ):
            out["per_party_mix"] = True
            out["limit"] = out.get("limit") or 16
    elif re.search(
        r"\b(invoice frequency|most invoices|by invoices?|invoices?)\b", t
    ):
        out["metric"] = "invoices"
    elif re.search(r"\b(avg(erage)? invoice|mt per invoice|invoice size)\b", t):
        out["metric"] = "invoice_mt"
    elif re.search(
        r"\b(percent|%|\bshare\b)\b.+\b(lahore|karachi|islamabad|city)\b", t
    ) or re.search(r"\b(lahore|karachi|islamabad)\b.+\b(percent|%|share)\b", t):
        out["metric"] = "geo_share"
        out["mode"] = "analyze"
    elif re.search(
        r"\bshare of\b|\bhighest share\b|\bhighest\b.{0,25}\bshare\b|"
        r"\bshare\b.{0,25}\bhighest\b",
        t,
    ):
        out["metric"] = "share_of_segment"
    elif re.search(
        r"\b(behind|poorly|poor performance|falling behind|falling in sales|"
        r"not doing well|underperform|below ams|below average|"
        r"behind on average|bottom\s+\d+)\b",
        t,
    ):
        out["metric"] = "vs_ams" if not re.search(r"\bby volume\b|\bbottom\b.+\bvolume\b", t) else "volume"
        out["sort"] = "asc"
        if re.search(r"\bbottom\b.+\bvolume\b|\bby volume\b.+\bbottom\b", t):
            out["metric"] = "volume"
            out["sort"] = "asc"
        elif re.search(r"\bbottom\b", t) and re.search(r"\bvolume\b", t):
            out["metric"] = "volume"
            out["sort"] = "asc"
        elif re.search(r"\bbottom\b", t) and not re.search(r"\b(ams|average)\b", t):
            out["metric"] = "volume"
            out["sort"] = "asc"
    elif re.search(r"\b(grow|growth|grew|vs\b.+\blast year|year over year|yoy)\b", t):
        out["metric"] = "yoy"
        if re.search(r"\blast year\b|\byear ago\b", t):
            from eva_dashboard.sales_query import MONTH_NAMES

            for name in MONTH_NAMES:
                if re.search(rf"\b{name}\b", t):
                    out["compare_period"] = f"{name} last year"
                    out["period"] = name
                    break
            if not out["compare_period"]:
                out["compare_period"] = None  # auto YoY of current period
        else:
            # "growth in July" → July vs July last year
            from eva_dashboard.sales_query import MONTH_NAMES

            for name in MONTH_NAMES:
                if re.search(rf"\b{name}\b", t):
                    out["period"] = name
                    out["compare_period"] = f"{name} last year"
                    break
    elif re.search(r"\bdoing well\b|\bperforming well\b|\bmanaged well\b", t):
        if re.search(r"\bwhich\b|\btop\b|\blist\b", t):
            out["metric"] = "vs_ams"
            out["sort"] = "desc"
        else:
            out["metric"] = "doing_well"
    elif re.search(r"\bvs\s*ams\b|\bagainst ams\b|\brelative to ams\b", t):
        out["metric"] = "vs_ams"
    elif context_ref and re.search(
        r"\b(top|highest|who\s+(are|were)\s+the\s+top)\b", t
    ):
        # "top distributors in this" → volume of the prior sales table
        out["metric"] = "volume"
    elif re.search(r"\b(by )?volume\b|\btop sales\b|\bhighest sale\b", t) and not re.search(
        r"\bgrowth\b", t
    ):
        out["metric"] = "volume"
    elif re.search(r"\bams\b|\baverage (monthly )?sale", t):
        out["metric"] = "ams"
    else:
        # Default for rankings: AMS
        out["metric"] = "ams"

    # Period phrases
    m_n = re.search(r"\b(last|past|previous)\s+(\d{1,2})\s+months?\b", t)
    if re.search(r"\blast quarter\b|\bprevious quarter\b", t):
        out["period"] = "last quarter"
    elif m_n:
        out["period"] = f"last {m_n.group(2)} months"
    elif re.search(r"\blast month\b", t):
        out["period"] = "last month"
    elif re.search(r"\bthis month\b|\bso far\b|\bmtd\b", t):
        out["period"] = "this month"
    elif out["metric"] == "yoy" and out.get("period"):
        pass
    else:
        from eva_dashboard.sales_query import MONTH_NAMES

        for name in MONTH_NAMES:
            if re.search(rf"\b{name}\b", t) and "last year" not in t:
                out["period"] = name
                break

    return out
