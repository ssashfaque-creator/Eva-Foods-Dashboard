"""Semantic Planner QuerySpec — LLM plans; Python executes blindly.

Division of responsibility:
- LLM: vocabulary, disambiguation, follow-up clear[], sort/metric choice
- Python: MT math, AMS windows, joins, zone mapping, date bounds

No silent mutation of a valid plan. Invalid plans return errors for the LLM
to self-correct.
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

PERIOD_TYPES = {
    "MTD",
    "LAST_N_MONTHS",
    "LAST_MONTH",
    "LAST_WEEK",
    "NAMED_MONTH",
    "SPECIFIC_MONTH",
    "CUSTOM_DATE",
}

FILTER_KEYS = (
    "city",
    "cities",
    "zone",
    "client_type",
    "client_types",
    "business_unit",
    "oil_type",
    "packing_category",
    "party",
    "parties",
    "party_ilike",
    "product",
    "active_only",
)

# Keys that define a named-customer scope (sticky across party follow-ups).
PARTY_SCOPE_KEYS = ("party", "parties", "party_ilike")

GROUP_BY_DIMS = (
    "city",
    "zone",
    "party",
    "business_unit",
    "packing_category",
    "product",
    "oil_type",
    "client_type",
)

ROW_DIMENSIONS = GROUP_BY_DIMS
COLUMN_DIMENSIONS = (
    "month",
    "client_type",
    "business_unit",
    "city",
    "oil_type",
    "packing_category",
)
PIVOT_METRICS = (
    "volume",
    "avg_price",
    "price_fetch",
    "ams",
    "vs_ams",
    "ams_growth",
)
OPERATIONS = (
    "pivot",
    "party_list",
    "party_lookup",
    "overview",
    "advanced",
)


PLAN_QUERY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "plan_query",
        "description": (
            "Universal pivot planner: translate ANY analytical ask into rows, "
            "columns, and metrics. The server executes blindly. "
            "Example — customer-wise price trends → "
            "row_dimensions=['party'], column_dimensions=['month'], "
            "metrics=['avg_price']. "
            "Follow-ups: context_handling='prior' + clear_filters."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "row_dimensions": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(ROW_DIMENSIONS)},
                    "description": (
                        "Row groupings. customer/party/account/buyer/store-wise "
                        "→ ['party']; spoken product/product-wise → "
                        "['packing_category']; SKU/SKU-wise → ['product']; "
                        "channel monthly → ['client_type','business_unit']; "
                        "default sales trend → ['business_unit']."
                    ),
                },
                "column_dimensions": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(COLUMN_DIMENSIONS)},
                    "description": (
                        "Pivot columns. Trends / 'last N months' / monthly "
                        "price MUST include 'month'."
                    ),
                },
                "metrics": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(PIVOT_METRICS)},
                    "description": (
                        "volume=MT; avg_price=PKR rate; "
                        "price_fetch=Incl GST/unit + cost factor + PF/maund "
                        "(engine computes; also for 'oil price fetched' / "
                        "'apply the cost factor' / 'Price Fetch' / 'recovery'); "
                        "ams / vs_ams / ams_growth for performance. "
                        "Plain rates without cost factor → ['avg_price']."
                    ),
                },
                "operation": {
                    "type": "string",
                    "enum": list(OPERATIONS),
                    "description": (
                        "Optional. Default 'pivot'. Use party_list / "
                        "party_lookup / overview / advanced only for non-table asks."
                    ),
                },
                "context_handling": {
                    "type": "string",
                    "enum": ["none", "prior"],
                    "description": (
                        "prior = follow-up from PRIOR_QUERY_CONTEXT; "
                        "none = fresh topic."
                    ),
                },
                "base": {
                    "type": "string",
                    "enum": ["none", "prior"],
                    "description": "Legacy alias for context_handling.",
                },
                "clear_filters": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(FILTER_KEYS) + ["business_units", "city_filter"],
                    },
                    "description": (
                        "When context_handling=prior, filter keys to REMOVE."
                    ),
                },
                "clear": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Legacy alias for clear_filters.",
                },
                "period_type": {
                    "type": "string",
                    "enum": sorted(PERIOD_TYPES),
                    "description": (
                        "REQUIRED. Sales with no period → LAST_N_MONTHS + "
                        "months_back=6 + column_dimensions=['month']. "
                        "A single calendar month ('March', 'March 2026') → "
                        "SPECIFIC_MONTH + target_month=YYYY-MM — DO NOT use "
                        "LAST_N_MONTHS and DO NOT put 'month' in "
                        "column_dimensions unless the user asked for a trend. "
                        "MTD only when user says this month/MTD/so far."
                    ),
                },
                "months_back": {
                    "type": "integer",
                    "description": "Required when period_type=LAST_N_MONTHS.",
                },
                "named_month": {
                    "type": "string",
                    "description": "Legacy month phrase (prefer target_month).",
                },
                "target_month": {
                    "type": "string",
                    "description": (
                        "When period_type=SPECIFIC_MONTH: exact month as YYYY-MM "
                        "(e.g. 2026-03 for March). Anchor year to live max sales "
                        "date when the user omits the year."
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
                        "business_units": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "business_unit": {"type": "string"},
                        "client_type": {"type": "string"},
                        "client_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Named channels for compares "
                                "(e.g. ['Imtiaz Store','Eva Distributors']). "
                                "Pair with row_dimensions=['client_type']."
                            ),
                        },
                        "city": {"type": "string"},
                        "cities": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Named cities only (e.g. ['Lahore','Karachi']). "
                                "Pair with row_dimensions=['city']."
                            ),
                        },
                        "city_filter": {"type": "string"},
                        "zone": {"type": "string"},
                        "oil_type": {"type": "string"},
                        "packing_category": {"type": "string"},
                        "party": {
                            "type": "string",
                            "description": (
                                "Spoken customer name (e.g. 'al shaheer'). "
                                "Python applies silent ILIKE — no clarify loop."
                            ),
                        },
                        "parties": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Multiple spoken customer names for compare "
                                "(e.g. ['al shaheer','Metro Habib']). "
                                "Python ORs ILIKE matches."
                            ),
                        },
                        "product": {"type": "string"},
                        "active_only": {"type": "boolean"},
                    },
                },
                "sort_order": {
                    "type": "string",
                    "enum": ["asc", "desc"],
                },
                "sort": {"type": "string", "enum": ["asc", "desc"]},
                "grown_only": {"type": "boolean"},
                "declined_only": {"type": "boolean"},
                "limit": {"type": "integer"},
                "title_mode": {"type": "string"},
                "excludes": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "compare": {"type": "string"},
                "advanced_mode": {"type": "string"},
                "party_query": {"type": "string"},
                "price_flags": {
                    "type": "object",
                    "properties": {
                        "include_price_fetch": {"type": "boolean"},
                        "include_cost_factor": {"type": "boolean"},
                        "factor_breakdown": {"type": "boolean"},
                        "factor_only": {"type": "boolean"},
                    },
                },
                "business_units": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Business Unit filter (category_1). "
                        "Eva Consumer / Eva Bulk / Maan … — NEVER client_type."
                    ),
                },
                "extracted_entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Ambiguous brand/product/packing/city/party phrases "
                        "(e.g. 'al shaheer', 'Metro Habib', 'Eva Consumer'). "
                        "Python maps them: brands→business_units, unresolved "
                        "names→silent party ILIKE. Prefer this over guessing."
                    ),
                },
                "rationale": {"type": "string"},
                # ---- legacy compat (tests / old clients; prefer universal fields) ----
                "intent": {"type": "string"},
                "group_by": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
                "column_dimension": {"type": "string"},
                "time_grain": {"type": "string", "enum": ["none", "month"]},
                "ranking_metric": {"type": "string"},
                "metric": {"type": "string"},
                "grain": {"type": "object"},
                "row_groups": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": [
                "row_dimensions",
                "metrics",
                "period_type",
                "context_handling",
            ],
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


def _party_scope_from_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    """Extract sticky customer scope keys from a filters dict."""
    out: dict[str, Any] = {}
    src = dict(filters or {})
    for key in PARTY_SCOPE_KEYS:
        val = src.get(key)
        if val is None or val == "" or val == []:
            continue
        out[key] = val
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
        party_scope = _party_scope_from_filters(filters)
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
                "party_scope": party_scope or None,
                "period_phrase": party_spec.get("period_phrase"),
                "period": party_spec.get("period"),
                "business_units": list(party_spec.get("business_units") or []) or None,
            }
        )
    if table_spec:
        filters = dict(table_spec.get("filters") or {})
        party_scope = _party_scope_from_filters(filters)
        return _compact(
            {
                "source": "sales",
                "intent_hint": "sales_matrix",
                "filters": _compact(filters),
                "party_scope": party_scope or None,
                "business_units": list(table_spec.get("business_units") or []) or None,
                "row_dimension": table_spec.get("row_dimension"),
                "row_dimensions": table_spec.get("row_dimensions"),
                "row_groups": table_spec.get("row_groups"),
                "column_dimension": table_spec.get("column_dimension"),
                "column_dimensions": table_spec.get("column_dimensions"),
                "metrics": table_spec.get("metrics"),
                "months_back": table_spec.get("months_back"),
                "period_phrase": table_spec.get("period_phrase"),
                "period": table_spec.get("period"),
                "compare": table_spec.get("compare"),
                "excludes": table_spec.get("excludes"),
            }
        )
    if price_spec:
        filters = dict(price_spec.get("filters") or {})
        party_scope = _party_scope_from_filters(filters)
        return _compact(
            {
                "source": "price",
                "intent_hint": "price",
                "filters": _compact(filters),
                "party_scope": party_scope or None,
                "period_phrase": price_spec.get("period_phrase"),
                "period": price_spec.get("period"),
            }
        )
    return None


def prior_context_for_prompt(prior: dict[str, Any] | None) -> str:
    if not prior:
        return (
            "PRIOR_QUERY_CONTEXT: none\n"
            "Fresh ask → context_handling='none'."
        )
    party_hint = ""
    scope = prior.get("party_scope") or _party_scope_from_filters(
        prior.get("filters")
    )
    if scope:
        party_hint = (
            "- CUSTOMER SCOPE ACTIVE: "
            f"{json.dumps(scope, default=str)}. "
            "Short follow-ups (price / Price Fetch / % of AMS / vs AMS / "
            "last purchase / days since invoice / SKU breakup) MUST use "
            "context_handling='prior', clear_filters=[] (or clear only "
            "non-party keys), and KEEP this party scope. "
            "Do NOT drop filters.party unless the user names a different customer.\n"
        )
    return (
        "PRIOR_QUERY_CONTEXT (last answer the user can Reply on):\n"
        f"{json.dumps(prior, indent=2, default=str)}\n\n"
        "Follow-up rules (STRICT):\n"
        "- Reshape / 'this…' / 'compared to…' → context_handling='prior'.\n"
        "- When context_handling='prior', clear_filters is REQUIRED "
        "(use [] only if every prior filter still applies).\n"
        f"{party_hint}"
        "- Lahore → national / all Pakistan → clear_filters:[\"city\"] "
        "(and \"zone\" if set). Omit filters.city.\n"
        "- Lahore → other cities league → clear_filters:[\"city\"], "
        "group_by=city.\n"
        "- Lahore → Karachi → clear_filters:[\"city\"], "
        "filters.city=\"Karachi\".\n"
        "- Fresh complete ask → context_handling='none'.\n"
        "- Keep business_units from prior when the user says 'this' and does "
        "not rename the brand.\n"
        "- distributor-wise / customer-wise after a brand table → "
        "row_dimensions=[\"party\"], clear_filters include client_type if sticky; "
        "metrics=[\"vs_ams\"] for lowest performing. Do NOT invent Eva Distributors.\n"
        "- Prefer Universal Pivot fields (row_dimensions / column_dimensions / "
        "metrics) over legacy intent labels.\n"
    )


def _derive_period_type(raw: dict[str, Any], period: dict[str, Any]) -> str | None:
    """Accept legacy period.phrase when period_type omitted (compat)."""
    explicit = str(raw.get("period_type") or "").strip().upper()
    # Treat SPECIFIC_MONTH / NAMED_MONTH aliases
    if explicit == "SPECIFIC_MONTH" or explicit == "NAMED_MONTH":
        return explicit
    if explicit in PERIOD_TYPES:
        return explicit
    if raw.get("target_month"):
        return "SPECIFIC_MONTH"
    phrase = str(period.get("phrase") or raw.get("period_phrase") or raw.get("named_month") or "").lower()
    if period.get("date_from") and period.get("date_to"):
        return "CUSTOM_DATE"
    if not phrase:
        return None
    if "last week" in phrase or phrase == "this week":
        return "LAST_WEEK" if "last" in phrase else "MTD"
    if re_last_n := __import__("re").search(
        r"\b(last|past|previous)\s+(\d{1,2})\s+months?\b", phrase
    ):
        return "LAST_N_MONTHS"
    if "last month" in phrase or "previous month" in phrase:
        return "LAST_MONTH"
    if phrase in {"this month", "mtd", "so far"}:
        return "MTD"
    # Bare month name / YYYY-MM → specific month (not a 6-month trend)
    if __import__("re").fullmatch(r"\d{4}-\d{2}", phrase.strip()):
        return "SPECIFIC_MONTH"
    month_names = (
        "january|february|march|april|may|june|july|august|september|"
        "october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
    )
    if __import__("re").search(rf"\b({month_names})\b", phrase):
        return "SPECIFIC_MONTH"
    return "NAMED_MONTH"


def _promote_group_by(raw_gb: Any, grain: dict[str, Any]) -> None:
    """Map group_by string|array → grain.group_by / row_dimension / row_groups / time_grain."""
    if raw_gb is None or raw_gb == "":
        return
    if isinstance(raw_gb, (list, tuple)):
        items = [str(x).strip().lower() for x in raw_gb if str(x).strip()]
    else:
        items = [str(raw_gb).strip().lower()]
    if not items:
        return

    if "month" in items:
        grain.setdefault("time_grain", "month")
        items = [x for x in items if x != "month"]
    if not items:
        return

    rank_dims = {"party", "city", "zone"}
    dims = [x for x in items if x in GROUP_BY_DIMS]
    if not dims:
        return

    # Pure ranking grain (party_rank / party_list)
    if all(d in rank_dims for d in dims) and not grain.get("group_by"):
        grain["group_by"] = dims[-1]
        return

    # Sales MultiIndex: [...parents, leaf]
    if len(dims) >= 2:
        if not grain.get("row_groups"):
            grain["row_groups"] = dims[:-1]
        if not grain.get("row_dimension"):
            grain["row_dimension"] = dims[-1]
        # Ranking dim as leaf still sets group_by for party_rank
        if dims[-1] in rank_dims and not grain.get("group_by"):
            grain["group_by"] = dims[-1]
        return

    d = dims[0]
    if d in rank_dims and not grain.get("group_by"):
        grain["group_by"] = d
    elif d in GROUP_BY_DIMS and not grain.get("row_dimension"):
        grain["row_dimension"] = d


def _as_dim_list(value: Any, *, allowed: tuple[str, ...] | set[str]) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        items = [str(x).strip().lower() for x in value if str(x).strip()]
    else:
        items = [str(value).strip().lower()]
    allow = set(allowed)
    return [x for x in items if x in allow]


def _derive_intent_from_universal(
    *,
    operation: str,
    row_dimensions: list[str],
    column_dimensions: list[str],
    metrics: list[str],
) -> str:
    """Internal routing label derived from the universal pivot (not LLM-facing)."""
    if operation and operation != "pivot":
        return {
            "party_list": "party_list",
            "party_lookup": "party_lookup",
            "overview": "overview",
            "advanced": "advanced",
        }.get(operation, operation)
    mets = set(metrics)
    if mets & {"vs_ams", "ams_growth"} and "month" not in column_dimensions:
        return "party_rank"
    if "price_fetch" in mets or "avg_price" in mets:
        return "price"
    if "month" in column_dimensions:
        return "sales_trend"
    return "sales_matrix"


def _legacy_intent_to_universal(
    intent: str,
    grain: dict[str, Any],
    metric: str | None,
    *,
    period_type: str | None,
) -> tuple[list[str], list[str], list[str], str]:
    """Map old intent/group_by plans → row/col/metric arrays + operation."""
    rows: list[str] = []
    cols: list[str] = []
    metrics: list[str] = []
    operation = "pivot"

    if intent == "party_list":
        return ["party"], [], ["volume"], "party_list"
    if intent == "party_lookup":
        return ["party"], [], ["volume"], "party_lookup"
    if intent == "overview":
        return [], [], ["volume"], "overview"
    if intent == "advanced":
        return [], [], ["volume"], "advanced"
    if intent == "party_rank":
        gb = grain.get("group_by") or "party"
        rows = [str(gb)]
        metrics = [str(metric or "ams")]
        return rows, cols, metrics, "pivot"
    if intent == "price":
        metrics = ["avg_price"]
        if grain.get("time_grain") == "month" or grain.get("column_dimension") == "month":
            cols = ["month"]
        return rows, cols, metrics, "pivot"
    if intent in {"sales_trend", "sales_matrix", "sales_analytical"}:
        if grain.get("row_groups") and grain.get("row_dimension"):
            rows = list(grain.get("row_groups") or []) + [grain["row_dimension"]]
        elif grain.get("row_dimension"):
            rows = [grain["row_dimension"]]
        elif grain.get("group_by") in GROUP_BY_DIMS:
            rows = [grain["group_by"]]
        else:
            rows = ["business_unit"]
        col = grain.get("column_dimension")
        if col:
            cols = [str(col)]
        elif intent == "sales_trend" or period_type == "LAST_N_MONTHS":
            cols = ["month"]
        metrics = ["volume"]
        if "month" in cols:
            metrics.append("ams")
        return rows, cols, metrics, "pivot"
    return rows, cols, metrics, operation


def normalize_query_spec(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce model JSON into a canonical universal QuerySpec."""
    raw = dict(raw or {})
    intent = str(raw.get("intent") or "").strip().lower()
    aliases = {
        "sales": "sales_trend",
        "matrix": "sales_matrix",
        "trend": "sales_trend",
        "analytical": "sales_analytical",
        "list_clients": "party_list",
        "analyze_parties": "party_rank",
        "lookup_party": "party_lookup",
        "query_price": "price",
        "query_sales": "sales_trend",
        "advanced_query": "advanced",
    }
    if intent in aliases:
        intent = aliases[intent]
    if intent and intent not in INTENTS:
        intent = ""

    base = str(
        raw.get("context_handling") or raw.get("base") or "none"
    ).strip().lower()
    if base not in {"none", "prior"}:
        base = "none"

    clear_omitted = (
        "clear_filters" not in raw and "clear" not in raw
    )
    clear_raw = raw.get("clear_filters")
    if clear_raw is None:
        clear_raw = raw.get("clear")
    if clear_raw is None:
        clear_raw = []
    clear = []
    for c in clear_raw or []:
        key = str(c)
        if key == "city_filter":
            key = "city"
        clear.append(key)

    filters = dict(raw.get("filters") or {})
    if filters.get("city_filter") and not filters.get("city"):
        filters["city"] = filters.pop("city_filter")
    elif "city_filter" in filters:
        filters.pop("city_filter", None)

    grain = dict(raw.get("grain") or {})
    _promote_group_by(raw.get("group_by"), grain)
    if raw.get("column_dimension") and not grain.get("column_dimension"):
        grain["column_dimension"] = raw["column_dimension"]
    if raw.get("months_back") is not None and grain.get("months_back") is None:
        grain["months_back"] = int(raw["months_back"])
    if raw.get("time_grain") and not grain.get("time_grain"):
        tg = str(raw["time_grain"]).strip().lower()
        if tg in {"month", "none"}:
            grain["time_grain"] = tg
    if raw.get("row_groups") and not grain.get("row_groups"):
        grain["row_groups"] = [
            str(g).strip().lower() for g in (raw.get("row_groups") or []) if g
        ]

    period = dict(raw.get("period") or {})
    if raw.get("period_phrase") and not period.get("phrase"):
        period["phrase"] = raw.get("period_phrase")
    if raw.get("named_month") and not period.get("phrase"):
        period["phrase"] = raw["named_month"]
    target_month = str(raw.get("target_month") or "").strip() or None
    if target_month and not period.get("phrase"):
        period["phrase"] = target_month

    period_type = _derive_period_type(raw, period)
    months_back = grain.get("months_back")
    if months_back is None and raw.get("months_back") is not None:
        months_back = int(raw["months_back"])
    if months_back is None and period_type == "LAST_N_MONTHS":
        import re as _re

        phrase = str(period.get("phrase") or "")
        m = _re.search(r"\b(last|past|previous)\s+(\d{1,2})\s+months?\b", phrase, _re.I)
        if m:
            months_back = int(m.group(2))
            grain["months_back"] = months_back
            grain.setdefault("column_dimension", "month")

    metric = raw.get("ranking_metric") or raw.get("metric")
    sort = raw.get("sort_order") or raw.get("sort") or "desc"
    bus = list(
        filters.get("business_units") or raw.get("business_units") or []
    )
    extracted_entities = [
        str(e).strip()
        for e in (raw.get("extracted_entities") or [])
        if str(e).strip()
    ]

    # ---- Universal pivot fields ----
    row_dimensions = _as_dim_list(raw.get("row_dimensions"), allowed=ROW_DIMENSIONS)
    column_dimensions = _as_dim_list(
        raw.get("column_dimensions"), allowed=COLUMN_DIMENSIONS
    )
    metrics = _as_dim_list(raw.get("metrics"), allowed=PIVOT_METRICS)
    operation = str(raw.get("operation") or "").strip().lower()
    if operation and operation not in OPERATIONS:
        operation = ""

    # Legacy → universal when new fields omitted
    if (not row_dimensions and not metrics) and intent:
        lr, lc, lm, lop = _legacy_intent_to_universal(
            intent, grain, str(metric) if metric else None, period_type=period_type
        )
        row_dimensions = lr
        column_dimensions = lc or column_dimensions
        metrics = lm
        if not operation:
            operation = lop

    # group_by / grain still promote into row_dimensions when missing
    if not row_dimensions:
        if grain.get("row_groups") and grain.get("row_dimension"):
            row_dimensions = list(grain.get("row_groups") or []) + [
                str(grain["row_dimension"])
            ]
        elif grain.get("row_dimension"):
            row_dimensions = [str(grain["row_dimension"])]
        elif grain.get("group_by") in GROUP_BY_DIMS:
            row_dimensions = [str(grain["group_by"])]

    if not column_dimensions:
        if grain.get("column_dimension"):
            column_dimensions = _as_dim_list(
                grain.get("column_dimension"), allowed=COLUMN_DIMENSIONS
            )
        elif grain.get("time_grain") == "month":
            column_dimensions = ["month"]

    if metric and not metrics:
        metrics = _as_dim_list(metric, allowed=PIVOT_METRICS)

    # Price Fetch flags → metric
    price_flags = dict(raw.get("price_flags") or {})
    if price_flags.get("include_price_fetch") and "price_fetch" not in metrics:
        metrics.append("price_fetch")
    if price_flags.get("include_cost_factor") and not metrics:
        metrics.append("price_fetch")

    if not operation:
        operation = "pivot"

    # Sync grain from universal (executor + resolve_period still read grain)
    if row_dimensions:
        if len(row_dimensions) >= 2:
            grain["row_groups"] = row_dimensions[:-1]
            grain["row_dimension"] = row_dimensions[-1]
        else:
            grain["row_dimension"] = row_dimensions[0]
            if row_dimensions[0] in {"party", "city", "zone"}:
                grain["group_by"] = row_dimensions[0]
    if column_dimensions:
        grain["column_dimension"] = column_dimensions[0]
        if column_dimensions[0] == "month":
            grain["time_grain"] = "month"

    # Derive internal intent for routing / prior_context compat
    if not intent:
        intent = _derive_intent_from_universal(
            operation=operation,
            row_dimensions=row_dimensions,
            column_dimensions=column_dimensions,
            metrics=metrics,
        )
    # Single calendar month + volume → Volume+AMS pack (not a month grid)
    if period_type in {"SPECIFIC_MONTH", "NAMED_MONTH"} and intent == "sales_matrix":
        if (set(metrics) & {"volume", "ams"}) or not metrics:
            if "month" not in column_dimensions:
                intent = "sales_trend"

    # Ranking metric from metrics array
    if not metric:
        for m in metrics:
            if m in {"vs_ams", "ams_growth", "ams", "volume"}:
                metric = m
                break

    return {
        "intent": intent,
        "operation": operation,
        "row_dimensions": row_dimensions,
        "column_dimensions": column_dimensions,
        "metrics": metrics,
        "base": base,
        "clear": clear,
        "_clear_omitted": clear_omitted,
        "period_type": period_type,
        "period": period,
        "months_back": months_back,
        "filters": filters,
        "excludes": dict(raw.get("excludes") or {}),
        "grain": grain,
        "metric": metric,
        "compare": raw.get("compare"),
        "sort": sort,
        "grown_only": bool(raw.get("grown_only") or False),
        "declined_only": bool(raw.get("declined_only") or False),
        "limit": int(raw.get("limit") or 0) or None,
        "title_mode": raw.get("title_mode"),
        "advanced_mode": raw.get("advanced_mode"),
        "party_query": raw.get("party_query") or filters.get("party"),
        "price_flags": price_flags,
        "rationale": raw.get("rationale") or "",
        "business_units": bus,
        "extracted_entities": extracted_entities,
        "target_month": target_month,
    }


