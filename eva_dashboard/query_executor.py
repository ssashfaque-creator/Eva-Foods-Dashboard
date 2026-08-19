"""Blind execution of a QuerySpec — universal pivot + legacy intent compat.

The LLM is the sole source of query parameters. This module:
1. Normalizes / validates the plan (universal rows/cols/metrics)
2. Merges prior only when base/context_handling='prior' + clear[]
3. Maps period_type → date windows (deterministic)
4. Runs engines (query_sales / analyze_parties / universal_pivot / …)
5. On invalid plans, returns ok=False + errors for the LLM to self-correct

Party names are resolved silently via ILIKE (no ambiguous-party retry loops).
Price Fetch uses a dedicated table renderer (never the monthly trend fallback).
"""

from __future__ import annotations

import re
from typing import Any

from eva_dashboard.client_language import (
    extract_all_client_types_from_text,
    extract_client_type_from_text,
    extract_oil_and_packing,
    extract_oil_type_from_text,
    match_client_type_alias,
    normalize_client_type,
    normalize_oil_type,
    normalize_packing_category,
)
from eva_dashboard.geo import extract_zone_from_text, normalize_zone
from eva_dashboard.advanced_analytics import party_profile
from eva_dashboard.party_analytics import (
    analyze_parties,
    extract_cities_from_text,
    list_clients,
    lookup_party,
    party_sales,
)
from eva_dashboard.entity_catalog import (
    BRAND_ENTITY_MAP,
    is_business_unit_label,
    resolve_extracted_entities,
)
from eva_dashboard.party_match import resolve_party_filter, resolve_party_filters
from eva_dashboard.query_spec import (
    PARTY_SCOPE_KEYS,
    merge_prior_into_spec,
    normalize_query_spec,
    resolve_period_from_spec,
    validate_query_spec,
)
from eva_dashboard.sales_query import (
    DEFAULT_FACTOR_CLIENT_TYPE,
    query_factor_costs,
    query_price,
    query_price_fetch_table,
    query_sales,
)
from eva_dashboard.universal_pivot import execute_universal_pivot
from eva_dashboard.spoken_constraints import (
    apply_spoken_constraints,
    extract_exclude_phrases as _spoken_exclude_phrases,
    party_exclude_needles as _party_exclude_needles,
    resolve_exclude_map as _resolve_spoken_excludes,
    strip_include_conflicts as _strip_conflicting_party_includes,
)


def _norm_key(text: str) -> str:
    return " ".join(str(text or "").strip().lower().replace("-", " ").split())


def _expand_business_units(values: list[str] | None) -> list[str]:
    """Expand brand phrases (Eva → Eva Consumer + Eva Bulk)."""
    out: list[str] = []
    for raw in values or []:
        key = _norm_key(str(raw))
        if key in BRAND_ENTITY_MAP:
            for b in BRAND_ENTITY_MAP[key]:
                if b not in out:
                    out.append(b)
        elif str(raw).strip() and str(raw).strip() not in out:
            out.append(str(raw).strip())
    return out


def _enforce_exclude_wins(
    spec: dict[str, Any],
    *,
    user_text: str = "",
) -> dict[str, Any]:
    """Authoritative polarity gate (any entity, not one-off names)."""
    return apply_spoken_constraints(spec, user_text=user_text)


def _build_party_polarity_preview(
    *,
    needles: list[str],
    polarity: str,
    period_phrase: str | None,
    date_from: str | None,
    date_to: str | None,
    filters: dict[str, Any],
    bus: list[str] | None = None,
) -> str:
    """Identification table for parties being excluded or included.

    Shown *before* the main result so fuzzy names like "al shaheer" are
    confirmed against the live clients/sales master.
    """
    from eva_dashboard.party_match import list_party_matches
    from eva_dashboard.sales_query import resolve_period

    clean = [str(n).strip() for n in needles if str(n or "").strip()]
    if not clean:
        return ""

    matched: list[dict[str, Any]] = []
    seen: set[str] = set()
    for needle in clean:
        # Exact/LIKE only — never fuzzy full scan in the exclude preview path
        names = list_party_matches(needle, limit=8, fuzzy=False)
        for name in names:
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            matched.append(
                {
                    "party": name,
                    "client_type": None,
                    "city": None,
                    "needle": needle,
                    "score": None,
                }
            )
        # No LIKE hits — still show the spoken needle so the user sees intent
        if not names and needle.lower() not in seen:
            seen.add(needle.lower())
            matched.append(
                {
                    "party": needle,
                    "client_type": None,
                    "city": None,
                    "needle": needle,
                    "score": None,
                }
            )

    if not matched:
        joined = ", ".join(f"**{n}**" for n in clean)
        return (
            f"### Clients identified for {polarity}\n\n"
            f"No customers matched {joined} in clients/sales. "
            f"The main table below is unchanged by this {polarity}.\n\n---\n\n"
        )

    period_info = resolve_period(
        period_phrase, date_from=date_from, date_to=date_to
    )
    d0 = period_info.get("date_from") if period_info.get("ok") is not False else None
    d1 = period_info.get("date_to") if period_info.get("ok") is not False else None

    # Volume for matched parties under current scope (before exclude).
    # Use LIKE on needles so multi-branch families (al shaheer…) stay fast.
    vol_by: dict[str, float] = {}
    if d0 and d1:
        try:
            from eva_dashboard.db import connect, init_db

            init_db()
            like_bits = " OR ".join(
                ["lower(s.party) LIKE ?" for _ in clean]
            )
            params: list[Any] = [d0, d1, *[f"%{n.lower()}%" for n in clean]]
            where = [
                "s.date >= ?",
                "s.date <= ?",
                f"({like_bits})",
            ]
            city = filters.get("city")
            if city:
                where.append(
                    "lower(trim(COALESCE(cl.city_filter, ''))) = lower(trim(?))"
                )
                params.append(city)
            units = [
                u
                for u in (
                    list(bus or [])
                    or list(filters.get("business_units") or [])
                    or (
                        [filters["business_unit"]]
                        if filters.get("business_unit")
                        else []
                    )
                )
                if u
            ]
            if len(units) == 1:
                where.append(
                    "lower(trim(COALESCE(c.category_1, ''))) = lower(trim(?))"
                )
                params.append(units[0])
            elif len(units) > 1:
                ph = ",".join("?" for _ in units)
                where.append(
                    f"lower(trim(COALESCE(c.category_1, ''))) IN ({ph})"
                )
                params.extend(u.lower().strip() for u in units)
            sql = f"""
                SELECT s.party,
                       ROUND(SUM(CASE WHEN COALESCE(s.mt_qty,0)<>0
                         THEN s.mt_qty ELSE 0 END), 3) AS mt
                FROM sales s
                LEFT JOIN clients cl
                  ON lower(trim(cl.client)) = lower(trim(s.party))
                LEFT JOIN category c
                  ON lower(trim(c.product)) = lower(trim(s.product))
                WHERE {' AND '.join(where)}
                GROUP BY s.party
                ORDER BY mt DESC
                LIMIT 25
            """
            with connect() as conn:
                found_rows = conn.execute(sql, params).fetchall()
            if found_rows:
                matched = []
                seen = set()
                for row in found_rows:
                    name = str(row["party"] or "").strip()
                    if not name or name.lower() in seen:
                        continue
                    seen.add(name.lower())
                    vol_by[name.lower()] = float(row["mt"] or 0)
                    matched.append(
                        {
                            "party": name,
                            "client_type": None,
                            "city": None,
                            "needle": clean[0],
                            "score": None,
                        }
                    )
        except Exception:  # noqa: BLE001
            vol_by = {}

    verb = "excluded" if polarity == "exclude" else "included"
    title = (
        "### Clients identified — these sales were excluded\n"
        if polarity == "exclude"
        else "### Clients identified — these sales were included\n"
    )
    period_lbl = period_info.get("label") or period_phrase or "selected period"
    lines = [
        title,
        f"_Matched for **{' / '.join(clean)}** in {period_lbl}:_\n",
        "| Party | Client Type | City | Volume in scope (MT) |",
        "| --- | --- | --- | --- |",
    ]
    for m in matched:
        mt = vol_by.get(m["party"].lower())
        mt_txt = f"{mt:,.3f}" if mt is not None else "—"
        lines.append(
            "| {party} | {ctype} | {city} | {mt} |".format(
                party=str(m["party"]).replace("|", "/"),
                ctype=str(m.get("client_type") or "—").replace("|", "/"),
                city=str(m.get("city") or "—").replace("|", "/"),
                mt=mt_txt,
            )
        )
    lines.append("")
    lines.append(
        f"_These customers are **{verb}** from the table below._\n\n---\n"
    )
    return "\n".join(lines) + "\n"


def _apply_extracted_entities(
    spec: dict[str, Any],
    *,
    user_text: str = "",
) -> dict[str, Any]:
    """Merge Python-resolved extracted_entities into filters / business_units.

    Channel aliases (metro, LMT, chase up, …) → client_type.
    Remaining unresolved names → silent party ILIKE (e.g. \"al shaheer\"),
    unless the user asked to *exclude* that name.
    """
    out = dict(spec)
    resolved = resolve_extracted_entities(list(out.get("extracted_entities") or []))
    filters = dict(out.get("filters") or {})
    bus = list(out.get("business_units") or filters.get("business_units") or [])
    exclude_needles = [
        _norm_key(p) for p in _spoken_exclude_phrases(user_text) if _norm_key(p)
    ]

    for b in resolved.get("business_units") or []:
        if b not in bus:
            bus.append(b)
    if resolved.get("business_unit") and not bus:
        bus = [resolved["business_unit"]]
    for key in ("oil_type", "packing_category", "city", "zone", "client_type"):
        if resolved.get(key) and not filters.get(key):
            filters[key] = resolved[key]

    # Unresolved → channel alias first, else party candidates
    unresolved = [
        str(u).strip()
        for u in (resolved.get("unresolved") or [])
        if str(u).strip() and len(str(u).strip()) >= 3
    ]
    party_bits: list[str] = []
    for u in unresolved:
        nu = _norm_key(u)
        if any(n in nu or nu in n for n in exclude_needles):
            continue  # "exclude al shaheer" must not become filters.party
        ct = match_client_type_alias(u)
        if ct and not filters.get("client_type"):
            filters["client_type"] = ct
        elif ct:
            continue  # already have a channel; don't also party-search it
        else:
            party_bits.append(u)
    if party_bits and not filters.get("party") and not filters.get("parties"):
        if len(party_bits) == 1:
            filters.setdefault("party", party_bits[0])
        else:
            filters["parties"] = party_bits

    if bus:
        out["business_units"] = bus
        filters["business_units"] = bus
        if len(bus) == 1:
            filters.setdefault("business_unit", bus[0])
    out["filters"] = filters
    out["_entity_resolution"] = resolved
    # Planner may already have put the excluded name in filters.party — strip now
    if exclude_needles:
        spoken_ex = _resolve_spoken_excludes(user_text)
        out = _strip_conflicting_party_includes(out, spoken_ex or {"party_like": exclude_needles})
    return out


def _is_factor_only_ask(
    user_text: str,
    *,
    metrics: list[str] | None = None,
    flags: dict[str, Any] | None = None,
) -> bool:
    """True when the ask is cost-factor lookup (not sales Price Fetch / rate)."""
    t = (user_text or "").lower()
    if not t.strip():
        return False
    if re.search(
        r"\b(price\s*fetch|avg\.?\s*rate|average\s+rate|average\s+price|"
        r"selling\s+price|\brate\b)\b",
        t,
    ):
        return False
    if re.search(
        r"\b("
        r"cost\s*factors?|factor\s*costs?|total\s*factor|"
        r"current\s+cost\s*factors?|current\s+factors?|"
        r"packing\s*costs?|product\s*costs?|"
        r"factor\s*break\s*down|factor\s*breakdown|"
        r"show\s+factors?|tell\s+me\s+(the\s+)?(current\s+)?(cost\s*)?factors?|"
        r"what'?s\s+the\s+factor|what\s+are\s+the\s+(cost\s*)?factors?"
        r")\b",
        t,
    ):
        return True
    flags = flags or {}
    mets = set(metrics or [])
    if flags.get("factor_only") or (
        flags.get("include_cost_factor")
        and "price_fetch" not in mets
        and "avg_price" not in mets
        and not flags.get("include_price_fetch")
    ):
        return True
    return False


