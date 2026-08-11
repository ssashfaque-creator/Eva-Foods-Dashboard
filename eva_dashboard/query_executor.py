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
    extract_oil_and_packing,
    match_client_type_alias,
    normalize_client_type,
    normalize_oil_type,
    normalize_packing_category,
)
from eva_dashboard.geo import normalize_zone
from eva_dashboard.party_analytics import (
    analyze_parties,
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


def _coerce_vocab_from_user_text(
    spec: dict[str, Any], user_text: str
) -> dict[str, Any]:
    """Hard safety nets: SKU→product, product→packing_category, price_fetch."""
    out = dict(spec)
    t = (user_text or "").lower()
    if not t.strip():
        return out

    rows = list(out.get("row_dimensions") or [])
    metrics = list(out.get("metrics") or [])
    cols = list(out.get("column_dimensions") or [])

    has_sku = bool(
        re.search(
            r"\bskus?\b|\bsku[-\s]?wise\b|\bitems?\b|\bitem[-\s]?wise\b|"
            r"\bby\s+sku\b|\bsku\s+break",
            t,
        )
    )
    has_product_spoken = bool(
        re.search(
            r"\bproduct[-\s]?wise\b|\bby\s+products?\b|"
            r"\bproducts?\s+(break|breakup|mix|layer)\b",
            t,
        )
    )

    if has_sku:
        rows = ["product" if r == "packing_category" else r for r in rows]
        if "product" not in rows:
            rows.append("product")
        # SKU breakup + price fetch → not a monthly trend
        if "price_fetch" in metrics or re.search(r"price\s*fetch|cost\s*factor", t):
            cols = [c for c in cols if c != "month"]
    elif has_product_spoken:
        rows = ["packing_category" if r == "product" else r for r in rows]
        if "packing_category" not in rows:
            rows.append("packing_category")

    # Pure cost-factor asks stay on factor_costs (do not force Price Fetch metric)
    if not _is_factor_only_ask(t, metrics=metrics, flags=out.get("price_flags")) and re.search(
        r"price\s*fetch|oil\s*price\s*fetched|apply\s+the\s+cost\s+factor|"
        r"what.?s\s+the\s+cost\s+factor|cost\s+factor",
        t,
    ):
        if "price_fetch" not in metrics:
            metrics.append("price_fetch")
        # Dedicated PF path — drop accidental month columns
        if has_sku or "product" in rows or "party" in rows:
            cols = [c for c in cols if c != "month"]

    # Channel words → client_type (never customer ILIKE)
    filters = dict(out.get("filters") or {})
    if not filters.get("client_type"):
        party_raw = filters.get("party") or out.get("party_query")
        parties = list(filters.get("parties") or [])
        redirected = False
        if party_raw:
            ct = match_client_type_alias(str(party_raw))
            if ct:
                filters["client_type"] = ct
                filters.pop("party", None)
                out.pop("party_query", None)
                redirected = True
        if parties:
            kept: list[str] = []
            for p in parties:
                ct = match_client_type_alias(str(p))
                if ct and not filters.get("client_type"):
                    filters["client_type"] = ct
                    redirected = True
                elif not ct:
                    kept.append(str(p))
            if kept:
                filters["parties"] = kept
            else:
                filters.pop("parties", None)
                redirected = True
        # Do NOT invent client_type from free user_text — that breaks blind
        # execute. Channel words must come from filters.party / parties /
        # extracted_entities / filters.client_type (then we redirect/normalize).
        if redirected:
            out["filters"] = filters

    out["row_dimensions"] = rows
    out["metrics"] = metrics
    out["column_dimensions"] = cols
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


def execute_query_spec(
    raw_spec: dict[str, Any],
    *,
    prior: dict[str, Any] | None = None,
    user_text: str = "",
) -> dict[str, Any]:
    """Execute a planned QuerySpec blindly (universal pivot or legacy)."""
    spec = normalize_query_spec(raw_spec)
    spec = merge_prior_into_spec(spec, prior)
    # Python entity resolution BEFORE validation (forgiving extracted_entities)
    spec = _apply_extracted_entities(spec)
    # Spoken vocab safety nets (SKU / product / price_fetch)
    spec = _coerce_vocab_from_user_text(spec, user_text)
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
            zone=filters.get("zone"),
            business_unit=bu or filters.get("business_unit"),
            business_units=bus_param,
            oil_type=filters.get("oil_type"),
            packing_category=filters.get("packing_category"),
            client_type=filters.get("client_type"),
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
    return result
