"""Golden Query RAG — few-shot retrieval for QuerySpec planning.

Industry-standard approach (Vanna / Cortex style): do NOT fine-tune on sales
rows. Store (natural language, correct QuerySpec) pairs and inject the top-k
nearest examples into the planner prompt.

Uses lightweight token-overlap retrieval by default (no ChromaDB required).
Optional OpenAI embeddings can be wired later without changing the API.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Stopwords that add little signal for retrieval
_STOP = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "for",
        "to",
        "and",
        "or",
        "is",
        "are",
        "was",
        "were",
        "me",
        "my",
        "this",
        "that",
        "with",
        "from",
        "by",
        "at",
        "as",
        "be",
        "it",
        "its",
        "please",
        "show",
        "give",
        "what",
        "how",
        "which",
        "when",
    }
)


def _tokenize(text: str) -> set[str]:
    toks = set(_TOKEN_RE.findall((text or "").lower()))
    return {t for t in toks if t not in _STOP and len(t) > 1}


def _overlap_score(query_toks: set[str], doc_toks: set[str]) -> float:
    if not query_toks or not doc_toks:
        return 0.0
    inter = len(query_toks & doc_toks)
    if not inter:
        return 0.0
    # Jaccard-ish with slight boost for query coverage
    union = len(query_toks | doc_toks)
    coverage = inter / max(len(query_toks), 1)
    return 0.55 * (inter / union) + 0.45 * coverage


def _package_golden_path() -> Path:
    return Path(__file__).resolve().parent / "golden_queries.json"


def _eval_golden_path() -> Path | None:
    # Dev / test checkout: tests/eval_answer_golden.json next to package root
    root = Path(__file__).resolve().parents[1]
    p = root / "tests" / "eval_answer_golden.json"
    return p if p.is_file() else None


def _load_json(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, dict):
        cases = raw.get("cases") or raw.get("queries") or raw.get("goldens") or []
    elif isinstance(raw, list):
        cases = raw
    else:
        return []
    out: list[dict[str, Any]] = []
    for c in cases:
        if not isinstance(c, dict):
            continue
        user = str(c.get("user_text") or c.get("question") or c.get("nl") or "").strip()
        plan = c.get("plan") or c.get("query_spec") or c.get("spec")
        if not user or not isinstance(plan, dict):
            continue
        out.append(
            {
                "id": str(c.get("id") or user[:40]),
                "user_text": user,
                "plan": plan,
                "prior": c.get("prior"),
                "tags": list(c.get("tags") or []),
                "notes": str(c.get("notes") or c.get("description") or ""),
            }
        )
    return out


@lru_cache(maxsize=1)
def load_golden_library() -> tuple[dict[str, Any], ...]:
    """Load packaged goldens + optional eval goldens (immutable tuple for cache)."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in (_package_golden_path(), _eval_golden_path()):
        if path is None:
            continue
        for item in _load_json(path):
            key = item["id"]
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
    return tuple(items)


def retrieve_golden_queries(
    user_text: str,
    *,
    k: int = 3,
    min_score: float = 0.12,
) -> list[dict[str, Any]]:
    """Return top-k golden (question, plan) pairs by token overlap."""
    q = _tokenize(user_text)
    if not q:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for item in load_golden_library():
        score = _overlap_score(q, _tokenize(item["user_text"]))
        # Soft boost when tags match tokens
        for tag in item.get("tags") or []:
            if _tokenize(str(tag)) & q:
                score += 0.05
        if score >= min_score:
            scored.append((score, item))
    scored.sort(key=lambda x: (-x[0], x[1]["id"]))
    return [it for _, it in scored[: max(1, k)]]


def format_goldens_for_prompt(
    user_text: str,
    *,
    k: int = 3,
) -> str:
    """Few-shot block injected beside MEMORY_CONTEXT."""
    hits = retrieve_golden_queries(user_text, k=k)
    if not hits:
        return (
            "GOLDEN_QUERY_EXAMPLES: none matched closely enough.\n"
            "Plan from MEMORY_CONTEXT + vocabulary + governed metrics."
        )
    lines = [
        "GOLDEN_QUERY_EXAMPLES (few-shot — mirror structure, adapt filters):\n"
    ]
    for i, hit in enumerate(hits, 1):
        plan = dict(hit["plan"])
        # Compact plan for token budget
        compact = {
            key: plan[key]
            for key in (
                "state_action",
                "context_handling",
                "operation",
                "row_dimensions",
                "column_dimensions",
                "metrics",
                "period_type",
                "months_back",
                "target_month",
                "filters",
                "clear_filters",
                "excludes",
                "metric_filters",
                "business_units",
                "extracted_entities",
            )
            if key in plan and plan[key] not in (None, "", [], {})
        }
        lines.append(f"Example {i} — Q: {hit['user_text']}")
        lines.append(f"QuerySpec: {json.dumps(compact, default=str)}")
        if hit.get("prior"):
            prior_f = (hit["prior"] or {}).get("filters") or {}
            if prior_f:
                lines.append(f"  (had prior filters: {json.dumps(prior_f, default=str)})")
        lines.append("")
    lines.append(
        "Use these as structural templates. Do NOT copy party/city names "
        "unless the user mentioned them."
    )
    return "\n".join(lines)


def clear_golden_cache() -> None:
    load_golden_library.cache_clear()
