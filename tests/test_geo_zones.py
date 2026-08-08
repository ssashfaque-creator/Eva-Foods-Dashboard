"""City → Zone mapping and chatbot zone/city hierarchy."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    _dispatch_tool,
    extract_regroup_dimension,
    resolve_forced_tool,
    resolve_regroup_request,
    resolve_row_dimension_request,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.geo import (
    DEFAULT_CITY,
    DEFAULT_ZONE,
    extract_zone_from_text,
    resolve_city_zone,
    zone_for_city,
)
from eva_dashboard.sales_query import _clients_lookup, _norm_party_key, query_sales


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed() -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, payload_json, updated_at) "
            "VALUES ('P1', 'Eva Consumer', 'Eva Canola', 'Stand up', '{}', datetime('now'))"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "South Dist", "Eva Distributors", "Karachi", "Karachi"),
                ("2", "Central Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("3", "North Dist", "Eva Distributors", "Islamabad", "Islamabad"),
                ("4", "Blank Dist", "Eva Distributors", "", ""),
                ("5", "Undef Dist", "Eva Distributors", "undefined", "undefined"),
            ],
        )
        rows = [
            ("2026-07-01", "South Dist", 10),
            ("2026-07-02", "Central Dist", 20),
            ("2026-07-03", "North Dist", 5),
            ("2026-07-04", "Blank Dist", 7),
            ("2026-07-05", "Undef Dist", 3),
        ]
        for i, (dt, party, mt) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, 'P1', ?, 'MT', ?,
                          'Eva Distributors', '{}')
                """,
                (f"gz-{i}", dt, party, mt, mt),
            )
        conn.commit()


def test_geo_defaults_and_mapping() -> None:
    assert resolve_city_zone("") == (DEFAULT_CITY, DEFAULT_ZONE)
    assert resolve_city_zone("undefined") == (DEFAULT_CITY, DEFAULT_ZONE)
    assert resolve_city_zone("Unmapped") == (DEFAULT_CITY, DEFAULT_ZONE)
    assert zone_for_city("Lahore") == "CENTRAL"
    assert zone_for_city("Islamabad") == "NORTH"
    assert zone_for_city("Quetta") == "SOUTH"
    assert extract_zone_from_text("show me south zone sales") == "SOUTH"
    assert extract_zone_from_text("central region") == "CENTRAL"
    assert extract_zone_from_text("north zone distributors") == "NORTH"


def test_blank_city_defaults_in_clients_lookup() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            lu = _clients_lookup()
            blank = lu[_norm_party_key("Blank Dist")]
            undef = lu[_norm_party_key("Undef Dist")]
            assert blank["city"] == "Karachi"
            assert blank["zone"] == "SOUTH"
            assert undef["city"] == "Karachi"
            assert undef["zone"] == "SOUTH"
            assert lu[_norm_party_key("Central Dist")]["zone"] == "CENTRAL"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_zone_pivot_and_filter() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            by_zone = query_sales(
                client_type="Eva Distributors",
                columns="month",
                months_back=3,
                row_dimension="zone",
            )
            assert by_zone["ok"] is True
            totals = {
                str(r.get("zone")): float(r.get("Total") or 0)
                for r in by_zone["matrix"]["rows"]
                if str(r.get("zone") or "").upper() in {"SOUTH", "CENTRAL", "NORTH"}
            }
            # South Dist 10 + Blank 7 + Undef 3 = 20
            assert totals.get("SOUTH") == 20
            assert totals.get("CENTRAL") == 20
            assert totals.get("NORTH") == 5

            south = query_sales(
                zone="SOUTH",
                client_type="Eva Distributors",
                columns="month",
                months_back=3,
            )
            assert south["ok"] is True
            assert south["filters"]["zone"] == "SOUTH"
            assert "zone **SOUTH**" in (south.get("answer_markdown") or "")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_city_wise_after_zone_nests_under_zone() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            assert resolve_row_dimension_request("zone wise") == "zone"
            assert extract_regroup_dimension("show city wise") == "city"
            prior = query_sales(
                client_type="Eva Distributors",
                columns="month",
                months_back=3,
                row_dimension="zone",
            )["table_spec"]
            rg = resolve_regroup_request("city wise", prior_spec=prior)
            assert rg is not None
            assert rg["row_dimension"] == "city"
            assert rg["row_groups"] == ["zone"]
            assert (
                resolve_forced_tool("city wise", prior_table_spec=prior)
                == "query_sales"
            )
            out = _dispatch_tool(
                "query_sales", {}, user_text="show city wise", prior_spec=prior
            )
            assert out["ok"] is True
            assert out["row_dimension"] == "city"
            headers = (out.get("matrix") or {}).get("row_headers") or []
            assert headers[:2] == ["zone", "city"]
            md = out.get("answer_markdown") or ""
            assert "Zone" in md and "City" in md or "Karachi" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_list_clients_by_zone_defaults_blank_to_karachi() -> None:
    from eva_dashboard.party_analytics import list_clients

    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = list_clients(
                zone="SOUTH",
                client_type="Eva Distributors",
                include_zero=True,
            )
            assert out["ok"] is True
            names = {r["client"] for r in out["clients"]}
            assert "South Dist" in names
            assert "Blank Dist" in names
            assert "Undef Dist" in names
            assert "Central Dist" not in names
            assert out["filters"]["zone"] == "SOUTH"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_dispatch_south_zone_sales() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = _dispatch_tool(
                "query_sales",
                {},
                user_text="show me south zone distributor sales",
            )
            assert out["ok"] is True
            assert out["filters"].get("zone") == "SOUTH"
            assert out["filters"].get("client_type") == "Eva Distributors"
            assert out["column_dimension"] == "month"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
