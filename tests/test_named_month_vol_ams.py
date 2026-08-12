"""Named-month Volume vs AMS: Eva brand, correct year, AMS + YoY columns."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.db import connect, init_db
from eva_dashboard.query_executor import _coerce_vocab_from_user_text, execute_query_spec


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed() -> None:
    init_db()
    with connect() as conn:
        for prod, bu, oil in (
            ("P1", "Eva Consumer", "Eva Canola"),
            ("P2", "Eva Bulk", "Eva Bulk"),
        ):
            conn.execute(
                "INSERT OR REPLACE INTO category "
                "(product, category_1, category_2, packing_category, "
                "payload_json, updated_at) VALUES "
                "(?, ?, ?, ?, '{}', datetime('now'))",
                (prod, bu, oil, "Stand up" if bu == "Eva Consumer" else "Bulk"),
            )
        conn.execute(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, "
            "payload_json, updated_at) VALUES "
            "('1', 'Alpha', 'Eva Distributors', 'Lahore', 'Lahore', '', "
            "'{}', datetime('now'))"
        )
        for i, (dt, prod, mt) in enumerate(
            [
                ("2026-04-10", "P1", 90),
                ("2026-05-10", "P1", 100),
                ("2026-06-10", "P1", 110),
                ("2026-04-10", "P2", 30),
                ("2026-05-10", "P2", 40),
                ("2026-06-10", "P2", 50),
                ("2026-07-10", "P1", 128),
                ("2026-07-10", "P2", 20),
                ("2025-07-10", "P1", 80),
                ("2025-07-10", "P2", 15),
                ("2026-08-05", "P1", 10),
            ]
        ):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type,
                  payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, 'Alpha', ?, ?, 'MT', ?,
                  100, ?, 'Eva Distributors', '{}')
                """,
                (f"nm-{i}", dt, prod, mt, mt, mt * 100),
            )
        conn.commit()


def test_eva_july_expands_both_bus_and_fixes_year():
    q = "show me Eva sales in lahore for july"
    out = _coerce_vocab_from_user_text(
        {
            "row_dimensions": ["business_unit"],
            "column_dimensions": ["client_type"],
            "metrics": ["volume", "ams", "vs_ams"],
            "period_type": "SPECIFIC_MONTH",
            "target_month": "2025-07",
            "filters": {"city": "Lahore"},
            "business_units": ["Eva Consumer"],
            "state_action": "clear",
        },
        q,
    )
    assert out.get("business_units") == ["Eva Consumer", "Eva Bulk"]


def test_eva_july_lahore_volume_ams_yoy():
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        _seed()
        q = "show me Eva sales in lahore for july"
        out = execute_query_spec(
            {
                "row_dimensions": ["business_unit"],
                "column_dimensions": ["client_type"],
                "metrics": ["volume", "ams", "vs_ams"],
                "period_type": "SPECIFIC_MONTH",
                "target_month": "2025-07",  # wrong year from planner
                "filters": {"city": "Lahore"},
                "business_units": ["Eva Consumer"],  # incomplete brand
                "state_action": "clear",
            },
            user_text=q,
        )
        assert out.get("ok"), out.get("plan_errors") or out.get("error")
        qs = out.get("query_spec") or {}
        assert qs.get("target_month") == "2026-07"
        assert qs.get("business_units") == ["Eva Consumer", "Eva Bulk"]
        assert out.get("mode") == "trend"
        trend = out.get("trend") or {}
        cols = trend.get("columns") or []
        assert "volume_mt" in cols
        assert "ams_mt" in cols
        assert "pct_vs_ams" in cols
        assert "prior_year_mt" in cols
        assert "yoy_pct" in cols
        rows = {
            str(r.get("business_unit")): r
            for r in (trend.get("rows") or [])
            if r.get("business_unit") != "Total"
        }
        assert rows["Eva Consumer"]["volume_mt"] == 128
        assert rows["Eva Consumer"]["ams_mt"] == 100
        assert rows["Eva Consumer"]["prior_year_mt"] == 80
        assert rows["Eva Bulk"]["volume_mt"] == 20
        assert rows["Eva Bulk"]["ams_mt"] == 40
        md = out.get("answer_markdown") or ""
        assert "Volume vs AMS" in md
        assert "YoY" in md
        assert "Eva Bulk" in md
