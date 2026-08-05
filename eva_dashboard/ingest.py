"""Ingest Excel uploads into the Eva Dashboard SQLite database."""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from eva_dashboard.costs import (
    compute_total_factor_costs,
    read_excel_raw,
)
from eva_dashboard.db import (
    connect,
    dataframe_records,
    init_db,
    json_dumps,
    now_iso,
)
from eva_dashboard.paths import uploads_dir


SALES_HEADER_ROW = 4  # Excel row 5
CLIENT_HEADER_ROW = 4
PRODUCT_COST_HEADER_ROW = 4


class IngestError(Exception):
    """Raised when an upload cannot be ingested."""


class DuplicateFileError(IngestError):
    """File content was already ingested."""


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _archive_upload(file_type: str, source: Path, original_name: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w.\-]+", "_", original_name).strip("._") or "upload.xlsx"
    stored = f"{stamp}__{safe}"
    dest = uploads_dir(file_type) / stored
    shutil.copy2(source, dest)
    return dest


def _already_ingested(conn, file_type: str, content_hash: str) -> dict | None:
    row = conn.execute(
        """
        SELECT id, original_name, stored_name, ingested_at, row_count
        FROM ingested_files
        WHERE file_type = ? AND content_hash = ?
        """,
        (file_type, content_hash),
    ).fetchone()
    return dict(row) if row else None


def _register_file(
    conn,
    *,
    file_type: str,
    original_name: str,
    stored_path: Path,
    content_hash: str,
    row_count: int,
    notes: str = "",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO ingested_files (
            file_type, original_name, stored_name, stored_path,
            content_hash, ingested_at, row_count, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_type,
            original_name,
            stored_path.name,
            str(stored_path),
            content_hash,
            now_iso(),
            row_count,
            notes,
        ),
    )
    return int(cur.lastrowid)


