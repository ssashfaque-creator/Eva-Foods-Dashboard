"""Deterministic execution of a QuerySpec against existing query engines."""

from __future__ import annotations

from typing import Any

from eva_dashboard.client_language import (
    extract_client_type_from_text,
    extract_oil_type_from_text,
    extract_packing_from_text,
    is_distributor_party_grain,
    normalize_client_type,
    normalize_oil_type,
    normalize_packing_category,
)
from eva_dashboard.geo import extract_zone_from_text, normalize_zone
from eva_dashboard.party_analytics import (
    analyze_parties,
    extract_city_from_text,
    list_clients,
    lookup_party,
    party_sales,
)
from eva_dashboard.query_spec import merge_prior_into_spec, normalize_query_spec
from eva_dashboard.sales_query import query_price, query_sales


def _canon_filters(filters: dict[str, Any]) -> dict[str, Any]:
    out = dict(filters or {})
    if out.get("city"):
        # Keep spoken city as-is; extractors already canonicalize when used
        out["city"] = str(out["city"]).strip()
    if out.get("zone"):
        out["zone"] = normalize_zone(out.get("zone"))
    if out.get("client_type"):
        out["client_type"] = normalize_client_type(out.get("client_type"))
    if out.get("oil_type"):
        out["oil_type"] = normalize_oil_type(out.get("oil_type"))
    if out.get("packing_category"):
        out["packing_category"] = normalize_packing_category(
            out.get("packing_category")
        )
    return out


