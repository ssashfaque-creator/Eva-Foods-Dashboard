"""Governed metrics / dimensions / operation synonyms for plan_query.

Loaded from ``metrics_catalog.json`` (semantic layer as data). Used to:
- teach the model via the system prompt
- resolve spoken management language → canonical QuerySpec fields
- soft-fill missing metrics/operations in the executor
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_CATALOG_PATH = Path(__file__).resolve().parent / "metrics_catalog.json"

# Canonical pivot metrics (must stay aligned with query_spec.PIVOT_METRICS)
CANONICAL_METRICS = (
    "volume",
    "avg_price",
    "price_fetch",
    "ams",
    "vs_ams",
    "ams_growth",
)


@lru_cache(maxsize=1)
def load_metrics_catalog() -> dict[str, Any]:
    if not _CATALOG_PATH.exists():
        return {"version": "0", "metrics": {}, "dimensions": {}, "operations": {}}
    return json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))


def _norm(text: str) -> str:
    t = (text or "").lower().strip()
    t = t.replace("%", " percent ")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _synonym_index(section: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (normalized_synonym, canonical_id) sorted longest-first."""
    pairs: list[tuple[str, str]] = []
    for canon, meta in (section or {}).items():
        syns = list((meta or {}).get("synonyms") or [])
        syns.append(canon.replace("_", " "))
        syns.append(canon)
        for s in syns:
            n = _norm(str(s))
            if n:
                pairs.append((n, str(canon)))
    pairs.sort(key=lambda x: (-len(x[0]), x[0], x[1]))
    # de-dupe keeping longest / first
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for syn, canon in pairs:
        if syn in seen:
            continue
        seen.add(syn)
        out.append((syn, canon))
    return out


# Synonyms too broad to auto-inject unless the plan is metric-empty / explicit.
_WEAK_METRIC_SYNS = frozenset(
    {
        "sales",
        "volume",
        "mt",
        "qty",
        "quantity",
        "growth",
        "grew",
        "gains",
        "rate",
        "price",
        "ams",
    }
)


def resolve_metrics_from_text(
    text: str,
    *,
    include_weak: bool = False,
) -> list[str]:
    """Map spoken phrases in ``text`` to canonical metric ids (order preserved)."""
    catalog = load_metrics_catalog()
    blob = f" {_norm(text)} "
    found: list[str] = []
    for syn, canon in _synonym_index(catalog.get("metrics") or {}):
        if canon not in CANONICAL_METRICS:
            continue
        if not include_weak and syn in _WEAK_METRIC_SYNS:
            continue
        if f" {syn} " in blob and canon not in found:
            found.append(canon)
    # Ambiguity: bare "price" → avg_price unless Price Fetch language won
    if "price_fetch" in found and "avg_price" in found:
        # Prefer price_fetch when both matched from overlapping words
        if re.search(r"price\s*fetch|recovery|cost\s*factor", (text or "").lower()):
            found = [m for m in found if m != "avg_price"]
        else:
            found = [m for m in found if m != "price_fetch"]
    return found


def resolve_dimensions_from_text(text: str) -> list[str]:
    catalog = load_metrics_catalog()
    blob = f" {_norm(text)} "
    found: list[str] = []
    for syn, canon in _synonym_index(catalog.get("dimensions") or {}):
        if f" {syn} " in blob and canon not in found:
            found.append(canon)
    return found


def resolve_operation_from_text(text: str) -> str | None:
    catalog = load_metrics_catalog()
    blob = f" {_norm(text)} "
    for syn, canon in _synonym_index(catalog.get("operations") or {}):
        if f" {syn} " in blob:
            return canon
    return None


def metric_default_sort(metric: str) -> str | None:
    meta = (load_metrics_catalog().get("metrics") or {}).get(metric) or {}
    sort = meta.get("default_sort")
    return str(sort) if sort in {"asc", "desc"} else None


