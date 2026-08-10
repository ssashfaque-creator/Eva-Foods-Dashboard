"""Individual distributor breakdown keeps the prior month sales grid."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    _dispatch_tool,
    _wants_party_month_matrix,
    resolve_forced_tool,
    resolve_regroup_request,
    suggest_preferred_tool,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.sales_query import query_sales
from eva_dashboard.table_export import (
    build_excel_bytes,
    build_pdf_bytes,
    export_payload_from_followup,
    matrix_to_records,
)


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed() -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, payload_json, updated_at) "
            "VALUES ('P1', 'Eva Consumer', 'Eva Canola', 'Stand up', '{}', datetime('now'))"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, 'Eva Distributors', 'Lahore', 'Lahore', '', '{}', datetime('now'))",
            [
                ("1", "Alpha Dist"),
                ("2", "Beta Dist"),
            ],
        )
        rows = [
            ("2026-03-01", "Alpha Dist", 10),
            ("2026-04-01", "Alpha Dist", 12),
            ("2026-05-01", "Alpha Dist", 11),
            ("2026-06-01", "Alpha Dist", 13),
            ("2026-07-01", "Alpha Dist", 14),
            ("2026-08-01", "Alpha Dist", 9),
            ("2026-03-01", "Beta Dist", 5),
            ("2026-04-01", "Beta Dist", 6),
            ("2026-05-01", "Beta Dist", 7),
            ("2026-06-01", "Beta Dist", 8),
            ("2026-07-01", "Beta Dist", 9),
            ("2026-08-01", "Beta Dist", 4),
        ]
        for i, (dt, party, mt) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, 'P1', ?, 'MT', ?,
                          'Eva Distributors', '{}')
                """,
                (f"pmd-{i}", dt, party, mt, mt),
            )
        conn.commit()


def test_individual_distributor_breakdown_keeps_month_grid() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            prior = query_sales(
                city="Lahore",
                client_type="Eva Distributors",
                columns="month",
                months_back=6,
                row_dimension="business_unit",
            )["table_spec"]
            assert prior["column_dimension"] == "month"
            assert _wants_party_month_matrix(
                "Can you show individual distributor breakdown", prior
            )
            assert (
                resolve_forced_tool(
                    "Can you show individual distributor breakdown",
                    prior_table_spec=prior,
                    explicit_followup=True,
                )
                == "query_sales"
            )
            rg = resolve_regroup_request(
                "Can you show individual distributor breakdown",
                prior_spec=prior,
            )
            assert rg is not None
            assert rg["row_dimension"] == "party"
            assert rg["row_groups"] == ["business_unit"]
            assert rg["columns"] == "month"

            out = _dispatch_tool(
                "list_clients",
                {},
                user_text="Can you show individual distributor breakdown",
                prior_spec=prior,
            )
            assert out["ok"] is True
            assert out.get("column_dimension") == "month"
            assert out.get("row_dimension") == "party"
            headers = (out.get("matrix") or {}).get("row_headers") or []
            assert "party" in headers
            assert "business_unit" in headers
            # Month columns preserved (plus AMS helpers / Total)
            cols = (out.get("matrix") or {}).get("columns") or []
            assert any("2026" in str(c) for c in cols)
            parties = {
                str(r.get("party"))
                for r in (out.get("matrix") or {}).get("rows") or []
                if r.get("party") and r.get("row_kind") != "total"
            }
            assert "Alpha Dist" in parties
            assert "Beta Dist" in parties
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_cold_individual_distributors_still_lists() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            assert (
                suggest_preferred_tool(
                    "By individual distributors", prior_table_spec=None
                )
                == "list_clients"
            )
            out = _dispatch_tool(
                "list_clients",
                {},
                user_text="By individual distributors in Lahore",
            )
            assert out["ok"] is True
            assert out["mode"] == "list_clients"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_table_export_excel_and_pdf() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            result = query_sales(
                city="Lahore",
                client_type="Eva Distributors",
                columns="month",
                months_back=6,
                row_dimension="party",
            )
            headers, data, dim_count = matrix_to_records(result["matrix"])
            xlsx = build_excel_bytes(
                title="Test sales",
                subtitle="Lahore · Eva Distributors",
                headers=headers,
                data=data,
            )
            pdf = build_pdf_bytes(
                title="Test sales",
                subtitle="Lahore · Eva Distributors",
                headers=headers,
                data=data,
                dim_count=dim_count,
            )
            assert xlsx[:2] == b"PK"  # zip/xlsx
            assert pdf[:4] == b"%PDF"
            snap = {
                "export": {
                    "title": "Test sales",
                    "subtitle": "Lahore",
                    "headers": headers,
                    "data": data,
                    "dim_count": dim_count,
                    "filename_stem": "eva_sales_table",
                }
            }
            payload = export_payload_from_followup(snap)
            assert payload and payload["ok"] is True
            assert payload["headers"] == headers
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
