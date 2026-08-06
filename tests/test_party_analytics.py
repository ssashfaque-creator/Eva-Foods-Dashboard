"""Tests for client lists and party analytics."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    _dispatch_tool,
    _looks_client_list,
    _looks_party_analytics,
    _looks_party_lookup,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.party_analytics import analyze_parties, list_clients
from eva_dashboard.sales_query import resolve_period


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
                ("Eva VTF Banaspati 1x5 Pouch", "Eva Consumer", "Eva VTF", "Pouch"),
                ("Eva Cooking Oil (16 Ltr Tin)", "Eva Bulk", "Eva Bulk", "Tin"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "Alpha Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("2", "Beta Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("3", "Gamma Dist", "Eva Distributors", "Karachi", "Karachi"),
                ("4", "Imtiaz A", "Imtiaz Store", "Lahore", "Lahore"),
                ("5", "Imtiaz B", "Imtiaz Store", "Karachi", "Karachi"),
                ("6", "Other Guy", "Other Clients", "Lahore", "Lahore"),
            ],
        )
        # Prior AMS months May–July; current Aug partial
        rows = []
        for month, day in [("05", 10), ("06", 10), ("07", 10)]:
            rows += [
                (f"2026-{month}-{day}", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30.0),
                (f"2026-{month}-{day}", "Beta Dist", "Eva Canola Oil (StandUpPouch)", 10.0),
                (f"2026-{month}-{day}", "Gamma Dist", "Eva Canola Oil (StandUpPouch)", 20.0),
                (f"2026-{month}-{day}", "Imtiaz A", "Eva VTF Banaspati 1x5 Pouch", 15.0),
                (f"2026-{month}-{day}", "Imtiaz B", "Eva VTF Banaspati 1x5 Pouch", 5.0),
                (f"2026-{month}-{day}", "Imtiaz A", "Eva Canola Oil (StandUpPouch)", 5.0),
            ]
        # July last year for YoY
        rows += [
            ("2025-07-10", "Alpha Dist", "Eva VTF Banaspati 1x5 Pouch", 8.0),
            ("2025-07-10", "Beta Dist", "Eva VTF Banaspati 1x5 Pouch", 20.0),
        ]
        # Current July VTF for YoY compare target
        rows += [
            ("2026-07-15", "Alpha Dist", "Eva VTF Banaspati 1x5 Pouch", 25.0),
            ("2026-07-15", "Beta Dist", "Eva VTF Banaspati 1x5 Pouch", 12.0),
        ]
        # August
        rows += [
            ("2026-08-01", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 12.0),
            ("2026-08-02", "Beta Dist", "Eva Canola Oil (StandUpPouch)", 2.0),
            ("2026-08-02", "Imtiaz A", "Eva VTF Banaspati 1x5 Pouch", 6.0),
            ("2026-08-03", "Other Guy", "Eva Canola Oil (StandUpPouch)", 1.0),
        ]
        for i, (dt, party, product, mt) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, '', '{}')
                """,
                (f"pa-{i}-{dt}-{party}", dt, party, product, mt, mt),
            )
        conn.commit()


def test_routing_client_list_vs_name_lookup() -> None:
    assert _looks_client_list("Who are my distributors in Lahore")
    assert not _looks_party_lookup("Who are my distributors in Lahore")
    assert _looks_party_lookup("Who is Al Bari?")
    assert _looks_party_analytics("Top 10 parties by AMS in Karachi")
    assert _looks_party_analytics("Which distributors grew VTF vs July last year")


def test_list_distributors_in_lahore() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = _dispatch_tool(
                "lookup_party",
                {"query": "Lahore"},
                user_text="Who are my distributors in Lahore",
            )
            assert out["ok"] is True
            assert out["mode"] == "list_clients"
            names = [c["client"] for c in out["clients"]]
            assert "Alpha Dist" in names
            assert "Beta Dist" in names
            assert "Other Guy" not in names
            assert "Gamma Dist" not in names
            assert all(c["client_type"] == "Eva Distributors" for c in out["clients"])
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_top_parties_ams_karachi() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = analyze_parties(
                period="August so far",
                city="Karachi",
                metric="ams",
                limit=5,
            )
            assert out["ok"] is True
            parties = [p["party"] for p in out["parties"]]
            assert "Gamma Dist" in parties or "Imtiaz B" in parties
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_imtiaz_vtf_share_and_geo_pct() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            share = analyze_parties(
                period="July 2026",
                client_type="Imtiaz Store",
                oil_type="Eva VTF",
                metric="share_of_segment",
                limit=5,
            )
            assert share["ok"] is True
            assert share["parties"][0]["party"] == "Imtiaz A"

            geo = analyze_parties(
                period="July 2026",
                oil_type="Eva VTF",
                metric="geo_share",
                share_city="Lahore",
            )
            assert geo["ok"] is True
            assert geo["share_pct"] is not None
            assert geo["share_pct"] > 50  # Imtiaz A VTF dominates in Lahore
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_distributors_vs_ams_and_yoy_vtf() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            well = analyze_parties(
                period="last 3 months",
                client_type="Eva Distributors",
                metric="vs_ams",
                limit=10,
            )
            assert well["ok"] is True
            assert well["parties"]

            yoy = analyze_parties(
                period="July 2026",
                compare_period="July last year",
                client_type="Eva Distributors",
                oil_type="Eva VTF",
                metric="yoy",
                limit=5,
            )
            assert yoy["ok"] is True
            # Alpha 25 vs 8 → bigger growth than Beta 12 vs 20
            assert yoy["parties"][0]["party"] == "Alpha Dist"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_resolve_last_quarter_and_last_year_month() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            q = resolve_period("last quarter")
            assert q["date_to"] == "2026-08-03"
            assert q["date_from"] == "2026-06-01"
            ly = resolve_period("July last year")
            assert ly["date_from"] == "2025-07-01"
            assert ly["date_to"] == "2025-07-31"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
