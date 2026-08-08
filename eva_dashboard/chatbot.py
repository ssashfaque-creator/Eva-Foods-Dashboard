"""OpenAI-powered chatbot with read-only access to Eva Foods SQLite data."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from eva_dashboard.client_language import (
    extract_all_client_types_from_text,
    extract_client_type_from_text,
    extract_oil_type_from_text,
    extract_packing_from_text,
    lookup_party,
    normalize_client_type,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.advanced_routing import infer_advanced_from_text, looks_advanced
from eva_dashboard.party_analytics import (
    analyze_parties,
    extract_city_from_text,
    infer_party_analytics_from_text,
    list_clients,
    party_sales,
)
from eva_dashboard.paths import data_root, db_path
from eva_dashboard.product_language import (
    glossary_for_prompt,
    product_sales,
    resolve_product_language,
)
from eva_dashboard.sales_query import (
    check_segment_inclusion,
    normalize_row_dimension,
    query_factor_costs,
    query_price,
    query_sales,
)
from eva_dashboard.seasonality import expected_month_close

DEFAULT_MODEL = "gpt-4o-mini"
MAX_SQL_ROWS = 200
MAX_TOOL_ROUNDS = 4  # Prefer one structured query_sales call over many SQL rounds
# Hard cap so a stalled OpenAI call cannot sit for the SDK default (10 minutes).
OPENAI_TIMEOUT_S = 45.0
MAX_API_HISTORY_MESSAGES = 12  # recent user/assistant turns only
MAX_API_ASSISTANT_CHARS = 1600  # drop huge HTML tables from prior turns

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|"
    r"PRAGMA|VACUUM|REINDEX|GRANT|REVOKE|TRUNCATE|INTO)\b",
    re.IGNORECASE,
)

_CATALOG_CANDIDATES = (
    Path(__file__).resolve().parent / "data_catalog.md",
    Path(__file__).resolve().parents[1] / "docs" / "DATA_CATALOG.md",
)


def load_data_catalog() -> str:
    for path in _CATALOG_CANDIDATES:
        if path.exists():
            return path.read_text(encoding="utf-8")
    return "Data catalog file missing. Use schema tool and inspect tables."


def resolve_api_key(explicit: str | None = None) -> str | None:
    key = (explicit or "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()
    return key or None


def sales_overview() -> dict[str, Any]:
    init_db()
    with connect() as conn:
        sales_n = conn.execute("SELECT COUNT(*) AS n FROM sales").fetchone()["n"]
        cats = conn.execute("SELECT COUNT(*) AS n FROM category").fetchone()["n"]
        clients = conn.execute("SELECT COUNT(*) AS n FROM clients").fetchone()["n"]
        factors = conn.execute("SELECT COUNT(*) AS n FROM factor_costs").fetchone()["n"]
        dates = conn.execute(
            "SELECT MIN(date) AS min_d, MAX(date) AS max_d, COUNT(DISTINCT date) AS n_days "
            "FROM sales WHERE date IS NOT NULL"
        ).fetchone()
        months = conn.execute(
            """
            SELECT substr(date, 1, 7) AS month,
                   COUNT(*) AS lines,
                   ROUND(SUM(COALESCE(mt_qty, 0)), 3) AS mt_sum
            FROM sales
            WHERE date IS NOT NULL
            GROUP BY substr(date, 1, 7)
            ORDER BY month
            """
        ).fetchall()
        categories = conn.execute(
            """
            SELECT category_1 AS business_unit, COUNT(*) AS products
            FROM category
            GROUP BY category_1
            ORDER BY category_1
            """
        ).fetchall()
        oil_types = conn.execute(
            """
            SELECT category_2 AS oil_type, COUNT(*) AS products
            FROM category
            WHERE category_2 IS NOT NULL AND trim(category_2) != ''
            GROUP BY category_2
            ORDER BY products DESC, category_2
            LIMIT 25
            """
        ).fetchall()
        packing = conn.execute(
            """
            SELECT packing_category, COUNT(*) AS products
            FROM category
            WHERE packing_category IS NOT NULL AND trim(packing_category) != ''
            GROUP BY packing_category
            ORDER BY products DESC, packing_category
            LIMIT 25
            """
        ).fetchall()
        client_types = conn.execute(
            """
            SELECT type, COUNT(*) AS n
            FROM clients
            WHERE type IS NOT NULL AND trim(type) != ''
            GROUP BY type
            ORDER BY n DESC
            LIMIT 20
            """
        ).fetchall()
        recent = conn.execute(
            """
            SELECT date, COUNT(*) AS lines,
                   ROUND(SUM(COALESCE(mt_qty,0)), 3) AS mt_sum
            FROM sales
            WHERE date IS NOT NULL
            GROUP BY date
            ORDER BY date DESC
            LIMIT 15
            """
        ).fetchall()
        sample_cities = conn.execute(
            """
            SELECT city_filter AS city, COUNT(*) AS clients
            FROM clients
            WHERE city_filter IS NOT NULL
              AND trim(city_filter) != ''
              AND lower(trim(city_filter)) != 'undefined'
            GROUP BY city_filter
            ORDER BY clients DESC
            LIMIT 15
            """
        ).fetchall()
    return {
        "sales_rows": sales_n,
        "products_in_category_map": cats,
        "clients": clients,
        "factor_cost_rows": factors,
        "sales_date_min": dates["min_d"],
        "sales_date_max": dates["max_d"],
        "distinct_sales_days": dates["n_days"],
        "months_available": [dict(r) for r in months],
        "categories": [dict(r) for r in categories],
        "business_units": [dict(r) for r in categories],
        "oil_types": [dict(r) for r in oil_types],
        "packing_categories": [dict(r) for r in packing],
        "client_types": [dict(r) for r in client_types],
        "top_cities": [dict(r) for r in sample_cities],
        "recent_days": [dict(r) for r in recent],
        "database_path": str(db_path()),
        "data_root": str(data_root()),
    }


def live_database_briefing() -> str:
    """Human-readable snapshot injected into the system prompt every turn."""
    try:
        ov = sales_overview()
    except Exception as exc:  # noqa: BLE001
        return f"LIVE DATABASE: unavailable ({exc})"

    months = ", ".join(
        f"{m['month']} ({m['mt_sum']} MT)" for m in ov.get("months_available") or []
    ) or "none"
    cats = ", ".join(
        f"{c.get('business_unit') or c.get('category_1')}×{c['products']}"
        for c in ov.get("business_units") or ov.get("categories") or []
    ) or "none"
    oils = ", ".join(
        f"{c['oil_type']}×{c['products']}" for c in (ov.get("oil_types") or [])[:12]
    ) or "none"
    packs = ", ".join(
        f"{c['packing_category']}×{c['products']}"
        for c in (ov.get("packing_categories") or [])[:12]
    ) or "none"
    cities = ", ".join(
        f"{c['city']}" for c in (ov.get("top_cities") or [])[:10]
    ) or "none"

    return f"""LIVE DATABASE STATE (authoritative — trust this over any other date knowledge):
- Database file: {ov['database_path']}
- Sales rows: {ov['sales_rows']:,}
- Sales date range: {ov['sales_date_min'] or 'n/a'} → {ov['sales_date_max'] or 'n/a'}
- Distinct sales days: {ov['distinct_sales_days']}
- Months present: {months}
- Category map products: {ov['products_in_category_map']:,}
- Business Units: {cats}
- Oil Types (sample): {oils}
- Packing Categories (sample): {packs}
- Clients: {ov['clients']:,}
- Factor cost rows: {ov['factor_cost_rows']:,}
- Example cities (City-Filter): {cities}

If the user asks about July 2026 (or any date inside the range above), QUERY THE DATABASE.
Never say the data "only goes to 2023" or cite an OpenAI knowledge cutoff for this app.
"""


def system_prompt() -> str:
    catalog = load_data_catalog()
    live = live_database_briefing()
    glossary = glossary_for_prompt()
    return f"""You are the Eva Foods in-app data analyst. Answer ONLY from the live SQLite database.

{live}

SPEED & TOOL RULES (v0.4.7):
1. MUST call a tool before any numbers. Prefer ONE primary tool. Never invent figures or cite an OpenAI knowledge cutoff.
2. Choose the tool yourself. Do NOT call get_schema / run_sql for normal pivots.
3. Geography = City-Filter (`city`). Always use the period label returned by the tool.
4. Read-only. Paste tool `answer_markdown` tables verbatim.
5. ### Analysis is written by you from the tool table (quality commercial insight) —
   never invent numbers; never paste generic canned filler.

DATA MODEL (filters you set; tools build tables):
- Product hierarchy: Business Unit → Oil Type → Packing Category → Product SKU.
- Client hierarchy: Client Type → Party (named client). City = City-Filter on clients.
- Channel = Client Type (trade channel). "Which channels grew/declined" → client_type
  rows with Volume + AMS + % vs AMS (not packing, not party list).
- Client-type aliases (set `client_type`, never invent a Business Unit for these):
  Imtiaz/store → Imtiaz Store; distributors → Eva Distributors; else exact live type
  (Chase Up, Metro, CSD, SPAR, Food Panda, Gelani, LMT, …).
- which/what/who + ANY client type (Distributors, Imtiaz, Metro, Chase Up, CSD,
  SPAR, Food Panda, Gelani, Online, LMT, …) → individual parties in that type
  (list_clients or analyze_parties). Inherit prior city/type/period.
  Examples: "which distributor is selling Maan"; "what Metro sells the most VTF";
  "which Chase Up is active in Lahore"; "who are the CSD stores".
- Named party / "who is X?" → lookup_party (not a client-type filter).
- "Who/list/individual distributors" → list_clients. "Distributor sales" → query_sales.
- Which distributors grew / vs AMS / vs last year (VTF etc.) → analyze_parties with
  YoY % (+ AMS columns when both asked). Never a packing matrix or bare MT list.

TOOL CHOICE:
- Volume pivots / month grids / regroup / include-bulk / remove / YoY on a table → query_sales
- Party rankings, growth, AMS, share, mix, new/lost/silent → analyze_parties
- Rate / price / Price Fetch / cost factor / packing cost / factor breakdown → query_price
- City/client compares (2+ sides: Lahore vs Karachi vs Islamabad; Imtiaz vs Metro
  vs Chase Up) → advanced_query with mode compare_cities/compare_client_types and
  `entities` for all sides. Dumping, expected month, filter grown/declined/>N MT
  → advanced_query too.
- Single spoken product → resolve_product_language then product_sales / filtered query
- Daily briefing → report_snapshot; what's loaded? → get_sales_overview

DEFAULTS (tools also enforce these):
- "Show me X sales" (party / client type / city) with NO named month → columns='month',
  months_back=6 + AMS (3) + AMS (6).
- Named month in the ask ("for July", "this month") → that month's Volume + AMS + % vs AMS
  only (not a 6-month grid). Explicit "last N months" / "month-wise" keeps the grid.
- "Sold to" / "which distributor bought [BU]" → list_clients with that business_unit,
  inheriting prior city / client type / period when this is a follow-up.
- Matrix mode: what/show/breakdown/average → matrix; how/performance/doing/trend → analytical.
- Row grain (query_sales): no BU → Business Unit; one BU → Packing Category (+ BU subtotal);
  oil set → Packing; packing set → Product SKU.
  Spoken "product" / "product wise" / "by product" → Packing Category (never SKU).
  Only "SKU" / "SKU wise" / "break it down further" (from packing) → individual SKU.
- Columns: client_type | city | month. Tables include row + column totals.
- [FOLLOW-UP …] / Reply: reuse that answer's filters via prior_spec (same grain unless asked
  to regroup, drill, remove, include/combine, list individuals, sold-to, or YoY-compare).

RESPONSE FORMAT:
- Start with the tool's `answer_markdown` tables verbatim (keep column order).
- Then ### Analysis: 2–3 one-line bullets from that table only (no bold labels).
- No metric bullet lists outside ### Analysis.

=== PRODUCT LANGUAGE (abbrev) ===
{glossary}

=== DATA CATALOG ===
{catalog}
"""


_ANALYSIS_SECTION_RE = re.compile(
    r"\n*###\s*Analysis\s*\n(.*?)(?=\n###\s|\Z)",
    flags=re.IGNORECASE | re.DOTALL,
)


def _strip_analysis_section(md: str) -> str:
    """Keep context + tables; drop a trailing ### Analysis block."""
    if not md:
        return ""
    cleaned = _ANALYSIS_SECTION_RE.sub("\n", md)
    return cleaned.rstrip() + ("\n" if cleaned.strip() else "")


def _extract_analysis_bullets(text: str) -> str:
    """Pull ### Analysis body, or bare bullet lines when the model skipped the header."""
    if not (text or "").strip():
        return ""
    m = _ANALYSIS_SECTION_RE.search("\n" + text)
    if m:
        body = m.group(1).strip()
        lines = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
        return "\n".join(lines)
    # Bare bullets only (no table) — treat as analysis
    lower = text.lower()
    if "eva-mtx" in lower or "<table" in lower or re.search(r"^\|.+\|", text, re.M):
        return ""
    bullets = [
        ln.rstrip()
        for ln in text.splitlines()
        if re.match(r"^\s*[-*•]", ln) and ln.strip()
    ]
    return "\n".join(bullets[:6])


def _compose_tables_plus_analysis(tool_md: str, model_reply: str) -> str:
    """Deterministic tables + GPT analysis; omit canned tool bullets when AI writes."""
    tables = _strip_analysis_section(tool_md).rstrip()
    model_analysis = _extract_analysis_bullets(model_reply)
    if model_analysis:
        return f"{tables}\n\n### Analysis\n{model_analysis}\n"
    # Last resort only — prefer no Analysis over repeating weak canned lines
    return tables + "\n"


def _wants_gpt_analysis(user_text: str, *, result_mode: str | None = None) -> bool:
    """Use a lean AI analysis turn after structured table tools.

    Always True for normal data answers — canned tool bullets are not the product.
    """
    del user_text, result_mode  # signature kept for callers
    return True


_ANALYSIS_RESPONSE_INSTRUCTIONS = (
    "Paste answer_markdown TABLES verbatim at the start of your reply "
    "(do not rebuild or change numbers). Then add ### Analysis with 2-3 "
    "one-line bullets from THIS table only."
)

_FAST_RESPONSE_INSTRUCTIONS = (
    "Use answer_markdown tables verbatim; Analysis will be written in a follow-up turn."
)

_ANALYSIS_ONLY_SYSTEM = (
    "You are Eva Foods' commercial analyst. Tables are already built — "
    "do NOT rebuild or invent numbers.\n"
    "Reply with ONLY ### Analysis and 2-3 one-line bullets. Keep it short.\n"
    "Rules:\n"
    "- No bold headings / labels (not 'Sales Leader:' — just the insight).\n"
    "- One fact per bullet; include the key number(s).\n"
    "- Prefer: who leads + gap/share; volume vs AMS / % change when present; "
    "one risk or soft spot.\n"
    "- Skip obvious restatements of every row. No filler.\n"
    "- Use only numbers visible in the tool tables.\n"
    "- If the tool says no sales / no matching entities / empty table, say that "
    "in one line — do NOT invent leaders or volumes."
)


def _last_tool_answer_markdown(messages: list[dict[str, Any]]) -> str | None:
    """Most recent tool payload answer_markdown (tables; may include Analysis)."""
    for m in reversed(messages):
        if m.get("role") != "tool":
            continue
        raw = m.get("content") or ""
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("answer_markdown"):
            return str(payload["answer_markdown"])
    return None


def _schema_text() -> str:
    init_db()
    lines: list[str] = [f"Database: {db_path()}", f"Data root: {data_root()}", ""]
    with connect() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        for row in tables:
            name = row["name"]
            if name.startswith("sqlite_"):
                continue
            lines.append(f"## {name}")
            cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
            for col in cols:
                lines.append(f"- {col['name']} ({col['type']})")
            count = conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]
            lines.append(f"rows: {count}")
            lines.append("")
    return "\n".join(lines)


def _validate_select(sql: str) -> str:
    text = sql.strip().rstrip(";")
    if not text:
        raise ValueError("Empty SQL")
    if ";" in text:
        raise ValueError("Only a single SQL statement is allowed")
    if _FORBIDDEN.search(text):
        raise ValueError("Only read-only SELECT / WITH queries are allowed")
    head = re.sub(r"^\s+", "", text, flags=re.IGNORECASE)
    if not re.match(r"^(SELECT|WITH)\b", head, flags=re.IGNORECASE):
        raise ValueError("Query must start with SELECT or WITH")
    return text


def run_sql(sql: str, limit: int = MAX_SQL_ROWS) -> dict[str, Any]:
    init_db()
    query = _validate_select(sql)
    lim = max(1, min(int(limit or MAX_SQL_ROWS), MAX_SQL_ROWS))
    if not re.search(r"\bLIMIT\b", query, flags=re.IGNORECASE):
        query = f"SELECT * FROM ({query}) AS _q LIMIT {lim}"
    with connect() as conn:
        try:
            conn.execute("PRAGMA query_only = ON")
        except sqlite3.Error:
            pass
        try:
            frame = pd.read_sql_query(query, conn)
        except sqlite3.Error as exc:
            return {"ok": False, "error": str(exc), "sql": query}
    records = json.loads(frame.to_json(orient="records", date_format="iso"))
    return {
        "ok": True,
        "sql": query,
        "row_count": len(records),
        "columns": list(frame.columns),
        "rows": records,
        "truncated": len(records) >= lim,
    }


def category_totals(date_from: str, date_to: str) -> dict[str, Any]:
    sql = """
    SELECT COALESCE(c.category_1, '(unmapped)') AS business_unit,
           COALESCE(c.category_1, '(unmapped)') AS category1,
           ROUND(SUM(
             CASE
               WHEN COALESCE(s.mt_qty, 0) <> 0 THEN s.mt_qty
               WHEN lower(trim(COALESCE(s.unit,''))) IN ('kg','kgs')
                 THEN COALESCE(s.qty,0)/1000.0
               WHEN lower(trim(COALESCE(s.unit,''))) IN
                    ('mt','m.t','m.t.','ton','tons','tonne','tonnes')
                 THEN COALESCE(s.qty,0)
               ELSE 0
             END
           ), 3) AS mt,
           COUNT(*) AS lines
    FROM sales s
    LEFT JOIN category c ON c.product = s.product
    WHERE s.date >= ? AND s.date <= ?
    GROUP BY COALESCE(c.category_1, '(unmapped)')
    ORDER BY mt DESC
    """
    init_db()
    with connect() as conn:
        frame = pd.read_sql_query(sql, conn, params=(date_from, date_to))
    return {
        "date_from": date_from,
        "date_to": date_to,
        "rows": json.loads(frame.to_json(orient="records")),
    }