def _to_iso_date(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value.date().isoformat()
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "totals"}:
        return None
    parsed = pd.to_datetime(text, errors="coerce", dayfirst=False)
    if pd.isna(parsed):
        parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def _cell(payload: dict, *names: str) -> Any:
    lower = {str(k).strip().lower(): v for k, v in payload.items()}
    for name in names:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _num(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sales_row_hash(payload: dict) -> str:
    parts = [
        str(_to_iso_date(_cell(payload, "Date")) or ""),
        str(_cell(payload, "Inv #", "Inv#") or ""),
        str(_cell(payload, "SRNO") or ""),
        str(_cell(payload, "Product") or "").strip().lower(),
        str(_cell(payload, "Party") or "").strip().lower(),
        str(_num(_cell(payload, "Qty")) or ""),
        str(_num(_cell(payload, "Incl GST/FED Amount")) or ""),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _read_sales_frame(path: Path) -> pd.DataFrame:
    sales = pd.read_excel(path, sheet_name="Sales", header=SALES_HEADER_ROW, engine="openpyxl")
    sales = sales.dropna(how="all")
    return sales


def ingest_sales(path: Path | str, original_name: str | None = None) -> dict:
    """Archive and append sales rows into the database (categories are separate)."""
    init_db()
    source = Path(path)
    if not source.exists():
        raise IngestError(f"File not found: {source}")
    original_name = original_name or source.name
    content_hash = _file_hash(source)

    with connect() as conn:
        existing = _already_ingested(conn, "sales", content_hash)
        if existing:
            raise DuplicateFileError(
                f"Already ingested as {existing['stored_name']} "
                f"on {existing['ingested_at']} ({existing['row_count']} rows)"
            )

    sales = _read_sales_frame(source)
    records = dataframe_records(sales)
    archived = _archive_upload("sales", source, original_name)

    inserted = 0
    skipped = 0
    with connect() as conn:
        file_id = _register_file(
            conn,
            file_type="sales",
            original_name=original_name,
            stored_path=archived,
            content_hash=content_hash,
            row_count=0,
        )
        for payload in records:
            date_iso = _to_iso_date(_cell(payload, "Date"))
            product = str(_cell(payload, "Product") or "").strip()
            if not date_iso or not product or product.lower() == "nan":
                skipped += 1
                continue
            row_hash = _sales_row_hash(payload)
            try:
                conn.execute(
                    """
                    INSERT INTO sales (
                        source_file_id, row_hash, imported_at,
                        date, party, inv_no, srno, product,
                        qty, unit, mes_qty, mes_unit, mt_qty, rate,
                        basic_amount, incl_gst_fed_amount, client_type,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        row_hash,
                        now_iso(),
                        date_iso,
                        str(_cell(payload, "Party") or "").strip() or None,
                        str(_cell(payload, "Inv #", "Inv#") or "").strip() or None,
                        str(_cell(payload, "SRNO") or "").strip() or None,
                        product,
                        _num(_cell(payload, "Qty")),
                        str(_cell(payload, "Unit") or "").strip() or None,
                        _num(_cell(payload, "Mes Qty")),
                        str(_cell(payload, "Mes Unit") or "").strip() or None,
                        _num(_cell(payload, "M.T Qty")),
                        _num(_cell(payload, "Rate")),
                        _num(_cell(payload, "Basic Amount")),
                        _num(_cell(payload, "Incl GST/FED Amount")),
                        str(_cell(payload, "Client Type") or "").strip() or None,
                        json_dumps(payload),
                    ),
                )
                inserted += 1
            except Exception:
                # Unique row_hash → already present from an earlier file
                skipped += 1

        conn.execute(
            "UPDATE ingested_files SET row_count = ?, notes = ? WHERE id = ?",
            (inserted, f"skipped={skipped}", file_id),
        )

    return {
        "file_type": "sales",
        "original_name": original_name,
        "stored_path": str(archived),
        "inserted": inserted,
        "skipped": skipped,
        "content_hash": content_hash,
    }


def ingest_categories(path: Path | str, original_name: str | None = None) -> dict:
    """Archive a category file and replace the entire category table."""
    from eva_dashboard.categories import parse_category_file

    init_db()
    source = Path(path)
    if not source.exists():
        raise IngestError(f"File not found: {source}")
    original_name = original_name or source.name
    content_hash = _file_hash(source)

    frame = parse_category_file(source)
    if frame.empty:
        raise IngestError("Category file has no product rows")

    archived = _archive_upload("categories", source, original_name)
    records = []
    for row in frame.itertuples(index=False):
        records.append(
            {
                "Product": row.product,
                "Business Unit": row.category1,
                "Oil Type": row.category2,
                "Packing Category": getattr(row, "packing_category", "") or "",
            }
        )

    with connect() as conn:
        # Allow re-upload of the same file content (full replace master data)
        conn.execute(
            "DELETE FROM ingested_files WHERE file_type = ? AND content_hash = ?",
            ("categories", content_hash),
        )
        file_id = _register_file(
            conn,
            file_type="categories",
            original_name=original_name,
            stored_path=archived,
            content_hash=content_hash,
            row_count=0,
            notes="replaced",
        )
        conn.execute("DELETE FROM category")
        for payload in records:
            product = str(_cell(payload, "Product") or "").strip()
            if not product:
                continue
            conn.execute(
                """
                INSERT INTO category (
                    product, category_1, category_2, packing_category,
                    payload_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    product,
                    str(_cell(payload, "Business Unit") or "").strip() or None,
                    str(_cell(payload, "Oil Type") or "").strip() or None,
                    str(_cell(payload, "Packing Category") or "").strip() or None,
                    json_dumps(payload),
                    now_iso(),
                ),
            )
        conn.execute(
            "UPDATE ingested_files SET row_count = ? WHERE id = ?",
            (len(frame), file_id),
        )

    return {
        "file_type": "categories",
        "original_name": original_name,
        "stored_path": str(archived),
        "replaced": len(frame),
        "content_hash": content_hash,
    }


def load_category_map_from_db() -> pd.DataFrame:
    """Return product taxonomy from the database.

    Columns:
      product, category1/business_unit, category2/oil_type, packing_category
    (category1/category2 kept for report/PDF joins)
    """
    init_db()
    with connect() as conn:
        frame = pd.read_sql_query(
            """
            SELECT
              product,
              category_1 AS category1,
              category_2 AS category2,
              COALESCE(packing_category, '') AS packing_category
            FROM category
            ORDER BY product
            """,
            conn,
        )
    if frame.empty:
        raise ValueError(
            "No product categories loaded. Upload a category file on the Sales data tab."
        )
    frame["product"] = frame["product"].astype(str).str.strip()
    frame["category1"] = frame["category1"].fillna("").astype(str).str.strip()
    frame["category2"] = frame["category2"].fillna("").astype(str).str.strip()
    frame["packing_category"] = (
        frame["packing_category"].fillna("").astype(str).str.strip()
    )
    frame["business_unit"] = frame["category1"]
    frame["oil_type"] = frame["category2"]
    return frame.reset_index(drop=True)


def category_count() -> int:
    init_db()
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM category").fetchone()["n"])


def ingest_clients(path: Path | str, original_name: str | None = None) -> dict:
    """Archive clients workbook and upsert all rows by ClientID."""
    init_db()
    source = Path(path)
    if not source.exists():
        raise IngestError(f"File not found: {source}")
    original_name = original_name or source.name
    content_hash = _file_hash(source)

    with connect() as conn:
        existing = _already_ingested(conn, "clients", content_hash)
        if existing:
            raise DuplicateFileError(
                f"Already ingested as {existing['stored_name']} on {existing['ingested_at']}"
            )

    frame = pd.read_excel(
        source, sheet_name="ClientListReport", header=CLIENT_HEADER_ROW, engine="openpyxl"
    )
    frame = frame.dropna(how="all")
    records = dataframe_records(frame)
    archived = _archive_upload("clients", source, original_name)

    upserted = 0
    with connect() as conn:
        file_id = _register_file(
            conn,
            file_type="clients",
            original_name=original_name,
            stored_path=archived,
            content_hash=content_hash,
            row_count=0,
        )
        for payload in records:
            client = str(_cell(payload, "Client") or "").strip()
            if not client or client.lower() == "nan":
                continue
            client_id = str(_cell(payload, "ClientID") or "").strip() or client
            conn.execute(
                """
                INSERT INTO clients (
                    client_id, client, type, city_filter, city, inactive,
                    payload_json, source_file_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    client = excluded.client,
                    type = excluded.type,
                    city_filter = excluded.city_filter,
                    city = excluded.city,
                    inactive = excluded.inactive,
                    payload_json = excluded.payload_json,
                    source_file_id = excluded.source_file_id,
                    updated_at = excluded.updated_at
                """,
                (
                    client_id,
                    client,
                    str(_cell(payload, "Type") or "").strip() or None,
                    str(_cell(payload, "City-Filter") or "").strip() or None,
                    str(_cell(payload, "City") or "").strip() or None,
                    str(_cell(payload, "InActive") or "").strip() or None,
                    json_dumps(payload),
                    file_id,
                    now_iso(),
                ),
            )
            upserted += 1
        conn.execute(
            "UPDATE ingested_files SET row_count = ? WHERE id = ?",
            (upserted, file_id),
        )

    return {
        "file_type": "clients",
        "original_name": original_name,
        "stored_path": str(archived),
        "upserted": upserted,
        "content_hash": content_hash,
    }


def _refresh_factor_costs(conn) -> int:
    """Recompute factor_costs from the latest product + packing lines on disk archives.

    Uses the most recently ingested product_costs and packing_costs files.
    """
    product_row = conn.execute(
        """
        SELECT stored_path FROM ingested_files
        WHERE file_type = 'product_costs'
        ORDER BY ingested_at DESC, id DESC LIMIT 1
        """
    ).fetchone()
    packing_row = conn.execute(
        """
        SELECT stored_path FROM ingested_files
        WHERE file_type = 'packing_costs'
        ORDER BY ingested_at DESC, id DESC LIMIT 1
        """
    ).fetchone()
    if not product_row or not packing_row:
        return 0

    result = compute_total_factor_costs(product_row["stored_path"], packing_row["stored_path"])
    frame = result.frame
    conn.execute("DELETE FROM factor_costs")
    updated = now_iso()
    count = 0
    for row in frame.itertuples(index=False):
        conn.execute(
            """
            INSERT INTO factor_costs (
                client_type, prod_id, product, unit,
                product_cost, packing_cost, total_factor_cost,
                product_cost_date, packing_cost_date, pcfid, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(row.ClientType),
                int(row.ProdID),
                str(row.Product),
                str(row.Unit),
                float(row.ProductCost),
                float(row.PackingCost),
                float(row.TotalFactorCost),
                None
                if pd.isna(row.ProductCostDate)
                else str(row.ProductCostDate),
                None
                if pd.isna(getattr(row, "PackingCostDate", None))
                else str(row.PackingCostDate),
                None if pd.isna(row.PCFID) else float(row.PCFID),
                updated,
            ),
        )
        count += 1
    return count


def ingest_product_costs(path: Path | str, original_name: str | None = None) -> dict:
    """Archive product cost factors (all columns) and refresh factor_costs if packing exists."""
    init_db()
    source = Path(path)
    if not source.exists():
        raise IngestError(f"File not found: {source}")
    original_name = original_name or source.name
    content_hash = _file_hash(source)

    with connect() as conn:
        existing = _already_ingested(conn, "product_costs", content_hash)
        if existing:
            raise DuplicateFileError(
                f"Already ingested as {existing['stored_name']} on {existing['ingested_at']}"
            )

    raw = read_excel_raw(source, sheet_name="ProductCostFactors")
    # Detect header row
    header_row = PRODUCT_COST_HEADER_ROW
    for i in range(min(10, len(raw))):
        vals = [str(v).strip().lower() for v in raw.iloc[i].tolist()]
        if "clienttype" in vals and "cost" in vals:
            header_row = i
            break
    headers = [str(v).strip() if v is not None and not pd.isna(v) else f"col_{i}"
               for i, v in enumerate(raw.iloc[header_row].tolist())]
    body = raw.iloc[header_row + 1 :].copy()
    body.columns = headers
    body = body.dropna(how="all")
    records = dataframe_records(body)
    archived = _archive_upload("product_costs", source, original_name)

    inserted = 0
    factors = 0
    with connect() as conn:
        file_id = _register_file(
            conn,
            file_type="product_costs",
            original_name=original_name,
            stored_path=archived,
            content_hash=content_hash,
            row_count=0,
        )
        # Keep history: append lines from this file
        for payload in records:
            client = str(_cell(payload, "ClientType") or "").strip()
            product = str(_cell(payload, "Product") or "").strip()
            if not client or not product:
                continue
            conn.execute(
                """
                INSERT INTO product_cost_lines (
                    source_file_id, pcfid, date, client_type, prod_id, product,
                    unit, product_cost_center, cost, payload_json, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    _num(_cell(payload, "PCFID")),
                    _to_iso_date(_cell(payload, "Date")),
                    client,
                    _num(_cell(payload, "ProdID")),
                    product,
                    str(_cell(payload, "Unit") or "").strip() or None,
                    str(_cell(payload, "ProductCostCenter") or "").strip() or None,
                    _num(_cell(payload, "Cost")),
                    json_dumps(payload),
                    now_iso(),
                ),
            )
            inserted += 1
        factors = _refresh_factor_costs(conn)
        conn.execute(
            "UPDATE ingested_files SET row_count = ?, notes = ? WHERE id = ?",
            (inserted, f"factor_rows={factors}", file_id),
        )

    return {
        "file_type": "product_costs",
        "original_name": original_name,
        "stored_path": str(archived),
        "inserted": inserted,
        "factor_rows": factors,
        "content_hash": content_hash,
    }


def ingest_packing_costs(path: Path | str, original_name: str | None = None) -> dict:
    """Archive packing costs (all columns) and refresh factor_costs if product costs exist."""
    init_db()
    source = Path(path)
    if not source.exists():
        raise IngestError(f"File not found: {source}")
    original_name = original_name or source.name
    content_hash = _file_hash(source)

    with connect() as conn:
        existing = _already_ingested(conn, "packing_costs", content_hash)
        if existing:
            raise DuplicateFileError(
                f"Already ingested as {existing['stored_name']} on {existing['ingested_at']}"
            )

    raw = read_excel_raw(source, sheet_name=0)
    raw = raw.dropna(how="all")
    # Headerless Google export: ProdID, Product, Cost, Unit [, Date]
    first = [str(v).strip().lower() if v is not None else "" for v in raw.iloc[0].tolist()]
    has_header = any(h in first for h in ("prodid", "product", "cost", "unit", "date"))
    if has_header:
        headers = [str(v).strip() for v in raw.iloc[0].tolist()]
        body = raw.iloc[1:].copy()
        body.columns = headers
    else:
        cols = list(raw.columns)
        rename = {}
        names = ["ProdID", "Product", "Cost", "Unit", "Date"]
        for i, col in enumerate(cols):
            if i < len(names):
                rename[col] = names[i]
        body = raw.rename(columns=rename)
    body = body.dropna(how="all")
    records = dataframe_records(body)
    archived = _archive_upload("packing_costs", source, original_name)

    inserted = 0
    factors = 0
    with connect() as conn:
        file_id = _register_file(
            conn,
            file_type="packing_costs",
            original_name=original_name,
            stored_path=archived,
            content_hash=content_hash,
            row_count=0,
        )
        for order, payload in enumerate(records):
            prod_id = _num(_cell(payload, "ProdID", "ProductID", "ID"))
            if prod_id is None:
                continue
            conn.execute(
                """
                INSERT INTO packing_cost_lines (
                    source_file_id, prod_id, product, cost, unit, date,
                    row_order, payload_json, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    prod_id,
                    str(_cell(payload, "Product") or "").strip() or None,
                    _num(_cell(payload, "Cost", "PackingCost")),
                    str(_cell(payload, "Unit") or "").strip() or None,
                    _to_iso_date(_cell(payload, "Date")),
                    order,
                    json_dumps(payload),
                    now_iso(),
                ),
            )
            inserted += 1
        factors = _refresh_factor_costs(conn)
        conn.execute(
            "UPDATE ingested_files SET row_count = ?, notes = ? WHERE id = ?",
            (inserted, f"factor_rows={factors}", file_id),
        )

    return {
        "file_type": "packing_costs",
        "original_name": original_name,
        "stored_path": str(archived),
        "inserted": inserted,
        "factor_rows": factors,
        "content_hash": content_hash,
    }


def list_ingested_files(file_type: str | None = None) -> pd.DataFrame:
    init_db()
    with connect() as conn:
        if file_type:
            rows = conn.execute(
                """
                SELECT id, file_type, original_name, stored_name, ingested_at, row_count, notes
                FROM ingested_files WHERE file_type = ?
                ORDER BY ingested_at DESC, id DESC
                """,
                (file_type,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, file_type, original_name, stored_name, ingested_at, row_count, notes
                FROM ingested_files
                ORDER BY ingested_at DESC, id DESC
                """
            ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def load_sales_table(
    search: str = "",
    date_from: str | None = None,
    date_to: str | None = None,
    party: str | None = None,
    product: str | None = None,
    limit: int = 5000,
) -> pd.DataFrame:
    init_db()
    clauses = ["1=1"]
    params: list[Any] = []
    if date_from:
        clauses.append("date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("date <= ?")
        params.append(date_to)
    if party:
        clauses.append("party LIKE ?")
        params.append(f"%{party}%")
    if product:
        clauses.append("product LIKE ?")
        params.append(f"%{product}%")
    if search:
        clauses.append(
            "(party LIKE ? OR product LIKE ? OR inv_no LIKE ? OR client_type LIKE ?)"
        )
        q = f"%{search}%"
        params.extend([q, q, q, q])
    sql = f"""
        SELECT date, party, inv_no, srno, product, qty, unit, mes_qty, mes_unit,
               mt_qty, rate, basic_amount, incl_gst_fed_amount, client_type,
               imported_at, payload_json
        FROM sales
        WHERE {' AND '.join(clauses)}
        ORDER BY date DESC, id DESC
        LIMIT ?
    """
    params.append(limit)
    with connect() as conn:
        frame = pd.read_sql_query(sql, conn, params=params)
    return frame


def load_clients_table(search: str = "", client_type: str | None = None) -> pd.DataFrame:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT client_id, client, type, city_filter, city, inactive, payload_json, updated_at FROM clients"
        ).fetchall()
    records = []
    for row in rows:
        payload = {}
        try:
            import json

            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        # Prefer full original columns from payload
        if payload:
            records.append(payload)
        else:
            records.append(dict(row))
    frame = pd.DataFrame(records)
    if frame.empty:
        return frame

    # Excel payloads often mix int/str in the same column (breaks Streamlit/Arrow)
    for col in frame.columns:
        if pd.api.types.is_object_dtype(frame[col]):
            frame[col] = frame[col].map(
                lambda v: ""
                if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v)
                else str(v)
            )

    if client_type:
        type_col = "Type" if "Type" in frame.columns else "type"
        if type_col in frame.columns:
            frame = frame[frame[type_col].astype(str) == str(client_type)]
    if search:
        mask = pd.Series(False, index=frame.index)
        for col in frame.columns:
            mask = mask | frame[col].astype(str).str.contains(search, case=False, na=False)
        frame = frame[mask]
    # Stable sort by client name if present
    for name_col in ("Client", "client"):
        if name_col in frame.columns:
            frame = frame.sort_values(name_col, kind="mergesort")
            break
    return frame.reset_index(drop=True)


def load_factor_costs_table(client_type: str | None = None) -> pd.DataFrame:
    init_db()
    with connect() as conn:
        if client_type:
            frame = pd.read_sql_query(
                """
                SELECT client_type, prod_id, product, unit,
                       product_cost, packing_cost, total_factor_cost,
                       product_cost_date, packing_cost_date, pcfid, updated_at
                FROM factor_costs WHERE client_type = ?
                ORDER BY product
                """,
                conn,
                params=(client_type,),
            )
        else:
            frame = pd.read_sql_query(
                """
                SELECT client_type, prod_id, product, unit,
                       product_cost, packing_cost, total_factor_cost,
                       product_cost_date, packing_cost_date, pcfid, updated_at
                FROM factor_costs
                ORDER BY client_type, product
                """,
                conn,
            )
    return frame


def list_factor_client_types() -> list[str]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT client_type FROM factor_costs ORDER BY client_type"
        ).fetchall()
    return [r["client_type"] for r in rows]


def sales_count() -> int:
    init_db()
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM sales").fetchone()["n"])


def clients_count() -> int:
    init_db()
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM clients").fetchone()["n"])
