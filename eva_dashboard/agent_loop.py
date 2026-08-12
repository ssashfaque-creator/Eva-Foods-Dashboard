"""Light agentic loop helpers: verify results, clarify ambiguity, multi-hop hints.

Phase 4 — sits beside plan_query execution (not a second regex planner):
1. Verify empty / contradictory results → plan_errors for LLM retry
2. Clarify when a customer name is genuinely ambiguous (not branch ILIKE)
3. Detect multi-hop / mixed-grain compares → investigation instructions
"""

from __future__ import annotations

import re
from typing import Any

from eva_dashboard.client_language import (
    extract_all_client_types_from_text,
    match_client_type_alias,
)
from eva_dashboard.party_match import (
    list_party_matches,
    party_matches_look_like_branches,
)


def looks_multi_hop(user_text: str) -> bool:
    """True when the ask likely needs more than one plan_query."""
    t = (user_text or "").lower()
    if not t.strip():
        return False
    return bool(
        re.search(
            r"\b("
            r"and\s+then|then\s+show|also\s+show|followed\s+by|"
            r"after\s+that|break\s+that\s+down|dig\s+(in|into)|"
            r"why\s+(is|are|did|was)|what\s+drove|"
            r"compare\s+.+\s+(with|vs\.?|versus)\s+.+"
            r")\b",
            t,
        )
    )


def looks_mixed_party_channel_compare(user_text: str) -> bool:
    """Party name vs channel (e.g. al shaheer vs Imtiaz) — needs two plans."""
    t = (user_text or "").lower()
    if not re.search(r"\b(compare|versus|vs\.?|against)\b", t):
        return False
    channels = extract_all_client_types_from_text(user_text)
    if not channels:
        return False
    # Strip channel words; leftover proper-name-ish tokens suggest a party side
    scrubbed = t
    for ch in channels:
        scrubbed = scrubbed.replace(str(ch).lower(), " ")
    for alias in ("imtiaz", "metro", "distributors", "lmt", "chase", "csd", "online"):
        scrubbed = re.sub(rf"\b{alias}\b", " ", scrubbed)
    scrubbed = re.sub(
        r"\b(compare|versus|vs|against|sales?|growth|ams|in|for|with|and|"
        r"lahore|karachi|islamabad|last|this|month|months)\b",
        " ",
        scrubbed,
    )
    leftover = [w for w in re.findall(r"[a-z][a-z0-9']{2,}", scrubbed)]
    return bool(leftover)


def should_clarify_party(
    *,
    query: str | None,
    matches: list[str] | None,
    operation: str | None = None,
    empty_result: bool = False,
) -> bool:
    """Clarify only when ambiguity is real — not for branch-style ILIKE families."""
    q = (query or "").strip()
    ms = [str(m).strip() for m in (matches or []) if str(m).strip()]
    if not q or len(ms) < 2:
        return False
    if party_matches_look_like_branches(q, ms):
        return False
    # Profile asks with divergent names → ask user to pick.
    # party_lookup already returns a scored identity table — do not replace it.
    if operation == "party_profile" and len(ms) >= 2:
        return True
    # Empty analytics result with many divergent matches → clarify
    if empty_result and len(ms) >= 3:
        return True
    return False


def clarify_party_markdown(query: str, matches: list[str]) -> str:
    lines = [
        f"Multiple customers match **{query}** — reply with the exact name:\n",
        "| # | Party |",
        "| --- | --- |",
    ]
    for i, name in enumerate(matches[:10], 1):
        lines.append(f"| {i} | {name} |")
    lines.append("")
    lines.append(
        "_Tip: once you pick a name, follow-ups like price / % of AMS / "
        "last purchase will stick to that customer._"
    )
    return "\n".join(lines) + "\n"


