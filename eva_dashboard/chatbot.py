"""OpenAI-powered chatbot with read-only access to Eva Foods SQLite data."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from eva_dashboard.client_language import (
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
    query_price,
    query_sales,
)
from eva_dashboard.seasonality import expected_month_close

DEFAULT_MODEL = "gpt-4o"
MAX_SQL_ROWS = 200
MAX_TOOL_ROUNDS = 4  # Prefer one structured query_sales call over many SQL rounds

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

SPEED & TOOL RULES:
1. For almost all SALES volume questions, call **query_sales ONCE** with structured filters
   (including **client_type** when the user names a client type).
   Do NOT write multi-step SQL. Do NOT call get_schema / run_sql first for sales pivots.
2. Interpret the user question → fill parameters → use returned markdown tables.
   One tool round is the goal.
3. NEVER invent numbers or cite an OpenAI knowledge cutoff.
4. Geography = City-Filter (`city` parameter). Always state the period label from the tool.
5. Read-only only.
6. Client **name** questions ("who is Al Bari?") → **lookup_party**.
7. Client **lists** ("who are my distributors in Lahore?") → **list_clients**
   (City-Filter + Client Type — NOT fuzzy name search on the city word).
8. Party rankings / AMS / share / YoY ("top 10 parties…", "which distributors
   doing well…", "% of VTF in Lahore") → **analyze_parties**.
9. Rate / price / Price Fetch → **query_price**.

CLIENT TYPE ALIASES (always set client_type — do NOT invent a Business Unit instead):
  • Imtiaz / Imtiaz store(s) / store → client_type='Imtiaz Store'
  • Distributor / Distributors / Eva distributors → client_type='Eva Distributors'
  • Mentions of Chase Up, Metro, CSD, SPAR, Food Panda, Gelani, North/Central LMT, etc.
    → exact client_type from live data
  Example: "Average sale for Imtiaz store last 6 months"
    → client_type='Imtiaz Store', columns='month', months_back=6
      (NO business_unit unless the user named one)
  Example: "Who are my distributors in Lahore?"
    → list_clients city='Lahore', client_type='Eva Distributors'

SALES MATRIX RULES (query_sales — set filters; tool builds the table):
Row drill-down:
  • No Business Unit → rows = Business Unit
  • One Business Unit → rows = Packing Category (NOT Oil Type), with Business Unit
    as a leading (merged) column + BU subtotal
  • Multiple Business Units → rows = Business Unit
  • Oil Type set → Packing; Packing set → Product
  • "Show by product" → Packing Category under Business Unit hierarchy
  • "Show by SKU" → SKU under Business Unit → Packing hierarchy, with packing
    and BU subtotals
Columns: client_type (default) | city | month. Every table has row Total + column Totals.
  • month-wise / last N months → columns='month', months_back=6 (+ Average column)
  • When client_type is filtered, columns auto-switch from client_type → city

Examples:
  "What were Eva Consumer sales in Lahore last month?"
    → business_unit='Eva Consumer', city='Lahore'  # Packing × client_type
  "Month-wise breakdown of Eva Consumer sales"
    → business_unit='Eva Consumer', columns='month', months_back=6
  "Add Eva Bulk to this table" (follow-up)
    → business_units=['Eva Consumer','Eva Bulk'], columns='month', months_back=6,
      prior_spec from previous answer — SAME table, extra BU row(s)
  "Does this include bulk?" (follow-up after Eva / Eva Consumer table)
    → inclusion check for Eva Bulk with SAME city/client/period; show Bulk-only
      table (included slice OR excluded sales). Then user may say:
  "Combine the tables" / "add bulk sales" / "include bulk"
    → merge prior BUs + Eva Bulk into one table (prior_spec)
  "Can you show city wise?" / "group by city" (follow-up)
    → promote City to the first row column (Y). If prior was month-wise,
      keep months as columns (X). Clears a single-city filter so all cities appear.
      Nest prior packing/SKU/BU under City when those were the prior rows.
  "Group by client type" / "business unit wise" / "as columns by city"
    → same idea; 'as columns' puts the dim on X instead of Y
  "How were Eva Consumer sales in July?"
    → analytical (city + client + AMS); rows = Packing Category
  "What's the Average sale for Imtiaz store last 6 months"
    → client_type='Imtiaz Store', columns='month', months_back=6
  "Show by product / product breakdown" (follow-up on a BU table)
    → row_dimension='packing_category', prior_spec — SAME filters/months
  "Dissect further / SKU wise / show by SKU" (follow-up)
    → row_dimension='product', prior_spec — SAME filters/months
  "Analyze these sales and compare with the same period last year" (follow-up)
    → query_sales compare='yoy', prior_spec — SAME filters/grain vs last year
      (NOT analyze_parties / NOT invent Eva Distributors)
  "Canola standup price for Distributors last week"
    → query_price: oil/packing or product_query='canola standup',
      client_type='Eva Distributors', period='last week'
  "What's the Price Fetch?" (follow-up) → query_price with include_price_fetch=true + prior_spec
  "Who is Al Bari?" → lookup_party query='Al Bari'
  "Who are my distributors in Lahore?"
    → list_clients city='Lahore', client_type='Eva Distributors'
  "Top 10 parties by AMS in Karachi"
    → analyze_parties city='Karachi', metric='ams', limit=10
  "Top 5 distributors for Eva VTF" → client_type + oil_type, metric='ams' (default)
  "Who were the top distributors in this" (follow-up after a sales table)
    → analyze_parties with prior_spec filters (city/BU/period) +
      client_type='Eva Distributors', metric='volume' (same sales as the table)
  "Which distributors are performing poorly in Lahore?"
    → metric='vs_ams', sort='asc', city='Lahore'
  "New parties last 6 months" / "new distributors last month"
    → metric='new_parties', period='last 6 months'
  "Lost / silent parties this month" → metric='lost_parties'
  "Product mix for Imtiaz" → packing_mix; "SKU wise" → product_mix
  "City league / top cities" → group_by='city'
  "Most invoices / invoice frequency" → metric='invoices'
  "Which distributors grew VTF most vs July last year?"
    → analyze_parties client_type='Eva Distributors', oil_type='Eva VTF',
      period='July', compare_period='July last year', metric='yoy'

MODE FROM LANGUAGE:
- what were / show / give me / breakdown / month-wise / average sale → matrix
- how were / how are / evaluate / assess / performance / doing / trend → analytical
- add / also include / plus / combine tables / include bulk → merge via prior_spec
- does this include bulk? → inclusion check (Bulk table for same filters)
- city wise / group by city|client type|BU|product → regroup prior table
  (default: dim on rows/Y; month stays on columns/X; 'as columns' → dim on X)
- show by product / product category / packing → rows = Packing Category (keep prior)
- dissect further / SKU / sku-wise → rows = Product SKU (keep prior)
- Messages starting with [FOLLOW-UP …] are replies to the previous answer —
  always reuse that answer's filters/table_spec.

SKU / product language questions (single product):
- resolve_product_language then product_sales (or query with packing/oil filters).

Other:
- Full daily briefing → report_snapshot
- What's loaded? → get_sales_overview
- Rare custom SQL only if query_sales cannot express it → run_sql

RESPONSE FORMAT:
- Markdown TABLES only for numbers (never bullet lists of metrics).
- One sentence of context (period + filters), then table(s).
- Columns already sorted highest-first by the tool — preserve that order.
- For query_sales / query_price / lookup_party / list_clients / analyze_parties:
  paste answer_markdown verbatim.

=== PRODUCT LANGUAGE (abbrev) ===
{glossary}

=== DATA CATALOG ===
{catalog}
"""


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
                "Do NOT invent a Business Unit for client-type-only questions. "
                "Month-wise = last N months + Average. Analytical = city + client + AMS "
                "when user says how were/evaluate."
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
                            "packing_category | product. Follow-ups: "
                            "'show by product' → packing_category; "
                            "'SKU wise' → product"
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
                            "(last N months + Average)"
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
                "List clients by City-Filter and/or Client Type. "
                "Use for 'who are my distributors in Lahore?'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "client_type": {"type": "string"},
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
                "Average Rate from sales (and Price Fetch when asked). Use for "
                "'Canola standup price for Distributors last week', "
                "'average rate', 'what's the Price Fetch?' follow-ups."
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
                "packing contribution, top SKUs, party profile, dumping/excessive sales."
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
                    "party_query": {"type": "string"},
                    "group_by": {"type": "string"},
                    "metric": {"type": "string"},
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
    if _looks_regroup(t):
        return True
    if _looks_row_drilldown(t):
        return True
    if _is_explicit_followup(t) and re.search(
        r"\b(add|include|combine|merge|bulk|consumer|group|city|wise)\b", t
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
            r"sku[- ]?wise|by sku|show by sku|sku break|"
            r"product[- ]?wise|by sku|show skus?|"
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
            r"client\s*types?\b",
            "client_type",
        ),
        (
            r"\b(client[- ]?type[- ]?wise|by\s+client\s*types?)\b",
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
            r"\b(as|into)\s+columns?\s+(by\s+)?client\s*types?\b",
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

    # SKU / product line items (check before "by product" packing)
    if re.search(
        r"\b(sku[- ]?wise|by sku|show by sku|sku break|skus?\b|"
        r"product[- ]?wise|item[- ]?wise|by items?)\b",
        t,
    ):
        return "product"

    # Oil Type rows
    if re.search(r"\b(oil types?|by oil|oil break\s*down|oil breakdown)\b", t):
        return "oil_type"

    # Packing / "product category" / "show by product"
    if re.search(
        r"\b("
        r"show by product|product break\s*down|product breakdown|"
        r"by product|product categor|"
        r"packing categor|by packing|show packing|pack(ing)? break"
        r")\b",
        t,
    ):
        return "packing_category"

    # Business Unit
    if re.search(r"\b(by business unit|by bu\b|show by bu|business unit break)\b", t):
        return "business_unit"

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


def _looks_party_lookup(text: str) -> bool:
    """Fuzzy single-name lookup only (not lists / rankings)."""
    t = (text or "").lower()
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
        r"in this|from this|this table"
        r")\b",
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
            r"city[- ]?wise|group by|as columns"
            r")\b",
            t,
        )
        or _looks_include_check(t)
        or _looks_combine_tables(t)
        or _looks_regroup(t)
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
        r"(imtiaz|distributors?|clients?)\b",
        t,
    ):
        return True
    return False


