"""Spoken constraint polarity — INCLUDE vs EXCLUDE from user language.

This is the semantic layer the planner must not override:

* ``filters.*`` / ``business_units`` = INCLUDE (keep only these)
* ``excludes.*`` = EXCLUDE (drop these from an otherwise open result)

The LLM often puts an excluded name into ``filters.party`` (include).
Python owns polarity from the user sentence so that mistake cannot survive
for *any* entity (party, city, channel, BU, packing, …) — not one-off names.

Examples all resolve the same way:
  "Lahore Eva sales but exclude al shaheer"
  "show metro without donations"
  "Karachi sales except Maan Bulk"
  "remove inactive and exclude sample parties"
"""

from __future__ import annotations

import re
from typing import Any

from eva_dashboard.query_spec import PARTY_SCOPE_KEYS

_EXCLUDE_VERBS = (
    r"(?:exclude|excluding|remove|without|drop|hide|filter\s+out|"
    r"except|excepting|but\s+not)"
)

# Dim keys users clear by name ("remove the city filter")
_CLEAR_FILTER_ALIASES: dict[str, str] = {
    "city": "city",
    "cities": "city",
    "client type": "client_type",
    "client types": "client_type",
    "channel": "client_type",
    "channels": "client_type",
    "zone": "zone",
    "zones": "zone",
    "party": "party",
    "parties": "party",
    "customer": "party",
    "customers": "party",
    "business unit": "business_units",
    "business units": "business_units",
    "bu": "business_units",
}


def _norm(text: str) -> str:
    return " ".join(str(text or "").strip().lower().replace("-", " ").split())


def extract_clear_filter_keys(user_text: str) -> list[str]:
    """Parse 'remove/clear the city and client type filter(s)' → filter keys.

    These are MEMORY clears, not party excludes.
    """
    t = (user_text or "").strip()
    if not t:
        return []
    keys: list[str] = []
    for m in re.finditer(
        r"\b(?:remove|clear|drop|delete|unset)\s+"
        r"(?:the\s+)?(.+?)\s+filters?\b",
        t,
        flags=re.IGNORECASE,
    ):
        chunk = re.sub(r"\s+", " ", m.group(1)).strip(" .,!?;:")
        # Split "city and client type" / "city, zone"
        parts = re.split(r"\s*(?:,|/|and|&)\s*", chunk, flags=re.IGNORECASE)
        for part in parts:
            p = _norm(part)
            p = re.sub(r"^(the|a|an)\s+", "", p).strip()
            mapped = _CLEAR_FILTER_ALIASES.get(p)
            if mapped and mapped not in keys:
                keys.append(mapped)
    # "doesn't have to be Lahore" / typo "docent have to be lahore"
    if looks_optional_city_scope(t) and "city" not in keys:
        keys.append("city")
    return keys


def looks_optional_city_scope(user_text: str) -> bool:
    """True when the user says the city lock is optional / not required."""
    t = _norm(user_text)
    if not t:
        return False
    return bool(
        re.search(
            r"\b(doesn'?t|doesnt|dont|do not|docent|dosent|dosen'?t)\s+"
            r"have\s+to\s+be\b|"
            r"\b(doesn'?t|doesnt|dont|do not|docent|dosent|dosen'?t)\s+"
            r"need\s+to\s+be\b|"
            r"\bnot\s+necessarily\b|"
            r"\b(no\s+need\s+for|need\s+not\s+be)\b|"
            r"\b(doesn'?t|doesnt)\s+have\s+to\s+remain\b|"
            r"\bwithout\s+(a\s+)?city\s+filter\b|"
            r"\b(any|all)\s+cit(y|ies)\b",
            t,
        )
    )