def _result_is_empty(result: dict[str, Any]) -> bool:
    if not result or result.get("ok") is False:
        return False  # errors handled elsewhere
    if result.get("mode") in {
        "party_pick",
        "clarify",
        "factor_costs",
        "party_lookup",
    }:
        return False
    # Identity search already answered
    matches = result.get("matches")
    if isinstance(matches, list) and matches:
        return False
    # Scalar / list payloads that mean we already have an answer
    if result.get("volume_mt") not in (None, 0, 0.0):
        return False
    if result.get("avg_rate") is not None or result.get("price_fetch") is not None:
        return False
    if result.get("by_month") or result.get("top_skus") or result.get("months"):
        return False
    if int(result.get("lines") or 0) > 0 or int(result.get("count") or 0) > 0:
        return False
    parties = result.get("parties") or result.get("clients") or result.get("entities")
    if isinstance(parties, list) and parties:
        return False
    rows = result.get("rows")
    if isinstance(rows, list) and rows:
        return False
    matrix = result.get("matrix") or {}
    mrows = list(matrix.get("rows") or [])
    data_rows = [
        r
        for r in mrows
        if not r.get("is_total")
        and str(r.get("business_unit") or r.get("label") or "").lower()
        not in {"total", "grand total"}
    ]
    if data_rows:
        # All-zero matrix still counts as empty for retry purposes
        numeric = False
        nonzero = False
        for r in data_rows:
            for k, v in r.items():
                if k in {"business_unit", "label", "is_total", "row_dimension"}:
                    continue
                if isinstance(v, (int, float)):
                    numeric = True
                    if v != 0:
                        nonzero = True
        if numeric and not nonzero:
            return True
        if numeric:
            return False
        return False
    md = (result.get("answer_markdown") or "").lower()
    if "no party matched" in md or "no pivot data" in md or "no sales dates" in md:
        return True

    def _markdown_has_nonzero() -> bool:
        # Digits that are not pure year tokens / zero
        for m in re.finditer(r"(?<![a-z])(\d+(?:\.\d+)?)(?![a-z])", md):
            try:
                val = float(m.group(1))
            except ValueError:
                continue
            if val == 0:
                continue
            # skip year-like 2024-2027 alone
            if 2000 <= val <= 2100 and "." not in m.group(1):
                continue
            return True
        return False

    # Rendered tables count only when they contain a non-zero figure
    if "eva-mtx" in md or "<table" in md or "| month |" in md:
        return not _markdown_has_nonzero()
    if "avg rate" in md or "price fetch" in md:
        return not _markdown_has_nonzero()
    if not mrows and not parties and result.get("volume_mt") in (None, 0, 0.0):
        return True
    return False


