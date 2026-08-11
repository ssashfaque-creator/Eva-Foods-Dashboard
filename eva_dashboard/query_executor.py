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
    extract_oil_and_packing,
    match_client_type_alias,
    normalize_client_type,
    normalize_oil_type,
    normalize_packing_category,
)
from eva_dashboard.geo import normalize_zone
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


def _apply_extracted_entities(spec: dict[str, Any]) -> dict[str, Any]:
    """Merge Python-resolved extracted_entities into filters / business_units.

    Channel aliases (metro, LMT, chase up, …) → client_type.
    Remaining unresolved names → silent party ILIKE (e.g. \"al shaheer\").
    """
    out = dict(spec)
    resolved = resolve_extracted_entities(list(out.get("extracted_entities") or []))
    filters = dict(out.get("filters") or {})
    bus = list(out.get("business_units") or filters.get("business_units") or [])

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


def _spoken_wise_dimension(user_text: str) -> str | None:
    """Detect explicit X-wise / by-X grain from the user sentence."""
    t = (user_text or "").lower()
    if not t.strip():
        return None
    # Prefer more specific product grains before geo/channel
    patterns: list[tuple[str, str]] = [
        (
            r"\bskus?\b|\bsku[-\s]?wise\b|\bitems?\b|\bitem[-\s]?wise\b|"
            r"\bby\s+sku\b|\bsku\s+break",
            "product",
        ),
        (
            r"\bproduct[-\s]?wise\b|\bby\s+products?\b|"
            r"\bproducts?\s+(break|breakup|mix|layer)\b",
            "packing_category",
        ),
        (
            r"\b("
            r"city[- ]?wise|citywide|city\s+wide|by\s+city|"
            r"cities\s+wise|show\s+city\s+wise|cities?\s+break(?:up|down)?"
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
            r"by\s+channels?|all\s+channels?"
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
            r"\b(party[- ]?wise|distributor[- ]?wise|by\s+part(y|ies)|"
            r"by\s+distributors?)\b",
            "party",
        ),
    ]
    for pat, dim in patterns:
        if re.search(pat, t):
            return dim
    return None


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

    wise_dim = _spoken_wise_dimension(t)
    has_sku = wise_dim == "product"
    has_product_spoken = wise_dim == "packing_category" and bool(
        re.search(
            r"\bproduct[-\s]?wise\b|\bby\s+products?\b|"
            r"\bproducts?\s+(break|breakup|mix|layer)\b|"
            r"\bthis\s+product\s+wise\b",
            t,
        )
    )
    nest_leaf_dims = {"packing_category", "product", "business_unit", "oil_type"}
    outer_dims = {"city", "zone", "client_type", "party", "business_unit"}

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

        if leaf in nest_leaf_dims and (prior_outers or current_outers):
            outers = prior_outers or current_outers
            # Preserve order, unique
            seen: set[str] = set()
            outers_u = []
            for r in outers:
                if r not in seen and r != leaf:
                    seen.add(r)
                    outers_u.append(r)
            rows = outers_u + [leaf]
            # Keep multi-filters that scoped the prior table
            if prior:
                pf = dict(prior.get("filters") or {})
                for key in ("cities", "client_types", "city", "client_type", "zone"):
                    if pf.get(key) and not filters.get(key):
                        filters[key] = pf[key]
        elif leaf in outer_dims:
            if add_layer and rows and leaf not in rows:
                rows = [leaf] + [r for r in rows if r != leaf]
            else:
                # Fresh cut on city/zone/channel — don't bury under packing
                rows = [leaf]
            if leaf == "city":
                # city-wise across all cities: drop single city lock unless multi named
                if not filters.get("cities"):
                    filters.pop("city", None)
            if leaf == "zone":
                filters.pop("zone", None)
            if leaf == "client_type" and not filters.get("client_types"):
                # channel-wise across channels
                filters.pop("client_type", None)
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

    out["filters"] = filters
    out["row_dimensions"] = rows
    out["metrics"] = metrics
    out["column_dimensions"] = cols
    if clear:
        out["clear_filters"] = clear
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

    Even when the model forgets ``context_handling='prior'``, short asks like
    \"what's the price\" / \"% of AMS\" / \"last purchase\" after a named-party
    answer should not lose the customer scope.
    """
    out = dict(spec)
    scope = _prior_party_scope(prior)
    if not scope:
        return out
    filters = dict(out.get("filters") or {})
    clear = set(out.get("clear") or [])
    if clear & set(PARTY_SCOPE_KEYS):
        return out
    if any(filters.get(k) not in (None, "", []) for k in PARTY_SCOPE_KEYS):
        return out
    stick = out.get("base") == "prior" or _looks_party_metric_followup(user_text)
    if not stick:
        return out
    for key, val in scope.items():
        filters[key] = val
    out["filters"] = filters
    if not out.get("party_query") and filters.get("party"):
        out["party_query"] = filters["party"]
    # Promote to prior merge semantics so period/BU inherit when model omitted them
    if out.get("base") != "prior" and _looks_party_metric_followup(user_text):
        out["base"] = "prior"
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
            r"give\s+me\s+(a\s+)?(full\s+)?(picture|profile|rundown)\s+(on|of|for)|"
            r"how\s+is\s+.+\s+doing\b|"
            r"everything\s+about"
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
    spec = normalize_query_spec(raw_spec)
    # Soft-stick party scope before merge so short follow-ups keep the customer
    spec = _stick_party_scope_from_prior(spec, prior, user_text=user_text)
    spec = merge_prior_into_spec(spec, prior)
    # Re-apply after merge in case merge cleared then model omitted party
    spec = _stick_party_scope_from_prior(spec, prior, user_text=user_text)
    # Python entity resolution BEFORE validation (forgiving extracted_entities)
    spec = _apply_extracted_entities(spec)
    # Spoken vocab safety nets (SKU / product / price_fetch)
    spec = _coerce_vocab_from_user_text(spec, user_text, prior=prior)
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
            # SPECIFIC_MONTH cleared month columns — keep grain non-month
            if grain.get("column_dimension") == "month":
                grain["column_dimension"] = "client_type"
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

    errors = validate_query_spec(spec, prior=prior)
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

    filters = _canon_filters(spec.get("filters") or {})
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
        # "who is al shaheer" — keep match list UI via lookup_party
        q = spec.get("party_query") or filters.get("party") or ""
        if phrase:
            result = party_sales(
                query=q, period=phrase, columns="city", mode="trend"
            )
        else:
            result = party_sales(
                query=q,
                period=None,
                columns="month",
                months_back=mb,
                mode="matrix",
            )
        if result.get("ok") is False:
            result = lookup_party(q, limit=int(spec.get("limit") or 10))
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
    elif intent == "party_rank" or (
        set(metrics) & {"vs_ams", "ams_growth"} and "month" not in column_dimensions
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
    elif "price_fetch" in metrics or "avg_price" in metrics:
        flags = dict(spec.get("price_flags") or {})
        want_fetch = (
            "price_fetch" in metrics
            or bool(flags.get("include_price_fetch"))
            or bool(flags.get("include_cost_factor"))
            or bool(flags.get("factor_breakdown"))
        )
        # Dedicated Price Fetch path — never monthly trend / matrix HTML
        if want_fetch:
            pf_rows = [d for d in row_dimensions if d != "month"]
            has_party_scope = bool(
                party_kw.get("party")
                or party_kw.get("parties")
                or party_kw.get("party_ilike")
            )
            if not pf_rows:
                # SKU breakup default when party scoped or user asked for fetch
                pf_rows = ["product"] if has_party_scope else []
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
            # Plain avg_price pivot (no cost factor)
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
    elif intent in {"sales_matrix", "sales_trend", "sales_analytical"} or (
        set(metrics) & {"volume", "ams"}
    ):
        mode = {
            "sales_matrix": "matrix",
            "sales_trend": "trend",
            "sales_analytical": "analytical",
        }.get(intent, "trend" if "month" in column_dimensions else "matrix")
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
    return result
