"""Deterministic unit tests for execute_query_spec — no LLM, raw QuerySpecs only."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.db import connect, init_db
from eva_dashboard.geo import resolve_city_zone, zone_for_city, resolve_city_label
from eva_dashboard.query_executor import execute_query_spec


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed_partial_august() -> None:
    """Sales through 2026-08-05 (partial August) + AMS history Apr–Jun."""
    init_db()
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, '{}', datetime('now'))",
            [
                ("Eva Canola", "Eva Consumer", "Eva Canola", "Stand up"),
                ("Eva Bulk Tin", "Eva Bulk", "Eva Bulk", "Tin"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "Alpha Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("2", "Beta Dist", "Eva Distributors", "Karachi", "Karachi"),
                ("3", "Gamma New", "Eva Distributors", "", ""),  # blank city
                ("4", "Delta OnlyJuly", "Eva Distributors", "Lahore", "Lahore"),
            ],
        )
        rows = [
            # Alpha: steady AMS ~30, July volume 20 → behind AMS
            ("2026-04-10", "Alpha Dist", "Eva Canola", 30.0),
            ("2026-05-10", "Alpha Dist", "Eva Canola", 30.0),
            ("2026-06-10", "Alpha Dist", "Eva Canola", 30.0),
            ("2026-07-05", "Alpha Dist", "Eva Canola", 20.0),
            ("2026-08-02", "Alpha Dist", "Eva Canola", 5.0),
            # Beta: AMS ~10, July volume 40 → ahead
            ("2026-04-11", "Beta Dist", "Eva Bulk Tin", 10.0),
            ("2026-05-11", "Beta Dist", "Eva Bulk Tin", 10.0),
            ("2026-06-11", "Beta Dist", "Eva Bulk Tin", 10.0),
            ("2026-07-06", "Beta Dist", "Eva Bulk Tin", 40.0),
            ("2026-08-03", "Beta Dist", "Eva Bulk Tin", 8.0),
            # Gamma: blank city in clients → Karachi/SOUTH; has AMS
            ("2026-04-12", "Gamma New", "Eva Canola", 15.0),
            ("2026-05-12", "Gamma New", "Eva Canola", 15.0),
            ("2026-06-12", "Gamma New", "Eva Canola", 15.0),
            ("2026-07-07", "Gamma New", "Eva Canola", 12.0),
            # Delta: July-only volume, no AMS baseline
            ("2026-07-08", "Delta OnlyJuly", "Eva Canola", 100.0),
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
                (f"se-{i}", dt, party, product, mt, mt),
            )
        conn.commit()


def test_geo_blank_city_defaults_to_karachi_south() -> None:
    """Geography fallback lives in Python geo.py — not the LLM."""
    assert resolve_city_label("") == "Karachi"
    assert resolve_city_label("undefined") == "Karachi"
    assert resolve_city_label(None) == "Karachi"
    assert zone_for_city("") == "SOUTH"
    assert zone_for_city("SomeUnknownTownXYZ") == "SOUTH"
    city, zone = resolve_city_zone("")
    assert city == "Karachi" and zone == "SOUTH"


def test_vs_ams_drops_zero_ams_baselines() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed_partial_august()
            out = execute_query_spec(
                {
                    "intent": "party_rank",
                    "context_handling": "none",
                    "clear_filters": [],
                    "period_type": "NAMED_MONTH",
                    "named_month": "July",
                    "group_by": "party",
                    "ranking_metric": "vs_ams",
                    "sort_order": "asc",
                    "title_mode": "underperformers",
                    "filters": {"client_type": "Eva Distributors"},
                    "business_units": ["Eva Consumer", "Eva Bulk"],
                }
            )
            assert out["ok"] is True, out
            parties = out.get("parties") or []
            names = [p["party"] for p in parties]
            # Delta has volume but AMS=0 — must not appear
            assert "Delta OnlyJuly" not in names
            for p in parties:
                assert (p.get("ams_mt") or 0) > 0
            # Lowest vs AMS should be Alpha (20 vs AMS 30) before Beta (40 vs 10)
            assert names[0] == "Alpha Dist"
            assert "Lowest" in (out.get("answer_markdown") or "")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_mtd_partial_month_period_type() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed_partial_august()
            out = execute_query_spec(
                {
                    "intent": "sales_matrix",
                    "context_handling": "none",
                    "clear_filters": [],
                    "period_type": "MTD",
                    "filters": {
                        "city": "Lahore",
                        "client_type": "Eva Distributors",
                    },
                    "business_units": ["Eva Consumer", "Eva Bulk"],
                }
            )
            assert out["ok"] is True, out
            label = str((out.get("period") or {}).get("label") or "")
            # Partial August through max sales date
            assert "2026-08" in label or "Aug" in label
            assert "MTD" in label or "through" in label.lower() or "Aug" in label
            md = out.get("answer_markdown") or ""
            assert "Lahore" in md
            assert "_No data._" not in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_last_n_months_and_blank_city_party_in_south_zone() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed_partial_august()
            # Month grid
            month_out = execute_query_spec(
                {
                    "intent": "sales_matrix",
                    "context_handling": "none",
                    "clear_filters": [],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "filters": {"client_type": "Eva Distributors"},
                    "business_units": ["Eva Consumer", "Eva Bulk"],
                }
            )
            assert month_out["ok"] is True, month_out
            assert month_out.get("column_dimension") == "month"
            assert "Aug 2026 MTD" not in (month_out.get("answer_markdown") or "")

            # Zone filter SOUTH should include Gamma (blank city → Karachi → SOUTH)
            zone_out = execute_query_spec(
                {
                    "intent": "party_rank",
                    "context_handling": "none",
                    "clear_filters": [],
                    "period_type": "NAMED_MONTH",
                    "named_month": "July",
                    "group_by": "party",
                    "ranking_metric": "volume",
                    "sort_order": "desc",
                    "filters": {"zone": "SOUTH", "client_type": "Eva Distributors"},
                }
            )
            assert zone_out["ok"] is True, zone_out
            names = [p["party"] for p in (zone_out.get("parties") or [])]
            assert "Gamma New" in names
            assert "Alpha Dist" not in names  # Lahore → CENTRAL
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
