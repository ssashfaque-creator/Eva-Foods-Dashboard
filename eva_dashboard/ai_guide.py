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
        "- \"last N months\" TREND / month-wise → column_dimensions=[\"month\"].",
        "- \"last N months\" party list/rank / volume+growth cuts / vs same N",
        "  months last year → row_dimensions=[\"party\"], NO month columns",
        "  (one window). compare=\"yoy\" + metric=\"yoy\" for calendar YoY.",
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
        "10. CHANNEL vs CUSTOMER (STRICT)",
        "- metro / metro habib / chase up / CSD / LMT / IMT / Imtiaz /",
        "  distributors → filters.client_type (channel group). NEVER filters.party.",
        "- Real customer names (al shaheer, Alpha Dist, …) → filters.party",
        "  or extracted_entities; Python silent ILIKE (no clarify loops).",
        "- Compare two customers → filters.parties=[\"al shaheer\",\"Alpha Dist\"]",
        "  + row_dimensions=[\"party\"].",
        "- \"who is X\" → operation=party_lookup (match list).",
        "- \"tell me about X\" / customer profile / rundown → "
        "operation=party_profile (volume, AMS, % vs AMS, last purchase, rate).",
        "",
        "11. PERFORMANCE METRICS (see also GOVERNED METRICS block)",
        "- lowest/worst performing → metrics=[\"vs_ams\"], sort_order=asc,",
        "  row_dimensions=[\"party\"].",
        "- least/lowest gains → metrics=[\"ams_growth\"], sort_order=asc.",
        "- biggest gains → metrics=[\"ams_growth\"], sort_order=desc.",
        "- last N months vs the same N months last year / YoY of a spoken",
        "  window → metrics=[\"yoy\"], compare=\"yoy\" (NOT ams_growth).",
        "- Stacked cuts (volume > X AND growth < Y%) → metric_filters AND;",
        "  party rows; no month columns.",
        "- \"% of their AMS\" / \"percent of AMS\" → metrics=[\"vs_ams\"].",
        "- \"what's the price\" (no cost factor) → metrics=[\"avg_price\"].",
        "- Price Fetch / recovery → metrics=[\"price_fetch\"].",
        "",
        "12. COMPARE ASKS (teach — choose the grain of comparison)",
        "- Read what is being compared: channels, cities, packings, parties,",
        "  oils, BUs — that becomes row_dimensions (or advanced_query entities).",
        "- Shared scope stays in filters (city / packing / oil / BU) WITHOUT",
        "  locking the compare grain itself into a single filter value.",
        "- Channel vs channel (Imtiaz vs distributors, distributors vs LMT):",
        "  row_dimensions=[\"client_type\"], filters.client_types=[both sides],",
        "  metrics=[\"volume\",\"ams\"] (or [\"ams_growth\"] for growth).",
        "  Do NOT set filters.client_type to only one side — both must be rows.",
        "  Optional: advanced_query mode=compare_client_types with entities=[...].",
        "- City vs city / city and city (Lahore vs Karachi, Lahore and Karachi):",
        "  row_dimensions=[\"city\"], filters.cities=[\"Lahore\",\"Karachi\"].",
        "  Do NOT omit cities (that shows all Pakistan) and do NOT keep only",
        "  filters.city=Lahore. Shared channel/packing goes in filters.",
        "  Optional: advanced_query mode=compare_cities with entities=[...].",
        "- Follow-up \"product wise\" / packing / SKU on a city|zone|channel table:",
        "  keep prior filters + nest leaf under the prior outer grain",
        "  (e.g. row_dimensions=[\"city\",\"packing_category\"]).",
        "- Follow-up \"add cities\" / \"show by city\" / \"by channel\" on a",
        "  BU|packing|SKU table: nest as OUTER grain, keep the leaf",
        "  (row_dimensions=[\"city\",\"business_unit\"] or",
        "  [\"client_type\",\"business_unit\"]). clear sticky city/client_type.",
        "- Fresh \"sales by channel\" → row_dimensions=[\"client_type\"],",
        "  clear_filters include client_type. Channel rows use reporting groups",
        "  (IMT/LMT/…); \"metro\" filter stays raw METRO HABIB parties.",
        "- \"compare with Karachi\" after a Lahore table → filters.cities=",
        "  [Lahore,Karachi], row city (+ keep prior nestable leaf).",
        "- \"same period last year\" / \"same N months last year\" → compare=\"yoy\".",
        "  \"which products led the growth\" → packing/product rows + compare=yoy,",
        "  prior filters.",
        "- \"remove X\" → excludes.{dim}=[value], state_action=modify,",
        "  keep grain (do not drop the table shape).",
        "- Packing vs packing (Stand up vs LMT packing scope is different —",
        "  LMT is a channel): standup vs tin → row_dimensions=[\"packing_category\"].",
        "- Growth vs prior AMS window → metrics=[\"ams_growth\"].",
        "- Calendar YoY of last N months / a named month → metrics=[\"yoy\"],",
        "  compare=\"yoy\" (add volume). Do not mix these two growth definitions.",
        "- 3–4 way compares: same pattern; list all sides as rows / entities.",
        "- Party vs channel (al shaheer vs Imtiaz): mixed grain — either",
        "  (a) two plan_query calls (party filter for al shaheer; client_type",
        "  for Imtiaz) then compare in Analysis, or (b) row_dimensions=[\"party\"]",
        "  for the named party plus a second plan for the channel total.",
        "- Party vs party → filters.parties=[...] + row_dimensions=[\"party\"].",
        "- VTF / oil / packing scoped compares: put oil_type / packing_category",
        "  / business_units in filters, compare grain on rows.",
        "- Prefer one clear table that shows every side side-by-side; explain",
        "  gaps in ### Analysis. Never invent volumes.",
    ]
    return "\n".join(parts)