def verify_query_result(
    result: dict[str, Any],
    *,
    query_spec: dict[str, Any] | None = None,
    user_text: str = "",
) -> dict[str, Any]:
    """Post-execute checks → warnings, retry plan_errors, or clarify payload."""
    spec = dict(query_spec or result.get("query_spec") or {})
    filters = dict(spec.get("filters") or {})
    operation = str(spec.get("operation") or result.get("mode") or "pivot")
    warnings: list[str] = []
    retry_errors: list[str] = []
    clarify: dict[str, Any] | None = None
    investigation: dict[str, Any] | None = None

    empty = _result_is_empty(result)
    party_q = (
        filters.get("party")
        or spec.get("party_query")
        or (
            (filters.get("party_ilike") or [None])[0]
            if filters.get("party_ilike")
            else None
        )
    )
    matches = list(
        filters.get("_party_matches")
        or result.get("matches")
        or []
    )
    if isinstance(matches, list) and matches and isinstance(matches[0], dict):
        matches = [
            str(m.get("client") or m.get("party") or m.get("name") or "")
            for m in matches
        ]
    matches = [m for m in matches if m]
    if party_q and not matches:
        matches = list_party_matches(str(party_q), limit=8)

    if should_clarify_party(
        query=str(party_q) if party_q else None,
        matches=matches,
        operation=operation,
        empty_result=empty,
    ):
        clarify = {
            "kind": "party",
            "query": party_q,
            "matches": matches[:10],
            "markdown": clarify_party_markdown(str(party_q), matches),
        }

    # Empty + unresolved customer fuzzy match → ask model to replan.
    # Do NOT retry valid zero tables for resolved channels/cities (0 MT is an answer).
    unresolved_party = bool(
        (filters.get("party_ilike") and not filters.get("party"))
        or (
            party_q
            and not filters.get("party")
            and not filters.get("client_type")
            and operation not in {"overview", "party_list", "party_lookup"}
        )
    )
    md_l = (result.get("answer_markdown") or "").lower()
    hard_empty = (
        "no pivot data" in md_l
        or "no party matched" in md_l
        or "no sales dates" in md_l
    )
    if (
        empty
        and not clarify
        and (unresolved_party or hard_empty)
        and operation not in {"overview", "party_list", "party_lookup"}
    ):
        period = spec.get("period") or {}
        bits = []
        if filters.get("city"):
            bits.append(f"city={filters['city']}")
        if filters.get("client_type"):
            bits.append(f"client_type={filters['client_type']}")
        if party_q:
            bits.append(f"party={party_q}")
        if filters.get("business_unit") or spec.get("business_units"):
            bits.append("business_unit filter set")
        label = period.get("label") or period.get("phrase") or spec.get("period_type")
        retry_errors.append(
            "Query returned no rows for "
            + (", ".join(bits) if bits else "the current filters")
            + (f" in period {label}." if label else ".")
            + " Retry plan_query: widen period (LAST_N_MONTHS/6), clear a filter "
            "via clear_filters, or fix party/city spelling via extracted_entities."
        )
    elif empty and not clarify and party_q:
        warnings.append(
            f"No volume for party scope `{party_q}` in this period — "
            "stated as zero / empty, not a planner failure."
        )

    # Mixed party-vs-channel compare → tell model to run two plans
    if looks_mixed_party_channel_compare(user_text):
        investigation = {
            "needed": True,
            "kind": "mixed_compare",
            "hint": (
                "Mixed party-vs-channel compare detected. Call plan_query TWICE: "
                "(1) filters.party / extracted_entities for the named customer; "
                "(2) filters.client_type for the channel — same metrics/period — "
                "then compare both tables in ### Analysis."
            ),
        }
    elif looks_multi_hop(user_text) and result.get("ok"):
        investigation = {
            "needed": True,
            "kind": "multi_hop",
            "hint": (
                "Multi-step ask detected. After this table, call plan_query again "
                "with context_handling='prior' for the next cut "
                "(product-wise / SKU / price / % vs AMS) before writing Analysis."
            ),
        }

    # Soft warnings (never fail the turn)
    if result.get("ok") and filters.get("party_ilike") and not filters.get("party"):
        warnings.append(
            "Customer matched via ILIKE (may include multiple branches). "
            "State the matched scope in Analysis."
        )

    return {
        "ok": not retry_errors and not clarify,
        "empty": empty,
        "warnings": warnings,
        "retry_errors": retry_errors,
        "clarify": clarify,
        "investigation": investigation,
    }


