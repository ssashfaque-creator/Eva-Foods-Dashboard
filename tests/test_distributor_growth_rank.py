"""Distributor growth / AMS / YoY ranking — not packing matrix or bare MT list."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    _dispatch_tool,
    _looks_national_scope,
    _looks_party_breakdown,
    _looks_party_growth_rank,
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
                ("Eva VTF Pouch", "Eva Consumer", "Eva VTF", "Pouch (ghee)"),
                ("Eva Canola Stand", "Eva Consumer", "Eva Canola", "Stand up"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "Alpha Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("2", "Beta Dist", "Eva Distributors", "Karachi", "Karachi"),
                ("3", "Gamma Dist", "Eva Distributors", "Lahore", "Lahore"),
            ],
        )
        rows = [
            # Current July 2026 VTF
            ("2026-07-05", "Alpha Dist", "Eva VTF Pouch", 40.0),
            ("2026-07-06", "Beta Dist", "Eva VTF Pouch", 12.0),
            ("2026-07-07", "Gamma Dist", "Eva VTF Pouch", 5.0),
            # Prior July 2025 VTF — Alpha grew, Beta declined, Gamma flat-ish
            ("2025-07-05", "Alpha Dist", "Eva VTF Pouch", 10.0),
            ("2025-07-06", "Beta Dist", "Eva VTF Pouch", 20.0),
            ("2025-07-07", "Gamma Dist", "Eva VTF Pouch", 5.0),
            # Current AMS window (Apr–Jun 2026)
            ("2026-04-05", "Alpha Dist", "Eva VTF Pouch", 15.0),
            ("2026-05-05", "Alpha Dist", "Eva VTF Pouch", 15.0),
            ("2026-06-05", "Alpha Dist", "Eva VTF Pouch", 15.0),
            ("2026-04-05", "Beta Dist", "Eva VTF Pouch", 4.0),
            ("2026-05-05", "Beta Dist", "Eva VTF Pouch", 4.0),
            ("2026-06-05", "Beta Dist", "Eva VTF Pouch", 4.0),
            # Prior AMS window (Jan–Mar 2026) — Beta declined AMS, Alpha grew
            ("2026-01-05", "Alpha Dist", "Eva VTF Pouch", 8.0),
            ("2026-02-05", "Alpha Dist", "Eva VTF Pouch", 8.0),
            ("2026-03-05", "Alpha Dist", "Eva VTF Pouch", 8.0),
            ("2026-01-05", "Beta Dist", "Eva VTF Pouch", 12.0),
            ("2026-02-05", "Beta Dist", "Eva VTF Pouch", 12.0),
            ("2026-03-05", "Beta Dist", "Eva VTF Pouch", 12.0),
        ]
        for i, (dt, party, product, mt) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?,
                          'Eva Distributors', '{}')
                """,
                (f"dg-{i}", dt, party, product, mt, mt),
            )
        conn.commit()




def test_dispatch_yoy_and_yoy_ams_tables() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            q1 = "which distributors have grown VTF sales since last year"
            out = _dispatch_tool(
                "analyze_parties",
                {
                    "period": "July 2026",
                    "metric": "ams_growth",
                    "oil_type": "Eva VTF",
                    "client_type": "Eva Distributors",
                    "grown_only": True,
                    "sort": "desc",
                },
                user_text=q1,
            )
            assert out["ok"] is True
            assert out["metric"] == "ams_growth"
            assert out["filters"]["oil_type"] == "Eva VTF"
            parties = [p["party"] for p in out["parties"]]
            assert "Alpha Dist" in parties
            # Grown-only: Beta declined and should be excluded
            assert "Beta Dist" not in parties
            md = out["answer_markdown"]
            assert "AMS growth" in md or "AMS gains" in md
            assert "AMS current (" in md
            assert "AMS prior (" in md
            assert "| Prior (MT) |" not in md
            assert "Business Unit" not in md

            q2 = (
                "show individual distributor sales for VTF with growth "
                "vs AMS and VS last year"
            )
            both = _dispatch_tool(
                "analyze_parties",
                {
                    "period": "July 2026",
                    "metric": "yoy_ams",
                    "oil_type": "Eva VTF",
                    "client_type": "Eva Distributors",
                },
                user_text=q2,
            )
            assert both["ok"] is True
            assert both["metric"] == "yoy_ams"
            md2 = both["answer_markdown"]
            assert "YoY" in md2
            assert "% vs AMS" in md2 or "AMS" in md2
            assert "Volume" in md2
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous




def test_nationally_clears_sticky_city_on_ams_decline() -> None:
    q = "Can you show nationally which distributors have had a decline in AMS"
    assert _looks_national_scope(q)
    assert _looks_party_growth_rank(q)

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
                "row_dimension": "packing_category",
                "period": {
                    "date_from": "2026-03-01",
                    "date_to": "2026-08-05",
                },
            }
            out = _dispatch_tool(
                "analyze_parties",
                {},
                user_text=q,
                prior_spec=prior,
            )
            assert out["ok"] is True
            assert out["metric"] == "ams_growth"
            assert out["filters"].get("city") is None, out["filters"]
            md = out["answer_markdown"]
            assert "city Karachi" not in md.lower()
            assert "AMS growth" in md
            parties = {p["party"] for p in out["parties"]}
            # National: both Lahore and Karachi distributors can appear
            assert "Alpha Dist" in parties or "Beta Dist" in parties
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