def extract_exclude_phrases(user_text: str) -> list[str]:
    """Raw phrases the user asked to drop (polarity = exclude)."""
    t = (user_text or "").strip()
    if not t:
        return []
    # Strip clear-filter clauses so "remove the city filter and include…"
    # is not treated as excluding a party named "city".
    scrubbed = re.sub(
        r"\b(?:remove|clear|drop|delete|unset)\s+"
        r"(?:the\s+)?.+?\s+filters?\b",
        " ",
        t,
        flags=re.IGNORECASE,
    )
    phrases: list[str] = []
    # "... exclude X" / "without X" / "except X"
    # Stop before a new INCLUDE ask: "exclude al shaheer and show me Eva…"
    for m in re.finditer(
        rf"\b{_EXCLUDE_VERBS}\s+"
        r"(?:the\s+)?(.+?)(?="
        rf"\s+(?:and|,|;)\s+{_EXCLUDE_VERBS}\b|"
        r"\s+and\s+include\b|"
        r"\s+and\s+(?:then\s+)?"
        r"(?:show|display|list|give|return|pull|fetch|get)\b|"
        r"\s+then\s+(?:show|display|list|give)\b|"
        r"\s+but\s+(?!not\b|exclude|excluding|remove|except)|"
        r"$)",
        scrubbed,
        flags=re.IGNORECASE,
    ):
        raw = re.sub(r"\s+", " ", m.group(1)).strip(" .,!?;:")
        # Strip follow-up tails: "al shaheer from this data/table"
        raw = re.sub(
            r"\s+from\s+(this|the)\s+(data|table|view|result|grid|matrix)\s*$",
            "",
            raw,
            flags=re.IGNORECASE,
        ).strip()
        raw = re.sub(
            r"\s+(items?|rows?|sales?|volumes?|data|again)\s*$",
            "",
            raw,
            flags=re.IGNORECASE,
        ).strip()
        # Ignore leftover filter-clear fragments / include tails
        if re.search(
            r"\b(filter|include|identified|first\s+\d+|both)\b",
            raw,
            flags=re.IGNORECASE,
        ):
            continue
        if raw and raw not in phrases:
            phrases.append(raw)
    # "but exclude X" / "again but without X"
    for m in re.finditer(
        rf"\bbut\s+(?:please\s+)?{_EXCLUDE_VERBS}\s+(.+)$",
        scrubbed,
        flags=re.IGNORECASE,
    ):
        raw = re.sub(r"\s+", " ", m.group(1)).strip(" .,!?;:")
        if re.search(r"\b(filter|include|identified)\b", raw, flags=re.IGNORECASE):
            continue
        if raw and raw not in phrases:
            phrases.append(raw)
    return phrases


