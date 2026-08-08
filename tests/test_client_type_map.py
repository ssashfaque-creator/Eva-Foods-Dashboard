"""Existing → new client-type grouping."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.client_language import (
    extract_client_type_from_text,
    normalize_client_type,
)
from eva_dashboard.client_type_map import map_client_type
from eva_dashboard.db import connect, init_db
from eva_dashboard.sales_query import query_sales


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def test_map_client_type_groups() -> None:
    assert map_client_type("CHASE UP") == "IMT"
    assert map_client_type("METRO HABIB") == "IMT"
    assert map_client_type("Canteen Store Department") == "IMT"
    assert map_client_type("SPAR - IMT") == "IMT"
    assert map_client_type("NORTH LMT") == "LMT"
    assert map_client_type("CENTRAL LMT") == "LMT"
    assert map_client_type("SOUTH LMT") == "LMT"
    assert map_client_type("GELANI MART") == "LMT"
    assert map_client_type("Local Dealers") == "Dealer"
    assert map_client_type("X-DEALERS") == "Dealer"
    assert map_client_type("Madarsa") == "DONATIONS"
    assert map_client_type("Utility Stores Corporation") == "USC"
    assert map_client_type("Direct Customers (Karachi)") == "Direct Customers"
    assert map_client_type("Online Customer") == "Online Customers"
    assert map_client_type("Eva Distributors") == "Eva Distributors"
    assert map_client_type("Imtiaz Store") == "Imtiaz Store"


def test_spoken_aliases_resolve_to_new_groups() -> None:
    assert normalize_client_type("chase up") == "IMT"
    assert normalize_client_type("metro") == "IMT"
    assert normalize_client_type("csd") == "IMT"
    assert normalize_client_type("north lmt") == "LMT"
    assert normalize_client_type("gelani") == "LMT"
    assert normalize_client_type("online") == "Online Customers"
    assert normalize_client_type("dealers") == "Dealer"
    assert extract_client_type_from_text("who are the CSD stores") == "IMT"
    assert extract_client_type_from_text("North LMT active") == "LMT"


def test_query_sales_pivots_by_new_client_type() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            init_db()
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO category "
                    "(product, category_1, category_2, packing_category, "
                    "payload_json, updated_at) VALUES "
                    "('P1', 'Eva Consumer', 'Eva Canola', 'Stand up', '{}', "
                    "datetime('now'))"
                )
                conn.executemany(
                    "INSERT OR REPLACE INTO clients "
                    "(client_id, client, type, city_filter, city, inactive, "
                    "payload_json, updated_at) VALUES "
                    "(?, ?, ?, 'Karachi', 'Karachi', '', '{}', datetime('now'))",
                    [
                        ("1", "Chase A", "CHASE UP"),
                        ("2", "Metro A", "METRO HABIB"),
                        ("3", "CSD A", "Canteen Store Department"),
                        ("4", "North LMT A", "NORTH LMT"),
                        ("5", "Dist A", "Eva Distributors"),
                    ],
                )
                for i, (party, mt, ctype) in enumerate(
                    [
                        ("Chase A", 10, "CHASE UP"),
                        ("Metro A", 20, "METRO HABIB"),
                        ("CSD A", 5, "Canteen Store Department"),
                        ("North LMT A", 8, "NORTH LMT"),
                        ("Dist A", 12, "Eva Distributors"),
                    ]
                ):
                    conn.execute(
                        """
                        INSERT INTO sales (
                          source_file_id, row_hash, imported_at, date, party,
                          product, qty, unit, mt_qty, client_type, payload_json
                        ) VALUES (NULL, ?, datetime('now'), '2026-07-01', ?,
                                  'P1', ?, 'MT', ?, ?, '{}')
                        """,
                        (f"ctm-{i}", party, mt, mt, ctype),
                    )
                conn.commit()

            by_type = query_sales(
                columns="month",
                months_back=3,
                row_dimension="client_type",
            )
            assert by_type["ok"] is True
            labels = {
                str(r.get("client_type"))
                for r in by_type["matrix"]["rows"]
                if r.get("client_type")
            }
            # Old labels must not appear
            assert "CHASE UP" not in labels
            assert "METRO HABIB" not in labels
            assert "NORTH LMT" not in labels
            assert "IMT" in labels
            assert "LMT" in labels
            assert "Eva Distributors" in labels

            imt = query_sales(
                client_type="chase up",
                columns="month",
                months_back=3,
            )
            assert imt["ok"] is True
            assert imt["filters"]["client_type"] == "IMT"
            # Chase + Metro + CSD = 35
            total = float(imt["matrix"]["rows"][0].get("Total") or 0) if imt[
                "matrix"
            ]["rows"] else 0
            # Month matrix may have Total column or sum months — use answer scope
            assert "IMT" in (imt.get("answer_markdown") or "")
            # Volume across months for filtered frame
            from eva_dashboard.sales_query import _fetch_lines

            frame = _fetch_lines(
                date_from="2026-05-01",
                date_to="2026-07-31",
                client_type="IMT",
            )
            assert float(frame["mt"].sum()) == 35.0
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
