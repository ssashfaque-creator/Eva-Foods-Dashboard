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
        "VOCABULARY (YOU translate spoken words → Universal Pivot fields):",
        "",
        "1. BRANDS → business_units (NEVER client_type) — STRICT",
        "- Eva → business_units=[\"Eva Consumer\", \"Eva Bulk\"] (BOTH).",
        "- Maan → business_units=[\"Maan Consumer\", \"Maan Bulk\"] (BOTH).",
        "- Consumer (alone) → business_units=[\"Eva Consumer\"].",
        "- \"Eva Consumer\" / \"Eva Bulk\" are BUSINESS UNITS (category_1).",
        "- Putting Eva Consumer in client_type is INVALID and will be rejected.",
        "- If unsure which column a brand/product belongs in → extracted_entities.",
        "",
        "2. PARTY / CUSTOMER → row_dimensions=['party'] (STRICT)",
        "- customer, customer-wise, party, party-wise, account, buyer, store,",
        "  distributor-wise, by distributor → ALWAYS row_dimensions=[\"party\"].",
        "- Do NOT invent filters.client_type unless a channel is named.",
        "- \"distributor sales\" / \"who are distributors\" → filters.client_type="
        "\"Eva Distributors\" (channel filter) — different from distributor-wise.",
        "- \"Eva distributor sales\" → business_units=[Eva Consumer, Eva Bulk] AND "
        "client_type=Eva Distributors.",
        "",
        "3. CHANNEL TYPES (client_type enum ONLY — never Business Units):",
        *_group(CLIENT_TYPE_ALIASES),
        "",
        "4. PRODUCT vs SKU (UNBREAKABLE)",
        "- Spoken \"product\" / \"product-wise\" / \"by product\" →",
        "  row_dimensions=[\"packing_category\"] (packing category, NOT SKU name).",
        "- Spoken \"SKU\" / \"SKU-wise\" / \"SKU breakup\" / \"item-wise\" →",
        "  row_dimensions=[\"product\"] (actual product / SKU name). ALWAYS.",
        "- \"canola standup\" → filters={oil_type:\"Eva Canola\", ",
        "packing_category:\"Stand up\"} (AND). Do NOT broaden to all Eva Consumer.",
        "",
        "5. PACKING:",
        *_group(PACKING_ALIASES),
        "",
        "6. OIL TYPES:",
        *_group(OIL_TYPE_ALIASES),
        "",
        "7. GEOGRAPHY",
        "- city / city_filter ← City-Filter; zone ONLY when named.",
        "- Do NOT invent zones for unrecognized cities (engine → Karachi/SOUTH).",
        "- nationally / other cities → clear_filters:[\"city\"] (+ zone if set).",
        "",
        "8. PERIOD + TREND DEFAULT",
        "- Sales with NO period → period_type=LAST_N_MONTHS, months_back=6,",
        "  column_dimensions=[\"month\"], row_dimensions=[\"business_unit\"],",
        "  metrics=[\"volume\",\"ams\"].",
        "- \"last N months\" → MUST set column_dimensions=[\"month\"].",
        "- \"this month\"/MTD/so far → period_type=MTD.",
        "- SINGLE month ('March', 'March 2026') → period_type=SPECIFIC_MONTH,",
        "  target_month=YYYY-MM. DO NOT use LAST_N_MONTHS. DO NOT put 'month'",
        "  in column_dimensions unless the user asked for a multi-month trend.",
        "",
        "9. PRICE / PRICE FETCH (STRICT — engine owns the math)",
        "- plain rate / average price → metrics=[\"avg_price\"].",
        "- Price Fetch / recovery / 'oil price fetched' / 'apply the cost factor'",
        "  / 'what's the cost factor' → metrics=[\"price_fetch\"].",
        "  Engine returns a dedicated table: row dims + Avg Price (Incl GST/unit)",
        "  + Cost Factor + Price Fetch/Maund. Do NOT use monthly trend for this.",
        "- SKU-wise Price Fetch → row_dimensions=[\"product\"], metrics=[\"price_fetch\"].",
        "- monthly price trends → column_dimensions=[\"month\"] + avg_price.",
        "- customer-wise price trends → row_dimensions=[\"party\"],",
        "  column_dimensions=[\"month\"], metrics=[\"avg_price\"].",
        "",
        "10. PARTY / CUSTOMER NAMES (silent ILIKE — no retry loops)",
        "- Put the spoken name in filters.party (e.g. \"al shaheer\") OR",
        "  extracted_entities=[\"al shaheer\"].",
        "- Compare two customers → filters.parties=[\"al shaheer\",\"Metro Habib\"]",
        "  (or extracted_entities with both names) + row_dimensions=[\"party\"].",
        "- Python applies SQL ILIKE '%name%' — do NOT ask the user to pick",
        "  among Al Shaheer branches for analytics. \"who is X\" →",
        "  operation=party_lookup (shows the match list).",
        "",
        "11. PERFORMANCE METRICS",
        "- lowest/worst performing → metrics=[\"vs_ams\"], sort_order=asc,",
        "  row_dimensions=[\"party\"].",
        "- least/lowest gains → metrics=[\"ams_growth\"], sort_order=asc.",
        "- biggest gains → metrics=[\"ams_growth\"], sort_order=desc.",
    ]
    return "\n".join(parts)


