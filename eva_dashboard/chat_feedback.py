"""Chat feedback / eval_failures — human-in-the-loop bad-answer capture."""

from __future__ import annotations

import json
from typing import Any

from eva_dashboard.db import connect, init_db, now_iso


def record_chat_feedback(
    *,
    rating: str,
    user_text: str = "",
    answer: str = "",
    route: dict[str, Any] | None = None,
    tool_trace: list[dict[str, Any]] | None = None,
    verify: dict[str, Any] | None = None,
    model: str = "",
    source: str = "streamlit",
    case_id: str = "",
) -> int:
    """Persist 👍/👎 feedback. Returns row id.

    Ratings: ``up`` / ``down``. Down votes are the weekly golden-eval intake.
    """
    init_db()
    rating_n = str(rating or "").strip().lower()
    if rating_n in {"👍", "thumbsup", "good", "+1", "1"}:
        rating_n = "up"
    elif rating_n in {"👎", "thumbsdown", "bad", "-1", "0"}:
        rating_n = "down"
    if rating_n not in {"up", "down"}:
        raise ValueError("rating must be up or down")

    route = route or {}
    issues = list((verify or {}).get("issues") or [])
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO eval_failures (
                created_at, case_id, user_text, answer, rating,
                route_kind, route_json, tool_trace_json, issues_json,
                model, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),
                str(case_id or "")[:120],
                str(user_text or "")[:4000],
                str(answer or "")[:20000],
                rating_n,
                str(route.get("kind") or ""),
                json.dumps(route, default=str)[:20000],
                json.dumps(list(tool_trace or []), default=str)[:50000],
                json.dumps(issues, default=str)[:8000],
                str(model or "")[:80],
                str(source or "streamlit")[:40],
            ),
        )
        return int(cur.lastrowid or 0)


def list_eval_failures(
    *,
    rating: str | None = "down",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Recent feedback rows for weekly golden promotion."""
    init_db()
    limit = max(1, min(int(limit), 500))
    sql = (
        "SELECT id, created_at, case_id, user_text, answer, rating, "
        "route_kind, model, source FROM eval_failures"
    )
    params: list[Any] = []
    if rating:
        sql += " WHERE rating = ?"
        params.append(str(rating))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
