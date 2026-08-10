"""Structured QuerySpec — the model plans; the executor runs.

Prior answer context is shown to the model every turn. Inheritance is explicit
via ``base`` + ``clear`` — never silent sticky merges.
"""

from __future__ import annotations

import json
from typing import Any


INTENTS = {
    "sales_matrix",
    "sales_trend",
    "sales_analytical",
    "party_list",
    "party_rank",
    "party_lookup",
    "price",
    "advanced",
    "overview",
}

FILTER_KEYS = (
    "city",
    "zone",
    "client_type",
    "business_unit",
    "oil_type",
    "packing_category",
    "party",
    "product",
    "active_only",
)


PLAN_QUERY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "plan_query",
        "description": (
            "Plan the data query for this user ask. Emit a complete QuerySpec. "
            "If PRIOR_QUERY_CONTEXT exists and the user is reshaping/comparing/"
            "following up, set base='prior' and use clear[] to drop sticky "
            "filters (e.g. clear city when comparing to other cities). "
            "For a fresh ask, set base='none'. Do NOT invent numbers — this "
            "only plans; the server executes and returns tables."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": sorted(INTENTS),
                    "description": (
                        "sales_* = volume pivots; party_rank = AMS/growth/"
                        "rankings; party_list = who are the parties; "
                        "party_lookup = one named party; price; advanced; overview"
                    ),
                },
                "base": {
                    "type": "string",
                    "enum": ["none", "prior"],
                    "description": (
                        "prior = start from PRIOR_QUERY_CONTEXT then apply "
                        "filters/clear/grain; none = ignore prior filters"
                    ),
                },
                "clear": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(FILTER_KEYS) + ["business_units"],
                    },
                    "description": (
                        "Filter keys to drop from prior (e.g. ['city'] when "
                        "user asks about other cities / nationally)"
                    ),
                },
                "period": {
                    "type": "object",
                    "properties": {
                        "phrase": {"type": "string"},
                        "date_from": {"type": "string"},
                        "date_to": {"type": "string"},
                    },
                },
                "filters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string"},
                        "zone": {"type": "string"},
                        "client_type": {"type": "string"},
                        "business_unit": {"type": "string"},
                        "business_units": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "oil_type": {"type": "string"},
                        "packing_category": {"type": "string"},
                        "party": {"type": "string"},
                        "product": {"type": "string"},
                        "active_only": {"type": "boolean"},
                    },
                },
                "excludes": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "grain": {
                    "type": "object",
                    "properties": {
                        "row_dimension": {"type": "string"},
                        "row_groups": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "column_dimension": {"type": "string"},
                        "months_back": {"type": "integer"},
                        "group_by": {
                            "type": "string",
                            "enum": ["party", "city", "zone"],
                        },
                    },
                },
                "metric": {
                    "type": "string",
                    "description": (
                        "For party_rank: ams|ams_growth|volume|yoy|yoy_ams|"
                        "vs_ams|packing_mix|product_mix|…"
                    ),
                },
                "compare": {"type": "string"},
                "sort": {"type": "string", "enum": ["asc", "desc"]},
                "grown_only": {"type": "boolean"},
                "declined_only": {"type": "boolean"},
                "limit": {"type": "integer"},
                "title_mode": {
                    "type": "string",
                    "enum": [
                        "biggest_gains",
                        "smallest_gains",
                        "biggest_declines",
                        "by_growth",
                    ],
                },
                "advanced_mode": {"type": "string"},
                "party_query": {
                    "type": "string",
                    "description": "Named party for party_lookup",
                },
                "price_flags": {
                    "type": "object",
                    "properties": {
                        "include_price_fetch": {"type": "boolean"},
                        "include_cost_factor": {"type": "boolean"},
                        "factor_breakdown": {"type": "boolean"},
                        "factor_only": {"type": "boolean"},
                    },
                },
                "rationale": {
                    "type": "string",
                    "description": "One short sentence: why this plan answers the user",
                },
            },
            "required": ["intent", "base"],
            "additionalProperties": False,
        },
    },
}


