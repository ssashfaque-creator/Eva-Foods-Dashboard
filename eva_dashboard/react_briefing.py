"""Compact commercial briefing for the ReAct chat loop.

The deprecated plan_query path injects the full system prompt (live DB,
vocabulary, metrics, product glossary). ReAct used only a 25-line tool
primer — so gpt-4o guessed QuerySpecs without date range, Eva=both BUs,
or party_profile vs who-is. This module is the missing teaching block.
"""

from __future__ import annotations


def react_queryspec_contract() -> str:
    """Field contract for run_standard_analytics_pivot.spec_dict."""
    return """
=== QUERY SPEC (run_standard_analytics_pivot.spec_dict) ===
Whenever other teaching text says plan_query / query_sales, call
run_standard_analytics_pivot with the same spec_dict — those old tool names
are not available on this ReAct path.
operations: pivot | party_list | party_lookup | party_profile | overview | advanced
row_dimensions: city, zone, party, business_unit, packing_category, product, oil_type, client_type
column_dimensions: month, client_type, business_unit, city, oil_type, packing_category
metrics: volume, avg_price, last_price, price_fetch, ams, vs_ams, ams_growth, yoy, yoy_ams
period_type: MTD | LAST_N_MONTHS | LAST_MONTH | LAST_WEEK | NAMED_MONTH | SPECIFIC_MONTH | CUSTOM_DATE
filters: city, cities, zone, client_type, client_types, oil_type, packing_category, party, parties, party_ilike, product
excludes: party_like, client_type, business_unit (EXCLUDE only — never put excluded names in filters)
state_action: keep | modify | clear   (follow-up vs fresh ask)
compare: yoy = calendar YoY of the spoken window (same span last year)
metric_filters: [{metric, op, value}] stacked with AND
  ('sales more than 10 MT but less than 5% growth' → two cuts)

TREND DEFAULT (no period spoken): LAST_N_MONTHS months_back=6, rows=business_unit, cols=month, metrics=[volume,ams].
Named month (March / July 2026) → SPECIFIC_MONTH + target_month=YYYY-MM (anchor year to LIVE DATABASE), no month columns unless they asked month-wise.
Party list/rank / stacked metric cuts over last N months → LAST_N_MONTHS + months_back=N, row_dimensions=['party'], NO month columns (one window, not a month grid).
Lowest/highest/least growth last N months vs the same months last year → metric='yoy', compare='yoy', sort=asc (lowest) or desc (highest). Never ams_growth for that. ams_growth is DIFFERENT: current AMS window vs the previous AMS window.
Complete new analytical ask (own period + cuts) → state_action='clear' (do not keep last-12-months memory).
'show all matching' / 'the all distributors' → limit=200.
Named customer INCLUDE → filters.party (or extracted_entities) + rows=party.
who is X (identity only) → operation=party_lookup.
tell me about X / customer rundown / last purchase → operation=party_profile.
Eva (brand, not a client type) → business_units=["Eva Consumer","Eva Bulk"]. Maan → Maan Consumer + Maan Bulk.
Channels (Imtiaz, distributors, Metro, LMT, IMT) → filters.client_type — NEVER filters.party.
product-wise / by product → packing_category. SKU-wise / item-wise → product.
City = clients.city_filter (never clients.city). Zone = SOUTH/CENTRAL/NORTH.
AMS = mean of the 3 full calendar months BEFORE the focus month — engine owns the window.
Effective MT: mt_qty if nonzero else kg/1000 else ton qty. Prefer mt_qty in SQL; never SUM(qty) as volume.
Price Fetch = (Incl GST/FED per kg − cost factor per kg) × 37.3246 (Ltrs cost ÷ 0.915). NEVER invent 37.3246/AMS in SQL.
Joins: sales.party ↔ clients.client (normalize); sales.product ↔ category.product (BU/oil/packing).
""".strip()


def react_commercial_briefing() -> str:
    """Live DB + QuerySpec + vocabulary + metrics + product language for ReAct."""
    from eva_dashboard.ai_guide import vocabulary_for_prompt
    from eva_dashboard.chatbot import live_database_briefing
    from eva_dashboard.metrics_catalog import metrics_for_prompt
    from eva_dashboard.product_language import glossary_for_prompt
    from eva_dashboard.spoken_constraints import polarity_brief_for_prompt

    parts = [
        live_database_briefing(),
        react_queryspec_contract(),
        polarity_brief_for_prompt(),
        vocabulary_for_prompt(),
        metrics_for_prompt(),
        glossary_for_prompt(),
    ]
    return "\n\n".join(p.strip() for p in parts if (p or "").strip())