def sales_by_city_and_category(
    *,
    category1: str,
    city: str,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    """MT for one Category 1 in one City-Filter over a date range."""
    sql = """
    SELECT
      ? AS category1,
      ? AS city,
      ? AS date_from,
      ? AS date_to,
      COUNT(*) AS lines,
      COUNT(DISTINCT s.party) AS parties,
      ROUND(SUM(
        CASE
          WHEN COALESCE(s.mt_qty, 0) <> 0 THEN s.mt_qty
          WHEN lower(trim(COALESCE(s.unit,''))) IN ('kg','kgs')
            THEN COALESCE(s.qty,0)/1000.0
          WHEN lower(trim(COALESCE(s.unit,''))) IN
               ('mt','m.t','m.t.','ton','tons','tonne','tonnes')
            THEN COALESCE(s.qty,0)
          ELSE 0
        END
      ), 3) AS mt,
      ROUND(SUM(COALESCE(s.incl_gst_fed_amount, 0)), 2) AS incl_gst_fed_amount
    FROM sales s
    JOIN category c ON c.product = s.product
    LEFT JOIN clients cl
      ON lower(trim(replace(replace(cl.client, '  ', ' '), '  ', ' ')))
       = lower(trim(replace(replace(s.party, '  ', ' '), '  ', ' ')))
    WHERE s.date >= ? AND s.date <= ?
      AND c.category_1 = ?
      AND lower(trim(COALESCE(cl.city_filter, ''))) = lower(trim(?))
    """
    init_db()
    with connect() as conn:
        total = pd.read_sql_query(
            sql,
            conn,
            params=(
                category1,
                city,
                date_from,
                date_to,
                date_from,
                date_to,
                category1,
                city,
            ),
        )
        top_parties = pd.read_sql_query(
            """
            SELECT s.party,
                   COUNT(*) AS lines,
                   ROUND(SUM(
                     CASE
                       WHEN COALESCE(s.mt_qty, 0) <> 0 THEN s.mt_qty
                       WHEN lower(trim(COALESCE(s.unit,''))) IN ('kg','kgs')
                         THEN COALESCE(s.qty,0)/1000.0
                       ELSE COALESCE(s.mt_qty, 0)
                     END
                   ), 3) AS mt
            FROM sales s
            JOIN category c ON c.product = s.product
            LEFT JOIN clients cl
              ON lower(trim(cl.client)) = lower(trim(s.party))
            WHERE s.date >= ? AND s.date <= ?
              AND c.category_1 = ?
              AND lower(trim(COALESCE(cl.city_filter, ''))) = lower(trim(?))
            GROUP BY s.party
            ORDER BY mt DESC
            LIMIT 15
            """,
            conn,
            params=(date_from, date_to, category1, city),
        )
        # Helpful diagnostics if empty
        city_matches = pd.read_sql_query(
            """
            SELECT city_filter, COUNT(*) AS clients
            FROM clients
            WHERE lower(city_filter) LIKE '%' || lower(?) || '%'
            GROUP BY city_filter
            ORDER BY clients DESC
            LIMIT 10
            """,
            conn,
            params=(city,),
        )
    return {
        "ok": True,
        "summary": json.loads(total.to_json(orient="records"))[0] if len(total) else {},
        "top_parties": json.loads(top_parties.to_json(orient="records")),
        "similar_city_filters": json.loads(city_matches.to_json(orient="records")),
        "note": (
            "City match uses clients.city_filter (City-Filter). "
            "If mt is 0, check similar_city_filters for spelling."
        ),
    }


def unmapped_products(limit: int = 50) -> dict[str, Any]:
    sql = """
    SELECT DISTINCT s.product
    FROM sales s
    LEFT JOIN category c ON c.product = s.product
    WHERE c.product IS NULL
    ORDER BY s.product
    LIMIT ?
    """
    init_db()
    with connect() as conn:
        frame = pd.read_sql_query(sql, conn, params=(limit,))
    return {"count": len(frame), "products": frame["product"].tolist()}


def prepare_report_snapshot(report_date: str) -> dict[str, Any]:
    from datetime import date as date_cls

    from eva_dashboard.db_report import prepare_report_from_db

    d = date_cls.fromisoformat(report_date[:10])
    data = prepare_report_from_db(report_date=d)
    return {
        "report_date": data.report_date.isoformat(),
        "month_start": data.month_start.isoformat(),
        "trailing_30_start": data.trailing_30_start.isoformat(),
        "ams_months": list(data.ams_months),
        "total_daily_mt": data.total_daily_mt,
        "total_mtd_mt": data.total_mtd_mt,
        "total_avg_30d_mt": data.total_avg_30d_mt,
        "total_ams_mt": data.total_ams_mt,
        "category_summary": [
            {
                "category1": r.category1,
                "daily_mt": r.daily_mt,
                "avg_30d_mt": r.avg_30d_mt,
                "mtd_mt": r.mtd_mt,
                "ams_mt": r.ams_mt,
            }
            for r in data.category_summary
        ],
        "price_fetch_summary": [
            {
                "client_type": r.client_type,
                "eva_oil": r.eva_oil,
                "eva_ghee": r.eva_ghee,
                "maan_oil": r.maan_oil,
                "maan_ghee": r.maan_ghee,
            }
            for r in data.price_fetch_summary
        ],
        "city_daily_top": json.loads(
            data.city_daily.head(10).to_json(orient="records", date_format="iso")
        ),
        "city_mtd_top": json.loads(
            data.city_mtd.head(10).to_json(orient="records", date_format="iso")
        ),
        "daily_line_count": len(data.daily_sales),
        "bulk_product_count": len(data.bulk_product_prices),
    }


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_sales",
            "description": (
                "PRIMARY sales volume (MT) tool. Builds pivots: rows = Packing when one "
                "Business Unit is set; columns = client_type|city|month. "
                "ALWAYS pass client_type when user names Imtiaz/Distributors/etc. "
                "Do NOT invent a Business Unit for city- or client-type-only questions. "
                "Dispatch fills city/client_type/columns from the user text when omitted. "
                "'Show me X sales' (city/client type/party) with no named month "
                "defaults to columns=month, months_back=6 + AMS 3/6. "
                "Named month (for July / this month) → Volume + AMS + % vs AMS only. "
                "Analytical = city + client + AMS when user says how were/evaluate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": (
                            "Natural period: 'last month', 'last week', 'July', "
                            "'July 2026', 'August so far', 'this month', or YYYY-MM"
                        ),
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Optional ISO YYYY-MM-DD (overrides period start)",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "Optional ISO YYYY-MM-DD (overrides period end)",
                    },
                    "city": {
                        "type": "string",
                        "description": "City-Filter filter, e.g. Lahore",
                    },
                    "business_unit": {
                        "type": "string",
                        "description": "e.g. Eva Consumer, Eva Bulk, Maan Consumer",
                    },
                    "business_units": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Multiple BUs in one table (e.g. after 'add Eva Bulk')"
                        ),
                    },
                    "oil_type": {
                        "type": "string",
                        "description": "e.g. Eva Canola, Eva Cooking, Eva VTF",
                    },
                    "packing_category": {
                        "type": "string",
                        "description": "e.g. Tin, Pet bottle, Stand up",
                    },
                    "client_type": {
                        "type": "string",
                        "description": (
                            "Filter to one Client Type. Aliases: Imtiaz/store → "
                            "Imtiaz Store; Distributor(s) → Eva Distributors"
                        ),
                    },
                    "row_dimension": {
                        "type": "string",
                        "description": (
                            "Override rows: business_unit | oil_type | "
                            "packing_category | product. Spoken product / "
                            "product-wise / by product → packing_category. "
                            "Only SKU wise / break down further → product."
                        ),
                        "enum": [
                            "business_unit",
                            "oil_type",
                            "packing_category",
                            "product",
                        ],
                    },
                    "columns": {
                        "type": "string",
                        "description": (
                            "client_type (default), city, or month "
                            "(last N months + AMS 3/6)"
                        ),
                        "enum": ["client_type", "city", "month"],
                    },
                    "months_back": {
                        "type": "integer",
                        "description": "For columns=month (default 6)",
                    },
                    "prior_spec": {
                        "type": "object",
                        "description": (
                            "Previous table_spec when user adds to the current table"
                        ),
                    },
                    "compare": {
                        "type": "string",
                        "enum": ["yoy"],
                        "description": (
                            "yoy = same calendar span last year; use with prior_spec "
                            "for 'analyze these sales vs last year'"
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "description": "matrix or analytical (usually set from language)",
                        "enum": ["matrix", "analytical"],
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_party",
            "description": (
                "Fuzzy search one client/party by NAME (e.g. 'who is Al Bari?'). "
                "Do NOT use for 'distributors in Lahore' — use list_clients."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Party / client name fragment, e.g. Al Bari",
                    },
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_clients",
            "description": (
                "List clients by City-Filter and/or Client Type, optionally "
                "filtered to a Business Unit (who bought / sold to). "
                "Use for 'who are my distributors in Lahore?' and "
                "'which distributor was Maan Consumer sold to'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "client_type": {"type": "string"},
                    "business_unit": {
                        "type": "string",
                        "description": (
                            "Filter volume to this Business Unit "
                            "(e.g. Maan Consumer) for sold-to / buyers asks."
                        ),
                    },
                    "period": {"type": "string"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_parties",
            "description": (
                "Rank parties/cities or answer AMS, underperformers, new/lost, "
                "packing/SKU mix, invoices, share, YoY. Default rank metric = AMS. "
                "Examples: top 5 distributors for Eva VTF; poorly in Lahore; "
                "new parties last 6 months; product mix for Imtiaz; city league; "
                "growth in July."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {"type": "string"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "city": {"type": "string"},
                    "client_type": {"type": "string"},
                    "business_unit": {"type": "string"},
                    "oil_type": {"type": "string"},
                    "packing_category": {"type": "string"},
                    "brand": {"type": "string"},
                    "metric": {
                        "type": "string",
                        "enum": [
                            "volume",
                            "ams",
                            "vs_ams",
                            "yoy",
                            "share_of_segment",
                            "segment_mix",
                            "geo_share",
                            "doing_well",
                            "new_parties",
                            "lost_parties",
                            "packing_mix",
                            "product_mix",
                            "invoices",
                            "invoice_mt",
                        ],
                    },
                    "compare_period": {"type": "string"},
                    "share_city": {"type": "string"},
                    "group_by": {
                        "type": "string",
                        "enum": ["party", "city"],
                    },
                    "mix_dimension": {
                        "type": "string",
                        "enum": ["packing_category", "product"],
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["desc", "asc"],
                        "description": "asc for poorly / behind AMS",
                    },
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_price",
            "description": (
                "Average Rate from sales, Price Fetch, and cost factors. Use for "
                "'Canola standup price for Distributors last week', average rate, "
                "'what's the Price Fetch?', 'what's the cost factor?', "
                "'show factor breakdown', 'packing cost for [SKU]'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "e.g. last week, last month, July 2026",
                    },
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "city": {"type": "string"},
                    "business_unit": {"type": "string"},
                    "oil_type": {"type": "string"},
                    "packing_category": {"type": "string"},
                    "client_type": {
                        "type": "string",
                        "description": "Imtiaz Store, Eva Distributors, …",
                    },
                    "product": {
                        "type": "string",
                        "description": "Exact sales.product name if known",
                    },
                    "product_query": {
                        "type": "string",
                        "description": "Spoken product, e.g. canola standup",
                    },
                    "include_price_fetch": {
                        "type": "boolean",
                        "description": (
                            "True when user asks for Price Fetch / recovery"
                        ),
                    },
                    "include_cost_factor": {
                        "type": "boolean",
                        "description": (
                            "True for cost factor / packing cost / factor asks "
                            "(also auto-on with Price Fetch)"
                        ),
                    },
                    "factor_breakdown": {
                        "type": "boolean",
                        "description": (
                            "True for factor breakdown / product cost + packing cost"
                        ),
                    },
                    "factor_only": {
                        "type": "boolean",
                        "description": (
                            "True when asking only for cost/packing/product factor "
                            "(no rate/sales period required)"
                        ),
                    },
                    "prior_spec": {
                        "type": "object",
                        "description": "Previous price_spec for follow-ups",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_schema",
            "description": "Return live SQLite schema and row counts. Rarely needed.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sales_overview",
            "description": (
                "Authoritative live overview: sales date min/max, months present, "
                "row counts, categories, cities."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "Read-only SELECT/WITH (max 200 rows). Use ONLY when query_sales "
                "cannot answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["sql"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "category_mt_totals",
            "description": (
                "MT by Business Unit (category_1) for an inclusive YYYY-MM-DD range. "
                "Prefer query_sales for pivots."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                },
                "required": ["date_from", "date_to"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sales_by_city_and_category",
            "description": (
                "Legacy helper: MT for one Business Unit in one city. "
                "Prefer query_sales."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category1": {
                        "type": "string",
                        "description": "Exact Business Unit, e.g. Eva Consumer",
                    },
                    "city": {"type": "string"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                },
                "required": ["category1", "city", "date_from", "date_to"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_unmapped_products",
            "description": "Sales products missing from the category map.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_snapshot",
            "description": (
                "Full PDF-equivalent summary metrics for one report date "
                "(category MT, city tops, Price Fetch, totals)."
            ),
            "parameters": {
                "type": "object",
                "properties": {"report_date": {"type": "string"}},
                "required": ["report_date"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_product_language",
            "description": (
                "Map spoken product language to exact sales.product names and categories."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "product_sales",
            "description": (
                "Sales summary for one product over a date range (optional city)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product": {"type": "string"},
                    "product_query": {"type": "string"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "city": {"type": "string"},
                },
                "required": ["date_from", "date_to"],
                "additionalProperties": False,
            },
        },
    },
]



TOOLS.append(
    {
        "type": "function",
        "function": {
            "name": "advanced_query",
            "description": (
                "Advanced analytics: city/client compare (+growth), WoW, packing/oil "
                "growth, expected month close, silent this week, not ordered packing, "
                "reactivated, days since invoice, concentration/shares, oil mix, "
                "packing contribution, top SKUs, party profile, dumping/excessive sales, "
                "filter entities by volume/YoY/MoM (sales > 10 MT, declined >10%, "
                "more this month than last)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": [
                            "compare_cities",
                            "compare_client_types",
                            "week_over_week",
                            "dimension_growth",
                            "expected_month",
                            "silent_week",
                            "not_ordered",
                            "reactivated",
                            "days_since_invoice",
                            "concentration",
                            "concentration_growth",
                            "oil_mix",
                            "packing_contribution",
                            "packing_share_of_party",
                            "top_skus",
                            "party_profile",
                            "dumping",
                            "filter_entities",
                        ],
                    },
                    "period": {"type": "string"},
                    "city": {"type": "string"},
                    "client_type": {"type": "string"},
                    "business_unit": {"type": "string"},
                    "oil_type": {"type": "string"},
                    "packing_category": {"type": "string"},
                    "left": {"type": "string"},
                    "right": {"type": "string"},
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "For multi-way compares: all cities or client types "
                            "(e.g. Lahore, Karachi, Islamabad or Imtiaz, Metro, Chase Up). "
                            "Prefer this over only left/right when 3+ sides."
                        ),
                    },
                    "party_query": {"type": "string"},
                    "group_by": {"type": "string"},
                    "metric": {"type": "string"},
                    "entity": {
                        "type": "string",
                        "description": (
                            "For filter_entities: party|product|packing_category|"
                            "oil_type|business_unit|city|client_type"
                        ),
                    },
                    "op": {
                        "type": "string",
                        "description": (
                            "For filter_entities: gt|gte|lt|lte|grown|declined"
                        ),
                    },
                    "threshold": {
                        "type": "number",
                        "description": (
                            "Volume MT, or % for YoY/MoM (e.g. 10 for declined >10%)"
                        ),
                    },
                    "limit": {"type": "integer"},
                    "exclude_client_types": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "additionalProperties": False,
            },
        },
    }
)


def _looks_analytical(text: str) -> bool:
    """True when the user asks for evaluation / performance, not a plain 'what were' dump."""
    t = (text or "").lower()
    # "how much" is a quantity ask → matrix, not analytical
    if re.search(r"\bhow much\b", t) and not re.search(
        r"\b(evaluate|assess|performance|trend|ams)\b", t
    ):
        return False
    # Sales YoY compare of the prior table is its own mode (not AMS analytical pack)
    if _looks_sales_yoy_compare(t):
        return False
    patterns = (
        r"\bhow were\b",
        r"\bhow are\b",
        r"\bhow is\b",
        r"\bhow did\b",
        r"\bhow do\b",
        r"\bhow have\b",
        r"\bhow has\b",
        r"\bevaluate\b",
        r"\bevaluating\b",
        r"\bevaluation\b",
        r"\bassess\b",
        r"\bassessing\b",
        r"\bassessment\b",
        r"\bperformance\b",
        r"\bdoing\b",  # "how are … doing"
        r"\btrend\b",
        r"\banaly[sz]e\b",
        r"\banalysis\b",
        r"\bvs\s*ams\b",
        r"\bagainst ams\b",
        r"\bcompared to ams\b",
        r"\binsight",
        r"\breview\b",
    )
    return any(re.search(p, t) for p in patterns)


def _looks_sales_yoy_compare(text: str) -> bool:
    """Compare prior sales table / scope to the same period last year (not party ranks)."""
    t = (text or "").lower()
    # Explicit party growth ranking stays on analyze_parties
    if re.search(
        r"\b(top|which)\b.+\b(distributors?|parties|imtiaz|clients?)\b",
        t,
    ) or re.search(
        r"\b(distributors?|parties)\b.+\b(grew|growth|top\s+sales\s+growth)\b",
        t,
    ):
        return False
    return bool(
        re.search(
            r"\b(compare|comparison|versus|vs\.?)\b.+\b"
            r"(last year|year ago|same period|prior year)\b",
            t,
        )
        or re.search(
            r"\b(same period last year|vs\.?\s*last year|versus last year|"
            r"compared? (to|with) last year)\b",
            t,
        )
        or (
            re.search(r"\b(analy[sz]e|analysis)\b.+\b(sales|these|this|them)\b", t)
            and re.search(r"\b(last year|year ago|yoy|year over year)\b", t)
        )
        or (
            _looks_context_followup(t)
            and re.search(r"\b(last year|year ago|same period|yoy)\b", t)
            and not re.search(
                r"\b(distributors?|parties|imtiaz stores?|top\s+\d)\b",
                t,
            )
        )
    )


_KNOWN_BUSINESS_UNITS = (
    "Eva Consumer",
    "Eva Bulk",
    "Maan Consumer",
    "Maan Bulk",
    "Cusine King",
    "Cuisine King",
    "Shortening",
    "Bulk Oil",
    "Meal",
    "Byproducts",
)


def _looks_month_wise(text: str) -> bool:
    t = (text or "").lower()
    return bool(
        re.search(
            r"\b(month[- ]?wise|monthly|by month|per month|last\s+\d+\s+months|"
            r"past\s+\d+\s+months|months?\s+breakdown|breakdown.*months?|"
            r"average sale|avg sale|avg\.?\s*sale)\b",
            t,
        )
    )


def _looks_single_month_only(text: str) -> bool:
    """True for 'July only' / 'just August' — keep a single-period view."""
    t = (text or "").lower()
    if re.search(r"\b(only|just)\b", t) and _extract_period_phrase(text):
        return True
    return False


def _wants_named_month_trend(text: str) -> bool:
    """Named calendar month → Volume + AMS + %change (not a 6-month grid).

    Explicit multi-month language ('last 6 months', 'month-wise') keeps the grid.
    Analytical 'how were / performance' keeps the full analytical pack.
    """
    if not (text or "").strip():
        return False
    if not _extract_period_phrase(text):
        return False
    t = (text or "").lower()
    if _looks_analytical(t):
        return False
    if _looks_month_wise(text):
        return False
    if re.search(r"\b(last|past|previous)\s+\d{1,2}\s+months?\b", t):
        return False
    if re.search(r"\b(last|past|previous)\s+quarter\b", t):
        return False
    # Mutating follow-ups keep prior grain; period-only / July-only → trend
    if _looks_table_op_followup(text) and not (
        _looks_single_month_only(text) or _looks_period_only_followup(text)
    ):
        return False
    if _looks_party_analytics(t) or _looks_client_list(t):
        return False
    if _looks_party_breakdown(t) or _looks_party_mix_query(t):
        return False
    return bool(
        re.search(r"\b(sales?|volume|mt|show|give|how much)\b", t)
        or _looks_named_party_sales(text)
        or extract_client_type_from_text(text)
        or extract_city_from_text(text)
    )


def _looks_scoped_entity_sales(text: str) -> bool:
    """True for 'show me X sales' where X is party / client type / city.

    These default to a month-wise matrix with AMS columns when no month is named.
    """
    t = (text or "").lower()
    if _looks_analytical(t) or _looks_party_analytics(t) or _looks_client_list(t):
        return False
    if _looks_party_breakdown(t) or _looks_party_mix_query(t):
        return False
    if not re.search(r"\b(sales?|volume|mt)\b", t):
        return False
    if _looks_named_party_sales(text):
        return True
    if extract_client_type_from_text(text):
        return True
    if extract_city_from_text(text):
        return True
    return False


def _looks_table_op_followup(text: str) -> bool:
    """True when this turn mutates / inspects a prior sales table."""
    return bool(
        _is_explicit_followup(text)
        or _looks_include_check(text)
        or _looks_combine_tables(text)
        or _looks_table_followup(text)
        or _looks_regroup(text)
        or _looks_remove(text)
        or _looks_row_drilldown(text)
        or _looks_sales_yoy_compare(text)
        or _looks_party_breakdown(text)
        or _looks_period_only_followup(text)
    )


def _wants_scoped_month_ams(text: str) -> bool:
    """Fresh city/client-type sales ask → month grid + AMS (no named month)."""
    if not (text or "").strip():
        return False
    if _wants_named_month_trend(text):
        return False
    if _looks_table_op_followup(text) or _looks_single_month_only(text):
        return False
    if _looks_analytical(text):
        return False
    if _looks_month_wise(text) or _looks_scoped_entity_sales(text):
        return True
    t = text.lower()
    if _looks_party_analytics(t) or _looks_client_list(t):
        return False
    return bool(
        (
            extract_client_type_from_text(text)
            or extract_city_from_text(text)
        )
        and re.search(r"\b(sales?|volume|mt|show|give)\b", t)
    )


def _should_redirect_scoped_sales(name: str, user_text: str) -> bool:
    """Under tool_choice=required, GPT may pick the wrong tool for show-me sales."""
    if name not in {
        "list_clients",
        "analyze_parties",
        "advanced_query",
        "lookup_party",
        "query_price",
    }:
        return False
    if _looks_named_party_sales(user_text) or _looks_table_op_followup(user_text):
        return False
    if _looks_scoped_entity_sales(user_text):
        return True
    t = (user_text or "").lower()
    if _looks_analytical(t) or _looks_party_analytics(t) or _looks_client_list(t):
        return False
    return bool(
        (
            extract_client_type_from_text(user_text)
            or extract_city_from_text(user_text)
        )
        and re.search(r"\b(sales?|volume|mt)\b", t)
    )


FOLLOWUP_MARKER = "[FOLLOW-UP on the answer you just gave]"


def _is_explicit_followup(text: str) -> bool:
    """True when the UI Reply button prefixed the user message."""
    t = (text or "").lstrip()
    return t.startswith("[FOLLOW-UP") or t.startswith(FOLLOWUP_MARKER)


def _looks_include_check(text: str) -> bool:
    """True for 'does this include bulk?' / 'was Eva Bulk included?'."""
    t = (text or "").lower()
    if re.search(
        r"\b("
        r"does (this|it|that) include|"
        r"is .{0,30} included|"
        r"was .{0,30} included|"
        r"are .{0,30} included|"
        r"included in (this|the|that|previous)|"
        r"any .{0,20} (in|included in) (this|the|that)"
        r")\b",
        t,
    ):
        return True
    if re.search(
        r"\b(include[sd]?|including)\b.+\b(bulk|consumer|eva|maan)\b",
        t,
    ) and re.search(r"\b(this|that|previous|table|above)\b", t):
        return True
    return False


def _looks_combine_tables(text: str) -> bool:
    t = (text or "").lower()
    return bool(
        re.search(
            r"\b(combine|merge)\b.+\b(table|tables|them|both|sales|together)\b",
            t,
        )
        or re.search(r"\b(combine|merge)\s+(the\s+)?(tables?|them|both)\b", t)
        or re.search(
            r"\b(add|include|also include|plus|append)\b.+\b"
            r"(bulk|consumer)\s*(sales?)?\b",
            t,
        )
    )


def _looks_table_followup(text: str) -> bool:
    t = (text or "").lower()
    if re.search(
        r"\b(add|also include|include|plus|with|append)\b.+\b"
        r"(eva|maan|bulk|consumer|cusine|cuisine|shortening|meal|byproduct)",
        t,
    ):
        return True
    if re.search(r"\badd\b.+\bto (this|the|that) table\b", t):
        return True
    if _looks_combine_tables(t):
        return True
    if _looks_remove(t):
        return True
    if _looks_regroup(t):
        return True
    if _looks_row_drilldown(t):
        return True
    if _looks_same_format(t):
        return True
    if _looks_hide_sku(t):
        return True
    if _is_explicit_followup(t) and re.search(
        r"\b(add|include|combine|merge|bulk|consumer|group|city|wise|remove|exclude)\b", t
    ):
        return True
    return False


def _prior_units_list(prior_spec: dict[str, Any] | None) -> list[str]:
    if not prior_spec:
        return []
    units: list[str] = []
    for u in prior_spec.get("business_units") or []:
        if u and u not in units:
            units.append(str(u))
    pf = prior_spec.get("filters") or {}
    one = pf.get("business_unit")
    if one and one not in units:
        units.append(str(one))
    # Nested prior from an include_check answer
    ic = prior_spec.get("include_check") or {}
    for u in ic.get("prior_business_units") or []:
        if u and u not in units:
            units.append(str(u))
    return units


def _companion_business_units(
    text: str,
    prior_spec: dict[str, Any] | None = None,
) -> list[str]:
    """Map bare 'bulk' / 'consumer' to the companion BU of the prior table."""
    t = (text or "").lower()
    prior_units = _prior_units_list(prior_spec)
    prior_l = [u.lower() for u in prior_units]
    out: list[str] = []

    has_explicit_bulk = bool(re.search(r"\b(eva|maan)\s+bulk\b", t))
    has_explicit_cons = bool(re.search(r"\b(eva|maan)\s+consumer\b", t))

    if re.search(r"\bbulk\b", t) and not has_explicit_bulk:
        if any("maan" in u and "consumer" in u for u in prior_l):
            out.append("Maan Bulk")
        elif any("maan" in u and "bulk" in u for u in prior_l):
            out.append("Maan Bulk")
        else:
            out.append("Eva Bulk")

    if re.search(r"\bconsumer\b", t) and not has_explicit_cons:
        # Only when clearly asking about consumer relative to a bulk table /
        # include-check — avoid hijacking "Eva Consumer" questions.
        if any("bulk" in u for u in prior_l) or _looks_include_check(t) or _looks_combine_tables(t):
            if any("maan" in u for u in prior_l):
                out.append("Maan Consumer")
            else:
                out.append("Eva Consumer")

    return out


def _resolve_include_segment(
    text: str,
    prior_spec: dict[str, Any] | None,
) -> str | None:
    """Which BU the user is asking about in an include-check question."""
    named = _extract_business_units_from_text(text)
    if named:
        return named[0]
    comps = _companion_business_units(text, prior_spec)
    if comps:
        return comps[0]
    # Default: if they said "include" without naming, assume Bulk vs Consumer prior
    prior_units = _prior_units_list(prior_spec)
    if any("consumer" in u.lower() for u in prior_units):
        if any("maan" in u.lower() for u in prior_units):
            return "Maan Bulk"
        return "Eva Bulk"
    if any("bulk" in u.lower() for u in prior_units):
        if any("maan" in u.lower() for u in prior_units):
            return "Maan Consumer"
        return "Eva Consumer"
    return "Eva Bulk"


def _looks_row_drilldown(text: str) -> bool:
    """True when user wants to change row grain of the current table."""
    t = (text or "").lower()
    return bool(
        re.search(
            r"\b("
            r"show by product|product break\s*down|product breakdown|"
            r"by product|product categor|"
            r"packing categor|by packing|show packing|"
            r"oil type|by oil|oil break|"
            r"dissect|drill\s*down|break( it)? down further|"
            r"sku[- ]?wise|by sku|show by sku|sku break|show skus?|"
            r"product[- ]?wise|"
            r"by business unit|by bu\b|show by bu"
            r")\b",
            t,
        )
        or re.search(r"\b(further|deeper)\b.+\b(break|split|dissect|detail)", t)
        or re.search(r"\b(break|split|dissect)\b.+\b(further|deeper|sku|product)", t)
    )


def _looks_regroup(text: str) -> bool:
    """True for 'city wise' / 'group by client type' follow-ups."""
    return resolve_regroup_request(text, prior_spec={"column_dimension": "_"}) is not None


def _looks_hide_sku(text: str) -> bool:
    """True for 'don't show individual sku' / 'hide skus' / 'no sku'."""
    t = (text or "").lower()
    return bool(
        re.search(
            r"\b("
            r"(don'?t|do\s+not|dont)\s+show(\s+individual)?\s+skus?|"
            r"hide(\s+individual)?\s+skus?|"
            r"without(\s+individual)?\s+skus?|"
            r"no(\s+more)?(\s+individual)?\s+skus?|"
            r"remove(\s+individual)?\s+skus?|"
            r"drop(\s+individual)?\s+skus?|"
            r"collapse\s+skus?"
            r")\b",
            t,
        )
    )


def _looks_national_scope(text: str) -> bool:
    """True for Pakistan / nationwide / all-over asks (clear city filter)."""
    t = (text or "").lower()
    return bool(
        re.search(
            r"\b("
            r"all\s+over\s+pakistan|across\s+pakistan|nationwide|national|"
            r"country[- ]?wide|all\s+pakistan|pakistan[- ]?wide|"
            r"all\s+over\s+the\s+country|across\s+the\s+country"
            r")\b",
            t,
        )
    )


def _looks_same_format(text: str) -> bool:
    """True for 'in the same format / same way / same table layout'."""
    t = (text or "").lower()
    return bool(
        re.search(
            r"\b("
            r"same\s+format|same\s+way|same\s+table|same\s+layout|"
            r"same\s+structure|same\s+columns|as\s+above|like\s+above|"
            r"same\s+style"
            r")\b",
            t,
        )
    )


def _looks_remove(text: str) -> bool:
    t = (text or "").lower()
    if _looks_hide_sku(t):
        return True
    return bool(
        re.search(
            r"\b(remove|exclude|drop|without|filter\s+out)\b.+"
            r"|\b(remove|exclude|drop)\s+(the\s+)?\w+",
            t,
        )
    ) and bool(
        re.search(
            r"\b(remove|exclude|drop|without|filter\s+out)\b",
            t,
        )
    )


_STRUCT_DIM_PHRASES: list[tuple[str, str]] = [
    ("client types", "client_type"),
    ("client type", "client_type"),
    ("business units", "business_unit"),
    ("business unit", "business_unit"),
    ("packing categories", "packing_category"),
    ("packing category", "packing_category"),
    ("oil types", "oil_type"),
    ("oil type", "oil_type"),
    ("packings", "packing_category"),
    ("packing", "packing_category"),
    ("products", "packing_category"),
    ("product", "packing_category"),
    ("skus", "product"),
    ("sku", "product"),
    ("cities", "city"),
    ("city", "city"),
    ("months", "month"),
    ("month", "month"),
    ("bu", "business_unit"),
]


def _extract_remove_phrase(text: str) -> str | None:
    t = (text or "").strip()
    if _looks_hide_sku(t):
        return "sku"
    m = re.search(
        r"\b(?:remove|exclude|drop|without|filter\s+out)\s+"
        r"(?:the\s+)?(.+?)(?:\s+from\s+(?:the\s+)?(?:table|view|this|that|above))?\s*$",
        t,
        flags=re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r"\b(?:remove|exclude|drop|without|filter\s+out)\s+(?:the\s+)?(.+)",
            t,
            flags=re.IGNORECASE,
        )
    if not m:
        return None
    phrase = re.sub(r"\s+", " ", m.group(1)).strip(" .,!?")
    phrase = re.sub(
        r"\s+from\s+(the\s+)?(table|view|this|that|above)$",
        "",
        phrase,
        flags=re.IGNORECASE,
    ).strip()
    # Strip trailing filler like "items" / "rows" after the value list
    phrase = re.sub(
        r"\s+(items?|rows?|lines?|entries)\s*$",
        "",
        phrase,
        flags=re.IGNORECASE,
    ).strip()
    return phrase or None


def _split_remove_value_phrases(phrase: str) -> list[str]:
    """Split 'A and B and C' / 'A, B, and C' into separate exclude phrases."""
    raw = (phrase or "").strip()
    if not raw:
        return []
    # Prefer known multi-word business units before naive splitting
    found_units = _extract_business_units_from_text(raw)
    if len(found_units) >= 2:
        return found_units
    parts = re.split(r"\s*,\s*|\s+and\s+|\s*&\s*", raw, flags=re.IGNORECASE)
    out: list[str] = []
    for p in parts:
        cleaned = re.sub(r"\s+", " ", p).strip(" .,!?")
        cleaned = re.sub(
            r"\s+(items?|rows?|lines?|entries)\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        if cleaned:
            out.append(cleaned)
    return out or ([raw] if raw else [])


def _phrase_as_struct_dim(phrase: str) -> str | None:
    key = (phrase or "").strip().lower()
    for needle, dim in _STRUCT_DIM_PHRASES:
        if key == needle or key == needle.replace(" ", ""):
            return dim
    return normalize_row_dimension(phrase)


def _active_structure(prior_spec: dict[str, Any]) -> dict[str, Any]:
    row_dim = normalize_row_dimension(prior_spec.get("row_dimension")) or prior_spec.get(
        "row_dimension"
    )
    col_dim = str(prior_spec.get("column_dimension") or "client_type")
    groups = [
        normalize_row_dimension(g) or str(g)
        for g in (prior_spec.get("row_groups") or [])
        if g
    ]
    return {"row_dimension": row_dim, "column_dimension": col_dim, "row_groups": groups}


def _resolve_exclude_value(phrase: str) -> tuple[str, str] | None:
    """Map a spoken value to (dimension, canonical_value)."""
    raw = (phrase or "").strip()
    if not raw:
        return None
    # Client types first (distributors, Imtiaz, …) — aliases / extract only,
    # never passthrough unknown text as a client type.
    from eva_dashboard.client_language import CLIENT_TYPE_ALIASES, list_known_client_types

    key = re.sub(r"\s+", " ", raw.lower()).strip()
    if key in CLIENT_TYPE_ALIASES:
        return ("client_type", CLIENT_TYPE_ALIASES[key])
    ctype = extract_client_type_from_text(raw)
    if ctype:
        return ("client_type", ctype)
    known_types = {re.sub(r"\s+", " ", t.lower()).strip(): t for t in list_known_client_types()}
    if key in known_types:
        return ("client_type", known_types[key])

    from eva_dashboard.party_analytics import extract_city_from_text

    city = extract_city_from_text(raw)
    if city:
        return ("city", city)
    for city_name in (
        "Lahore",
        "Karachi",
        "Islamabad",
        "Faisalabad",
        "Multan",
        "Peshawar",
        "Rawalpindi",
        "Gujranwala",
        "Sialkot",
        "Hyderabad",
        "Quetta",
    ):
        if raw.lower() == city_name.lower():
            return ("city", city_name)
    units = _extract_business_units_from_text(raw)
    if units:
        return ("business_unit", units[0])
    oil = extract_oil_type_from_text(raw)
    if oil:
        return ("oil_type", oil)
    pack = extract_packing_from_text(raw)
    if pack:
        return ("packing_category", pack)
    return None


def resolve_remove_request(
    text: str,
    *,
    prior_spec: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Handle 'remove X' follow-ups.

    Rules:
    - If X is an active row/column/grouping **dimension** → remove that layer
      (filters stay the same).
    - If X is a **value** (Lahore, distributors, Eva Consumer, …) → exclude that
      value from rows/columns (filter it out of the data).
    """
    if not prior_spec or not _looks_remove(text):
        return None
    phrase = _extract_remove_phrase(text)
    if not phrase:
        return None

    struct = _active_structure(prior_spec)
    dim_name = _phrase_as_struct_dim(phrase)
    pf = dict(prior_spec.get("filters") or {})

    # --- Structural layer removal ---
    if dim_name and dim_name in set(struct["row_groups"]) | {
        struct["row_dimension"],
        struct["column_dimension"],
    }:
        out: dict[str, Any] = {
            "mode": "remove_layer",
            "dimension": dim_name,
            "columns": struct["column_dimension"],
            "row_dimension": struct["row_dimension"],
            "row_groups": list(struct["row_groups"]),
            "clear_filters": [],
            "excludes": {},
            "lock_columns": True,
        }
        if dim_name in out["row_groups"]:
            out["row_groups"] = [g for g in out["row_groups"] if g != dim_name]
        elif dim_name == struct["row_dimension"]:
            # Drop leaf row dim → fall back under remaining groups or auto grain
            if out["row_groups"]:
                # Promote last group to leaf if we only had groups+leaf
                out["row_dimension"] = out["row_groups"][-1]
                out["row_groups"] = out["row_groups"][:-1]
            else:
                # Sensible default from remaining filters
                if pf.get("business_unit") or len(prior_spec.get("business_units") or []) == 1:
                    out["row_dimension"] = "packing_category"
                else:
                    out["row_dimension"] = "business_unit"
        elif dim_name == struct["column_dimension"]:
            out["columns"] = (
                "client_type"
                if struct["column_dimension"] != "client_type"
                else "city"
            )
            if out["columns"] == "city" and pf.get("city"):
                out["columns"] = "client_type"
            if out["columns"] == "month":
                out["columns"] = "client_type"
        return out

    # --- Value exclusion (filter out row/column values; support multi-value) ---
    excludes: dict[str, list[str]] = {}
    for part in _split_remove_value_phrases(phrase):
        resolved = _resolve_exclude_value(part)
        if not resolved:
            continue
        ex_dim, ex_val = resolved
        bucket = excludes.setdefault(ex_dim, [])
        if ex_val not in bucket:
            bucket.append(ex_val)
    if not excludes:
        return None
    # Also drop excluded BUs from an active multi-BU filter list
    clear: list[str] = []
    prior_units = [
        str(u)
        for u in (prior_spec.get("business_units") or [])
        if u
    ]
    pf_bu = (prior_spec.get("filters") or {}).get("business_unit")
    if pf_bu and pf_bu not in prior_units:
        prior_units.append(str(pf_bu))
    drop_bus = set(excludes.get("business_unit") or [])
    keep_units = [u for u in prior_units if u not in drop_bus]
    out = {
        "mode": "exclude_value",
        "dimension": next(iter(excludes)),
        "value": (excludes[next(iter(excludes))] or [None])[0],
        "columns": struct["column_dimension"],
        "row_dimension": struct["row_dimension"],
        "row_groups": list(struct["row_groups"]),
        "clear_filters": clear,
        "excludes": excludes,
        "lock_columns": True,
        "business_units": keep_units,
    }
    return out


def extract_regroup_dimension(text: str) -> str | None:
    """Which dimension the user wants to group by (city, client_type, …)."""
    t = (text or "").lower()
    patterns: list[tuple[str, str]] = [
        (
            r"\b(group(ed)?\s+by|break(?:\s*down)?\s+by|split\s+by|"
            r"organise\s+by|organize\s+by)\s+(cities|city)\b",
            "city",
        ),
        (r"\b(city[- ]?wise|by\s+city|cities\s+wise|show\s+city\s+wise)\b", "city"),
        (
            r"\b(group(ed)?\s+by|break(?:\s*down)?\s+by|split\s+by)\s+"
            r"(client\s*types?|channels?)\b",
            "client_type",
        ),
        (
            r"\b(client[- ]?type[- ]?wise|channel[- ]?wise|by\s+client\s*types?|"
            r"by\s+channels?)\b",
            "client_type",
        ),
        (
            r"\b(group(ed)?\s+by|break(?:\s*down)?\s+by|split\s+by)\s+"
            r"(business\s*units?|bu)\b",
            "business_unit",
        ),
        (
            r"\b(business\s*unit[- ]?wise|bu[- ]?wise|by\s+business\s*units?|by\s+bu)\b",
            "business_unit",
        ),
        (
            r"\b(group(ed)?\s+by|break(?:\s*down)?\s+by|split\s+by)\s+packings?\b",
            "packing_category",
        ),
        (
            r"\b(group(ed)?\s+by|break(?:\s*down)?\s+by|split\s+by)\s+products?\b",
            "packing_category",
        ),
        (
            r"\b(group(ed)?\s+by|break(?:\s*down)?\s+by|split\s+by)\s+skus?\b",
            "product",
        ),
        (
            r"\b(group(ed)?\s+by|break(?:\s*down)?\s+by|split\s+by)\s+oil\s*types?\b",
            "oil_type",
        ),
        (
            r"\b(as|into)\s+columns?\s+(by\s+)?(cities|city)\b",
            "city",
        ),
        (
            r"\b(as|into)\s+columns?\s+(by\s+)?(client\s*types?|channels?)\b",
            "client_type",
        ),
        (
            r"\b(as|into)\s+columns?\s+(by\s+)?(business\s*units?|bu)\b",
            "business_unit",
        ),
    ]
    for pat, dim in patterns:
        if re.search(pat, t):
            return dim
    return None


def _looks_channel_language(text: str) -> bool:
    """True when the user means trade channels (= client types)."""
    t = (text or "").lower()
    return bool(
        re.search(r"\bchannels?\b|\btrade\s*channels?\b|\bclient\s*types?\b", t)
    )


def _looks_channel_growth_ask(text: str) -> bool:
    """'Which channels grew / declined' → client_type Volume + AMS + %."""
    if not _looks_channel_language(text):
        return False
    t = (text or "").lower()
    return bool(
        re.search(
            r"\b("
            r"grew|grown|grow|growth|declined?|dropped|fallen|fell|"
            r"increased?|decreased?|up|down|vs\s*ams|against ams|"
            r"performance|doing"
            r")\b",
            t,
        )
    )


def resolve_regroup_request(
    text: str,
    *,
    prior_spec: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Map 'city wise' / 'group by X' to row vs column changes on the prior table.

    Heuristics:
    - Month columns stay on X (columns) unless the user asks for month-wise columns.
    - 'group by X' / 'X wise' → X becomes the first row column (Y) by default.
    - Explicit 'as columns' → X becomes column dimension.
    - If X was a filter (e.g. city=Lahore), clear that filter so values can vary.
    - When prior rows were packing/SKU/BU, nest them under the new group.
    """
    if not prior_spec:
        return None
    dim = extract_regroup_dimension(text)
    if not dim:
        return None

    t = (text or "").lower()
    prior_col = str(prior_spec.get("column_dimension") or "client_type")
    prior_row = normalize_row_dimension(prior_spec.get("row_dimension")) or prior_spec.get(
        "row_dimension"
    )
    pf = dict(prior_spec.get("filters") or {})

    explicit_col = bool(
        re.search(
            r"\b(as columns?|across columns?|columns? by|on (the )?columns?|"
            r"x[ -]?axis|into columns?)\b",
            t,
        )
    )
    explicit_row = bool(
        re.search(
            r"\b(as rows?|first column|on (the )?rows?|y[ -]?axis|row[- ]?wise)\b",
            t,
        )
    )

    if explicit_col:
        axis = "column"
    elif explicit_row:
        axis = "row"
    elif prior_col == "month":
        # Keep the time series on X; put the new group on Y
        axis = "row"
    elif dim == prior_col:
        # Already on columns — move to first row column
        axis = "row"
    else:
        axis = "row"

    clear: list[str] = []
    if dim == "city" and pf.get("city"):
        clear.append("city")
    if dim == "client_type" and pf.get("client_type"):
        clear.append("client_type")
    if dim == "oil_type" and pf.get("oil_type"):
        clear.append("oil_type")
    if dim == "packing_category" and pf.get("packing_category"):
        clear.append("packing_category")
    if dim == "business_unit" and (
        pf.get("business_unit") or prior_spec.get("business_units")
    ):
        clear.append("business_unit")

    out: dict[str, Any] = {
        "axis": axis,
        "dimension": dim,
        "clear_filters": clear,
    }

    nestable = {"packing_category", "product", "business_unit", "oil_type"}
    if axis == "row":
        if (
            prior_row
            and prior_row != dim
            and prior_row in nestable
            and dim in {"city", "client_type", "business_unit"}
        ):
            out["row_dimension"] = prior_row
            out["row_groups"] = [dim]
        else:
            out["row_dimension"] = dim
            out["row_groups"] = []
        if prior_col == dim:
            out["columns"] = "client_type" if dim != "client_type" else "city"
        else:
            out["columns"] = prior_col
    else:
        out["columns"] = dim
        out["row_dimension"] = prior_row if prior_row and prior_row != dim else None
        out["row_groups"] = list(prior_spec.get("row_groups") or [])

    return out


def resolve_row_dimension_request(
    text: str,
    *,
    prior_row_dimension: str | None = None,
) -> str | None:
    """Map follow-up language to an explicit query_sales row_dimension."""
    t = (text or "").lower()
    if not t:
        return None

    # Hide / collapse SKU layer → packing (must beat bare "sku" match below)
    if _looks_hide_sku(t):
        return "packing_category"

    # SKU / individual line items only when user says SKU (or item-wise)
    if re.search(
        r"\b(sku[- ]?wise|by sku|show by sku|sku break|skus?\b|"
        r"item[- ]?wise|by items?|show skus?)\b",
        t,
    ):
        return "product"

    # Oil Type rows
    if re.search(r"\b(oil types?|by oil|oil break\s*down|oil breakdown)\b", t):
        return "oil_type"

    # Packing = spoken "product" (product wise / by product / product breakdown)
    if re.search(
        r"\b("
        r"show by product|product break\s*down|product breakdown|"
        r"product[- ]?wise|by product|product categor|"
        r"packing categor|by packing|show packing|pack(ing)? break"
        r")\b",
        t,
    ):
        return "packing_category"

    # Business Unit
    if re.search(r"\b(by business unit|by bu\b|show by bu|business unit break)\b", t):
        return "business_unit"

    # Channels / client types as rows
    if re.search(
        r"\b(by\s+channels?|channel[- ]?wise|by\s+client\s*types?|"
        r"client[- ]?type[- ]?wise|show\s+channels?)\b",
        t,
    ):
        return "client_type"

    # Generic deepen: one step from prior grain
    if re.search(
        r"\b(dissect|drill\s*down|break( it)? down further|"
        r"split further|go deeper|more detail)\b",
        t,
    ) or re.search(r"\b(further|deeper)\b.+\b(break|split|dissect|detail)", t):
        prior = (prior_row_dimension or "").strip().lower()
        if prior in {"", "business_unit"}:
            return "packing_category"
        if prior in {"oil_type", "packing_category"}:
            return "product"
        if prior == "product":
            return "product"
        return "packing_category"

    return None



def _extract_named_party_query(text: str) -> str | None:
    """Extract a free-text client/party name from a sales question."""
    from eva_dashboard.sales_query import MONTH_NAMES
    from eva_dashboard.categories import BUSINESS_UNIT_ALIASES

    raw = (text or "").strip()
    if not raw:
        return None
    t = re.sub(r"(?i)^(can you|could you|please|pls)\s+", "", raw).strip()

    _month_tail = (
        r"(?:\s+(?:in|for|of)\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|"
        r"may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?|this\s+month|last\s+month|this\s+week|"
        r"last\s+week|last\s+\d+\s+months?|so\s+far|mtd|20\d{2}).*)?$"
    )
    patterns = [
        # "sales for the client Rubina Shaheen in July"
        r"(?i)(?:sales?|volume|mt)\s+for\s+(?:the\s+)?"
        r"(?:client|party|distributor|customer)\s+(.+?)" + _month_tail,
        # "sales for Alpha Dist in July" / "sales of Gamma Dist this month"
        # Negative lookahead avoids "sales for the last 6 months" / "sales for July"
        r"(?i)(?:sales?|volume|mt)\s+(?:for|of)\s+"
        r"(?!(?:the\s+)?(?:last|this|next)\b)"
        r"(?!(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?|mtd|so\s+far)\b)"
        r"(.+?)" + _month_tail,
        # "client Alpha Dist sales"
        r"(?i)(?:client|party|distributor|customer)\s+(.+?)\s+"
        r"(?:sales?|volume|mt)\b",
        # "show me Alpha Dist sales in July"
        r"(?i)(?:show|give)(?:\s+me)?\s+(.+?)\s+sales?"
        r"(?:\s+(?:in|for|of)\s+\w+.*)?$",
        # "what were Rubina Shaheen sales last month"
        r"(?i)(?:what\s+)?(?:were|was|are|is)\s+(.+?)\s+sales?"
        r"(?:\s+(?:in|for|of|last|this)\b.*)?$",
    ]
    name = None
    for pat in patterns:
        m = re.search(pat, t)
        if m:
            name = m.group(1).strip(" ?.,\"'")
            break
    if not name:
        return None

    name = re.sub(
        r"(?i)\s+(?:in|for|of)\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
        r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?|this\s+month|last\s+month|this\s+week|"
        r"last\s+week|so\s+far|mtd|20\d{2})\s*$",
        "",
        name,
    ).strip(" ?.,\"'")
    name = re.sub(
        r"(?i)^(the\s+)?(client|party|distributor|customer)\s+",
        "",
        name,
    ).strip()
    name = re.sub(r"(?i)\s+last\s+\d+\s+months?\s*$", "", name).strip()
    name = re.sub(
        r"(?i)\s+(?:this|last)\s+(?:month|week)\s*$",
        "",
        name,
    ).strip()

    low = name.lower()
    if not name or len(name) < 3 or not re.search(r"[a-zA-Z]", name):
        return None
    if low in {"a", "an", "the"}:
        return None
    # Reject bare period phrases mistaken for party names
    if re.fullmatch(
        r"(the\s+)?(last|this|next)(\s+\d+)?(\s+(months?|weeks?|years?|quarters?))?",
        low,
    ):
        return None
    if re.search(
        r"\b("
        r"break\s*down|month|wise|average|avg|compare|versus|\bvs\b|"
        r"include|combine|merge|group|remove|exclude|packing|product|sku|"
        r"city|cities|total|all sales|so far|mtd|"
        r"expected|forecast|projected|dumping|excessive|our|your|my|"
        r"total|overall|company|national"
        r")\b",
        low,
    ):
        return None
    if re.match(r"^(a|an|the|our|your|my)\s+", low):
        return None
    if low in {"our", "your", "my"}:
        return None
    # Reject only when the *entire* name is a client-type alias/label.
    # Party names like "Alpha Dist" contain "dist" but are not types.
    from eva_dashboard.client_language import CLIENT_TYPE_ALIASES, _norm as _ct_norm

    name_key = _ct_norm(name)
    if name_key in CLIENT_TYPE_ALIASES:
        return None
    from eva_dashboard.client_language import list_known_client_types

    if any(_ct_norm(k) == name_key for k in list_known_client_types()):
        return None
    if low in {
        "eva distributors",
        "distributors",
        "distributor",
        "imtiaz",
        "imtiaz store",
        "clients",
        "parties",
        "sales",
        "all",
        "eva consumer",
        "eva bulk",
        "maan consumer",
        "maan bulk",
        "cusine king",
        "cuisine king",
    }:
        return None
    if low in {a.lower() for a in BUSINESS_UNIT_ALIASES}:
        return None
    if low in MONTH_NAMES or re.fullmatch(r"20\d{2}", low):
        return None
    cities = {
        "lahore",
        "karachi",
        "islamabad",
        "faisalabad",
        "multan",
        "peshawar",
        "rawalpindi",
        "gujranwala",
        "sialkot",
        "hyderabad",
        "quetta",
    }
    if low in cities:
        return None
    if re.search(r"\bdistributors?\b", low):
        return None
    # Reject business-unit phrases ("Eva Consumer and Eva Bulk")
    bu_names = (
        "eva consumer",
        "eva bulk",
        "maan consumer",
        "maan bulk",
        "cusine king",
        "cuisine king",
    )
    if any(b in low for b in bu_names):
        return None
    if " and " in low or " & " in low:
        return None
    return name


def _looks_named_party_sales(text: str) -> bool:
    """True when the user asks for sales of a specific named client/party."""
    t = (text or "").lower()
    if _looks_party_breakdown(t):
        return False
    if _looks_period_only_followup(t) and not _extract_named_party_query(text):
        return False
    if re.search(
        r"\b(sales?|volume|mt)\b.+\b(for|of)\b.+\b(client|party|distributor|customer)\b",
        t,
    ):
        return _extract_named_party_query(text) is not None
    if re.search(
        r"\b(client|party|distributor|customer)\b.+\b(sales?|volume|mt)\b",
        t,
    ):
        return _extract_named_party_query(text) is not None
    if re.search(r"\b(show|give)\b.+\bsales?\b", t):
        return _extract_named_party_query(text) is not None
    if re.search(r"\bsales?\s+(?:for|of)\b", t):
        return _extract_named_party_query(text) is not None
    if re.search(r"\b(were|was|are|is)\b.+\bsales?\b", t):
        return _extract_named_party_query(text) is not None
    return False


def _looks_party_lookup(text: str) -> bool:
    """Fuzzy single-name lookup only (not lists / rankings)."""
    t = (text or "").lower()
    if _looks_named_party_sales(text):
        return False
    if _looks_client_list(t) or _looks_party_analytics(t):
        return False
    return bool(
        re.search(
            r"\b(who\s+is|who'?s|find\s+(the\s+)?client|"
            r"search\s+(for\s+)?(client|party)|lookup\s+(client|party)|"
            r"tell me about)\b",
            t,
        )
    )


def _looks_client_list(text: str) -> bool:
    t = (text or "").lower()
    if _looks_named_party_sales(text):
        return False
    if looks_advanced(text):
        return False
    if _looks_party_mix_query(t):
        return False
    if _looks_party_breakdown(t):
        return True
    if re.search(
        r"\b("
        r"top\s+\d|top\s+(distributors?|parties|clients|imtiaz)|"
        r"who\s+(are|were)\s+the\s+top|highest|grow|growth|doing well|"
        r"share of|percent|% of|ams|"
        r"poorly|poor performance|falling|behind|underperform|not doing well|"
        r"new\s+(parties|clients|distributors)|new in\b|"
        r"lost\s+(parties|clients|distributors)|silent|"
        r"product mix|packing mix|product break|sku[- ]?wise|breakdown|"
        r"invoice|frequency|city league|by volume|vs\s*ams|"
        r"in this|from this|this table|"
        r"reactivated?|reactivation"
        r")\b",
        t,
    ):
        return False
    # "distributor sales in Karachi" is a sales matrix, not a client list
    if re.search(r"\b(sales?|volume|mt)\b", t) and not re.search(
        r"\b(who are|list|individual|[- ]wise)\b",
        t,
    ):
        return False
    return bool(
        re.search(
            r"\b(who are|list|show( me)?)\b.+\b"
            r"(distributors?|clients?|parties|imtiaz|stores?)\b",
            t,
        )
        or re.search(
            r"\b(distributors?|clients?)\b.+\b(in|at)\b.+\b"
            r"(lahore|karachi|islamabad|faisalabad|multan|peshawar)\b",
            t,
        )
    )


def _looks_context_followup(text: str) -> bool:
    """True when the user refers to the previous answer/table (in this / from that)."""
    if _is_explicit_followup(text):
        return True
    t = (text or "").lower()
    return bool(
        re.search(
            r"\b("
            r"in this|from this|in that|from that|this table|that table|"
            r"above|these sales|those sales|in the above|from the above|"
            r"same (period|filters?|scope|table)|for this|"
            r"combine the tables|merge the tables|add bulk|include bulk|"
            r"city[- ]?wise|group by|as columns|remove |exclude |without |"
            r"show this|this by"
            r")\b",
            t,
        )
        or _looks_include_check(t)
        or _looks_combine_tables(t)
        or _looks_regroup(t)
        or _looks_remove(t)
        or _looks_party_breakdown(t)
        or _looks_period_only_followup(t)
    )


def _looks_party_mix_query(text: str) -> bool:
    """True for party/client mix questions (not table row drill-downs)."""
    t = (text or "").lower()
    if re.search(
        r"\b(product mix|packing mix|pack(ing)? mix|category mix|mix for)\b",
        t,
    ):
        return True
    if re.search(r"\b(sku[- ]?wise|product[- ]?wise)\b", t) and re.search(
        r"\b(imtiaz|distributors?|clients?)\b",
        t,
    ):
        return True
    # "product breakdown for distributors/Imtiaz" — not plain BU packing breakdown
    if re.search(
        r"\b(product|packing|pack)\s+break(\s*down|down)?\b.+\b"
        r"(imtiaz|distributors?|clients?|each|per)\b",
        t,
    ):
        return True
    if re.search(
        r"\b(product|packing|pack)\s+break(\s*down|down)?\b.+\b"
        r"for\s+(each|every|per)\b",
        t,
    ):
        return True
    return False


def _looks_per_party_mix(text: str) -> bool:
    """True when mix should be shown per distributor/party (not one aggregate)."""
    t = (text or "").lower()
    if not _looks_party_mix_query(t):
        return False
    return bool(
        re.search(
            r"\b("
            r"for\s+each|each\s+(distributor|party|client)|"
            r"per\s+(distributor|party|client)|"
            r"every\s+(distributor|party|client)|"
            r"(distributors?|parties|clients?)[- ]wise"
            r")\b",
            t,
        )
    )


def _looks_party_growth_rank(text: str) -> bool:
    """Which distributors/parties grew/declined (YoY) — analyze_parties, not filter/sales.

    Leaves segment compares (Imtiaz vs distributors) and % threshold filters
    (grew more than 30%) on advanced_query.
    """
    t = (text or "").lower()
    has_parties = bool(
        re.search(
            r"\b(distributors?|parties|clients?|imtiaz|stores?|customers?)\b",
            t,
        )
    )
    if not has_parties:
        return False
    # Pairwise / multi client-type or city compare stays advanced
    if re.search(r"\bcompar(?:e|ison|ing)\b", t) and (
        re.search(r"\bvs\.?\b|\bversus\b", t)
        or len(extract_all_client_types_from_text(text)) >= 2
    ):
        return False
    # "% threshold" filter_entities (grew more than 30%, declined >10%)
    if re.search(
        r"\b(more than|greater than|over|at least|>)\s*[\d.]+\s*%|"
        r"\b(grown|grew|growth|declined?|dropped).{0,24}"
        r"(more than|greater than|over|at least|>)\s*[\d.]+",
        t,
    ):
        return False
    return bool(
        re.search(
            r"\b("
            r"grown|grew|growth|grow|"
            r"declined?|dropped|fallen|fell|"
            r"vs\.?\s*ams|against ams|relative to ams|"
            r"year over year|\byoy\b|vs\.?\s*last year|versus last year|"
            r"since last year|from last year"
            r")\b",
            t,
        )
    )


def _looks_party_analytics(text: str) -> bool:
    t = (text or "").lower()
    # Sales-table YoY compare is query_sales, not party ranking
    if _looks_sales_yoy_compare(t):
        return False
    # Party growth/AMS rankings beat advanced filter_entities
    if _looks_party_growth_rank(t):
        return True
    # Advanced analytics (compare / filter / days-since / …) wins over rankings
    if looks_advanced(text):
        return False
    # Distributor-wise break of a prior table is list_clients, not rankings
    # (unless growth / AMS / YoY columns were requested — then rank)
    if _looks_party_breakdown(t) and not _looks_party_growth_rank(t):
        return False
    # Row drill-downs ("show by product" / "SKU wise" alone) stay on query_sales
    if _looks_row_drilldown(t) and not _looks_party_mix_query(t):
        return False
    # Plain packing/product breakdown of a BU/city is a sales matrix
    if re.search(
        r"\b(packing|product|oil)\s+break(\s*down|down)\b",
        t,
    ) and not _looks_party_mix_query(t):
        return False
    return bool(
        re.search(
            r"\b("
            r"top\s+\d+|highest sale|highest share|which\s+(imtiaz|distributor)|"
            r"who\s+(are|were)\s+the\s+top|"
            r"top\s+(distributors?|parties|clients|imtiaz|stores?|cities|city)|"
            r"doing well|performing well|performing poorly|poorly|"
            r"falling behind|falling in sales|behind on|not doing well|"
            r"underperform|grew|grown|grow(th)?|"
            r"percent of|% of|share of|vs\s*ams|against ams|"
            r"relative to ams|year over year|\byoy\b|"
            r"parties by|by average|by ams|by volume|"
            r"new\s+(parties|clients|distributors|imtiaz)|new in\b|"
            r"lost\s+(parties|clients|distributors)|silent\s+"
            r"(parties|distributors|clients)|"
            r"product mix|packing mix|pack(ing)? mix|mix for|"
            r"invoices?|invoice frequency|most invoices|by invoices?|"
            r"city league|top\s+\d+\s+cities|rank(ed)? cities|"
            r"top\s+\d+\s+distributors|"
            r"distributors? for|imtiaz stores? selling|"
            r"behind on average|falling behind|bottom\s+\d+|"
            # last year only with party/growth intent
            r"(distributors?|parties|imtiaz).{0,40}last year|"
            r"last year.{0,40}(distributors?|parties|imtiaz|growth|grew|grown)"
            r")\b",
            t,
        )
        or _looks_party_mix_query(t)
        or (
            _looks_context_followup(t)
            and re.search(
                r"\b(distributors?|parties|clients|imtiaz|stores?)\b",
                t,
            )
            and re.search(r"\b(top|best|highest|rank)\b", t)
        )
    )


def _party_filters_from_prior(
    prior_spec: dict[str, Any] | None,
    user_text: str,
) -> dict[str, Any]:
    """Carry city / BU / period / etc. from the last sales table on follow-ups."""
    if not prior_spec:
        return {}
    if not (
        _looks_context_followup(user_text)
        or _looks_party_breakdown(user_text)
        or _looks_period_only_followup(user_text)
        or _looks_party_mix_query(user_text)
        or _looks_which_parties_ask(user_text)
        or _looks_sold_to_parties(user_text)
        or _looks_party_analytics(user_text)
        or _looks_client_list(user_text)
        or _looks_party_growth_rank(user_text)
    ):
        return {}
    pf = prior_spec.get("filters") or {}
    out: dict[str, Any] = {}
    # National / all-over Pakistan asks must not keep a sticky city
    if pf.get("city") and not _looks_national_scope(user_text):
        out["city"] = pf["city"]
    if pf.get("oil_type"):
        out["oil_type"] = pf["oil_type"]
    if pf.get("packing_category"):
        out["packing_category"] = pf["packing_category"]
    if pf.get("client_type"):
        out["client_type"] = pf["client_type"]
    bu = pf.get("business_unit")
    units = list(prior_spec.get("business_units") or [])
    if not bu and len(units) == 1:
        bu = units[0]
    # Growth / national ranking: only keep BU when the user named one
    named_bus = _extract_business_units_from_text(user_text)
    if named_bus:
        out["business_unit"] = named_bus[0]
    elif bu and not (
        _looks_national_scope(user_text) or _looks_party_growth_rank(user_text)
    ):
        out["business_unit"] = bu
    # Prefer explicit period phrase; else fixed dates from prior period
    if prior_spec.get("period_phrase"):
        out["period"] = prior_spec["period_phrase"]
    elif prior_spec.get("period"):
        p = prior_spec["period"] or {}
        if p.get("date_from") and p.get("date_to"):
            out["date_from"] = p["date_from"]
            out["date_to"] = p["date_to"]
            out["period"] = None
    return out


def _looks_sold_to_parties(text: str) -> bool:
    """True for 'which distributor was X sold to' / 'who bought Maan Consumer'."""
    t = (text or "").lower()
    return bool(
        re.search(
            r"\b("
            r"sold\s+to|"
            r"sell(?:s|ing)?\s+to|"
            r"who\s+(bought|purchased|takes?|took)|"
            r"which\s+(distributors?|parties|clients?|customers?)\s+"
            r"(was|were|did|bought|purchased|take|took)|"
            r"(was|were)\s+(the\s+)?.{0,40}\s+sold\s+to|"
            r"(is|are)\s+selling\b|"
            r"\bselling\b.+\b(maan|eva|cusine|cuisine|consumer|bulk)\b|"
            r"buyers?\s+(of|for)|"
            r"who\s+(are|were)\s+the\s+(buyers?|distributors?|parties)\s+"
            r"(of|for|that\s+bought)"
            r")\b",
            t,
        )
    )


def _client_type_mentioned(text: str) -> str | None:
    """Canonical client type if the ask names any known channel/type."""
    return extract_client_type_from_text(text)


def _looks_which_parties_ask(text: str) -> bool:
    """which/what + any client type → identify individual parties in that type.

    Applies to all channels (Distributors, Imtiaz, Metro, Chase Up, CSD, SPAR,
    Food Panda, Gelani, Online, LMT, …), not only distributors/Imtiaz.
    Covers selling-BU / sells-most / active-in / who-are asks.
    """
    t = (text or "").lower()
    if _looks_channel_growth_ask(t) or looks_advanced(text):
        return False
    # Plain "X sales" matrix (e.g. Imtiaz sales / Metro sales) — not party list
    if re.search(
        r"\b(distributors?|imtiaz|metro|chase\s*up|spar|gelani|csd|"
        r"food\s*panda|online|lmt)\s+sales\b",
        t,
    ) and not re.search(r"\b(selling|sells)\b", t):
        return False
    if re.search(r"\bchannels?\b", t) and not _client_type_mentioned(text):
        return False
    # Leave AMS / growth / share rankings to analyze_parties heuristics
    if re.search(
        r"\b("
        r"ams|vs\s*ams|against ams|falling behind|behind on|underperform|"
        r"grew|growth|yoy|year over year|share of|percent|% of|"
        r"new\s+(parties|clients|distributors)|lost\s+(parties|clients)|"
        r"silent|not ordered|days since|invoice frequency"
        r")\b",
        t,
    ):
        return False

    ctype = _client_type_mentioned(text)
    generic_parties = bool(
        re.search(r"\b(parties|clients?|customers?)\b", t)
    )
    if not ctype and not generic_parties:
        return False

    identify = bool(
        re.search(
            r"\b("
            r"selling|sells|sold|active|bought|buyers?|"
            r"sells?\s+the\s+most|who\s+sells|"
            r"who\s+are|who\s+were"
            r")\b",
            t,
        )
    )
    which_word = bool(re.search(r"\b(which|what|who)\b", t))
    # "which Metro" / "what Chase Up" / "who are the distributors"
    which_type = which_word and (bool(ctype) or generic_parties)
    return bool(
        identify
        and (which_type or re.search(r"\b(selling|sells|active)\b", t))
    )


def _looks_party_rank_ask(text: str) -> bool:
    """Rank individuals (sells the most / top / highest)."""
    t = (text or "").lower()
    return bool(
        re.search(
            r"\b("
            r"most|top\s+\d*|highest|best|rank|largest|biggest|"
            r"sells?\s+the\s+most|highest\s+(sale|volume|mt|vtf)"
            r")\b",
            t,
        )
    )


def _looks_party_breakdown(text: str) -> bool:
    """True for individual-distributor break / sold-to / buyers of a BU."""
    t = (text or "").lower()
    # Product/packing mix for each distributor is analyze_parties, not a list
    if _looks_party_mix_query(t):
        return False
    # Growth / AMS / YoY on individuals → ranked analyze_parties table
    if _looks_party_growth_rank(t):
        return False
    if _looks_sold_to_parties(t):
        return True
    return bool(
        re.search(
            r"\b("
            r"individual\s+distributors?|"
            r"by\s+(individual\s+)?(distributors?|parties|clients?|party)|"
            r"(distributors?|parties|clients?)[- ]wise|"
            r"break(\s*down|down)?\s+(by|of)\s+(the\s+)?"
            r"(distributors?|parties|clients?)|"
            r"break\s+of\s+(the\s+)?(distributors?|parties|clients?)"
            r")\b",
            t,
        )
    )


def _extract_period_phrase(text: str) -> str | None:
    """Pull a resolvable period phrase (e.g. 'July', 'July 2026', 'this month')."""
    from eva_dashboard.sales_query import MONTH_NAMES
    import calendar as _cal

    t = (text or "").lower()
    for phrase in (
        "this month",
        "last month",
        "this week",
        "last week",
        "so far",
        "mtd",
    ):
        if phrase in t:
            return phrase
    year_m = re.search(r"(20\d{2})", t)
    year = year_m.group(1) if year_m else None
    month_num = None
    for name, num in MONTH_NAMES.items():
        if re.search(rf"\b{re.escape(name)}\b", t):
            month_num = num
            break
    if month_num is None:
        return None
    nice = _cal.month_name[month_num]
    return f"{nice} {year}" if year else nice


def _looks_period_only_followup(text: str) -> bool:
    """True when the user only changes the period (e.g. 'show July only')."""
    t = (text or "").lower().strip()
    if not _extract_period_phrase(text):
        return False
    if _looks_regroup(t) or _looks_remove(t) or _looks_include_check(t):
        return False
    if _looks_combine_tables(t):
        return False
    if re.search(
        r"\b(by |wise|remove |exclude |group by|sku|packing|product break|"
        r"add |compare|yoy|vs last year)\b",
        t,
    ):
        return False
    if re.search(r"\b(only|just)\b", t):
        return True
    cleaned = t
    for phrase in (
        "show", "give me", "for", "in", "please", "can you", "sales", "sale",
        "the", "a", "me",
    ):
        cleaned = re.sub(rf"\b{phrase}\b", " ", cleaned)
    period = _extract_period_phrase(text)
    if period:
        for tok in period.lower().split():
            cleaned = re.sub(rf"\b{re.escape(tok)}\b", " ", cleaned)
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned).strip()
    return len(cleaned.split()) == 0


def _last_party_spec(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the most recent list_clients / analyze_parties party_spec."""
    for m in reversed(messages):
        if m.get("role") == "assistant":
            spec = (m.get("_eva_followup") or {}).get("party_spec")
            if spec:
                return spec
        if m.get("role") != "tool":
            continue
        raw = m.get("content") or ""
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("party_spec"):
            return payload["party_spec"]
        if isinstance(payload, dict) and payload.get("mode") == "list_clients":
            return {
                "kind": "list_clients",
                "filters": payload.get("filters") or {},
                "period": payload.get("period"),
                "period_phrase": None,
                "limit": payload.get("count") or 200,
            }
    return None


def _replay_party_spec(
    party_spec: dict[str, Any],
    *,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Re-run list_clients / analyze_parties with an optional new period."""
    filters = dict(party_spec.get("filters") or {})
    kind = party_spec.get("kind") or "list_clients"
    p_phrase = period
    d0 = date_from
    d1 = date_to
    if not p_phrase and not d0:
        if party_spec.get("period_phrase"):
            p_phrase = party_spec["period_phrase"]
        else:
            p = party_spec.get("period") or {}
            d0, d1 = p.get("date_from"), p.get("date_to")
    if kind == "analyze_parties":
        return analyze_parties(
            period=p_phrase,
            date_from=d0,
            date_to=d1,
            city=filters.get("city"),
            client_type=filters.get("client_type"),
            business_unit=filters.get("business_unit"),
            oil_type=filters.get("oil_type"),
            packing_category=filters.get("packing_category"),
            metric=party_spec.get("metric") or "volume",
            limit=int(party_spec.get("limit") or 100),
            group_by=party_spec.get("group_by") or "party",
        )
    return list_clients(
        city=filters.get("city"),
        client_type=filters.get("client_type"),
        business_unit=filters.get("business_unit"),
        period=p_phrase,
        date_from=d0,
        date_to=d1,
        limit=int(party_spec.get("limit") or 200),
        include_zero=bool(party_spec.get("include_zero")),
    )


def _looks_cost_factor_ask(text: str) -> bool:
    """Cost factor / packing cost / factor breakdown (not volume sales)."""
    t = (text or "").lower()
    return bool(
        re.search(
            r"\b("
            r"cost\s*factors?|factor\s*costs?|total\s*factor|"
            r"packing\s*costs?|product\s*costs?|"
            r"factor\s*break\s*down|factor\s*breakdown|"
            r"show\s+factors?|what'?s\s+the\s+factor|"
            r"what\s+is\s+the\s+(cost\s*)?factor|"
            r"factor\s+for\b"
            r")\b",
            t,
        )
    )


def _looks_factor_breakdown_ask(text: str) -> bool:
    t = (text or "").lower()
    return bool(
        re.search(
            r"\b("
            r"factor\s*break\s*down|factor\s*breakdown|"
            r"product\s*cost|packing\s*cost|"
            r"split\s+(the\s+)?factor|break\s+down\s+(the\s+)?factor"
            r")\b",
            t,
        )
    )


def _looks_factor_only_ask(text: str) -> bool:
    """Pure factor lookup — no rate/Price Fetch / sales period required."""
    t = (text or "").lower()
    if not _looks_cost_factor_ask(t) and not _looks_factor_breakdown_ask(t):
        return False
    if re.search(
        r"\b(price\s*fetch|avg\.?\s*rate|average\s+rate|average\s+price|"
        r"selling\s+price|\brate\b)\b",
        t,
    ):
        return False
    return True


def _looks_price_query(text: str) -> bool:
    t = (text or "").lower()
    if _looks_party_lookup(t) or _looks_client_list(t) or _looks_party_analytics(t):
        return False
    # "average sale" is volume, not rate
    if re.search(r"\baverage\s+sales?\b|\bavg\.?\s+sales?\b", t):
        return False
    if _looks_cost_factor_ask(t) or _looks_factor_breakdown_ask(t):
        return True
    return bool(
        re.search(
            r"\b(price\s*fetch|price fetch|avg\.?\s*rate|average\s+rate|"
            r"average\s+price|avg\.?\s*price|\brate\b|\bpriced?\b|"
            r"what'?s\s+the\s+price|selling\s+price)\b",
            t,
        )
    )


def _looks_price_fetch_followup(text: str) -> bool:
    t = (text or "").lower()
    return bool(re.search(r"\bprice\s*fetch\b|\brecovery\b", t))


def _extract_business_units_from_text(text: str) -> list[str]:
    t = text or ""
    found: list[str] = []
    lower = t.lower()
    # Longer names first
    candidates = sorted(_KNOWN_BUSINESS_UNITS, key=len, reverse=True)
    for name in candidates:
        if name.lower() in lower:
            from eva_dashboard.categories import BUSINESS_UNIT_ALIASES

            norm = BUSINESS_UNIT_ALIASES.get(name.lower(), name)
            if name.lower() == "cuisine king":
                norm = "Cusine King"
            if norm not in found:
                found.append(norm)
    # Informal "eva bulk" / "maan consumer"
    informal = [
        ("eva consumer", "Eva Consumer"),
        ("eva bulk", "Eva Bulk"),
        ("maan consumer", "Maan Consumer"),
        ("maan bulk", "Maan Bulk"),
        ("cusine king", "Cusine King"),
        ("cuisine king", "Cusine King"),
    ]
    for needle, label in informal:
        if needle in lower and label not in found:
            found.append(label)
    # Bare brand shorthand: "selling maan" → Maan Consumer
    if re.search(r"\bmaan\b", lower) and "Maan Consumer" not in found and (
        "Maan Bulk" not in found
    ):
        if re.search(r"\bmaan\s+bulk\b", lower):
            found.append("Maan Bulk")
        else:
            found.append("Maan Consumer")
    return found


def _months_back_from_text(text: str, default: int = 6) -> int:
    m = re.search(r"\b(?:last|past|previous)\s+(\d{1,2})\s+months?\b", (text or "").lower())
    if m:
        return max(1, min(24, int(m.group(1))))
    return default


def _last_table_spec(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the most recent query_sales table_spec (tool result or Reply meta)."""
    for m in reversed(messages):
        if m.get("role") == "assistant":
            spec = (m.get("_eva_followup") or {}).get("table_spec")
            if spec:
                return spec
        if m.get("role") != "tool":
            continue
        raw = m.get("content") or ""
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("table_spec"):
            return payload["table_spec"]
        # Compact tool payload may only have table_spec nested after shrink —
        # also accept top-level keys we keep
        if isinstance(payload, dict) and payload.get("mode") and (
            payload.get("filters") or payload.get("business_units") is not None
        ):
            # Rebuild minimal spec from compact result
            return {
                "period": payload.get("period"),
                "filters": payload.get("filters") or {},
                "business_units": payload.get("business_units") or [],
                "column_dimension": payload.get("column_dimension")
                or (payload.get("table_spec") or {}).get("column_dimension"),
                "row_dimension": payload.get("row_dimension"),
                "months_back": (payload.get("table_spec") or {}).get("months_back"),
            }
    return None


def _last_price_spec(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for m in reversed(messages):
        if m.get("role") == "assistant":
            spec = (m.get("_eva_followup") or {}).get("price_spec")
            if spec:
                return spec
        if m.get("role") != "tool":
            continue
        raw = m.get("content") or ""
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("price_spec"):
            return payload["price_spec"]
        if isinstance(payload, dict) and payload.get("avg_rate") is not None:
            return {
                "period": payload.get("period"),
                "filters": payload.get("filters") or {},
                "include_price_fetch": bool(payload.get("include_price_fetch")),
            }
    return None


def _prune_session_messages(working: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Persist only user/assistant turns (+ follow-up meta) — drop tool blobs."""
    out: list[dict[str, Any]] = []
    for m in working:
        if m.get("role") not in {"user", "assistant"}:
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        entry: dict[str, Any] = {"role": m["role"], "content": m["content"]}
        if m.get("_eva_followup"):
            entry["_eva_followup"] = m["_eva_followup"]
        out.append(entry)
    return out



def _dispatch_advanced(arguments: dict, user_text: str, prior_spec=None):
    from eva_dashboard.advanced_analytics import (
        compare_segments,
        week_over_week,
        rank_dimension_growth,
        mix_or_share,
        silent_parties,
        not_ordered,
        reactivated_parties,
        days_since_last_invoice,
        party_profile,
        detect_dumping,
        top_skus,
        filter_entities,
    )
    from eva_dashboard.seasonality import expected_month_close
    from eva_dashboard.advanced_routing import infer_advanced_from_text

    inferred = infer_advanced_from_text(user_text)
    mode = arguments.get("mode") or inferred.get("mode")
    city = arguments.get("city") or inferred.get("city")
    ctype = normalize_client_type(
        arguments.get("client_type") or inferred.get("client_type")
    )
    bu = arguments.get("business_unit") or inferred.get("business_unit")
    oil = arguments.get("oil_type") or inferred.get("oil_type")
    pack = arguments.get("packing_category") or inferred.get("packing_category")
    period = arguments.get("period") or inferred.get("period")
    exclude = arguments.get("exclude_client_types") or inferred.get("exclude_client_types")
    limit = int(arguments.get("limit") or inferred.get("limit") or 10)
    metric = arguments.get("metric") or inferred.get("metric") or "volume"
    left = arguments.get("left") or inferred.get("left")
    right = arguments.get("right") or inferred.get("right")
    entities = arguments.get("entities") or inferred.get("entities")
    if isinstance(entities, str):
        entities = [e.strip() for e in entities.split(",") if e.strip()]
    elif entities is not None:
        entities = [str(e).strip() for e in entities if str(e).strip()]
    group_by = arguments.get("group_by") or inferred.get("group_by")
    party_query = arguments.get("party_query") or inferred.get("party_query") or user_text
    entity = arguments.get("entity") or inferred.get("entity") or "party"
    op = arguments.get("op") or inferred.get("op")
    threshold = arguments.get("threshold")
    if threshold is None:
        threshold = inferred.get("threshold")

    # Inherit filters from prior sales table when user says dump break-down follow-up
    if prior_spec and inferred.get("mode") == "dumping" and group_by:
        pf = prior_spec.get("filters") or {}
        city = city or pf.get("city")
        ctype = ctype or pf.get("client_type")
        bu = bu or pf.get("business_unit")
        oil = oil or pf.get("oil_type")
        pack = pack or pf.get("packing_category")

    if mode in {"compare_cities", "compare_client_types"}:
        return compare_segments(
            segment="city" if mode == "compare_cities" else "client_type",
            left=left or (None if entities else "Lahore"),
            right=right or (None if entities else "Karachi"),
            entities=entities,
            metric=metric,
            period=period,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            client_type=ctype if mode == "compare_cities" else None,
            city=city if mode != "compare_cities" else None,
            exclude_client_types=exclude,
        )
    if mode == "week_over_week":
        return week_over_week(
            city=city, client_type=ctype, business_unit=bu, oil_type=oil,
            packing_category=pack, exclude_client_types=exclude,
        )
    if mode == "dimension_growth":
        return rank_dimension_growth(
            dimension=inferred.get("dimension") or "packing_category",
            period=period, city=city, client_type=ctype, business_unit=bu,
            oil_type=oil, packing_category=pack, exclude_client_types=exclude,
            limit=limit, sort=inferred.get("sort") or "desc",
        )
    if mode == "expected_month":
        return expected_month_close(
            period=period or "this month", city=city, client_type=ctype,
            business_unit=bu, oil_type=oil, packing_category=pack,
            exclude_client_types=exclude,
        )
    if mode in {"silent_week", "silent_parties"}:
        return silent_parties(
            grain="week", period=period or "this week", city=city,
            client_type=ctype, business_unit=bu, oil_type=oil,
            packing_category=pack, exclude_client_types=exclude, limit=limit,
        )
    if mode == "not_ordered":
        return not_ordered(
            period=period or "this month", city=city, client_type=ctype,
            business_unit=bu, oil_type=oil, packing_category=pack,
            exclude_client_types=exclude, limit=limit,
        )
    if mode == "reactivated":
        return reactivated_parties(
            city=city, client_type=ctype, business_unit=bu,
            exclude_client_types=exclude, limit=limit,
        )
    if mode == "days_since_invoice":
        return days_since_last_invoice(
            city=city, client_type=ctype, business_unit=bu,
            packing_category=pack, exclude_client_types=exclude, limit=limit,
        )
    if mode in {"concentration", "concentration_growth", "oil_mix",
                "packing_contribution", "packing_share_of_party"}:
        return mix_or_share(
            mode=("concentration" if mode == "concentration_growth" else mode),
            period=period, city=city, client_type=ctype, business_unit=bu,
            oil_type=oil, packing_category=pack, exclude_client_types=exclude,
            metric="growth" if mode == "concentration_growth" else metric,
            limit=limit,
        )
    if mode == "top_skus":
        return top_skus(
            period=period, city=city, client_type=ctype, business_unit=bu,
            oil_type=oil, packing_category=pack, exclude_client_types=exclude,
            limit=limit, sort=inferred.get("sort") or "desc",
        )
    if mode == "party_profile":
        return party_profile(query=party_query, period=period)
    if mode == "dumping":
        return detect_dumping(
            period=period, city=city, client_type=ctype, business_unit=bu,
            oil_type=oil, packing_category=pack, exclude_client_types=exclude,
            group_by=group_by, limit=limit,
        )
    if mode == "filter_entities":
        thr = None if threshold is None else float(threshold)
        return filter_entities(
            entity=entity,
            metric=metric,
            op=op or "gt",
            threshold=thr,
            period=period,
            city=city,
            client_type=ctype,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            exclude_client_types=exclude,
            limit=max(limit, 50) if limit < 50 else limit,
        )
    return {"ok": False, "error": f"Unknown advanced mode: {mode}"}


def _dispatch_channel_growth(
    user_text: str,
    *,
    prior_spec: dict[str, Any] | None = None,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Client-type Volume + AMS + % for channel grew/declined asks."""
    args = arguments or {}
    pf = (prior_spec or {}).get("filters") or {}
    mentioned = _extract_business_units_from_text(user_text)
    prior_units = _prior_units_list(prior_spec) if prior_spec else []
    units = mentioned or prior_units
    period = (
        args.get("period")
        or _extract_period_phrase(user_text)
        or (prior_spec or {}).get("period_phrase")
    )
    date_from = args.get("date_from")
    date_to = args.get("date_to")
    if not period and not date_from and prior_spec:
        p = prior_spec.get("period") or {}
        date_from = p.get("date_from")
        date_to = p.get("date_to")
    if not period and not date_from:
        period = "this month"
    city = (
        args.get("city")
        or extract_city_from_text(user_text)
        or pf.get("city")
    )
    oil = (
        args.get("oil_type")
        or extract_oil_type_from_text(user_text)
        or pf.get("oil_type")
    )
    pack = (
        args.get("packing_category")
        or extract_packing_from_text(user_text)
        or pf.get("packing_category")
    )
    if len(units) == 1:
        bu_param, bus_param = units[0], None
    elif len(units) > 1:
        bu_param, bus_param = None, units
    else:
        bu_param = pf.get("business_unit")
        bus_param = None
    return query_sales(
        period=period,
        date_from=date_from,
        date_to=date_to,
        city=city,
        business_unit=bu_param,
        business_units=bus_param,
        oil_type=oil,
        packing_category=pack,
        client_type=None,
        columns="city",
        mode="trend",
        row_dimension="client_type",
        clear_filters=["client_type"],
        prior_spec=None,
    )


def _wants_party_month_matrix(
    user_text: str,
    prior_spec: dict[str, Any] | None,
) -> bool:
    """Selling-BU / buyers asks should mirror a prior month sales table."""
    if not prior_spec:
        return False
    if str(prior_spec.get("column_dimension") or "") != "month":
        return False
    if _looks_party_rank_ask(user_text):
        return False
    t = (user_text or "").lower()
    selling = bool(
        re.search(r"\b(selling|sells|sold\s+to|buyers?|bought)\b", t)
    )
    has_bu = bool(_extract_business_units_from_text(user_text))
    return selling and has_bu


def _dispatch_which_parties(
    user_text: str,
    *,
    prior_spec: dict[str, Any] | None = None,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Identify individual parties in a client type for which/what asks."""
    args = arguments or {}
    inferred = infer_party_analytics_from_text(user_text)
    prior_ctx = _party_filters_from_prior(prior_spec, user_text)
    t = (user_text or "").lower()

    ctype = normalize_client_type(
        args.get("client_type")
        or inferred.get("client_type")
        or _client_type_mentioned(user_text)
        or prior_ctx.get("client_type")
    )
    city_from_text = (
        args.get("city")
        or inferred.get("city")
        or extract_city_from_text(user_text)
    )
    prior_ctype = normalize_client_type(prior_ctx.get("client_type"))
    # Switching client type (Distributors → Imtiaz) must not keep prior city
    if _looks_national_scope(user_text):
        city = None
    elif city_from_text:
        city = city_from_text
    elif ctype and prior_ctype and ctype != prior_ctype:
        city = None
    else:
        city = prior_ctx.get("city")
    units = _extract_business_units_from_text(user_text)
    bu = (
        units[0]
        if len(units) == 1
        else (
            inferred.get("business_unit")
            or prior_ctx.get("business_unit")
        )
    )
    oil = (
        args.get("oil_type")
        or inferred.get("oil_type")
        or extract_oil_type_from_text(user_text)
        or prior_ctx.get("oil_type")
    )
    pack = (
        args.get("packing_category")
        or inferred.get("packing_category")
        or extract_packing_from_text(user_text)
        or prior_ctx.get("packing_category")
    )
    period = (
        args.get("period")
        or _extract_period_phrase(user_text)
        or inferred.get("period")
        or prior_ctx.get("period")
    )
    date_from = args.get("date_from") or prior_ctx.get("date_from")
    date_to = args.get("date_to") or prior_ctx.get("date_to")

    # Rank / oil / packing filters need analyze_parties (list_clients is BU/city/type only)
    if _looks_party_rank_ask(user_text) or oil or pack:
        return analyze_parties(
            period=period,
            date_from=date_from,
            date_to=date_to,
            city=city,
            client_type=ctype,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            metric="volume",
            sort="desc",
            limit=int(args.get("limit") or inferred.get("limit") or 10),
            group_by="party",
        )

    # Mirror prior month table: distributors as rows × months as columns
    if _wants_party_month_matrix(user_text, prior_spec):
        months_back = int((prior_spec or {}).get("months_back") or 6)
        return query_sales(
            city=city,
            client_type=ctype,
            business_unit=bu,
            oil_type=oil,
            packing_category=pack,
            columns="month",
            months_back=months_back,
            mode="matrix",
            row_dimension="party",
            lock_columns=True,
            period=period,
            date_from=date_from,
            date_to=date_to,
            prior_spec=None,
        )

    return list_clients(
        city=city,
        client_type=ctype,
        business_unit=bu,
        period=period,
        date_from=date_from,
        date_to=date_to,
        limit=int(args.get("limit") or inferred.get("limit") or 200),
    )


def _dispatch_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    user_text: str = "",
    prior_spec: dict[str, Any] | None = None,
    prior_price_spec: dict[str, Any] | None = None,
    prior_party_spec: dict[str, Any] | None = None,
) -> Any:
    # Channels (= client types) grew / declined → lean Volume + AMS + %
    if _looks_channel_growth_ask(user_text):
        return _dispatch_channel_growth(
            user_text, prior_spec=prior_spec, arguments=arguments
        )

    # which/what + distributors|Imtiaz → individual parties (selling BU / most VTF / active)
    if _looks_which_parties_ask(user_text) or (
        _looks_sold_to_parties(user_text)
        and (
            _extract_business_units_from_text(user_text)
            or prior_spec
            or extract_client_type_from_text(user_text)
        )
    ):
        return _dispatch_which_parties(
            user_text, prior_spec=prior_spec, arguments=arguments
        )

    # Named client/party sales (do NOT inherit prior city/client_type)
    if _looks_named_party_sales(user_text):
        pq = _extract_named_party_query(user_text) or user_text
        period = _extract_period_phrase(user_text)
        # Named month → Volume + AMS + %change; else last 6 months + AMS grid.
        if period and (
            _wants_named_month_trend(user_text) or _looks_single_month_only(user_text)
        ):
            return party_sales(
                query=pq, period=period, columns="city", mode="trend"
            )
        months_back = _months_back_from_text(user_text, 6)
        return party_sales(
            query=pq,
            period=None,
            columns="month",
            months_back=months_back,
            mode="matrix",
        )

    # Period-only follow-up on a party/distributor list → stay on that view
    if prior_party_spec and _looks_period_only_followup(user_text):
        new_period = _extract_period_phrase(user_text)
        return _replay_party_spec(prior_party_spec, period=new_period)

    # Individual distributors / "sold to" / buyers of a BU → party list
    if (
        name
        in {"query_sales", "list_clients", "analyze_parties", "lookup_party"}
        and _looks_party_breakdown(user_text)
        and (prior_spec or _extract_business_units_from_text(user_text))
    ):
        prior_ctx = _party_filters_from_prior(prior_spec, user_text) if prior_spec else {}
        units = _extract_business_units_from_text(user_text)
        bu = (
            units[0]
            if len(units) == 1
            else (
                arguments.get("business_unit")
                or prior_ctx.get("business_unit")
            )
        )
        ctype = normalize_client_type(
            arguments.get("client_type")
            or prior_ctx.get("client_type")
            or (
                "Eva Distributors"
                if re.search(r"\bdistributors?\b", (user_text or "").lower())
                else None
            )
        )
        city = (
            arguments.get("city")
            or extract_city_from_text(user_text)
            or prior_ctx.get("city")
        )
        return list_clients(
            city=city,
            client_type=ctype,
            business_unit=bu,
            period=(
                arguments.get("period")
                or _extract_period_phrase(user_text)
                or prior_ctx.get("period")
            ),
            date_from=arguments.get("date_from") or prior_ctx.get("date_from"),
            date_to=arguments.get("date_to") or prior_ctx.get("date_to"),
            limit=int(arguments.get("limit") or 200),
        )

    # v0.4.2: wrong tool under required → still honor show-me X sales defaults
    if _should_redirect_scoped_sales(name, user_text):
        name = "query_sales"

    if name == "query_sales":
        # "Does this include bulk?" — show Bulk-only for prior scope
        if _looks_include_check(user_text) and prior_spec:
            segment = _resolve_include_segment(user_text, prior_spec)
            mode_ic = "analytical" if _looks_analytical(user_text) else "matrix"
            return check_segment_inclusion(
                prior_spec=prior_spec,
                segment=segment or "Eva Bulk",
                mode=mode_ic,
            )

        if _looks_analytical(user_text) and not (
            prior_spec and (_looks_remove(user_text) or _looks_regroup(user_text))
        ):
            mode = "analytical"
        else:
            mode = "matrix"

        columns = arguments.get("columns") or "client_type"
        months_back = int(arguments.get("months_back") or 6)
        # Named month → lean Volume + AMS + %change (not a 6-month grid).
        # Bare "show me X sales" → months + AMS grid (even with stale prior_spec).
        if mode != "analytical" and _wants_named_month_trend(user_text):
            mode = "trend"
            if str(columns).lower().replace(" ", "_") in {
                "month",
                "months",
                "monthly",
                "month_wise",
                "monthwise",
            }:
                columns = "client_type"
        elif mode != "analytical" and _wants_scoped_month_ams(user_text):
            columns = "month"
            months_back = _months_back_from_text(user_text, 6)
            mode = "matrix"
        elif _looks_month_wise(user_text):
            columns = "month"
            months_back = _months_back_from_text(user_text, 6)
            mode = "matrix"

        units = list(arguments.get("business_units") or [])
        if arguments.get("business_unit"):
            units.append(arguments["business_unit"])

        prior_row = (prior_spec or {}).get("row_dimension") if prior_spec else None
        remove = (
            resolve_remove_request(user_text, prior_spec=prior_spec)
            if prior_spec
            else None
        )
        regroup = (
            None
            if remove
            else (
                resolve_regroup_request(user_text, prior_spec=prior_spec)
                if prior_spec
                else None
            )
        )
        # Prefer remove / regroup over packing/SKU drill language when both could match
        row_dim = arguments.get("row_dimension")
        row_groups: list[str] | None = None
        clear_filters: list[str] | None = None
        excludes: dict[str, list[str]] | None = None
        lock_columns = False
        asked_dim = resolve_row_dimension_request(
            user_text, prior_row_dimension=prior_row
        )
        if remove:
            row_dim = remove.get("row_dimension")
            row_groups = list(remove.get("row_groups") or []) or None
            clear_filters = list(remove.get("clear_filters") or []) or None
            excludes = dict(remove.get("excludes") or {}) or None
            columns = str(remove.get("columns") or columns)
            lock_columns = True
            mode = "matrix"
            # "exclude maan and break down by product" → apply packing grain too
            if asked_dim and asked_dim != row_dim:
                row_dim = asked_dim
                # Drop prior SKU groups when collapsing / changing leaf
                if asked_dim != "product":
                    row_groups = [
                        g for g in (row_groups or []) if g != "product"
                    ] or None
        elif regroup:
            row_dim = regroup.get("row_dimension")
            row_groups = list(regroup.get("row_groups") or []) or None
            clear_filters = list(regroup.get("clear_filters") or []) or None
            columns = str(regroup.get("columns") or columns)
            lock_columns = True
            mode = "matrix"
        else:
            # Spoken product/SKU/drill language wins over model-supplied grain
            # ("product wise" must stay packing_category, not SKU).
            if asked_dim:
                row_dim = asked_dim
            else:
                row_dim = row_dim or None
        is_same_format = _looks_same_format(user_text)
        is_drill = (not remove and not regroup) and (
            bool(asked_dim)
            or (
                bool(row_dim)
                and not is_same_format
                and not _looks_combine_tables(user_text)
            )
            or _looks_row_drilldown(user_text)
        )
        is_regroup = bool(regroup)
        is_remove = bool(remove)

        # Follow-up: merge mentioned BUs / keep prior table / change row grain / YoY
        is_yoy = _looks_sales_yoy_compare(user_text)
        is_combine = _looks_combine_tables(user_text) or _looks_table_followup(user_text)
        use_prior = prior_spec if (
            (
                is_combine
                or is_drill
                or is_yoy
                or is_regroup
                or is_remove
                or is_same_format
                or _is_explicit_followup(user_text)
            )
            and prior_spec
        ) else None
        if use_prior or is_combine or is_drill or is_yoy or is_regroup or is_remove:
            # IMPORTANT: on remove/exclude, do NOT add named/companion BUs —
            # that inverted "remove Maan Bulk" into filtering TO those units.
            if not is_remove:
                for u in _extract_business_units_from_text(user_text):
                    if u not in units:
                        units.append(u)
                for u in _companion_business_units(user_text, prior_spec):
                    if u not in units:
                        units.append(u)
            else:
                # Keep prior BUs minus the excluded ones (from resolve_remove_request).
                # Empty keep + excludes = show remaining BUs via exclude filter only.
                if remove is not None and "business_units" in remove:
                    units = list(remove.get("business_units") or [])
                elif prior_spec:
                    units = [
                        u
                        for u in _prior_units_list(prior_spec)
                        if u not in set((excludes or {}).get("business_unit") or [])
                    ]
            # Combine after include_check: restore original prior units + segment
            if (
                prior_spec
                and (prior_spec.get("include_check") or is_combine)
                and not is_regroup
                and not is_remove
            ):
                ic = prior_spec.get("include_check") or {}
                base = ic.get("prior_spec") or prior_spec
                for u in _prior_units_list(base):
                    if u not in units:
                        units.insert(0, u)
                seg = ic.get("segment")
                if seg and seg not in units:
                    units.append(seg)
                # Prefer the original table's period/columns when combining
                if ic.get("prior_spec"):
                    use_prior = ic["prior_spec"]
            if prior_spec and not use_prior:
                use_prior = prior_spec
            if (
                use_prior
                and use_prior.get("column_dimension") == "month"
                and not lock_columns
                and mode not in {"trend", "analytical"}
                and not _wants_named_month_trend(user_text)
            ):
                columns = "month"
                months_back = int(use_prior.get("months_back") or months_back)

        # Include-BU / same-format: keep prior packing/SKU grain (ignore model product)
        if use_prior and (
            is_same_format
            or (
                (_looks_combine_tables(user_text) or _looks_table_followup(user_text))
                and not asked_dim
                and not is_remove
                and not is_regroup
                and not _looks_row_drilldown(user_text)
            )
        ):
            prior_keep = normalize_row_dimension(
                use_prior.get("row_dimension")
            ) or use_prior.get("row_dimension")
            if prior_keep:
                row_dim = prior_keep
            if use_prior.get("row_groups") and not row_groups:
                row_groups = list(use_prior.get("row_groups") or []) or None
            if use_prior.get("column_dimension") == "month":
                columns = "month"
                months_back = int(use_prior.get("months_back") or months_back)
                lock_columns = True
                mode = "matrix"

        # Dedupe units preserving order
        seen: set[str] = set()
        uniq: list[str] = []
        for u in units:
            if u and u not in seen:
                seen.add(u)
                uniq.append(u)

        ctype = arguments.get("client_type") or extract_client_type_from_text(user_text)
        ctype = normalize_client_type(ctype) if ctype else None
        oil = arguments.get("oil_type") or extract_oil_type_from_text(user_text)
        pack = arguments.get("packing_category") or extract_packing_from_text(user_text)
        city_arg = arguments.get("city") or extract_city_from_text(user_text)
        period_arg = arguments.get("period") or _extract_period_phrase(user_text)

        # National / all-over Pakistan → drop sticky city
        if _looks_national_scope(user_text):
            city_arg = None
            clear_filters = list(clear_filters or [])
            if "city" not in clear_filters:
                clear_filters.append("city")

        # Same format with a new client type: keep grain, clear prior BU filter
        if is_same_format and ctype and use_prior:
            prior_ct = normalize_client_type(
                (use_prior.get("filters") or {}).get("client_type")
            )
            if prior_ct and ctype != prior_ct and not _extract_business_units_from_text(
                user_text
            ):
                uniq = []
                clear_filters = list(clear_filters or [])
                if "business_unit" not in clear_filters:
                    clear_filters.append("business_unit")

        # Standalone "exclude online / without metro" → client_type excludes
        from eva_dashboard.advanced_routing import extract_exclude_client_types

        excl_types = list(arguments.get("exclude_client_types") or [])
        excl_types.extend(extract_exclude_client_types(user_text) or [])
        if excl_types:
            excludes = dict(excludes or {})
            cur = list(excludes.get("client_type") or [])
            for ct in excl_types:
                canon = normalize_client_type(ct) or ct
                if canon and canon not in cur:
                    cur.append(canon)
            if cur:
                excludes["client_type"] = cur

        # YoY of "these sales": do not invent a client type (e.g. Eva Distributors)
        if is_yoy and use_prior:
            ctype = None
            oil = oil or None
            pack = pack or None
            # Prefer prior BUs only — drop model-invented units not in user text
            if not _extract_business_units_from_text(user_text):
                uniq = []

        # Drop model-invented BUs when user scoped by client type and/or city only.
        mentioned_units = (
            _extract_business_units_from_text(user_text)
            + _companion_business_units(user_text, prior_spec)
        )
        scoped_filter_only = bool((ctype or city_arg) and not mentioned_units)
        if (scoped_filter_only or (is_drill and use_prior and not mentioned_units)) and (
            not is_combine and not is_regroup and not is_remove
        ):
            uniq = []

        # Regroup / remove: do not invent city/client from this short follow-up text
        if is_regroup or is_remove:
            city_arg = None
            if not extract_client_type_from_text(user_text):
                ctype = None
            if is_remove:
                # Units already set from remove keep-list / excludes above
                pass
            elif not _extract_business_units_from_text(user_text):
                # Keep prior BUs via prior_spec unless clearing business_unit
                uniq = []

        if len(uniq) == 1:
            bu_param: str | None = uniq[0]
            bus_param: list[str] | None = None
        elif len(uniq) > 1:
            bu_param = None
            bus_param = uniq
        else:
            bu_param = None
            bus_param = None

        # Preserve prior column grain on YoY follow-up
        if is_yoy and use_prior and use_prior.get("column_dimension") and not lock_columns:
            columns = str(use_prior["column_dimension"])

        # Ignore GPT-invented prior_spec unless this turn is a real table follow-up
        model_prior = arguments.get("prior_spec")
        if model_prior and not (
            use_prior
            or is_combine
            or is_drill
            or is_yoy
            or is_regroup
            or is_remove
            or _is_explicit_followup(user_text)
        ):
            model_prior = None

        return query_sales(
            period=period_arg,
            date_from=arguments.get("date_from"),
            date_to=arguments.get("date_to"),
            city=city_arg,
            business_unit=bu_param,
            business_units=bus_param,
            oil_type=oil,
            packing_category=pack,
            client_type=ctype,
            columns=columns,
            months_back=months_back,
            mode=mode,
            row_dimension=row_dim,
            row_groups=row_groups,
            clear_filters=clear_filters,
            lock_columns=lock_columns,
            excludes=excludes,
            prior_spec=use_prior or model_prior,
            compare="yoy" if is_yoy else (arguments.get("compare") or None),
        )
    if name == "lookup_party":
        # If language is clearly a city+type list, redirect
        if _looks_client_list(user_text):
            inferred = infer_party_analytics_from_text(user_text)
            return list_clients(
                city=inferred.get("city") or arguments.get("city"),
                client_type=inferred.get("client_type")
                or normalize_client_type(arguments.get("client_type")),
                limit=int(arguments.get("limit") or inferred.get("limit") or 200),
            )
        return lookup_party(
            arguments.get("query") or user_text,
            limit=int(arguments.get("limit") or 10),
        )
    if name == "list_clients":
        # Growth / AMS / YoY on individuals must not become a plain MT list
        if _looks_party_growth_rank(user_text):
            return _dispatch_tool(
                "analyze_parties",
                arguments,
                user_text=user_text,
                prior_spec=prior_spec,
                prior_price_spec=prior_price_spec,
                prior_party_spec=prior_party_spec,
            )
        inferred = infer_party_analytics_from_text(user_text)
        prior_ctx = _party_filters_from_prior(prior_spec, user_text)
        # Period-only on a prior party list (when tool still chosen as list_clients)
        if prior_party_spec and _looks_period_only_followup(user_text):
            return _replay_party_spec(
                prior_party_spec, period=_extract_period_phrase(user_text)
            )
        units = _extract_business_units_from_text(user_text)
        bu = (
            units[0]
            if len(units) == 1
            else (
                arguments.get("business_unit")
                or inferred.get("business_unit")
                or prior_ctx.get("business_unit")
            )
        )
        city = (
            arguments.get("city")
            or inferred.get("city")
            or prior_ctx.get("city")
        )
        ctype = normalize_client_type(
            arguments.get("client_type")
            or inferred.get("client_type")
            or prior_ctx.get("client_type")
            or (
                "Eva Distributors"
                if re.search(r"\bdistributors?\b", (user_text or "").lower())
                else None
            )
        )
        period = (
            _extract_period_phrase(user_text)
            if (
                _looks_period_only_followup(user_text)
                or _looks_sold_to_parties(user_text)
            )
            else None
        )
        return list_clients(
            city=city,
            client_type=ctype,
            business_unit=bu,
            period=period
            or arguments.get("period")
            or inferred.get("period")
            or prior_ctx.get("period"),
            date_from=arguments.get("date_from") or prior_ctx.get("date_from"),
            date_to=arguments.get("date_to") or prior_ctx.get("date_to"),
            limit=int(arguments.get("limit") or inferred.get("limit") or 200),
        )
    if name == "advanced_query":
        return _dispatch_advanced(arguments, user_text, prior_spec=prior_spec)
    if name == "analyze_parties":
        inferred = infer_party_analytics_from_text(user_text)
        prior_ctx = _party_filters_from_prior(prior_spec, user_text)
        # Mix / ranking follow-up after a party list: inherit that list's filters
        if (
            not prior_ctx
            and prior_party_spec
            and (
                _looks_party_mix_query(user_text)
                or _looks_context_followup(user_text)
            )
        ):
            prior_ctx = _party_filters_from_prior(
                {
                    "filters": dict(prior_party_spec.get("filters") or {}),
                    "period_phrase": prior_party_spec.get("period_phrase"),
                    "period": prior_party_spec.get("period"),
                    "business_units": [],
                },
                user_text,
            )
        # Context follow-up ("in this"): match the sales table numbers → volume
        metric = (
            arguments.get("metric")
            or inferred.get("metric")
            or ("volume" if prior_ctx else None)
            or "ams"
        )
        # Mix language always wins — never fall back to volume/AMS tops
        if _looks_party_mix_query(user_text):
            inferred_m = str(inferred.get("metric") or "").strip().lower()
            arg_m = str(arguments.get("metric") or "").strip().lower()
            if inferred_m in {"packing_mix", "product_mix"}:
                metric = inferred_m
            elif arg_m in {
                "packing_mix",
                "product_mix",
                "product_breakdown",
                "pack_mix",
                "sku_mix",
                "sku_breakdown",
                "sku_wise",
            }:
                metric = (
                    "product_mix"
                    if arg_m in {"product_mix", "sku_mix", "sku_breakdown", "sku_wise"}
                    else "packing_mix"
                )
            else:
                metric = "packing_mix"
        if (
            prior_ctx
            and metric == "ams"
            and (inferred.get("metric") or "ams") == "ams"
            and not arguments.get("metric")
            and not _looks_party_mix_query(user_text)
        ):
            if not re.search(
                r"\b(ams|average (monthly )?sale|growth|yoy|vs\s*ams)\b",
                (user_text or "").lower(),
            ):
                metric = "volume"
        per_party_mix = bool(
            arguments.get("per_party_mix")
            or inferred.get("per_party_mix")
            or _looks_per_party_mix(user_text)
        )
        # Per-party mix: show enough distributors (not default top-10 AMS)
        mix_limit = int(arguments.get("limit") or inferred.get("limit") or 10)
        if per_party_mix and not arguments.get("limit") and not inferred.get("limit"):
            mix_limit = 16
        named_bus = _extract_business_units_from_text(user_text)
        # Prefer spoken BU; do not trust model-invented business_unit on growth asks
        bu_arg = (
            named_bus[0]
            if named_bus
            else (
                inferred.get("business_unit")
                or (
                    None
                    if (
                        _looks_national_scope(user_text)
                        or _looks_party_growth_rank(user_text)
                    )
                    else (
                        arguments.get("business_unit")
                        or prior_ctx.get("business_unit")
                    )
                )
            )
        )
        city_arg = (
            None
            if _looks_national_scope(user_text)
            else (
                extract_city_from_text(user_text)
                or inferred.get("city")
                or (
                    None
                    if _looks_party_growth_rank(user_text)
                    else arguments.get("city")
                )
                or prior_ctx.get("city")
            )
        )
        return analyze_parties(
            period=arguments.get("period")
            or inferred.get("period")
            or prior_ctx.get("period"),
            date_from=arguments.get("date_from") or prior_ctx.get("date_from"),
            date_to=arguments.get("date_to") or prior_ctx.get("date_to"),
            city=city_arg,
            client_type=normalize_client_type(
                arguments.get("client_type")
                or inferred.get("client_type")
                or prior_ctx.get("client_type")
            ),
            business_unit=bu_arg,
            oil_type=arguments.get("oil_type")
            or inferred.get("oil_type")
            or extract_oil_type_from_text(user_text)
            or prior_ctx.get("oil_type"),
            packing_category=arguments.get("packing_category")
            or inferred.get("packing_category")
            or extract_packing_from_text(user_text)
            or prior_ctx.get("packing_category"),
            brand=arguments.get("brand") or inferred.get("brand"),
            metric=metric,
            compare_period=arguments.get("compare_period")
            or inferred.get("compare_period"),
            share_city=arguments.get("share_city")
            or (
                inferred.get("city")
                if (arguments.get("metric") or inferred.get("metric")) == "geo_share"
                else None
            )
            or extract_city_from_text(user_text),
            group_by=arguments.get("group_by") or inferred.get("group_by") or "party",
            mix_dimension=arguments.get("mix_dimension")
            or inferred.get("mix_dimension"),
            sort=arguments.get("sort") or inferred.get("sort") or "desc",
            limit=mix_limit,
            per_party_mix=per_party_mix,
            grown_only=bool(
                arguments.get("grown_only") or inferred.get("grown_only")
            ),
        )
    if name == "query_price":
        ctype = arguments.get("client_type") or extract_client_type_from_text(user_text)
        oil = arguments.get("oil_type") or extract_oil_type_from_text(user_text)
        pack = arguments.get("packing_category") or extract_packing_from_text(user_text)
        include_pf = bool(arguments.get("include_price_fetch"))
        include_cf = bool(arguments.get("include_cost_factor"))
        factor_bd = bool(arguments.get("factor_breakdown"))
        factor_only = bool(arguments.get("factor_only"))
        if _looks_price_fetch_followup(user_text):
            include_pf = True
        if _looks_cost_factor_ask(user_text) or _looks_factor_breakdown_ask(user_text):
            include_cf = True
        if _looks_factor_breakdown_ask(user_text):
            factor_bd = True
        if _looks_factor_only_ask(user_text) or factor_only:
            factor_only = True
            include_cf = True
        use_prior = arguments.get("prior_spec") or (
            prior_price_spec
            if (
                prior_price_spec
                and (
                    _looks_price_fetch_followup(user_text)
                    or _looks_cost_factor_ask(user_text)
                    or _looks_factor_breakdown_ask(user_text)
                )
            )
            else None
        )
        product_query = arguments.get("product_query")
        if not product_query and not arguments.get("product"):
            product_query = user_text
        if factor_only:
            return query_factor_costs(
                client_type=ctype,
                business_unit=arguments.get("business_unit"),
                oil_type=oil,
                packing_category=pack,
                product=arguments.get("product"),
                product_query=product_query,
                breakdown=True,
                prior_spec=use_prior,
            )
        return query_price(
            period=arguments.get("period") or _extract_period_phrase(user_text),
            date_from=arguments.get("date_from"),
            date_to=arguments.get("date_to"),
            city=arguments.get("city") or extract_city_from_text(user_text),
            business_unit=arguments.get("business_unit"),
            oil_type=oil,
            packing_category=pack,
            client_type=ctype,
            product=arguments.get("product"),
            product_query=product_query,
            include_price_fetch=include_pf,
            include_cost_factor=include_cf,
            factor_breakdown=factor_bd,
            prior_spec=use_prior,
        )
    if name == "get_schema":
        return {"schema": _schema_text()}
    if name == "get_sales_overview":
        return sales_overview()
    if name == "run_sql":
        return run_sql(
            arguments.get("sql", ""),
            limit=int(arguments.get("limit") or MAX_SQL_ROWS),
        )
    if name == "category_mt_totals":
        return category_totals(arguments["date_from"], arguments["date_to"])
    if name == "sales_by_city_and_category":
        return sales_by_city_and_category(
            category1=arguments["category1"],
            city=arguments["city"],
            date_from=arguments["date_from"],
            date_to=arguments["date_to"],
        )
    if name == "list_unmapped_products":
        return unmapped_products(limit=int(arguments.get("limit") or 50))
    if name == "report_snapshot":
        return prepare_report_snapshot(arguments["report_date"])
    if name == "resolve_product_language":
        return resolve_product_language(
            arguments.get("query", ""),
            limit=int(arguments.get("limit") or 8),
        )
    if name == "product_sales":
        return product_sales(
            product=arguments.get("product"),
            product_query=arguments.get("product_query"),
            date_from=arguments["date_from"],
            date_to=arguments["date_to"],
            city=arguments.get("city"),
        )
    return {"error": f"Unknown tool: {name}"}


def _looks_sales_matrix(text: str) -> bool:
    if (
        _looks_party_lookup(text)
        or _looks_price_query(text)
        or _looks_client_list(text)
        or _looks_party_analytics(text)
    ):
        return False
    if _looks_sales_yoy_compare(text):
        return True
    if _looks_row_drilldown(text):
        return True
    t = (text or "").lower()
    sales_keys = (
        "sale", "mt", "lahore", "karachi", "city", "client", "distributor",
        "imtiaz", "store", "eva consumer", "eva bulk", "maan", "last month",
        "this month", "so far", "july", "june", "august", "how are", "how were",
        "doing", "breakdown", "city wise", "city-wise", "ams", "trend",
        "evaluate", "assess", "performance", "packing", "pet", "standup",
        "jerry", "month", "monthly", "add ", "sku", "product", "dissect",
        "faisalabad", "analyze", "analyse", "compare",
    )
    return any(k in t for k in sales_keys)


def _looks_factual(text: str) -> bool:
    t = (text or "").lower()
    keys = (
        "sale", "mt", "july", "june", "202", "lahore", "karachi", "eva", "maan",
        "price", "fetch", "ams", "ads", "city", "client", "cost", "report",
        "how much", "total", "top", "compare", "month", "daily", "mtd",
        "canola", "cooking", "sunflower", "sun ", "vtf", "banaspati", "ghee",
        "shortening", "bake", "cuisine", "cusine", "jerry", "pet", "pouch",
        "pillow", "standup", "stand up", "product", "sku", "tin", "bucket",
        "so far", "doing", "breakdown", "august", "april", "march",
        "evaluate", "assess", "performance", "imtiaz", "distributor", "store",
        "who is", "who's", "who are", "rate", "al bari", "party", "share",
        "percent", "grow", "quarter", "bulk", "include", "combine", "merge",
        "follow-up", "follow up",
        "reactivat", "declin", "invoice", "dump", "silent", "group by",
        "oil type", "packing", "expected", "forecast", "yoy", "wow",
        "week over", "exclude", "without", "remove", "city wise", "sku wise",
        "pakistan", "nationwide", "national", "all over",
        # All trade channels / client types
        "channel", "metro", "chase", "spar", "gelani", "panda", "food panda",
        "csd", "canteen", "online", "lmt", "active", "selling", "sells",
        "which", "what ",
    )
    return any(k in t for k in keys)


def _api_history_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Strip UI-only metadata before sending to the OpenAI API."""
    out: dict[str, Any] = {"role": msg.get("role")}
    if "content" in msg:
        out["content"] = msg.get("content")
    if msg.get("tool_calls"):
        out["tool_calls"] = msg["tool_calls"]
    if msg.get("tool_call_id"):
        out["tool_call_id"] = msg["tool_call_id"]
    if msg.get("name"):
        out["name"] = msg["name"]
    return out


def _compact_assistant_for_api(content: str) -> str:
    """Keep prior answers short in the API payload (tables are huge HTML)."""
    text = content or ""
    if len(text) <= MAX_API_ASSISTANT_CHARS:
        return text
    head = text.split("\n", 1)[0][:240].strip()
    return (
        f"{head}\n\n"
        f"[Prior table omitted from history — {len(text)} chars. "
        "Use Reply / prior_spec for follow-ups on that answer.]\n"
    )


def _working_messages_for_api(working: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a lean OpenAI payload: system + recent turns; compact old tables.

    Current-turn tool messages are kept intact so the model can read results.
    """
    system = next((m for m in working if m.get("role") == "system"), None)
    # Find start of the current tool round (last assistant with tool_calls,
    # or keep trailing tool msgs after the last user).
    last_user_i = max(
        (i for i, m in enumerate(working) if m.get("role") == "user"),
        default=-1,
    )
    current_tail = working[last_user_i:] if last_user_i >= 0 else []
    prior = working[:last_user_i] if last_user_i >= 0 else list(working)

    prior_compact: list[dict[str, Any]] = []
    for m in prior:
        role = m.get("role")
        if role == "system":
            continue
        if role == "tool":
            continue
        if role == "assistant" and m.get("tool_calls") and not (m.get("content") or "").strip():
            continue
        if role in {"user", "assistant"}:
            content = m.get("content") or ""
            if role == "assistant":
                content = _compact_assistant_for_api(content)
            prior_compact.append({"role": role, "content": content})
    if len(prior_compact) > MAX_API_HISTORY_MESSAGES:
        prior_compact = prior_compact[-MAX_API_HISTORY_MESSAGES:]

    out: list[dict[str, Any]] = []
    if system:
        out.append(_api_history_message(system))
    out.extend(prior_compact)
    # Current user + any in-flight assistant/tool messages this turn
    for m in current_tail:
        out.append(_api_history_message(m))
    return out


def _attach_followup_meta(
    messages: list[dict[str, Any]],
    *,
    table_spec: dict[str, Any] | None = None,
    price_spec: dict[str, Any] | None = None,
    party_spec: dict[str, Any] | None = None,
) -> None:
    """Stamp the last assistant turn so the Reply button can pin prior filters."""
    for m in reversed(messages):
        if m.get("role") == "assistant" and (m.get("content") or "").strip():
            meta = dict(m.get("_eva_followup") or {})
            if table_spec:
                meta["table_spec"] = table_spec
            if price_spec:
                meta["price_spec"] = price_spec
            if party_spec:
                meta["party_spec"] = party_spec
            if meta:
                m["_eva_followup"] = meta
            return


def _strip_html_for_csv(text: str) -> str:
    """Flatten HTML/markdown tables into readable plain text for CSV cells."""
    t = text or ""
    t = re.sub(r"(?is)<br\s*/?>", "\n", t)
    t = re.sub(r"(?is)</tr\s*>", "\n", t)
    t = re.sub(r"(?is)</(p|div|h[1-6]|li)\s*>", "\n", t)
    t = re.sub(r"(?is)<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _tools_used_between(
    messages: list[dict[str, Any]],
    *,
    after_idx: int,
    before_idx: int,
) -> list[str]:
    names: list[str] = []
    for m in messages[after_idx + 1 : before_idx + 1]:
        for tc in m.get("tool_calls") or []:
            fn = (tc.get("function") or {}).get("name") if isinstance(tc, dict) else None
            if fn:
                names.append(str(fn))
        if m.get("role") == "tool" and m.get("name"):
            # tool result rows — skip; names come from tool_calls
            pass
    # unique preserve order
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _followup_summary(meta: dict[str, Any] | None) -> str:
    if not meta:
        return ""
    bits: list[str] = []
    for key in ("table_spec", "party_spec", "price_spec"):
        spec = meta.get(key) or {}
        if not isinstance(spec, dict) or not spec:
            continue
        filters = spec.get("filters") or {}
        parts = [f"{k}={v}" for k, v in filters.items() if v]
        if spec.get("column_dimension"):
            parts.append(f"columns={spec.get('column_dimension')}")
        if spec.get("row_dimension"):
            parts.append(f"rows={spec.get('row_dimension')}")
        if spec.get("months_back"):
            parts.append(f"months_back={spec.get('months_back')}")
        if spec.get("period_phrase"):
            parts.append(f"period={spec.get('period_phrase')}")
        elif (spec.get("period") or {}).get("label"):
            parts.append(f"period={(spec.get('period') or {}).get('label')}")
        if parts:
            bits.append(f"{key}: " + "; ".join(parts))
    return " | ".join(bits)


def export_chat_training_csv(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
) -> str:
    """Build a CSV of user↔assistant turns with blank comment columns for training.

    Columns:
      turn_id, exported_at, model, is_followup, user_question, assistant_answer,
      assistant_answer_plain, tools_used, forced_tool_hint, filters_summary,
      comment, rating_1_to_5, expected_answer_notes, preferred_tool
    """
    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fieldnames = [
        "turn_id",
        "exported_at",
        "model",
        "is_followup",
        "user_question",
        "assistant_answer",
        "assistant_answer_plain",
        "tools_used",
        "forced_tool_hint",
        "suggested_tool",
        "filters_summary",
        "comment",
        "rating_1_to_5",
        "expected_answer_notes",
        "preferred_tool",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()

    turn_id = 0
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") != "user":
            i += 1
            continue
        raw_q = str(msg.get("content") or "")
        is_followup = _is_explicit_followup(raw_q)
        question = raw_q
        if is_followup and "\n\n" in raw_q:
            question = raw_q.split("\n\n", 1)[1]
        question = question.strip()

        # Find next assistant message with content
        j = i + 1
        assistant_idx = None
        while j < len(messages):
            if messages[j].get("role") == "assistant" and (
                messages[j].get("content") or ""
            ).strip():
                assistant_idx = j
                break
            if messages[j].get("role") == "user":
                break
            j += 1

        answer = ""
        meta: dict[str, Any] = {}
        tools: list[str] = []
        if assistant_idx is not None:
            answer = str(messages[assistant_idx].get("content") or "")
            meta = dict(messages[assistant_idx].get("_eva_followup") or {})
            tools = _tools_used_between(
                messages, after_idx=i, before_idx=assistant_idx
            )

        q_for_router = (
            f"{FOLLOWUP_MARKER}\n\n{question}" if is_followup else question
        )
        forced_hint = ""
        suggested = ""
        if question:
            try:
                forced_hint = resolve_forced_tool(
                    q_for_router,
                    prior_table_spec=meta.get("table_spec"),
                    prior_party_spec=meta.get("party_spec"),
                    explicit_followup=is_followup,
                )
                suggested = suggest_preferred_tool(
                    q_for_router,
                    prior_table_spec=meta.get("table_spec"),
                    prior_party_spec=meta.get("party_spec"),
                    explicit_followup=is_followup,
                )
            except Exception:
                forced_hint = ""
                suggested = ""

        turn_id += 1
        writer.writerow(
            {
                "turn_id": turn_id,
                "exported_at": exported_at,
                "model": model or DEFAULT_MODEL,
                "is_followup": "yes" if is_followup else "no",
                "user_question": question,
                "assistant_answer": answer,
                "assistant_answer_plain": _strip_html_for_csv(answer),
                "tools_used": "|".join(tools),
                "forced_tool_hint": forced_hint,
                "suggested_tool": suggested,
                "filters_summary": _followup_summary(meta),
                "comment": "",
                "rating_1_to_5": "",
                "expected_answer_notes": "",
                "preferred_tool": "",
            }
        )
        i = (assistant_idx + 1) if assistant_idx is not None else i + 1

    return buf.getvalue()


def suggest_preferred_tool(
    user_text: str,
    *,
    prior_table_spec: dict[str, Any] | None = None,
    prior_party_spec: dict[str, Any] | None = None,
    explicit_followup: bool | None = None,
) -> str:
    """Heuristic preferred tool for eval / CSV hints (does NOT force the API).

    Richer than ``resolve_forced_tool`` — used for golden-set preferred labels
    and training feedback, not for OpenAI tool_choice.
    """
    text = user_text or ""
    if not _looks_factual(text):
        return "auto"

    is_followup = (
        bool(explicit_followup)
        if explicit_followup is not None
        else _is_explicit_followup(text)
    )
    has_table_prior = bool(prior_table_spec or prior_party_spec)
    table_followup = (
        _looks_include_check(text)
        or _looks_combine_tables(text)
        or _looks_regroup(text)
        or _looks_remove(text)
        or _looks_hide_sku(text)
        or _looks_same_format(text)
    ) and (has_table_prior or is_followup)

    if _looks_named_party_sales(text):
        return "lookup_party"
    if prior_party_spec and _looks_period_only_followup(text):
        kind = prior_party_spec.get("kind") or "list_clients"
        return "analyze_parties" if kind == "analyze_parties" else "list_clients"
    if _looks_channel_growth_ask(text):
        return "query_sales"
    if _looks_party_growth_rank(text):
        return "analyze_parties"
    if looks_advanced(text):
        return "advanced_query"
    if _looks_which_parties_ask(text) or _looks_sold_to_parties(text):
        if (
            _looks_party_rank_ask(text)
            or extract_oil_type_from_text(text)
            or extract_packing_from_text(text)
        ):
            return "analyze_parties"
        if _wants_party_month_matrix(text, prior_table_spec):
            return "query_sales"
        return "list_clients"
    if _looks_party_breakdown(text):
        return "list_clients"
    if _looks_party_mix_query(text) or _looks_party_analytics(text):
        return "analyze_parties"
    if table_followup:
        return "query_sales"
    if _looks_client_list(text):
        return "list_clients"
    if _looks_party_lookup(text):
        return "lookup_party"
    if _looks_price_query(text):
        return "query_price"
    if _looks_sales_yoy_compare(text) or _looks_sales_matrix(text):
        return "query_sales"
    return "required"


def resolve_forced_tool(
    user_text: str,
    *,
    prior_table_spec: dict[str, Any] | None = None,
    prior_party_spec: dict[str, Any] | None = None,
    explicit_followup: bool | None = None,
) -> str:
    """Return the forced OpenAI tool_choice for round 0 (v0.4.0 slim router).

    Force only high-confidence cases; otherwise require a tool and let the model
    choose. Values: ``lookup_party``, ``list_clients``, ``analyze_parties``,
    ``query_sales``, ``required``, or ``auto``.
    """
    text = user_text or ""
    if not _looks_factual(text):
        return "auto"

    is_followup = (
        bool(explicit_followup)
        if explicit_followup is not None
        else _is_explicit_followup(text)
    )
    has_table_prior = bool(prior_table_spec or prior_party_spec)
    has_party_prior = bool(prior_party_spec)

    # 1) Named party sales / who-is lookup — high confidence, keep forced
    if _looks_named_party_sales(text) or _looks_party_lookup(text):
        return "lookup_party"

    # 2) Period-only follow-up on a prior party/distributor list
    if has_party_prior and _looks_period_only_followup(text):
        kind = (prior_party_spec or {}).get("kind") or "list_clients"
        return "analyze_parties" if kind == "analyze_parties" else "list_clients"

    # 2b) Channels grew/declined → client_type Volume + AMS + % (even on Reply)
    if _looks_channel_growth_ask(text):
        return "query_sales"

    # 2b2) Rate / Price Fetch / cost factor / packing cost / factor breakdown
    if _looks_price_query(text):
        return "query_price"

    # 2b3) Which distributors grew / vs AMS / YoY — before advanced filter_entities
    if _looks_party_growth_rank(text):
        return "analyze_parties"

    # 2c) High-confidence advanced modes (city/client compares, etc.)
    if looks_advanced(text):
        adv = infer_advanced_from_text(text)
        mode = str(adv.get("mode") or "")
        if mode in {
            "compare_cities",
            "compare_client_types",
            "dumping",
            "expected_month",
            "filter_entities",
            "dimension_growth",
        }:
            return "advanced_query"
        return "required"

    # 2d) which/what distributors|Imtiaz … (selling BU / most VTF / active)
    if _looks_which_parties_ask(text) or _looks_sold_to_parties(text):
        if (
            _looks_party_rank_ask(text)
            or extract_oil_type_from_text(text)
            or extract_packing_from_text(text)
        ):
            return "analyze_parties"
        # Selling a BU after a month table → party × month sales matrix
        if _wants_party_month_matrix(text, prior_table_spec):
            return "query_sales"
        return "list_clients"

    # 3) Reply / pinned-table follow-ups that must stay on the sales table
    table_ops = (
        _looks_include_check(text)
        or _looks_combine_tables(text)
        or _looks_regroup(text)
        or _looks_remove(text)
        or _looks_hide_sku(text)
        or _looks_same_format(text)
        or _looks_row_drilldown(text)
        or _looks_sales_yoy_compare(text)
    )
    if (has_table_prior or is_followup) and table_ops:
        return "query_sales"

    # 4) "By individual distributors" after a sales table → list those parties
    if (has_table_prior or is_followup) and _looks_party_breakdown(text):
        return "list_clients"

    # 5) Reply follow-up: only force sales when it still looks like a matrix ask.
    # Ad-hoc party / advanced questions must not be pinned to query_sales.
    if is_followup and has_table_prior and not has_party_prior:
        if (
            looks_advanced(text)
            or _looks_party_mix_query(text)
            or _looks_party_analytics(text)
            or _looks_client_list(text)
            or _looks_which_parties_ask(text)
            or _looks_sold_to_parties(text)
            or _looks_channel_growth_ask(text)
            or _looks_price_query(text)
        ):
            if _looks_price_query(text):
                return "query_price"
            return "required"
        if (
            _looks_sales_matrix(text)
            or _looks_analytical(text)
            or _wants_scoped_month_ams(text)
            or _wants_named_month_trend(text)
        ):
            return "query_sales"
        return "required"

    # Default factual ask: require a tool; model chooses which one
    return "required"


def chat_completion(
    messages: list[dict[str, Any]],
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    on_status: Callable[[str], None] | None = None,
    forced_prior_spec: dict[str, Any] | None = None,
    forced_prior_price_spec: dict[str, Any] | None = None,
    forced_prior_party_spec: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Run a chat turn with tools. System prompt is refreshed with live DB state every turn.

    ``forced_prior_spec`` comes from the Reply button — pin follow-ups to that
    answer's filters even when newer tables exist in the thread.
    """
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "OpenAI package is not installed. Run: pip install openai"
        ) from exc

    client = OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_S)

    # Drop any prior system message and inject a fresh live briefing + catalog.
    history = [m for m in messages if m.get("role") != "system"]
    working: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt()},
        *history,
    ]

    last_user = ""
    for m in reversed(history):
        if m.get("role") == "user" and m.get("content"):
            last_user = str(m["content"])
            break

    for round_i in range(MAX_TOOL_ROUNDS):
        if on_status:
            on_status("Thinking…" if round_i == 0 else "Reading your database…")

        # v0.4.0: require a tool on factual asks; force a name only for
        # named-party / Reply follow-ups (see resolve_forced_tool).
        tool_choice: Any = "auto"
        if round_i == 0 and _looks_factual(last_user):
            prior_party_guess = forced_prior_party_spec or _last_party_spec(working)
            prior_table_guess = forced_prior_spec or _last_table_spec(working)
            forced_name = resolve_forced_tool(
                last_user,
                prior_table_spec=prior_table_guess,
                prior_party_spec=prior_party_guess,
                explicit_followup=_is_explicit_followup(last_user),
            )
            if forced_name == "required":
                tool_choice = "required"
            elif forced_name == "auto":
                tool_choice = "auto"
            else:
                tool_choice = {
                    "type": "function",
                    "function": {"name": forced_name},
                }

        api_messages = _working_messages_for_api(working)
        try:
            response = client.chat.completions.create(
                model=model,
                messages=api_messages,
                tools=TOOLS,
                tool_choice=tool_choice,
                temperature=0.1,
            )
        except Exception as exc:  # noqa: BLE001
            err = str(exc).lower()
            if "timeout" in err or "timed out" in err:
                raise RuntimeError(
                    f"OpenAI request timed out after {OPENAI_TIMEOUT_S:.0f}s. "
                    "Try again — if this keeps happening, check network / API key."
                ) from exc
            raise
        choice = response.choices[0]
        msg = choice.message
        tool_calls = msg.tool_calls or []

        assistant_entry: dict[str, Any] = {
            "role": "assistant",
            "content": msg.content or "",
        }
        if tool_calls:
            assistant_entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in tool_calls
            ]
        working.append(assistant_entry)

        if not tool_calls:
            text = (msg.content or "").strip()
            # Soft guard: if the model still invents a cutoff, force an overview tool.
            bad = re.search(
                r"(only (have|had) access|knowledge cutoff|data up to|until (19|20)\d{2})",
                text,
                flags=re.IGNORECASE,
            )
            if bad and round_i < MAX_TOOL_ROUNDS - 1:
                if on_status:
                    on_status("Correcting with live database overview…")
                working.append(
                    {
                        "role": "user",
                        "content": (
                            "STOP. You invented a data cutoff. Call get_sales_overview now, "
                            "then answer using ONLY those dates and a real SQL/tool query."
                        ),
                    }
                )
                continue
            # Multi-tool turns: keep deterministic tables + GPT analysis
            tool_md = _last_tool_answer_markdown(working)
            if tool_md and (
                "eva-mtx" in tool_md
                or "<table" in tool_md.lower()
                or re.search(r"^\|.+\|", tool_md, re.M)
            ):
                text = _compose_tables_plus_analysis(tool_md, text)
                working[-1]["content"] = text
            _attach_followup_meta(
                working,
                table_spec=forced_prior_spec or _last_table_spec(working),
                price_spec=forced_prior_price_spec or _last_price_spec(working),
                party_spec=forced_prior_party_spec or _last_party_spec(working),
            )
            return text, _prune_session_messages(working)

        sales_markdown: str | None = None
        last_result_mode: str | None = None
        last_table_spec: dict[str, Any] | None = None
        last_price_spec: dict[str, Any] | None = None
        last_party_spec: dict[str, Any] | None = None
        for tc in tool_calls:
            name = tc.function.name
            if on_status:
                on_status(f"Querying database: {name}…")
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                prior = forced_prior_spec or _last_table_spec(working)
                prior_price = forced_prior_price_spec or _last_price_spec(working)
                prior_party = forced_prior_party_spec or _last_party_spec(working)
                result = _dispatch_tool(
                    name,
                    args,
                    user_text=last_user,
                    prior_spec=prior,
                    prior_price_spec=prior_price,
                    prior_party_spec=prior_party,
                )
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}

            # Prefer deterministic markdown tables from structured tools.
            # GPT analysis is optional (skipped on simple show-me for speed).
            if (
                name
                in {
                    "query_sales",
                    "query_price",
                    "lookup_party",
                    "list_clients",
                    "analyze_parties",
                    "advanced_query",
                }
                and isinstance(result, dict)
                and result.get("ok")
                and result.get("answer_markdown")
            ):
                sales_markdown = str(result["answer_markdown"])
                last_result_mode = str(result.get("mode") or "") or None
                want_gpt = _wants_gpt_analysis(
                    last_user, result_mode=last_result_mode
                )
                md_for_model = (
                    _strip_analysis_section(sales_markdown)
                    if want_gpt
                    else sales_markdown
                )
                instructions = (
                    _ANALYSIS_RESPONSE_INSTRUCTIONS
                    if want_gpt
                    else _FAST_RESPONSE_INSTRUCTIONS
                )
                if result.get("party_spec"):
                    last_party_spec = result["party_spec"]
                if name == "query_sales":
                    if result.get("table_spec"):
                        last_table_spec = result["table_spec"]
                    result = {
                        "ok": True,
                        "mode": result.get("mode"),
                        "period": result.get("period"),
                        "filters": result.get("filters"),
                        "business_units": result.get("business_units"),
                        "row_dimension": result.get("row_dimension"),
                        "column_dimension": result.get("column_dimension"),
                        "table_spec": result.get("table_spec"),
                        "required_table_count": result.get("required_table_count"),
                        "answer_markdown": md_for_model,
                        "response_instructions": instructions,
                    }
                elif name == "query_price":
                    if result.get("price_spec"):
                        last_price_spec = result["price_spec"]
                    result = {
                        "ok": True,
                        "period": result.get("period"),
                        "filters": result.get("filters"),
                        "avg_rate": result.get("avg_rate"),
                        "amount_per_kg": result.get("amount_per_kg"),
                        "price_fetch": result.get("price_fetch"),
                        "include_price_fetch": result.get("include_price_fetch"),
                        "price_spec": result.get("price_spec"),
                        "answer_markdown": md_for_model,
                        "response_instructions": instructions,
                    }
                else:
                    result = {
                        "ok": True,
                        "mode": result.get("mode"),
                        "metric": result.get("metric"),
                        "period": result.get("period"),
                        "filters": result.get("filters"),
                        "query": result.get("query"),
                        "matches": result.get("matches"),
                        "clients": result.get("clients"),
                        "parties": result.get("parties"),
                        "count": result.get("count"),
                        "party_spec": result.get("party_spec"),
                        "answer_markdown": md_for_model,
                        "response_instructions": instructions,
                    }

            working.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str)[:120_000],
                }
            )

        if sales_markdown and len(tool_calls) == 1:
            follow_meta = dict(
                table_spec=last_table_spec
                or forced_prior_spec
                or _last_table_spec(working),
                price_spec=last_price_spec
                or forced_prior_price_spec
                or _last_price_spec(working),
                party_spec=last_party_spec
                or forced_prior_party_spec
                or _last_party_spec(working),
            )
            # Fast path: simple show-me → return tool markdown immediately
            # (OpenAI already chose the tool; skip a second narrative round.)
            if not _wants_gpt_analysis(last_user, result_mode=last_result_mode):
                working.append({"role": "assistant", "content": sales_markdown})
                _attach_followup_meta(working, **follow_meta)
                return sales_markdown, _prune_session_messages(working)

            # Slow path: lean analysis-only call (no tools schema, short system)
            if on_status:
                on_status("Writing analysis…")
            analysis_messages = [
                {"role": "system", "content": _ANALYSIS_ONLY_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"User question: {last_user}\n\n"
                        f"Tool tables:\n{_strip_analysis_section(sales_markdown)}"
                    ),
                },
            ]
            try:
                analysis_resp = client.chat.completions.create(
                    model=model,
                    messages=analysis_messages,
                    temperature=0.35,
                    max_tokens=500,
                )
                model_reply = (
                    analysis_resp.choices[0].message.content or ""
                ).strip()
            except Exception:  # noqa: BLE001
                model_reply = ""
            final = _compose_tables_plus_analysis(sales_markdown, model_reply)
            working.append({"role": "assistant", "content": final})
            _attach_followup_meta(working, **follow_meta)
            return final, _prune_session_messages(working)

    return (
        "I hit the tool-call limit before finishing. Please ask a more specific question.",
        _prune_session_messages(working),
    )