def resolve_exclude_map(
    user_text: str,
    *,
    prior_spec: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Map spoken exclude phrases → ``excludes`` dict (any dimension)."""
    from eva_dashboard.chatbot import (
        _prior_units_list,
        _resolve_exclude_value,
        _resolve_segment_business_units,
        _split_remove_value_phrases,
    )

    out: dict[str, list[str]] = {}
    prior_units = _prior_units_list(prior_spec) if prior_spec else []
    for phrase in extract_exclude_phrases(user_text):
        for part in _split_remove_value_phrases(phrase) or [phrase]:
            part_s = str(part or "").strip()
            # Bare "bulk" / "consumer" → brand Bulk/Consumer BUs (not party_like)
            segment_units = _resolve_segment_business_units(
                part_s, prior_units=prior_units
            )
            if segment_units:
                bucket = out.setdefault("business_unit", [])
                for u in segment_units:
                    if u not in bucket:
                        bucket.append(u)
                continue
            resolved = _resolve_exclude_value(part_s)
            if not resolved:
                if len(part_s) >= 3:
                    resolved = ("party_like", part_s)
                else:
                    continue
            dim, val = resolved
            bucket = out.setdefault(dim, [])
            if val not in bucket:
                bucket.append(val)
            # Exact party → also fragment so sister/unmapped spellings drop
            if dim == "party" and len(part_s) >= 3 and part_s not in out.get(
                "party_like", []
            ):
                out.setdefault("party_like", []).append(part_s)
    return out


def party_exclude_needles(excludes: dict[str, Any] | None) -> list[str]:
    needles: list[str] = []
    for key in ("party", "party_like", "parties"):
        for v in (excludes or {}).get(key) or []:
            n = _norm(str(v))
            if n and n not in needles:
                needles.append(n)
    return needles


def _hits_party(val: Any, needles: list[str]) -> bool:
    nv = _norm(str(val or ""))
    if not nv or not needles:
        return False
    return any(n in nv or nv in n for n in needles)


def strip_include_conflicts(
    spec: dict[str, Any],
    excludes: dict[str, Any] | None,
) -> dict[str, Any]:
    """Remove INCLUDE filters that collide with EXCLUDE polarity."""
    out = dict(spec)
    ex = excludes or {}
    filters = dict(out.get("filters") or {})
    needles = party_exclude_needles(ex)

    def _drop_list(key: str, bad: set[str]) -> None:
        cur = [str(v) for v in (filters.get(key) or []) if v]
        if not cur:
            return
        kept = [v for v in cur if _norm(v) not in bad and not _hits_party(v, list(bad))]
        if kept:
            filters[key] = kept
        else:
            filters.pop(key, None)

    # Party polarity
    if needles:
        if _hits_party(filters.get("party"), needles):
            filters.pop("party", None)
        if _hits_party(out.get("party_query"), needles):
            out["party_query"] = None
        parties = [p for p in (filters.get("parties") or []) if not _hits_party(p, needles)]
        if parties:
            filters["parties"] = parties
        else:
            filters.pop("parties", None)
        ilike = [p for p in (filters.get("party_ilike") or []) if not _hits_party(p, needles)]
        if ilike:
            filters["party_ilike"] = ilike
        else:
            filters.pop("party_ilike", None)
        out["extracted_entities"] = [
            e
            for e in (out.get("extracted_entities") or [])
            if not _hits_party(e, needles)
        ]

    # Same-dimension value excludes (city / channel / BU / oil / packing)
    for dim in ("city", "zone", "client_type", "oil_type", "packing_category", "product"):
        bad = {_norm(v) for v in (ex.get(dim) or []) if v}
        if not bad:
            continue
        if _norm(str(filters.get(dim) or "")) in bad:
            filters.pop(dim, None)
        plural = f"{dim}s" if dim != "client_type" else "client_types"
        if dim == "client_type":
            plural = "client_types"
        elif dim == "city":
            plural = "cities"
        _drop_list(plural, bad)

    bad_bus = {_norm(v) for v in (ex.get("business_unit") or []) if v}
    if bad_bus:
        bus = [
            b
            for b in (out.get("business_units") or filters.get("business_units") or [])
            if _norm(str(b)) not in bad_bus
        ]
        out["business_units"] = bus
        if bus:
            filters["business_units"] = bus
        else:
            filters.pop("business_units", None)
            filters.pop("business_unit", None)
        if _norm(str(filters.get("business_unit") or "")) in bad_bus:
            filters.pop("business_unit", None)

    out["filters"] = filters
    return out


def apply_spoken_constraints(
    spec: dict[str, Any],
    *,
    user_text: str = "",
) -> dict[str, Any]:
    """Authoritative polarity pass: spoken EXCLUDE wins over INCLUDE filters.

    Call after planner merge / entity resolve / silent party match so nothing
    can re-introduce an included entity the user asked to drop.
    Also merges spoken metric thresholds (AMS > 10, growth > x, …).
    """
    from eva_dashboard.metric_filters import merge_metric_filters, parse_metric_filters

    out = dict(spec)
    # Metric thresholds are independent of include/exclude polarity
    out["metric_filters"] = merge_metric_filters(
        list(out.get("metric_filters") or []),
        parse_metric_filters(user_text) if user_text else [],
    )

    spoken_ex = (
        resolve_exclude_map(
            user_text,
            prior_spec={
                "business_units": list(out.get("business_units") or []),
                "filters": dict(out.get("filters") or {}),
            },
        )
        if user_text
        else {}
    )
    excludes = dict(out.get("excludes") or {})
    for dim, vals in spoken_ex.items():
        bucket = list(excludes.get(dim) or [])
        for v in vals or []:
            if v not in bucket:
                bucket.append(v)
        excludes[dim] = bucket
    if not excludes:
        return out
    out["excludes"] = excludes

    # Party excludes → clear entire party INCLUDE scope (sticky prior cannot win)
    if excludes.get("party") or excludes.get("party_like") or excludes.get("parties"):
        clear = list(out.get("clear") or [])
        for key in (*PARTY_SCOPE_KEYS, "party_query"):
            if key not in clear:
                clear.append(key)
        out["clear"] = clear
        out["clear_filters"] = list(clear)
        out["_clear_omitted"] = False
        filters = dict(out.get("filters") or {})
        for key in PARTY_SCOPE_KEYS:
            filters.pop(key, None)
        out["filters"] = filters
        out["party_query"] = None

    return strip_include_conflicts(out, excludes)


def polarity_brief_for_prompt() -> str:
    """Short contract injected into the system prompt."""
    return """
=== FILTER POLARITY (CRITICAL) ===
filters.* / business_units = INCLUDE. excludes.* = EXCLUDE.
exclude/remove/without/except/but not → excludes only — NEVER filters.party /
filters.city / filters.client_type / extracted_entities.
Ex: "Eva Lahore sales but exclude al shaheer" →
  filters:{city:Lahore}, business_units:[Eva Consumer,Eva Bulk],
  excludes:{party_like:[al shaheer]}  (NOT filters.party).
Ex: "without donations" → excludes:{client_type:[DONATIONS]}.
Python enforces polarity from the user sentence if the plan inverts it.
""".strip()