_WISE_PATTERNS: list[tuple[str, str]] = [
    (
        r"\bskus?\b|\bsku[-\s]?wise\b|\bitems?\b|\bitem[-\s]?wise\b|"
        r"\bby\s+sku\b|\bsku\s+break|\ball\s+skus?\b",
        "product",
    ),
    (
        r"\bproduct[-\s]?wise\b|\bthis\s+product\s+wise\b|\bby\s+products?\b|"
        r"\bproducts?\s+(break|breakup|mix|layer)\b",
        "packing_category",
    ),
    (
        r"\b("
        r"city[- ]?wise|citywide|city\s+wide|by\s+city|"
        r"cities\s+wise|show\s+(me\s+)?(this\s+)?(by\s+)?city|"
        r"add(ing)?\s+(a\s+)?cities|add(ing)?\s+(a\s+)?city|"
        r"cities?\s+break(?:up|down)?|sales\s+by\s+city"
        r")\b",
        "city",
    ),
    (
        r"\b(zone[- ]?wise|by\s+zone|zones?\s+wise|region[- ]?wise|by\s+region)\b",
        "zone",
    ),
    (
        r"\b("
        r"client[- ]?type[- ]?wise|channel[- ]?wise|by\s+client\s*types?|"
        r"by\s+client\s+type|by\s+channels?|all\s+channels?|for\s+all\s+channels?|"
        r"show\s+(me\s+)?(this\s+)?by\s+channels?|"
        r"sales\s+by\s+channels?|group\s+by\s+channels?|"
        r"channel\s+break(?:up|down)?"
        r")\b",
        "client_type",
    ),
    (
        r"\b(bu[- ]?wise|business[- ]?unit[- ]?wise|by\s+business\s*units?)\b",
        "business_unit",
    ),
    (
        r"\b(packing[- ]?wise|by\s+packing|pack[- ]?wise)\b",
        "packing_category",
    ),
    (
        r"\b(party[- ]?wise|distributor[- ]?wise|customer[- ]?wise|"
        r"by\s+part(y|ies)|by\s+distributors?|by\s+customers?|"
        r"customer\s+break(?:up|down)?|party\s+break(?:up|down)?|"
        r"break(?:up|down)?\s+customer[- ]?wise|"
        r"break(?:up|down)?\s+(by\s+)?customers?|"
        r"main\s+customers?|who\s+(were|are|bought)|"
        r"which\s+customers?|top\s+customers?|key\s+customers?"
        r")\b",
        "party",
    ),
]


def _spoken_wise_dimensions(user_text: str) -> list[str]:
    """All spoken X-wise grains, ordered outer → leaf when both are present.

    So "all SKUs … for all channels" → ``['client_type', 'product']``, not SKU alone.
    """
    t = (user_text or "").lower()
    if not t.strip():
        return []
    found: list[str] = []
    for pat, dim in _WISE_PATTERNS:
        if re.search(pat, t) and dim not in found:
            found.append(dim)
    if not found:
        return []
    outer_order = ("city", "zone", "client_type", "party", "business_unit")
    outers = [d for d in outer_order if d in found]
    # SKU beats packing when both spoken
    leaf: str | None = None
    if "product" in found:
        leaf = "product"
    elif "packing_category" in found:
        leaf = "packing_category"
    elif "oil_type" in found:
        leaf = "oil_type"
    if outers and leaf:
        return outers + [leaf]
    if leaf and not outers:
        return [leaf]
    if outers:
        return outers
    return found


def _spoken_wise_dimension(user_text: str) -> str | None:
    """Primary spoken wise grain (first of multi-wise, leaf preferred when nested)."""
    dims = _spoken_wise_dimensions(user_text)
    if not dims:
        return None
    # Prefer leaf when multi-wise so existing has_sku / nest-leaf checks still fire
    for leaf in ("product", "packing_category", "oil_type"):
        if leaf in dims:
            return leaf
    return dims[0]


def _looks_drop_party_grain(user_text: str) -> bool:
    """True when the user wants customers off the table (grain), not channel exclude.

    Examples: 'remove the distributor layer', 'don't show customers',
    'overall by business unit I don't need customer names',
    'include distributors but don't show customers in the table'.
    """
    t = (user_text or "").lower()
    if not t.strip():
        return False
    return bool(
        re.search(
            r"\b("
            r"remove\s+(the\s+)?(distributor|customer|party)\s+layer|"
            r"drop\s+(the\s+)?(distributor|customer|party)\s+layer|"
            r"don'?t\s+(need|show|want)\s+(the\s+)?(individual\s+)?"
            r"(customer|party|distributor)\s*names?|"
            r"don'?t\s+show\s+(the\s+)?(customers?|parties|distributors?)"
            r"(\s+in\s+(the\s+)?table)?|"
            r"without\s+(showing\s+)?(individual\s+)?"
            r"(customer|party|distributor)\s+names?|"
            r"no\s+(customer|party|distributor)\s+(names?|column|layer|break)|"
            r"overall\s+by\s+business\s+unit|"
            r"include\s+distributors?\s+but\s+don'?t\s+show\s+customers?"
            r")\b",
            t,
        )
    )


def _looks_yoy_compare(user_text: str) -> bool:
    from eva_dashboard.metric_filters import looks_yoy_period_compare

    t = (user_text or "").lower()
    if looks_yoy_period_compare(user_text):
        return True
    return bool(
        # "July 2025 vs 2026" / "2025 vs 2026" same-month year compare
        re.search(
            r"\b(?:"
            r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
            r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
            r"nov(?:ember)?|dec(?:ember)?"
            r")\s+20\d{2}\s+(?:vs\.?|versus|compared?\s+to)\s+20\d{2}\b",
            t,
        )
        or re.search(
            r"\b20\d{2}\s+(?:vs\.?|versus)\s+20\d{2}\b",
            t,
        )
    )


def _looks_main_customers_ask(user_text: str) -> bool:
    """Follow-ups that want a customer list of the prior scope."""
    t = (user_text or "").lower()
    return bool(
        re.search(
            r"\b("
            r"main\s+customers?|key\s+customers?|top\s+customers?|"
            r"who\s+(were|are)\s+the\s+(main\s+)?customers?|"
            r"who\s+bought|which\s+customers?|"
            r"customer\s+wise|customer[- ]?wise|by\s+customers?"
            r")\b",
            t,
        )
    )


def _extract_top_n(user_text: str) -> int | None:
    t = (user_text or "").lower()
    m = re.search(r"\btop\s+(\d{1,3})\b", t)
    if not m:
        # "the 10 distributors with the highest…" / "show 5 parties"
        m = re.search(
            r"\b(?:the\s+)?(\d{1,3})\s+"
            r"(?:distributors?|parties|customers?|clients?|stores?)\b",
            t,
        )
    if not m:
        return None
    return max(1, min(200, int(m.group(1))))


def _looks_growth_drivers(user_text: str) -> bool:
    t = (user_text or "").lower()
    return bool(
        re.search(
            r"\b("
            r"(products?|skus?|packings?|items?)\s+led\s+(the\s+)?growth|"
            r"led\s+the\s+growth|what\s+drove|growth\s+drivers?|"
            r"which\s+(products?|skus?|packings?)\s+(drove|led|grew)"
            r")\b",
            t,
        )
    )


