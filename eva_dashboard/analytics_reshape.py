"""Dynamic reshape planner for analytics follow-ups.

Turns spoken intent ("compared to other cities", "by zone", "this growth")
into structured overrides: grain, geography expand, metric continuity.
This is guidance applied when the model omits args — not a forced tool router.
"""

from __future__ import annotations

import re
from typing import Any

from eva_dashboard.geo import extract_zone_from_text
from eva_dashboard.party_analytics import extract_city_from_text


def _t(text: str) -> str:
    return (text or "").lower()


def wants_city_grain(text: str) -> bool:
    t = _t(text)
    return bool(
        re.search(
            r"\b("
            r"other\s+cities|across\s+cities|among\s+cities|between\s+cities|"
            r"vs\.?\s+other\s+cities|versus\s+other\s+cities|"
            r"compared?\s+to\s+other\s+cities|"
            r"compared?\s+.{0,24}\bcities\b|"
            r"how\s+.{0,40}\bother\s+cities\b|"
            r"city\s+league|rank(ed)?\s+cities|top\s+\d+\s+cities|top\s+cities|"
            r"cities\s+by|by\s+city|city[- ]?wise|city\s+break|"
            r"\bcities\b"
            r")\b",
            t,
        )
    )


def wants_zone_grain(text: str) -> bool:
    t = _t(text)
    return bool(
        re.search(
            r"\b("
            r"other\s+zones|across\s+zones|among\s+zones|"
            r"zone\s+league|rank(ed)?\s+zones|top\s+zones|"
            r"zones\s+by|by\s+zone|zone[- ]?wise"
            r")\b",
            t,
        )
    )


def wants_geo_expand(text: str) -> bool:
    """Clear sticky city/zone — user wants a wider geography than the prior table."""
    t = _t(text)
    if re.search(
        r"\b("
        r"all\s+over\s+pakistan|across\s+pakistan|nationwide|nationally|national|"
        r"country[- ]?wide|all\s+pakistan|pakistan[- ]?wide|"
        r"all\s+over\s+the\s+country|across\s+the\s+country"
        r")\b",
        t,
    ):
        return True
    # Contrasting with other geos expands scope even without "nationally"
    if re.search(
        r"\b("
        r"other\s+cities|other\s+zones|rest\s+of\s+(the\s+)?(cities|country)|"
        r"compared?\s+to\s+other|"
        r"vs\.?\s+other|versus\s+other|"
        r"across\s+cities|across\s+zones|among\s+cities|"
        r"how\s+does\s+this\s+compare|how\s+is\s+this\s+.{0,20}compared?"
        r")\b",
        t,
    ):
        return True
    return False


def wants_metric_continuity(text: str) -> bool:
    """Follow-up that refers to the prior ranking metric (this growth / same)."""
    t = _t(text)
    return bool(
        re.search(
            r"\b("
            r"this\s+growth|that\s+growth|same\s+growth|the\s+growth|"
            r"this\s+ams|that\s+ams|same\s+metric|"
            r"how\s+is\s+this|how\s+does\s+this|"
            r"compared?\s+to|relative\s+to"
            r")\b",
            t,
        )
    )


def rank_title_mode(text: str, *, inferred: dict[str, Any]) -> str:
    """biggest_gains | smallest_gains | biggest_declines | by_growth."""
    t = _t(text)
    if inferred.get("declined_only") or (
        re.search(r"\b(declined?|dropped|fallen|fell)\b", t)
        and not re.search(r"\b(gains?|grown|grew)\b", t)
    ):
        return "biggest_declines"
    if re.search(
        r"\b(least|lowest|smallest|bottom|worst|fewest)\b",
        t,
    ):
        return "smallest_gains"
    if inferred.get("grown_only") or re.search(
        r"\b(biggest|highest|top|most|largest|best|greatest)\s+"
        r"(ams\s*)?(gains?|growth)\b|"
        r"\b(biggest|highest|top)\b.+\b(gains?|growth)\b",
        t,
    ):
        return "biggest_gains"
    return "by_growth"


