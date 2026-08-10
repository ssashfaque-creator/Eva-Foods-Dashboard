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
        "Client / channel types:",
        *_group(CLIENT_TYPE_ALIASES),
        "",
        "Packing categories (spoken \"product\" usually means packing, not SKU):",
        *_group(PACKING_ALIASES),
        "",
        "Oil types:",
        *_group(OIL_TYPE_ALIASES),
        "",
        "Business units (set business_unit / business_units):",
        "- Eva Consumer, Eva Bulk, Maan Consumer, Maan Bulk, Cusine King, …",
        "- Bare \"Eva sales\" → both Eva Consumer + Eva Bulk",
        "- Bare \"Maan sales\" → both Maan Consumer + Maan Bulk",
        "- \"selling maan\" (who buys) → Maan Consumer parties, not a BU sales matrix",
        "",
        "Geography:",
        "- city ← City-Filter on clients (Karachi, Lahore, …)",
        "- zone ← SOUTH | CENTRAL | NORTH (mapped from city)",
        "- blank/unmapped city → treat as Karachi → SOUTH",
        "- \"nationally\" / \"all over Pakistan\" → clear city + zone",
        "",
        "Metrics language (analyze_parties.metric + sort):",
        "- AMS / average monthly sales → ams or ams_growth",
        "- least / lowest / smallest / bottom gains or growth → sort=asc "
        "(do NOT set grown_only)",
        "- biggest / highest / top gains → sort=desc",
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
    ]
    return "\n".join(parts)


def tool_guide_for_prompt() -> str:
    return """
TOOLS — you choose which to call and with which arguments:

1) query_sales — volume pivots / month grids / channel×month / BU×packing.
   Set filters (city, zone, client_type, business_unit(s), oil_type, packing),
   row_dimension, columns (client_type|city|month), months_back, mode
   (matrix|analytical|trend). Use for \"show sales\", channel-wise, city sales,
   brand sales, include/remove/regroup follow-ups on a prior table.

2) analyze_parties — party/city rankings and AMS/growth.
   Set metric (ams, ams_growth, yoy, vs_ams, packing_mix, …), sort (asc|desc),
   grown_only / declined_only when the user asks to filter growers/decliners,
   city/client_type/oil filters, limit. Use for least/biggest gains, top parties,
   decline in AMS, product mix per distributor.

3) list_clients — who are the parties in a channel/city/BU (identity list).
   Not for rankings or growth tables.

4) lookup_party — one named party profile / that party's sales.
   Use when a real distributor/store name is the subject (not \"Eva\" / \"Maan\" brands).

5) query_price — average rate, Price Fetch, cost factor, packing cost.

6) advanced_query — multi-city/client compares, dumping (volume excess),
   same-date price differences (mode=price_dispersion), expected month,
   filter entities (sales > N MT, declined > N%).

7) get_sales_overview / report_snapshot — what data is loaded / daily briefing.

8) resolve_product_language → then product_sales for a single spoken SKU.

YOU decide tool + arguments from the user's intent. Prefer one tool call.
Numbers come only from tool tables — paste answer_markdown verbatim, then Analysis.
""".strip()