def _fill_spoken_period_and_month_grain(
    *,
    user_text: str,
    period: dict[str, Any],
    grain: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fill blank period/grain from spoken text — never override explicit values.

    Critical for plan_query: the model often sets city/client_type/BUs but omits
    ``period`` / ``grain.column_dimension``. An empty period then falls through
    ``resolve_period(None)`` → current data month MTD (e.g. Aug 2026 MTD),
    wiping an explicit user window like "last 6 months".
    """
    from eva_dashboard.chatbot import (
        _extract_period_phrase,
        _looks_month_wise,
        _months_back_from_text,
    )

    period = dict(period or {})
    grain = dict(grain or {})
    phrase = (period.get("phrase") or "").strip() or None
    date_from = period.get("date_from")
    date_to = period.get("date_to")

    # Period: fill only when the plan left phrase and ISO bounds blank.
    if user_text and not phrase and not date_from and not date_to:
        spoken = _extract_period_phrase(user_text)
        if spoken:
            period["phrase"] = spoken

    # last N months / month-wise → month columns (only when grain blank).
    # columns=month ignores a wrong single-month period phrase and uses months_back.
    if user_text and not grain.get("column_dimension") and _looks_month_wise(user_text):
        grain["column_dimension"] = "month"
        if grain.get("months_back") is None:
            grain["months_back"] = _months_back_from_text(user_text, 6)

    return period, grain


def execute_query_spec(
    raw_spec: dict[str, Any],
    *,
    prior: dict[str, Any] | None = None,
    user_text: str = "",
) -> dict[str, Any]:
    """Run a planned QuerySpec. No silent sticky merges outside base/clear."""
    spec = normalize_query_spec(raw_spec)
    spec = merge_prior_into_spec(spec, prior)
    filters = _canon_filters(spec.get("filters") or {})
    grain = dict(spec.get("grain") or {})
    period = dict(spec.get("period") or {})
    intent = spec["intent"]
    bus = list(spec.get("business_units") or filters.get("business_units") or [])
    if filters.get("business_unit") and not bus:
        bus = [filters["business_unit"]]

    # Helpers ONLY fill blanks the model left empty from spoken text.
    # Never invent Eva Distributors from "distributor-wise" grain language.
    # Never override an explicit period / grain / business_units the plan set.
    if user_text:
        if not filters.get("city"):
            filters["city"] = extract_city_from_text(user_text)
        if not filters.get("zone"):
            filters["zone"] = normalize_zone(extract_zone_from_text(user_text))
        if not filters.get("client_type") and not is_distributor_party_grain(
            user_text
        ):
            filters["client_type"] = normalize_client_type(
                extract_client_type_from_text(user_text)
            )
        if not filters.get("oil_type"):
            filters["oil_type"] = extract_oil_type_from_text(user_text)
        if not filters.get("packing_category"):
            filters["packing_category"] = extract_packing_from_text(user_text)
        # Grain language must never keep a sticky Eva Distributors channel.
        if is_distributor_party_grain(user_text):
            filters["client_type"] = None
        period, grain = _fill_spoken_period_and_month_grain(
            user_text=user_text, period=period, grain=grain
        )
        if not bus and not filters.get("business_unit"):
            from eva_dashboard.chatbot import _extract_business_units_from_text

            spoken_bus = _extract_business_units_from_text(user_text)
            if spoken_bus:
                bus = list(spoken_bus)

    phrase = (period.get("phrase") or "").strip() or None
    date_from = period.get("date_from")
    date_to = period.get("date_to")

    # Safety: city league cannot keep a city filter
    if (grain.get("group_by") or "") == "city":
        # Unless the user named that single city as the subject of a city-rank
        # inside a larger ask — for "other cities" we already cleared.
        if "city" in (spec.get("clear") or []) or not (
            raw_spec.get("filters") or {}
        ).get("city"):
            if not extract_city_from_text(user_text):
                filters["city"] = None

    result: dict[str, Any]
    if intent in {"sales_matrix", "sales_trend", "sales_analytical"}:
        mode = {
            "sales_matrix": "matrix",
            "sales_trend": "trend",
            "sales_analytical": "analytical",
        }[intent]
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
            party=filters.get("party"),
            columns=grain.get("column_dimension") or "client_type",
            months_back=int(grain.get("months_back") or 6),
            row_dimension=grain.get("row_dimension"),
            row_groups=list(grain.get("row_groups") or []) or None,
            excludes=spec.get("excludes") or None,
            mode=mode,
            compare=spec.get("compare"),
            active_only=bool(filters.get("active_only")),
            prior_spec=None,  # never silent sticky
        )
    elif intent == "party_list":
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
    elif intent == "party_rank":
        group_by = grain.get("group_by") or "party"
        limit = int(spec.get("limit") or (25 if group_by in {"city", "zone"} else 25))
        # Multi-BU brand scope (Eva Consumer+Bulk) via brand filter — do not
        # silently drop prior Eva scope when the plan carried both units.
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
            metric=str(spec.get("metric") or "ams"),
            group_by=group_by,
            sort=str(spec.get("sort") or "desc"),
            grown_only=bool(spec.get("grown_only")),
            declined_only=bool(spec.get("declined_only")),
            limit=limit,
            active_only=bool(filters.get("active_only")),
            title_mode=spec.get("title_mode"),
            mix_dimension=grain.get("mix_dimension"),
        )
    elif intent == "party_lookup":
        q = spec.get("party_query") or filters.get("party") or user_text
        if phrase:
            result = party_sales(
                query=q, period=phrase, columns="city", mode="trend"
            )
        else:
            result = party_sales(
                query=q,
                period=None,
                columns="month",
                months_back=int(grain.get("months_back") or 6),
                mode="matrix",
            )
        if result.get("ok") is False:
            result = lookup_party(q, limit=int(spec.get("limit") or 10))
    elif intent == "price":
        flags = spec.get("price_flags") or {}
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
            include_price_fetch=bool(flags.get("include_price_fetch")),
            include_cost_factor=bool(flags.get("include_cost_factor")),
            factor_breakdown=bool(flags.get("factor_breakdown")),
        )
    elif intent == "advanced":
        from eva_dashboard.chatbot import _dispatch_advanced

        result = _dispatch_advanced(
            {
                "mode": spec.get("advanced_mode"),
                **filters,
                "period": phrase,
                "date_from": date_from,
                "date_to": date_to,
            },
            user_text,
            prior_spec=None,
        )
    elif intent == "overview":
        from eva_dashboard.chatbot import sales_overview

        result = sales_overview()
    else:
        result = {
            "ok": False,
            "error": f"Unsupported intent: {intent}",
        }

    if isinstance(result, dict):
        result = dict(result)
        # Persist filled period/grain so follow-ups see what was actually run.
        filled_spec = dict(spec)
        filled_spec["period"] = period
        filled_spec["grain"] = grain
        filled_spec["filters"] = filters
        result["query_spec"] = filled_spec
        result.setdefault("ok", True)
    return result


def heuristic_plan_query(
    user_text: str,
    *,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Offline planner for tests / fallback when the model skips plan_query.

    Uses vocabulary + reshape — still emits an explicit QuerySpec (no mute sticky).
    """
    from eva_dashboard.analytics_reshape import resolve_analytics_reshape
    from eva_dashboard.chatbot import (
        _extract_business_units_from_text,
        _looks_named_party_sales,
        _looks_price_query,
        _looks_scoped_performance_sales,
        suggest_preferred_tool,
    )
    from eva_dashboard.party_analytics import infer_party_analytics_from_text
    from eva_dashboard.advanced_routing import looks_advanced, infer_advanced_from_text

    preferred = suggest_preferred_tool(user_text)
    inf = infer_party_analytics_from_text(user_text)
    text_l = (user_text or "").lower()

    # Map preferred tool → intent
    intent_map = {
        "query_sales": "sales_analytical"
        if _looks_scoped_performance_sales(user_text)
        else "sales_matrix",
        "analyze_parties": "party_rank",
        "list_clients": "party_list",
        "lookup_party": "party_lookup",
        "query_price": "price",
        "advanced_query": "advanced",
    }
    intent = intent_map.get(preferred, "sales_matrix")
    if looks_advanced(user_text):
        intent = "advanced"

    grain_distributors = is_distributor_party_grain(user_text)
    is_followup = bool(prior) and (
        "this growth" in text_l
        or "compared" in text_l
        or "other cities" in text_l
        or "other zones" in text_l
        or "same format" in text_l
        or "show this" in text_l
        or "this distributor" in text_l
        or grain_distributors
        or text_l.strip().startswith("[follow-up")
    )
    # "distributor wise / lowest performing distributors" after a sales table
    # is a party rank reshape of that table — not a fresh channel list.
    if grain_distributors and (
        "lowest" in text_l
        or "worst" in text_l
        or "performing" in text_l
        or "show this" in text_l
        or bool(prior)
    ):
        intent = "party_rank"
    base = "prior" if is_followup and prior else "none"

    reshape = resolve_analytics_reshape(
        user_text,
        arguments={},
        inferred=inf,
        prior_party_spec=prior if (prior or {}).get("source") == "party" else None,
        prior_ctx=dict((prior or {}).get("filters") or {}),
    )

    filters: dict[str, Any] = {
        "city": inf.get("city"),
        "zone": inf.get("zone"),
        "client_type": None if grain_distributors else inf.get("client_type"),
        "business_unit": inf.get("business_unit"),
        "oil_type": inf.get("oil_type"),
        "packing_category": inf.get("packing_category"),
    }
    bus = _extract_business_units_from_text(user_text)
    # Carry prior Eva/Maan multi-BU when follow-up didn't rename the brand.
    if not bus and base == "prior" and prior:
        bus = list(prior.get("business_units") or [])
        if not bus:
            pbu = (prior.get("filters") or {}).get("business_unit")
            if pbu:
                bus = [pbu]
    clear: list[str] = []
    if reshape.get("clear_city"):
        clear.append("city")
        filters["city"] = None
    if reshape.get("clear_zone"):
        clear.append("zone")
        filters["zone"] = None
    if grain_distributors:
        # Explicitly drop a sticky channel so party grain can see all buyers
        # of the prior brand scope (e.g. Eva Consumer+Bulk).
        clear.append("client_type")
        filters["client_type"] = None

    grain: dict[str, Any] = {}
    if intent == "party_rank":
        grain["group_by"] = reshape.get("group_by") or inf.get("group_by") or "party"
    if intent.startswith("sales"):
        from eva_dashboard.chatbot import _looks_month_wise, _months_back_from_text

        # last N months / month-wise → month grid (not a single-month analytical pack)
        if _looks_month_wise(user_text) or (
            "month" in text_l and "wise" in text_l
        ):
            grain["column_dimension"] = "month"
            grain["months_back"] = _months_back_from_text(user_text, 6)
            # Month columns force matrix mode in query_sales; prefer matrix intent.
            if intent == "sales_analytical":
                intent = "sales_matrix"
        if "city wise" in text_l or "city-wise" in text_l:
            grain["row_dimension"] = "city"
        if "channel" in text_l:
            grain["row_dimension"] = "client_type"

    metric = reshape.get("metric") or inf.get("metric")
    if intent == "party_rank" and not metric:
        metric = "ams"
    sort = reshape.get("sort") or inf.get("sort") or "desc"
    title_mode = reshape.get("title_mode") or inf.get("title_mode")

    # Prefer spoken period (incl. last N months); fall back to party-analytics infer.
    from eva_dashboard.chatbot import _extract_period_phrase

    period_phrase = _extract_period_phrase(user_text) or inf.get("period")

    spec: dict[str, Any] = {
        "intent": intent,
        "base": base,
        "clear": clear,
        "filters": {k: v for k, v in filters.items() if v is not None},
        "grain": grain,
        "metric": metric,
        "sort": sort,
        "grown_only": bool(reshape.get("grown_only") or False),
        "declined_only": bool(reshape.get("declined_only") or False),
        "title_mode": title_mode,
        "business_units": bus,
        "period": {"phrase": period_phrase} if period_phrase else {},
        "rationale": f"heuristic plan via {preferred}",
    }
    if intent == "party_lookup" and _looks_named_party_sales(user_text):
        from eva_dashboard.chatbot import _extract_named_party_query

        spec["party_query"] = _extract_named_party_query(user_text)
    if intent == "advanced":
        adv = infer_advanced_from_text(user_text)
        spec["advanced_mode"] = adv.get("mode")
    if intent == "price" and _looks_price_query(user_text):
        spec["price_flags"] = {"include_price_fetch": "price fetch" in text_l}

    return normalize_query_spec(spec)
