"""Final edge cases: SPECIFIC_MONTH, fuzzy party, price_fetch metric."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.db import connect, init_db
from eva_dashboard.party_match import fuzzy_match_party
from eva_dashboard.query_executor import execute_query_spec
from eva_dashboard.query_spec import (
    PLAN_QUERY_TOOL,
    normalize_query_spec,
    resolve_period_from_spec,
)


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
                ("Eva Bulk Tin", "Eva Bulk", "Eva Bulk", "Tin"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "Alpha Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("2", "AL SHAHEER CORPORATION LIMITED", "Eva Distributors", "Karachi", "Karachi"),
                ("3", "AL SHAHEER TRADERS", "Eva Distributors", "Lahore", "Lahore"),
            ],
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO factor_costs
            (client_type, prod_id, product, unit, product_cost, packing_cost,
             total_factor_cost, updated_at)
            VALUES ('Eva Distributors', 1, 'Eva Canola Oil (StandUpPouch)', 'Ltrs',
                    100, 50, 150.0, datetime('now'))
            """
        )
        rows = [
            # March + later months
            ("2026-03-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 10.0, 500.0, 50000.0),
            ("2026-03-12", "AL SHAHEER CORPORATION LIMITED", "Eva Canola Oil (StandUpPouch)", 20.0, 510.0, 102000.0),
            ("2026-04-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 12.0, 520.0, 62400.0),
            ("2026-05-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 14.0, 530.0, 74200.0),
            ("2026-06-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 16.0, 540.0, 86400.0),
            ("2026-07-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 18.0, 520.0, 93600.0),
            ("2026-08-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 8.0, 525.0, 42000.0),
            ("2026-07-11", "AL SHAHEER TRADERS", "Eva Canola Oil (StandUpPouch)", 5.0, 500.0, 25000.0),
        ]
        for i, (dt, party, product, mt, rate, incl) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type,
                  payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, ?,
                          'Eva Distributors', '{}')
                """,
                (f"ec-{i}", dt, party, product, mt, mt, rate, incl),
            )
        conn.commit()


def test_schema_has_specific_month_and_price_fetch() -> None:
    props = PLAN_QUERY_TOOL["function"]["parameters"]["properties"]
    assert "SPECIFIC_MONTH" in props["period_type"]["enum"]
    assert "target_month" in props
    assert "price_fetch" in props["metrics"]["items"]["enum"]


def test_march_phrase_derives_specific_month() -> None:
    spec = normalize_query_spec(
        {
            "row_dimensions": ["business_unit"],
            "metrics": ["volume", "ams"],
            "period": {"phrase": "March"},
            "context_handling": "none",
            "business_units": ["Eva Consumer"],
        }
    )
    assert spec["period_type"] == "SPECIFIC_MONTH"


def test_specific_month_not_a_six_month_grid() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            plan = {
                "row_dimensions": ["business_unit"],
                "metrics": ["volume", "ams"],
                "period_type": "SPECIFIC_MONTH",
                "target_month": "2026-03",
                "context_handling": "none",
                "business_units": ["Eva Consumer"],
            }
            resolved = resolve_period_from_spec(normalize_query_spec(plan))
            assert "month" not in (resolved.get("column_dimensions") or [])
            period = resolved["period"]
            assert period.get("date_from", "").startswith("2026-03")
            assert period.get("date_to", "").startswith("2026-03")

            out = execute_query_spec(plan)
            assert out["ok"] is True, out
            assert out.get("column_dimension") != "month"
            md = out.get("answer_markdown") or ""
            assert "Mar 2026" in md or "March" in md or "2026-03" in (
                (out.get("period") or {}).get("label") or ""
            )
            # Must not be a Mar–Aug month grid
            assert "Jun 2026" not in md
            assert "AMS (6 months)" not in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_fuzzy_party_unique_match() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            # Unique fragment
            hit = fuzzy_match_party("CORPORATION LIMITED")
            assert hit["ok"] is True
            assert hit["party"] == "AL SHAHEER CORPORATION LIMITED"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_fuzzy_party_ambiguous_al_shaheer() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            hit = fuzzy_match_party("al shaheer")
            assert hit["ok"] is False
            assert "Ambiguous" in (hit.get("error") or "")
            assert len(hit.get("matches") or []) >= 2

            out = execute_query_spec(
                {
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "filters": {"party": "al shaheer"},
                }
            )
            assert out["ok"] is False
            assert out.get("matches")
            assert "Ambiguous" in (out.get("error") or "")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_price_fetch_metric_uses_engine_math() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "metrics": ["price_fetch"],
                    "row_dimensions": [],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "filters": {
                        "oil_type": "Eva Canola",
                        "packing_category": "Stand up",
                        "client_type": "Eva Distributors",
                    },
                }
            )
            assert out["ok"] is True, out
            assert out.get("include_price_fetch") is True
            assert out.get("price_fetch") is not None
            assert out.get("cost_factor") == 150.0
            md = out.get("answer_markdown") or ""
            assert "Price Fetch" in md
            assert "Cost Factor" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
