"""Dynamic metadata grounding — inject business glossary snippets on demand.

When a user mentions a channel (Imtiaz, Metro, …) or brand alias, retrieve the
exact governed spelling and inject a short JSON block into the planner prompt.
"""

from __future__ import annotations

import json
import re
from typing import Any


def ground_entities_for_prompt(user_text: str) -> str:
    """Short JSON glossary block for matched business terms."""
    from eva_dashboard.client_language import (
        CLIENT_TYPE_ALIASES,
        extract_all_client_types_from_text,
    )
    from eva_dashboard.client_type_map import CLIENT_TYPE_GROUP, sql_client_type_values

    t = user_text or ""
    if not t.strip():
        return ""

    channels = extract_all_client_types_from_text(t)
    channel_blocks: list[dict[str, Any]] = []
    for ch in channels:
        # Reverse aliases that map to this canonical / group label
        aliases = sorted(
            {a for a, canon in CLIENT_TYPE_ALIASES.items() if canon == ch}
        )[:10]
        members: list[str] = []
        try:
            members = list(sql_client_type_values(ch) or [])[:12]
        except Exception:
            # Fallback: raw types that remap to this group
            members = [k for k, v in CLIENT_TYPE_GROUP.items() if v == ch][:12]
        channel_blocks.append(
            {
                "canonical": ch,
                "spoken_aliases": aliases,
                "db_members": members,
                "filter_key": "client_type",
            }
        )

    brand_hits: list[str] = []
    tl = t.lower()
    brand_map = {
        "eva consumer": "Eva Consumer",
        "eva industrial": "Eva Industrial",
        "maan oil": "Maan Oil",
        "maan consumer": "Maan Consumer",
    }
    for spoken, canon in brand_map.items():
        if spoken in tl:
            brand_hits.append(canon)

    if not channel_blocks and not brand_hits:
        return ""

    payload: dict[str, Any] = {}
    if channel_blocks:
        payload["channels"] = channel_blocks
        payload["channel_rule"] = (
            "Use filters.client_type with the canonical name. "
            "Never put channel names in filters.party."
        )
    if brand_hits:
        payload["business_units"] = brand_hits
        payload["bu_rule"] = (
            "Eva/Maan* brand names → business_units, NEVER client_type."
        )
    return (
        "GROUNDED_GLOSSARY (use exact spellings in filters):\n"
        f"{json.dumps(payload, indent=2, default=str)}\n"
    )
