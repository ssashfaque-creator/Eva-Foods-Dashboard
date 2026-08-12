"""Intent router — classify asks before the ReAct agent picks tools.

Routes:
  standard  → legacy QuerySpec engines (AMS / volume / ranks / Price Fetch)
  discovery → guarded SQL (min/max, who-at-rate, dispersion, ad-hoc)
  math      → calculator (after numbers exist); often paired with discovery
  clarify   → ask one short question when the ask is too ambiguous
"""

from __future__ import annotations

import re
from typing import Any, Literal

AskKind = Literal["standard", "discovery", "math", "clarify", "mixed"]

# Price / rate discovery (not period avg rate matrices)
_DISCOVERY = re.compile(
    r"\b("
    r"lowest|highest|minimum|maximum|min\b|max\b|"
    r"who\s+(bought|paid|got|was\s+sold)|"
    r"sold\s+to|buyer|at\s+that\s+(price|rate)|"
    r"same[- ]?date|on\s+the\s+same\s+day|"
    r"price\s+dispers|price\s+varian|different\s+price|"
    r"different\s+rate|rate\s+differ|"
    r"which\s+.+\s+at\s+(a\s+)?(different|higher|lower)\s+(price|rate)"
    r")\b",
    flags=re.I,
)

_MATH = re.compile(
    r"\b("
    r"multiply|multiplied|divide|divided|times\b|"
    r"\*|×|÷|"
    r"convert|conversion|factor\s+of|"
    r"\d+(?:\.\d+)?\s*[x×*]\s*\d|"
    r"divide\s+by|divided\s+by"
    r")\b|"
    # Require an explicit division slash (avoid matching bare years like "2025")
    r"(?<![A-Za-z0-9])/\s*\d+(?:\.\d+)?\b",
    flags=re.I,
)

_STANDARD = re.compile(
    r"\b("
    r"ams|vs\.?\s*ams|average\s+monthly|"
    r"volume|tonnage|\bmt\b|"
    r"business\s+unit|bu[- ]?wise|"
    r"last\s+\d+\s+months|trend|month[- ]?wise|"
    r"grown|growth|declined|vs\s+ams|"
    r"price\s*fetch|cost\s*factor|"
    r"top\s+\d+|party\s+rank|customer[- ]?wise|"
    r"who\s+is\b|who'?s\b"
    r")\b",
    flags=re.I,
)

# Bare "price" without fetch/avg/last/lowest → often ambiguous
_AMBIGUOUS_PRICE = re.compile(
    r"\b(what'?s|what\s+is|show|give|tell)\s+(the\s+)?(pepsi\s+)?price\b|"
    r"\b(pepsi|imtiaz|customer|party)\s+price\b|"
    r"\bprice\s+of\s+\w+\b",
    flags=re.I,
)

_HAS_PRICE_QUALIFIER = re.compile(
    r"\b("
    r"avg|average|last|latest|fetch|lowest|highest|min|max|"
    r"rate|sold\s+at|incl\s*gst"
    r")\b",
    flags=re.I,
)


