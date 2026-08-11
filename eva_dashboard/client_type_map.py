"""Existing client type → new reporting group.

Raw Excel / sales labels are remapped so pivots, filters, and chatbot
answers always use the new groupings (never the old long-tail types).

Mapping is data-driven: ``client_type_groups.json`` (ops remap sheet).
Spoken shorthands (metro → METRO HABIB, imt → IMT) live in
``client_language.CLIENT_TYPE_ALIASES``.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_GROUPS_PATH = Path(__file__).resolve().parent / "client_type_groups.json"


@lru_cache(maxsize=1)
def load_client_type_groups() -> dict[str, str]:
    """Raw client type → reporting channel group."""
    if not _GROUPS_PATH.exists():
        return {}
    data = json.loads(_GROUPS_PATH.read_text(encoding="utf-8"))
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, dict):
        return {}
    return {str(k): str(v) for k, v in groups.items() if k is not None and v is not None}


# Back-compat name used across the codebase / tests
CLIENT_TYPE_GROUP: dict[str, str] = load_client_type_groups()


def reload_client_type_groups() -> dict[str, str]:
    """Reload JSON (tests / hot swap). Clears caches."""
    load_client_type_groups.cache_clear()
    global CLIENT_TYPE_GROUP, _GROUP_BY_KEY, _NEW_GROUPS, _NEW_GROUP_BY_KEY
    global _SOURCES_BY_GROUP
    CLIENT_TYPE_GROUP = load_client_type_groups()
    _rebuild_indexes()
    return CLIENT_TYPE_GROUP


def _norm_key(value: str | None) -> str:
    if not value:
        return ""
    t = str(value).strip().lower()
    t = t.replace("_", " ").replace("-", " ")
    t = re.sub(r"[/]+", "/", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _rebuild_indexes() -> None:
    global _GROUP_BY_KEY, _NEW_GROUPS, _NEW_GROUP_BY_KEY, _SOURCES_BY_GROUP
    _GROUP_BY_KEY = {_norm_key(src): dest for src, dest in CLIENT_TYPE_GROUP.items()}
    _NEW_GROUPS = set(CLIENT_TYPE_GROUP.values())
    _NEW_GROUP_BY_KEY = {_norm_key(g): g for g in _NEW_GROUPS}
    _SOURCES_BY_GROUP = {}
    for src, dest in CLIENT_TYPE_GROUP.items():
        _SOURCES_BY_GROUP.setdefault(dest, []).append(src)
    for g in _NEW_GROUPS:
        bucket = _SOURCES_BY_GROUP.setdefault(g, [])
        if g not in bucket:
            bucket.append(g)


_GROUP_BY_KEY: dict[str, str] = {}
_NEW_GROUPS: set[str] = set()
_NEW_GROUP_BY_KEY: dict[str, str] = {}
_SOURCES_BY_GROUP: dict[str, list[str]] = {}
_rebuild_indexes()


def map_client_type(value: str | None) -> str | None:
    """Map a raw / existing client type to its new reporting group.

    Unknown labels pass through unchanged (trimmed). Blank → None.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    key = _norm_key(text)
    if key in _GROUP_BY_KEY:
        return _GROUP_BY_KEY[key]
    if key in _NEW_GROUP_BY_KEY:
        return _NEW_GROUP_BY_KEY[key]
    return text


def is_new_client_type(value: str | None) -> bool:
    if not value:
        return False
    return _norm_key(value) in _NEW_GROUP_BY_KEY


def list_new_client_types() -> list[str]:
    return sorted(_NEW_GROUPS, key=lambda s: s.lower())


def raw_client_types_for_group(group: str | None) -> list[str]:
    """Raw/source labels (plus the group itself) that belong to ``group``."""
    mapped = map_client_type(group)
    if not mapped:
        return []
    sources = list(_SOURCES_BY_GROUP.get(mapped) or [mapped])
    out: list[str] = []
    seen: set[str] = set()
    for s in sources:
        k = _norm_key(s)
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def canonical_raw_client_type(value: str | None) -> str | None:
    """Canonical spelling of an existing/source client type, if known."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    key = _norm_key(text)
    for src in CLIENT_TYPE_GROUP:
        if _norm_key(src) == key:
            return src
    return text


def is_specific_raw_client_type(value: str | None) -> bool:
    """True when ``value`` names an old/source type that rolls up into a broader group.

    E.g. CHASE UP → IMT (specific). Bare group names like IMT / LMT are not.
    """
    if not value:
        return False
    key = _norm_key(value)
    if key in _NEW_GROUP_BY_KEY and key not in _GROUP_BY_KEY:
        return False
    if key not in _GROUP_BY_KEY:
        return False
    dest = _GROUP_BY_KEY[key]
    return _norm_key(dest) != key


def classify_client_type_filter(
    value: str | None,
) -> tuple[str, str] | None:
    """How to apply a client-type filter.

    Returns ``(\"raw\", label)`` for a specific old type (Chase Up, CSD, …)
    or ``(\"group\", label)`` for a new reporting group (IMT, LMT, …).
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if is_specific_raw_client_type(text):
        return ("raw", canonical_raw_client_type(text) or text)
    mapped = map_client_type(text)
    if mapped:
        return ("group", mapped)
    return ("raw", text)


def sql_client_type_values(value: str | None) -> list[str]:
    """Labels to match against raw ``sales.client_type`` / ``clients.type``."""
    classified = classify_client_type_filter(value)
    if not classified:
        return []
    mode, label = classified
    if mode == "raw":
        return [label]
    return raw_client_types_for_group(label) or [label]
