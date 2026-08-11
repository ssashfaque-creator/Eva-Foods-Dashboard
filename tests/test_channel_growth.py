"""Channels (= client types) grew / declined → Volume + AMS + %."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.advanced_routing import looks_advanced
from eva_dashboard.chatbot import (
    _dispatch_tool,
    _looks_channel_growth_ask,
    extract_regroup_dimension,
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
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "Alpha Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("2", "Beta Store", "Imtiaz Store", "Karachi", "Karachi"),
            ],
        )
        rows = []
        for m, dist_mt, imtiaz_mt in [
            ("04", 30, 10),
            ("05", 30, 10),
            ("06", 30, 10),
            ("07", 28, 80),  # Imtiaz grows hard; distributors soft
        ]:
            rows.append(
                (f"2026-{m}-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", dist_mt, "Eva Distributors")
            )
            rows.append(
                (f"2026-{m}-06", "Beta Store", "Eva Canola Oil (StandUpPouch)", imtiaz_mt, "Imtiaz Store")
            )
        for i, (dt, party, prod, mt, ct) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, '{}')
                """,
                (f"ch-{i}", dt, party, prod, mt, mt, ct),
            )
        conn.commit()




def test_channel_growth_uses_client_type_trend() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            prior = {
                "filters": {
                    "business_unit": "Eva Consumer",
                    "client_type": None,
                    "city": None,
                },
                "period_phrase": "July 2026",
                "period": {
                    "date_from": "2026-07-01",
                    "date_to": "2026-07-31",
                    "label": "Jul 2026",
                },
                "column_dimension": "city",
                "row_dimension": "packing_category",
                "business_units": ["Eva Consumer"],
            }
            out = _dispatch_tool(
                "query_sales",
                {},
                user_text="which channels grew sales and which declined",
                prior_spec=prior,
            )
            assert out.get("ok") is True
            assert out.get("mode") == "trend"
            assert out.get("row_dimension") == "client_type"
            assert out.get("filters", {}).get("business_unit") == "Eva Consumer"
            assert out.get("filters", {}).get("client_type") is None
            assert (out.get("period") or {}).get("date_from", "").startswith("2026-07")
            trend = out.get("trend") or {}
            cols = trend.get("columns") or []
            assert "volume_mt" in cols and "ams_mt" in cols
            assert "pct_vs_ams" in cols or "pct_vs_expected" in cols
            labels = {
                str(r.get("client_type"))
                for r in (trend.get("rows") or [])
                if r.get("client_type") not in {None, "Total"}
            }
            assert "Imtiaz Store" in labels
            assert "Direct Customers" in labels  # Eva Distributors rolls up here
            md = out.get("answer_markdown") or ""
            assert "Stand up" not in md  # not packing view
            assert "Volume vs AMS" in md or "AMS (MT)" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