def route_ask(user_text: str, *, prior: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify the user ask and return routing guidance for the agent."""
    t = (user_text or "").strip()
    low = t.lower()
    if not t:
        return {
            "kind": "clarify",
            "confidence": 0.2,
            "preferred_tools": [],
            "blocked_tools": [],
            "clarify_question": "What would you like to see — volume, AMS, price, or a specific customer?",
            "rationale": "empty ask",
            "prompt_block": "",
        }

    discovery = bool(_DISCOVERY.search(low))
    math = bool(_MATH.search(low))
    standard = bool(_STANDARD.search(low))

    # Ambiguous bare price (no qualifier, no prior price context)
    ambiguous_price = bool(_AMBIGUOUS_PRICE.search(low)) and not _HAS_PRICE_QUALIFIER.search(
        low
    )
    prior_has_price = bool(
        prior
        and (
            set(prior.get("metrics") or []) & {"avg_price", "price_fetch", "last_price"}
            or prior.get("price_spec")
        )
    )

    # Learned price preference → skip clarify and apply default
    learned_price: str | None = None
    if ambiguous_price and not prior_has_price and not discovery and not math:
        try:
            from eva_dashboard.personal_lexicon import (
                default_price_metric,
                parse_price_preference,
                remember_price_preference_from_text,
            )

            # If this turn already names a type, remember it
            spoken = parse_price_preference(t)
            if spoken:
                remember_price_preference_from_text(t)
                learned_price = spoken
            else:
                learned_price = default_price_metric()
        except Exception:  # noqa: BLE001
            learned_price = None

    kind: AskKind
    confidence: float
    preferred: list[str]
    blocked: list[str] = []
    clarify_q: str | None = None
    rationale: str

    if (
        ambiguous_price
        and not prior_has_price
        and not discovery
        and not math
        and learned_price
    ):
        # Clarify-once → default forever
        if learned_price in {"price_fetch", "avg_price", "last_price"}:
            kind = "standard"
            confidence = 0.8
            preferred = ["run_standard_analytics_pivot"]
            if learned_price == "price_fetch":
                blocked = ["execute_read_only_sql"]
            rationale = f"bare price → learned pref {learned_price}"
        else:
            kind = "discovery"
            confidence = 0.8
            preferred = [
                "lookup_entity_values",
                "execute_read_only_sql",
                "run_standard_analytics_pivot",
            ]
            rationale = f"bare price → learned pref {learned_price}"
    elif ambiguous_price and not prior_has_price and not discovery and not math:
        kind = "clarify"
        confidence = 0.55
        preferred = []
        clarify_q = (
            "Which price — average rate, last sold, lowest rate, or Price Fetch?"
        )
        rationale = "bare price ask without qualifier"
    elif discovery and math:
        kind = "mixed"
        confidence = 0.85
        preferred = [
            "lookup_entity_values",
            "execute_read_only_sql",
            "calculate_expression",
        ]
        blocked = []  # allow legacy if needed, but prefer SQL+math
        rationale = "discovery + arithmetic"
    elif discovery:
        kind = "discovery"
        confidence = 0.9
        preferred = ["lookup_entity_values", "execute_read_only_sql", "get_database_schema"]
        # Soft-block reinventing AMS in SQL via prompt; hard block below for pure AMS
        blocked = []
        rationale = "min/max / who-at-rate / dispersion style ask"
    elif math and not standard:
        kind = "math"
        confidence = 0.8
        preferred = [
            "lookup_entity_values",
            "execute_read_only_sql",
            "run_standard_analytics_pivot",
            "calculate_expression",
        ]
        rationale = "arithmetic on a fetched number"
    elif standard and not discovery:
        kind = "standard"
        confidence = 0.9 if re.search(r"\b(ams|volume|price\s*fetch)\b", low) else 0.75
        preferred = ["run_standard_analytics_pivot"]
        # Hard preference: don't invent AMS windows / growth in raw SQL
        if re.search(
            r"\b("
            r"ams|vs\.?\s*ams|price\s*fetch|cost\s*factor|"
            r"grown|growth|declined|grown_only|declined_only"
            r")\b",
            low,
        ):
            blocked = ["execute_read_only_sql"]
        rationale = "standard commercial pivot / rank / Price Fetch"
    elif math and standard:
        kind = "mixed"
        confidence = 0.8
        preferred = ["run_standard_analytics_pivot", "calculate_expression"]
        rationale = "standard table then arithmetic"
    else:
        # Default: let agent choose, slight lean to legacy for sales language
        if re.search(r"\b(sales?|show|table|by\s+bu|by\s+city)\b", low):
            kind = "standard"
            confidence = 0.55
            preferred = ["run_standard_analytics_pivot"]
            rationale = "generic sales ask — prefer legacy engines"
        else:
            kind = "discovery"
            confidence = 0.5
            preferred = [
                "get_database_schema",
                "lookup_entity_values",
                "execute_read_only_sql",
            ]
            rationale = "unclassified — allow discovery SQL"

    prompt_block = _prompt_block(
        kind=kind,
        confidence=confidence,
        preferred=preferred,
        blocked=blocked,
        clarify_q=clarify_q,
        rationale=rationale,
    )
    return {
        "kind": kind,
        "confidence": confidence,
        "preferred_tools": preferred,
        "blocked_tools": blocked,
        "clarify_question": clarify_q,
        "rationale": rationale,
        "prompt_block": prompt_block,
    }


def _prompt_block(
    *,
    kind: str,
    confidence: float,
    preferred: list[str],
    blocked: list[str],
    clarify_q: str | None,
    rationale: str,
) -> str:
    lines = [
        "=== ROUTING (authoritative) ===",
        f"kind={kind} confidence={confidence:.2f} ({rationale})",
    ]
    if preferred:
        lines.append("Prefer tools (in order): " + ", ".join(preferred))
    if blocked:
        lines.append(
            "Do NOT call: "
            + ", ".join(blocked)
            + " — use run_standard_analytics_pivot for AMS / Price Fetch instead."
        )
    if kind == "clarify" and clarify_q:
        lines.append(
            "AMBIGUOUS ASK: reply with ONLY this clarifying question "
            f"(no tools): {clarify_q}"
        )
    # Surface learned price default when routing applied it
    if "learned pref" in rationale:
        lines.append(
            "Use the learned default_price_metric from PERSONAL_LEXICON; "
            "state the assumption in one short line."
        )
    if kind in {"discovery", "mixed"}:
        lines.append(
            "For volume in SQL use: CASE WHEN COALESCE(mt_qty,0)<>0 THEN mt_qty "
            "WHEN lower(unit) IN ('kg','kgs') THEN qty/1000.0 "
            "WHEN lower(unit) IN ('mt','ton','tons') THEN qty ELSE 0 END. "
            "Never invent AMS or Price Fetch formulas in SQL."
        )
    if kind in {"math", "mixed"}:
        lines.append(
            "Fetch the number with a tool first, then call calculate_expression — "
            "never multiply/divide in your head."
        )
    return "\n".join(lines)


def tool_allowed(tool_name: str, route: dict[str, Any] | None) -> tuple[bool, str]:
    """Soft-enforce blocked tools from the router."""
    if not route:
        return True, ""
    blocked = set(route.get("blocked_tools") or [])
    if tool_name in blocked:
        return (
            False,
            f"Router blocked `{tool_name}` for this ask ({route.get('rationale')}). "
            f"Use: {', '.join(route.get('preferred_tools') or ['run_standard_analytics_pivot'])}.",
        )
    return True, ""
