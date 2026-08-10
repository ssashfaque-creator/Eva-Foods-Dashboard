"""Teaching material for the chatbot model — data model + vocabulary.

This is guidance for the LLM, not hard routing. The model chooses tools and
arguments; tools compute tables deterministically.
"""

from __future__ import annotations

from eva_dashboard.client_language import (
    CLIENT_TYPE_ALIASES,
    OIL_TYPE_ALIASES,
    PACKING_ALIASES,
)


def vocabulary_for_prompt() -> str:
    """Compact spoken → canonical dictionary for the system prompt."""
    # Deduplicate aliases → one line per canonical target
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
        "VOCABULARY (spoken words → system values). Use these when setting tool args:",
        "",
        "Client / channel types (ONLY when the user names a channel):",
        *_group(CLIENT_TYPE_ALIASES),
        "",
        "CRITICAL — distributor grain vs channel:",
        "- \"distributor-wise\" / \"by distributor\" / \"party-wise\" / "
        "\"lowest performing distributors\" → grain.group_by=party. "
        "Do NOT set filters.client_type=Eva Distributors.",
        "- Set Eva Distributors ONLY when they name that channel "
        "(\"Eva Distributors\", \"distributor sales\" as a channel ask).",
        "- After an Eva Consumer vs Eva Bulk table, \"show this distributor "
        "wise…\" → base=prior, keep business_units, clear client_type, "
        "party_rank by party.",
        "",
        "Packing categories (spoken \"product\" usually means packing, not SKU):",
        *_group(PACKING_ALIASES),
        "",
        "Oil types:",
        *_group(OIL_TYPE_ALIASES),
        "",
        "Business units (ALWAYS set business_unit / business_units in plan_query):",
        "- Eva → Eva Consumer + Eva Bulk (BOTH). Never include Shortening, Meal, etc.",
        "- Maan → Maan Consumer + Maan Bulk (BOTH).",
        "- Consumer (alone) → Eva Consumer only.",
        "- Bulk (alone, Eva context) → Eva Bulk; \"Maan bulk\" → Maan Bulk.",
        "- Eva Consumer, Eva Bulk, Maan Consumer, Maan Bulk, Cusine King are full names.",
        "- \"selling maan\" (who buys) → Maan Consumer parties, not a BU sales matrix.",
        "",
        "Geography:",
        "- city ← City-Filter on clients (Karachi, Lahore, …)",
        "- zone ← SOUTH | CENTRAL | NORTH (mapped from city)",
        "- blank/unmapped city → treat as Karachi → SOUTH",
        "- \"nationally\" / \"all over Pakistan\" → clear city + zone",
        "- \"other cities\" / \"compared to other cities\" / city league → "
        "group_by=city and CLEAR city filter (do not keep sticky Lahore)",
        "- \"other zones\" / by zone → group_by=zone and clear city",
        "",
        "Metrics language (analyze_parties.metric + sort):",
        "- AMS = mean MT of the 3 full months BEFORE the report month "
        "(not the same window as Volume). Parties with Volume but AMS=0 "
        "have no baseline — do not call them \"lowest AMS\".",
        "- \"lowest / worst performing\" distributors → metric=vs_ams, "
        "sort=asc, title_mode=underperformers (behind their AMS).",
        "- least / lowest / smallest / bottom gains or growth → "
        "metric=ams_growth, sort=asc (do NOT set grown_only)",
        "- biggest / highest / top gains → sort=desc + grown_only only if "
        "they asked for growers",
        "- \"this growth\" / \"how is this compared…\" → keep prior metric "
        "(usually ams_growth) and reshape grain/geography",
        "- only growing / that have grown → grown_only=true",
        "- declined / dropped / fallen → declined_only=true, sort=asc",
        "- vs AMS / behind on AMS → vs_ams",
        "- YoY / last year volume → yoy or yoy_ams",
        "- invoices / invoice size → invoices / invoice_mt",
        "",
        "Table shape language (query_sales):",
        "- channel / client type wise → row_dimension=client_type",
        "- city wise → row_dimension=city",
        "- zone wise → row_dimension=zone",
        "- product wise / by product → row_dimension=packing_category "
        "(SKU only if user says SKU)",
        "- month wise / last N months → columns=month",
        "- named month (\"for July\") → that month Volume + AMS + % "
        "(mode=trend is fine)",
        "- how are / performance → analytical tone is OK; still use a tool",
        "",
        "Follow-up reshape (analyze_parties):",
        "- \"show this … wise\" / \"this distributor wise\" → base=prior, "
        "keep prior business_units / brand scope",
        "- Keep channel/oil/packing from the prior answer when user says "
        "\"this growth\" / \"compared to…\" — but CLEAR client_type when "
        "they ask distributor-/party-wise grain",
        "- Change group_by when they ask for cities/zones vs parties",
        "- Expanding geography clears sticky city — never rank cities "
        "while filtered to one city",
    ]
    return "\n".join(parts)


def tool_guide_for_prompt() -> str:
    return """
PRIMARY TOOL — plan_query (use this for almost every factual ask):
Emit a QuerySpec JSON. The server executes it. Key fields:
  intent: sales_matrix|sales_trend|sales_analytical|party_rank|party_list|
          party_lookup|price|advanced|overview
  base: none (fresh) | prior (follow-up — start from PRIOR_QUERY_CONTEXT)
  clear: filter keys to drop from prior (e.g. [\"city\"] for other cities)
  filters / grain / metric / sort / grown_only / declined_only / title_mode

Examples:
- \"least AMS gains\" → party_rank, metric=ams_growth, sort=asc,
  grown_only=false, title_mode=smallest_gains
- \"growth vs other cities\" (after Lahore party growth) → base=prior,
  clear=[\"city\"], grain.group_by=city, metric=ams_growth, title_mode=by_growth
- \"show me Eva sales in Lahore\" → sales_*, city=Lahore,
  business_units=[\"Eva Consumer\", \"Eva Bulk\"]  ← Eva means both; not Shortening
- \"Maan sales\" → business_units=[\"Maan Consumer\", \"Maan Bulk\"]
- \"Consumer sales\" → business_unit=\"Eva Consumer\"
- After Eva Consumer vs Eva Bulk: \"show this distributor wise, lowest
  performing distributors\" → base=prior, intent=party_rank,
  business_units=[Eva Consumer, Eva Bulk], clear=[\"client_type\"],
  grain.group_by=party, metric=vs_ams, sort=asc, title_mode=underperformers
  (NOT filters.client_type=Eva Distributors; NOT metric=ams \"Top parties\")
- \"who are Eva Distributors in Lahore\" → party_list + that channel
- named store/distributor sales → party_lookup

Legacy tools (query_sales, analyze_parties, …) still exist but prefer plan_query.
Numbers come only from executed tables — paste answer_markdown, then Analysis.
""".strip()
