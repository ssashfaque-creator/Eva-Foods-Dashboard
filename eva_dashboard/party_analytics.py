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
from eva_dashboard.client_type_map import (
    classify_client_type_filter,
    map_client_type,
    sql_client_type_values,
)
from eva_dashboard.data import _prior_three_month_ranges, pct_change
from eva_dashboard.db import connect, init_db
from eva_dashboard.fmt import mt_round
from eva_dashboard.sales_query import (
    _attach_client_dims,
    _normalize_business_unit,
    _parties_matching,
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
    cities = extract_cities_from_text(text)
    return cities[0] if cities else None


def extract_cities_from_text(text: str) -> list[str]:
    """All distinct cities mentioned, in left-to-right order.

    Handles "Lahore vs Karachi", "Lahore and Karachi", etc.
    """
    t = (text or "").strip()
    if not t:
        return []
    lower = t.lower()
    catalog = list(list_known_cities())
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
        if city not in catalog:
            catalog.append(city)
    # Longer names first so "Nankana Sahib" wins over fragments
    catalog = sorted(set(catalog), key=len, reverse=True)
    hits: list[tuple[int, int, str]] = []
    for city in catalog:
        c = city.lower()
        for m in re.finditer(r"(?<!\w)" + re.escape(c) + r"(?!\w)", lower):
            start, end = m.start(), m.end()
            # Skip overlaps with a longer already-claimed span
            if any(not (end <= s or start >= e) for s, e, _ in hits):
                continue
            hits.append((start, end, city))
    hits.sort(key=lambda h: h[0])
    out: list[str] = []
    for _, _, city in hits:
        if city not in out:
            out.append(city)
    return out


def _fetch_party_lines(
    *,
    date_from: str,
    date_to: str,
    city: str | None = None,
    zone: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    brand_prefix: str | None = None,
) -> pd.DataFrame:
    """Line-level MT with party + taxonomy for analytics.

    Fast path: sales ↔ category in SQL, city/zone/client_type from in-memory
    clients map — same approach as ``sales_query._fetch_lines``.
    """
    from eva_dashboard.geo import normalize_zone

    init_db()
    params: list[Any] = [date_from, date_to]
    where = ["s.date >= ?", "s.date <= ?", "s.party IS NOT NULL", "trim(s.party) != ''"]
    zone_n = normalize_zone(zone) if zone else None

    if city or zone_n or client_type:
        matched = _parties_matching(
            city=city, zone=zone_n, client_type=client_type
        ) or []
        if client_type and not city and not zone_n:
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
                    "zone",
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

    sql = f"""
    SELECT
      s.date,
      s.party,
      s.inv_no,
      s.product,
      COALESCE(NULLIF(trim(c.category_1), ''), '(unmapped)') AS business_unit,
      COALESCE(NULLIF(trim(c.category_2), ''), '(unmapped)') AS oil_type,
      COALESCE(NULLIF(trim(c.packing_category), ''), '(unmapped)') AS packing_category,
      COALESCE(NULLIF(trim(s.client_type), ''), '') AS sales_client_type,
      {_MT_EXPR} AS mt
    FROM sales s
    LEFT JOIN category c ON c.product = s.product
    WHERE {' AND '.join(where)}
    """
    with connect() as conn:
        frame = pd.read_sql_query(sql, conn, params=params)
    frame = _attach_client_dims(frame)
    if city:
        ck = city.strip().lower()
        frame = frame[frame["city"].astype(str).str.strip().str.lower() == ck]
    if zone_n:
        zk = zone_n.strip().lower()
        frame = frame[frame["zone"].astype(str).str.strip().str.lower() == zk]
    if client_type:
        classified = classify_client_type_filter(client_type)
        if classified:
            mode, label = classified
            tk = label.strip().lower()
            col = "client_type_raw" if mode == "raw" else "client_type"
            if col not in frame.columns:
                col = "client_type"
            frame = frame[frame[col].astype(str).str.strip().str.lower() == tk]
    return frame.reset_index(drop=True)


def _first_sale_dates(
    *,
    city: str | None = None,
    zone: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    brand_prefix: str | None = None,
) -> dict[str, str]:
    """Earliest sale date per party (optional filters)."""
    # Reuse the fast party-line fetch over the full sales range, then min(date)
    min_d, max_d = _sales_date_bounds()
    if not min_d or not max_d:
        return {}
    frame = _fetch_party_lines(
        date_from=min_d.isoformat(),
        date_to=max_d.isoformat(),
        city=city,
        zone=zone,
        client_type=client_type,
        business_unit=business_unit,
        oil_type=oil_type,
        packing_category=packing_category,
        brand_prefix=brand_prefix,
    )
    if frame.empty:
        return {}
    firsts = frame.groupby("party", as_index=False)["date"].min()
    return {
        str(r["party"]): str(r["date"])[:10]
        for _, r in firsts.iterrows()
        if r.get("party") is not None and r.get("date") is not None
    }


def _ams_window_label(as_of: date) -> str:
    """Short label for the 3 full months before ``as_of`` (e.g. May–Jul 2026)."""
    ranges = _prior_three_month_ranges(as_of.replace(day=1))
    if not ranges:
        return ""
    start, end = ranges[0][0], ranges[-1][1]
    if start.year == end.year:
        return f"{start.strftime('%b')}–{end.strftime('%b %Y')}"
    return f"{start.strftime('%b %Y')}–{end.strftime('%b %Y')}"


def _calendar_months_spanned(date_from: str, date_to: str) -> int:
    """Inclusive calendar months in a date span (Mar 1 → Aug 12 = 6)."""
    a = date.fromisoformat(str(date_from)[:10])
    b = date.fromisoformat(str(date_to)[:10])
    return max(1, (b.year - a.year) * 12 + (b.month - a.month) + 1)


def _shift_calendar_months(d: date, delta: int) -> date:
    """Shift a date by whole calendar months, clamping the day."""
    m = d.month + int(delta)
    y = d.year
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def _ams_by_entity(
    *,
    as_of: date,
    entity_key: str,
    city: str | None,
    client_type: str | None,
    business_unit: str | None,
    oil_type: str | None,
    packing_category: str | None,
    brand_prefix: str | None,
    zone: str | None = None,
) -> dict[str, float]:
    """Average monthly sales for the prior 3 full months, keyed by entity_key.

    entity_key must match the ranking grain: party | city | zone.
    """
    key = entity_key if entity_key in {"party", "city", "zone"} else "party"
    ranges = _prior_three_month_ranges(as_of)
    monthly: list[dict[str, float]] = []
    keys: set[str] = set()
    for start, end in ranges:
        frame = _fetch_party_lines(
            date_from=start.isoformat(),
            date_to=end.isoformat(),
            city=city,
            zone=zone,
            client_type=client_type,
            business_unit=business_unit,
            oil_type=oil_type,
            packing_category=packing_category,
            brand_prefix=brand_prefix,
        )
        if frame.empty:
            monthly.append({})
            continue
        if key not in frame.columns:
            monthly.append({})
            continue
        totals = {
            str(k): float(v)
            for k, v in frame.groupby(key)["mt"].sum().items()
            if str(k).strip()
        }
        keys.update(totals)
        monthly.append(totals)
    return {name: sum(m.get(name, 0.0) for m in monthly) / 3.0 for name in keys}


def _ams_by_party(
    *,
    as_of: date,
    city: str | None,
    client_type: str | None,
    business_unit: str | None,
    oil_type: str | None,
    packing_category: str | None,
    brand_prefix: str | None,
    zone: str | None = None,
) -> dict[str, float]:
    return _ams_by_entity(
        as_of=as_of,
        entity_key="party",
        city=city,
        zone=zone,
        client_type=client_type,
        business_unit=business_unit,
        oil_type=oil_type,
        packing_category=packing_category,
        brand_prefix=brand_prefix,
    )


def list_clients(
    *,
    city: str | None = None,
    zone: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 200,
    include_zero: bool = False,
    active_only: bool = False,
) -> dict[str, Any]:
    """List clients matching city / zone / client type / BU — not fuzzy name search.

    ``business_unit`` scopes the volume ranking (who bought that BU) while
    still listing clients in the city/type scope when provided.
    ``active_only`` drops clients flagged inactive on the master.
    """
    from eva_dashboard.geo import normalize_zone, resolve_city_zone

    city_f = (city or "").strip() or None
    zone_f = normalize_zone(zone) if zone else None
    ctype = normalize_client_type((client_type or "").strip() or None)
    bu = _normalize_business_unit(business_unit)
    active_only_f = bool(active_only)
    if not city_f and not zone_f and not ctype and not bu:
        return {
            "ok": False,
            "error": (
                "Pass city, zone, client_type, and/or business_unit "
                "(e.g. Lahore + Eva Distributors, SOUTH zone, or Maan Consumer)."
            ),
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
    clients = pd.DataFrame()
    # BU-only (no city/zone/type): discover buyers from sales — skip full client scan
    if bu and not city_f and not zone_f and not ctype and d0 and d1:
        sales_only = _fetch_party_lines(
            date_from=d0,
            date_to=d1,
            business_unit=bu,
        )
        if not sales_only.empty:
            clients = (
                sales_only.groupby("party", as_index=False)
                .agg(mt=("mt", "sum"))
                .rename(columns={"party": "client"})
            )
            clients["client_type"] = ""
            clients["city_filter"] = ""
            clients["city"] = ""
            clients["zone"] = ""
            clients["inactive"] = ""
    else:
        params: list[Any] = []
        where = ["cl.client IS NOT NULL", "trim(cl.client) != ''"]
        # Specific old type → exact raw match; NEW group → all source types
        if ctype:
            raw_types = sql_client_type_values(ctype) or [ctype]
            placeholders = ",".join("?" for _ in raw_types)
            where.append(
                f"lower(trim(COALESCE(cl.type, ''))) IN ({placeholders})"
            )
            params.extend(t.lower().strip() for t in raw_types)

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
        if not clients.empty:
            clients["client_type_raw"] = clients["client_type"]
            classified = classify_client_type_filter(ctype) if ctype else None
            if classified and classified[0] == "raw":
                # Keep the specific old label when the user asked for it
                clients["client_type"] = clients["client_type_raw"]
            else:
                clients["client_type"] = clients["client_type_raw"].map(
                    lambda t: map_client_type(t) or t
                )
            resolved = clients["city_filter"].map(resolve_city_zone)
            clients["city_filter"] = resolved.map(lambda x: x[0])
            clients["zone"] = resolved.map(lambda x: x[1])
            if city_f:
                ck = city_f.strip().lower()
                clients = clients[
                    clients["city_filter"].astype(str).str.strip().str.lower() == ck
                ]
            if zone_f:
                zk = zone_f.strip().lower()
                clients = clients[
                    clients["zone"].astype(str).str.strip().str.lower() == zk
                ]
            if ctype and classified:
                mode, label = classified
                tk = label.strip().lower()
                col = "client_type_raw" if mode == "raw" else "client_type"
                clients = clients[
                    clients[col].astype(str).str.strip().str.lower() == tk
                ]

    mt_map: dict[str, float] = {}
    from_sales_only = bool(
        bu and not city_f and not zone_f and not ctype and "mt" in clients.columns
    )
    if d0 and d1 and not clients.empty and not from_sales_only:
        sales = _fetch_party_lines(
            date_from=d0,
            date_to=d1,
            city=city_f,
            zone=zone_f,
            client_type=ctype,
            business_unit=bu,
        )
        if not sales.empty:
            mt_map = {
                str(k): float(v)
                for k, v in sales.groupby("party")["mt"].sum().items()
            }

    rows: list[dict[str, Any]] = []
    inactive_lookup: dict[str, str] = {}
    if active_only_f:
        try:
            from eva_dashboard.sales_query import _clients_lookup, _norm_party_key

            inactive_lookup = {
                _norm_party_key(meta.get("client") or key): str(
                    meta.get("inactive") or ""
                )
                for key, meta in _clients_lookup().items()
            }
        except Exception:  # noqa: BLE001
            inactive_lookup = {}
    for _, r in clients.iterrows():
        name = str(r["client"])
        inactive_flag = str(r.get("inactive") or "").strip().upper()
        if not inactive_flag and inactive_lookup:
            from eva_dashboard.sales_query import _norm_party_key

            inactive_flag = str(
                inactive_lookup.get(_norm_party_key(name), "")
            ).strip().upper()
        if active_only_f and inactive_flag in {
            "Y",
            "YES",
            "1",
            "TRUE",
            "INACTIVE",
        }:
            continue
        if from_sales_only:
            mt = round(float(r.get("mt") or 0.0), 3)
        else:
            mt = round(mt_map.get(name, 0.0), 3)
        if not include_zero and mt <= 0:
            continue
        rows.append(
            {
                "client": name,
                "client_type": r.get("client_type") or ctype or "—",
                "city_filter": (r.get("city_filter") or None) or None,
                "city": (r.get("city") or None) or None,
                "zone": (r.get("zone") or None) or None,
                "inactive": inactive_flag or (r.get("inactive") or None) or None,
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
    if zone_f:
        scope_bits.append(f"Zone **{zone_f}**")
    if bu:
        scope_bits.append(f"BU **{bu}**")
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
    show_zone = not (
        zone_f and all(str(r.get("zone") or "") == zone_f for r in rows)
    )

    headers = ["#", "Client"]
    if show_type:
        headers.append("Client Type")
    if show_zone:
        headers.append("Zone")
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
        if show_zone:
            cells.append(str(r.get("zone") or "—").replace("|", "/"))
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
        "filters": {
            "city": city_f,
            "zone": zone_f,
            "client_type": ctype,
            "business_unit": bu,
            "active_only": True if active_only_f else None,
        },
        "period_phrase": period,
        "period": {
            "date_from": (period_info or {}).get("date_from"),
            "date_to": (period_info or {}).get("date_to"),
            "label": (period_info or {}).get("label"),
        },
        "limit": lim,
        "include_zero": include_zero,
        "active_only": active_only_f,
    }

    return {
        "ok": True,
        "mode": "list_clients",
        "filters": {
            "city": city_f,
            "zone": zone_f,
            "client_type": ctype,
            "business_unit": bu,
        },
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
    zone: str | None = None,
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
    grown_only: bool = False,
    declined_only: bool = False,
    active_only: bool = False,
    title_mode: str | None = None,
    metric_filters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Rank / summarize parties or cities.

    metric:
      volume | ams | vs_ams | underperformers | yoy | yoy_ams | ams_growth |
      share_of_segment | segment_mix | geo_share | doing_well | new_parties |
      lost_parties | packing_mix | product_mix | invoices | invoice_mt

    group_by: party (default) | city | zone
    mix_dimension: packing_category | product (for mix metrics)
    per_party_mix: when True with packing/product_mix, one mix table per party
    sort: desc (default) | asc  — underperformers force asc on % vs AMS
    grown_only: when True with yoy/yoy_ams/ams_growth, keep growth % > 0
    declined_only: when True with those metrics, keep growth % < 0
    metric_filters: post-agg cuts e.g. [{metric:ams, op:gt, value:10}]
    title_mode: biggest_gains | smallest_gains | biggest_declines | by_growth
    Default ranking metric is AMS unless the user asks for volume/growth.
    """
    from eva_dashboard.geo import normalize_zone

    city_f = (city or "").strip() or None
    zone_f = normalize_zone(zone) if zone else None
    ctype = normalize_client_type((client_type or "").strip() or None)
    bu = _normalize_business_unit(business_unit)
    oil = normalize_oil_type((oil_type or "").strip() or None)
    pack = normalize_packing_category((packing_category or "").strip() or None)
    active_only_f = bool(active_only)
    brand_prefix = None
    brand_n = (brand or "").strip().lower()
    if brand_n in {"eva", "eva foods"}:
        brand_prefix = "Eva"
    elif brand_n in {"maan"}:
        brand_prefix = "Maan"

    group = (group_by or "party").strip().lower()
    if group in {"cities", "city_filter"}:
        group = "city"
    if group in {"zones", "region", "regions"}:
        group = "zone"
    if group not in {"party", "city", "zone"}:
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
    if metric_n in {
        "ams_growth",
        "ams_yoy",
        "growth_ams",
        "ams_and_month_yoy",
        "dual_growth",
    }:
        metric_n = "ams_growth"
    if metric_n in {"year_over_year", "yoy_growth", "growth", "sales_growth"}:
        metric_n = "yoy"
    if metric_n in {"yoy_ams", "yoy_vs_ams", "growth_vs_ams", "ams_and_yoy"}:
        metric_n = "yoy_ams"
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

    # Default period: current data month (MTD if partial). Growth ranks use
    # the last *full* calendar month so YoY is not a partial-vs-partial compare.
    if not period and not date_from:
        period = "this month"
    period_info = resolve_period(period, date_from=date_from, date_to=date_to)
    if period_info.get("ok") is False or not period_info.get("date_from"):
        return {
            "ok": False,
            "error": period_info.get("error") or "Bad period",
            "period": period_info,
        }
    if (
        metric_n == "ams_growth"
        and not date_from
        and (not period or str(period).strip().lower() in {"this month", "mtd"})
        and bool(period_info.get("partial_month"))
    ):
        # Step back to previous full month from the MTD end date
        end = date.fromisoformat(str(period_info["date_to"]))
        y, m = end.year, end.month - 1
        if m == 0:
            y, m = y - 1, 12
        last_full = date(y, m, calendar.monthrange(y, m)[1])
        period_info = resolve_period(last_full.strftime("%B %Y"))
        if period_info.get("ok") is False or not period_info.get("date_from"):
            return {
                "ok": False,
                "error": period_info.get("error") or "Bad period",
                "period": period_info,
            }
    d0, d1 = period_info["date_from"], period_info["date_to"]
    as_of = date.fromisoformat(d1)
    span_months = _calendar_months_spanned(d0, d1)

    filters = {
        "city": city_f,
        "zone": zone_f,
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
            zone=zone_f,
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
            zone=zone_f,
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
            zone=zone_f,
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
            zone=zone_f,
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
            zone=zone_f,
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
            zone=zone_f,
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
        zone=zone_f,
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
            zone=zone_f,
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
        # AMS share of segment within each party's baseline (prior 3 months)
        party_ams = _ams_by_entity(
            as_of=as_of.replace(day=1),
            entity_key="party",
            city=city_f,
            zone=zone_f,
            client_type=ctype,
            business_unit=None,
            oil_type=None,
            packing_category=None,
            brand_prefix=None,
        )
        seg_ams = _ams_by_entity(
            as_of=as_of.replace(day=1),
            entity_key="party",
            city=city_f,
            zone=zone_f,
            client_type=ctype,
            business_unit=segment_bu,
            oil_type=segment_oil,
            packing_category=segment_pack,
            brand_prefix=brand_prefix,
        )
        rows = []
        parties = set(party_total.index) | set(seg_total.index)
        for party in parties:
            p_mt = float(party_total.get(party, 0.0))
            s_mt = float(seg_total.get(party, 0.0))
            p_ams = float(party_ams.get(party, 0.0))
            s_ams = float(seg_ams.get(party, 0.0))
            if metric_n == "share_of_segment":
                score = (s_mt / all_seg_mt * 100.0) if all_seg_mt else None
                ams_share = None
            else:
                score = (s_mt / p_mt * 100.0) if p_mt else None
                ams_share = (s_ams / p_ams * 100.0) if p_ams else None
            meta = _party_meta(party_frame if not party_frame.empty else seg_frame, party)
            rows.append(
                {
                    "party": party,
                    **meta,
                    "volume_mt": mt_round(p_mt),
                    "segment_mt": mt_round(s_mt),
                    "ams_mt": mt_round(p_ams),
                    "segment_ams_mt": mt_round(s_ams),
                    "ams_share_pct": (
                        round(ams_share, 1) if ams_share is not None else None
                    ),
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
        extra = ["volume_mt", "segment_mt"]
        overrides = None
        if metric_n == "segment_mix":
            extra = [
                "volume_mt",
                "segment_mt",
                "ams_mt",
                "segment_ams_mt",
                "ams_share_pct",
            ]
            overrides = {
                "segment_mt": "Segment vol (MT)",
                "segment_ams_mt": "Segment AMS (MT)",
                "ams_share_pct": "% of party AMS",
            }
        return _party_table_result(
            rows=rows,
            period_info=period_info,
            filters={**filters, "segment_oil": segment_oil},
            metric=metric_n,
            score_label=score_label,
            extra_cols=extra,
            blurb=(
                f"Party share of "
                f"{segment_oil or segment_bu or segment_pack or brand_prefix} "
                f"({_scope_blurb(filters, period_info)})"
                + (
                    " · % of party volume and % of party AMS"
                    if metric_n == "segment_mix"
                    else ""
                )
            ),
            col_label_overrides=overrides,
        )

    # Volume / AMS / vs AMS / doing well / YoY / invoices (party / city / zone)
    if group == "zone":
        entity_key = "zone"
    elif group == "city":
        entity_key = "city"
    else:
        entity_key = "party"
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

    ams = _ams_by_entity(
        as_of=as_of.replace(day=1),
        entity_key=entity_key if group in {"city", "zone", "party"} else "party",
        city=city_f,
        zone=zone_f,
        client_type=ctype,
        business_unit=bu,
        oil_type=oil,
        packing_category=pack,
        brand_prefix=brand_prefix,
    )

    compare_info = None
    compare_vol: dict[str, float] = {}
    mom_vol: dict[str, float] = {}
    filter_metrics = {
        str(f.get("metric") or "") for f in (metric_filters or [])
    }
    want_yoy = metric_n in {"yoy", "yoy_ams", "ams_growth"} or "yoy" in filter_metrics
    want_pop = metric_n == "pop" or "pop" in filter_metrics
    want_mom = metric_n == "mom" or "mom" in filter_metrics
    want_ams_growth = metric_n == "ams_growth" or "ams_growth" in filter_metrics
    if want_pop:
        start = date.fromisoformat(d0)
        end = date.fromisoformat(d1)
        c_start = _shift_calendar_months(start, -span_months)
        c_end = _shift_calendar_months(end, -span_months)
        compare_info = {
            "ok": True,
            "date_from": c_start.isoformat(),
            "date_to": c_end.isoformat(),
            "label": (
                f"{c_start.strftime('%b %Y')} → {c_end.strftime('%b %Y')} "
                f"(prior {span_months} months)"
            ),
        }
        cmp_frame = _fetch_party_lines(
            date_from=compare_info["date_from"],
            date_to=compare_info["date_to"],
            city=city_f,
            zone=zone_f,
            client_type=ctype,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            brand_prefix=brand_prefix,
        )
        if not cmp_frame.empty:
            compare_vol = cmp_frame.groupby(entity_key)["mt"].sum().to_dict()
    elif want_yoy:
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
                "label": (
                    f"{c_start.strftime('%b %Y')} → {c_end.strftime('%b %Y')} (YoY)"
                ),
            }
        if compare_info.get("ok") is False:
            return {"ok": False, "error": compare_info.get("error"), "period": compare_info}
        cmp_frame = _fetch_party_lines(
            date_from=compare_info["date_from"],
            date_to=compare_info["date_to"],
            city=city_f,
            zone=zone_f,
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
    if want_mom:
        from eva_dashboard.advanced_analytics import _mom_prior_dates

        m0, m1 = _mom_prior_dates(d0, d1)
        mom_frame = _fetch_party_lines(
            date_from=m0,
            date_to=m1,
            city=city_f,
            zone=zone_f,
            client_type=ctype,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            brand_prefix=brand_prefix,
        )
        mom_vol = (
            mom_frame.groupby(entity_key)["mt"].sum().to_dict()
            if not mom_frame.empty
            else {}
        )

    # Prior 3-month AMS window (the 3 full months before current AMS window)
    prior_ams: dict[str, float] = {}
    ams_current_label = ""
    ams_prior_label = ""
    if want_ams_growth:
        ams_as_of = as_of.replace(day=1)
        ams_current_label = _ams_window_label(ams_as_of)
        prior_as_of = ams_as_of
        for _ in range(3):
            y, m = prior_as_of.year, prior_as_of.month - 1
            if m == 0:
                y, m = y - 1, 12
            prior_as_of = date(y, m, 1)
        ams_prior_label = _ams_window_label(prior_as_of)
        # Prior AMS must use the SAME grain as the ranking (city/zone/party)
        prior_ams = _ams_by_entity(
            as_of=prior_as_of,
            entity_key=entity_key if group in {"city", "zone", "party"} else "party",
            city=city_f,
            zone=zone_f,
            client_type=ctype,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            brand_prefix=brand_prefix,
        )

    partial = bool(period_info.get("partial_month"))
    days_elapsed = int(period_info.get("days_elapsed") or 0)
    days_in_month = int(period_info.get("days_in_month") or 30)

    rows = []
    entities = (
        set(volume)
        | set(ams)
        | set(compare_vol)
        | set(mom_vol)
        | set(invoice_counts)
        | set(prior_ams)
    )
    for ent in entities:
        vol = float(volume.get(ent, 0.0))
        ams_v = float(ams.get(ent, 0.0))
        ams_prior_v = float(prior_ams.get(ent, 0.0))
        expected = None
        if partial and days_in_month:
            expected = (days_elapsed / days_in_month) * ams_v
        baseline = expected if (partial and expected is not None) else ams_v
        vs = pct_change(vol, baseline) if baseline else None
        prior = float(compare_vol.get(ent, 0.0))
        yoy = pct_change(vol, prior) if want_yoy else None
        pop = pct_change(vol, prior) if want_pop else None
        mom_prior = float(mom_vol.get(ent, 0.0))
        mom = pct_change(vol, mom_prior) if want_mom else None
        ams_growth = (
            pct_change(ams_v, ams_prior_v) if want_ams_growth else None
        )
        period_ams = (vol / span_months) if span_months >= 2 else None
        prior_period_ams = (
            (prior / span_months)
            if (want_yoy or want_pop)
            and span_months >= 2
            else None
        )
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
            "volume_mt": vol,
            "ams_mt": ams_v,
            "period_ams_mt": period_ams,
            "prior_period_ams_mt": prior_period_ams,
            "ams_prior_mt": ams_prior_v if want_ams_growth else None,
            "ams_growth_pct": ams_growth,
            "expected_mt": expected if expected is not None else None,
            "pct_vs_ams": vs,
            "prior_mt": prior if (want_yoy or want_pop) else None,
            "yoy_pct": yoy,
            "pop_pct": pop,
            "mom_prior_mt": mom_prior if want_mom else None,
            "mom_pct": mom,
            "invoices": inv_n,
            "avg_invoice_mt": mt_round(avg_inv) if avg_inv is not None else None,
            "doing_well": bool(vs is not None and vs >= 0),
        }
        rows.append(entry)

    if active_only_f and group == "party" and rows:
        try:
            from eva_dashboard.sales_query import _inactive_party_keys, _norm_party_key

            inactive_keys = _inactive_party_keys()
            rows = [
                r
                for r in rows
                if _norm_party_key(str(r.get("party") or "")) not in inactive_keys
            ]
        except Exception:  # noqa: BLE001
            pass
    if active_only_f:
        filters["active_only"] = True

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
            vol_cell = mt_round(r["volume_mt"])
            base_cell = mt_round(base) if base is not None else "—"
            lines.append(
                f"| {name} | {r.get('city') or '—'} | {vol_cell} | "
                f"{base_cell} | "
                f"{r['pct_vs_ams']:+.1f}% |"
                if r["pct_vs_ams"] is not None
                else f"| {name} | {r.get('city') or '—'} | {vol_cell} | "
                f"{base_cell} | — |"
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
        # AMS=0 means no prior-3-month baseline — not "lowest AMS".
        # Ascending ranks would otherwise fill with zeros while Volume > 0.
        if sort_n == "asc":
            rows = [r for r in rows if (r["ams_mt"] or 0) > 0]
        rows.sort(key=lambda r: sk(r["ams_mt"], r["volume_mt"] or 0))
        score_key, score_label = "ams_mt", "AMS (MT)"
    elif metric_n == "vs_ams":
        # Need an AMS baseline to judge under/over-performance.
        # Drop empty shells (0 volume AND 0 AMS) — they are not real declines.
        rows = [
            r
            for r in rows
            if (r.get("ams_mt") or 0) > 0
            and (
                (r.get("volume_mt") or 0) > 0
                or (r.get("ams_mt") or 0) > 0
            )
        ]
        if declined_only:
            rows = [
                r
                for r in rows
                if isinstance(r.get("pct_vs_ams"), (int, float))
                and float(r["pct_vs_ams"]) < 0
            ]
        rows.sort(key=lambda r: sk(r["pct_vs_ams"], r["volume_mt"] or 0))
        score_key, score_label = "pct_vs_ams", "% vs AMS/Expected"
    elif metric_n == "ams_growth":
        # Semantic layer: exclude zero-AMS baselines (useless comparisons).
        rows = [
            r
            for r in rows
            if (r.get("ams_mt") or 0) > 0 and (r.get("ams_prior_mt") or 0) > 0
        ]
        rows.sort(
            key=lambda r: sk(
                r.get("ams_growth_pct"),
                float(r.get("ams_prior_mt") or 0),
            )
        )
        if grown_only and not declined_only:
            grown = [
                r
                for r in rows
                if isinstance(r.get("ams_growth_pct"), (int, float))
                and float(r["ams_growth_pct"]) > 0
            ]
            # Fallback: "grown since last year" may only move on volume YoY
            if not grown:
                grown = [
                    r
                    for r in rows
                    if isinstance(r.get("yoy_pct"), (int, float))
                    and float(r["yoy_pct"]) > 0
                ]
            if grown:
                rows = grown
            else:
                grown_only = False
        if declined_only:
            # Meaningful AMS declines: had prior AMS and current AMS fell.
            declined = [
                r
                for r in rows
                if (r.get("ams_prior_mt") or 0) > 0
                and isinstance(r.get("ams_growth_pct"), (int, float))
                and float(r["ams_growth_pct"]) < 0
            ]
            if declined:
                rows = declined
            else:
                declined_only = False
        score_key, score_label = "ams_growth_pct", "AMS growth %"
    elif metric_n in {"yoy", "yoy_ams"}:
        rows = [r for r in rows if (r["volume_mt"] or 0) > 0 or (r["prior_mt"] or 0) > 0]
        rows.sort(key=lambda r: sk(r["yoy_pct"], r["volume_mt"] or 0))
        if grown_only and not declined_only:
            grown = [
                r
                for r in rows
                if isinstance(r.get("yoy_pct"), (int, float)) and float(r["yoy_pct"]) > 0
            ]
            if grown:
                rows = grown
            else:
                grown_only = False
        if declined_only:
            declined = [
                r
                for r in rows
                if isinstance(r.get("yoy_pct"), (int, float)) and float(r["yoy_pct"]) < 0
            ]
            if declined:
                rows = declined
            else:
                declined_only = False
        score_key, score_label = "yoy_pct", "YoY %"
    elif metric_n == "pop":
        rows = [
            r
            for r in rows
            if (r["volume_mt"] or 0) > 0 or (r["prior_mt"] or 0) > 0
        ]
        rows.sort(key=lambda r: sk(r["pop_pct"], r["volume_mt"] or 0))
        score_key, score_label = "pop_pct", "vs prior period %"
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

    # Spoken / planned numeric cuts (AMS > 10, growth > 30%, …) before limit
    if metric_filters:
        from eva_dashboard.metric_filters import apply_metric_filters

        rows = apply_metric_filters(rows, metric_filters)

    _MT_KEYS = (
        "volume_mt",
        "ams_mt",
        "period_ams_mt",
        "prior_period_ams_mt",
        "ams_prior_mt",
        "expected_mt",
        "prior_mt",
        "mom_prior_mt",
        "avg_invoice_mt",
    )
    _PCT_KEYS = ("ams_growth_pct", "pct_vs_ams", "yoy_pct", "mom_pct", "pop_pct")
    for r in rows:
        for k in _MT_KEYS:
            if r.get(k) is not None:
                r[k] = mt_round(r[k])
        for k in _PCT_KEYS:
            if r.get(k) is not None:
                r[k] = round(float(r[k]), 1)

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
    if metric_n == "pop":
        extra = [
            "volume_mt",
            "period_ams_mt",
            "prior_mt",
            "prior_period_ams_mt",
            "pop_pct",
        ]
    if metric_n == "yoy_ams":
        extra = ["volume_mt", "ams_mt", "pct_vs_ams", "prior_mt", "yoy_pct"]
    if metric_n == "ams_growth":
        # AMS columns only — do not mix YoY volume (confuses "last year AMS").
        extra = [
            "ams_mt",
            "ams_prior_mt",
            "ams_growth_pct",
            "volume_mt",
        ]
    if metric_n == "invoices":
        extra = ["invoices", "volume_mt", "avg_invoice_mt"]
    if metric_n == "invoice_mt":
        extra = ["avg_invoice_mt", "invoices", "volume_mt"]
    # Always surface columns the stacked cuts actually test (volume+yoy, …)
    _FILTER_EXTRA = {
        "volume": ("volume_mt",),
        "ams": ("ams_mt",),
        "vs_ams": ("pct_vs_ams",),
        "yoy": ("prior_mt", "yoy_pct"),
        "pop": ("period_ams_mt", "prior_mt", "prior_period_ams_mt", "pop_pct"),
        "mom": ("mom_prior_mt", "mom_pct"),
        "ams_growth": ("ams_mt", "ams_prior_mt", "ams_growth_pct"),
    }
    for mid in filter_metrics:
        for col in _FILTER_EXTRA.get(mid, ()):
            if col not in extra:
                extra.append(col)

    blurb = _scope_blurb(filters, period_info)
    entity_word = "cities" if group == "city" else "parties"
    col_label_overrides: dict[str, str] | None = None
    if metric_n == "ams_growth":
        cur_lbl = ams_current_label or "current 3 months"
        prior_lbl = ams_prior_label or "prior 3 months"
        col_label_overrides = {
            "ams_mt": f"AMS current ({cur_lbl})",
            "ams_prior_mt": f"AMS prior ({prior_lbl})",
            "ams_growth_pct": "AMS growth %",
            "volume_mt": "Volume in period (MT)",
        }
        blurb += (
            f" · AMS current = avg MT of {cur_lbl}"
            f" · AMS prior = avg MT of {prior_lbl}"
        )
        if grown_only:
            blurb += " · grown only"
        if declined_only:
            blurb += " · declined only"
        if metric_filters:
            from eva_dashboard.metric_filters import metric_filters_blurb

            cut = metric_filters_blurb(metric_filters)
            if cut:
                blurb += f" · {cut}"
        mode = (title_mode or "").strip().lower()
        if not mode:
            if declined_only:
                mode = "biggest_declines"
            elif sort_n == "asc":
                mode = "smallest_gains"
            elif grown_only:
                mode = "biggest_gains"
            else:
                mode = "by_growth"
        entity_title = "Cities" if group == "city" else (
            "Zones" if group == "zone" else "Parties"
        )
        if mode == "biggest_declines":
            blurb = f"Biggest AMS declines — {blurb}"
        elif mode == "smallest_gains":
            blurb = f"Smallest AMS gains — {blurb}"
        elif mode == "biggest_gains":
            blurb = f"Biggest AMS gains — {blurb}"
        else:
            blurb = f"{entity_title} by AMS growth % — {blurb}"
    else:
        if compare_info and (
            metric_n in {"yoy", "yoy_ams", "pop"}
            or "yoy_pct" in extra
            or "pop_pct" in extra
        ):
            blurb += f" vs {compare_info.get('label')}"
            if grown_only:
                blurb += " · grown only"
            if declined_only:
                blurb += " · declined only"
            if metric_n == "pop":
                col_label_overrides = {
                    "prior_mt": "Volume prior period (MT)",
                    "pop_pct": "vs prior period %",
                    "period_ams_mt": "AMS in period (MT)",
                    "prior_period_ams_mt": "AMS prior period (MT)",
                    "volume_mt": "Volume in period (MT)",
                }
            else:
                col_label_overrides = {
                    "prior_mt": "Volume last year (MT)",
                    "yoy_pct": "Volume YoY %",
                }
        if metric_n == "vs_ams":
            direction = "furthest behind" if sort_n == "asc" else "furthest ahead of"
            blurb += (
                f" · {entity_word} {direction} "
                + (
                    "Expected (days/month × AMS)"
                    if partial
                    else "AMS (3 prior months)"
                )
            )
        if metric_filters:
            from eva_dashboard.metric_filters import metric_filters_blurb

            cut = metric_filters_blurb(metric_filters)
            if cut:
                blurb += f" · {cut}"
        mode = (title_mode or "").strip().lower()
        low_title = sort_n == "asc" or mode in {
            "underperformers",
            "lowest",
            "smallest",
            "smallest_gains",
        }
        if low_title:
            blurb = f"Lowest {entity_word} by {score_label} — {blurb}"
        else:
            blurb = f"Top {entity_word} by {score_label} — {blurb}"

    return _party_table_result(
        rows=rows,
        period_info=period_info,
        filters=filters,
        metric=metric_n,
        score_label=score_label,
        extra_cols=extra,
        blurb=blurb,
        compare_period=compare_info,
        entity_key=entity_key,
        col_label_overrides=col_label_overrides,
    )


def _party_meta(frame: pd.DataFrame, party: str) -> dict[str, Any]:
    part = frame[frame["party"] == party]
    ctype = city = zone = None
    if not part.empty:
        if "client_type" in part.columns:
            raw = str(part["client_type"].iloc[0] or "").strip()
            ctype = raw or None
        if "city" in part.columns:
            raw = str(part["city"].iloc[0] or "").strip()
            city = raw or None
        if "zone" in part.columns:
            raw = str(part["zone"].iloc[0] or "").strip()
            zone = raw or None
    # Fill gaps from clients master (name mismatches / AMS-only parties)
    if not ctype or not city:
        try:
            from eva_dashboard.sales_query import _clients_lookup, _norm_party_key

            meta = _clients_lookup().get(_norm_party_key(party)) or {}
            if not ctype:
                ctype = (meta.get("type") or meta.get("type_raw") or None) or None
            if not city:
                city = meta.get("city") or None
            if not zone:
                zone = meta.get("zone") or None
        except Exception:  # noqa: BLE001
            pass
    return {"client_type": ctype, "city": city, "zone": zone}


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

    if metric in {"yoy", "yoy_ams"}:
        scored = [r for r in rows if isinstance(r.get("yoy_pct"), (int, float))]
        if scored:
            best = scored[0]
            tip = (
                f"**{best.get('party') or best.get('city')}** leads YoY "
                f"({best['yoy_pct']:+.1f}%; {best.get('volume_mt')} vs "
                f"{best.get('prior_mt')} MT prior)"
            )
            if metric == "yoy_ams" and best.get("pct_vs_ams") is not None:
                tip += f"; {best['pct_vs_ams']:+.1f}% vs AMS"
            tips.append(tip + ".")
            neg = [r for r in scored if float(r["yoy_pct"]) < 0]
            if neg:
                tips.append(
                    f"{len(neg)} of {len(scored)} listed are still down YoY."
                )
            elif metric == "yoy_ams":
                behind = [
                    r
                    for r in scored
                    if isinstance(r.get("pct_vs_ams"), (int, float))
                    and float(r["pct_vs_ams"]) < 0
                ]
                if behind:
                    tips.append(
                        f"{len(behind)} of {len(scored)} listed are below AMS."
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
    if filters.get("zone"):
        bits.append(f"zone **{filters['zone']}**")
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
    col_label_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    col_labels = {
        "volume_mt": "Volume (MT)",
        "ams_mt": "AMS (MT)",
        "period_ams_mt": "AMS in period (MT)",
        "prior_period_ams_mt": "AMS last year (MT)",
        "ams_prior_mt": "AMS prior (MT)",
        "ams_growth_pct": "AMS growth %",
        "expected_mt": "Expected (MT)",
        "segment_mt": "Segment (MT)",
        "segment_ams_mt": "Segment AMS (MT)",
        "ams_share_pct": "% of party AMS",
        "prior_mt": "Volume last year (MT)",
        "pct_vs_ams": "% vs AMS",
        "yoy_pct": "Volume YoY %",
        "pop_pct": "vs prior period %",
        "first_sale": "First sale",
        "invoices": "Invoices",
        "avg_invoice_mt": "Avg MT / invoice",
    }
    if col_label_overrides:
        col_labels.update(col_label_overrides)
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
            elif c.endswith("_mt"):
                cells.append(str(mt_round(val)))
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
                "zone": filters.get("zone"),
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
            "group_by": (
                entity_key if entity_key in {"party", "city", "zone"} else "party"
            ),
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
    columns: str = "month",
    months_back: int = 6,
    mode: str = "matrix",
) -> dict[str, Any]:
    """Resolve a named client/party then show their sales for a period.

    Default view: last N months as columns + AMS (3/6 months).
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
    best_score = max(float(m.get("match_score") or 0) for m in matches)
    # Weak fuzzy hits (e.g. nonsense query → random party at 0.4) are not matches.
    if not exact and best_score < 0.50:
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
        columns=columns or "month",
        months_back=int(months_back or 6),
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


