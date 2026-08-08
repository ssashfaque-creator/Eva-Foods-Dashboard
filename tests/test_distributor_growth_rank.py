"""Distributor growth / AMS / YoY ranking — not packing matrix or bare MT list."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    _dispatch_tool,
    _looks_party_breakdown,
    _looks_party_growth_rank,
    resolve_forced_tool,
    suggest_preferred_tool,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.party_analytics import infer_party_analytics_from_text


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
            # AMS window months
            ("2026-04-05", "Alpha Dist", "Eva VTF Pouch", 15.0),
            ("2026-05-05", "Alpha Dist", "Eva VTF Pouch", 15.0),
            ("2026-06-05", "Alpha Dist", "Eva VTF Pouch", 15.0),
            ("2026-04-05", "Beta Dist", "Eva VTF Pouch", 10.0),
            ("2026-05-05", "Beta Dist", "Eva VTF Pouch", 10.0),
            ("2026-06-05", "Beta Dist", "Eva VTF Pouch", 10.0),
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


def test_routing_distributor_growth_not_matrix_or_list() -> None:
    q1 = "which distributors have grown VTF sales since last year"
    q2 = (
        "show individual distributor sales for VTF with growth "
        "vs AMS and VS last year"
    )
    assert _looks_party_growth_rank(q1)
    assert _looks_party_growth_rank(q2)
    assert not _looks_party_breakdown(q2)
    assert resolve_forced_tool(q1) == "analyze_parties"
    assert resolve_forced_tool(q2) == "analyze_parties"
    assert suggest_preferred_tool(q1) == "analyze_parties"
    assert suggest_preferred_tool(q2) == "analyze_parties"

    inf1 = infer_party_analytics_from_text(q1)
    assert inf1["metric"] == "ams_growth"
    assert inf1["oil_type"] == "Eva VTF"
    assert inf1["client_type"] == "Eva Distributors"
    assert inf1.get("grown_only") is True

    inf2 = infer_party_analytics_from_text(q2)
    assert inf2["metric"] == "yoy_ams"
    assert inf2["oil_type"] == "Eva VTF"


def test_dispatch_yoy_and_yoy_ams_tables() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            q1 = "which distributors have grown VTF sales since last year"
            out = _dispatch_tool(
                "analyze_parties",
                {"period": "July 2026"},
                user_text=q1,
            )
            assert out["ok"] is True
            assert out["metric"] == "ams_growth"
            assert out["filters"]["oil_type"] == "Eva VTF"
            parties = [p["party"] for p in out["parties"]]
            assert "Alpha Dist" in parties
            # Grown-only: Beta declined YoY and should be excluded
            assert "Beta Dist" not in parties
            md = out["answer_markdown"]
            assert "YoY" in md
            assert "AMS growth" in md
            assert "Business Unit" not in md  # not a packing matrix

            # Wrong tool choice still redirects
            via_list = _dispatch_tool(
                "list_clients",
                {},
                user_text=q1,
            )
            assert via_list.get("metric") == "ams_growth"

            q2 = (
                "show individual distributor sales for VTF with growth "
                "vs AMS and VS last year"
            )
            both = _dispatch_tool(
                "analyze_parties",
                {"period": "July 2026"},
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
