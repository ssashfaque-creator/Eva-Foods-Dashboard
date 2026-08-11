"""Teaching material for the Semantic Planner — vocabulary only, not routing."""

from __future__ import annotations

from eva_dashboard.client_language import (
    CLIENT_TYPE_ALIASES,
    OIL_TYPE_ALIASES,
    PACKING_ALIASES,
)


def vocabulary_for_prompt() -> str:
    """Compact spoken → canonical dictionary for the system prompt."""

    def _group(aliases: dict[str, str]) -> list[str]:
        by_canon: dict[str, list[str]] = {}
        for spoken, canon in aliases.items():
            by_canon.setdefault(canon, []).append(spoken)
        lines: list[str] = []
        for canon in sorted(by_canon):
            spoken = ", ".join(sorted(by_canon[canon], key=len)[:8])
            lines.append(f"- {canon} ← {spoken}")
        return lines

    parts = [
        "VOCABULARY (YOU translate spoken words → QuerySpec fields):",
        "",
        "1. BRANDS → business_units",
        "- Eva → [\"Eva Consumer\", \"Eva Bulk\"] (BOTH). Never Shortening/Meal.",
        "- Maan → [\"Maan Consumer\", \"Maan Bulk\"] (BOTH).",
        "- Consumer (alone) → [\"Eva Consumer\"].",
        "- Bulk (Eva context) → Eva Bulk; \"Maan bulk\" → Maan Bulk.",
        "- Full names: Eva Consumer, Eva Bulk, Maan Consumer, Maan Bulk, Cusine King.",
        "",
        "2. DISTRIBUTOR AMBIGUITY (CRITICAL)",
        "- \"distributor sales\" / \"who are distributors\" → filters.client_type="
        "\"Eva Distributors\".",
        "- \"distributor-wise\" / \"by distributor\" / \"party-wise\" → group_by="
        "\"party\". Do NOT invent client_type unless the channel is named.",
        "- \"Eva distributor sales\" → BOTH brand BUs AND client_type=Eva Distributors.",
        "",
        "3. CHANNEL TYPES (only when named as a channel):",
        *_group(CLIENT_TYPE_ALIASES),
        "",
        "4. PRODUCT vs SKU",
        "- \"product-wise\" / \"by product\" → group_by or row_dimension="
        "packing_category.",
        "- \"SKU-wise\" / \"by SKU\" → product.",
        "",
        "5. PACKING:",
        *_group(PACKING_ALIASES),
        "",
        "6. OIL TYPES:",
        *_group(OIL_TYPE_ALIASES),
        "",
        "7. GEOGRAPHY",
        "- city / city_filter ← City-Filter (Lahore, Karachi, …)",
        "- zone ← SOUTH | CENTRAL | NORTH — ONLY when the user names a zone.",
        "- Do NOT invent a zone for unrecognized cities. The Python engine maps "
        "blank/unmapped/undefined City-Filter → Karachi → SOUTH automatically.",
        "- nationally / all over Pakistan → context_handling=prior (if follow-up) "
        "+ clear_filters:[\"city\",\"zone\"] (omit city filter).",
        "- other cities / city league → group_by=city + clear_filters:[\"city\"]",
        "- Switching Lahore → national: MUST clear_filters:[\"city\"].",
        "",
        "8. PERIOD (period_type is REQUIRED on every plan)",
        "- unspecified → period_type=MTD",
        "- \"last 6 months\" → period_type=LAST_N_MONTHS, months_back=6",
        "- \"July\" / \"July 2026\" → period_type=NAMED_MONTH, named_month=…",
        "- \"last month\" → LAST_MONTH; \"last week\" → LAST_WEEK",
        "",
        "9. PERFORMANCE METRICS (party_rank)",
        "- lowest/worst performing → ranking_metric=vs_ams, sort_order=asc",
        "- least/lowest gains → ranking_metric=ams_growth, sort_order=asc "
        "(grown_only=false)",
        "- biggest/highest gains → ams_growth, sort_order=desc",
        "- AMS = prior 3 full months mean (engine handles this; parties with "
        "AMS=0 are excluded from vs_ams / ams_growth ranks).",
    ]
    return "\n".join(parts)


def tool_guide_for_prompt() -> str:
    return """
PRIMARY TOOL — plan_query (use for almost every factual ask):
Emit a complete QuerySpec. Server executes BLINDLY — it will not rewrite your plan.
If you omit required fields you get plan_errors; fix and call plan_query again.

Required every time: intent, period_type
Also set: context_handling (none|prior), filters, group_by / column_dimension,
business_units, ranking_metric, sort_order, clear_filters when following up.

Examples:
- \"how Eva distributor sales in Lahore are doing last 6 months\" →
  intent=sales_matrix, period_type=LAST_N_MONTHS, months_back=6,
  filters={city:Lahore, client_type:Eva Distributors},
  business_units=[Eva Consumer, Eva Bulk]
- \"show me Eva sales in Lahore\" → sales_*, city=Lahore,
  business_units=[Eva Consumer, Eva Bulk], period_type=MTD (if no period spoken)
- After Eva Consumer vs Bulk: \"show this distributor-wise, lowest performing\" →
  context_handling=prior, intent=party_rank, group_by=party,
  clear_filters=[\"client_type\"], ranking_metric=vs_ams, sort_order=asc,
  keep business_units from prior
- \"least AMS gains\" → party_rank, ranking_metric=ams_growth, sort_order=asc
- \"growth vs other cities\" → prior, clear_filters=[\"city\"], group_by=city,
  ranking_metric=ams_growth
- \"who are Eva Distributors in Lahore\" → party_list + that channel

After tables: paste answer_markdown verbatim, then ### Analysis (2–4 bullets).
Numbers come only from executed tables.
""".strip()
