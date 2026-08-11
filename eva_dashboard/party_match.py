"""Fuzzy party / customer matching with disambiguation for the planner."""

from __future__ import annotations

from typing import Any

from eva_dashboard.db import connect, init_db


def fuzzy_match_party(query: str | None, *, limit: int = 8) -> dict[str, Any]:
    """Resolve a spoken party name against ``clients`` / ``sales``.

    - Exactly 1 ILIKE match → ``{ok: True, party: <canonical>}``
    - 0 or >1 matches → ``{ok: False, error, matches}`` for the LLM to ask
      the user to clarify.
    """
    q = (query or "").strip()
    if not q:
        return {"ok": True, "party": None}

    needle = q.lower()
    init_db()
    matches: list[str] = []
    seen: set[str] = set()

    with connect() as conn:
        # Prefer exact (case-insensitive) first
        exact = conn.execute(
            """
            SELECT client AS name FROM clients
            WHERE client IS NOT NULL AND trim(client) != ''
              AND lower(trim(client)) = ?
            UNION
            SELECT party AS name FROM sales
            WHERE party IS NOT NULL AND trim(party) != ''
              AND lower(trim(party)) = ?
            LIMIT 5
            """,
            (needle, needle),
        ).fetchall()
        for row in exact:
            name = str(row["name"] or "").strip()
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                matches.append(name)

        if len(matches) == 1:
            return {"ok": True, "party": matches[0], "match_mode": "exact"}

        # Containment / ILIKE
        like = f"%{needle}%"
        rows = conn.execute(
            """
            SELECT name, MAX(priority) AS priority FROM (
              SELECT client AS name, 2 AS priority FROM clients
              WHERE client IS NOT NULL AND trim(client) != ''
                AND lower(client) LIKE ?
              UNION ALL
              SELECT party AS name, 1 AS priority FROM sales
              WHERE party IS NOT NULL AND trim(party) != ''
                AND lower(party) LIKE ?
            )
            GROUP BY name
            ORDER BY priority DESC, length(name) ASC, name COLLATE NOCASE
            LIMIT ?
            """,
            (like, like, int(limit)),
        ).fetchall()
        matches = []
        seen = set()
        for row in rows:
            name = str(row["name"] or "").strip()
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                matches.append(name)

    if len(matches) == 1:
        return {"ok": True, "party": matches[0], "match_mode": "ilike"}

    if not matches:
        return {
            "ok": False,
            "error": (
                f"No party matched '{q}'. Ask the user to confirm the customer / "
                "distributor name, or try a longer fragment."
            ),
            "matches": [],
            "query": q,
        }

    top = matches[:5]
    return {
        "ok": False,
        "error": (
            f"Ambiguous party '{q}'. Top matches: {top}. "
            "Please ask the user to clarify which one they mean."
        ),
        "matches": top,
        "query": q,
    }
