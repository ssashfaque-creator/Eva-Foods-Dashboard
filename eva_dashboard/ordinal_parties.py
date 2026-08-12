"""Ordinal party picks from a prior who-is / party_lookup match list.

Examples: "first 2", "the first two", "#1 and #2", "1 and 2", "number 1".
"""

from __future__ import annotations

import re
from typing import Any

_WORD_NUM = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _to_int(token: str) -> int | None:
    t = (token or "").strip().lower()
    if not t:
        return None
    if t.isdigit():
        return int(t)
    return _WORD_NUM.get(t)


def extract_ordinal_indices(user_text: str) -> list[int]:
    """Return 1-based ordinal indices mentioned in the ask (deduped, ordered)."""
    t = (user_text or "").lower()
    if not t.strip():
        return []
    found: list[int] = []

    def _add(n: int | None) -> None:
        if n is None or n < 1 or n > 25:
            return
        if n not in found:
            found.append(n)

    # "first 2" / "first two" / "the first three"
    m = re.search(
        r"\b(?:the\s+)?first\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
        t,
    )
    if m:
        n = _to_int(m.group(1))
        if n:
            for i in range(1, n + 1):
                _add(i)

    # "#1 and #2" / "numbers 1 and 2" / "1 and 2" / "1, 2, and 3"
    for m in re.finditer(r"#\s*(\d+)\b", t):
        _add(int(m.group(1)))
    m = re.search(
        r"\b(?:numbers?|matches?|rows?|ones?)\s+"
        r"((?:\d+\s*(?:,|and|&)\s*)+\d+)\b",
        t,
    )
    if m:
        for tok in re.findall(r"\d+", m.group(1)):
            _add(int(tok))
    # Bare "1 and 2" near volume/show language (avoid dates)
    if not found and re.search(
        r"\b(show|volumes?|sales?|include|both|those)\b", t
    ):
        m = re.search(
            r"\b(\d+|one|two|three)\s+(?:and|&)\s+(\d+|one|two|three)\b",
            t,
        )
        if m:
            _add(_to_int(m.group(1)))
            _add(_to_int(m.group(2)))

    return found


def matches_from_prior(prior: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not prior:
        return []
    for key in ("matches", "party_matches", "lookup_matches"):
        raw = prior.get(key)
        if isinstance(raw, list) and raw:
            return [m for m in raw if isinstance(m, dict)]
    # Nested under party_scope / filters metadata
    scope = prior.get("party_scope") or {}
    if isinstance(scope.get("matches"), list):
        return [m for m in scope["matches"] if isinstance(m, dict)]
    return []


def resolve_ordinal_party_names(
    user_text: str,
    prior: dict[str, Any] | None,
) -> list[str]:
    """Map spoken ordinals onto prior who-is match names."""
    matches = matches_from_prior(prior)
    if not matches:
        return []
    idxs = extract_ordinal_indices(user_text)
    if not idxs:
        return []
    names: list[str] = []
    for i in idxs:
        if 1 <= i <= len(matches):
            name = str(
                matches[i - 1].get("client")
                or matches[i - 1].get("party")
                or matches[i - 1].get("name")
                or ""
            ).strip()
            if name and name not in names:
                names.append(name)
    return names


def looks_ordinal_party_followup(user_text: str) -> bool:
    t = (user_text or "").lower()
    if not t.strip():
        return False
    if extract_ordinal_indices(t):
        return True
    return bool(
        re.search(
            r"\b(both|those|these)\s+(matches?|customers?|parties|ones|al\s+\w+)\b|"
            r"\bthe\s+(matches?|ones|customers?)\s+(you\s+)?identified\b|"
            r"\bboth\s+.+\s+customers?\b|"
            r"\binclude\s+both\b",
            t,
        )
    )
