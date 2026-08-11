"""Blind execution of a QuerySpec — no silent intent mutation.

The LLM is the sole source of query parameters. This module:
1. Normalizes / validates the plan
2. Merges prior only when base/context_handling='prior' + clear[]
3. Maps period_type → date windows (deterministic)
4. Runs engines
5. On invalid plans, returns ok=False + errors for the LLM to self-correct
"""

from __future__ import annotations

from typing import Any

from eva_dashboard.client_language import (
    extract_oil_and_packing,
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
from eva_dashboard.query_spec import (
    merge_prior_into_spec,
    normalize_query_spec,
    resolve_period_from_spec,
    validate_query_spec,
)
from eva_dashboard.sales_query import query_price, query_sales


def _canon_filters(filters: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize provided filter values only — never invent filters."""
    out = dict(filters or {})
    if out.get("city"):
        out["city"] = str(out["city"]).strip()
    if out.get("zone"):
        out["zone"] = normalize_zone(out.get("zone"))
    if out.get("client_type"):
        out["client_type"] = normalize_client_type(out.get("client_type"))

    oil = out.get("oil_type")
    pack = out.get("packing_category")
    # Defensive: composite phrases left in oil_type / product → split
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
        # Composite spoken product is not an exact SKU — drop product filter
        if o2 and p2 and " " in str(product).strip():
            # Keep exact SKU when it looks like a real product name with size codes
            if not any(ch.isdigit() for ch in str(product)):
                out.pop("product", None)

    if oil:
        out["oil_type"] = normalize_oil_type(oil)
    if pack:
        out["packing_category"] = normalize_packing_category(pack)
    return out


def execute_query_spec(
    raw_spec: dict[str, Any],
    *,
    prior: dict[str, Any] | None = None,
    user_text: str = "",  # kept for API compat; NOT used to mutate the plan
) -> dict[str, Any]:
    """Execute a planned QuerySpec blindly.

    ``user_text`` is ignored for planning — the LLM owns intent. Invalid specs
    return ``{ok: False, error, plan_errors}`` so the model can retry.
    """
    del user_text  # explicit: no spoken-text mutation
    spec = normalize_query_spec(raw_spec)
    spec = merge_prior_into_spec(spec, prior)

    # Resolve period_type → phrase / month grain (deterministic semantic layer)
    resolved = resolve_period_from_spec(spec)
    spec["period"] = resolved["period"]
    spec["grain"] = resolved["grain"]
    if resolved.get("months_back") is not None:
        spec["months_back"] = resolved["months_back"]

    errors = validate_query_spec(spec, prior=prior)
    if errors:
        return {
            "ok": False,
            "error": "Incomplete QuerySpec — fix and call plan_query again.",
            "plan_errors": errors,
            "query_spec": spec,
            "response_instructions": (
                "REQUIRED: Call plan_query again with a complete QuerySpec. "
                "Address every plan_errors item. Do not invent numbers."
            ),
        }

    filters = _canon_filters(spec.get("filters") or {})
    grain = dict(spec.get("grain") or {})
    period = dict(spec.get("period") or {})
    intent = spec["intent"]
    bus = list(spec.get("business_units") or filters.get("business_units") or [])
    if filters.get("business_unit") and not bus:
        bus = [filters["business_unit"]]

    phrase = (period.get("phrase") or "").strip() or None
    date_from = period.get("date_from")
    date_to = period.get("date_to")

    result: dict[str, Any]
    if intent in {"sales_matrix", "sales_trend", "sales_analytical"}:
        mode = {
            "sales_matrix": "matrix",
            "sales_trend": "trend",
            "sales_analytical": "analytical",
        }[intent]
        # LAST_N_MONTHS semantic → month columns (already set in resolve_period)
        columns = grain.get("column_dimension") or "client_type"
        # Row grain from group_by / row_dimension when it is a sales dimension
        row_dim = grain.get("row_dimension")
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
            columns=columns,
            months_back=int(spec.get("months_back") or grain.get("months_back") or 6),
            row_dimension=row_dim,
            row_groups=list(grain.get("row_groups") or []) or None,
            excludes=spec.get("excludes") or None,
            mode=mode,
            compare=spec.get("compare"),
            active_only=bool(filters.get("active_only")),
            prior_spec=None,
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
                months_back=int(spec.get("months_back") or grain.get("months_back") or 6),
                mode="matrix",
            )
        if result.get("ok") is False:
            result = lookup_party(q, limit=int(spec.get("limit") or 10))
    elif intent == "price":
        flags = spec.get("price_flags") or {}
        time_grain = str(
            grain.get("time_grain") or (spec.get("time_grain") or "none")
        ).strip().lower()
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
            time_grain=time_grain if time_grain in {"month"} else None,
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
            "",
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
        filled_spec = dict(spec)
        filled_spec.pop("_clear_omitted", None)
        filled_spec["period"] = period
        filled_spec["grain"] = grain
        filled_spec["filters"] = filters
        filled_spec["business_units"] = bus
        result["query_spec"] = filled_spec
        result.setdefault("ok", True)
    return result


