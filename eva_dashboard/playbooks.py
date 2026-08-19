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
        "id": "compound_metric_rank",
        "pattern": re.compile(
            r"\b(more than|less than|greater than|at least|below|above|over)\b"
            r".+\b(but|and)\b.+"
            r"\b(more than|less than|greater than|at least|below|above|over|"
            r"growth|yoy|ams|volume|sales)\b",
            flags=re.I,
        ),
        "title": "Stacked metric cuts on a party list",
        "steps": [
            "run_standard_analytics_pivot with row_dimensions=['party'] and "
            "NO column_dimensions month (one window, not a month grid)",
            "Put EVERY numeric cut in metric_filters (AND). "
            "sales/volume MT → metric=volume; '10 MT AMS' / AMS>10 → metric=ams "
            "(the 3-month AMS KPI — never also add volume>10 for that number); "
            "calendar YoY % → metric=yoy; "
            "AMS-window growth (no last-year language) → metric=ams_growth. "
            "A trailing % on AMS/volume is growth ('less than 5% in AMS'), "
            "not an AMS-tonnage cut.",
            "Last N months vs the same N months last year → "
            "period_type=LAST_N_MONTHS, months_back=N, compare='yoy', metric='yoy'. "
            "Last N months vs the prior N months → compare='prior', metric='pop'. "
            "Do NOT use ams_growth for those compares — AMS growth is always "
            "the current 3-month AMS window vs the previous 3-month AMS window.",
            "Distributors → filters.client_type='Eva Distributors'. "
            "'all' matching parties → limit=200. Complete new ask → "
            "state_action='clear'. Never invent AMS/YoY in SQL.",
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
            "If the ask is last N months vs the same N months last year / YoY, "
            "follow yoy_compare (compare='yoy', metric_filters metric=yoy). "
            "If last N vs the prior N months, follow pop_compare "
            "(compare='prior', metric='pop'). Do NOT use ams_growth for those.",
            "Otherwise run_standard_analytics_pivot with metrics including "
            "ams_growth, row_dimensions=['party'], "
            "filters.client_type='Eva Distributors' when distributors are named, "
            "grown_only=true if they ask who grew",
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
        "id": "party_profile",
        "pattern": re.compile(
            r"\b(tell\s+me\s+about|customer\s+profile|party\s+profile|"
            r"rundown\s+on|full\s+picture|everything\s+about|"
            r"last\s+(purchase|invoice)\b)",
            flags=re.I,
        ),
        "title": "Customer profile",
        "steps": [
            "run_standard_analytics_pivot operation=party_profile "
            "(volume, AMS, % vs AMS, last purchase, rate) — not party_lookup",
            "SPECIFIC_MONTH if a month is named; GROUNDED_PARTIES for the name",
            "Do NOT answer with an identity-only who-is table",
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
            r"\b(yoy|year\s*over\s*year|year\s+on\s+year|"
            r"vs\.?\s*(last\s+)?year|versus\s+(the\s+)?(same\s+)?(last\s+)?year|"
            r"same\s+(period|\d+\s+months?|months?|three|six|twelve)\s+last\s+year|"
            r"same\s+(window|span|time|months?)\s+last\s+year|"
            r"last\s+\d+\s+months?\s+(vs\.?|versus|compared?\s+to).{0,80}last\s+year|"
            r"20\d{2}\s+vs\.?\s+20\d{2})\b|"
            r"\b(july|jan|feb|mar|apr|may|jun|aug|sep|oct|nov|dec)\w*\s+20\d{2}\s+vs",
            flags=re.I,
        ),
        "title": "YoY compare",
        "steps": [
            "run_standard_analytics_pivot with compare='yoy'. "
            "Last N months vs the same N months last year → "
            "LAST_N_MONTHS + months_back=N, metric='yoy' (calendar YoY of that "
            "window). Named month vs last year → SPECIFIC_MONTH of the later year.",
            "Party/distributor lists: row_dimensions=['party'], no month columns. "
            "Lowest/least/smallest growth vs last year → metric='yoy', sort=asc. "
            "Highest/biggest growth vs last year → metric='yoy', sort=desc. "
            "Never ams_growth / title_mode=smallest_gains for 'same months last year'. "
            "'with AMS>10' is a size cut (metric_filters), not metric='yoy_ams'.",
            "ams_growth is a different metric (current 3-month AMS window vs prior "
            "3-month AMS window) — do not use it for last-N vs last year OR last-N "
            "vs the prior N months.",
        ],
    },
    {
        "id": "pop_compare",
        "pattern": re.compile(
            r"\b(prior|previous|preceding)\s+(\d+\s+)?months?\b|"
            r"\bvs\.?\s+(the\s+)?(prior|previous)\s+(period|\d+\s+months?)\b|"
            r"\bperiod\s+over\s+period\b",
            flags=re.I,
        ),
        "title": "Prior-period compare",
        "steps": [
            "run_standard_analytics_pivot with compare='prior', metric='pop'. "
            "Last N months vs the N months immediately before that window. "
            "NOT ams_growth (3-month AMS vs previous 3-month AMS) and NOT "
            "yoy (same span last year).",
            "Party/distributor lists: row_dimensions=['party'], no month columns. "
            "Growth of AMS over those N-month windows is the same % as volume.",
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
    from eva_dashboard.metric_filters import (
        looks_prior_period_compare,
        looks_yoy_period_compare,
        parse_metric_filters,
    )

    t = user_text or ""
    yoy = looks_yoy_period_compare(t)
    pop = looks_prior_period_compare(t)
    stacked = len(parse_metric_filters(t)) >= 2
    hits: list[dict[str, Any]] = []
    for pb in _PLAYBOOKS:
        if pb["id"] == "distributors_grown" and (yoy or pop):
            # Calendar YoY / prior-period are not AMS-window growth ranking.
            continue
        if pb["id"] == "pop_compare" and yoy:
            continue
        matched = bool(pb["pattern"].search(t))
        if pb["id"] == "yoy_compare" and yoy:
            matched = True
        if pb["id"] == "pop_compare" and pop:
            matched = True
        if pb["id"] == "compound_metric_rank" and stacked:
            matched = True
        if matched:
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
