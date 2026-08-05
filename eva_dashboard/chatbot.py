"""OpenAI-powered chatbot with read-only access to Eva Foods SQLite data."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from eva_dashboard.db import connect, init_db
from eva_dashboard.paths import data_root, db_path

DEFAULT_MODEL = "gpt-4o"
MAX_SQL_ROWS = 200
MAX_TOOL_ROUNDS = 10

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
            SELECT category_1, COUNT(*) AS products
            FROM category
            GROUP BY category_1
            ORDER BY category_1
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
        f"{c['category_1']}×{c['products']}" for c in ov.get("categories") or []
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
- Category map products: {ov['products_in_category_map']:,} ({cats})
- Clients: {ov['clients']:,}
- Factor cost rows: {ov['factor_cost_rows']:,}
- Example cities (City-Filter): {cities}

If the user asks about July 2026 (or any date inside the range above), QUERY THE DATABASE.
Never say the data "only goes to 2023" or cite an OpenAI knowledge cutoff for this app.
"""


def system_prompt() -> str:
    catalog = load_data_catalog()
    live = live_database_briefing()
    return f"""You are the Eva Foods in-app data analyst. You answer ONLY from the live SQLite database
and the catalog below. You are not a general web assistant.

{live}

CRITICAL ANTI-HALLUCINATION RULES:
1. NEVER invent numbers, dates, cities, or "I only have data until …" statements.
2. NEVER use your pretraining knowledge cutoff. This app's sales data is often in 2025–2026.
3. For ANY factual question (sales, MT, Price Fetch, cities, clients, costs), you MUST call tools
   before answering. Prefer run_sql / category_mt_totals / sales_by_city_and_category / report_snapshot.
4. If a month/year is ambiguous (e.g. "July"), use the year from LIVE DATABASE STATE
   (usually the max sales year), and say which year you used.
5. If the query returns zero rows, say so and show the SQL date filter you used + available range.
6. Always state the exact date range used in the answer.
7. Geography = clients.city_filter (City-Filter), joined via normalized party/client names.
8. Product type = category.category_1 (e.g. 'Eva Consumer'), joined on exact product name.
9. Read-only only. Never claim you uploaded/changed data.
10. Be useful: give MT, line counts, top parties, and short interpretation when asked for a report.

HOW TO ANSWER COMMON QUESTIONS:
- "Eva Consumer sales in Lahore for July 2026"
  → tool sales_by_city_and_category with category1='Eva Consumer', city='Lahore',
    date_from='2026-07-01', date_to='2026-07-31'
  OR equivalent run_sql joining sales→category→clients.
- "Full briefing for 30 Jun 2026" → report_snapshot(report_date='2026-06-30')
- "What's in the database?" → get_sales_overview

=== DATA CATALOG (business rules & formulas) ===
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
    SELECT COALESCE(c.category_1, '(unmapped)') AS category1,
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
            "name": "get_schema",
            "description": "Return live SQLite schema and row counts.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sales_overview",
            "description": (
                "Authoritative live overview: sales date min/max, months present, "
                "row counts, categories, cities. Call this before claiming any date range."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": "Read-only SELECT/WITH against eva.db (max 200 rows).",
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
            "description": "MT by Category 1 for an inclusive YYYY-MM-DD range.",
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
                "MT + amount for one Category 1 in one City-Filter over a date range, "
                "plus top parties. Use for questions like "
                "'Eva Consumer sales in Lahore for July 2026'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category1": {
                        "type": "string",
                        "description": "Exact Category 1, e.g. Eva Consumer",
                    },
                    "city": {
                        "type": "string",
                        "description": "City-Filter value, e.g. Lahore",
                    },
                    "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD"},
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
]


def _dispatch_tool(name: str, arguments: dict[str, Any]) -> Any:
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
    return {"error": f"Unknown tool: {name}"}


def _looks_factual(text: str) -> bool:
    t = (text or "").lower()
    keys = (
        "sale", "mt", "july", "june", "202", "lahore", "karachi", "eva", "maan",
        "price", "fetch", "ams", "ads", "city", "client", "cost", "report",
        "how much", "total", "top", "compare", "month", "daily", "mtd",
    )
    return any(k in t for k in keys)


def chat_completion(
    messages: list[dict[str, Any]],
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    on_status: Callable[[str], None] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Run a chat turn with tools. System prompt is refreshed with live DB state every turn."""
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

        # First round on factual questions: require a tool call so the model cannot invent.
        tool_choice: Any = "auto"
        if round_i == 0 and _looks_factual(last_user):
            tool_choice = "required"

        response = client.chat.completions.create(
            model=model,
            messages=working,
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
            return text, working

        for tc in tool_calls:
            name = tc.function.name
            if on_status:
                on_status(f"Querying database: {name}…")
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result = _dispatch_tool(name, args)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
            working.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str)[:120_000],
                }
            )

    return (
        "I hit the tool-call limit before finishing. Please ask a more specific question.",
        working,
    )
