"""Party / customer matching — silent ILIKE for analytics, lists for lookup."""

from __future__ import annotations

import re
from typing import Any

from eva_dashboard.db import connect, init_db

# City / branch suffixes stripped when detecting Al Shaheer-style families.
_BRANCH_SUFFIX_TOKENS = frozenset(
    {
        "lahore",
        "karachi",
        "islamabad",
        "rawalpindi",
        "faisalabad",
        "multan",
        "peshawar",
        "quetta",
        "hyderabad",
        "sialkot",
        "gujranwala",
        "sahiwal",
        "sukkur",
        "bahawalpur",
        "dha",
        "gulberg",
        "clifton",
        "outlet",
        "store",
        "branch",
        "north",
        "south",
        "east",
        "west",
        "central",
        "city",
        "mall",
        "plaza",
        "warehouse",
        "depot",
        "hq",
        "head",
        "office",
    }
)


def party_stem(name: str) -> str:
    """Normalize a party name to a branch-family stem (drop city/outlet tokens)."""
    tokens = re.findall(r"[a-z0-9]+", (name or "").lower())
    kept = [t for t in tokens if t not in _BRANCH_SUFFIX_TOKENS and len(t) >= 2]
    return " ".join(kept).strip()


def party_matches_look_like_branches(query: str, matches: list[str]) -> bool:
    """True when matches are the same customer family (Al Shaheer Lahore/Karachi).

    Prefers shared stem after stripping city/branch suffixes. Falls back to
    requiring every match to contain every significant query token.
    """
    ms = [str(m).strip() for m in (matches or []) if str(m).strip()]
    if len(ms) < 2:
        return False
    tokens = [
        t
        for t in re.findall(r"[a-z0-9]+", (query or "").lower())
        if len(t) >= 3 and t not in {"the", "and", "for", "ltd", "pvt"}
    ]
    stems = {party_stem(m) for m in ms}
    stems.discard("")
    if len(stems) == 1:
        stem = next(iter(stems))
        if not tokens:
            return True
        # Query tokens should sit inside the shared stem (or raw names)
        blob = f"{stem} " + " ".join(m.lower() for m in ms)
        return all(t in blob for t in tokens)
    if not tokens:
        return False
    # Divergent stems — only treat as branches when every match contains
    # every query token (still one family spoken as a short name).
    hit = 0
    for m in ms:
        ml = m.lower()
        if all(t in ml for t in tokens):
            hit += 1
    return hit == len(ms) and len(tokens) >= 2


def family_display_label(query: str, matches: list[str]) -> str:
    """Human label for an aggregated branch family profile."""
    q = re.sub(r"\s+", " ", (query or "").strip())
    if q:
        return f"{q.title()} (all branches)"
    stems = {party_stem(m) for m in matches if party_stem(m)}
    if len(stems) == 1:
        stem = next(iter(stems))
        return f"{stem.title()} (all branches)"
    return "Customer family (all branches)"


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

        # Fuzzy fallback when LIKE misses typos ("al shaher" → Al Shaheer…)
        if not matches:
            from eva_dashboard.client_language import lookup_party

            fuzzy = lookup_party(q, limit=limit)
            for m in fuzzy.get("matches") or []:
                name = str(m.get("client") or "").strip()
                key = name.lower()
                if name and key not in seen and float(m.get("match_score") or 0) >= 0.45:
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