def tool_guide_for_prompt() -> str:
    return """
PRIMARY TOOL — plan_query (Universal Pivot) — ONLY analytics path:
Emit row_dimensions + column_dimensions + metrics + period_type +
state_action (keep|modify|clear) — legacy context_handling still accepted.
Do NOT pick rigid intents like sales_matrix vs sales_trend — describe the pivot.
Do NOT call query_sales / list_clients / analyze_parties / lookup_party /
advanced_query for analytics — they are disabled; server will reject them.
Server executes BLINDLY. plan_errors → fix and call plan_query again.
Customer follow-ups (price / % AMS / last purchase): state_action='keep'
(or context_handling='prior'),
clear_filters=[], keep filters.party from PRIOR_QUERY_CONTEXT.party_scope.

INVESTIGATION (Phase 4):
- Empty result plan_errors → widen period / clear_filters / fix entities, retry.
- Clarify markdown (multiple customers) → ask user to pick; do not invent.
- INVESTIGATION hint on a tool result → call plan_query again before Analysis.
- Party vs channel compare (al shaheer vs Imtiaz) → two plan_query calls.

Required: row_dimensions, metrics, period_type, context_handling
(prefer state_action=keep|modify|clear)
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
- \"sales for metro\" / \"metro habib sales\" → filters.client_type="METRO HABIB"
  (NOT filters.party — metro is a channel group)
- \"LMT sales\" → filters.client_type="LMT"
- \"compare al shaheer with Alpha Dist\" →
  filters.parties=["al shaheer","Alpha Dist"], row_dimensions=["party"],
  metrics=["volume","ams"], column_dimensions=["month"]
- \"compare Imtiaz vs distributors in Lahore\" →
  row_dimensions=["client_type"], column_dimensions=["month"],
  metrics=["volume","ams"],
  filters={city:Lahore, client_types:[Imtiaz Store, Eva Distributors]},
  LAST_N_MONTHS/6 — both channels as rows (not a single client_type lock)
- \"compare distributor sales and Imtiaz sales in Lahore\" → same pattern
- \"compare VTF growth in Imtiaz vs distributors in Lahore\" →
  row_dimensions=["client_type"], metrics=["ams_growth"],
  filters={city:Lahore, client_types:[Imtiaz Store, Eva Distributors],
  oil_type:…VTF…} (or extracted_entities=["VTF"]), LAST_N_MONTHS/6
- \"compare standup sales in distributors vs LMT\" →
  row_dimensions=["client_type"], metrics=["volume","ams"],
  filters={packing_category:Stand up,
  client_types:[Eva Distributors, LMT]}, LAST_N_MONTHS/6
- \"Imtiaz sales in Lahore vs Karachi\" / \"…Lahore and Karachi\" →
  row_dimensions=["city"], column_dimensions=["month"],
  metrics=["volume","ams"],
  filters={client_type:Imtiaz Store, cities:[Lahore, Karachi]},
  LAST_N_MONTHS/6 — only those cities as rows
- \"compare Imtiaz sales growth in Lahore vs Karachi\" →
  row_dimensions=["city"], metrics=["ams_growth"],
  filters={client_type:Imtiaz Store, cities:[Lahore, Karachi]},
  LAST_N_MONTHS/6
- After a city table: \"show this product wise\" →
  context_handling=prior, row_dimensions=["city","packing_category"],
  keep filters.cities / client_type from prior
- \"compare al shaheer growth with Imtiaz\" →
  plan_query #1: filters.party/extracted_entities=["al shaheer"],
  metrics=["ams_growth"]; plan_query #2: filters.client_type=Imtiaz Store,
  metrics=["ams_growth"]; then compare both tables in Analysis
- \"who is al shaheer\" → operation=party_lookup, party_query="al shaheer"
- \"tell me about Alpha Dist\" / \"customer profile for al shaheer\" /
  \"give me a rundown on Alpha Dist\" → operation=party_profile,
  party_query or filters.party / extracted_entities, period_type=MTD
  (or SPECIFIC_MONTH). Returns volume, AMS, % vs AMS, last purchase,
  avg rate, top SKUs — then follow up with price / SKU-wise via prior.
- After brand table: \"distributor-wise, lowest performing\" →
  context_handling=prior, row_dimensions=["party"], metrics=["vs_ams"],
  sort_order=asc, clear_filters=["client_type"]
- \"who are Eva Distributors in Lahore\" →
  operation=party_list, row_dimensions=["party"], metrics=["volume"],
  filters={client_type:Eva Distributors, city:Lahore}

After tables: paste answer_markdown verbatim, then ### Analysis (2–4 bullets).
""".strip()
