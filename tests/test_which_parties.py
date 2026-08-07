"""which/what + client type → individual parties with filters."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    _dispatch_tool,
    _extract_business_units_from_text,
    _looks_which_parties_ask,
    resolve_forced_tool,
    suggest_preferred_tool,
)
from eva_dashboard.db import connect, init_db


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed() -> None:
    init_db()
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, '{}', datetime('now'))",
            [
                ("Eva Canola Oil (StandUpPouch)", "Eva Consumer", "Eva Canola", "Stand up"),
                ("Maan Canola Oil", "Maan Consumer", "Maan Canola", "Stand up"),
                ("Eva VTF Banaspati 16 Kg Tin", "Eva Bulk", "Eva VTF", "16 ltr / 16 Kg"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "Alpha Dist", "Eva Distributors", "Karachi", "Karachi"),
                ("2", "Gamma Dist", "Eva Distributors", "Karachi", "Karachi"),
                ("3", "Beta Store", "Imtiaz Store", "Lahore", "Lahore"),
                ("4", "Delta Store", "Imtiaz Store", "Lahore", "Lahore"),
                ("5", "Epsilon Dist", "Eva Distributors", "Lahore", "Lahore"),
            ],
        )
        rows = [
            ("2026-07-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 40, "Eva Distributors"),
            ("2026-07-06", "Gamma Dist", "Maan Canola Oil", 12, "Eva Distributors"),
            ("2026-07-07", "Alpha Dist", "Maan Canola Oil", 3, "Eva Distributors"),
            ("2026-07-08", "Beta Store", "Eva VTF Banaspati 16 Kg Tin", 25, "Imtiaz Store"),
            ("2026-07-09", "Delta Store", "Eva VTF Banaspati 16 Kg Tin", 10, "Imtiaz Store"),
            ("2026-07-10", "Epsilon Dist", "Eva Canola Oil (StandUpPouch)", 15, "Eva Distributors"),
        ]
        for i, (dt, party, prod, mt, ct) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, '{}')
                """,
                (f"wp-{i}", dt, party, prod, mt, mt, ct),
            )
        conn.commit()


def test_bare_maan_extracts_maan_consumer() -> None:
    assert _extract_business_units_from_text("selling maan") == ["Maan Consumer"]
    assert _looks_which_parties_ask("which distributor is selling maan")
    assert _looks_which_parties_ask("what Imtiaz store sells the most VTF")
    assert _looks_which_parties_ask("what distributors are active in Lahore")


def test_selling_maan_inherits_prior_and_filters_bu() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            prior = {
                "filters": {
                    "city": "Karachi",
                    "client_type": "Eva Distributors",
                },
                "column_dimension": "month",
                "months_back": 6,
                "period": {
                    "date_from": "2026-03-01",
                    "date_to": "2026-08-07",
                    "label": "Last 6 months",
                },
            }
            q = "which distributor is selling maan"
            assert (
                resolve_forced_tool(q, prior_table_spec=prior, explicit_followup=True)
                == "list_clients"
            )
            assert suggest_preferred_tool(q, prior_table_spec=prior) == "list_clients"
            out = _dispatch_tool(
                "query_sales",
                {},
                user_text=q,
                prior_spec=prior,
            )
            assert out.get("ok") is True
            assert out.get("mode") == "list_clients"
            assert out.get("filters", {}).get("city") == "Karachi"
            assert out.get("filters", {}).get("client_type") == "Eva Distributors"
            assert out.get("filters", {}).get("business_unit") == "Maan Consumer"
            clients = [c["client"] for c in (out.get("clients") or [])]
            assert "Gamma Dist" in clients
            assert "Alpha Dist" in clients
            # Pure Eva Consumer-only parties with no Maan must not appear alone as the answer
            assert clients[0] == "Gamma Dist"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_imtiaz_sells_most_vtf_ranks_stores() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            q = "what Imtiaz store sells the most VTF"
            assert suggest_preferred_tool(q) == "analyze_parties"
            out = _dispatch_tool("list_clients", {}, user_text=q)
            assert out.get("ok") is True
            assert out.get("filters", {}).get("client_type") == "Imtiaz Store"
            assert out.get("filters", {}).get("oil_type") == "Eva VTF"
            rows = out.get("rows") or out.get("parties") or out.get("clients") or []
            # analyze_parties payload shape
            md = out.get("answer_markdown") or ""
            assert "Beta Store" in md
            assert "VTF" in md or "Eva VTF" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_active_distributors_in_lahore() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            prior = {
                "filters": {"city": "Karachi", "client_type": "Eva Distributors"},
                "column_dimension": "month",
            }
            q = "what distributors are active in Lahore"
            # Must not be forced back onto the Karachi sales matrix on Reply
            assert (
                resolve_forced_tool(q, prior_table_spec=prior, explicit_followup=True)
                == "list_clients"
            )
            out = _dispatch_tool(
                "query_sales", {}, user_text=q, prior_spec=prior
            )
            assert out.get("mode") == "list_clients"
            assert out.get("filters", {}).get("city") == "Lahore"
            assert out.get("filters", {}).get("client_type") == "Eva Distributors"
            clients = {c["client"] for c in (out.get("clients") or [])}
            assert "Epsilon Dist" in clients
            assert "Alpha Dist" not in clients
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
