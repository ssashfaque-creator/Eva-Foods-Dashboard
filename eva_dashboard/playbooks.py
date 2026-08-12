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
            "execute_read_only_sql: SELECT MIN(rate) AS min_rate FROM sales WHERE …",
            "execute_read_only_sql: SELECT party, product, date, rate, mt_qty FROM sales "
            "WHERE rate = <min_rate> [AND party LIKE …] ORDER BY date DESC LIMIT 20",
            "Present party, date, product, rate, MT — then ### Analysis",
        ],
    },
    {
        "id": "highest_rate_then_buyer",
        "pattern": re.compile(
            r"\b(highest|maximum|max)\b.+\b(rate|price)\b|"
            r"\b(rate|price)\b.+\b(highest|maximum)\b.+\b(who|buyer)\b",
            flags=re.I,
        ),
        "title": "Highest rate → who paid",
        "steps": [
            "Resolve party/product if named",
            "execute_read_only_sql: SELECT MAX(rate) AS max_rate …",
            "Second SQL: parties/products at that max rate with date + MT",
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
        "id": "distributors_declined",
        "pattern": re.compile(
            r"\b(distributors?|parties|customers?)\b.+\b(declin\w*|dropp\w*|fell|behind|lost)\b|"
            r"\b(declin\w*|dropp\w*|fell|behind)\b.+\b(distributors?|ams|vs\.?\s*ams)\b|"
            r"\bdeclined_only\b|\bunderperform",
            flags=re.I,
        ),
        "title": "Distributor decline / behind AMS",
        "steps": [
            "run_standard_analytics_pivot metric=vs_ams or ams_growth, sort=asc, "
            "declined_only=true when asking who declined vs AMS",
            "filters.client_type='Eva Distributors' when distributors named",
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
    {
        "id": "top_n_customers_month",
        "pattern": re.compile(
            r"\btop\s+\d+\b.+\b(customers?|parties|distributors?)\b|"
            r"\b(customers?|parties)\b.+\btop\s+\d+\b|"
            r"\bwho\s+are\s+the\s+top\b",
            flags=re.I,
        ),
        "title": "Top-N customers",
        "steps": [
            "run_standard_analytics_pivot rows=['party'], metrics volume/ams/vs_ams, "
            "limit=N, SPECIFIC_MONTH if a month is named",
            "Apply metric_filters if AMS threshold was stated",
            "Do not return more than N body rows",
        ],
    },
    {
        "id": "volume_and_avg_price",
        "pattern": re.compile(
            r"\b(volume|sales|mt)\b.+\b(average|avg)\s*(price|rate)\b|"
            r"\b(average|avg)\s*(price|rate)\b.+\b(volume|sales|mt)\b|"
            r"\bmonthly\s+sales\s+and\s+(average\s+)?(price|rate)\b",
            flags=re.I,
        ),
        "title": "Volume + average price",
        "steps": [
            "run_standard_analytics_pivot metrics=['volume','avg_price'] "
            "(universal pivot) — never multiply MT via bad joins",
            "Party scope from GROUNDED_PARTIES when a customer is named",
        ],
    },
    {
        "id": "price_fetch_followup",
        "pattern": re.compile(
            r"\bprice\s*fetch\b|\boil\s*price\s*fetched\b|\bapply\s+the\s+cost\s+factor\b",
            flags=re.I,
        ),
        "title": "Price Fetch",
        "steps": [
            "run_standard_analytics_pivot metrics=['price_fetch'] with prior filters "
            "(oil/packing/channel) — never invent 37.3246 math in SQL",
            "Keep sticky MemoryContext filters unless user clears them",
        ],
    },
    {
        "id": "yoy_compare",
        "pattern": re.compile(
            r"\b(yoy|year\s*over\s*year|vs\.?\s*(last\s+)?year|20\d{2}\s+vs\.?\s+20\d{2})\b|"
            r"\b(july|jan|feb|mar|apr|may|jun|aug|sep|oct|nov|dec)\w*\s+20\d{2}\s+vs",
            flags=re.I,
        ),
        "title": "YoY compare",
        "steps": [
            "run_standard_analytics_pivot with compare='yoy' and the later year as "
            "SPECIFIC_MONTH / current period",
            "Keep channel/BU filters from spoken text",
        ],
    },
    {
        "id": "exclude_then_refresh",
        "pattern": re.compile(
            r"\b(exclude|excluding|without|remove|drop)\b.+\b(and\s+)?(show|list|give)?|"
            r"\b(exclude|remove).+\bams\b",
            flags=re.I,
        ),
        "title": "Exclude then refresh table",
        "steps": [
            "If AMS/volume threshold exclude → metric_filters (not party_like)",
            "Else excludes.party_like / client_type / business_unit from spoken polarity",
            "Re-run the prior grain via run_standard_analytics_pivot with base=prior",
        ],
    },
    {
        "id": "last_price_sold",
        "pattern": re.compile(
            r"\b(last|latest|most\s+recent)\s+(price|rate)\b|"
            r"\blast\s+(price\s+)?sold\b|\bdate\s+of\s+sale\b",
            flags=re.I,
        ),
        "title": "Last sold price",
        "steps": [
            "run_standard_analytics_pivot metrics=['last_price'] "
            "(+ product row grain for SKU breakup)",
            "Or SQL: ORDER BY date DESC LIMIT 1 per party/product if novel grain",
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


def playbook_ids(user_text: str) -> list[str]:
    return [h["id"] for h in match_playbooks(user_text)]