def _compact(d: dict[str, Any] | None) -> dict[str, Any]:
    if not d:
        return {}
    out: dict[str, Any] = {}
    for k, v in d.items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        out[k] = v
    return out


def prior_context_payload(
    *,
    table_spec: dict[str, Any] | None = None,
    party_spec: dict[str, Any] | None = None,
    price_spec: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build the PRIOR_QUERY_CONTEXT dict shown to the model."""
    if party_spec:
        filters = dict(party_spec.get("filters") or {})
        return _compact(
            {
                "source": "party",
                "kind": party_spec.get("kind") or "analyze_parties",
                "intent_hint": (
                    "party_rank"
                    if (party_spec.get("kind") or "") == "analyze_parties"
                    or party_spec.get("metric")
                    else "party_list"
                ),
                "metric": party_spec.get("metric"),
                "group_by": party_spec.get("group_by") or filters.get("group_by"),
                "filters": _compact(filters),
                "period_phrase": party_spec.get("period_phrase"),
                "period": party_spec.get("period"),
            }
        )
    if table_spec:
        filters = dict(table_spec.get("filters") or {})
        return _compact(
            {
                "source": "sales",
                "intent_hint": "sales_matrix",
                "filters": _compact(filters),
                "business_units": list(table_spec.get("business_units") or []) or None,
                "row_dimension": table_spec.get("row_dimension"),
                "row_groups": table_spec.get("row_groups"),
                "column_dimension": table_spec.get("column_dimension"),
                "months_back": table_spec.get("months_back"),
                "period_phrase": table_spec.get("period_phrase"),
                "period": table_spec.get("period"),
                "compare": table_spec.get("compare"),
                "excludes": table_spec.get("excludes"),
            }
        )
    if price_spec:
        return _compact(
            {
                "source": "price",
                "intent_hint": "price",
                "filters": _compact(dict(price_spec.get("filters") or {})),
                "period_phrase": price_spec.get("period_phrase"),
                "period": price_spec.get("period"),
            }
        )
    return None


def prior_context_for_prompt(prior: dict[str, Any] | None) -> str:
    if not prior:
        return (
            "PRIOR_QUERY_CONTEXT: none\n"
            "This is a fresh ask — set base='none' unless the user clearly "
            "refers to a previous table."
        )
    return (
        "PRIOR_QUERY_CONTEXT (the last answer the user can Reply on):\n"
        f"{json.dumps(prior, indent=2, default=str)}\n\n"
        "Rules:\n"
        "- Fresh complete ask → base='none'.\n"
        "- Follow-up / reshape / 'this growth' / 'compared to other…' → "
        "base='prior', keep useful filters (e.g. client_type), and clear[] "
        "anything that contradicts the new ask.\n"
        "- 'other cities' / city league → intent=party_rank, group_by=city, "
        "clear=['city'], keep metric (often ams_growth).\n"
        "- 'least/lowest gains' → sort=asc, grown_only=false, "
        "title_mode=smallest_gains.\n"
        "- Brand Eva sales → intent=sales_*, business_units=["
        "Eva Consumer, Eva Bulk] only (never Shortening / other BUs).\n"
        "- Brand Maan sales → [Maan Consumer, Maan Bulk] only.\n"
    )


def normalize_query_spec(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce model JSON into a safe QuerySpec."""
    raw = dict(raw or {})
    intent = str(raw.get("intent") or "sales_matrix").strip().lower()
    if intent not in INTENTS:
        # Map common aliases
        aliases = {
            "sales": "sales_matrix",
            "matrix": "sales_matrix",
            "trend": "sales_trend",
            "analytical": "sales_analytical",
            "list_clients": "party_list",
            "analyze_parties": "party_rank",
            "lookup_party": "party_lookup",
            "query_price": "price",
            "query_sales": "sales_matrix",
            "advanced_query": "advanced",
        }
        intent = aliases.get(intent, "sales_matrix")
    base = str(raw.get("base") or "none").strip().lower()
    if base not in {"none", "prior"}:
        base = "none"
    clear = [str(c) for c in (raw.get("clear") or []) if c]
    filters = dict(raw.get("filters") or {})
    grain = dict(raw.get("grain") or {})
    period = dict(raw.get("period") or {})
    # Flatten period.phrase convenience
    if raw.get("period_phrase") and not period.get("phrase"):
        period["phrase"] = raw.get("period_phrase")
    return {
        "intent": intent,
        "base": base,
        "clear": clear,
        "period": period,
        "filters": filters,
        "excludes": dict(raw.get("excludes") or {}),
        "grain": grain,
        "metric": raw.get("metric"),
        "compare": raw.get("compare"),
        "sort": raw.get("sort") or "desc",
        "grown_only": bool(raw.get("grown_only") or False),
        "declined_only": bool(raw.get("declined_only") or False),
        "limit": int(raw.get("limit") or 0) or None,
        "title_mode": raw.get("title_mode"),
        "advanced_mode": raw.get("advanced_mode"),
        "party_query": raw.get("party_query") or filters.get("party"),
        "price_flags": dict(raw.get("price_flags") or {}),
        "rationale": raw.get("rationale") or "",
        "business_units": list(
            filters.get("business_units") or raw.get("business_units") or []
        ),
    }


def merge_prior_into_spec(
    spec: dict[str, Any],
    prior: dict[str, Any] | None,
) -> dict[str, Any]:
    """Apply base=prior: start from prior filters/metric/grain, then patches + clear."""
    out = dict(spec)
    if out.get("base") != "prior" or not prior:
        return out
    prior_filters = dict(prior.get("filters") or {})
    merged_filters = dict(prior_filters)
    # Patches from spec overwrite
    for k, v in (spec.get("filters") or {}).items():
        if v is not None and v != "":
            merged_filters[k] = v
    # Explicit clears win
    for key in spec.get("clear") or []:
        merged_filters.pop(key, None)
        if key == "business_units":
            out["business_units"] = []
        if key == "business_unit":
            merged_filters.pop("business_unit", None)
    out["filters"] = merged_filters

    # Inherit metric / group_by / period when omitted
    if not out.get("metric") and prior.get("metric"):
        out["metric"] = prior.get("metric")
    grain = dict(out.get("grain") or {})
    if not grain.get("group_by") and prior.get("group_by"):
        grain["group_by"] = prior.get("group_by")
    if not grain.get("row_dimension") and prior.get("row_dimension"):
        grain["row_dimension"] = prior.get("row_dimension")
    if not grain.get("column_dimension") and prior.get("column_dimension"):
        grain["column_dimension"] = prior.get("column_dimension")
    if grain.get("months_back") is None and prior.get("months_back") is not None:
        grain["months_back"] = prior.get("months_back")
    out["grain"] = grain

    period = dict(out.get("period") or {})
    if not period.get("phrase") and not period.get("date_from"):
        if prior.get("period_phrase"):
            period["phrase"] = prior.get("period_phrase")
        elif isinstance(prior.get("period"), dict):
            p = prior["period"]
            period.setdefault("date_from", p.get("date_from"))
            period.setdefault("date_to", p.get("date_to"))
    out["period"] = period

    if not out.get("business_units") and prior.get("business_units"):
        if "business_units" not in (spec.get("clear") or []):
            out["business_units"] = list(prior.get("business_units") or [])

    if not out.get("excludes") and prior.get("excludes"):
        out["excludes"] = dict(prior.get("excludes") or {})

    # City grain without a named city ⇒ clear city (safety)
    if (grain.get("group_by") or "") == "city" and not merged_filters.get("city"):
        merged_filters.pop("city", None)
        out["filters"] = merged_filters
    if (grain.get("group_by") or "") == "city" and "city" not in (
        spec.get("clear") or []
    ):
        # If user asked city league, city filter must not remain
        if not (spec.get("filters") or {}).get("city"):
            merged_filters.pop("city", None)
            out["filters"] = merged_filters

    return out