def _suggest_fixes_from_errors(
    errors: list[str],
    *,
    query_spec: dict[str, Any] | None = None,
) -> list[str]:
    """Heuristic suggested_fixes for the self-correction loop (invisible to user)."""
    fixes: list[str] = []
    blob = " ".join(errors).lower()
    spec = dict(query_spec or {})
    filters = dict(spec.get("filters") or {})
    if "client_type" in blob and "party" in blob:
        fixes.append(
            "Move product/brand names out of client_type; use business_units "
            "for Eva/Maan and filters.party only for customer names."
        )
    if "not a valid client_type" in blob or "unknown client" in blob:
        fixes.append(
            "Use a governed channel from GROUNDED_GLOSSARY / vocabulary "
            "(e.g. Imtiaz Store, Eva Distributors). Brands are business_units."
        )
    if "clear_filters" in blob or "context_handling" in blob:
        fixes.append(
            "Set state_action='modify' (or context_handling='prior') and emit "
            "clear_filters explicitly (use [] if keeping every prior filter)."
        )
    if "still said" in blob and "city" in blob:
        fixes.append(
            "User still named the city — remove 'city' from clear_filters and "
            "set filters.city to that city. Prefer state_action='clear' for a "
            "complete ask that restates city + brands."
        )
    if "empty" in blob or "no rows" in blob or "no pivot" in blob:
        fixes.append(
            "Widen period to LAST_N_MONTHS/6, clear a sticky city/party via "
            "clear_filters, or fix spelling via extracted_entities."
        )
    if filters.get("party") and "client_type" in blob:
        fixes.append(
            "If the name is a channel alias (metro, imtiaz), use "
            "filters.client_type — not filters.party."
        )
    if not fixes:
        fixes.append(
            "Revise QuerySpec: check row_dimensions, metrics, period_type, "
            "state_action, and filter polarity (INCLUDE vs EXCLUDE)."
        )
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for f in fixes:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def build_correction_feedback(
    result: dict[str, Any] | None,
    *,
    attempt: int = 1,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Structured feedback for the LLM self-correction loop.

    Never shown to the end user — attached to the tool JSON payload so the
    planner can revise QuerySpec up to ``max_attempts`` times.
    """
    if not isinstance(result, dict):
        return {
            "kind": "execution_error",
            "attempt": attempt,
            "max_attempts": max_attempts,
            "errors": ["Non-dict tool result"],
            "suggested_fixes": ["Call plan_query again with a complete QuerySpec."],
            "show_to_user": False,
        }
    spec = dict(result.get("query_spec") or {})
    errors: list[str] = []
    kind = "ok"

    if result.get("ok") is False:
        plan_errs = [str(e) for e in (result.get("plan_errors") or []) if e]
        if plan_errs:
            kind = "validation_error"
            errors.extend(plan_errs)
        err = result.get("error")
        if err:
            kind = "execution_error" if kind == "ok" else kind
            errors.append(str(err))
        if not errors:
            kind = "execution_error"
            errors.append("Plan execution failed with no detail.")
    else:
        verification = result.get("verification") or {}
        retry = [str(e) for e in (verification.get("retry_errors") or []) if e]
        if retry:
            kind = "empty_result"
            errors.extend(retry)
        elif _result_is_empty(result) and result.get("mode") not in {
            "party_pick",
            "clarify",
            "party_lookup",
            "factor_costs",
        }:
            # Soft empty — still ok for user, but flag for optional replan
            kind = "ok"
            return {
                "kind": "ok",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "errors": [],
                "suggested_fixes": [],
                "empty_summary": "Result table is empty or all-zero.",
                "show_to_user": False,
            }

    if kind == "ok":
        return {
            "kind": "ok",
            "attempt": attempt,
            "max_attempts": max_attempts,
            "errors": [],
            "suggested_fixes": [],
            "show_to_user": False,
        }

    compact_spec = {
        k: spec[k]
        for k in (
            "state_action",
            "context_handling",
            "operation",
            "row_dimensions",
            "column_dimensions",
            "metrics",
            "period_type",
            "filters",
            "clear_filters",
            "clear",
            "excludes",
            "business_units",
            "extracted_entities",
            "metric_filters",
        )
        if k in spec and spec[k] not in (None, "", [], {})
    }
    return {
        "kind": kind,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "errors": errors,
        "suggested_fixes": _suggest_fixes_from_errors(errors, query_spec=spec),
        "failed_query_spec": compact_spec,
        "show_to_user": False,
        "response_instructions": (
            f"SYSTEM ERROR (attempt {attempt}/{max_attempts}) — do NOT show "
            "this to the user. Call plan_query again with a corrected "
            f"QuerySpec. Errors: {'; '.join(errors[:3])}"
        ),
    }


def apply_verification(
    result: dict[str, Any],
    *,
    user_text: str = "",
) -> dict[str, Any]:
    """Mutate/annotate an execute_query_spec result with verification outcomes."""
    if not isinstance(result, dict):
        return result
    out = dict(result)
    if out.get("ok") is False and out.get("plan_errors"):
        out["feedback"] = build_correction_feedback(out)
        return out

    verification = verify_query_result(
        out,
        query_spec=out.get("query_spec"),
        user_text=user_text,
    )
    out["verification"] = verification

    clarify = verification.get("clarify")
    # Never overwrite a finished who-is / identity table with a bare pick list
    if (
        clarify
        and clarify.get("markdown")
        and str(out.get("mode") or "") != "party_lookup"
        and not (isinstance(out.get("matches"), list) and out.get("matches")
                 and "Client search" in (out.get("answer_markdown") or ""))
    ):
        out["ok"] = True
        out["mode"] = "clarify"
        out["answer_markdown"] = clarify["markdown"]
        out["matches"] = clarify.get("matches") or []
        out["response_instructions"] = (
            "REQUIRED: Paste clarify markdown verbatim. Ask the user to pick "
            "an exact customer name. Do not invent volumes."
        )
        out["feedback"] = build_correction_feedback(out)
        return out

    # Empty → ask model to replan (unless we already have a useful pick UI)
    if (
        verification.get("retry_errors")
        and out.get("mode") not in {"party_pick", "party_lookup"}
    ):
        # Keep ok=True with empty table if answer_markdown already explains;
        # only force retry when there is essentially nothing to show.
        md = (out.get("answer_markdown") or "").strip()
        if not md or _result_is_empty(out):
            out["ok"] = False
            out["error"] = "Empty result — revise the QuerySpec."
            out["plan_errors"] = list(verification["retry_errors"])
            out["response_instructions"] = (
                "REQUIRED: Call plan_query again with a corrected QuerySpec. "
                + verification["retry_errors"][0]
            )
            out["feedback"] = build_correction_feedback(out)
            return out

    inv = verification.get("investigation")
    if inv and inv.get("needed") and out.get("ok"):
        hint = inv.get("hint") or ""
        existing = out.get("response_instructions") or ""
        out["response_instructions"] = (
            f"{existing}\n\nINVESTIGATION: {hint}".strip()
            if existing
            else f"INVESTIGATION: {hint}"
        )
        warnings = verification.get("warnings") or []
        if warnings:
            out["verification_warnings"] = warnings

    out["feedback"] = build_correction_feedback(out)
    return out


def build_mixed_compare_subplans(
    *,
    user_text: str,
    base_spec: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build two QuerySpecs for party-vs-channel compares (optional auto path)."""
    channels = extract_all_client_types_from_text(user_text)
    if not channels:
        return []
    base = dict(base_spec or {})
    metrics = list(base.get("metrics") or ["volume", "ams"])
    period_type = base.get("period_type") or "LAST_N_MONTHS"
    months_back = base.get("months_back") or 6
    target_month = base.get("target_month")
    filters = dict(base.get("filters") or {})
    # Drop channel lock from shared filters; each subplan sets its own
    shared = {
        k: v
        for k, v in filters.items()
        if k not in {"client_type", "client_types", "party", "parties", "party_ilike"}
        and v not in (None, "", [])
    }

    # Party side: remove channel words from text for entity extraction hint
    t = user_text or ""
    party_hint = t
    for ch in channels:
        party_hint = re.sub(re.escape(ch), " ", party_hint, flags=re.I)
    for alias in ("imtiaz", "metro", "distributors", "distributor", "lmt", "chase up"):
        party_hint = re.sub(rf"\b{alias}\b", " ", party_hint, flags=re.I)
    party_hint = re.sub(
        r"\b(compare|versus|vs\.?|against|sales?|growth|with|and)\b",
        " ",
        party_hint,
        flags=re.I,
    )
    party_bits = [
        w
        for w in re.findall(r"[A-Za-z][A-Za-z0-9'&./-]{2,}", party_hint)
        if w.lower()
        not in {
            "lahore",
            "karachi",
            "islamabad",
            "july",
            "june",
            "march",
            "april",
            "last",
            "this",
            "month",
            "months",
            "ams",
        }
    ]
    party_query = " ".join(party_bits[:4]).strip()

    plans: list[dict[str, Any]] = []
    if party_query:
        plans.append(
            {
                "operation": "pivot",
                "row_dimensions": ["party"],
                "column_dimensions": ["month"] if period_type == "LAST_N_MONTHS" else [],
                "metrics": metrics,
                "period_type": period_type,
                "months_back": months_back,
                "target_month": target_month,
                "context_handling": "none",
                "filters": dict(shared),
                "extracted_entities": [party_query],
                "rationale": f"party side of mixed compare ({party_query})",
            }
        )
    for ch in channels[:2]:
        canon = match_client_type_alias(ch) or ch
        plans.append(
            {
                "operation": "pivot",
                "row_dimensions": ["client_type"],
                "column_dimensions": ["month"] if period_type == "LAST_N_MONTHS" else [],
                "metrics": metrics,
                "period_type": period_type,
                "months_back": months_back,
                "target_month": target_month,
                "context_handling": "none",
                "filters": {**shared, "client_type": canon},
                "rationale": f"channel side of mixed compare ({canon})",
            }
        )
    return plans


# ---------------------------------------------------------------------------
# Phase 5 — Multi-step ReAct agent (OpenAI native tool calling)
# ---------------------------------------------------------------------------

import json
import os
from typing import Callable

REACT_SYSTEM_PROMPT = """You are Eva Foods AI Sales Analyst (v2 ReAct).
Answer commercial questions accurately using tools. Never invent MT, rates, or AMS.

A ROUTING block may be injected below — obey preferred/blocked tools.

TOOL CHOICE:
1. run_standard_analytics_pivot — standard volume matrices, AMS, vs AMS, AMS growth,
   brand/BU trends, party ranks, Price Fetch / avg rate when the ask matches
   normal commercial tables. Prefer this for volume/AMS pivots.
2. execute_read_only_sql — novel asks: min/max price, who bought at a rate,
   same-date price dispersion, custom aggregations, discovery SQL.
   NEVER invent AMS windows or Price Fetch (37.3246 / 0.915) in SQL.
3. calculate_expression — ANY arithmetic (× 24.7 / 6, deltas, conversions).
   NEVER compute numbers in your head — call this tool.
4. get_database_schema — before writing unfamiliar SQL, inspect DDL.
5. lookup_entity_values — resolve exact party/product/client_type strings first.

WORKFLOW:
- You may call multiple tools across turns (multi-hop).
- For "lowest price then who bought it": SQL for MIN(rate) → SQL/filter parties.
- For "Pepsi price × 24.7 / 6": lookup entity → SQL rate → calculate_expression.
- Prefer sales.mt_qty for volume; join category on product; clients on party name.
- Final reply: Markdown table(s) with the numbers, then ### Analysis (2–4 bullets).
- If a tool errors, fix the query and retry — do not invent data.
- If the ask is ambiguous, ask ONE clarifying question instead of guessing.
"""

REACT_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "execute_read_only_sql",
            "description": (
                "Execute a read-only SELECT/WITH SQL query on eva.db for "
                "custom/novel questions (min/max, dispersion, ad-hoc)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql_query": {
                        "type": "string",
                        "description": "A single SELECT or WITH query.",
                    }
                },
                "required": ["sql_query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_expression",
            "description": "Evaluate a mathematical expression with Python arithmetic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "e.g. '(1500 * 24.7) / 6'",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_database_schema",
            "description": "Get DDL table schemas to construct correct SQL.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_entity_values",
            "description": "Search distinct values (party, product, client type, …).",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "sales|clients|category|factor_costs|…",
                    },
                    "column_name": {
                        "type": "string",
                        "description": "e.g. party, client, product, type",
                    },
                    "search_term": {"type": "string"},
                },
                "required": ["table_name", "column_name", "search_term"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_standard_analytics_pivot",
            "description": (
                "Run standard Eva commercial analytics (Volume, AMS, party ranks, "
                "Price Fetch) via deterministic Python engines."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spec_dict": {
                        "type": "object",
                        "description": (
                            "QuerySpec: row_dimensions, column_dimensions, metrics, "
                            "period_type, months_back, target_month, filters, "
                            "business_units, limit, sort, operation, …"
                        ),
                    },
                    "user_text": {
                        "type": "string",
                        "description": "Original user question (helps spoken filters).",
                    },
                },
                "required": ["spec_dict"],
            },
        },
    },
]