def resolve_analytics_reshape(
    user_text: str,
    *,
    arguments: dict[str, Any] | None = None,
    inferred: dict[str, Any] | None = None,
    prior_party_spec: dict[str, Any] | None = None,
    prior_ctx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return structured overrides for analyze_parties.

    Keys:
      group_by, clear_city, clear_zone, metric, sort, grown_only, declined_only,
      title_mode, from_prior_metric, keep_client_type
    """
    args = arguments or {}
    inf = inferred or {}
    prior = prior_party_spec or {}
    prior_filters = dict(prior.get("filters") or {})
    ctx = prior_ctx or {}
    text = user_text or ""

    out: dict[str, Any] = {
        "clear_city": False,
        "clear_zone": False,
        "title_mode": rank_title_mode(text, inferred=inf),
        "from_prior_metric": False,
    }

    # Grain: spoken city/zone league wins (reshape), else model → inferred → prior
    if wants_zone_grain(text):
        group = "zone"
    elif wants_city_grain(text):
        group = "city"
    else:
        group = str(args.get("group_by") or "").strip().lower()
        if group not in {"party", "city", "zone"}:
            group = str(inf.get("group_by") or prior.get("group_by") or "party")
            group = group if group in {"party", "city", "zone"} else "party"
    out["group_by"] = group

    # Geography expand when contrasting with other cities/zones, or grain is geo.
    # Only trust cities/zones spoken in THIS turn — ignore sticky model/prior args.
    named_city = extract_city_from_text(text)
    named_zone = extract_zone_from_text(text)
    expand = wants_geo_expand(text)
    if group == "city" and not named_city:
        # Ranking cities while filtering to one city is contradictory
        out["clear_city"] = True
        out["clear_zone"] = True
    elif group == "zone" and not named_zone:
        out["clear_city"] = True
        out["clear_zone"] = True
    elif expand and not named_city:
        out["clear_city"] = True
        if not named_zone:
            out["clear_zone"] = True

    # Metric continuity from prior analyze_parties answer
    prior_metric = str(prior.get("metric") or "").strip().lower()
    arg_metric = str(args.get("metric") or "").strip().lower()
    inf_metric = str(inf.get("metric") or "").strip().lower()
    if arg_metric:
        out["metric"] = arg_metric
    elif wants_metric_continuity(text) and prior_metric in {
        "ams_growth",
        "yoy",
        "yoy_ams",
        "vs_ams",
        "ams",
        "volume",
    }:
        out["metric"] = prior_metric
        out["from_prior_metric"] = True
    elif inf_metric:
        out["metric"] = inf_metric

    # Keep channel / oil / packing from prior when reshaping geography
    if out.get("clear_city") or out.get("from_prior_metric") or expand:
        for key in ("client_type", "oil_type", "packing_category", "active_only"):
            if args.get(key) is not None:
                out[key] = args.get(key)
            elif inf.get(key) is not None:
                out[key] = inf.get(key)
            elif prior_filters.get(key) is not None:
                out[key] = prior_filters.get(key)
            elif ctx.get(key) is not None:
                out[key] = ctx.get(key)

    # Sort / filters from inference when model omitted
    if "sort" in args:
        out["sort"] = args.get("sort")
    elif inf.get("sort"):
        out["sort"] = inf.get("sort")
    if "grown_only" in args:
        out["grown_only"] = bool(args.get("grown_only"))
    elif "grown_only" in inf:
        out["grown_only"] = bool(inf.get("grown_only"))
    if "declined_only" in args:
        out["declined_only"] = bool(args.get("declined_only"))
    elif "declined_only" in inf:
        out["declined_only"] = bool(inf.get("declined_only"))

    # Comparative city asks are not "biggest gains" unless wording says so
    if out["title_mode"] == "by_growth":
        out.setdefault("grown_only", False)

    return out
