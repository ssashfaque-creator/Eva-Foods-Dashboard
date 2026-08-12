"""SQLite persistence for Eva Dashboard."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from eva_dashboard.paths import db_path


def sanitize_column(name: Any) -> str:
    text = str(name or "").strip()
    text = re.sub(r"[^\w]+", "_", text, flags=re.UNICODE)
    text = re.sub(r"_+", "_", text).strip("_").lower()
    if not text:
        text = "col"
    if text[0].isdigit():
        text = f"c_{text}"
    return text


def unique_sanitized(columns: list[Any]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for col in columns:
        base = sanitize_column(col)
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(base if n == 0 else f"{base}_{n+1}")
    return out


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    db = Path(path) if path else db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ingested_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_type TEXT NOT NULL,
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ingested_hash
    ON ingested_files(file_type, content_hash);

CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id INTEGER REFERENCES ingested_files(id),
    row_hash TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    date TEXT,
    party TEXT,
    inv_no TEXT,
    srno TEXT,
    product TEXT,
    qty REAL,
    unit TEXT,
    mes_qty REAL,
    mes_unit TEXT,
    mt_qty REAL,
    rate REAL,
    basic_amount REAL,
    incl_gst_fed_amount REAL,
    client_type TEXT,
    payload_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_row_hash ON sales(row_hash);
CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(date);
CREATE INDEX IF NOT EXISTS idx_sales_party ON sales(party);
CREATE INDEX IF NOT EXISTS idx_sales_product ON sales(product);

CREATE TABLE IF NOT EXISTS category (
    product TEXT PRIMARY KEY,
    category_1 TEXT,
    category_2 TEXT,
    packing_category TEXT,
    payload_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    client TEXT,
    type TEXT,
    city_filter TEXT,
    city TEXT,
    inactive TEXT,
    payload_json TEXT NOT NULL,
    source_file_id INTEGER REFERENCES ingested_files(id),
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clients_name ON clients(client);
CREATE INDEX IF NOT EXISTS idx_clients_type ON clients(type);

CREATE TABLE IF NOT EXISTS product_cost_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id INTEGER REFERENCES ingested_files(id),
    pcfid REAL,
    date TEXT,
    client_type TEXT,
    prod_id REAL,
    product TEXT,
    unit TEXT,
    product_cost_center TEXT,
    cost REAL,
    payload_json TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pcost_client_prod ON product_cost_lines(client_type, prod_id);

CREATE TABLE IF NOT EXISTS packing_cost_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id INTEGER REFERENCES ingested_files(id),
    prod_id REAL,
    product TEXT,
    cost REAL,
    unit TEXT,
    date TEXT,
    row_order INTEGER,
    payload_json TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pack_prod ON packing_cost_lines(prod_id);

CREATE TABLE IF NOT EXISTS factor_costs (
    client_type TEXT NOT NULL,
    prod_id INTEGER NOT NULL,
    product TEXT,
    unit TEXT,
    product_cost REAL,
    packing_cost REAL,
    total_factor_cost REAL,
    product_cost_date TEXT,
    packing_cost_date TEXT,
    pcfid REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (client_type, prod_id)
);

CREATE TABLE IF NOT EXISTS eval_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    case_id TEXT,
    user_text TEXT,
    answer TEXT,
    rating TEXT NOT NULL,
    route_kind TEXT,
    route_json TEXT,
    tool_trace_json TEXT,
    issues_json TEXT,
    model TEXT,
    source TEXT
);
"""


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Additive migrations for existing eva.db files."""
    cat_cols = {row[1] for row in conn.execute("PRAGMA table_info(category)").fetchall()}
    if cat_cols and "packing_category" not in cat_cols:
        conn.execute("ALTER TABLE category ADD COLUMN packing_category TEXT")


def init_db(path: Path | None = None) -> Path:
    db = Path(path) if path else db_path()
    with connect(db) as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate_schema(conn)
    return db


def dataframe_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert a DataFrame to JSON-safe row dicts preserving original column names."""
    clean = frame.where(pd.notnull(frame), None)
    records: list[dict[str, Any]] = []
    for row in clean.to_dict(orient="records"):
        out: dict[str, Any] = {}
        for key, value in row.items():
            if hasattr(value, "isoformat"):
                out[str(key)] = value.isoformat()
            elif isinstance(value, (pd.Timestamp,)):
                out[str(key)] = None if pd.isna(value) else value.isoformat()
            else:
                out[str(key)] = value
        records.append(out)
    return records


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