def validate_query_spec(
    spec: dict[str, Any],
    *,
    prior: dict[str, Any] | None = None,
) -> list[str]:
    """Return human-readable plan errors for the LLM (empty = ok)."""
    errors: list[str] = []
    operation = str(spec.get("operation") or "pivot")
    metrics = list(spec.get("metrics") or [])
    rows = list(spec.get("row_dimensions") or [])
    if operation == "pivot":
        if not metrics:
            errors.append(
                "Missing metrics. Use e.g. [\"volume\",\"ams\"], [\"avg_price\"], "
                "or [\"price_fetch\"]."
            )
        if not rows and not (
            set(metrics) & {"avg_price", "price_fetch"}
        ):
            errors.append(
                "Missing row_dimensions. Example: customer-wise → [\"party\"]; "
                "default sales trend → [\"business_unit\"]."
            )
    if not spec.get("period_type"):
        errors.append(
            "Missing period_type. REQUIRED: MTD | LAST_N_MONTHS | LAST_MONTH | "
            "LAST_WEEK | SPECIFIC_MONTH | NAMED_MONTH | CUSTOM_DATE. "
            "If the user said 'last 6 months', use LAST_N_MONTHS + months_back=6 "
            "+ column_dimensions=[\"month\"]. "
            "If the user said 'March' / a single month, use SPECIFIC_MONTH + "
            "target_month=YYYY-MM (NOT LAST_N_MONTHS). "
            "If unspecified for a sales ask, use LAST_N_MONTHS + months_back=6 "
            "(Trend Default) — not MTD."
        )
    # Follow-up state: when using prior, clear_filters must be explicit
    # (empty list is OK — means keep all prior filters).
    if spec.get("base") == "prior":
        raw_clear = spec.get("clear")
        # normalize_query_spec always sets clear to a list; detect omission via
        # a sentinel set during normalize when neither clear nor clear_filters given.
        if spec.get("_clear_omitted"):
            errors.append(
                "context_handling='prior' requires clear_filters (array). "
                "Use [] to keep all prior filters, or e.g. [\"city\"] when the "
                "user switches to national / other cities / a new city."
            )
        # Sticky city while ranking other cities / national
        grain = spec.get("grain") or {}
        filters = spec.get("filters") or {}
        clear = set(spec.get("clear") or [])
        row_dims = list(spec.get("row_dimensions") or [])
        city_row = (grain.get("group_by") or "") == "city" or (
            len(row_dims) == 1 and row_dims[0] == "city"
        )
        if city_row and filters.get("city") and ("city" not in clear):
            errors.append(
                "row_dimensions=['city'] while filters.city is set ranks inside "
                "one city. For 'other cities' / national, clear_filters:[\"city\"] "
                "and omit filters.city."
            )
    pt = spec.get("period_type")
    if pt == "LAST_N_MONTHS":
        mb = spec.get("months_back") or (spec.get("grain") or {}).get("months_back")
        if not mb:
            errors.append(
                "period_type=LAST_N_MONTHS requires months_back (e.g. 6)."
            )
    if pt == "SPECIFIC_MONTH":
        tm = spec.get("target_month") or (spec.get("period") or {}).get("phrase")
        if not tm:
            errors.append(
                "period_type=SPECIFIC_MONTH requires target_month (YYYY-MM) "
                "or named_month / period.phrase (e.g. 'March' / '2026-03')."
            )
    if pt == "NAMED_MONTH":
        phrase = (spec.get("period") or {}).get("phrase")
        if not phrase and not spec.get("target_month"):
            errors.append(
                "period_type=NAMED_MONTH requires named_month or period.phrase "
                "(e.g. 'July' or 'July 2026'). Prefer SPECIFIC_MONTH + target_month."
            )
    if pt == "CUSTOM_DATE":
        period = spec.get("period") or {}
        if not (period.get("date_from") and period.get("date_to")):
            errors.append(
                "period_type=CUSTOM_DATE requires period.date_from and period.date_to."
            )
    if (
        spec.get("intent") == "party_lookup"
        or spec.get("operation") == "party_lookup"
    ) and not (
        spec.get("party_query") or (spec.get("filters") or {}).get("party")
    ):
        errors.append("party_lookup requires party_query (the party name).")

    # Strict categorical enums (Enterprise Semantic Layer)
    from eva_dashboard.entity_catalog import validate_categorical_filters

    enum_filters = dict(spec.get("filters") or {})
    if spec.get("business_units"):
        enum_filters["business_units"] = list(spec.get("business_units") or [])
    errors.extend(validate_categorical_filters(enum_filters))
    return errors


