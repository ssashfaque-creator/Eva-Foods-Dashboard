"""Spoken + planned thresholds on computed metrics (AMS > 10, growth > x, …).

Applied after aggregation so any ranking / price / pivot table can keep only
rows that satisfy the user's numeric cut — not hardcoded to one party or metric.
"""

from __future__ import annotations

import re
from typing import Any

# Canonical metric id → column name on result row dicts
METRIC_ROW_COLUMNS: dict[str, tuple[str, ...]] = {
    "ams": ("period_ams_mt", "ams_mt", "ams"),
    "ams_growth": ("ams_growth_pct", "ams_growth"),
    "volume": ("volume_mt", "volume", "mt"),
    "vs_ams": ("pct_vs_ams", "vs_ams"),
    "yoy": ("yoy_pct", "yoy"),
    "mom": ("mom_pct", "mom"),
    "last_price": ("last_price",),
    "avg_price": ("avg_price_incl_gst", "avg_price", "incl_gst_per_unit"),
    "price_fetch": ("price_fetch",),
    "invoices": ("invoices",),
}

_METRIC_ALIASES: list[tuple[str, str]] = [
    (r"ams\s*growth|growth\s+in\s+ams|growth\s*%|growth\s+percent|\bgrowth\b", "ams_growth"),
    (r"%\s*vs\s*ams|vs\.?\s*ams|versus\s+ams", "vs_ams"),
    (r"yoy|year\s*over\s*year|year\s+on\s+year", "yoy"),
    (r"mom|month\s*over\s*month|month\s+on\s+month|vs\.?\s*last\s+month", "mom"),
    (r"ams|average\s+monthly\s+sales", "ams"),
    (r"volume|\bsales\b|sales\s+mt|tonnage|\bmt\b", "volume"),
    (r"last\s+price|latest\s+price", "last_price"),
    (r"avg\s+price|average\s+price|average\s+rate", "avg_price"),
    (r"price\s*fetch", "price_fetch"),
    (r"invoices?", "invoices"),
]

_OPS = {
    "more than": "gt",
    "greater than": "gt",
    "over": "gt",
    "above": "gt",
    "at least": "gte",
    "or more": "gte",
    ">=": "gte",
    ">": "gt",
    "less than": "lt",
    "below": "lt",
    "under": "lt",
    "at most": "lte",
    "or less": "lte",
    "<=": "lte",
    "<": "lt",
    "equal to": "eq",
    "equals": "eq",
    "=": "eq",
}


def _norm(text: str) -> str:
    return " ".join(str(text or "").strip().lower().replace("-", " ").split())


def looks_yoy_period_compare(user_text: str) -> bool:
    """True when the user wants calendar YoY (same span last year), not AMS-window growth.

    Covers 'YoY', 'vs last year', and 'last N months vs the same N months last year'.
    """
    t = (user_text or "").lower()
    if not t.strip():
        return False
    return bool(
        re.search(
            r"\b("
            r"year\s+over\s+year|\byoy\b|year\s+on\s+year|"
            r"same\s+period\s+last\s+year|"
            r"same\s+\d+\s+months?\s+last\s+year|"
            r"same\s+(?:three|six|twelve)\s+months?\s+last\s+year|"
            r"same\s+months?\s+last\s+year|"
            r"corresponding\s+period\s+last\s+year|"
            r"same\s+(?:window|span|time|months?)\s+last\s+year|"
            r"last\s+year\s+same\s+(?:period|\d+\s+months?|months?)|"
            r"vs\.?\s*(?:the\s+)?(?:same\s+)?(?:\d+\s+months?\s+|months?\s+)?last\s+year|"
            r"versus\s+(?:the\s+)?(?:same\s+)?(?:\d+\s+months?\s+)?last\s+year|"
            r"compared?\s+(?:with|to)\s+(?:the\s+)?(?:same\s+)?(?:\d+\s+months?\s+)?"
            r"last\s+year|"
            r"last\s+\d+\s+months?\s+(?:vs\.?|versus|compared?\s+to).{0,60}last\s+year|"
            r"vs\.?\s+(?:the\s+)?previous\s+year|"
            r"year\s+ago"
            r")\b",
            t,
        )
    )


def _resolve_metric_name(blob: str) -> str | None:
    t = _norm(blob)
    for pat, canon in _METRIC_ALIASES:
        if re.search(rf"^{pat}$", t) or re.search(rf"\b{pat}\b", t):
            return canon
    return None


