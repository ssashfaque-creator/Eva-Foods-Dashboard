"""Adapter: ReAct agent → existing QuerySpec / execute_query_spec engines."""

from __future__ import annotations

from typing import Any

from eva_dashboard.query_executor import execute_query_spec


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