def _coerce_vocab_from_user_text(
    spec: dict[str, Any],
    user_text: str,
    *,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Hard safety nets for spoken grain, multi-value compares, and layer follow-ups.

    General rules (any filter / grain, not just Imtiaz/Lahore):
    - Named 2+ cities → filters.cities + row_dimensions include city
    - Named 2+ channels in a compare → filters.client_types + row client_type
    - "X wise" follow-up nests under prior outer grain (city/zone/channel/…)
    - SKU / product spoken vocab unchanged
    """
    out = dict(spec)
    t = (user_text or "").lower()
    if not t.strip():
        return out

    rows = list(out.get("row_dimensions") or [])
    metrics = list(out.get("metrics") or [])
    cols = list(out.get("column_dimensions") or [])
    filters = dict(out.get("filters") or {})
    clear = list(out.get("clear_filters") or [])

    prior_rows: list[str] = []
    if prior:
        prior_rows = list(prior.get("row_dimensions") or [])
        if not prior_rows:
            g = prior.get("grain") or {}
            leaf = g.get("row_dimension") or prior.get("row_dimension")
            groups = list(g.get("row_groups") or prior.get("row_groups") or [])
            if leaf:
                prior_rows = list(groups) + [str(leaf)]

    wise_dims = _spoken_wise_dimensions(t)
    wise_dim = _spoken_wise_dimension(t)
    has_sku = "product" in wise_dims
    has_product_spoken = "packing_category" in wise_dims and bool(
        re.search(
            r"\bproduct[-\s]?wise\b|\bby\s+products?\b|"
            r"\bproducts?\s+(break|breakup|mix|layer)\b|"
            r"\bthis\s+product\s+wise\b",
            t,
        )
    )
    nest_leaf_dims = {"packing_category", "product", "business_unit", "oil_type"}
    outer_dims = {"city", "zone", "client_type", "party", "business_unit"}

    # Fresh complete ask / explicit clear: do not soft-stick prior city/filters
    state_action = str(out.get("state_action") or "").strip().lower()
    explicit_clear = state_action == "clear"
    fresh_complete = False
    try:
        from eva_dashboard.chatbot import _looks_complete_sales_ask

        fresh_complete = bool(_looks_complete_sales_ask(user_text))
    except Exception:  # noqa: BLE001
        fresh_complete = False
    named_cities_early = extract_cities_from_text(user_text)
    from eva_dashboard.spoken_constraints import looks_optional_city_scope

    optional_city = looks_optional_city_scope(user_text)
    # Sticky city must drop on fresh asks that never named a city, or when the
    # user says the city lock is optional ("doesn't have to be Lahore").
    if (fresh_complete or explicit_clear or optional_city) and not named_cities_early:
        filters.pop("city", None)
        filters.pop("cities", None)
        if "city" not in clear:
            clear.append("city")
        if "cities" not in clear:
            clear.append("cities")

    # --- Drop party/customer grain (not channel exclude) ---
    # "remove distributor layer" / "don't show customers" / "overall by BU"
    if _looks_drop_party_grain(user_text):
        base_rows = list(prior_rows) if prior_rows else list(rows)
        rows = [r for r in base_rows if r != "party"]
        if not rows:
            rows = ["business_unit"]
        # Do not turn this into Eva Distributors exclude
        if prior:
            if out.get("base") != "prior":
                out["base"] = "prior"
                out["state_action"] = out.get("state_action") or "modify"
            out["_clear_omitted"] = False
            # Inherit metrics/cols from prior when omitted
            if not metrics and prior.get("metrics"):
                metrics = list(prior.get("metrics") or [])
            prior_cols = list(
                prior.get("column_dimensions")
                or (
                    [prior.get("column_dimension")]
                    if prior.get("column_dimension")
                    else []
                )
            )
            if not cols and prior_cols:
                cols = [c for c in prior_cols if c]

    # --- Explicit "remove the city / client type filter" → clear_filters ---
    from eva_dashboard.spoken_constraints import extract_clear_filter_keys

    clear_keys = extract_clear_filter_keys(user_text)
    if clear_keys:
        for key in clear_keys:
            if key == "business_units":
                out["business_units"] = []
                filters.pop("business_unit", None)
                filters.pop("business_units", None)
            elif key == "party":
                for pk in ("party", "parties", "party_ilike"):
                    filters.pop(pk, None)
            else:
                filters.pop(key, None)
                if key == "city":
                    filters.pop("cities", None)
                if key == "client_type":
                    filters.pop("client_types", None)
            if key not in clear:
                clear.append(key)
        if prior and out.get("base") != "prior":
            out["base"] = "prior"
            out["state_action"] = out.get("state_action") or "modify"
        out["_clear_omitted"] = False

    # --- Ordinal picks from prior who-is matches ("first 2", "#1 and #2") ---
    from eva_dashboard.ordinal_parties import (
        looks_ordinal_party_followup,
        resolve_ordinal_party_names,
    )

    ordinal_followup = bool(prior and looks_ordinal_party_followup(user_text))
    if ordinal_followup:
        picked = resolve_ordinal_party_names(user_text, prior)
        if not picked and re.search(r"\bboth\b", t):
            # "both" / "those matches" → first two when available
            from eva_dashboard.ordinal_parties import matches_from_prior

            ms = matches_from_prior(prior)
            picked = [
                str(m.get("client") or m.get("party") or "").strip()
                for m in ms[:2]
                if str(m.get("client") or m.get("party") or "").strip()
            ]
        if picked:
            filters.pop("party", None)
            filters.pop("party_ilike", None)
            if len(picked) == 1:
                filters["party"] = picked[0]
                filters.pop("parties", None)
            else:
                filters["parties"] = picked
            rows = ["party"]
            # Who-is follow-ups must not keep unrelated sticky city/channel
            # from a previous Imtiaz/price turn unless the user restated them.
            spoken_city = extract_cities_from_text(user_text)
            spoken_ch = extract_all_client_types_from_text(user_text)
            if not spoken_city:
                filters.pop("city", None)
                filters.pop("cities", None)
                if "city" not in clear:
                    clear.append("city")
            if not spoken_ch:
                filters.pop("client_type", None)
                filters.pop("client_types", None)
                if "client_type" not in clear:
                    clear.append("client_type")
            out["base"] = "prior"
            out["state_action"] = "modify"
            out["_clear_omitted"] = False
            # Ordinal picks must not be wiped by mis-parsed "remove … filter" excludes
            out["excludes"] = {}
            if not metrics:
                metrics = ["volume", "ams"]
            if "month" not in cols:
                cols = ["month"]

    # --- Multi-wise in one ask: "all SKUs … for all channels" → Channel × SKU ---
    # Do this before single-leaf nest logic so a mistaken party×SKU plan cannot win.
    spoken_outers = [
        d for d in ("city", "zone", "client_type") if d in wise_dims
    ]
    spoken_leaf = (
        "product"
        if "product" in wise_dims
        else ("packing_category" if has_product_spoken else None)
    )
    if spoken_outers and spoken_leaf:
        rows = spoken_outers + [spoken_leaf]
        # Drop party unless the user also asked party-wise
        if "party" not in wise_dims:
            rows = [r for r in rows if r != "party"]
        if "client_type" in spoken_outers and not filters.get("client_types"):
            filters.pop("client_type", None)
            if "client_type" not in clear:
                clear.append("client_type")
        if "city" in spoken_outers and not filters.get("cities"):
            # city-wise across cities only when they asked city grain, not when
            # city is merely a sticky filter from prior context.
            if re.search(
                r"\b(city[- ]?wise|by\s+city|all\s+cities|cities?\s+break)",
                t,
            ):
                filters.pop("city", None)
                if "city" not in clear:
                    clear.append("city")

    # --- Multi-city named set (lahore vs/and karachi, …) ---
    named_cities = extract_cities_from_text(user_text)
    if len(named_cities) >= 2:
        filters["cities"] = named_cities
        filters.pop("city", None)
        if "city" in clear:
            clear = [c for c in clear if c != "city"]
        # Ensure city is the (outer) row grain unless user asked a different wise cut
        if wise_dim not in nest_leaf_dims and wise_dim not in {"zone", "client_type", "party", "business_unit"}:
            if "city" not in rows:
                rows = ["city"]
            elif rows[0] != "city":
                rows = ["city"] + [r for r in rows if r != "city"]
        vol_metrics = set(metrics) & {"volume", "ams", "vs_ams", "ams_growth"}
        price_metrics = set(metrics) & {"price_fetch", "avg_price"}
        if (vol_metrics or not metrics) and not price_metrics and "month" not in cols:
            cols = ["month"]
            metrics = metrics or ["volume", "ams"]

    # --- Multi-channel compare (Imtiaz vs distributors, …) ---
    named_channels = extract_all_client_types_from_text(user_text)
    compareish = bool(
        re.search(r"\b(compare|comparison|versus|vs\.?|against)\b", t)
        or re.search(r"\bsales\b.*\band\b.*\bsales\b", t)
        or (" and " in t and "sales" in t and len(named_channels) >= 2)
    )
    if len(named_channels) >= 2 and compareish:
        filters["client_types"] = named_channels
        filters.pop("client_type", None)
        if "client_type" in clear:
            clear = [c for c in clear if c != "client_type"]
        # Channel compare grain unless user explicitly asked city/party/product wise
        if wise_dim not in {"city", "zone", "party", "product", "packing_category"}:
            rows = ["client_type"]
        vol_metrics = set(metrics) & {"volume", "ams", "vs_ams", "ams_growth"}
        price_metrics = set(metrics) & {"price_fetch", "avg_price"}
        if (vol_metrics or not metrics) and not price_metrics and "month" not in cols:
            cols = ["month"]
            metrics = metrics or ["volume", "ams"]
    # Do NOT invent a single city/client_type from user_text — the planner must
    # set filters (blind execute). Multi-city / multi-channel compare above is
    # the only filter injection allowed from spoken text.

    # --- Compare with <City> on a prior single-city table ---
    if prior and len(named_cities) == 1:
        pf = dict(prior.get("filters") or {})
        prior_city = pf.get("city")
        prior_cities = [str(c) for c in (pf.get("cities") or []) if c]
        compare_city = bool(
            re.search(
                r"\b(compare|versus|vs\.?)\s+(with|to|against)?\b|"
                r"\b(and|also|plus)\s+(with\s+)?",
                t,
            )
            or re.search(r"\bcompare\s+with\b", t)
        )
        if compare_city and (prior_city or prior_cities):
            base = list(prior_cities) if prior_cities else [str(prior_city)]
            extra = named_cities[0]
            if extra not in base:
                filters["cities"] = base + [extra]
                filters.pop("city", None)
                if "city" in clear:
                    clear = [c for c in clear if c != "city"]
                prior_nestables = [r for r in prior_rows if r in nest_leaf_dims]
                if prior_nestables:
                    rows = ["city"] + prior_nestables
                elif wise_dim not in nest_leaf_dims:
                    rows = ["city"]
                vol_metrics = set(metrics) & {"volume", "ams", "vs_ams", "ams_growth"}
                price_metrics = set(metrics) & {"price_fetch", "avg_price"}
                if (vol_metrics or not metrics) and not price_metrics and "month" not in cols:
                    cols = ["month"]
                    metrics = metrics or ["volume", "ams"]
                if out.get("base") != "prior":
                    out["base"] = "prior"

    # --- Spoken wise / layer follow-ups ---
    add_layer = bool(re.search(r"\b(add|nest|layer|under)\b", t))
    if has_sku or has_product_spoken or wise_dim in nest_leaf_dims | outer_dims:
        leaf = (
            "product"
            if has_sku
            else (
                "packing_category"
                if has_product_spoken or wise_dim == "packing_category"
                else wise_dim
            )
        )
        # Nest under prior outer grain when follow-up only changes the leaf
        prior_outers = [r for r in prior_rows if r in outer_dims and r != leaf]
        current_outers = [r for r in rows if r in outer_dims and r != leaf]
        # Also keep multi-city / multi-channel outer if just set above
        if "city" in rows and leaf != "city":
            current_outers = ["city"] + [r for r in current_outers if r != "city"]
        if "client_type" in rows and leaf != "client_type":
            current_outers = ["client_type"] + [
                r for r in current_outers if r != "client_type"
            ]

        if leaf in nest_leaf_dims and (prior_outers or current_outers or "party" in wise_dims):
            # Spoken customer/party-wise wins as outer over a sticky prior BU
            if "party" in wise_dims and leaf != "party":
                outers = ["party"]
            elif spoken_outers:
                # "by client type for all SKU" must keep channel outer, not prior BU
                outers = list(spoken_outers)
            elif has_product_spoken and leaf == "packing_category":
                # product-wise replaces BU grain; prior BUs stay as filters
                outers = [
                    r for r in (prior_outers or current_outers) if r != "business_unit"
                ]
            else:
                outers = prior_outers or current_outers
            # If a dim is already a sticky singleton filter (party from profile,
            # single city/channel lock), keep it as a filter — do not also nest
            # it as an outer row grain (SKU-wise under one customer → product only).
            scoped = dict(filters)
            if prior and not explicit_clear and not fresh_complete:
                for k, v in dict(prior.get("filters") or {}).items():
                    if v not in (None, "", []) and k not in scoped:
                        scoped[k] = v
                for k, v in dict(prior.get("party_scope") or {}).items():
                    if v not in (None, "", []) and k not in scoped:
                        scoped[k] = v

            def _singleton_scoped(dim: str) -> bool:
                # Explicit multi-wise ("for all channels" + SKUs) must keep the outer
                if dim in wise_dims and dim in {"city", "zone", "client_type"}:
                    return False
                if dim == "party":
                    return bool(
                        scoped.get("party")
                        or scoped.get("party_ilike")
                        or (
                            isinstance(scoped.get("parties"), list)
                            and len(scoped.get("parties") or []) == 1
                        )
                    )
                if dim == "city":
                    return bool(scoped.get("city")) and not scoped.get("cities")
                if dim == "client_type":
                    return bool(scoped.get("client_type")) and not scoped.get(
                        "client_types"
                    )
                if dim == "zone":
                    return bool(scoped.get("zone"))
                return False

            outers = [r for r in outers if not _singleton_scoped(r)]
            # Preserve order, unique
            seen: set[str] = set()
            outers_u = []
            for r in outers:
                if r not in seen and r != leaf:
                    seen.add(r)
                    outers_u.append(r)
            rows = (outers_u + [leaf]) if outers_u else [leaf]
            # Keep multi-filters that scoped the prior table — but never re-lock a
            # grain the user just asked to expand (all channels / city-wise).
            # Fresh/clear asks must not re-stick prior city/zone/channel.
            if prior and not explicit_clear and not fresh_complete:
                pf = dict(prior.get("filters") or {})
                for key in ("cities", "client_types", "city", "client_type", "zone"):
                    if not pf.get(key) or filters.get(key):
                        continue
                    if key in {"city", "cities"} and "city" in clear:
                        continue
                    grain = (
                        "client_type"
                        if key.startswith("client_type")
                        else ("city" if key.startswith("city") else key)
                    )
                    if grain in wise_dims and grain in clear:
                        continue
                    if grain in wise_dims and grain in {
                        "client_type",
                        "city",
                        "zone",
                    }:
                        continue
                    filters[key] = pf[key]
        elif leaf in outer_dims:
            prior_nestables = [r for r in prior_rows if r in nest_leaf_dims]
            current_nestables = [r for r in rows if r in nest_leaf_dims]
            flat_only = bool(re.search(r"\b(only|just|flat)\b", t))
            # Nest only from PRIOR leaf hierarchy (or explicit add/nest/layer).
            # Fresh "city wise" with a mistaken BU plan must stay a flat city cut.
            nestables = prior_nestables
            if add_layer and not nestables:
                nestables = current_nestables
            # "add city" / "show by city" / "by channel" on a prior BU|packing|SKU
            # table → outer grain + keep nestable leaf (city | business_unit …)
            if (
                leaf in {"city", "zone", "client_type"}
                and nestables
                and not flat_only
            ):
                seen_n: set[str] = set()
                nest_u: list[str] = []
                for r in nestables:
                    if r not in seen_n and r != leaf:
                        seen_n.add(r)
                        nest_u.append(r)
                rows = [leaf] + nest_u
            elif add_layer and (rows or nestables):
                base = [r for r in (rows or nestables) if r != leaf]
                rows = [leaf] + base
            else:
                # Fresh cut on city/zone/channel — don't bury under packing
                rows = [leaf]
            if leaf == "city":
                # city-wise across all cities: drop single city lock unless multi named
                if not filters.get("cities"):
                    filters.pop("city", None)
                if "city" not in clear:
                    clear.append("city")
            if leaf == "zone":
                filters.pop("zone", None)
                if "zone" not in clear:
                    clear.append("zone")
            if leaf == "client_type" and not filters.get("client_types"):
                # channel-wise across channels
                filters.pop("client_type", None)
                if "client_type" not in clear:
                    clear.append("client_type")
        else:
            # leaf packing/product/bu without prior outer
            if leaf == "product":
                rows = ["product" if r == "packing_category" else r for r in rows]
                if "product" not in rows:
                    rows.append("product")
            elif leaf == "packing_category":
                rows = ["packing_category" if r == "product" else r for r in rows]
                if "packing_category" not in rows:
                    rows.append("packing_category")
            elif leaf and leaf not in rows:
                rows.append(leaf)

        if has_sku and (
            "price_fetch" in metrics or re.search(r"price\s*fetch|cost\s*factor", t)
        ):
            cols = [c for c in cols if c != "month"]
        elif leaf in outer_dims or leaf in nest_leaf_dims:
            vol_metrics = set(metrics) & {"volume", "ams", "vs_ams", "ams_growth"}
            price_metrics = set(metrics) & {"price_fetch", "avg_price"}
            if (vol_metrics or not metrics) and not price_metrics and "month" not in cols:
                cols = ["month"]
                metrics = metrics or ["volume", "ams"]

        # Grain-only follow-ups (customer-wise / city-wise / …) must keep prior
        # filters (city, Eva BUs, …) — promote base=prior when prior exists.
        # Never override an explicit clear / fresh complete ask.
        if (
            prior
            and leaf
            and out.get("base") != "prior"
            and not explicit_clear
            and not fresh_complete
        ):
            out["base"] = "prior"
            out["state_action"] = out.get("state_action") or "modify"
            if out.get("_clear_omitted"):
                out["_clear_omitted"] = False
                out["clear"] = list(out.get("clear") or clear or [])

        # Product-wise on a prior monthly grid → keep month columns + period
        if prior and has_product_spoken and not has_sku:
            prior_cols = list(
                prior.get("column_dimensions")
                or (
                    [prior.get("column_dimension")]
                    if prior.get("column_dimension")
                    else []
                )
            )
            prior_had_month = "month" in prior_cols or prior.get(
                "column_dimension"
            ) == "month"
            if prior_had_month:
                cols = ["month"]
                prior_pt = str(prior.get("period_type") or "")
                prior_mb = prior.get("months_back")
                if prior_pt == "LAST_N_MONTHS" or prior_mb:
                    out["period_type"] = "LAST_N_MONTHS"
                    out["months_back"] = int(prior_mb or 6)
                    out.pop("target_month", None)
                    period = dict(out.get("period") or {})
                    period["phrase"] = f"last {out['months_back']} months"
                    out["period"] = period
                if not re.search(
                    r"\b(price\s*fetch|avg\.?\s*rate|average\s+(price|rate)|"
                    r"cost\s*factor)\b",
                    t,
                ):
                    prior_mets = list(prior.get("metrics") or [])
                    if prior_mets:
                        metrics = prior_mets
                    elif not (set(metrics) & {"volume", "ams"}):
                        metrics = ["volume", "ams"]
            if prior and out.get("base") != "prior" and not explicit_clear:
                out["base"] = "prior"
                out["state_action"] = out.get("state_action") or "modify"
                out["_clear_omitted"] = False

    # Pure cost-factor asks stay on factor_costs (do not force Price Fetch metric)
    if not _is_factor_only_ask(t, metrics=metrics, flags=out.get("price_flags")) and re.search(
        r"price\s*fetch|oil\s*price\s*fetched|apply\s+the\s+cost\s+factor|"
        r"what.?s\s+the\s+cost\s+factor|cost\s+factor",
        t,
    ):
        if "price_fetch" not in metrics:
            metrics.append("price_fetch")
        if has_sku or "product" in rows or "party" in rows:
            cols = [c for c in cols if c != "month"]

    # Channel words → client_type (never customer ILIKE)
    if not filters.get("client_type") and not filters.get("client_types"):
        party_raw = filters.get("party") or out.get("party_query")
        parties = list(filters.get("parties") or [])
        if party_raw:
            ct = match_client_type_alias(str(party_raw))
            if ct:
                filters["client_type"] = ct
                filters.pop("party", None)
                out.pop("party_query", None)
        if parties:
            kept: list[str] = []
            for p in parties:
                ct = match_client_type_alias(str(p))
                if ct and not filters.get("client_type") and not filters.get("client_types"):
                    filters["client_type"] = ct
                elif not ct:
                    kept.append(str(p))
            if kept:
                filters["parties"] = kept
            else:
                filters.pop("parties", None)

    # --- YoY / same period last year / "July 2025 vs 2026" ---
    if _looks_yoy_compare(t) and not out.get("compare"):
        out["compare"] = "yoy"
        if prior and out.get("base") != "prior":
            out["base"] = "prior"
        # Prefer the later year as the current SPECIFIC_MONTH
        years = [int(y) for y in re.findall(r"\b(20\d{2})\b", t)]
        if len(years) >= 2:
            later = max(years)
            # Keep spoken month if present
            name_to_num = {
                "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3,
                "march": 3, "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
                "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
                "september": 9, "oct": 10, "october": 10, "nov": 11,
                "november": 11, "dec": 12, "december": 12,
            }
            mm = re.search(
                r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
                r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
                r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
                t,
            )
            if mm:
                num = name_to_num.get(mm.group(1).lower())
                if num:
                    out["period_type"] = "SPECIFIC_MONTH"
                    out["target_month"] = f"{later:04d}-{num:02d}"
                    period = dict(out.get("period") or {})
                    period["phrase"] = out["target_month"]
                    out["period"] = period
                    cols = [c for c in cols if c != "month"]
        # Distributor channel on YoY year compares
        if re.search(r"\bdistributors?\b", t) and not filters.get("client_type"):
            ct = extract_client_type_from_text(user_text) or "Eva Distributors"
            filters["client_type"] = ct
        # Channel is a FILTER on year-vs-year — don't also make it the only row grain
        if filters.get("client_type") and rows == ["client_type"]:
            rows = ["business_unit"]
            cols = []

    # --- Main customers / customer-wise of prior oil|BU|month scope ---
    # Flat Customer × Volume + Avg Price (never party × client_type).
    # Preserve nests like customer-wise × packing-wise.
    nest_leafs = {"packing_category", "product", "oil_type"} & set(wise_dims)
    main_customers = _looks_main_customers_ask(user_text)
    if (main_customers or "party" in wise_dims) and prior and not nest_leafs:
        prior_mets = list(prior.get("metrics") or [])
        prior_filters = dict(prior.get("filters") or {})
        has_price_prior = bool(
            set(prior_mets) & {"avg_price", "price_fetch"}
            or prior_filters.get("oil_type")
            or re.search(r"\b(price|rate)\b", t)
        )
        rows = ["party"]
        cols = []
        if has_price_prior or "avg_price" in metrics or "price_fetch" in metrics:
            if "volume" not in metrics:
                metrics = ["volume"] + [m for m in metrics if m != "volume"]
            if "avg_price" not in metrics:
                metrics = [m for m in metrics if m != "price_fetch"] + ["avg_price"]
            # Drop channel crosstab leftovers
            metrics = [m for m in metrics if m in {"volume", "avg_price", "ams"}]
            if "volume" not in metrics:
                metrics.insert(0, "volume")
            if "avg_price" not in metrics:
                metrics.append("avg_price")
        elif not metrics:
            metrics = ["volume", "ams"]
        # Inherit prior filters (oil / BU / city / period) via base=prior
        if out.get("base") != "prior":
            out["base"] = "prior"
            out["state_action"] = out.get("state_action") or "modify"
        out["_clear_omitted"] = False
        out["context_handling"] = "prior"
        if "clear_filters" not in out and "clear" not in out:
            out["clear_filters"] = list(clear)
            out["clear"] = list(clear)
        # Carry period from prior when this turn is just "who were the customers"
        if prior.get("period_type") and not out.get("period_type"):
            out["period_type"] = prior.get("period_type")
        if prior.get("target_month") and not out.get("target_month"):
            out["target_month"] = prior.get("target_month")
        if prior.get("months_back") and out.get("months_back") is None:
            out["months_back"] = prior.get("months_back")
        for key in (
            "oil_type",
            "packing_category",
            "city",
            "zone",
            "client_type",
            "business_unit",
            "business_units",
        ):
            if prior_filters.get(key) and not filters.get(key):
                filters[key] = prior_filters[key]
        if prior.get("business_units") and not out.get("business_units"):
            out["business_units"] = list(prior.get("business_units") or [])
    elif (main_customers or "party" in wise_dims) and prior and nest_leafs:
        # Customer × packing/SKU nest — keep prior scope, don't flatten metrics
        if out.get("base") != "prior":
            out["base"] = "prior"
            out["state_action"] = out.get("state_action") or "modify"
        out["_clear_omitted"] = False

    # Spoken top-N ("top 5 parties" / "10 distributors")
    top_n = _extract_top_n(user_text)
    if top_n and (out.get("limit") in (None, 0) or int(out.get("limit") or 0) > top_n):
        out["limit"] = top_n
    # Top-N customer asks on a named month → Volume vs AMS trend (not a month grid)
    if (
        top_n
        and re.search(r"\b(customers?|parties|distributors?|clients?)\b", t)
        and (
            out.get("period_type") in {"SPECIFIC_MONTH", "NAMED_MONTH"}
            or re.search(
                r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
                r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
                r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
                t,
            )
        )
        and "party" in (rows or ["party"])
    ):
        rows = ["party"]
        cols = [c for c in cols if c != "month"]
        if not (set(metrics) & {"volume", "ams", "vs_ams"}):
            metrics = ["volume", "ams", "vs_ams"]
        out["intent"] = out.get("intent") or "sales_trend"

    # Spoken geography zone ("from the North", "in Central")
    spoken_zone = extract_zone_from_text(user_text)
    if spoken_zone and not filters.get("zone"):
        filters["zone"] = spoken_zone

    # Bare "distributor sales" / distributor ranking without a channel filter.
    # Skip when the plan already locked a non-channel grain (e.g. BU matrix) and
    # the model intentionally left client_type empty — except clear rank asks.
    if (
        re.search(r"\bdistributors?\b", t)
        and not filters.get("client_type")
        and not filters.get("client_types")
        and "client_type" not in wise_dims
    ):
        ct = extract_client_type_from_text(user_text)
        wants_dist_rank = bool(
            re.search(
                r"\b("
                r"sales\s+by\s+distributors?|"
                r"by\s+distributor|"
                r"distributors?\s+with|"
                r"distributors?\s+whose|"
                r"which\s+distributors?|"
                r"show\s+me\s+distributors?|"
                r"distributor\s+sales|"
                r"distributors?\s+sales"
                r")\b",
                t,
            )
        )
        # Only invent the channel when ranking/listing distributors, or when
        # rows are already party-grained. Do not rewrite a BU sales matrix plan.
        if ct and (wants_dist_rank or "party" in rows or out.get("intent") == "party_rank"):
            filters["client_type"] = ct
        elif wants_dist_rank or "party" in rows or out.get("intent") == "party_rank":
            filters["client_type"] = "Eva Distributors"

    # Fresh "exclude X and show Eva …" must stay BU×month — never flip to party
    # just because a party name appeared in an exclude phrase.
    if (
        re.search(
            r"\b(remove|exclude|excluding|without|drop|hide|filter\s+out|except)\b",
            t,
        )
        and not prior
        and "party" not in wise_dims
        and not re.search(
            r"\b(customer|party|distributor)[- ]?wise\b|"
            r"\bby\s+(customers?|parties|distributors?)\b",
            t,
        )
    ):
        if rows == ["party"] or (rows and rows[-1] == "party"):
            rows = ["business_unit"]
            if "month" not in cols:
                cols = ["month"]
            if not metrics:
                metrics = ["volume", "ams"]

    # AMS / growth / YoY / volume thresholds → party rank (filters actually apply)
    from eva_dashboard.metric_filters import merge_metric_filters, parse_metric_filters

    spoken_mfs = parse_metric_filters(user_text)
    if spoken_mfs:
        out["metric_filters"] = merge_metric_filters(
            list(out.get("metric_filters") or []), spoken_mfs
        )
    mfs = list(out.get("metric_filters") or [])
    mf_ids = {str(f.get("metric") or "") for f in mfs}
    yoy_ask = _looks_yoy_compare(t) or "yoy" in mf_ids
    low_growth = bool(
        re.search(
            r"\b(least|smallest|lowest|worst|slowest)\s+"
            r"(?:the\s+)?(?:ams\s+)?(?:growth|gains?|increases?|yoy)\b|"
            r"\b(least|smallest|lowest|worst)\s+growth\s+in\s+ams\b|"
            r"\bleast\s+growth\b|"
            r"\bsmallest\s+ams\s+gains?\b",
            t,
        )
    )
    high_growth = bool(
        re.search(
            r"\b(biggest|highest|most|largest|fastest|top)\s+"
            r"(?:the\s+)?(?:ams\s+)?(?:growth|gains?|increases?|yoy)\b",
            t,
        )
    )
    if yoy_ask:
        remapped = []
        for entry in mfs:
            if str(entry.get("metric") or "") == "ams_growth":
                remapped.append({**entry, "metric": "yoy"})
            else:
                remapped.append(entry)
        mfs = merge_metric_filters([], remapped)
        out["metric_filters"] = mfs
        mf_ids = {str(f.get("metric") or "") for f in mfs}
    rank_cuts = mf_ids & {
        "ams",
        "volume",
        "yoy",
        "ams_growth",
        "vs_ams",
        "mom",
    }
    yoy_rank = yoy_ask and (
        low_growth
        or high_growth
        or bool(rank_cuts)
        or "yoy" in mf_ids
        or "party" in rows
        or bool(
            re.search(
                r"\b("
                r"(?:the\s+)?all\s+(?:the\s+)?(?:distributors?|parties|customers?|clients?)|"
                r"which\s+(?:distributors?|parties|customers?|clients?)|"
                r"(?:distributors?|parties|customers?|clients?)\s+with\b|"
                r"show\s+me\s+(?:the\s+)?(?:all\s+)?(?:distributors?|parties)|"
                r"party[- ]?wise|customer[- ]?wise|by\s+(?:party|customer|distributor)"
                r")\b",
                t,
            )
        )
    )
    if (
        rank_cuts
        and (
            re.search(r"\b(distributors?|parties|customers?|clients?)\b", t)
            or "party" in rows
            or prior
            or bool(mf_ids & {"volume", "yoy", "ams_growth", "ams"})
        )
    ) or yoy_rank:
        rows = ["party"]
        cols = [c for c in cols if c != "month"]
        out["intent"] = "party_rank"
        out["operation"] = out.get("operation") or "pivot"
        if yoy_ask or "yoy" in mf_ids:
            # Calendar YoY of the spoken window — not AMS vs prior AMS window.
            # Bare "growth in AMS" with last-year language is still calendar YoY
            # (AMS of a 3-month window grows at the same % as its volume).
            out["metric"] = "yoy"
            out["compare"] = "yoy"
            metrics = ["volume", "yoy"]
            # Size cuts like "with AMS>10" are metric_filters, not a request
            # to rank by trailing 3-month AMS (yoy_ams).
            if (
                "ams" not in mf_ids
                and re.search(
                    r"\b(and|with|plus|including)\s+ams\b|\bams\s+and\b",
                    t,
                )
                and not re.search(
                    r"growth\s+in\s+ams|ams\s+growth|ams\s*[><=]",
                    t,
                )
            ):
                out["metric"] = "yoy_ams"
                metrics = ["volume", "ams", "yoy"]
            if low_growth:
                out["sort"] = "asc"
                out["title_mode"] = "lowest"
            elif high_growth:
                out["sort"] = "desc"
        elif "ams_growth" in mf_ids or "ams_growth" in metrics:
            out["metric"] = "ams_growth"
        elif "vs_ams" in mf_ids or "vs_ams" in metrics:
            out["metric"] = "vs_ams"
        elif "volume" in mf_ids:
            out["metric"] = "volume"
        elif not out.get("metric"):
            out["metric"] = "ams"
        if not metrics:
            metrics = ["volume", "ams"]
        # Fresh complete ask with its own period — don't glue last conversation.
        if re.search(
            r"\b("
            r"last\s+\d+\s+months?|this\s+month|mtd|last\s+month|"
            r"same\s+\d+\s+months?\s+last\s+year|"
            r"same\s+months?\s+last\s+year|20\d{2}"
            r")\b",
            t,
        ):
            out["base"] = "none"
            out["state_action"] = "clear"
            out["context_handling"] = "none"
        else:
            if prior and out.get("base") != "prior":
                out["base"] = "prior"
                out["state_action"] = out.get("state_action") or "modify"
        spoken_top = _extract_top_n(user_text)
        if spoken_top:
            out["limit"] = spoken_top
        elif re.search(
            r"\b(?:the\s+)?all\s+(?:the\s+)?(distributors?|parties|customers?|clients?)\b",
            t,
        ) or rank_cuts:
            out["limit"] = max(int(out.get("limit") or 0), 200)
        elif yoy_rank:
            out["limit"] = max(int(out.get("limit") or 0), 200)
        out["_clear_omitted"] = False

    # "declined the most … vs AMS" / "least growth" / "smallest AMS gains"
    if re.search(
        r"\b(declined|dropped|fell|underperform|behind)\b.+\b(ams|expected)\b|"
        r"\bvs\.?\s*ams\b.+\b(declin|drop|worst|most)\b|"
        r"\b(declined|dropped)\s+the\s+most\b|"
        r"\b(least|smallest|lowest|worst)\s+"
        r"(ams\s+)?(growth|gains?|increases?)\b|"
        r"\bsmallest\s+ams\s+gains?\b|"
        r"\bleast\s+growth\b",
        t,
    ):
        rows = ["party"]
        cols = []
        out["intent"] = "party_rank"
        least_growth = bool(
            re.search(
                r"\b(least|smallest|lowest|worst)\s+"
                r"(ams\s+)?(growth|gains?|increases?)\b|"
                r"\bleast\s+growth\b|"
                r"\bsmallest\s+ams\s+gains?\b",
                t,
            )
        )
        if yoy_ask:
            # Calendar YoY of the spoken window — never AMS-window "smallest gains".
            out["metric"] = "yoy"
            out["compare"] = "yoy"
            out["sort"] = "asc" if least_growth else (out.get("sort") or "asc")
            out["title_mode"] = "lowest" if least_growth else out.get("title_mode")
            metrics = ["volume", "yoy"]
        elif least_growth:
            out["metric"] = "ams_growth"
            out["sort"] = "asc"
            out["title_mode"] = "smallest_gains"
            metrics = ["ams_growth", "ams", "volume"]
        else:
            out["metric"] = "vs_ams"
            out["declined_only"] = True
            out["sort"] = "asc"
            metrics = ["vs_ams", "volume", "ams"]
        if re.search(r"\bdistributors?\b", t) and not filters.get("client_type"):
            filters["client_type"] = (
                extract_client_type_from_text(user_text) or "Eva Distributors"
            )

    # "% of sales being VTF" → segment_mix (share of party volume + AMS)
    if re.search(
        r"\b(share|percent|%)\b.+\b(sales?|volume)\b.+\b(being|as|in)\b|"
        r"\bhighest\s+share\b.+\b(vtf|oil|product|sku)\b|"
        r"\bshare\s+of\s+their\s+sales\b",
        t,
    ):
        oil_hit = extract_oil_type_from_text(user_text)
        if oil_hit or re.search(r"\bvtf\b", t):
            rows = ["party"]
            cols = []
            out["intent"] = "party_rank"
            out["metric"] = "segment_mix"
            metrics = ["segment_mix"]
            if oil_hit:
                filters["oil_type"] = oil_hit
            elif re.search(r"\bvtf\b", t):
                filters["oil_type"] = "Eva VTF"
            if re.search(r"\bdistributors?\b", t) and not filters.get("client_type"):
                filters["client_type"] = (
                    extract_client_type_from_text(user_text) or "Eva Distributors"
                )
            grain = dict(out.get("grain") or {})
            grain["mix_dimension"] = "oil_type"
            out["grain"] = grain

    # --- Which products/SKUs led the growth ---
    if _looks_growth_drivers(t):
        out["compare"] = out.get("compare") or "yoy"
        if re.search(r"\bskus?\b", t):
            rows = ["product"]
        else:
            # Spoken "product" = packing category in Eva vocabulary
            rows = ["packing_category"]
        vol_metrics = set(metrics) & {"volume", "ams", "vs_ams", "ams_growth"}
        if not vol_metrics:
            metrics = metrics or ["volume", "ams"]
        if prior and out.get("base") != "prior":
            out["base"] = "prior"

    # --- Remove / exclude values (same-sentence OR follow-up) ---
    # "show Lahore Eva sales but exclude al shaheer" must EXCLUDE, never filter TO.
    # Skip when this turn is an ordinal who-is pick + clear-filter ask — those
    # must not invent party_like excludes from "remove the city filter…".
    if (
        not ordinal_followup
        and re.search(
            r"\b(remove|exclude|excluding|without|drop|hide|filter\s+out|except)\b",
            t,
        )
    ):
        spoken_ex = _resolve_spoken_excludes(user_text, prior_spec=prior)
        if spoken_ex:
            excludes = dict(out.get("excludes") or {})
            if prior:
                for dim, vals in dict(prior.get("excludes") or {}).items():
                    bucket = list(excludes.get(dim) or [])
                    for v in vals or []:
                        if v not in bucket:
                            bucket.append(v)
                    excludes[dim] = bucket
            for dim, vals in spoken_ex.items():
                bucket = list(excludes.get(dim) or [])
                for v in vals or []:
                    if v not in bucket:
                        bucket.append(v)
                excludes[dim] = bucket
            out["excludes"] = excludes
            # Keep prior table shape whenever prior had a grain, unless the user
            # explicitly asked customer/party-wise. Exclude alone must not flip
            # BU×month → party×month ("excluding al shaheer").
            explicit_party_grain = "party" in wise_dims or bool(
                re.search(
                    r"\b(customer|party|distributor)[- ]?wise\b|"
                    r"\bby\s+(customers?|parties|distributors?)\b",
                    t,
                )
            )
            if prior and prior_rows and not explicit_party_grain:
                rows = list(prior_rows)
                prior_cols = list(
                    prior.get("column_dimensions")
                    or (
                        [prior.get("column_dimension")]
                        if prior.get("column_dimension")
                        else []
                    )
                )
                if prior_cols:
                    cols = [c for c in prior_cols if c]
                prior_mets = list(prior.get("metrics") or [])
                if prior_mets:
                    metrics = prior_mets
                if out.get("base") != "prior":
                    out["base"] = "prior"
                    out["state_action"] = out.get("state_action") or "modify"
                out["_clear_omitted"] = False
            # Strip INCLUDE party filters that collide with excludes
            stripped = _strip_conflicting_party_includes(
                {
                    "filters": filters,
                    "party_query": out.get("party_query"),
                    "extracted_entities": list(out.get("extracted_entities") or []),
                },
                excludes,
            )
            filters = dict(stripped.get("filters") or {})
            if "party_query" in stripped:
                out["party_query"] = stripped.get("party_query")
            if "extracted_entities" in stripped:
                out["extracted_entities"] = stripped.get("extracted_entities")
            # Party excludes must clear sticky party INCLUDE scope
            if (
                excludes.get("party")
                or excludes.get("party_like")
                or excludes.get("parties")
            ):
                for key in PARTY_SCOPE_KEYS:
                    filters.pop(key, None)
                    if key not in clear:
                        clear.append(key)
                out["party_query"] = None
                if "party_query" not in clear:
                    clear.append("party_query")
                out["_clear_omitted"] = False
        elif prior:
            # Structural layer remove (remove city layer) still needs prior
            try:
                from eva_dashboard.chatbot import resolve_remove_request

                prior_spec = {
                    "row_dimension": (prior_rows[-1] if prior_rows else None),
                    "row_groups": list(prior_rows[:-1]) if len(prior_rows) > 1 else [],
                    "column_dimension": (
                        (prior.get("column_dimensions") or [None])[0]
                        or prior.get("column_dimension")
                        or "month"
                    ),
                    "filters": dict(prior.get("filters") or {}),
                    "business_units": list(prior.get("business_units") or []),
                    "excludes": dict(prior.get("excludes") or {}),
                }
                removed = resolve_remove_request(user_text, prior_spec=prior_spec)
                if removed and removed.get("mode") == "remove_layer":
                    leaf = removed.get("row_dimension")
                    groups = list(removed.get("row_groups") or [])
                    rows = groups + ([str(leaf)] if leaf else [])
                    if out.get("base") != "prior":
                        out["base"] = "prior"
            except Exception:  # noqa: BLE001
                pass

    # Bare month name: lock YYYY-MM to on-screen year, else live max sales date
    # (so "August" ≠ 2025-08 when data is through Aug 2026).
    if re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\b",
        t,
    ) and not re.search(r"20\d{2}", t):
        month_labels: list[str] = []
        if prior:
            month_labels = list(
                prior.get("month_labels")
                or (prior.get("period") or {}).get("month_labels")
                or []
            )
            for c in prior.get("columns") or prior.get("column_dimensions") or []:
                if re.match(r"^\d{4}-\d{2}$", str(c)):
                    month_labels.append(str(c))
        name_to_num = {
            "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
            "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7,
            "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
            "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12,
            "december": 12,
        }
        m = re.search(
            r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
            r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
            r"nov(?:ember)?|dec(?:ember)?)\b",
            t,
        )
        want = name_to_num.get((m.group(1) if m else "").lower())
        years = sorted(
            {
                int(str(lab)[:4])
                for lab in month_labels
                if re.match(r"^\d{4}-\d{2}$", str(lab))
                and want
                and int(str(lab)[5:7]) == want
            },
            reverse=True,
        )
        target: str | None = None
        if want and years:
            target = f"{years[0]:04d}-{want:02d}"
        elif want and m:
            try:
                from eva_dashboard.sales_query import resolve_period

                info = resolve_period(m.group(1))
                if info.get("ok") is not False and info.get("date_from"):
                    target = str(info["date_from"])[:7]
            except Exception:  # noqa: BLE001
                target = None
            # Correct a planner year that disagrees with the live anchor
            if target:
                bad_tm = str(out.get("target_month") or "").strip()
                if bad_tm and re.match(r"^\d{4}-\d{2}$", bad_tm) and bad_tm != target:
                    pass  # overwrite below
        if want and target:
            out["period_type"] = "SPECIFIC_MONTH"
            out["target_month"] = target
            period = dict(out.get("period") or {})
            period["phrase"] = target
            out["period"] = period

    # Oil/volume + average price asks → volume + avg_price (not price_fetch grid)
    oil_spoken = extract_oil_type_from_text(user_text)
    if oil_spoken and not filters.get("oil_type"):
        filters["oil_type"] = oil_spoken
    if oil_spoken or filters.get("oil_type"):
        wants_vol = bool(
            re.search(r"\b(volume|sold|sales?|how much|mt)\b", t)
        )
        wants_avg_price = bool(
            re.search(
                r"\b("
                r"average\s+(price|rate)|avg\.?\s*(price|rate)|"
                r"at what price|what price|with(?:\s+the)?\s+price|"
                r"pricing\s+data|include\s+pric"
                r")\b",
                t,
            )
        )
        if wants_vol and "volume" not in metrics:
            metrics.append("volume")
        if wants_avg_price and "avg_price" not in metrics:
            metrics.append("avg_price")
        if wants_vol and wants_avg_price:
            # Single-month oil summary — not a channel crosstab
            cols = [c for c in cols if c not in {"client_type", "month"}]
            if not rows or rows == ["business_unit"]:
                rows = rows or ["business_unit"]

    # Explicit clear must not merge sticky prior filters. Fresh complete asks
    # already clear unspoken city above; keep base=prior when exclude/grain
    # follow-ups promoted it (city stays dropped via clear_filters).
    if explicit_clear and out.get("base") == "prior":
        out["base"] = "sales"

    # Spoken brand (Eva / Maan) → Consumer + Bulk — don't keep a planner's
    # single-BU truncation of a brand ask.
    try:
        from eva_dashboard.chatbot import _extract_business_units_from_text

        spoken_bus = _extract_business_units_from_text(user_text)
    except Exception:  # noqa: BLE001
        spoken_bus = []
    if spoken_bus:
        cur_bus = [str(b) for b in (out.get("business_units") or []) if b]
        # Upgrade when planner omitted BUs or kept a strict subset of the brand
        if not cur_bus or set(cur_bus).issubset(set(spoken_bus)):
            out["business_units"] = list(spoken_bus)
            filters["business_units"] = list(spoken_bus)
            if len(spoken_bus) != 1:
                filters.pop("business_unit", None)

    out["filters"] = filters
    out["row_dimensions"] = rows
    out["metrics"] = metrics
    out["column_dimensions"] = cols
    if clear:
        out["clear_filters"] = clear
        # Also mirror onto `clear` used by merge_prior_into_spec
        out["clear"] = list(dict.fromkeys(list(out.get("clear") or []) + clear))
    return out


def _canon_filters(filters: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize provided filter values only — never invent filters."""
    out = dict(filters or {})
    if out.get("city"):
        out["city"] = str(out["city"]).strip()
    if out.get("zone"):
        out["zone"] = normalize_zone(out.get("zone"))
    # Do NOT normalize client_type when it is clearly a Business Unit —
    # validation must reject that for the LLM retry loop.
    if out.get("client_type") and not is_business_unit_label(str(out["client_type"])):
        out["client_type"] = normalize_client_type(out.get("client_type"))
    if out.get("cities"):
        cities = [str(c).strip() for c in out["cities"] if str(c).strip()]
        if cities:
            out["cities"] = cities
            out.pop("city", None)
        else:
            out.pop("cities", None)
    if out.get("client_types"):
        cts: list[str] = []
        for raw in out["client_types"]:
            if is_business_unit_label(str(raw)):
                continue
            ct = normalize_client_type(str(raw))
            if ct and ct not in cts:
                cts.append(ct)
        if cts:
            out["client_types"] = cts
            out.pop("client_type", None)
        else:
            out.pop("client_types", None)

    oil = out.get("oil_type")
    pack = out.get("packing_category")
    if oil and not pack:
        o2, p2 = extract_oil_and_packing(str(oil))
        if o2 and p2:
            oil, pack = o2, p2
    if pack and not oil:
        o2, p2 = extract_oil_and_packing(str(pack))
        if o2 and p2:
            oil, pack = o2, p2
    product = out.get("product")
    if product and (not oil or not pack):
        o2, p2 = extract_oil_and_packing(str(product))
        if o2 and not oil:
            oil = o2
        if p2 and not pack:
            pack = p2
        if o2 and p2 and " " in str(product).strip():
            if not any(ch.isdigit() for ch in str(product)):
                out.pop("product", None)

    if oil:
        out["oil_type"] = normalize_oil_type(oil)
    if pack:
        out["packing_category"] = normalize_packing_category(pack)

    if out.get("parties") and not isinstance(out["parties"], list):
        out["parties"] = [str(out["parties"])]
    if out.get("cities"):
        if not isinstance(out["cities"], list):
            out["cities"] = [str(out["cities"])]
        out["cities"] = [str(c).strip() for c in out["cities"] if str(c).strip()]
        if out["cities"]:
            out.pop("city", None)
        else:
            out.pop("cities", None)
    if out.get("client_types"):
        if not isinstance(out["client_types"], list):
            out["client_types"] = [str(out["client_types"])]
        normed: list[str] = []
        for raw in out["client_types"]:
            n = normalize_client_type(str(raw).strip()) or str(raw).strip()
            if n and n not in normed:
                normed.append(n)
        if normed:
            out["client_types"] = normed
            out.pop("client_type", None)
        else:
            out.pop("client_types", None)
    return out


def _looks_party_metric_followup(user_text: str) -> bool:
    """Short follow-ups that should keep a prior customer scope.

    Conservative: only short metric asks, or ones that refer back to
    \"their/this/that\" customer. Full fresh asks keep context_handling=none.
    """
    t = (user_text or "").lower().strip()
    if not t:
        return False
    has_metric = bool(
        re.search(
            r"\b("
            r"price\s*fetch|avg\.?\s*rate|average\s+(price|rate)|"
            r"what'?s\s+the\s+price|what\s+is\s+the\s+price|"
            r"selling\s+price|"
            r"%\s*(of\s+)?(their\s+|its\s+)?ams|percent\s+of\s+ams|"
            r"vs\.?\s*ams|against\s+ams|of\s+their\s+ams|"
            r"last\s+(purchase|invoice|order|buy)|"
            r"days\s+since|when\s+did\s+they\s+last|"
            r"sku[-\s]?wise|product\s+break|packing\s+break|"
            r"cost\s*factor"
            r")\b",
            t,
        )
    )
    if not has_metric:
        return False
    refers_back = bool(
        re.search(r"\b(their|this|that|same|the\s+customer|the\s+party)\b", t)
    )
    # Long / fully scoped asks are fresh topics, not sticky follow-ups
    if len(t.split()) > 12 and not refers_back:
        return False
    if (
        re.search(
            r"\b(in|for)\s+(lahore|karachi|islamabad|imtiaz|distributors?|"
            r"eva\s+consumer|maan)\b",
            t,
        )
        and not refers_back
        and len(t.split()) > 6
    ):
        return False
    return True


def _prior_party_scope(prior: dict[str, Any] | None) -> dict[str, Any]:
    if not prior:
        return {}
    scope = dict(prior.get("party_scope") or {})
    filters = dict(prior.get("filters") or {})
    for key in PARTY_SCOPE_KEYS:
        if key not in scope and filters.get(key) not in (None, "", []):
            scope[key] = filters[key]
    return scope


def _stick_party_scope_from_prior(
    spec: dict[str, Any],
    prior: dict[str, Any] | None,
    *,
    user_text: str = "",
) -> dict[str, Any]:
    """Keep prior customer filters on metric follow-ups / explicit prior base.

    When the LLM sets ``state_action`` explicitly:
    - clear → never soft-stick
    - keep / modify → stick only via normal prior merge (base already prior)
    Soft heuristic stick (force base=prior) only runs for legacy plans that
    omit state_action, so we stop guessing against the model's intent.
    """
    out = dict(spec)
    # Never re-stick a party the user just asked to exclude/remove.
    spoken_ex = _resolve_spoken_excludes(user_text) if user_text else {}
    if spoken_ex.get("party") or spoken_ex.get("party_like") or spoken_ex.get("parties"):
        return out
    if _party_exclude_needles(out.get("excludes")):
        # Spec already carries party excludes — don't restore include scope
        return out

    action = str(out.get("state_action") or "").strip().lower()
    explicit = bool(out.get("_state_action_explicit"))
    if explicit and action == "clear":
        return out

    scope = _prior_party_scope(prior)
    if not scope:
        return out
    # If prior party matches a spoken/spec exclude, do not stick it
    trial = {"filters": dict(scope), "excludes": dict(out.get("excludes") or {})}
    trial = _strip_conflicting_party_includes(trial, trial.get("excludes") or spoken_ex)
    if not any(
        (trial.get("filters") or {}).get(k) not in (None, "", [])
        for k in PARTY_SCOPE_KEYS
    ):
        return out
    filters = dict(out.get("filters") or {})
    clear = set(out.get("clear") or [])
    if clear & set(PARTY_SCOPE_KEYS):
        return out
    if any(filters.get(k) not in (None, "", []) for k in PARTY_SCOPE_KEYS):
        return out

    # Explicit keep/modify with base=prior: inherit party scope if omitted
    if out.get("base") == "prior":
        stick = True
    elif explicit:
        # Model said clear already handled; keep without base shouldn't happen
        stick = False
    else:
        # Legacy soft stick only when state_action was not declared
        stick = _looks_party_metric_followup(user_text)
    if not stick:
        return out
    for key, val in scope.items():
        filters[key] = val
    out["filters"] = filters
    if not out.get("party_query") and filters.get("party"):
        out["party_query"] = filters["party"]
    # Promote to prior merge semantics so period/BU inherit when model omitted them
    if out.get("base") != "prior" and not explicit and _looks_party_metric_followup(
        user_text
    ):
        out["base"] = "prior"
        out["state_action"] = "keep"
        # clear_filters was omitted — treat as keep-all for this soft stick
        if out.get("_clear_omitted"):
            out["_clear_omitted"] = False
            out["clear"] = list(out.get("clear") or [])
    return out


def _resolve_party_filters_silent(filters: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Inject exact party / silent party_ilike — never returns plan_errors.

    Channel aliases (metro, metro habib, LMT, chase up, …) are redirected to
    ``client_type`` — never treated as customer-name ILIKE.
    """
    out = dict(filters)
    queries: list[str] = []
    if out.get("parties"):
        queries.extend(str(p).strip() for p in out["parties"] if str(p).strip())
    party_raw = out.get("party") or spec.get("party_query")
    if party_raw and str(party_raw).strip():
        pr = str(party_raw).strip()
        if pr not in queries:
            queries.insert(0, pr)

    if not queries:
        return out

    # Peel channel aliases → client_type
    party_queries: list[str] = []
    for q in queries:
        ct = match_client_type_alias(q)
        if ct:
            if not out.get("client_type"):
                out["client_type"] = ct
            continue
        party_queries.append(q)

    if not party_queries:
        # Pure channel ask (e.g. filters.party="metro habib")
        out.pop("party", None)
        out.pop("parties", None)
        out.pop("party_ilike", None)
        if spec.get("party_query") and match_client_type_alias(str(spec.get("party_query"))):
            spec.pop("party_query", None)
        return out

    queries = party_queries
    out.pop("parties", None)

    if len(queries) == 1:
        matched = resolve_party_filter(queries[0])
        if matched.get("party"):
            out["party"] = matched["party"]
            out.pop("party_ilike", None)
        else:
            out.pop("party", None)
            out["party_ilike"] = list(matched.get("party_ilike") or queries)
        out["_party_matches"] = matched.get("matches") or []
        if spec.get("party_query") and matched.get("party"):
            spec["party_query"] = matched["party"]
        return out

    multi = resolve_party_filters(queries)
    out.pop("party", None)
    if multi.get("parties") and not multi.get("party_ilike"):
        out["parties"] = list(multi["parties"])
        out.pop("party_ilike", None)
    else:
        if multi.get("parties"):
            out["parties"] = list(multi["parties"])
        if multi.get("party_ilike"):
            out["party_ilike"] = list(multi["party_ilike"])
    out["_party_matches"] = multi.get("matches") or []
    return out


def _looks_party_profile_ask(user_text: str) -> bool:
    """True for 'tell me about X' / customer rundown language."""
    t = (user_text or "").lower().strip()
    if not t:
        return False
    return bool(
        re.search(
            r"\b("
            r"tell\s+me\s+about|customer\s+profile|party\s+profile|"
            r"profile\s+of|rundown\s+on|overview\s+of|"
            r"give\s+me\s+(a\s+)?(full\s+)?(picture|profile|rundown)"
            r"(\s+(on|of|for))?|"
            r"full\s+picture|"
            r"how\s+(is|are)\s+.+\s+(doing|performing)\b|"
            r"how\s+(is|are)\s+they\s+(doing|performing)\b|"
            r"everything\s+about|"
            r"last\s+(purchase|invoice|order|buy)(\s+date)?|"
            r"days\s+since(\s+last)?|"
            r"when\s+did\s+they\s+last"
            r")\b",
            t,
        )
    )


def execute_query_spec(
    raw_spec: dict[str, Any],
    *,
    prior: dict[str, Any] | None = None,
    user_text: str = "",
) -> dict[str, Any]:
    """Execute a planned QuerySpec blindly (universal pivot or legacy)."""
    # Hard short-circuit: "who is X" never enters the pivot / retry loop
    if user_text and re.search(
        r"\b(who\s+is|who'?s)\b", user_text, flags=re.I
    ) and not re.search(
        r"\b(sales?|volume|ams|price|growth|doing|performance|exclude)\b",
        user_text,
        flags=re.I,
    ):
        m = re.search(
            r"\b(?:who\s+is|who'?s)\s+(.+?)(?:\?|$)",
            user_text,
            flags=re.I,
        )
        q = (m.group(1).strip(" .,!?") if m else "") or (
            (raw_spec or {}).get("party_query")
            or ((raw_spec or {}).get("filters") or {}).get("party")
            or ""
        )
        result = lookup_party(str(q), limit=int((raw_spec or {}).get("limit") or 10))
        matches = list(result.get("matches") or [])
        result["query_spec"] = {
            "operation": "party_lookup",
            "intent": "party_lookup",
            "party_query": result.get("query") or q,
            "row_dimensions": ["party"],
            "metrics": ["volume", "ams"],
            "matches": matches,
        }
        # Stamp matches so "show AMS for 1 and 2" resolves without re-fuzzy
        result["party_spec"] = {
            "kind": "party_lookup",
            "matches": matches,
            "filters": {},
        }
        result["table_spec"] = {
            "filters": {},
            "row_dimension": "party",
            "row_dimensions": ["party"],
            "column_dimensions": ["month"],
            "metrics": ["volume", "ams"],
            "matches": matches,
            "kind": "party_lookup",
        }
        if len(matches) == 1 and float(matches[0].get("match_score") or 0) >= 0.72:
            name = str(matches[0].get("client") or "").strip()
            if name:
                result["party"] = name
                result["party_spec"]["filters"] = {"party": name}
                result["table_spec"]["filters"] = {"party": name}
        from eva_dashboard.query_spec import build_query_state
        from eva_dashboard.agent_loop import apply_verification

        result["query_state"] = build_query_state(
            query_spec=result.get("query_spec"),
            table_spec=result.get("table_spec"),
            party_spec=result.get("party_spec"),
            result_mode="party_lookup",
        )
        return apply_verification(result, user_text=user_text)

    spec = normalize_query_spec(raw_spec)
    # Soft-stick party scope before merge so short follow-ups keep the customer
    # (skipped automatically when the user asked to exclude/remove a party).
    spec = _enforce_exclude_wins(spec, user_text=user_text)
    spec = _stick_party_scope_from_prior(spec, prior, user_text=user_text)
    spec = merge_prior_into_spec(spec, prior)
    # Re-apply after merge in case merge cleared then model omitted party
    spec = _stick_party_scope_from_prior(spec, prior, user_text=user_text)
    spec = _enforce_exclude_wins(spec, user_text=user_text)
    # Python entity resolution BEFORE validation (forgiving extracted_entities).
    # Pass user_text so "exclude al shaheer" never becomes filters.party.
    spec = _apply_extracted_entities(spec, user_text=user_text)
    # Spoken vocab safety nets (SKU / product / price_fetch / excludes)
    spec = _coerce_vocab_from_user_text(spec, user_text, prior=prior)
    # Final safety: excludes always beat party includes
    spec = _enforce_exclude_wins(spec, user_text=user_text)
    # Follow-up intents may promote base=prior after the first merge — re-apply
    if prior and spec.get("base") == "prior":
        keep_rows = list(spec.get("row_dimensions") or [])
        keep_cols = list(spec.get("column_dimensions") or [])
        keep_ex = dict(spec.get("excludes") or {})
        keep_compare = spec.get("compare")
        keep_metrics = list(spec.get("metrics") or [])
        keep_metric_filters = list(spec.get("metric_filters") or [])
        keep_clear = list(spec.get("clear") or [])
        spec = merge_prior_into_spec(spec, prior)
        if keep_rows:
            spec["row_dimensions"] = keep_rows
        if keep_cols:
            spec["column_dimensions"] = keep_cols
        if keep_compare:
            spec["compare"] = keep_compare
        if keep_metrics:
            spec["metrics"] = keep_metrics
        if keep_metric_filters:
            spec["metric_filters"] = keep_metric_filters
        if keep_clear:
            # Preserve party clear so merge cannot restore excluded includes
            merged_clear = list(spec.get("clear") or [])
            for c in keep_clear:
                if c not in merged_clear:
                    merged_clear.append(c)
            spec["clear"] = merged_clear
        if keep_ex or prior.get("excludes"):
            merged_ex = dict(prior.get("excludes") or {})
            for k, vals in keep_ex.items():
                bucket = list(merged_ex.get(k) or [])
                for v in vals or []:
                    if v not in bucket:
                        bucket.append(v)
                merged_ex[k] = bucket
            spec["excludes"] = merged_ex
        # Rematch can restore sticky party — strip again if excluded
        spec = _enforce_exclude_wins(spec, user_text=user_text)
    # Governed metrics/operations synonyms (Phase 3 semantic layer)
    from eva_dashboard.metrics_catalog import apply_metric_synonyms_to_spec

    spec = apply_metric_synonyms_to_spec(spec, user_text)
    # Promote profile asks even when the model left operation=pivot
    if (
        spec.get("operation") in {"", "pivot", None}
        and _looks_party_profile_ask(user_text)
        and (
            spec.get("party_query")
            or (spec.get("filters") or {}).get("party")
            or (spec.get("filters") or {}).get("party_ilike")
            or (spec.get("extracted_entities") or [])
        )
    ):
        spec["operation"] = "party_profile"
        spec["intent"] = "party_profile"
        if not spec.get("metrics"):
            spec["metrics"] = ["volume", "ams", "vs_ams"]
        if not spec.get("row_dimensions"):
            spec["row_dimensions"] = ["party"]
    # "who is X" → identity lookup (never a sales matrix / tool loop)
    if (
        spec.get("operation") in {"", "pivot", None, "party_profile"}
        and re.search(r"\b(who\s+is|who'?s)\b", (user_text or "").lower())
        and not re.search(
            r"\b(sales?|volume|ams|price|growth|doing|performance)\b",
            (user_text or "").lower(),
        )
    ):
        spec["operation"] = "party_lookup"
        spec["intent"] = "party_lookup"
        if not spec.get("party_query"):
            ents = list(spec.get("extracted_entities") or [])
            filt_party = (spec.get("filters") or {}).get("party")
            if filt_party:
                spec["party_query"] = filt_party
            elif ents:
                spec["party_query"] = ents[0]
            else:
                m = re.search(
                    r"\b(?:who\s+is|who'?s)\s+(.+?)(?:\?|$)",
                    user_text or "",
                    flags=re.IGNORECASE,
                )
                if m:
                    spec["party_query"] = m.group(1).strip(" .,!?")
    spec["business_units"] = _expand_business_units(
        list(spec.get("business_units") or [])
    )
    if spec.get("business_units"):
        filters = dict(spec.get("filters") or {})
        filters["business_units"] = list(spec["business_units"])
        spec["filters"] = filters

    resolved = resolve_period_from_spec(spec)
    spec["period"] = resolved["period"]
    spec["grain"] = resolved["grain"]
    if resolved.get("months_back") is not None:
        spec["months_back"] = resolved["months_back"]
    if resolved.get("target_month"):
        spec["target_month"] = resolved["target_month"]
    if "row_dimensions" in resolved:
        spec["row_dimensions"] = resolved["row_dimensions"]
    if "column_dimensions" in resolved:
        spec["column_dimensions"] = resolved["column_dimensions"]
        grain = dict(spec.get("grain") or {})
        cols_r = resolved["column_dimensions"]
        if cols_r:
            grain["column_dimension"] = cols_r[0]
            if cols_r[0] == "month":
                grain["time_grain"] = "month"
        else:
            # SPECIFIC_MONTH cleared month columns — pick a non-conflicting
            # column grain (never equal the row leaf, e.g. channel×channel).
            # volume + avg_price oil/period summaries stay flat (no channel grid).
            mets_now = set(spec.get("metrics") or [])
            rows_now = list(spec.get("row_dimensions") or [])
            if "volume" in mets_now and "avg_price" in mets_now:
                grain.pop("column_dimension", None)
                spec["column_dimensions"] = []
            elif "party" in rows_now:
                # Customer lists stay flat — never invent party × client_type
                grain.pop("column_dimension", None)
                spec["column_dimensions"] = []
            elif grain.get("column_dimension") == "month" or not cols_r:
                row_set = set(rows_now)
                leaf = rows_now[-1] if rows_now else None
                fallback = "client_type"
                for candidate in ("client_type", "city", "business_unit", "zone"):
                    if candidate != leaf and candidate not in row_set:
                        fallback = candidate
                        break
                grain["column_dimension"] = fallback
                spec["column_dimensions"] = [fallback]
            grain["time_grain"] = "none"
        spec["grain"] = grain
    # Sync grain row dims from resolved/defaulted row_dimensions
    rows = list(spec.get("row_dimensions") or [])
    if rows:
        grain = dict(spec.get("grain") or {})
        if len(rows) >= 2:
            grain["row_groups"] = rows[:-1]
            grain["row_dimension"] = rows[-1]
        else:
            grain["row_dimension"] = rows[0]
            if rows[0] in {"party", "city", "zone"}:
                grain["group_by"] = rows[0]
        spec["grain"] = grain

    # Canonicalize filters before validation so spoken aliases (metro →
    # METRO HABIB, lmt → LMT) pass the client_type enum check.
    spec["filters"] = _canon_filters(spec.get("filters") or {})

    errors = validate_query_spec(spec, prior=prior, user_text=user_text)
    if errors:
        return {
            "ok": False,
            "error": "Validation failed — fix the QuerySpec and call plan_query again.",
            "plan_errors": errors,
            "query_spec": spec,
            "response_instructions": (
                "REQUIRED: Call plan_query again with a corrected QuerySpec. "
                "Address every plan_errors item. "
                "Business Units (Eva Consumer, Eva Bulk, …) go in business_units — "
                "NEVER in client_type. Use extracted_entities when unsure. "
                "For a single month like March use period_type=SPECIFIC_MONTH + "
                "target_month=YYYY-MM (not LAST_N_MONTHS). "
                "SKU / SKU-wise → row_dimensions=[\"product\"]. "
                "product / product-wise → row_dimensions=[\"packing_category\"]. "
                "Do not invent numbers."
            ),
        }

    filters = dict(spec.get("filters") or {})
    grain = dict(spec.get("grain") or {})
    period = dict(spec.get("period") or {})
    intent = spec.get("intent") or ""
    operation = str(spec.get("operation") or "pivot")
    row_dimensions = list(spec.get("row_dimensions") or [])
    column_dimensions = list(spec.get("column_dimensions") or [])
    metrics = list(spec.get("metrics") or [])
    bus = _expand_business_units(
        list(spec.get("business_units") or filters.get("business_units") or [])
    )
    if filters.get("business_unit") and not bus:
        bus = _expand_business_units([filters["business_unit"]])

    # Silent party resolution for all analytics paths (no LLM retry loops).
    # party_lookup / party_list keep their own disambiguation UIs.
    if operation not in {"party_list", "party_lookup"} and intent not in {
        "party_list",
        "party_lookup",
    }:
        filters = _resolve_party_filters_silent(filters, spec)
        spec["filters"] = filters

    # NUCLEAR GATE (last moment before engines): exclude language always wins.
    # Silent party resolve / sticky prior must not leave filters.party=Al Shaheer
    # when the user said "exclude al shaheer".
    spec = _enforce_exclude_wins(spec, user_text=user_text)
    filters = dict(spec.get("filters") or {})

    phrase = (period.get("phrase") or "").strip() or None
    date_from = period.get("date_from")
    date_to = period.get("date_to")
    mb = int(spec.get("months_back") or grain.get("months_back") or 6)

    party_kw = {
        "party": filters.get("party"),
        "parties": filters.get("parties"),
        "party_ilike": filters.get("party_ilike"),
    }

    result: dict[str, Any]

    # Named-month Volume vs AMS packs (BU/city/channel × AMS) must not fall into
    # party ranking just because metrics include vs_ams.
    named_month_vol_ams = (
        str(spec.get("period_type") or "") in {"SPECIFIC_MONTH", "NAMED_MONTH"}
        and "volume" in set(metrics)
        and bool(row_dimensions)
        and "party" not in row_dimensions
    )

    # ---- Special non-pivot operations ----
    if operation == "party_list" or intent == "party_list":
        result = list_clients(
            city=filters.get("city"),
            zone=filters.get("zone"),
            client_type=filters.get("client_type"),
            business_unit=filters.get("business_unit") or (bus[0] if bus else None),
            period=phrase,
            date_from=date_from,
            date_to=date_to,
            limit=int(spec.get("limit") or 200),
            active_only=bool(filters.get("active_only")),
        )
    elif operation == "party_lookup" or intent == "party_lookup":
        # "who is al shaheer" → identity match card (not a sales matrix).
        # Sales for a named party belongs on pivot / party_profile.
        q = (
            spec.get("party_query")
            or filters.get("party")
            or (
                (filters.get("party_ilike") or [None])[0]
                if filters.get("party_ilike")
                else None
            )
            or ""
        )
        # Prefer extracted entity when planner left party_query empty
        if not str(q).strip():
            ents = list(spec.get("extracted_entities") or [])
            q = ents[0] if ents else ""
        result = lookup_party(str(q), limit=int(spec.get("limit") or 10))
        # Stamp matches for ordinal follow-ups ("first 2", "#1 and #2")
        matches = list(result.get("matches") or [])
        result["party_spec"] = {
            "kind": "party_lookup",
            "matches": matches,
            "filters": {},
        }
        # Single strong match → also stamp sticky party for follow-ups
        if len(matches) == 1 and float(matches[0].get("match_score") or 0) >= 0.72:
            name = str(matches[0].get("client") or "").strip()
            if name:
                result["party"] = name
                result["party_spec"]["filters"] = {"party": name}
                result["table_spec"] = {
                    "filters": {"party": name},
                    "row_dimension": "party",
                    "row_dimensions": ["party"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "matches": matches,
                }
        elif matches:
            # Multi-match: keep matches on table_spec so memory can resolve ordinals
            result["table_spec"] = {
                "filters": {},
                "row_dimension": "party",
                "row_dimensions": ["party"],
                "column_dimensions": ["month"],
                "metrics": ["volume", "ams"],
                "matches": matches,
                "kind": "party_lookup",
            }
    elif operation == "party_profile" or intent == "party_profile":
        q = (
            spec.get("party_query")
            or filters.get("party")
            or (
                (filters.get("party_ilike") or [None])[0]
                if filters.get("party_ilike")
                else None
            )
            or ""
        )
        # Prefer exact resolved party when silent ILIKE already matched one name
        exact_party = filters.get("party")
        result = party_profile(
            query=str(q) if q else None,
            party=str(exact_party) if exact_party else None,
            period=phrase,
            date_from=date_from,
            date_to=date_to,
            months_back=mb,
            city=filters.get("city"),
            client_type=filters.get("client_type"),
            business_unit=filters.get("business_unit") or (bus[0] if len(bus) == 1 else None),
            business_units=bus if len(bus) > 1 else None,
            oil_type=filters.get("oil_type"),
            packing_category=filters.get("packing_category"),
        )
    elif operation == "overview" or intent == "overview":
        from eva_dashboard.chatbot import sales_overview

        result = sales_overview()
    elif operation == "advanced" or intent == "advanced":
        from eva_dashboard.chatbot import _dispatch_advanced

        result = _dispatch_advanced(
            {
                "mode": spec.get("advanced_mode"),
                **filters,
                "period": phrase,
                "date_from": date_from,
                "date_to": date_to,
            },
            "",
            prior_spec=None,
        )
    elif not named_month_vol_ams and (
        intent == "party_rank"
        or str(spec.get("metric") or "")
        in {"yoy", "yoy_ams", "ams_growth", "vs_ams"}
        or set(metrics) & {"yoy", "yoy_ams"}
        or (
            set(metrics) & {"vs_ams", "ams_growth"}
            and "month" not in column_dimensions
        )
    ):
        group_by = grain.get("group_by") or (
            row_dimensions[0] if row_dimensions else "party"
        )
        limit = int(spec.get("limit") or 25)
        brand = None
        single_bu = filters.get("business_unit") or (
            bus[0] if len(bus) == 1 else None
        )
        if len(bus) > 1 and not single_bu:
            lowers = [str(b).lower() for b in bus]
            if all(b.startswith("eva") for b in lowers):
                brand = "eva"
            elif all(b.startswith("maan") for b in lowers):
                brand = "maan"
        rank_metric = str(
            spec.get("metric") or (metrics[0] if metrics else "ams")
        )
        result = analyze_parties(
            period=phrase,
            date_from=date_from,
            date_to=date_to,
            city=filters.get("city"),
            zone=filters.get("zone"),
            client_type=filters.get("client_type"),
            business_unit=single_bu,
            brand=brand,
            oil_type=filters.get("oil_type"),
            packing_category=filters.get("packing_category"),
            metric=rank_metric,
            group_by=group_by,
            sort=str(spec.get("sort") or "desc"),
            grown_only=bool(spec.get("grown_only")),
            declined_only=bool(spec.get("declined_only")),
            limit=limit,
            active_only=bool(filters.get("active_only")),
            title_mode=spec.get("title_mode"),
            mix_dimension=grain.get("mix_dimension"),
            metric_filters=list(spec.get("metric_filters") or []),
        )
    elif _is_factor_only_ask(user_text, metrics=metrics, flags=spec.get("price_flags")):
        # Cost factors live in factor_costs — not sales Price Fetch.
        ctype = filters.get("client_type") or DEFAULT_FACTOR_CLIENT_TYPE
        result = query_factor_costs(
            client_type=ctype,
            business_unit=filters.get("business_unit") or (bus[0] if bus else None),
            oil_type=filters.get("oil_type"),
            packing_category=filters.get("packing_category"),
            product=filters.get("product"),
            breakdown=True,
            limit=int(spec.get("limit") or 80),
        )
    elif "price_fetch" in metrics or "avg_price" in metrics or "last_price" in metrics:
        flags = dict(spec.get("price_flags") or {})
        want_last = "last_price" in metrics or bool(
            re.search(
                r"\b(last|latest|most\s+recent)\s+price|"
                r"last\s+(price\s+)?sold|price\s+date\s+of\s+sale|"
                r"date\s+of\s+sale|sale\s+date\b",
                user_text or "",
                flags=re.I,
            )
        )
        want_fetch = (
            "price_fetch" in metrics
            or want_last
            or bool(flags.get("include_price_fetch"))
            or bool(flags.get("include_cost_factor"))
            or bool(flags.get("factor_breakdown"))
        )
        # Dedicated Price Fetch / last-price path — never monthly trend HTML
        if want_fetch or want_last:
            pf_rows = [d for d in row_dimensions if d != "month"]
            has_party_scope = bool(
                party_kw.get("party")
                or party_kw.get("parties")
                or party_kw.get("party_ilike")
            )
            if not pf_rows:
                # SKU breakup default for last price / party-scoped fetch
                pf_rows = ["product"] if (has_party_scope or want_last) else []
            if want_last and "product" not in pf_rows:
                pf_rows = list(pf_rows) + ["product"]
            # Safety: spoken channel grain must appear even if the planner
            # emitted party×SKU (common failure mode for "all channels").
            spoken_pf = _spoken_wise_dimensions(user_text or "")
            if "client_type" in spoken_pf and "client_type" not in pf_rows:
                pf_rows = ["client_type"] + [d for d in pf_rows if d != "party"]
            if (
                "client_type" in spoken_pf
                and "party" not in spoken_pf
                and "party" in pf_rows
            ):
                pf_rows = [d for d in pf_rows if d != "party"]
            if "client_type" in spoken_pf and not filters.get("client_types"):
                filters.pop("client_type", None)
            if pf_rows:
                result = query_price_fetch_table(
                    row_dimensions=pf_rows,
                    period=phrase,
                    date_from=date_from,
                    date_to=date_to,
                    city=filters.get("city"),
                    business_unit=(
                        filters.get("business_unit")
                        or (bus[0] if len(bus) == 1 else None)
                    ),
                    business_units=bus if len(bus) > 1 else None,
                    oil_type=filters.get("oil_type"),
                    packing_category=filters.get("packing_category"),
                    client_type=filters.get("client_type"),
                    product=filters.get("product"),
                    price_mode="last" if want_last else "avg",
                    **party_kw,
                )
            else:
                result = query_price(
                    period=phrase,
                    date_from=date_from,
                    date_to=date_to,
                    city=filters.get("city"),
                    business_unit=(
                        filters.get("business_unit") or (bus[0] if bus else None)
                    ),
                    oil_type=filters.get("oil_type"),
                    packing_category=filters.get("packing_category"),
                    client_type=filters.get("client_type"),
                    product=filters.get("product"),
                    include_price_fetch=True,
                    include_cost_factor=True,
                    factor_breakdown=bool(flags.get("factor_breakdown")),
                    time_grain=None,
                    **party_kw,
                )
        elif bool(row_dimensions):
            # Plain avg_price pivot (no cost factor) — party + volume + avg_price
            # renders as a single Customer × Volume + Avg Rate table.
            result = execute_universal_pivot(
                row_dimensions=row_dimensions,
                column_dimensions=column_dimensions,
                metrics=metrics,
                period=phrase,
                date_from=date_from,
                date_to=date_to,
                months_back=mb,
                city=filters.get("city"),
                zone=filters.get("zone"),
                business_unit=bus[0] if len(bus) == 1 else filters.get("business_unit"),
                business_units=bus if len(bus) > 1 else None,
                oil_type=filters.get("oil_type"),
                packing_category=filters.get("packing_category"),
                client_type=filters.get("client_type"),
                active_only=bool(filters.get("active_only")),
                limit=int(spec.get("limit") or 0) or None,
                **party_kw,
            )
        else:
            time_grain = "month" if "month" in column_dimensions else None
            if not time_grain and grain.get("time_grain") == "month":
                time_grain = "month"
            result = query_price(
                period=phrase,
                date_from=date_from,
                date_to=date_to,
                city=filters.get("city"),
                business_unit=filters.get("business_unit") or (bus[0] if bus else None),
                oil_type=filters.get("oil_type"),
                packing_category=filters.get("packing_category"),
                client_type=filters.get("client_type"),
                product=filters.get("product"),
                include_price_fetch=False,
                include_cost_factor=False,
                factor_breakdown=False,
                time_grain=time_grain,
                **party_kw,
            )
    elif named_month_vol_ams or intent in {
        "sales_matrix",
        "sales_trend",
        "sales_analytical",
    } or (set(metrics) & {"volume", "ams"}):
        mode = {
            "sales_matrix": "matrix",
            "sales_trend": "trend",
            "sales_analytical": "analytical",
        }.get(
            intent,
            "trend"
            if ("month" in column_dimensions or named_month_vol_ams)
            else "matrix",
        )
        columns = (
            column_dimensions[0]
            if column_dimensions
            else (grain.get("column_dimension") or "client_type")
        )
        if row_dimensions:
            row_dim = row_dimensions[-1]
            row_groups = row_dimensions[:-1] or None
        else:
            row_dim = grain.get("row_dimension")
            row_groups = list(grain.get("row_groups") or []) or None
        bu = bus[0] if len(bus) == 1 else None
        bus_param = bus if len(bus) > 1 else None
        result = query_sales(
            period=phrase,
            date_from=date_from,
            date_to=date_to,
            city=filters.get("city"),
            cities=list(filters.get("cities") or []) or None,
            zone=filters.get("zone"),
            business_unit=bu or filters.get("business_unit"),
            business_units=bus_param,
            oil_type=filters.get("oil_type"),
            packing_category=filters.get("packing_category"),
            client_type=filters.get("client_type"),
            client_types=list(filters.get("client_types") or []) or None,
            columns=columns,
            months_back=mb,
            row_dimension=row_dim,
            row_groups=row_groups,
            excludes=spec.get("excludes") or None,
            mode=mode,
            compare=spec.get("compare"),
            active_only=bool(filters.get("active_only")),
            prior_spec=None,
            limit=int(spec.get("limit") or 0) or None,
            metric_filters=list(spec.get("metric_filters") or []) or None,
            **party_kw,
        )
    else:
        result = {
            "ok": False,
            "error": (
                f"Unsupported plan (operation={operation}, metrics={metrics}, "
                f"rows={row_dimensions}, cols={column_dimensions})."
            ),
        }

    if isinstance(result, dict):
        result = dict(result)
        filled_spec = dict(spec)
        filled_spec.pop("_clear_omitted", None)
        filled_spec["period"] = period
        filled_spec["grain"] = grain
        filled_spec["filters"] = filters
        filled_spec["business_units"] = bus
        filled_spec["row_dimensions"] = row_dimensions
        filled_spec["column_dimensions"] = column_dimensions
        filled_spec["metrics"] = metrics
        result["query_spec"] = filled_spec
        result.setdefault("ok", True)

        # Identify matched parties before exclude/include so fuzzy names are clear
        excludes = dict(spec.get("excludes") or {})
        ex_needles = _party_exclude_needles(excludes)
        if ex_needles and result.get("ok") and result.get("answer_markdown"):
            preview = _build_party_polarity_preview(
                needles=ex_needles,
                polarity="exclude",
                period_phrase=phrase,
                date_from=date_from,
                date_to=date_to,
                filters=filters,
                bus=bus,
            )
            if preview:
                result["answer_markdown"] = preview + str(result["answer_markdown"])
                result["exclude_preview"] = True
        elif (
            result.get("ok")
            and result.get("answer_markdown")
            and re.search(
                r"\b(only\s+show|just\s+show|include|keep\s+only)\b",
                user_text or "",
                flags=re.I,
            )
        ):
            inc_needles: list[str] = []
            if filters.get("party"):
                inc_needles.append(str(filters["party"]))
            for p in filters.get("parties") or []:
                if p:
                    inc_needles.append(str(p))
            for p in filters.get("party_ilike") or []:
                if p:
                    inc_needles.append(str(p))
            if not inc_needles and spec.get("party_query"):
                inc_needles.append(str(spec["party_query"]))
            if inc_needles:
                preview = _build_party_polarity_preview(
                    needles=inc_needles,
                    polarity="include",
                    period_phrase=phrase,
                    date_from=date_from,
                    date_to=date_to,
                    filters={
                        k: v
                        for k, v in filters.items()
                        if k not in PARTY_SCOPE_KEYS
                    },
                    bus=bus,
                )
                if preview:
                    result["answer_markdown"] = preview + str(
                        result["answer_markdown"]
                    )
                    result["include_preview"] = True

        # Ensure follow-up specs retain party scope for PRIOR_QUERY_CONTEXT
        party_bits = {
            k: filters[k]
            for k in PARTY_SCOPE_KEYS
            if filters.get(k) not in (None, "", [])
        }
        if party_bits:
            for spec_key in ("table_spec", "price_spec", "party_spec"):
                if not isinstance(result.get(spec_key), dict):
                    continue
                stamped = dict(result[spec_key])
                f = dict(stamped.get("filters") or {})
                for k, v in party_bits.items():
                    f.setdefault(k, v)
                stamped["filters"] = f
                result[spec_key] = stamped
        from eva_dashboard.query_spec import build_query_state

        result["query_state"] = build_query_state(
            query_spec=filled_spec,
            table_spec=result.get("table_spec"),
            party_spec=result.get("party_spec"),
            price_spec=result.get("price_spec"),
            result_mode=str(result.get("mode") or filled_spec.get("intent") or ""),
        )
        # Phase 4: verify / clarify / multi-hop investigation hints
        from eva_dashboard.agent_loop import apply_verification

        result = apply_verification(result, user_text=user_text)
    return result