def _resolve_specific_month_bounds(spec: dict[str, Any]) -> dict[str, Any]:
    """Compute ISO bounds for SPECIFIC_MONTH / NAMED_MONTH; never a month grid."""
    from eva_dashboard.sales_query import resolve_period

    period = dict(spec.get("period") or {})
    tm = str(spec.get("target_month") or "").strip()
    phrase = tm or str(period.get("phrase") or spec.get("named_month") or "").strip()
    info = resolve_period(phrase or None)
    if info.get("ok") is False or not info.get("date_from"):
        return {"period": period, "error": info.get("error")}
    period["date_from"] = info["date_from"]
    period["date_to"] = info["date_to"]
    period["phrase"] = phrase or info.get("label")
    period["label"] = info.get("label")
    # Persist YYYY-MM for the planner
    try:
        target = str(info["date_from"])[:7]
    except Exception:  # noqa: BLE001
        target = tm or None
    return {"period": period, "target_month": target, "period_info": info}


def resolve_period_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Map period_type → executor period phrase / bounds / month grain flags.

    Deterministic business logic — not LLM judgment.
    """
    pt = spec.get("period_type") or "MTD"
    period = dict(spec.get("period") or {})
    grain = dict(spec.get("grain") or {})
    mb = spec.get("months_back") or grain.get("months_back")
    rows = list(spec.get("row_dimensions") or [])
    cols = list(spec.get("column_dimensions") or [])
    mets = set(spec.get("metrics") or [])

    if pt == "MTD":
        period.setdefault("phrase", "this month")
    elif pt == "LAST_MONTH":
        period.setdefault("phrase", "last month")
    elif pt == "LAST_WEEK":
        period.setdefault("phrase", "last week")
    elif pt == "LAST_N_MONTHS":
        n = int(mb or 6)
        period["phrase"] = f"last {n} months"
        grain["column_dimension"] = grain.get("column_dimension") or "month"
        grain["months_back"] = n
        if "month" not in cols:
            cols = ["month"] + [c for c in cols if c != "month"]
        if not rows and not grain.get("row_dimension") and not grain.get("group_by"):
            if not (mets & {"avg_price", "price_fetch"}) and str(
                spec.get("operation") or "pivot"
            ) == "pivot":
                if str(spec.get("intent") or "") in {
                    "",
                    "sales_trend",
                    "sales_matrix",
                }:
                    grain["row_dimension"] = "business_unit"
                    rows = ["business_unit"]
        return {
            "period": period,
            "grain": grain,
            "months_back": n,
            "row_dimensions": rows,
            "column_dimensions": cols,
        }
    elif pt in {"SPECIFIC_MONTH", "NAMED_MONTH"}:
        # Exact calendar month — NOT a multi-month time-series
        resolved = _resolve_specific_month_bounds(spec)
        period = resolved.get("period") or period
        if resolved.get("target_month"):
            spec["target_month"] = resolved["target_month"]
        # Strip accidental month pivot columns — single month is not a trend grid
        cols = [c for c in cols if c != "month"]
        if grain.get("column_dimension") == "month":
            grain.pop("column_dimension", None)
        grain["time_grain"] = "none"
        if not rows and not (mets & {"avg_price", "price_fetch"}):
            grain.setdefault("row_dimension", "business_unit")
            rows = rows or ["business_unit"]
        # Volume for one month → Volume+AMS pack (client_type cross-tab, not months)
        if ("volume" in mets or "ams" in mets or not mets) and not cols:
            grain.setdefault("column_dimension", "client_type")
        return {
            "period": period,
            "grain": grain,
            "months_back": None,
            "row_dimensions": rows,
            "column_dimensions": cols,
            "target_month": resolved.get("target_month"),
        }
    elif pt == "CUSTOM_DATE":
        pass

    return {
        "period": period,
        "grain": grain,
        "months_back": grain.get("months_back") or mb,
        "row_dimensions": rows,
        "column_dimensions": cols,
    }


def _clear_party_scope(filters: dict[str, Any]) -> None:
    """Drop all customer-scope keys together (party / parties / party_ilike)."""
    for key in PARTY_SCOPE_KEYS:
        filters.pop(key, None)


def merge_prior_into_spec(
    spec: dict[str, Any],
    prior: dict[str, Any] | None,
) -> dict[str, Any]:
    """Apply base=prior: start from prior filters/metric/grain, then patches + clear."""
    out = dict(spec)
    if out.get("base") != "prior" or not prior:
        return out
    prior_filters = dict(prior.get("filters") or {})
    # Promote party_scope into filters when prior stored it separately
    for k, v in (prior.get("party_scope") or {}).items():
        prior_filters.setdefault(k, v)
    merged_filters = dict(prior_filters)
    for k, v in (spec.get("filters") or {}).items():
        if v is not None and v != "":
            merged_filters[k] = v
    clear = list(spec.get("clear") or [])
    for key in clear:
        if key in PARTY_SCOPE_KEYS or key == "party_query":
            _clear_party_scope(merged_filters)
            out["party_query"] = None
            continue
        merged_filters.pop(key, None)
        if key == "business_units":
            out["business_units"] = []
        if key == "business_unit":
            merged_filters.pop("business_unit", None)
    out["filters"] = merged_filters

    if not out.get("metric") and prior.get("metric"):
        out["metric"] = prior.get("metric")
    if not out.get("row_dimensions") and prior.get("row_dimensions"):
        out["row_dimensions"] = list(prior.get("row_dimensions") or [])
    if not out.get("column_dimensions") and prior.get("column_dimensions"):
        out["column_dimensions"] = list(prior.get("column_dimensions") or [])
    if not out.get("metrics") and prior.get("metrics"):
        out["metrics"] = list(prior.get("metrics") or [])
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

    # Inherit period only when this plan did not set period_type / phrase
    period = dict(out.get("period") or {})
    if not out.get("period_type") and not period.get("phrase") and not period.get(
        "date_from"
    ):
        if prior.get("period_phrase"):
            period["phrase"] = prior.get("period_phrase")
        elif isinstance(prior.get("period"), dict):
            p = prior["period"]
            period.setdefault("date_from", p.get("date_from"))
            period.setdefault("date_to", p.get("date_to"))
        out["period"] = period
        out["period_type"] = _derive_period_type({}, period)

    if not out.get("business_units") and prior.get("business_units"):
        if "business_units" not in clear:
            out["business_units"] = list(prior.get("business_units") or [])

    if not out.get("excludes") and prior.get("excludes"):
        out["excludes"] = dict(prior.get("excludes") or {})

    # Keep party_query aligned with sticky party filter
    if not out.get("party_query") and merged_filters.get("party"):
        out["party_query"] = merged_filters.get("party")

    # City league safety: ranking cities cannot keep a sticky city filter
    # unless the plan explicitly re-set city after clear.
    if (grain.get("group_by") or "") == "city":
        if "city" in clear or not (spec.get("filters") or {}).get("city"):
            merged_filters.pop("city", None)
            out["filters"] = merged_filters

    return out