def _cut_entry(metric: str, op: str, value: float, *, pct: bool) -> dict[str, Any]:
    """Bind a parsed threshold to a canonical metric.

    A trailing % on AMS/volume is growth (\"less than 5% in AMS\"), not an
    AMS-tonnage cut. Bare \"AMS > 10\" stays AMS tons.
    """
    if pct and metric in {"ams", "volume"}:
        metric = "ams_growth"
    return {"metric": metric, "op": op, "value": value}


def parse_metric_filters(user_text: str) -> list[dict[str, Any]]:
    """Extract metric thresholds from spoken language."""
    # Preserve numeric negatives (AMS / YoY < -20) before dash→space norm
    raw = str(user_text or "")
    protected = re.sub(
        r"(?<![a-z0-9])-(\d+(?:\.\d+)?)",
        r"NEG\1",
        raw,
        flags=re.I,
    )
    t = _norm(protected).replace("neg", "-")
    if not t:
        return []
    found: list[dict[str, Any]] = []
    # "ams more than 10", "growth > 30%", "volume at least 5"
    op_alt = "|".join(
        re.escape(k) for k in sorted(_OPS.keys(), key=len, reverse=True)
    )
    metric_alt = (
        r"ams\s*growth|growth\s+in\s+ams|growth\s*%|\bgrowth\b|%\s*vs\s*ams|vs\.?\s*ams|"
        r"average\s+monthly\s+sales|ams|volume|\bsales\b|tonnage|\bmt\b|"
        r"yoy|year\s*over\s*year|year\s+on\s+year|"
        r"mom|month\s*over\s*month|month\s+on\s+month|vs\.?\s*last\s+month|"
        r"last\s+price|avg\s+price|price\s*fetch|invoices?"
    )
    pattern = re.compile(
        rf"(?:(?:customers?|parties?|rows?|accounts?)\s+with\s+)?"
        rf"(?:(?:only\s+show|show\s+only|keep\s+only|filter\s+to|"
        rf"exclude|excluding|without|drop|remove|except)\s+)?"
        rf"(?:(?:all\s+)?(?:customers?|parties?|rows?|accounts?)\s+"
        rf"(?:with|having)\s+)?"
        rf"(?P<metric>{metric_alt})"
        rf"\s*(?P<op>{op_alt})"
        rf"\s*(?P<value>-?\d+(?:\.\d+)?)\s*(?P<pct>%|percent|pct)?",
        flags=re.I,
    )
    for m in pattern.finditer(t):
        metric = _resolve_metric_name(m.group("metric"))
        if not metric:
            continue
        op = _OPS.get(m.group("op").lower().strip())
        if not op:
            continue
        value = float(m.group("value"))
        entry = _cut_entry(
            metric, op, value, pct=bool(m.group("pct"))
        )
        if entry not in found:
            found.append(entry)

    # Reversed: "less than 10 ams" / "more than 20 volume"
    rev = re.compile(
        rf"(?:(?:customers?|parties?|rows?)\s+with\s+)?"
        rf"(?P<op>{op_alt})"
        rf"\s*(?P<value>-?\d+(?:\.\d+)?)\s*(?P<pct>%|percent|pct)?\s*"
        rf"(?P<metric>{metric_alt})",
        flags=re.I,
    )
    for m in rev.finditer(t):
        metric = _resolve_metric_name(m.group("metric"))
        if not metric:
            continue
        op = _OPS.get(m.group("op").lower().strip())
        if not op:
            continue
        value = float(m.group("value"))
        entry = _cut_entry(
            metric, op, value, pct=bool(m.group("pct"))
        )
        if entry not in found:
            found.append(entry)

    # "exclude … less than 10 ams" → keep rows with AMS >= 10 (invert the cut)
    if re.search(
        r"\b(exclude|excluding|without|drop|remove|except|filter\s+out)\b",
        t,
    ) and re.search(r"\b(less\s+than|below|under|at\s+most|or\s+less|<=|<)\b", t):
        inverted: list[dict[str, Any]] = []
        flip = {"lt": "gte", "lte": "gt"}
        for entry in found:
            op = str(entry.get("op") or "")
            if op in flip:
                inverted.append(
                    {
                        "metric": entry["metric"],
                        "op": flip[op],
                        "value": entry["value"],
                    }
                )
            else:
                inverted.append(entry)
        if inverted:
            found = inverted

    # "grew more than 30%" / "dropped more than 20%" — default AMS growth,
    # but honor explicit yoy/mom in the same sentence.
    for m in re.finditer(
        rf"\b(grew|grown|gained|dropped|declined|fell)\s+"
        rf"(?P<op>{op_alt})\s*(?P<value>\d+(?:\.\d+)?)\s*(%|percent|pct)?",
        t,
        flags=re.I,
    ):
        op = _OPS.get(m.group("op").lower().strip())
        if not op:
            continue
        value = float(m.group("value"))
        verb = m.group(1).lower()
        metric = "ams_growth"
        if looks_yoy_period_compare(raw) or re.search(
            r"\b(yoy|year\s*over\s*year)\b", t
        ):
            metric = "yoy"
        elif re.search(r"\b(mom|month\s*over\s*month|last\s+month)\b", t):
            metric = "mom"
        if verb in {"dropped", "declined", "fell"} and op == "gt":
            entry = {"metric": metric, "op": "lt", "value": -abs(value)}
        else:
            entry = {"metric": metric, "op": op, "value": value}
        if entry not in found:
            found.append(entry)
    # Calendar YoY language ("same 3 months last year") → growth cuts are yoy,
    # not AMS-window growth (which is the previous 3-month AMS vs current AMS).
    if looks_yoy_period_compare(raw):
        remapped: list[dict[str, Any]] = []
        for entry in found:
            if str(entry.get("metric") or "") == "ams_growth":
                remapped.append({**entry, "metric": "yoy"})
            else:
                remapped.append(entry)
        found = remapped
    return found


