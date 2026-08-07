"""Intent detection for advanced analytics features."""

from __future__ import annotations

import re
from typing import Any

from eva_dashboard.client_language import (
    extract_client_type_from_text,
    extract_oil_type_from_text,
    extract_packing_from_text,
    normalize_client_type,
)
from eva_dashboard.party_analytics import extract_city_from_text


def extract_exclude_client_types(text: str) -> list[str]:
    t = (text or "").lower()
    out: list[str] = []
    # exclude online / without online / except metro
    m = re.search(
        r"\b(exclude|excluding|except|without|exclud(?:e|ing))\b\s+"
        r"(.+?)(?:\s*$|\s+for\b|\s+in\b|\s+this\b)",
        t,
    )
    blob = m.group(2) if m else ""
    if "online" in t and re.search(r"\b(exclude|except|without|excluding)\b.+\bonline\b", t):
        out.append("Online Customer")
    if "metro" in blob or re.search(r"\b(exclude|except|without)\b.+\bmetro\b", t):
        ct = normalize_client_type("metro")
        if ct:
            out.append(ct)
    # also "exclude Online Customer(s)"
    for alias, canon in (
        ("online", "Online Customer"),
        ("canteen", "Canteen Store Department"),
        ("hashoo", "HASHOO GROUP"),
    ):
        if re.search(rf"\b(exclude|except|without)\b.+\b{alias}\b", t):
            if canon not in out:
                out.append(canon)
    return out


