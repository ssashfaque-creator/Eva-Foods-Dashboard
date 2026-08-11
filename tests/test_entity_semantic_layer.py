"""Enterprise Semantic Layer — enums, rejection loop, extracted_entities."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import get_tools
from eva_dashboard.db import connect, init_db
from eva_dashboard.entity_catalog import (
    clear_entity_catalog_cache,
    is_business_unit_label,
    load_entity_catalog,
    resolve_extracted_entities,
    validate_categorical_filters,
)
from eva_dashboard.query_executor import execute_query_spec
from eva_dashboard.query_spec import PLAN_QUERY_TOOL


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")
    clear_entity_catalog_cache()


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
        conn.execute(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES ('1', 'Alpha Dist', 'Eva Distributors', 'Lahore', 'Lahore', '', "
            "'{}', datetime('now'))"
        )
        for i, (dt, mt) in enumerate(
            [
                ("2026-03-10", 10),
                ("2026-04-10", 12),
                ("2026-05-10", 14),
                ("2026-06-10", 16),
                ("2026-07-10", 18),
                ("2026-08-05", 8),
            ]
        ):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, 'Alpha Dist',
                  'Eva Canola Oil (StandUpPouch)', ?, 'MT', ?, 'Eva Distributors', '{}')
                """,
                (f"es-{i}", dt, mt, mt),
            )
        conn.commit()
    clear_entity_catalog_cache()


def test_is_business_unit_not_client_channel() -> None:
    assert is_business_unit_label("Eva Consumer")
    assert is_business_unit_label("Consumer")
    assert is_business_unit_label("Eva Bulk")
    assert not is_business_unit_label("Eva Distributors")
    assert not is_business_unit_label("Imtiaz Store")


def test_validate_rejects_eva_consumer_as_client_type() -> None:
    errors = validate_categorical_filters({"client_type": "Eva Consumer"})
    assert errors
    assert any("business_units" in e for e in errors)
    assert any("Eva Consumer" in e for e in errors)


def test_plan_query_tool_has_strict_enums() -> None:
    tools = get_tools()
    plan = next(t for t in tools if t["function"]["name"] == "plan_query")
    props = plan["function"]["parameters"]["properties"]
    assert "extracted_entities" in props
    bus_enum = props["business_units"]["items"]["enum"]
    assert "Eva Consumer" in bus_enum
    assert "Eva Bulk" in bus_enum
    ct_enum = props["filters"]["properties"]["client_type"]["enum"]
    assert "Eva Distributors" in ct_enum
    assert "Eva Consumer" not in ct_enum


def test_resolve_extracted_entities_brand_and_sku() -> None:
    resolved = resolve_extracted_entities(["Eva", "canola", "standup"])
    assert "Eva Consumer" in resolved["business_units"]
    assert "Eva Bulk" in resolved["business_units"]
    assert resolved["oil_type"] == "Eva Canola"
    assert resolved["packing_category"] == "Stand up"


def test_executor_rejects_misrouted_client_type_not_empty_table() -> None:
    """The screenshot bug: Eva Consumer in client_type must NOT return empty MT."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "filters": {
                        "city": "Lahore",
                        "client_type": "Eva Consumer",  # WRONG column
                    },
                }
            )
            assert out["ok"] is False
            assert out.get("plan_errors")
            joined = " ".join(out["plan_errors"])
            assert "business_units" in joined
            assert "Eva Consumer" in joined
            # Must not silently return an empty matrix
            assert not (out.get("matrix") or {}).get("columns")
        finally:
            clear_entity_catalog_cache()
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_executor_accepts_eva_consumer_as_business_unit() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "business_units": ["Eva Consumer"],
                    "filters": {"city": "Lahore"},
                }
            )
            assert out["ok"] is True, out
            md = out.get("answer_markdown") or ""
            assert "No data" not in md and "no recorded" not in md.lower()
            rows = (out.get("matrix") or {}).get("rows") or []
            assert rows, out
        finally:
            clear_entity_catalog_cache()
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_extracted_entities_path_for_eva_consumer() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "extracted_entities": ["Eva Consumer"],
                    "filters": {"city": "Lahore"},
                }
            )
            assert out["ok"] is True, out
            qs = out.get("query_spec") or {}
            assert "Eva Consumer" in (qs.get("business_units") or [])
            assert not (qs.get("filters") or {}).get("client_type")
        finally:
            clear_entity_catalog_cache()
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_base_schema_includes_extracted_entities() -> None:
    props = PLAN_QUERY_TOOL["function"]["parameters"]["properties"]
    assert "extracted_entities" in props


def test_catalog_lists_canonical_bus() -> None:
    clear_entity_catalog_cache()
    cat = load_entity_catalog()
    for bu in ("Eva Consumer", "Eva Bulk", "Maan Consumer", "Maan Bulk"):
        assert bu in cat["business_units"]