def apply_metric_synonyms_to_spec(
    spec: dict[str, Any],
    user_text: str,
) -> dict[str, Any]:
    """Soft-fill metrics / operation from governed synonyms when the plan is thin.

    Does not remove metrics the planner already chose. Only adds missing
    canonical metrics clearly requested in the user text, or promotes
    party_profile when operation synonyms match.
    """
    out = dict(spec)
    text = user_text or ""
    if not text.strip():
        return out

    metrics = list(out.get("metrics") or [])
    # Prefer strong synonyms always; allow weak ones only when planner omitted metrics
    inferred = resolve_metrics_from_text(text, include_weak=not bool(metrics))
    for m in inferred:
        if m not in metrics:
            metrics.append(m)
    if metrics:
        out["metrics"] = metrics

    # Sort hint for performance metrics when omitted
    if not out.get("sort") or out.get("sort") == "desc":
        for m in metrics:
            default = metric_default_sort(m)
            if default and re.search(
                r"\b(lowest|least|worst|behind|falling|smallest|bottom)\b",
                text.lower(),
            ):
                out["sort"] = default
                break

    op = str(out.get("operation") or "pivot")
    op_hint = resolve_operation_from_text(text)
    if op_hint == "party_lookup" and op in {"", "pivot", "None", "none", "party_profile"}:
        # "who is X" wins over profile when both synonyms could match
        if re.search(r"\b(who\s+is|who'?s)\b", text.lower()):
            out["operation"] = "party_lookup"
            out["intent"] = "party_lookup"
    if op_hint == "party_profile" and op in {"", "pivot", "None", "none"}:
        # Only promote when a party scope exists or entities were extracted
        filters = dict(out.get("filters") or {})
        if (
            out.get("party_query")
            or filters.get("party")
            or filters.get("parties")
            or filters.get("party_ilike")
            or out.get("extracted_entities")
        ):
            out["operation"] = "party_profile"
            out["intent"] = "party_profile"
            if not out.get("row_dimensions"):
                out["row_dimensions"] = ["party"]
            if not out.get("metrics"):
                out["metrics"] = ["volume", "ams", "vs_ams"]

    # Dimension grain from synonyms when rows omitted
    rows = list(out.get("row_dimensions") or [])
    if not rows:
        dims = resolve_dimensions_from_text(text)
        for d in dims:
            if d == "month":
                cols = list(out.get("column_dimensions") or [])
                if "month" not in cols:
                    out["column_dimensions"] = cols + ["month"]
            elif d not in rows:
                rows.append(d)
        if rows:
            out["row_dimensions"] = rows

    return out


def metrics_for_prompt() -> str:
    """Compact governed-metric block for the system prompt."""
    catalog = load_metrics_catalog()
    lines = [
        "GOVERNED METRICS (canonical ids — use these in plan_query.metrics):",
        f"(catalog v{catalog.get('version', '?')})",
        "",
    ]
    for canon, meta in (catalog.get("metrics") or {}).items():
        syns = ", ".join(list((meta or {}).get("synonyms") or [])[:8])
        label = (meta or {}).get("label") or canon
        desc = (meta or {}).get("description") or ""
        lines.append(f"- {canon} — {label}: {desc}")
        if syns:
            lines.append(f"  spoken ← {syns}")
    lines.append("")
    lines.append("GOVERNED OPERATIONS (optional plan_query.operation):")
    for canon, meta in (catalog.get("operations") or {}).items():
        syns = ", ".join(list((meta or {}).get("synonyms") or [])[:6])
        lines.append(f"- {canon} ← {syns}")
    lines.append("")
    lines.append("GOVERNED DIMENSIONS (row_dimensions / column_dimensions):")
    for canon, meta in (catalog.get("dimensions") or {}).items():
        syns = ", ".join(list((meta or {}).get("synonyms") or [])[:6])
        lines.append(f"- {canon} ← {syns}")
    return "\n".join(lines)
