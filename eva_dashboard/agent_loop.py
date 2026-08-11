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
from eva_dashboard.party_match import list_party_matches


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


def party_matches_look_like_branches(query: str, matches: list[str]) -> bool:
    """True when matches share the query tokens (Al Shaheer branches)."""
    tokens = [
        t
        for t in re.findall(r"[a-z0-9]+", (query or "").lower())
        if len(t) >= 3 and t not in {"the", "and", "for", "ltd", "pvt"}
    ]
    if not tokens or not matches:
        return False
    hit = 0
    for m in matches:
        ml = m.lower()
        if any(t in ml for t in tokens):
            hit += 1
    return hit >= max(1, int(0.75 * len(matches)))


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
    # Profile / lookup asks with divergent names → ask user to pick
    if operation in {"party_profile", "party_lookup"} and len(ms) >= 2:
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
    if result.get("mode") in {"party_pick", "clarify", "factor_costs"}:
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
        return out

    verification = verify_query_result(
        out,
        query_spec=out.get("query_spec"),
        user_text=user_text,
    )
    out["verification"] = verification

    clarify = verification.get("clarify")
    if clarify and clarify.get("markdown"):
        out["ok"] = True
        out["mode"] = "clarify"
        out["answer_markdown"] = clarify["markdown"]
        out["matches"] = clarify.get("matches") or []
        out["response_instructions"] = (
            "REQUIRED: Paste clarify markdown verbatim. Ask the user to pick "
            "an exact customer name. Do not invent volumes."
        )
        return out

    # Empty → ask model to replan (unless we already have a useful pick UI)
    if verification.get("retry_errors") and out.get("mode") != "party_pick":
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
