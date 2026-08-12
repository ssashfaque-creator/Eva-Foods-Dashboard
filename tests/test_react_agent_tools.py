"""ReAct agent tools: SQL, calculator, discovery, legacy adapter."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.agent_loop import (
    REACT_TOOLS_SCHEMA,
    dispatch_react_tool,
    react_agent_enabled,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.tools.calculator_tool import calculate_expression
from eva_dashboard.tools.discovery_tool import get_database_schema, lookup_entity_values
from eva_dashboard.tools.legacy_tool import run_standard_analytics_pivot
from eva_dashboard.tools.sql_tool import execute_read_only_sql


def _seed(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")
    import eva_dashboard.sales_query as sq

    sq._CLIENTS_CACHE = None
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, "
            "payload_json, updated_at) VALUES "
            "('Oil1', 'Eva Consumer', 'Eva Canola', 'Stand up', "
            "'{}', datetime('now'))"
        )
        conn.execute(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, "
            "payload_json, updated_at) VALUES "
            "('1', 'PEPSI-COLA INTERNATIONAL', 'Direct Customers', "
            "'Lahore', 'Lahore', '', '{}', datetime('now'))"
        )
        conn.execute(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, "
            "payload_json, updated_at) VALUES "
            "('2', 'OTHER BUYER', 'Direct Customers', "
            "'Lahore', 'Lahore', '', '{}', datetime('now'))"
        )
        for i, (party, rate, mt) in enumerate(
            (
                ("PEPSI-COLA INTERNATIONAL", 100.0, 10.0),
                ("PEPSI-COLA INTERNATIONAL", 80.0, 5.0),
                ("OTHER BUYER", 90.0, 8.0),
            )
        ):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type,
                  payload_json
                ) VALUES (NULL, ?, datetime('now'), '2026-03-15', ?, 'Oil1',
                  ?, 'MT', ?, ?, ?, 'Direct Customers', '{}')
                """,
                (f"r-{i}", party, mt, mt, rate, mt * rate),
            )
        conn.commit()


def test_calculate_expression_safe() -> None:
    out = calculate_expression("(1500 * 24.7) / 6")
    assert out["ok"] is True
    assert abs(float(out["result"]) - 6175.0) < 1e-6
    bad = calculate_expression("__import__('os').system('id')")
    assert bad["ok"] is False


def test_sql_tool_blocks_writes_and_returns_min_rate() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _seed(tmp)
        try:
            blocked = execute_read_only_sql("DELETE FROM sales")
            assert blocked["ok"] is False
            out = execute_read_only_sql(
                "SELECT party, MIN(rate) AS min_rate FROM sales "
                "WHERE party LIKE '%PEPSI%' GROUP BY party"
            )
            assert out["ok"] is True
            assert out["row_count"] == 1
            assert float(out["rows"][0]["min_rate"]) == 80.0
            assert "80" in out["markdown"]
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_discovery_schema_and_entity_lookup() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _seed(tmp)
        try:
            schema = get_database_schema()
            assert schema["ok"] is True
            assert "sales" in (schema.get("tables") or [])
            assert "CREATE TABLE" in schema["markdown"]
            found = lookup_entity_values("sales", "party", "pepsi")
            assert found["ok"] is True
            assert any("PEPSI" in m.upper() for m in found["matches"])
            denied = lookup_entity_values("sales;drop", "party", "x")
            assert denied["ok"] is False
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_legacy_tool_volume_ams_path() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _seed(tmp)
        try:
            out = run_standard_analytics_pivot(
                {
                    "operation": "pivot",
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "filters": {},
                },
                user_text="Show me volume and AMS by Business Unit for the last 6 months.",
            )
            assert out["ok"] is True
            assert "Eva Consumer" in out["markdown"] or "Volume" in out["markdown"]
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_dispatch_react_tools_and_flag() -> None:
    assert len(REACT_TOOLS_SCHEMA) == 5
    names = {t["function"]["name"] for t in REACT_TOOLS_SCHEMA}
    assert names == {
        "execute_read_only_sql",
        "calculate_expression",
        "get_database_schema",
        "lookup_entity_values",
        "run_standard_analytics_pivot",
    }
    calc = dispatch_react_tool(
        "calculate_expression", {"expression": "24.7 / 6"}
    )
    assert calc["ok"] is True
    assert abs(float(calc["result"]) - (24.7 / 6)) < 1e-9

    previous = os.environ.get("EVA_REACT_AGENT")
    try:
        os.environ["EVA_REACT_AGENT"] = "1"
        assert react_agent_enabled() is True
        os.environ["EVA_REACT_AGENT"] = "0"
        assert react_agent_enabled() is False
    finally:
        if previous is None:
            os.environ.pop("EVA_REACT_AGENT", None)
        else:
            os.environ["EVA_REACT_AGENT"] = previous


def test_multi_hop_sql_then_math_via_dispatch() -> None:
    """Simulate Test 3: fetch Pepsi min rate then multiply by 24.7 / 6."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _seed(tmp)
        try:
            sql = dispatch_react_tool(
                "execute_read_only_sql",
                {
                    "sql_query": (
                        "SELECT MIN(rate) AS min_rate FROM sales "
                        "WHERE party LIKE '%PEPSI%'"
                    )
                },
            )
            assert sql["ok"] is True
            rate = float(sql["rows"][0]["min_rate"])
            assert rate == 80.0
            math = dispatch_react_tool(
                "calculate_expression",
                {"expression": f"({rate} * 24.7) / 6"},
            )
            assert math["ok"] is True
            assert abs(float(math["result"]) - ((80.0 * 24.7) / 6)) < 1e-6
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