def looks_like_metric_threshold_phrase(text: str) -> bool:
    """True when a fragment is an AMS/volume cut, not a customer name."""
    raw = (text or "").strip()
    if not raw:
        return False
    if not re.search(
        r"\b(ams|volume|growth|yoy|mom|sales|average\s+monthly)\b",
        raw,
        flags=re.I,
    ):
        return False
    if not re.search(
        r"\b(less\s+than|more\s+than|greater\s+than|below|above|under|"
        r"over|at\s+least|at\s+most|>|<|>=|<=)\b",
        raw,
        flags=re.I,
    ):
        return False
    return bool(parse_metric_filters(raw))


def looks_metric_threshold_exclude(user_text: str) -> bool:
    """True when 'exclude … less than 10 ams' is a metric cut, not a party name."""
    return looks_like_metric_threshold_phrase(user_text) and bool(
        re.search(
            r"\b(exclude|excluding|without|drop|remove|except|filter\s+out)\b",
            (user_text or "").lower(),
        )
    )


def merge_metric_filters(
    existing: list[dict[str, Any]] | None,
    spoken: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for src in (existing or []) + (spoken or []):
        if not isinstance(src, dict):
            continue
        metric = str(src.get("metric") or "").strip()
        op = str(src.get("op") or "").strip().lower()
        if metric not in METRIC_ROW_COLUMNS or op not in {
            "gt",
            "gte",
            "lt",
            "lte",
            "eq",
        }:
            continue
        try:
            value = float(src.get("value"))
        except (TypeError, ValueError):
            continue
        entry = {"metric": metric, "op": op, "value": value}
        if entry not in out:
            out.append(entry)
    return out


def _row_metric_value(row: dict[str, Any], metric: str) -> float | None:
    for key in METRIC_ROW_COLUMNS.get(metric) or ():
        if key not in row:
            continue
        val = row.get(key)
        if val is None or val == "":
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def row_passes_metric_filters(
    row: dict[str, Any],
    filters: list[dict[str, Any]] | None,
) -> bool:
    if not filters:
        return True
    for f in filters:
        metric = str(f.get("metric") or "")
        op = str(f.get("op") or "")
        try:
            threshold = float(f.get("value"))
        except (TypeError, ValueError):
            continue
        val = _row_metric_value(row, metric)
        if val is None:
            return False
        if op == "gt" and not (val > threshold):
            return False
        if op == "gte" and not (val >= threshold):
            return False
        if op == "lt" and not (val < threshold):
            return False
        if op == "lte" and not (val <= threshold):
            return False
        if op == "eq" and not (abs(val - threshold) < 1e-9):
            return False
    return True


def apply_metric_filters(
    rows: list[dict[str, Any]],
    filters: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not filters or not rows:
        return rows
    return [r for r in rows if row_passes_metric_filters(r, filters)]


def metric_filters_blurb(filters: list[dict[str, Any]] | None) -> str:
    if not filters:
        return ""
    bits: list[str] = []
    sym = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "eq": "="}
    for f in filters:
        bits.append(
            f"{f.get('metric')} {sym.get(str(f.get('op')), f.get('op'))} {f.get('value')}"
        )
    return " · ".join(bits)
