"""Multi-hop playbooks — turn common commercial asks into reliable tool recipes."""

from __future__ import annotations

import re
from typing import Any


_PLAYBOOKS: list[dict[str, Any]] = [
    {
        "id": "lowest_rate_then_buyer",
        "pattern": re.compile(
            r"\b(lowest|minimum|min)\b.+\b(rate|price)\b|"
            r"\b(rate|price)\b.+\b(lowest|minimum)\b.+\b(who|buyer|sold\s+to)\b|"
            r"\bwho\b.+\b(lowest|minimum)\b.+\b(rate|price)\b",
            flags=re.I,
        ),
        "title": "Lowest rate → who bought",
        "steps": [
            "lookup_entity_values on sales.party (or clients.client) for any named customer",
            "execute_read_only_sql: SELECT MIN(rate) AS min_rate, party FROM sales "
            "WHERE … GROUP BY party OR global MIN then filter",
            "execute_read_only_sql: SELECT party, product, date, rate, mt_qty FROM sales "
            "WHERE rate = <min_rate> [AND party LIKE …] ORDER BY date DESC LIMIT 20",
            "Present a small table: party, date, product, rate, MT — then ### Analysis",
        ],
    },
    {
        "id": "rate_then_math",
        "pattern": re.compile(
            r"\b(multiply|divide|times|\*|×|/)\b.+\b(rate|price)\b|"
            r"\b(rate|price)\b.+\b(multiply|divide|times|\*|×)\b|"
            r"\b\d+(?:\.\d+)?\s*(?:\*|×|x)\s*\d",
            flags=re.I,
        ),
        "title": "Fetch rate → calculate_expression",
        "steps": [
            "Resolve the party/product with lookup_entity_values",
            "execute_read_only_sql to fetch the exact rate (MIN/AVG/last as implied)",
            "calculate_expression with the literal numbers — never multiply in your head",
            "Show source rate + formula + Result",
        ],
    },
    {
        "id": "distributors_grown",
        "pattern": re.compile(
            r"\b(distributors?|parties|customers?)\b.+\b(grown|growth|grew)\b|"
            r"\b(grown|growth|grew)\b.+\b(distributors?|parties|customers?)\b|"
            r"\bams\s+growth\b|\bgrown_only\b",
            flags=re.I,
        ),
        "title": "Distributor growth ranking",
        "steps": [
            "run_standard_analytics_pivot with metrics including ams_growth, "
            "row_dimensions=['party'], filters.client_type='Eva Distributors' "
            "when distributors are named, grown_only=true if they ask who grew",
            "Do NOT invent AMS growth in SQL",
        ],
    },
    {
        "id": "same_date_price_variance",
        "pattern": re.compile(
            r"\b(same[- ]?date|same\s+day|on\s+the\s+same\s+day)\b.+\b(price|rate)\b|"
            r"\b(different|differing|dispersion|variance)\b.+\b(price|rate)\b|"
            r"\bsold\s+at\s+different\s+(price|rate)s?\b",
            flags=re.I,
        ),
        "title": "Same-date rate dispersion",
        "steps": [
            "execute_read_only_sql grouping by date, product: "
            "COUNT(DISTINCT rate), MIN(rate), MAX(rate), COUNT(DISTINCT party)",
            "Filter HAVING COUNT(DISTINCT rate) > 1",
            "Optional second SQL listing parties at each rate on that date",
        ],
    },
    {
        "id": "who_is_then_sales",
        "pattern": re.compile(
            r"\bwho\s+is\b.+\b(and|,)\s*(show|what|sales|volume|ams)\b",
            flags=re.I,
        ),
        "title": "Identity then sales",
        "steps": [
            "lookup_entity_values / run_standard_analytics_pivot operation=party_lookup",
            "Then pivot volume/AMS for the resolved party",
        ],
    },
]


def match_playbooks(user_text: str) -> list[dict[str, Any]]:
    t = user_text or ""
    hits: list[dict[str, Any]] = []
    for pb in _PLAYBOOKS:
        if pb["pattern"].search(t):
            hits.append(
                {
                    "id": pb["id"],
                    "title": pb["title"],
                    "steps": list(pb["steps"]),
                }
            )
    return hits


def playbook_prompt_block(user_text: str) -> str:
    hits = match_playbooks(user_text)
    if not hits:
        return ""
    lines = ["=== PLAYBOOK (follow these tool steps) ==="]
    for h in hits[:2]:
        lines.append(f"## {h['title']} ({h['id']})")
        for i, step in enumerate(h["steps"], 1):
            lines.append(f"{i}. {step}")
    return "\n".join(lines)
