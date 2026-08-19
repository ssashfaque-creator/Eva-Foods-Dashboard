"""Adapter: ReAct agent → existing QuerySpec / execute_query_spec engines."""

from __future__ import annotations

import json
from typing import Any

from eva_dashboard.query_executor import execute_query_spec
from eva_dashboard.query_spec import PLAN_QUERY_TOOL

# QuerySpec fields the model may flatten onto the tool call instead of wrapping
# them in spec_dict. user_text is intentionally excluded — it is not a spec field.
QUERY_SPEC_TOOL_KEYS = frozenset(
    PLAN_QUERY_TOOL["function"]["parameters"]["properties"]
) | {"period_phrase", "mode"}


def _as_spec_dict(value: Any) -> dict[str, Any] | None:
    """Return a non-empty QuerySpec dict, including JSON-string wrappers."""
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict) or not value:
        return None
    return dict(value)


def query_spec_from_tool_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """Recover QuerySpec from a ReAct tool-call payload.

    gpt-4o usually wraps fields in ``spec_dict``, but also often emits the same
    QuerySpec keys at the top level (``row_dimensions``, ``filters``, …) and
    leaves ``spec_dict`` omitted or ``{}``. An empty wrapper must fall through
    to those flattened fields so the engine can run.
    """
    args = args if isinstance(args, dict) else {}
    wrapped = _as_spec_dict(args.get("spec_dict"))
    if wrapped:
        return wrapped
    wrapped = _as_spec_dict(args.get("query_spec"))
    if wrapped:
        return wrapped
    spec = {k: v for k, v in args.items() if k in QUERY_SPEC_TOOL_KEYS}
    action = str(spec.get("state_action") or "").strip().lower()
    if (
        action in {"keep", "modify"}
        and "clear_filters" not in spec
        and "clear" not in spec
    ):
        # Flattened follow-ups omit clear_filters; [] means keep prior filters.
        spec["clear_filters"] = []
    return spec


def run_standard_analytics_pivot(
    spec_dict: dict[str, Any] | None,
    *,
    user_text: str = "",
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run standard commercial analytics (Volume, AMS, Price Fetch, ranks).

    Uses the deterministic Python engines via ``execute_query_spec`` — do not
    reinvent AMS / MT / Price Fetch math in raw SQL.
    """
    raw = dict(spec_dict or {})
    if not raw:
        return {
            "ok": False,
            "error": "Empty QuerySpec",
            "markdown": "Legacy Engine Error: empty spec_dict",
        }
    try:
        result = execute_query_spec(
            raw,
            prior=prior,
            user_text=user_text or "",
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc),
            "markdown": f"Legacy Engine Error: {exc}",
        }

    if not isinstance(result, dict):
        return {
            "ok": False,
            "error": "Unexpected engine result",
            "markdown": "Legacy Engine Error: unexpected result type",
        }

    md = str(result.get("answer_markdown") or "").strip()
    if not md:
        err = result.get("error") or result.get("plan_errors") or "No table generated."
        md = f"Legacy Engine: {err}"
        return {
            "ok": bool(result.get("ok")),
            "error": str(err),
            "markdown": md,
            "result": result,
        }

    return {
        "ok": bool(result.get("ok", True)),
        "markdown": md,
        "answer_markdown": md,
        "query_spec": result.get("query_spec"),
        "table_spec": result.get("table_spec"),
        "party_spec": result.get("party_spec"),
        "price_spec": result.get("price_spec"),
        "query_state": result.get("query_state"),
        "mode": result.get("mode"),
        "result": result,
    }
