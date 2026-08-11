"""Tests for advanced analytics, seasonality, and whole-number MT."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.advanced_analytics import (
    compare_segments,
    detect_dumping,
    filter_entities,
    not_ordered,
    silent_parties,
)
from eva_dashboard.advanced_routing import looks_advanced
from eva_dashboard.chatbot import _dispatch_tool
from eva_dashboard.db import connect, init_db
from eva_dashboard.sales_query import query_sales
from eva_dashboard.seasonality import expected_month_close, recompute_seasonality


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
                ("Eva Cooking Oil Pillow 1L", "Eva Consumer", "Eva Cooking", "Pillow"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "Alpha Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("2", "Beta Dist", "Eva Distributors", "Karachi", "Karachi"),
                ("3", "Silent Guy", "Eva Distributors", "Lahore", "Lahore"),
                ("4", "Isb Dist", "Eva Distributors", "Islamabad", "Islamabad"),
                ("5", "Metro Lhr", "METRO HABIB", "Lahore", "Lahore"),
                ("6", "Chase Up Khi", "CHASE UP", "Karachi", "Karachi"),
                ("7", "Imtiaz Lhr", "Imtiaz Store", "Lahore", "Lahore"),
            ],
        )
        rows = []
        for month in ("05", "06", "07"):
            rows += [
                (f"2026-{month}-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30.0, "A"),
                (f"2026-{month}-05", "Beta Dist", "Eva Canola Oil (StandUpPouch)", 20.0, "B"),
                (f"2026-{month}-05", "Silent Guy", "Eva Canola Oil (StandUpPouch)", 15.0, "S"),
                (f"2026-{month}-05", "Isb Dist", "Eva Canola Oil (StandUpPouch)", 12.0, "I"),
                (f"2026-{month}-12", "Alpha Dist", "Eva Cooking Oil Pillow 1L", 8.0, "P"),
                (f"2026-{month}-12", "Metro Lhr", "Eva Canola Oil (StandUpPouch)", 18.0, "M"),
                (f"2026-{month}-12", "Chase Up Khi", "Eva Canola Oil (StandUpPouch)", 14.0, "C"),
                (f"2026-{month}-12", "Imtiaz Lhr", "Eva Canola Oil (StandUpPouch)", 22.0, "Z"),
            ]
        rows += [
            ("2026-08-01", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 10.0, "A8"),
            ("2026-08-04", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 90.0, "DUMP"),
            ("2025-08-02", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 10.0, "Y"),
            ("2025-08-02", "Beta Dist", "Eva Canola Oil (StandUpPouch)", 30.0, "Y2"),
            ("2025-08-02", "Isb Dist", "Eva Canola Oil (StandUpPouch)", 8.0, "Y3"),
        ]
        for i, (dt, party, product, mt, inv) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, inv_no, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, '', '{}')
                """,
                (f"advt-{i}", dt, party, product, mt, mt, inv),
            )
        conn.commit()


def test_mt_whole_numbers_and_seasonality_expected() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            mat = query_sales(
                period="August so far",
                business_unit="Eva Consumer",
                city="Lahore",
            )
            assert mat["ok"] is True
            for row in mat["matrix"]["rows"]:
                for k, v in row.items():
                    if k in {
                        "packing_category",
                        "business_unit",
                        "product",
                        "oil_type",
                        "row_kind",
                        "city",
                        "client_type",
                    }:
                        continue
                    assert isinstance(v, int), (k, v, type(v))
            assert mat["matrix"].get("hierarchical") is True
            sea = recompute_seasonality()
            assert sea["ok"] is True
            exp = expected_month_close(business_unit="Eva Consumer")
            assert exp["ok"] is True
            assert isinstance(exp["seasonality_projection_mt"], int)
            assert "### Analysis" in exp["answer_markdown"]
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous




