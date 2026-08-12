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
        "style": {},
        "recent_parties": [],
        "last_clarify": {},
        "ask_stats": {},
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
    style = raw.get("style") or {}
    if isinstance(style, dict):
        data["style"] = {
            str(k): v for k, v in style.items() if v not in (None, "", [], {})
        }
    recent = raw.get("recent_parties") or []
    if isinstance(recent, list):
        data["recent_parties"] = [str(x) for x in recent if str(x).strip()][:20]
    last_clarify = raw.get("last_clarify") or {}
    if isinstance(last_clarify, dict):
        data["last_clarify"] = last_clarify
    ask_stats = raw.get("ask_stats") or {}
    if isinstance(ask_stats, dict):
        data["ask_stats"] = ask_stats
    return data


def save_lexicon(data: dict[str, Any]) -> None:
    path = lexicon_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    seeds = set(_SEED_PARTY_ALIASES)
    learned = {
        k: v
        for k, v in (data.get("party_aliases") or {}).items()
        if k not in seeds or v != _SEED_PARTY_ALIASES.get(k)
    }
    payload = {
        "party_aliases": learned,
        "prefs": dict(data.get("prefs") or {}),
        "style": dict(data.get("style") or {}),
        "recent_parties": list(data.get("recent_parties") or [])[:20],
        "last_clarify": dict(data.get("last_clarify") or {}),
        "ask_stats": dict(data.get("ask_stats") or {}),
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
    style = data.get("style") or {}
    if style:
        payload["reply_style"] = style
        payload["style_rule"] = (
            "Honor reply_style: table_first=true means lead with the markdown "
            "table; brief_analysis=true means max 3 short Analysis bullets."
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


def remember_clarify(question: str, topic_key: str) -> None:
    """Record that we already asked a clarify — avoid looping."""
    data = load_lexicon()
    data["last_clarify"] = {
        "topic": _norm(topic_key)[:80],
        "question": str(question or "")[:240],
    }
    save_lexicon(data)


def should_skip_clarify(user_text: str, clarify_question: str) -> bool:
    """True when we just asked the same clarify and user didn't add detail."""
    data = load_lexicon()
    last = data.get("last_clarify") or {}
    if not last:
        return False
    prev_q = _norm(str(last.get("question") or ""))
    cur_q = _norm(clarify_question)
    # Same clarify topic + short user reply → don't re-ask; pick a default
    if prev_q and prev_q == cur_q:
        return True
    topic = str(last.get("topic") or "")
    if topic and topic in _norm(user_text) and len(_norm(user_text).split()) <= 6:
        # User answered briefly after clarify — don't clarify again
        return True
    return False


def learn_from_turn(
    user_text: str,
    *,
    route: dict[str, Any] | None = None,
    grounding: dict[str, Any] | None = None,
    tool_trace: list[dict[str, Any]] | None = None,
    answer: str = "",
    verify_ok: bool = False,
) -> None:
    """Update lexicon after a successful (or partially useful) agent turn."""
    route = route or {}
    grounding = grounding or {}
    trace = list(tool_trace or [])

    # Learn party aliases from grounding hits
    for hit in grounding.get("party_hits") or []:
        spoken = str(hit.get("spoken") or "")
        resolved = str(hit.get("resolved") or "")
        if spoken and resolved:
            remember_party_alias(spoken, resolved)

    # Style: if answer has a table and short analysis, remember preference
    if verify_ok and answer:
        has_table = "|" in answer or "<table" in answer.lower()
        analysis = "### analysis" in answer.lower()
        bullets = len(re.findall(r"^\s*[-*]", answer, flags=re.M))
        data = load_lexicon()
        style = dict(data.get("style") or {})
        if has_table:
            style["table_first"] = True
        if analysis and bullets <= 4:
            style["brief_analysis"] = True
        if style != data.get("style"):
            data["style"] = style
            save_lexicon(data)

    # Ask-kind stats for future routing bias
    kind = str(route.get("kind") or "")
    if kind:
        data = load_lexicon()
        stats = dict(data.get("ask_stats") or {})
        stats[kind] = int(stats.get(kind) or 0) + 1
        # Count successful tools
        ok_tools = sum(1 for t in trace if t.get("ok"))
        stats["tool_ok_total"] = int(stats.get("tool_ok_total") or 0) + ok_tools
        data["ask_stats"] = stats
        save_lexicon(data)

    # Extract party-like names from successful SQL LIKE patterns
    for t in trace:
        if t.get("tool") != "execute_read_only_sql" or not t.get("ok"):
            continue
        args = t.get("args") or {}
        sql = str(args.get("sql_query") or "")
        for m in re.finditer(
            r"(?:party|client)\s+LIKE\s+'%([^%']{3,40})%'",
            sql,
            flags=re.I,
        ):
            frag = m.group(1).strip()
            if frag and len(frag) >= 3:
                # Map a short token from user text if present
                for tok in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", user_text or ""):
                    if tok.lower() in frag.lower() and len(tok) >= 3:
                        remember_party_alias(tok, frag)
                        break

