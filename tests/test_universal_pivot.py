"""Universal Pivot — customer-wise price trends and schema normalize."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.db import connect, init_db
from eva_dashboard.query_executor import execute_query_spec
from eva_dashboard.query_spec import normalize_query_spec, validate_query_spec


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
        rows = [
            ("2026-05-10", "Alpha Dist", 10.0, 500.0),
            ("2026-06-10", "Alpha Dist", 12.0, 520.0),
            ("2026-07-10", "Alpha Dist", 14.0, 540.0),
            ("2026-08-05", "Alpha Dist", 8.0, 560.0),
            ("2026-05-11", "Beta Store", 20.0, 480.0),
            ("2026-06-11", "Beta Store", 22.0, 490.0),
            ("2026-07-11", "Beta Store", 24.0, 500.0),
            ("2026-08-04", "Beta Store", 6.0, 510.0),
        ]
        for i, (dt, party, mt, rate) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type,
                  payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?,
                          'Eva Canola Oil (StandUpPouch)', ?, 'MT', ?, ?, ?,
                          'Eva Distributors', '{}')
                """,
                (f"up-{i}", dt, party, mt, mt, rate, rate * mt * 1000),
            )
        conn.commit()


def test_normalize_universal_customer_price_trend() -> None:
    spec = normalize_query_spec(
        {
            "row_dimensions": ["party"],
            "column_dimensions": ["month"],
            "metrics": ["avg_price"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 6,
            "context_handling": "none",
        }
    )
    assert spec["row_dimensions"] == ["party"]
    assert spec["column_dimensions"] == ["month"]
    assert spec["metrics"] == ["avg_price"]
    assert spec["intent"] == "price"
    assert not validate_query_spec(spec)


def test_legacy_intent_still_normalizes() -> None:
    spec = normalize_query_spec(
        {
            "intent": "sales_trend",
            "period_type": "LAST_N_MONTHS",
            "months_back": 6,
            "context_handling": "none",
        }
    )
    assert "business_unit" in spec["row_dimensions"]
    assert "month" in spec["column_dimensions"]
    assert "volume" in spec["metrics"]


def test_execute_customer_wise_price_trends() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "row_dimensions": ["party"],
                    "column_dimensions": ["month"],
                    "metrics": ["avg_price"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                }
            )
            assert out["ok"] is True, out
            assert out.get("mode") == "universal_pivot"
            assert out.get("value_format") == "pkr"
            matrix = out.get("matrix") or {}
            assert matrix.get("row_dimension") == "party"
            assert matrix.get("column_dimension") == "month"
            parties = {
                str(r.get("party"))
                for r in (matrix.get("rows") or [])
                if r.get("party")
            }
            assert "Alpha Dist" in parties
            assert "Beta Store" in parties
            md = out.get("answer_markdown") or ""
            assert "Avg price" in md
            assert "PKR" in md
            assert "eva-mtx" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def _seed_meal_july() -> None:
    init_db()
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, "
            "payload_json, updated_at) VALUES (?, ?, ?, ?, '{}', datetime('now'))",
            [
                ("CM1", "Meal", "Canola Meal", "Canola Meal"),
                ("SM1", "Meal", "Soya Meal", "Soya Meal"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, "
            "payload_json, updated_at) VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "Meal Cust", "Meal Clients", "Lahore", "Lahore"),
                ("2", "Bulk Cust", "Bulk Debtors", "Lahore", "Lahore"),
            ],
        )
        rows = [
            ("2026-07-05", "Meal Cust", "CM1", 10, 80, "Meal Clients"),
            ("2026-07-06", "Meal Cust", "SM1", 20, 90, "Meal Clients"),
            ("2026-07-07", "Bulk Cust", "CM1", 5, 85, "Bulk Debtors"),
            ("2026-07-10 00:00:00", "Meal Cust", "CM1", 4, 82, "Meal Clients"),
        ]
        for i, (dt, party, prod, mt, rate, ct) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type,
                  payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, ?,
                  ?, '{}')
                """,
                (f"mj-{i}", dt, party, prod, mt, mt, rate, mt * rate, ct),
            )
        conn.commit()


def test_july_meal_avg_price_by_product_is_flat_not_channel_grid() -> None:
    """'avg price of meal by product in July' must not invent a client_type grid."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed_meal_july()
            out = execute_query_spec(
                {
                    "state_action": "clear",
                    "row_dimensions": ["packing_category"],
                    "metrics": ["avg_price"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "filters": {"business_units": ["Meal"]},
                    "context_handling": "none",
                },
                user_text="show me the average price of meal by product in July",
            )
            assert out.get("ok"), out.get("plan_errors") or out.get("error")
            qs = out.get("query_spec") or {}
            assert qs.get("row_dimensions") == ["packing_category"]
            assert qs.get("column_dimensions") == []
            mat = out.get("matrix") or {}
            assert mat.get("columns") == ["Avg Rate"]
            by_pack = {
                str(r.get("packing_category")): r.get("Avg Rate")
                for r in (mat.get("rows") or [])
            }
            assert by_pack.get("Canola Meal") not in (None, "")
            assert by_pack.get("Soya Meal") not in (None, "")
            assert float(by_pack["Soya Meal"]) == 90.0
            # 10@80 + 5@85 + 4@82 (datetime date must join)
            canola = float(by_pack["Canola Meal"])
            expected = (10 * 80 + 5 * 85 + 4 * 82) / 19
            assert abs(canola - expected) < 0.05
            md = out.get("answer_markdown") or ""
            assert "Client Type" not in md
            assert "Meal Clients" not in md
            assert "—" not in md or "80" in md or "90" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_named_month_avg_price_honors_explicit_channel_columns() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed_meal_july()
            out = execute_query_spec(
                {
                    "state_action": "clear",
                    "row_dimensions": ["packing_category"],
                    "column_dimensions": ["client_type"],
                    "metrics": ["avg_price"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "filters": {"business_units": ["Meal"]},
                    "context_handling": "none",
                },
                user_text="show meal average price by product and channel in July",
            )
            assert out.get("ok"), out.get("plan_errors") or out.get("error")
            qs = out.get("query_spec") or {}
            assert qs.get("column_dimensions") == ["client_type"]
            mat = out.get("matrix") or {}
            assert "Meal Clients" in (mat.get("columns") or [])
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
