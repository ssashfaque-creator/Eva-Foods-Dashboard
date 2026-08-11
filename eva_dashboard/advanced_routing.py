"""Lightweight keyword gates for advanced analytics — not argument inference.

Mode and filters must come from the model via plan_query / tool args.
"""

from __future__ import annotations

import re

from eva_dashboard.client_language import normalize_client_type


def extract_exclude_client_types(text: str) -> list[str]:
    """Parse explicit exclude/except/without channel mentions (legacy helper)."""
    t = (text or "").lower()
    out: list[str] = []
    m = re.search(
        r"\b(exclude|excluding|except|without|exclud(?:e|ing))\b\s+"
        r"(.+?)(?:\s*$|\s+for\b|\s+in\b|\s+this\b)",
        t,
    )
    blob = m.group(2) if m else ""
    if "online" in t and re.search(
        r"\b(exclude|except|without|excluding)\b.+\bonline\b", t
    ):
        out.append("Online Customer")
    if "metro" in blob or re.search(r"\b(exclude|except|without)\b.+\bmetro\b", t):
        ct = normalize_client_type("metro")
        if ct:
            out.append(ct)
    for alias, canon in (
        ("online", "Online Customer"),
        ("canteen", "Canteen Store Department"),
        ("hashoo", "HASHOO GROUP"),
    ):
        if re.search(rf"\b(exclude|except|without)\b.+\b{alias}\b", t):
            if canon not in out:
                out.append(canon)
    return out


def looks_advanced(text: str) -> bool:
    """Keyword gate only — mode must come from the model tool args / plan_query."""
    t = (text or "").lower()
    return bool(
        re.search(
            r"\b("
            r"dumping|price\s+dispersion|price\s+spread|"
            r"silent\s+part|not\s+ordered|zero\s+this\s+week|"
            r"reactivated|days\s+since|week\s+over\s+week|\bwow\b|"
            r"expected\s+(sales|month)|seasonalit|"
            r"compare\s+(imtiaz|metro|lahore|karachi|channels?)|"
            r"vs\s+(imtiaz|metro|distributors?)|"
            r"top\s+skus?|filter\s+entit|party\s+profile|"
            r"concentration|grew\s+more\s+than|declined\s+more\s+than|"
            r"packing\s+is\s+growing|growing\s+fastest"
            r")\b",
            t,
        )
    )
