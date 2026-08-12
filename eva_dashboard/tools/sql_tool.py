"""Safe read-only SQLite execution for ad-hoc / discovery asks."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

import pandas as pd

from eva_dashboard.db import connect, init_db
from eva_dashboard.paths import db_path

# SQLite authorizer action codes (read-ish)
_SQLITE_CREATE_INDEX = 1
_SQLITE_CREATE_TABLE = 2
_SQLITE_DELETE = 9
_SQLITE_DROP_TABLE = 10
_SQLITE_INSERT = 18
_SQLITE_PRAGMA = 19
_SQLITE_READ = 20
_SQLITE_SELECT = 21
_SQLITE_TRANSACTION = 22
_SQLITE_UPDATE = 23
_SQLITE_ATTACH = 24
_SQLITE_DETACH = 25
_SQLITE_ALTER_TABLE = 26
_SQLITE_FUNCTION = 31
_SQLITE_SAVEPOINT = 32
_SQLITE_RECURSIVE = 33

_ALLOWED_ACTIONS = {
    _SQLITE_READ,
    _SQLITE_SELECT,
    _SQLITE_FUNCTION,
    _SQLITE_TRANSACTION,
    _SQLITE_SAVEPOINT,
    _SQLITE_RECURSIVE,
}

_FORBIDDEN = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|"
    r"ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|GRANT|REVOKE"
    r")\b",
    flags=re.IGNORECASE,
)

DEFAULT_SQL_LIMIT = 50
MAX_SQL_LIMIT = 200


def enforce_read_only(
    action_code: int,
    arg1: str | None,
    arg2: str | None,
    db_name: str | None,
    trigger_name: str | None,
) -> int:
    """SQLite authorizer callback — deny any modification."""
    del arg1, arg2, db_name, trigger_name
    if int(action_code) in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _validate_select(sql: str) -> str:
    text = (sql or "").strip().rstrip(";")
    if not text:
        raise ValueError("Empty SQL")
    if ";" in text:
        raise ValueError("Only a single SQL statement is allowed")
    if _FORBIDDEN.search(text):
        raise ValueError("Only read-only SELECT / WITH queries are permitted")
    head = re.sub(r"^\s+", "", text, flags=re.IGNORECASE)
    if not re.match(r"^(SELECT|WITH)\b", head, flags=re.IGNORECASE):
        raise ValueError("Query must start with SELECT or WITH")
    return text


def _ensure_limit(sql: str, *, limit: int = DEFAULT_SQL_LIMIT) -> str:
    lim = max(1, min(int(limit or DEFAULT_SQL_LIMIT), MAX_SQL_LIMIT))
    if re.search(r"\bLIMIT\b", sql, flags=re.IGNORECASE):
        return sql
    return f"SELECT * FROM ({sql}) AS _eva_q LIMIT {lim}"


def _df_to_markdown(df: pd.DataFrame) -> str:
    if df.empty:
        return "Query executed successfully. Result set is EMPTY."
    try:
        return df.to_markdown(index=False)
    except Exception:  # noqa: BLE001
        # tabulate optional — fall back to plain text
        return df.to_string(index=False)


def execute_read_only_sql(
    sql_query: str,
    *,
    db_file: str | None = None,
    limit: int = DEFAULT_SQL_LIMIT,
) -> dict[str, Any]:
    """Run a read-only SELECT and return markdown + structured rows."""
    try:
        query = _validate_select(sql_query)
        query = _ensure_limit(query, limit=limit)
    except ValueError as exc:
        return {
            "ok": False,
            "error": f"Security / validation: {exc}",
            "markdown": f"Error: {exc}",
        }

    init_db()
    path = db_file or str(db_path())
    try:
        with connect(path) as conn:
            try:
                conn.execute("PRAGMA query_only = ON")
            except sqlite3.Error:
                pass
            try:
                conn.set_authorizer(enforce_read_only)
            except (AttributeError, sqlite3.Error):
                pass
            try:
                frame = pd.read_sql_query(query, conn)
            finally:
                try:
                    conn.set_authorizer(None)  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc),
            "sql": query,
            "markdown": f"SQL Execution Error: {exc}",
        }

    md = _df_to_markdown(frame)
    records = frame.head(MAX_SQL_LIMIT).to_dict(orient="records")
    return {
        "ok": True,
        "sql": query,
        "row_count": int(len(frame)),
        "columns": list(frame.columns),
        "rows": records,
        "markdown": md,
        "answer_markdown": md if not frame.empty else "_No rows._\n",
    }