def infer_advanced_from_text(text: str) -> dict[str, Any]:
    t = (text or "").lower()
    out: dict[str, Any] = {
        "mode": None,
        "city": extract_city_from_text(text),
        "client_type": extract_client_type_from_text(text),
        "oil_type": extract_oil_type_from_text(text),
        "packing_category": extract_packing_from_text(text),
        "business_unit": None,
        "period": None,
        "metric": "volume",
        "limit": 10,
        "sort": "desc",
        "exclude_client_types": extract_exclude_client_types(text),
        "left": None,
        "right": None,
        "segment": "city",
        "dimension": "packing_category",
        "group_by": None,
        "party_query": None,
        "grain": "week",
        "entity": "party",
        "op": None,
        "threshold": None,
    }
    if re.search(r"\beva consumer\b", t):
        out["business_unit"] = "Eva Consumer"
    elif re.search(r"\beva bulk\b", t):
        out["business_unit"] = "Eva Bulk"

    if re.search(r"\bvtf\b", t):
        out["oil_type"] = "Eva VTF"

    # Period
    if re.search(r"\bthis week\b", t):
        out["period"] = "this week"
        out["grain"] = "week"
    elif re.search(r"\blast week\b", t):
        out["period"] = "last week"
    elif re.search(r"\bthis month\b|\bso far\b|\bmtd\b", t):
        out["period"] = "this month"
    elif re.search(r"\blast month\b", t):
        out["period"] = "last month"

    m_lim = re.search(r"\b(?:top|bottom)\s+(\d{1,3})\b", t)
    if m_lim:
        out["limit"] = int(m_lim.group(1))
    if re.search(r"\bbottom\b", t):
        out["sort"] = "asc"

    # Dumping
    if re.search(
        r"\b(dumping|excessive sales?|excess sale|likely dump|"
        r"identify (any )?dump|show (any )?dump)\b",
        t,
    ):
        out["mode"] = "dumping"
        if re.search(r"\bbreak( down)? by\b|\bby business unit\b|\bby bu\b", t):
            out["group_by"] = "business_unit"
        elif re.search(r"\bby packing\b|\bby pack\b", t):
            out["group_by"] = "packing_category"
        elif re.search(r"\bby oil\b", t):
            out["group_by"] = "oil_type"
        elif re.search(r"\bby client\b|\bby client type\b", t):
            out["group_by"] = "client_type"
        elif re.search(r"\bby city\b", t):
            out["group_by"] = "city"
        return out

    # Expected month / run rate / forecast
    if re.search(
        r"\b(expected (month )?close|expected sales|what sales should we expect|"
        r"forecast|run[- ]?rate|project(ed)? (month|sales)|month end estimate)\b",
        t,
    ):
        out["mode"] = "expected_month"
        out["period"] = out["period"] or "this month"
        return out

    # Silent / no sale this week
    if re.search(
        r"\b("
        r"no sales? this week|not had any sale this week|"
        r"haven'?t (bought|ordered|purchased) this week|"
        r"zero (sales? )?this week|silent this week|"
        r"customers? (with )?no sale this week|"
        r"who (has|have) not (bought|ordered) this week|"
        r"no invoice this week"
        r")\b",
        t,
    ):
        out["mode"] = "silent_week"
        out["period"] = "this week"
        out["grain"] = "week"
        return out

    # Not ordered X
    if re.search(
        r"\b("
        r"not ordered|haven'?t ordered|have not ordered|"
        r"no (orders?|sales?) (of|for)|missing (orders?|sales?)|"
        r"did not (buy|order)|zero .+ this month"
        r")\b",
        t,
    ) and (out["packing_category"] or out["oil_type"] or re.search(r"\bstand ?up|pillow|pet|jerry|pouch|tin\b", t)):
        out["mode"] = "not_ordered"
        out["period"] = out["period"] or "this month"
        return out

    # Reactivated
    if re.search(
        r"\b(reactivated?|reactivation|came back|"
        r"silent last quarter.{0,40}buy|buying now)\b",
        t,
    ):
        out["mode"] = "reactivated"
        return out

    # Days since invoice
    if re.search(
        r"\b(days? since (last )?(invoice|sale|order)|last invoice|"
        r"how long since|inactive days)\b",
        t,
    ):
        out["mode"] = "days_since_invoice"
        return out

    # WoW
    if re.search(
        r"\b(week[- ]?over[- ]?week|wow|vs last week|versus last week|"
        r"compared? (to|with) last week|what changed vs last week)\b",
        t,
    ):
        out["mode"] = "week_over_week"
        return out

    # City / client compare
    cities = re.findall(
        r"\b(lahore|karachi|faisalabad|islamabad|multan|peshawar|rawalpindi|"
        r"gujranwala|sialkot|hyderabad|quetta)\b",
        t,
    )
    if re.search(r"\bcompar(e|ison|ing)\b", t) and len(cities) >= 2:
        out["mode"] = "compare_cities"
        out["segment"] = "city"
        out["left"] = cities[0].title()
        out["right"] = cities[1].title()
        if re.search(r"\b(growth|grew|yoy|year over year)\b", t):
            out["metric"] = "growth"
        return out

    if re.search(r"\bcompar(e|ison|ing)\b", t) and re.search(
        r"\b(imtiaz|distributors?)\b.+\b(imtiaz|distributors?)\b", t
    ):
        out["mode"] = "compare_client_types"
        out["segment"] = "client_type"
        # order of mention
        if t.find("imtiaz") < t.find("distribut"):
            out["left"], out["right"] = "Imtiaz Store", "Eva Distributors"
        else:
            out["left"], out["right"] = "Eva Distributors", "Imtiaz Store"
        if re.search(r"\b(growth|grew|yoy)\b", t):
            out["metric"] = "growth"
        return out

    # Filter entities by volume / YoY / MoM conditions
    # e.g. sales > 10 MT, declined more than 10%, more this month than last
    volume_m = re.search(
        r"(?:sales|volume).{0,40}(?:more than|greater than|over|above|>)\s*"
        r"([\d.]+)\s*(?:mt|tons?|tonnes?)\b"
        r"|"
        r"(?:more than|greater than|over|above|>)\s*([\d.]+)\s*(?:mt|tons?|tonnes?)\b"
        r"|"
        r"(?:sales|volume).{0,40}(?:less than|under|below|<)\s*"
        r"([\d.]+)\s*(?:mt|tons?|tonnes?)\b"
        r"|"
        r"(?:less than|under|below|<)\s*([\d.]+)\s*(?:mt|tons?|tonnes?)\b",
        t,
    )
    pct_decl = re.search(
        r"(?:declined?|dropped|fallen|fell|decreased?|down)\s+"
        r"(?:by\s+)?(?:more than|over|at least|>)\s*([\d.]+)\s*%",
        t,
    )
    pct_grow = re.search(
        r"(?:grown|grew|increased?|up)\s+"
        r"(?:by\s+)?(?:more than|over|at least|>)\s*([\d.]+)\s*%",
        t,
    )
    has_mom = bool(
        re.search(
            r"\bmore\s+(?:sales|volume)\s+this\s+month\s+than\s+last\b|"
            r"\bthis\s+month\b.{0,40}\b(?:more|higher|greater)\b.{0,25}"
            r"\b(?:than|vs\.?)\b.{0,15}\blast\s+month\b|"
            r"\bmore this month than last(?:\s+month)?\b|"
            r"\b(?:mom|month[- ]over[- ]month)\b.{0,30}"
            r"\b(grown|grew|increased?|declined?|dropped)\b|"
            r"\b(grown|grew|increased?|declined?|dropped)\b.{0,30}"
            r"\b(?:mom|month[- ]over[- ]month|vs last month)\b",
            t,
        )
    )
    has_growth_filter = bool(
        re.search(
            r"\b(?:where|that|which|who)\b.{0,60}"
            r"(?:sales|volume).{0,30}"
            r"(?:have |has )?(?:declined|dropped|fallen|decreased|grown|increased|grew)\b|"
            r"\b(?:sales|volume)\s+have\s+(?:declined|dropped|fallen|decreased|"
            r"grown|increased)\b|"
            r"\b(?:customers?|distributors?|parties|clients?|channels?|"
            r"client\s*types?|products?|skus?|packing|oils?)\b.{0,40}"
            r"(?:that |which )?(?:have |has )?"
            r"(?:declined|dropped|fallen|decreased|grown|increased|grew)\b|"
            r"\b(?:which\s+)?channels?\b.{0,40}"
            r"(?:grew|grown|declined|dropped|increased|decreased)\b|"
            r"\bcustomers that have grown\b|"
            r"\bwhere sales have declined\b",
            t,
        )
    )
    # Pure packing/oil ranking stays on dimension_growth (handled below)
    packing_oil_rank = bool(
        re.search(
            r"\b(which packing|packing.{0,20}grow|grow.{0,20}packing|"
            r"fastest packing|oil.{0,20}grow|which oil)\b",
            t,
        )
        or re.search(r"\b(rank|top)\b.+\b(packing|oil)\b.+\b(growth|grow)\b", t)
    ) and not (volume_m or pct_decl or pct_grow or has_mom)

    if (volume_m or pct_decl or pct_grow or has_mom or has_growth_filter) and not packing_oil_rank:
        out["mode"] = "filter_entities"
        out["limit"] = max(out["limit"], 50)

        # Entity
        if re.search(r"\b(skus?|products?)\b", t):
            out["entity"] = "product"
        elif re.search(r"\bpacking\b", t):
            out["entity"] = "packing_category"
        elif re.search(r"\boil\b", t) and not re.search(r"\boil\s+for\b", t):
            out["entity"] = "oil_type"
        elif re.search(r"\b(business units?|bus)\b", t):
            out["entity"] = "business_unit"
        elif re.search(r"\b(channels?|client\s*types?)\b", t):
            # Channel = Client Type (trade channel)
            out["entity"] = "client_type"
        elif re.search(r"\bcities\b", t) and not out["city"]:
            out["entity"] = "city"
        else:
            out["entity"] = "party"
            if not out["client_type"] and re.search(r"\bdistributors?\b", t):
                out["client_type"] = "Eva Distributors"

        if volume_m:
            out["metric"] = "volume"
            # groups: more(...), more(bare), less(...), less(bare)
            thr = next(
                (g for g in volume_m.groups() if g is not None),
                None,
            )
            out["threshold"] = float(thr) if thr is not None else None
            if re.search(
                r"(?:less than|under|below|<)\s*[\d.]+\s*(?:mt|tons?|tonnes?)",
                t,
            ):
                out["op"] = "lt"
            else:
                out["op"] = "gt"
        elif has_mom and not (pct_decl or pct_grow):
            out["metric"] = "mom"
            out["period"] = out["period"] or "this month"
            if re.search(r"\b(declined?|dropped|fallen|decreased|down)\b", t):
                out["op"] = "declined"
            else:
                out["op"] = "grown"
            out["threshold"] = 0.0
        else:
            # YoY growth/decline (default when not MoM/volume)
            out["metric"] = "mom" if has_mom else "yoy"
            if pct_decl:
                out["op"] = "declined"
                out["threshold"] = float(pct_decl.group(1))
            elif pct_grow:
                out["op"] = "grown"
                out["threshold"] = float(pct_grow.group(1))
            elif re.search(
                r"\b(declined|dropped|fallen|decreased|down)\b", t
            ):
                out["op"] = "declined"
                out["threshold"] = 0.0
            else:
                out["op"] = "grown"
                out["threshold"] = 0.0
        return out

    # Packing / oil growth ranks
    if re.search(
        r"\b(which packing|packing.{0,20}grow|grow.{0,20}packing|"
        r"fastest packing|oil.{0,20}grow|which oil)\b",
        t,
    ) or (
        re.search(r"\b(rank|top)\b.+\b(packing|oil)\b.+\b(growth|grow)\b", t)
    ):
        out["mode"] = "dimension_growth"
        out["dimension"] = "oil_type" if re.search(r"\boil\b", t) else "packing_category"
        out["metric"] = "growth"
        return out

    # Concentration / shares of distributors
    if re.search(
        r"\b(share of (different )?distributors|distributor shares?|"
        r"concentration|depend(ency)?|pakistan share|"
        r"shares? of .{0,20}distributors)\b",
        t,
    ):
        out["mode"] = "concentration"
        if not out["client_type"] and re.search(r"\bdistributors?\b", t):
            out["client_type"] = "Eva Distributors"
        if re.search(r"\bgrowth\b", t) and re.search(r"\bimtiaz\b", t):
            out["mode"] = "concentration_growth"
            out["client_type"] = "Imtiaz Store"
            out["metric"] = "growth"
        return out

    if re.search(r"\bgrowth at (different )?imtiaz\b|\bimtiaz stores?.{0,20}growth\b", t):
        out["mode"] = "concentration_growth"
        out["client_type"] = "Imtiaz Store"
        out["metric"] = "growth"
        return out

    # Oil mix
    if re.search(r"\b(oil[- ]?type mix|oil mix|canola vs cooking|split .{0,15}oil)\b", t):
        out["mode"] = "oil_mix"
        return out

    # Packing contribution / share of customer
    if re.search(
        r"\b(packing share of (customer|party|client)|share of customer volume|"
        r"packing.?s share of .{0,20}volume)\b",
        t,
    ):
        out["mode"] = "packing_share_of_party"
        return out

    if re.search(
        r"\b(contribution of|share of)\b.+\b(stand ?up|pillow|packing|pet|jerry)\b|"
        r"\bpacking contribution\b|\bstand ?up share\b",
        t,
    ):
        out["mode"] = "packing_contribution"
        return out

    # Top SKUs
    if re.search(r"\b(top|bottom)\s+\d*\s*skus?\b|\bsku (rank|table|list)\b", t):
        out["mode"] = "top_skus"
        return out

    # Single party profile
    if re.search(
        r"\b(ams for|sales of|profile of|history of|tell me about)\b|"
        r"\bwhat is ams for\b|\blast \d+ months\b.+\b(for|of)\b",
        t,
    ) and not re.search(r"\b(distributors?|imtiaz stores?|cities|clients)\b", t):
        # Extract party-ish query
        m = re.search(
            r"(?:ams for|sales of|profile of|history of|tell me about|what is ams for)\s+(.+)$",
            t,
        )
        if m:
            out["mode"] = "party_profile"
            out["party_query"] = m.group(1).strip(" ?.")
            return out
        # "Sales of X last 6 months"
        m2 = re.search(r"sales of\s+(.+?)\s+last\s+\d+", t)
        if m2:
            out["mode"] = "party_profile"
            out["party_query"] = m2.group(1).strip()
            return out

    return out


def looks_advanced(text: str) -> bool:
    return infer_advanced_from_text(text).get("mode") is not None