def _looks_party_analytics(text: str) -> bool:
    t = (text or "").lower()
    # Sales-table YoY compare is query_sales, not party ranking
    if _looks_sales_yoy_compare(t):
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
            r"underperform|grew|grow(th|n)?|"
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
            r"last year.{0,40}(distributors?|parties|imtiaz|growth|grew)"
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
    """Carry city / BU / period / etc. from the last sales table when user says 'in this'."""
    if not prior_spec or not _looks_context_followup(user_text):
        return {}
    pf = prior_spec.get("filters") or {}
    out: dict[str, Any] = {}
    if pf.get("city"):
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
    if bu:
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


def _looks_price_query(text: str) -> bool:
    t = (text or "").lower()
    if _looks_party_lookup(t) or _looks_client_list(t) or _looks_party_analytics(t):
        return False
    # "average sale" is volume, not rate
    if re.search(r"\baverage\s+sales?\b|\bavg\.?\s+sales?\b", t):
        return False
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
    return found


def _months_back_from_text(text: str, default: int = 6) -> int:
    m = re.search(r"\b(?:last|past|previous)\s+(\d{1,2})\s+months?\b", (text or "").lower())
    if m:
        return max(1, min(24, int(m.group(1))))
    return default


def _last_table_spec(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the most recent query_sales table_spec in tool results."""
    for m in reversed(messages):
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
    group_by = arguments.get("group_by") or inferred.get("group_by")
    party_query = arguments.get("party_query") or inferred.get("party_query") or user_text

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
            left=left or "Lahore",
            right=right or "Karachi",
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
    return {"ok": False, "error": f"Unknown advanced mode: {mode}"}


def _dispatch_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    user_text: str = "",
    prior_spec: dict[str, Any] | None = None,
    prior_price_spec: dict[str, Any] | None = None,
) -> Any:
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

        if _looks_analytical(user_text):
            mode = "analytical"
        else:
            mode = "matrix"

        columns = arguments.get("columns") or "client_type"
        months_back = int(arguments.get("months_back") or 6)
        if _looks_month_wise(user_text):
            columns = "month"
            months_back = _months_back_from_text(user_text, months_back)
            mode = "matrix"

        units = list(arguments.get("business_units") or [])
        if arguments.get("business_unit"):
            units.append(arguments["business_unit"])

        prior_row = (prior_spec or {}).get("row_dimension") if prior_spec else None
        regroup = (
            resolve_regroup_request(user_text, prior_spec=prior_spec)
            if prior_spec
            else None
        )
        # Prefer regroup over packing/SKU drill language when both could match
        row_dim = arguments.get("row_dimension")
        row_groups: list[str] | None = None
        clear_filters: list[str] | None = None
        lock_columns = False
        if regroup:
            row_dim = regroup.get("row_dimension")
            row_groups = list(regroup.get("row_groups") or []) or None
            clear_filters = list(regroup.get("clear_filters") or []) or None
            columns = str(regroup.get("columns") or columns)
            lock_columns = True
            mode = "matrix"
        else:
            row_dim = row_dim or resolve_row_dimension_request(
                user_text, prior_row_dimension=prior_row
            )
        is_drill = (not regroup) and (
            bool(row_dim) or _looks_row_drilldown(user_text)
        )
        is_regroup = bool(regroup)

        # Follow-up: merge mentioned BUs / keep prior table / change row grain / YoY
        is_yoy = _looks_sales_yoy_compare(user_text)
        is_combine = _looks_combine_tables(user_text) or _looks_table_followup(user_text)
        use_prior = prior_spec if (
            (
                is_combine
                or is_drill
                or is_yoy
                or is_regroup
                or _is_explicit_followup(user_text)
            )
            and prior_spec
        ) else None
        if use_prior or is_combine or is_drill or is_yoy or is_regroup:
            for u in _extract_business_units_from_text(user_text):
                if u not in units:
                    units.append(u)
            for u in _companion_business_units(user_text, prior_spec):
                if u not in units:
                    units.append(u)
            # Combine after include_check: restore original prior units + segment
            if prior_spec and (prior_spec.get("include_check") or is_combine) and not is_regroup:
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
            ):
                columns = "month"
                months_back = int(use_prior.get("months_back") or months_back)

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
        city_arg = arguments.get("city")

        # YoY of "these sales": do not invent a client type (e.g. Eva Distributors)
        if is_yoy and use_prior:
            ctype = None
            oil = oil or None
            pack = pack or None
            # Prefer prior BUs only — drop model-invented units not in user text
            if not _extract_business_units_from_text(user_text):
                uniq = []

        # If user asked about a client type but did not name a BU, do not keep a
        # model-invented Business Unit (e.g. Eva Consumer for "Imtiaz store").
        mentioned_units = (
            _extract_business_units_from_text(user_text)
            + _companion_business_units(user_text, prior_spec)
        )
        if (ctype and not mentioned_units) or (is_drill and use_prior and not mentioned_units):
            # Drill-downs keep prior BU filters via prior_spec, not model args
            if is_drill and use_prior and not mentioned_units:
                uniq = []
            elif ctype and not mentioned_units and not is_combine and not is_regroup:
                uniq = []

        # Regroup: do not invent city/client from this short follow-up text
        if is_regroup:
            city_arg = None
            if not extract_client_type_from_text(user_text):
                ctype = None
            if not _extract_business_units_from_text(user_text):
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

        return query_sales(
            period=arguments.get("period"),
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
            prior_spec=use_prior or arguments.get("prior_spec"),
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
        inferred = infer_party_analytics_from_text(user_text)
        return list_clients(
            city=arguments.get("city") or inferred.get("city"),
            client_type=normalize_client_type(
                arguments.get("client_type") or inferred.get("client_type")
            ),
            period=arguments.get("period") or inferred.get("period"),
            date_from=arguments.get("date_from"),
            date_to=arguments.get("date_to"),
            limit=int(arguments.get("limit") or inferred.get("limit") or 200),
        )
    if name == "advanced_query":
        return _dispatch_advanced(arguments, user_text, prior_spec=prior_spec)
    if name == "analyze_parties":
        inferred = infer_party_analytics_from_text(user_text)
        prior_ctx = _party_filters_from_prior(prior_spec, user_text)
        # Context follow-up ("in this"): match the sales table numbers → volume
        metric = (
            arguments.get("metric")
            or inferred.get("metric")
            or ("volume" if prior_ctx else None)
            or "ams"
        )
        if (
            prior_ctx
            and metric == "ams"
            and (inferred.get("metric") or "ams") == "ams"
            and not arguments.get("metric")
        ):
            if not re.search(
                r"\b(ams|average (monthly )?sale|growth|yoy|vs\s*ams)\b",
                (user_text or "").lower(),
            ):
                metric = "volume"
        return analyze_parties(
            period=arguments.get("period")
            or inferred.get("period")
            or prior_ctx.get("period"),
            date_from=arguments.get("date_from") or prior_ctx.get("date_from"),
            date_to=arguments.get("date_to") or prior_ctx.get("date_to"),
            city=arguments.get("city")
            or inferred.get("city")
            or prior_ctx.get("city"),
            client_type=normalize_client_type(
                arguments.get("client_type")
                or inferred.get("client_type")
                or prior_ctx.get("client_type")
            ),
            business_unit=arguments.get("business_unit")
            or inferred.get("business_unit")
            or prior_ctx.get("business_unit"),
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
            limit=int(arguments.get("limit") or inferred.get("limit") or 10),
        )
    if name == "query_price":
        ctype = arguments.get("client_type") or extract_client_type_from_text(user_text)
        oil = arguments.get("oil_type") or extract_oil_type_from_text(user_text)
        pack = arguments.get("packing_category") or extract_packing_from_text(user_text)
        include_pf = bool(arguments.get("include_price_fetch"))
        if _looks_price_fetch_followup(user_text):
            include_pf = True
        use_prior = arguments.get("prior_spec") or (
            prior_price_spec
            if (_looks_price_fetch_followup(user_text) and prior_price_spec)
            else None
        )
        product_query = arguments.get("product_query")
        if not product_query and not arguments.get("product"):
            product_query = user_text
        return query_price(
            period=arguments.get("period"),
            date_from=arguments.get("date_from"),
            date_to=arguments.get("date_to"),
            city=arguments.get("city"),
            business_unit=arguments.get("business_unit"),
            oil_type=oil,
            packing_category=pack,
            client_type=ctype,
            product=arguments.get("product"),
            product_query=product_query,
            include_price_fetch=include_pf,
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


def _attach_followup_meta(
    messages: list[dict[str, Any]],
    *,
    table_spec: dict[str, Any] | None = None,
    price_spec: dict[str, Any] | None = None,
) -> None:
    """Stamp the last assistant turn so the Reply button can pin prior filters."""
    for m in reversed(messages):
        if m.get("role") == "assistant" and (m.get("content") or "").strip():
            meta = dict(m.get("_eva_followup") or {})
            if table_spec:
                meta["table_spec"] = table_spec
            if price_spec:
                meta["price_spec"] = price_spec
            if meta:
                m["_eva_followup"] = meta
            return


def chat_completion(
    messages: list[dict[str, Any]],
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    on_status: Callable[[str], None] | None = None,
    forced_prior_spec: dict[str, Any] | None = None,
    forced_prior_price_spec: dict[str, Any] | None = None,
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

    client = OpenAI(api_key=api_key)

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

        # First round: require tools; force the right primary tool when possible.
        tool_choice: Any = "auto"
        if round_i == 0 and _looks_factual(last_user):
            if _looks_include_check(last_user) or _looks_combine_tables(last_user) or _looks_regroup(last_user):
                tool_choice = {
                    "type": "function",
                    "function": {"name": "query_sales"},
                }
            elif looks_advanced(last_user):
                tool_choice = {
                    "type": "function",
                    "function": {"name": "advanced_query"},
                }
            elif _looks_client_list(last_user):
                tool_choice = {
                    "type": "function",
                    "function": {"name": "list_clients"},
                }
            elif _looks_party_analytics(last_user):
                tool_choice = {
                    "type": "function",
                    "function": {"name": "analyze_parties"},
                }
            elif _looks_party_lookup(last_user):
                tool_choice = {
                    "type": "function",
                    "function": {"name": "lookup_party"},
                }
            elif _looks_price_query(last_user):
                tool_choice = {
                    "type": "function",
                    "function": {"name": "query_price"},
                }
            elif _looks_sales_yoy_compare(last_user) or _looks_sales_matrix(last_user):
                tool_choice = {
                    "type": "function",
                    "function": {"name": "query_sales"},
                }
            else:
                tool_choice = "required"

        api_messages = [_api_history_message(m) for m in working]
        response = client.chat.completions.create(
            model=model,
            messages=api_messages,
            tools=TOOLS,
            tool_choice=tool_choice,
            temperature=0.1,
        )
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
            _attach_followup_meta(
                working,
                table_spec=forced_prior_spec or _last_table_spec(working),
                price_spec=forced_prior_price_spec or _last_price_spec(working),
            )
            return text, working

        sales_markdown: str | None = None
        last_table_spec: dict[str, Any] | None = None
        last_price_spec: dict[str, Any] | None = None
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
                result = _dispatch_tool(
                    name,
                    args,
                    user_text=last_user,
                    prior_spec=prior,
                    prior_price_spec=prior_price,
                )
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}

            # Prefer deterministic markdown from structured tools
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
                        "answer_markdown": sales_markdown,
                        "response_instructions": (
                            "Use answer_markdown verbatim as the reply."
                        ),
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
                        "answer_markdown": sales_markdown,
                        "response_instructions": (
                            "Use answer_markdown verbatim as the reply."
                        ),
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
                        "answer_markdown": sales_markdown,
                        "response_instructions": (
                            "Use answer_markdown verbatim as the reply."
                        ),
                    }

            working.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str)[:120_000],
                }
            )

        # Do not let the model drop tables — return formatted sales answer directly
        if sales_markdown and len(tool_calls) == 1:
            working.append({"role": "assistant", "content": sales_markdown})
            _attach_followup_meta(
                working,
                table_spec=last_table_spec or forced_prior_spec or _last_table_spec(working),
                price_spec=last_price_spec or forced_prior_price_spec or _last_price_spec(working),
            )
            return sales_markdown, working

    return (
        "I hit the tool-call limit before finishing. Please ask a more specific question.",
        working,
    )
