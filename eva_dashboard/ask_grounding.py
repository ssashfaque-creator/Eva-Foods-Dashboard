"""Auto entity grounding for the ReAct agent — resolve names before tools run."""

from __future__ import annotations

import json
import re
from typing import Any

from eva_dashboard.personal_lexicon import (
    expand_aliases_in_text,
    lexicon_prompt_block,
    remember_party_alias,
    sync_prefs_from_memory,
)


# Candidate tokens that look like customer nicknames (not stopwords)
_STOP = frozenset(
    {
        "show",
        "me",
        "the",
        "what",
        "whats",
        "what's",
        "who",
        "was",
        "were",
        "are",
        "is",
        "for",
        "last",
        "this",
        "month",
        "months",
        "year",
        "sales",
        "volume",
        "ams",
        "price",
        "rate",
        "fetch",
        "lowest",
        "highest",
        "avg",
        "average",
        "and",
        "with",
        "from",
        "in",
        "of",
        "to",
        "by",
        "top",
        "all",
        "only",
        "please",
        "give",
        "list",
        "table",
        "customer",
        "customers",
        "party",
        "parties",
        "distributor",
        "distributors",
        "eva",
        "maan",
        "bulk",
        "consumer",
        "lahore",
        "karachi",
        "multan",
        "islamabad",
        "north",
        "south",
        "central",
        "multiply",
        "divide",
        "times",
    }
)


def _candidate_needles(user_text: str, expansions: list[dict[str, str]]) -> list[str]:
    needles: list[str] = []
    for ex in expansions:
        m = str(ex.get("maps_to") or "").strip()
        if m and m not in needles:
            needles.append(m)
        sp = str(ex.get("spoken") or "").strip()
        if sp and sp not in needles:
            needles.append(sp)
    # Proper-noun-ish tokens / quoted phrases
    for m in re.finditer(r"[\"']([^\"']{3,40})[\"']", user_text or ""):
        needles.append(m.group(1).strip())
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9&.'-]{2,}", user_text or "")
    # Keep capitalized tokens and multi-word brands already in expansions
    for tok in tokens:
        low = tok.lower().strip(".")
        if low in _STOP or len(low) < 3:
            continue
        if tok[0].isupper() or low in {"pepsi", "pepsico", "imtiaz", "shaheer"}:
            if tok not in needles:
                needles.append(tok)
    # Dedupe preserving order
    out: list[str] = []
    seen: set[str] = set()
    for n in needles:
        k = n.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(n)
    return out[:6]


def ground_ask_for_agent(
    user_text: str,
    *,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve parties / aliases and build a prompt block for run_agent_loop."""
    from eva_dashboard.party_match import list_party_matches
    from eva_dashboard.semantic_grounding import ground_entities_for_prompt

    sync_prefs_from_memory(prior)
    _, expansions = expand_aliases_in_text(user_text)
    needles = _candidate_needles(user_text, expansions)

    party_hits: list[dict[str, Any]] = []
    for needle in needles:
        try:
            matches = list_party_matches(needle, limit=5, fuzzy=True)
        except Exception:  # noqa: BLE001
            matches = []
        if not matches:
            continue
        top = matches[0]
        party_hits.append(
            {
                "spoken": needle,
                "resolved": top,
                "alternates": matches[1:4],
                "unique": len(matches) == 1
                or (
                    len(matches) > 1
                    and matches[0].lower() == matches[1].lower()
                ),
            }
        )
        # Learn alias when confident
        if len(matches) == 1 or (
            len(needle) >= 4 and matches[0].upper().find(needle.upper()) >= 0
        ):
            remember_party_alias(needle, matches[0])

    glossary = ground_entities_for_prompt(user_text)
    lexicon = lexicon_prompt_block(user_text)

    parts: list[str] = []
    if lexicon:
        parts.append(lexicon.strip())
    if glossary:
        parts.append(glossary.strip())
    if party_hits:
        payload = {
            "parties": [
                {
                    "spoken": h["spoken"],
                    "use_in_sql_like": h["resolved"],
                    "alternates": h["alternates"],
                }
                for h in party_hits
            ],
            "rule": (
                "Use use_in_sql_like with LIKE '%…%' or filters.party / party_ilike. "
                "If alternates exist and the user was vague, prefer the top match "
                "but mention alternates in Analysis."
            ),
        }
        parts.append(
            "GROUNDED_PARTIES (resolved before tools):\n"
            f"{json.dumps(payload, indent=2, default=str)}"
        )

    return {
        "prompt_block": "\n\n".join(parts).strip(),
        "party_hits": party_hits,
        "expansions": expansions,
    }
