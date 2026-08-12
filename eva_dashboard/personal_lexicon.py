"""Personal lexicon — nicknames and habitual prefs that make Eva "know you".

Stored under ``{data_root}/personal_lexicon.json`` so it survives updates.
Learns from successful party / channel resolutions during chat.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from eva_dashboard.paths import data_root

_LEXICON_NAME = "personal_lexicon.json"

# Built-in seeds (always available even before learning)
_SEED_PARTY_ALIASES: dict[str, str] = {
    "pepsi": "PEPSI",
    "pepsico": "PEPSI",
    "coca cola": "COCA",
    "coke": "COCA",
    "al shaheer": "SHAHEER",
    "shaheer": "SHAHEER",
    "imtiaz": "IMTIAZ",
}


def lexicon_path() -> Path:
    return data_root() / _LEXICON_NAME


def load_lexicon() -> dict[str, Any]:
    path = lexicon_path()
    data: dict[str, Any] = {
        "party_aliases": dict(_SEED_PARTY_ALIASES),
        "prefs": {},
        "recent_parties": [],
    }
    if not path.exists():
        return data
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return data
    if not isinstance(raw, dict):
        return data
    aliases = dict(_SEED_PARTY_ALIASES)
    for k, v in (raw.get("party_aliases") or {}).items():
        nk = _norm(str(k))
        nv = str(v or "").strip()
        if nk and nv:
            aliases[nk] = nv
    data["party_aliases"] = aliases
    prefs = raw.get("prefs") or {}
    if isinstance(prefs, dict):
        data["prefs"] = {
            str(k): v for k, v in prefs.items() if v not in (None, "", [], {})
        }
    recent = raw.get("recent_parties") or []
    if isinstance(recent, list):
        data["recent_parties"] = [str(x) for x in recent if str(x).strip()][:20]
    return data


def save_lexicon(data: dict[str, Any]) -> None:
    path = lexicon_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Don't persist seed-only duplicates as noise — persist user-learned + prefs
    seeds = set(_SEED_PARTY_ALIASES)
    learned = {
        k: v
        for k, v in (data.get("party_aliases") or {}).items()
        if k not in seeds or v != _SEED_PARTY_ALIASES.get(k)
    }
    payload = {
        "party_aliases": learned,
        "prefs": dict(data.get("prefs") or {}),
        "recent_parties": list(data.get("recent_parties") or [])[:20],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _norm(text: str) -> str:
    t = (text or "").lower().strip()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def remember_party_alias(spoken: str, canonical: str) -> None:
    """Learn spoken → canonical party fragment after a successful resolve."""
    sp = _norm(spoken)
    can = str(canonical or "").strip()
    if len(sp) < 2 or len(can) < 3:
        return
    data = load_lexicon()
    data["party_aliases"][sp] = can
    recent = [can] + [p for p in data.get("recent_parties") or [] if p != can]
    data["recent_parties"] = recent[:20]
    save_lexicon(data)


def remember_pref(key: str, value: Any) -> None:
    """Sticky prefs: default_city, default_bus, ams_window, etc."""
    k = str(key or "").strip()
    if not k or value in (None, "", [], {}):
        return
    data = load_lexicon()
    prefs = dict(data.get("prefs") or {})
    prefs[k] = value
    data["prefs"] = prefs
    save_lexicon(data)


def sync_prefs_from_memory(prior: dict[str, Any] | None) -> None:
    """Capture habitual filters from the active memory prior."""
    if not prior:
        return
    filters = dict(prior.get("filters") or {})
    if filters.get("city"):
        remember_pref("default_city", str(filters["city"]))
    bus = list(prior.get("business_units") or filters.get("business_units") or [])
    if bus:
        remember_pref("default_business_units", bus[:4])
    if filters.get("client_type"):
        remember_pref("default_client_type", str(filters["client_type"]))


def expand_aliases_in_text(user_text: str) -> tuple[str, list[dict[str, str]]]:
    """Replace known nicknames with richer search hints (non-destructive).

    Returns (hint_text, expansions). Does not rewrite the user message blindly —
    expansions are for grounding / prompt injection.
    """
    data = load_lexicon()
    aliases: dict[str, str] = dict(data.get("party_aliases") or {})
    # Longest keys first
    keys = sorted(aliases.keys(), key=len, reverse=True)
    expansions: list[dict[str, str]] = []
    low = _norm(user_text)
    for key in keys:
        if len(key) < 2:
            continue
        if re.search(rf"\b{re.escape(key)}\b", low):
            expansions.append({"spoken": key, "maps_to": aliases[key]})
    hint = user_text
    return hint, expansions


def lexicon_prompt_block(user_text: str = "") -> str:
    """JSON block for the agent system prompt."""
    data = load_lexicon()
    _, expansions = expand_aliases_in_text(user_text)
    payload: dict[str, Any] = {}
    if expansions:
        payload["matched_aliases"] = expansions
    prefs = data.get("prefs") or {}
    if prefs:
        payload["your_prefs"] = prefs
        payload["pref_rule"] = (
            "Apply default_city / default_business_units only when the user "
            "did not name a competing city/BU and the ask is a follow-up or "
            "underspecified sales question — never override an explicit clear."
        )
    recent = data.get("recent_parties") or []
    if recent:
        payload["recent_parties"] = recent[:8]
    if not payload:
        return ""
    return (
        "PERSONAL_LEXICON (how this user talks — use exact maps_to for lookups):\n"
        f"{json.dumps(payload, indent=2, default=str)}\n"
    )
