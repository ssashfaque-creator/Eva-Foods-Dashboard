"""Party / customer matching — silent ILIKE for analytics, lists for lookup."""

from __future__ import annotations

from typing import Any

from eva_dashboard.db import connect, init_db


def list_party_matches(query: str | None, *, limit: int = 8) -> list[str]:
    """Return canonical party names matching ``query`` (exact first, then ILIKE)."""
    q = (query or "").strip()
    if not q:
        return []

    needle = q.lower()
    init_db()
    matches: list[str] = []
    seen: set[str] = set()

    with connect() as conn:
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
            return matches

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

    return matches


def resolve_party_filter(query: str | None, *, limit: int = 8) -> dict[str, Any]:
    """Resolve a spoken party name for analytics — never fails.

    - Exactly 1 match → exact ``party`` filter
    - 0 or >1 matches → silent ``party_ilike`` fragment (SQL ``LIKE %frag%``)
      so "al shaheer" captures every Al Shaheer branch without LLM retries
    """
    q = (query or "").strip()
    if not q:
        return {
            "ok": True,
            "party": None,
            "party_ilike": None,
            "matches": [],
            "match_mode": None,
        }

    matches = list_party_matches(q, limit=limit)
    if len(matches) == 1:
        mode = "exact" if matches[0].lower().strip() == q.lower() else "ilike"
        return {
            "ok": True,
            "party": matches[0],
            "party_ilike": None,
            "matches": matches,
            "match_mode": mode,
            "query": q,
        }

    # Ambiguous or unknown — inject ILIKE; empty result set is fine
    return {
        "ok": True,
        "party": None,
        "party_ilike": [q],
        "matches": matches,
        "match_mode": "ilike_filter",
        "query": q,
    }


def resolve_party_filters(
    queries: list[str] | None, *, limit: int = 8
) -> dict[str, Any]:
    """Resolve one or more spoken party names (compare Al Shaheer vs Metro…)."""
    parties: list[str] = []
    party_ilike: list[str] = []
    matches: list[str] = []
    seen_exact: set[str] = set()
    seen_like: set[str] = set()

    for raw in queries or []:
        q = str(raw or "").strip()
        if not q:
            continue
        resolved = resolve_party_filter(q, limit=limit)
        for m in resolved.get("matches") or []:
            if m not in matches:
                matches.append(m)
        if resolved.get("party"):
            key = str(resolved["party"]).lower()
            if key not in seen_exact:
                seen_exact.add(key)
                parties.append(str(resolved["party"]))
        for frag in resolved.get("party_ilike") or []:
            key = frag.lower().strip()
            if key and key not in seen_like:
                seen_like.add(key)
                party_ilike.append(frag)

    return {
        "ok": True,
        "parties": parties,
        "party_ilike": party_ilike,
        "matches": matches,
    }


def fuzzy_match_party(query: str | None, *, limit: int = 8) -> dict[str, Any]:
    """Backward-compatible wrapper — analytics-safe silent resolution.

    Prefer ``resolve_party_filter``. Always returns ``ok: True`` for analytics.
    """
    return resolve_party_filter(query, limit=limit)