def _tool_payload_text(payload: dict[str, Any] | str) -> str:
    if isinstance(payload, str):
        return payload
    if payload.get("markdown"):
        return str(payload["markdown"])
    if payload.get("answer_markdown"):
        return str(payload["answer_markdown"])
    return json.dumps(payload, default=str)[:80_000]


def dispatch_react_tool(
    name: str,
    args: dict[str, Any],
    *,
    user_text: str = "",
    prior: dict[str, Any] | None = None,
    route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one ReAct tool call; returns a dict with markdown + ok."""
    from eva_dashboard.tools.calculator_tool import calculate_expression
    from eva_dashboard.tools.discovery_tool import (
        get_database_schema,
        lookup_entity_values,
    )
    from eva_dashboard.tools.intent_router import tool_allowed
    from eva_dashboard.tools.legacy_tool import run_standard_analytics_pivot
    from eva_dashboard.tools.sql_tool import execute_read_only_sql

    allowed, reason = tool_allowed(name, route)
    if not allowed:
        return {"ok": False, "error": reason, "markdown": f"Error: {reason}"}

    if name == "execute_read_only_sql":
        return execute_read_only_sql(str(args.get("sql_query") or ""))
    if name == "calculate_expression":
        return calculate_expression(str(args.get("expression") or ""))
    if name == "get_database_schema":
        return get_database_schema()
    if name == "lookup_entity_values":
        return lookup_entity_values(
            str(args.get("table_name") or ""),
            str(args.get("column_name") or ""),
            str(args.get("search_term") or ""),
        )
    if name == "run_standard_analytics_pivot":
        spec = args.get("spec_dict") or args.get("query_spec") or {}
        if not isinstance(spec, dict):
            return {
                "ok": False,
                "error": "spec_dict must be an object",
                "markdown": "Legacy Engine Error: spec_dict must be an object",
            }
        return run_standard_analytics_pivot(
            spec,
            user_text=str(args.get("user_text") or user_text or ""),
            prior=prior,
        )
    return {
        "ok": False,
        "error": f"Unknown tool {name}",
        "markdown": f"Error: Unknown tool {name}",
    }


def run_agent_loop(
    user_query: str,
    *,
    client: Any,
    model: str = "gpt-4o-mini",
    history: list[dict[str, Any]] | None = None,
    memory_block: str = "",
    prior: dict[str, Any] | None = None,
    max_turns: int = 8,
    on_status: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Multi-step ReAct execution loop with OpenAI native tool calling.

    Returns ``{ok, answer, messages, last_legacy_result, tool_trace, route}``.
    """
    from eva_dashboard.tools.answer_verifier import verify_agent_answer
    from eva_dashboard.tools.intent_router import route_ask
    from eva_dashboard.ask_grounding import ground_ask_for_agent
    from eva_dashboard.playbooks import playbook_prompt_block

    from eva_dashboard.personal_lexicon import (
        learn_from_turn,
        remember_clarify,
        should_skip_clarify,
    )

    route = route_ask(user_query, prior=prior)
    grounded = ground_ask_for_agent(user_query, prior=prior)
    playbook = playbook_prompt_block(user_query)

    def _finish(payload: dict[str, Any]) -> dict[str, Any]:
        """Persist lexicon/style learning after a usable turn."""
        try:
            learn_from_turn(
                user_query,
                route=route,
                grounding=grounded,
                tool_trace=list(payload.get("tool_trace") or []),
                answer=str(payload.get("answer") or ""),
                verify_ok=bool(payload.get("ok")),
            )
        except Exception:  # noqa: BLE001 — learning must never break chat
            pass
        return payload

    # High-confidence clarify: skip tools, return the one question
    if (
        route.get("kind") == "clarify"
        and float(route.get("confidence") or 0) >= 0.5
        and route.get("clarify_question")
        # Don't clarify if we already grounded a unique party + price qualifier path
        and not (
            grounded.get("party_hits")
            and re.search(r"\b(avg|average|last|lowest|fetch|rate)\b", user_query, flags=re.I)
        )
    ):
        q = str(route["clarify_question"])
        if should_skip_clarify(user_query, q):
            # Already asked — pick a default and continue with tools
            route = {
                **route,
                "kind": "mixed",
                "confidence": 0.55,
                "preferred_tools": [
                    "lookup_entity_values",
                    "run_standard_analytics_pivot",
                    "execute_read_only_sql",
                ],
                "blocked_tools": [],
                "clarify_question": None,
                "rationale": "clarify_skipped_recent — use a sensible default",
                "prompt_block": (
                    "=== ROUTING (authoritative) ===\n"
                    "kind=mixed confidence=0.55 (clarify skipped — already asked).\n"
                    "Do NOT ask another clarifying question. Default to last sold "
                    "price if the price type is still ambiguous; state that "
                    "assumption in one line, then fetch with tools."
                ),
            }
        else:
            remember_clarify(q, user_query)
            return _finish(
                {
                    "ok": True,
                    "answer": q,
                    "messages": [
                        {"role": "system", "content": REACT_SYSTEM_PROMPT},
                        {"role": "user", "content": user_query},
                        {"role": "assistant", "content": q},
                    ],
                    "last_legacy_result": None,
                    "tool_trace": [],
                    "route": route,
                    "grounding": grounded,
                }
            )

    system = REACT_SYSTEM_PROMPT
    if route.get("prompt_block"):
        system = system + "\n\n" + str(route["prompt_block"])
    if playbook:
        system = system + "\n\n" + playbook
    if grounded.get("prompt_block"):
        system = system + "\n\n" + str(grounded["prompt_block"])
    if memory_block.strip():
        system = system + "\n\n" + memory_block.strip()

    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    # Prior user/assistant turns (no system / tool clutter)
    for m in history or []:
        role = str(m.get("role") or "")
        if role in {"user", "assistant"} and m.get("content"):
            # Skip empty assistant stubs that only had tool_calls
            if role == "assistant" and m.get("tool_calls") and not str(
                m.get("content") or ""
            ).strip():
                continue
            messages.append({"role": role, "content": str(m["content"])})
    messages.append({"role": "user", "content": user_query})

    tool_trace: list[dict[str, Any]] = []
    last_legacy: dict[str, Any] | None = None
    verify_retries = 0
    max_verify_retries = 2

    for turn in range(max(1, int(max_turns))):
        if on_status:
            on_status("Thinking…" if turn == 0 else "Using tools…")
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=REACT_TOOLS_SCHEMA,
                tool_choice="auto",
                temperature=0.1,
            )
        except Exception as exc:  # noqa: BLE001
            return _finish(
                {
                    "ok": False,
                    "error": str(exc),
                    "answer": f"Agent error talking to the model: {exc}",
                    "messages": messages,
                    "last_legacy_result": last_legacy,
                    "tool_trace": tool_trace,
                    "route": route,
                    "grounding": grounded,
                }
            )

        msg = response.choices[0].message
        tool_calls = list(msg.tool_calls or [])
        assistant_entry: dict[str, Any] = {
            "role": "assistant",
            "content": msg.content or "",
        }
        if tool_calls:
            assistant_entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in tool_calls
            ]
        messages.append(assistant_entry)

        if not tool_calls:
            answer = (msg.content or "").strip()
            check = verify_agent_answer(
                user_query,
                answer,
                tool_trace=tool_trace,
                route=route,
            )
            if check.get("ok") or verify_retries >= max_verify_retries:
                return _finish(
                    {
                        "ok": bool(check.get("ok")),
                        "answer": answer
                        or "I could not produce an answer. Please rephrase the question.",
                        "messages": messages,
                        "last_legacy_result": last_legacy,
                        "tool_trace": tool_trace,
                        "route": route,
                        "verify": check,
                        "grounding": grounded,
                    }
                )
            # Retry with verifier feedback
            verify_retries += 1
            if on_status:
                on_status("Checking answer…")
            messages.append(
                {
                    "role": "user",
                    "content": str(check.get("retry_hint") or "Please fix with tools."),
                }
            )
            continue

        for tc in tool_calls:
            fn_name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            if on_status:
                on_status(f"Running {fn_name}…")
            payload = dispatch_react_tool(
                fn_name,
                args,
                user_text=user_query,
                prior=prior,
                route=route,
            )
            tool_trace.append(
                {
                    "tool": fn_name,
                    "args": args,
                    "ok": bool(payload.get("ok")),
                    "preview": str(payload.get("markdown") or "")[:500],
                }
            )
            if fn_name == "run_standard_analytics_pivot" and isinstance(
                payload.get("result"), dict
            ):
                last_legacy = payload["result"]
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _tool_payload_text(payload)[:100_000],
                }
            )

    return _finish(
        {
            "ok": False,
            "error": "max_turns",
            "answer": (
                "Agent reached maximum execution steps without concluding. "
                "Please refine your query."
            ),
            "messages": messages,
            "last_legacy_result": last_legacy,
            "tool_trace": tool_trace,
            "route": route,
            "grounding": grounded,
        }
    )


def react_agent_enabled() -> bool:
    """Feature flag — default ON for v1.4 ReAct path."""
    raw = os.environ.get("EVA_REACT_AGENT", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}
