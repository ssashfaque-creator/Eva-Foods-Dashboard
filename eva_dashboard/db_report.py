"""Build PDF reports from data already stored in SQLite."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from eva_dashboard.data import (
    normalize_name,
    prepare_report_from_frames,
    resolve_credit_days,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.paths import data_root
from eva_dashboard.report import generate_pdf


def reports_dir() -> Path:
    path = data_root() / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def available_sales_dates() -> list[date]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT date FROM sales
            WHERE date IS NOT NULL
            ORDER BY date
            """
        ).fetchall()
    out: list[date] = []
    for row in rows:
        try:
            out.append(date.fromisoformat(str(row["date"])[:10]))
        except ValueError:
            continue
    return out


def _sales_frame_from_db() -> pd.DataFrame:
    init_db()
    with connect() as conn:
        frame = pd.read_sql_query(
            """
            SELECT date, party, product, qty, unit, mes_qty, mes_unit, mt_qty,
                   rate, basic_amount, incl_gst_fed_amount AS incl_gst_fed,
                   client_type AS sales_client_type
            FROM sales
            ORDER BY date, id
            """,
            conn,
        )
    if frame.empty:
        raise ValueError("No sales in the database yet. Import a sales file first.")

    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    frame = frame.dropna(subset=["date", "product"]).copy()
    frame["product"] = frame["product"].astype(str).str.strip()
    frame["party"] = frame["party"].fillna("").astype(str).str.strip()
    frame["sales_client_type"] = (
        frame["sales_client_type"].fillna("").astype(str).str.strip()
    )
    frame.loc[
        frame["sales_client_type"].str.lower().isin({"nan", "none", ""}),
        "sales_client_type",
    ] = ""
    for col in ("qty", "mes_qty", "mt_qty", "rate", "basic_amount", "incl_gst_fed"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0.0)
    frame["unit"] = frame["unit"].fillna("").astype(str).str.strip()
    frame["mes_unit"] = frame["mes_unit"].fillna("").astype(str).str.strip()
    frame.loc[frame["mes_unit"].str.lower().isin({"nan", "none"}), "mes_unit"] = ""

    from eva_dashboard.data import effective_mt_qty

    frame["effective_mt"] = [
        effective_mt_qty(q, u, m)
        for q, u, m in zip(frame["qty"], frame["unit"], frame["mt_qty"], strict=True)
    ]
    frame["party_key"] = frame["party"].map(normalize_name)
    return frame.reset_index(drop=True)


def _category_frame_from_db() -> pd.DataFrame:
    """Load product categories from the database (uploaded category file)."""
    from eva_dashboard.ingest import load_category_map_from_db

    return load_category_map_from_db()


def _payload_get(payload: dict, *names: str) -> Any:
    lower = {str(k).strip().lower(): v for k, v in payload.items()}
    for name in names:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _clients_frame_from_db() -> pd.DataFrame | None:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT client_id, client, type, city_filter, city, inactive, payload_json
            FROM clients
            """
        ).fetchall()
    if not rows:
        return None

    records = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        client = str(row["client"] or _payload_get(payload, "Client") or "").strip()
        if not client:
            continue
        payment = str(_payload_get(payload, "PaymentType") or "").strip()
        cr_days = _payload_get(payload, "CrDays")
        records.append(
            {
                "party_key": normalize_name(client),
                "client_id": str(row["client_id"] or _payload_get(payload, "ClientID") or "").strip(),
                "client": client,
                "locality": str(_payload_get(payload, "Locality") or "").strip(),
                "zone": str(_payload_get(payload, "Zone") or "").strip(),
                "area": str(_payload_get(payload, "Area") or "").strip(),
                "territory": str(_payload_get(payload, "Territory") or "").strip(),
                "client_type": str(row["type"] or _payload_get(payload, "Type") or "").strip(),
                "city": str(
                    row["city_filter"] or _payload_get(payload, "City-Filter") or ""
                ).strip(),
                "payment_type": payment,
                "credit_days": resolve_credit_days(cr_days, payment),
                "cr_limit": pd.to_numeric(
                    _payload_get(payload, "CrLimit"), errors="coerce"
                ),
                "_inactive": str(row["inactive"] or "").strip().upper() == "Y",
                "_city_missing": (
                    str(row["city_filter"] or "").strip() == ""
                    or str(row["city_filter"] or "").strip().lower() == "undefined"
                ),
            }
        )

    clients = pd.DataFrame(records)
    if clients.empty:
        return None
    for col in ("locality", "zone", "area", "territory", "client_type", "payment_type", "city"):
        clients[col] = clients[col].fillna("").astype(str).str.strip()
        clients.loc[clients[col].str.lower().isin({"nan", "none"}), col] = ""
    # Never leave None in sort keys (mixed None/int ClientIDs crash sort)
    clients["client_id"] = clients["client_id"].fillna("").astype(str).str.strip()
    clients.loc[clients["client_id"].str.lower().isin({"nan", "none"}), "client_id"] = ""
    clients = clients.sort_values(
        ["_inactive", "_city_missing", "client_id"], kind="mergesort"
    )
    clients = clients.drop_duplicates("party_key", keep="first")
    return clients[
        [
            "party_key",
            "client_id",
            "client",
            "locality",
            "zone",
            "area",
            "territory",
            "client_type",
            "city",
            "payment_type",
            "credit_days",
            "cr_limit",
        ]
    ].reset_index(drop=True)


def _factor_costs_from_db() -> pd.DataFrame | None:
    init_db()
    with connect() as conn:
        frame = pd.read_sql_query(
            """
            SELECT client_type AS ClientType,
                   product AS Product,
                   unit AS Unit,
                   total_factor_cost AS TotalFactorCost
            FROM factor_costs
            """,
            conn,
        )
    if frame.empty:
        return None
    frame["ClientType"] = frame["ClientType"].astype(str).str.strip()
    frame["Product"] = frame["Product"].astype(str).str.strip()
    frame["Unit"] = frame["Unit"].astype(str).str.strip()
    frame["TotalFactorCost"] = pd.to_numeric(frame["TotalFactorCost"], errors="coerce")
    frame = frame.dropna(subset=["ClientType", "Product", "TotalFactorCost"])
    frame = frame.drop_duplicates(subset=["ClientType", "Product"], keep="last")
    return frame.reset_index(drop=True)


def prepare_report_from_db(report_date: date | None = None):
    """Build SalesReportData from SQLite (sales + category + clients + factor costs)."""
    sales = _sales_frame_from_db()
    categories = _category_frame_from_db()
    clients = _clients_frame_from_db()
    factors = _factor_costs_from_db()
    return prepare_report_from_frames(
        sales,
        categories,
        clients=clients,
        report_date=report_date,
        factor_costs=factors,
        source_path=data_root() / "eva.db",
        clients_path=data_root() / "eva.db" if clients is not None else None,
    )


def generate_sales_dashboard_pdf(
    report_date: date | None = None,
    output_path: Path | str | None = None,
) -> Path:
    """Generate the sales dashboard PDF from DB data and return the output path."""
    data = prepare_report_from_db(report_date=report_date)
    if output_path is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = (
            reports_dir()
            / f"sales_report_{data.report_date.isoformat()}_{stamp}.pdf"
        )
    return generate_pdf(data, output_path)


def list_generated_reports(limit: int = 20) -> list[Path]:
    folder = reports_dir()
    files = sorted(folder.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]
