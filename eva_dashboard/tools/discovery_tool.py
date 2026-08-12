"""Schema inspection + entity value lookup for the ReAct agent."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from eva_dashboard.db import connect, init_db
from eva_dashboard.paths import db_path

_ALLOWED_TABLES = frozenset(
    {
        "sales",
        "clients",
        "category",
        "factor_costs",
        "product_cost_lines",
        "packing_cost_lines",
        "ingested_files",
        "sqlite_master",
    }
)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str, *, kind: str) -> str:
    text = str(name or "").strip()
    if not _IDENT.match(text):
        raise ValueError(f"Invalid {kind} identifier: {name!r}")
    return text


def get_database_schema(*, db_file: str | None = None) -> dict[str, Any]:
    """Return DDL for user tables in eva.db."""
    init_db()
    path = db_file or str(db_path())
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='table' AND sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
    ddl = "\n\n".join(str(r["sql"]) for r in rows if r["sql"])
    # Compact column cheat-sheet for the agent
    hints = [
        "Key joins:",
        "- sales.product = category.product",
        "- sales.party ≈ clients.client (normalize spaces/case)",
        "- Effective MT: prefer sales.mt_qty; else qty/unit rules",
        "- Geo filter: clients.city_filter (not clients.city)",
        "- Rate: sales.rate (PKR); amounts: sales.incl_gst_fed_amount",
    ]
    md = (ddl or "_No tables._") + "\n\n" + "\n".join(hints)
    return {
        "ok": True,
        "tables": [str(r["name"]) for r in rows],
        "markdown": md,
        "answer_markdown": md,
    }


def lookup_entity_values(
    table_name: str,
    column_name: str,
    search_term: str,
    *,
    db_file: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search distinct matching values in a whitelisted table/column."""
    try:
        table = _safe_ident(table_name, kind="table")
        column = _safe_ident(column_name, kind="column")
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "markdown": f"Error: {exc}"}

    if table not in _ALLOWED_TABLES or table == "sqlite_master":
        return {
            "ok": False,
            "error": f"Table not allowed: {table}",
            "markdown": f"Error: table `{table}` is not searchable. "
            f"Allowed: {', '.join(sorted(_ALLOWED_TABLES - {'sqlite_master'}))}",
        }

    term = str(search_term or "").strip()
    if not term:
        return {
            "ok": False,
            "error": "Empty search_term",
            "markdown": "Error: search_term is required",
        }

    lim = max(1, min(int(limit or 10), 50))
    init_db()
    path = db_file or str(db_path())
    sql = (
        f"SELECT DISTINCT {column} AS value FROM {table} "
        f"WHERE {column} IS NOT NULL AND TRIM(CAST({column} AS TEXT)) <> '' "
        f"AND LOWER(CAST({column} AS TEXT)) LIKE LOWER(?) "
        f"ORDER BY value LIMIT {lim}"
    )
    try:
        with connect(path) as conn:
            # Verify column exists
            cols = {
                str(r[1])
                for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in cols:
                return {
                    "ok": False,
                    "error": f"Unknown column {column} on {table}",
                    "markdown": (
                        f"Error: `{table}` has columns: {', '.join(sorted(cols))}"
                    ),
                }
            frame = pd.read_sql_query(sql, conn, params=(f"%{term}%",))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "markdown": f"Lookup Error: {exc}"}

    if frame.empty:
        md = (
            f"No matching entities found in {table}.{column} for '{term}'."
        )
        return {"ok": True, "matches": [], "markdown": md, "answer_markdown": md}

    try:
        md = frame.to_markdown(index=False)
    except Exception:  # noqa: BLE001
        md = frame.to_string(index=False)
    matches = [str(v) for v in frame["value"].tolist()]
    return {
        "ok": True,
        "matches": matches,
        "markdown": md,
        "answer_markdown": md,
    }