def tool_guide_for_prompt() -> str:
    return """
PRIMARY TOOL — plan_query (Universal Pivot):
Emit row_dimensions + column_dimensions + metrics + period_type + context_handling.
Do NOT pick rigid intents like sales_matrix vs sales_trend — describe the pivot.
Server executes BLINDLY. plan_errors → fix and call plan_query again.

Required: row_dimensions, metrics, period_type, context_handling
Optional: column_dimensions, filters, months_back, clear_filters, operation,
sort_order, business_units, extracted_entities, party_query, price_flags.

FILTER CONTRACT (Enterprise):
- business_units / client_type / oil_type / packing_category are STRICT enums.
- Eva Consumer belongs in business_units — NEVER client_type.
- If unsure → extracted_entities=["Eva Consumer"] and let Python place it.
- plan_errors mean Validation failed — fix the field and call plan_query again.

Examples:
- \"customer-wise price trends last 6 months\" →
  row_dimensions=["party"], column_dimensions=["month"], metrics=["avg_price"],
  period_type=LAST_N_MONTHS, months_back=6
- \"Eva Consumer sales in Lahore last 6 months\" →
  business_units=["Eva Consumer"], filters={city:Lahore},
  row_dimensions=["business_unit"], column_dimensions=["month"],
  metrics=["volume","ams"]  # NOT client_type=Eva Consumer
- \"how Eva distributor sales in Lahore are doing last 6 months\" →
  row_dimensions=["business_unit"], column_dimensions=["month"],
  metrics=["volume","ams"], filters={city:Lahore, client_type:Eva Distributors},
  business_units=[Eva Consumer, Eva Bulk], LAST_N_MONTHS/6
- \"show me Eva sales in Lahore\" (no period) → same trend default (BU×Month+AMS)
- \"channel monthly table\" →
  row_dimensions=["client_type","business_unit"], column_dimensions=["month"],
  metrics=["volume","ams"], LAST_N_MONTHS/6
- \"Eva canola standup sales last 6 months\" →
  filters={oil_type:Eva Canola, packing_category:Stand up},
  row_dimensions=["business_unit"], column_dimensions=["month"],
  metrics=["volume","ams"]
- \"monthly average price for Eva canola standup\" →
  metrics=["avg_price"], column_dimensions=["month"], LAST_N_MONTHS/6,
  filters={oil_type:Eva Canola, packing_category:Stand up}
- \"March Eva sales in Lahore\" →
  period_type=SPECIFIC_MONTH, target_month=2026-03 (anchor year to live data),
  business_units=[Eva Consumer, Eva Bulk], filters={city:Lahore},
  row_dimensions=["business_unit"], metrics=["volume","ams"]
  — NO column_dimensions month, NO LAST_N_MONTHS
- \"Price Fetch for canola standup Distributors\" / \"oil price fetched\" /
  \"apply the cost factor\" → metrics=["price_fetch"],
  filters={oil_type:Eva Canola, packing_category:Stand up,
  client_type:Eva Distributors}
- \"SKU-wise breakup of al shaheer with average prices and the price fetch\" →
  row_dimensions=["product"], metrics=["price_fetch"],
  filters.party="al shaheer" (or extracted_entities=["al shaheer"]),
  LAST_N_MONTHS/6 — NO column_dimensions month
- \"product-wise sales\" → row_dimensions=["packing_category"] (NOT product)
- \"al shaheer sales last 6 months\" → filters.party="al shaheer"
  (Python ILIKE — includes all Al Shaheer branches; no clarify loop)
- \"compare al shaheer with Metro Habib\" →
  filters.parties=["al shaheer","Metro Habib"], row_dimensions=["party"],
  metrics=["volume","ams"], column_dimensions=["month"]
- \"who is al shaheer\" → operation=party_lookup, party_query="al shaheer"
- After brand table: \"distributor-wise, lowest performing\" →
  context_handling=prior, row_dimensions=["party"], metrics=["vs_ams"],
  sort_order=asc, clear_filters=["client_type"]
- \"who are Eva Distributors in Lahore\" →
  operation=party_list, row_dimensions=["party"], metrics=["volume"],
  filters={client_type:Eva Distributors, city:Lahore}

After tables: paste answer_markdown verbatim, then ### Analysis (2–4 bullets).
""".strip()
