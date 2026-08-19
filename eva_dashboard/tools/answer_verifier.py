"""Post-tool answer verifier — catch empty / off-topic / invented AMS answers."""

from __future__ import annotations

import re
from typing import Any


def verify_agent_answer(
    user_text: str,
    answer: str,
    *,
    tool_trace: list[dict[str, Any]] | None = None,
    route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return ``{ok, issues, retry_hint}`` for the ReAct final reply."""
    text = (answer or "").strip()
    q = (user_text or "").lower()
    issues: list[str] = []
    trace = list(tool_trace or [])
    route = route or {}
    kind = str(route.get("kind") or "")

    if not text:
        issues.append("empty answer")
    if re.search(
        r"\b(i don'?t know|as an ai|knowledge cutoff|cannot access)\b",
        text,
        flags=re.I,
    ):
        issues.append("model refused or claimed no access")

    # Clarifications are OK without tools
    if kind == "clarify":
        if "?" in text and len(text) < 400:
            return {"ok": True, "issues": [], "retry_hint": ""}
        issues.append("clarify route should ask one short question")

    tool_ok = [t for t in trace if t.get("ok")]
    tool_fail = [t for t in trace if t.get("ok") is False]
    used_names = {str(t.get("tool") or "") for t in trace}

    # Math asks must use calculator
    if kind in {"math", "mixed"} and re.search(
        r"\b(multiply|divide|times|\*|×|/)\b", q
    ):
        if "calculate_expression" not in used_names and re.search(
            r"\d", text
        ):
            # Allow if they only clarified
            if "?" not in text[:120]:
                issues.append("arithmetic ask without calculate_expression tool")

    # Standard AMS/volume should prefer legacy
    if kind == "standard" and re.search(
        r"\b(ams|volume|price\s*fetch|sales?|tell\s+me\s+about)\b", q
    ):
        if (
            "run_standard_analytics_pivot" not in used_names
            and "execute_read_only_sql" in used_names
        ):
            issues.append(
                "AMS/volume/Price Fetch should use run_standard_analytics_pivot, not raw SQL"
            )

    # Discovery should have attempted SQL or legacy with data
    if kind == "discovery" and not tool_ok and not (
        "?" in text and len(text) < 400
    ):
        issues.append("discovery ask produced no successful tool result")

    # Empty-result smell
    if re.search(
        r"result set is empty|no rows|_no rows_|no matching|no results|"
        r"legacy engine error|"
        r"sql execution error|security / validation",
        text,
        flags=re.I,
    ) and not re.search(r"\b(0(\.0+)?\s*mt|zero)\b", text, flags=re.I):
        # Empty can be a valid answer; only retry if user expected data keywords
        if re.search(r"\b(show|list|who|lowest|highest|top|pepsi|sales)\b", q):
            if all(
                (not t.get("ok"))
                or "EMPTY" in str(t.get("preview") or "").upper()
                or "no row" in str(t.get("preview") or "").lower()
                or "no results" in str(t.get("preview") or "").lower()
                for t in trace
            ) or not tool_ok:
                issues.append("tools returned empty/error and answer has no data table")

    # Hallucinated AMS definition smell when SQL path invented AMS
    if "execute_read_only_sql" in used_names and re.search(
        r"\bams\b", q
    ) and "run_standard_analytics_pivot" not in used_names:
        if re.search(r"\bams\b", text, flags=re.I):
            issues.append(
                "AMS mentioned from SQL path — rerun via run_standard_analytics_pivot"
            )

    if tool_fail and not tool_ok and kind != "clarify":
        issues.append("all tool calls failed")

    # Numbers asked but answer has no digits and no table
    wants_numbers = bool(
        re.search(
            r"\b(how much|volume|rate|price|ams|mt|top|lowest|highest|sales)\b",
            q,
        )
    )
    has_numbers = bool(re.search(r"\d", text))
    has_table = bool(
        "|" in text or "<table" in text.lower() or "result:" in text.lower()
    )
    if wants_numbers and not has_numbers and not has_table and "?" not in text:
        issues.append("numeric ask but answer has no numbers or table")

    ok = not issues
    retry_hint = ""
    if not ok:
        bits = [
            "Your previous answer failed verification:",
            *[f"- {i}" for i in issues],
            "Fix with tools (do not invent numbers).",
        ]
        if kind == "standard" or "AMS" in " ".join(issues):
            bits.append(
                "Call run_standard_analytics_pivot with a proper QuerySpec."
            )
        if kind in {"discovery", "mixed", "math"}:
            bits.append(
                "Use lookup_entity_values / execute_read_only_sql / calculate_expression as needed."
            )
        if kind == "clarify":
            bits.append(
                f"Ask only: {route.get('clarify_question') or 'Please clarify the metric.'}"
            )
        retry_hint = "\n".join(bits)

    return {"ok": ok, "issues": issues, "retry_hint": retry_hint}
