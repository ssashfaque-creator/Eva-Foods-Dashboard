"""Bare Eva brand sales must hit query_sales — never lookup_party."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    _dispatch_tool,
    _extract_business_units_from_text,
    _extract_named_party_query,
    _looks_named_party_sales,
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
                ("Eva Canola Stand", "Eva Consumer", "Eva Canola", "Stand up"),
                ("Eva Bulk Tin", "Eva Bulk", "Eva VTF", "Tin (oil)"),
                ("Maan Canola", "Maan Consumer", "Maan Canola", "Stand up"),
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES ('1', 'Alpha Dist', 'Eva Distributors', 'Karachi', 'Karachi', '', '{}', datetime('now'))"
        )
        for i, (dt, prod, mt) in enumerate(
            [
                ("2026-06-01", "Eva Canola Stand", 20),
                ("2026-07-01", "Eva Canola Stand", 25),
                ("2026-07-02", "Eva Bulk Tin", 10),
                ("2026-07-03", "Maan Canola", 8),
            ]
        ):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, 'Alpha Dist', ?, ?, 'MT', ?,
                          'Eva Distributors', '{}')
                """,
                (f"eb-{i}", dt, prod, mt, mt),
            )
        conn.commit()


def test_eva_sales_not_named_party() -> None:
    q = "how are Eva sales in karachi"
    assert _extract_named_party_query(q) is None
    assert not _looks_named_party_sales(q)
    assert resolve_forced_tool(q) == "required"
    assert suggest_preferred_tool(q) == "query_sales"
    bus = _extract_business_units_from_text(q)
    assert "Eva Consumer" in bus
    assert "Eva Bulk" in bus


def test_eva_sales_karachi_dispatches_sales_not_lookup() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            q = "how are Eva sales in Karachi"
            # AI-first: model should pick query_sales (taught via vocab + soft hint)
            out = _dispatch_tool("query_sales", {}, user_text=q)
            assert out["ok"] is True
            assert out.get("mode") in {"analytical", "matrix", "trend"}
            filters = out.get("filters") or {}
            assert filters.get("city") == "Karachi"
            # Eva brand → both Eva BUs (Maan not in the filter list)
            bus = list(out.get("business_units") or [])
            assert "Eva Consumer" in bus
            assert "Eva Bulk" in bus
            assert "Maan Consumer" not in bus
            md = out.get("answer_markdown") or ""
            assert "Karachi" in md or "karachi" in md.lower()
            assert "Maan" not in md or "Maan Consumer" not in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_real_party_name_still_extracts() -> None:
    assert _extract_named_party_query("how are Alpha Dist sales in Karachi") == (
        "Alpha Dist"
    )
    assert _looks_named_party_sales("show me Alpha Dist sales")


def test_maan_sales_not_named_party() -> None:
    q = "how are Maan sales in karachi"
    assert _extract_named_party_query(q) is None
    assert not _looks_named_party_sales(q)
    assert resolve_forced_tool(q) == "required"
    assert suggest_preferred_tool(q) == "query_sales"
    bus = _extract_business_units_from_text(q)
    assert set(bus) == {"Maan Consumer", "Maan Bulk"}


def test_maan_sales_karachi_dispatches_sales_not_lookup() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            q = "how are Maan sales in Karachi"
            out = _dispatch_tool("query_sales", {}, user_text=q)
            assert out["ok"] is True
            assert out.get("mode") in {"analytical", "matrix", "trend"}
            filters = out.get("filters") or {}
            assert filters.get("city") == "Karachi"
            bus = list(out.get("business_units") or [])
            assert "Maan Consumer" in bus
            assert "Maan Bulk" in bus
            assert "Eva Consumer" not in bus
            md = out.get("answer_markdown") or ""
            assert "Top parties by AMS" not in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_distributor_sales_not_ams_ranking() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            q = "how are distributor sales in karachi"
            assert resolve_forced_tool(q) == "required"
            assert suggest_preferred_tool(q) == "query_sales"
            out = _dispatch_tool("query_sales", {}, user_text=q)
            assert out["ok"] is True
            assert out.get("mode") in {"analytical", "matrix", "trend"}
            assert (out.get("filters") or {}).get("client_type") == (
                "Eva Distributors"
            )
            assert (out.get("filters") or {}).get("city") == "Karachi"
            assert "Top parties by AMS" not in (out.get("answer_markdown") or "")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
