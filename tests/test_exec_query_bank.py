"""Executive query bank — tool, mode, and filter expectations for common asks."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

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
                ("Maan Bulk Tin", "Maan Bulk", "Maan VTF", "Tin (oil)"),
            ],
        )
        clients = [
            ("1", "Alpha Dist", "Eva Distributors", "Karachi", ""),
            ("2", "Beta Dist", "Eva Distributors", "Lahore", ""),
            ("3", "Gamma Dist", "Eva Distributors", "Karachi", "Y"),
            ("4", "Sample for Marketing", "Eva Distributors", "Karachi", ""),
        ]
        for cid, name, ctype, city, inactive in clients:
            conn.execute(
                "INSERT OR REPLACE INTO clients "
                "(client_id, client, type, city_filter, city, inactive, "
                "payload_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, '{}', datetime('now'))",
                (cid, name, ctype, city, city, inactive),
            )
        rows = [
            ("2026-05-05", "Alpha Dist", "Eva Canola Stand", 20, "Eva Distributors"),
            ("2026-06-05", "Alpha Dist", "Eva Canola Stand", 22, "Eva Distributors"),
            ("2026-07-05", "Alpha Dist", "Eva Canola Stand", 25, "Eva Distributors"),
            ("2026-07-06", "Alpha Dist", "Eva Bulk Tin", 8, "Eva Distributors"),
            ("2026-07-07", "Beta Dist", "Eva Canola Stand", 15, "Eva Distributors"),
            ("2026-07-08", "Alpha Dist", "Maan Canola", 12, "Eva Distributors"),
            ("2026-07-09", "Alpha Dist", "Maan Bulk Tin", 6, "Eva Distributors"),
            ("2026-05-05", "Gamma Dist", "Eva Canola Stand", 10, "Eva Distributors"),
            ("2026-06-05", "Gamma Dist", "Eva Canola Stand", 10, "Eva Distributors"),
            ("2026-07-05", "Sample for Marketing", "Eva Canola Stand", 4, "Eva Distributors"),
            ("2025-07-05", "Alpha Dist", "Eva Canola Stand", 10, "Eva Distributors"),
            ("2025-07-05", "Beta Dist", "Eva Canola Stand", 20, "Eva Distributors"),
        ]
        for i, (dt, party, prod, mt, ct) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, '{}')
                """,
                (f"qb-{i}", dt, party, prod, mt, mt, ct),
            )
        conn.commit()


# query, expected forced tool, expected mode family, filter checks
CASES: list[tuple[str, str, set[str], dict[str, Any]]] = [
    (
        "how are Eva sales in karachi",
        "query_sales",
        {"analytical", "matrix", "trend"},
        {"city": "Karachi", "bus": {"Eva Consumer", "Eva Bulk"}, "not_metric_ams_rank": True},
    ),
    (
        "how are Maan sales in karachi",
        "query_sales",
        {"analytical", "matrix", "trend"},
        {"city": "Karachi", "bus": {"Maan Consumer", "Maan Bulk"}, "not_metric_ams_rank": True},
    ),
    (
        "how are maan sales in Lahore",
        "query_sales",
        {"analytical", "matrix", "trend"},
        {"city": "Lahore", "bus": {"Maan Consumer", "Maan Bulk"}},
    ),
    (
        "how are distributor sales in karachi",
        "query_sales",
        {"analytical", "matrix", "trend"},
        {
            "city": "Karachi",
            "client_type": "Eva Distributors",
            "not_metric_ams_rank": True,
        },
    ),
    (
        "show me distributor sales in Karachi",
        "query_sales",
        {"analytical", "matrix", "trend"},
        {"city": "Karachi", "client_type": "Eva Distributors"},
    ),
    (
        "Eva sales in Karachi",
        "query_sales",
        {"analytical", "matrix", "trend"},
        {"city": "Karachi", "bus": {"Eva Consumer", "Eva Bulk"}},
    ),
    (
        "Maan sales nationally",
        "query_sales",
        {"analytical", "matrix", "trend"},
        {"city": None, "bus": {"Maan Consumer", "Maan Bulk"}},
    ),
    (
        "how are Eva Consumer sales in Karachi",
        "query_sales",
        {"analytical", "matrix", "trend"},
        {"city": "Karachi", "bus": {"Eva Consumer"}},
    ),
    (
        "how are sales in Karachi",
        "query_sales",
        {"analytical", "matrix", "trend"},
        {"city": "Karachi", "not_metric_ams_rank": True},
    ),
    (
        "how is maan performance in Karachi",
        "query_sales",
        {"analytical", "matrix", "trend"},
        {"city": "Karachi", "bus": {"Maan Consumer", "Maan Bulk"}},
    ),
    (
        "distributor performance in Karachi",
        "query_sales",
        {"analytical", "matrix", "trend"},
        {
            "city": "Karachi",
            "client_type": "Eva Distributors",
            "not_metric_ams_rank": True,
        },
    ),
    (
        "evaluate maan in Karachi",
        "query_sales",
        {"analytical", "matrix", "trend"},
        {"city": "Karachi", "bus": {"Maan Consumer", "Maan Bulk"}},
    ),
    (
        "which distributors have had the biggest decline in ams",
        "analyze_parties",
        {"ams_growth", "analyze_parties", "yoy", "yoy_ams"},
        {"metric": "ams_growth"},
    ),
    (
        "nationally which distributors have had a decline in AMS",
        "analyze_parties",
        {"ams_growth", "analyze_parties", "yoy", "yoy_ams"},
        {"city": None, "metric": "ams_growth"},
    ),
]


@pytest.fixture(scope="module")
def seeded_env():
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        _seed()
        yield tmp
        if previous is None:
            os.environ.pop("EVA_DATA_DIR", None)
        else:
            os.environ["EVA_DATA_DIR"] = previous


@pytest.mark.parametrize("query,tool,modes,expect", CASES)
def test_exec_query_bank_routing(seeded_env, query, tool, modes, expect):
    forced = resolve_forced_tool(query)
    preferred = suggest_preferred_tool(query)
    assert forced == tool, f"forced={forced} for {query!r}"
    assert preferred == tool, f"preferred={preferred} for {query!r}"
    assert not _looks_named_party_sales(query) or tool == "lookup_party"
    if "Eva" in query or "eva" in query.lower():
        if "sales" in query.lower() and "distributor" not in query.lower():
            assert _extract_named_party_query(query) is None
    if "Maan" in query or "maan" in query.lower():
        if "sales" in query.lower() and "distributor" not in query.lower():
            assert _extract_named_party_query(query) is None


@pytest.mark.parametrize("query,tool,modes,expect", CASES)
def test_exec_query_bank_dispatch(seeded_env, query, tool, modes, expect):
    # Correct tool
    out = _dispatch_tool(tool, {}, user_text=query)
    assert out.get("ok") is True, out
    mode = out.get("mode") or out.get("metric")
    assert mode in modes or out.get("metric") in modes, (
        f"mode={out.get('mode')} metric={out.get('metric')} for {query!r}"
    )
    filters = out.get("filters") or {}
    if "city" in expect:
        assert filters.get("city") == expect["city"], filters
    if expect.get("client_type"):
        assert filters.get("client_type") == expect["client_type"], filters
    if expect.get("bus"):
        bus = set(out.get("business_units") or [])
        if filters.get("business_unit"):
            bus.add(filters["business_unit"])
        assert expect["bus"].issubset(bus), bus
    if expect.get("metric"):
        assert out.get("metric") == expect["metric"], out.get("metric")
    if expect.get("not_metric_ams_rank"):
        md = out.get("answer_markdown") or ""
        assert "Top parties by AMS" not in md
        assert out.get("metric") != "ams" or out.get("mode") in {
            "analytical",
            "matrix",
            "trend",
        }

    # Wrong model pick of analyze_parties / lookup_party must still redirect
    if tool == "query_sales":
        for wrong in ("analyze_parties", "lookup_party"):
            redirected = _dispatch_tool(wrong, {}, user_text=query)
            assert redirected.get("ok") is True, redirected
            md = redirected.get("answer_markdown") or ""
            assert "Top parties by AMS" not in md, query
            assert "Could not find" not in md, query


def test_maan_brand_expands_like_eva():
    bus = _extract_business_units_from_text("how are Maan sales in Karachi")
    assert set(bus) == {"Maan Consumer", "Maan Bulk"}
    bus2 = _extract_business_units_from_text("selling maan")
    assert bus2 == ["Maan Consumer"]


def test_remove_inactive_and_sample_still_work(seeded_env):
    prior = _dispatch_tool(
        "query_sales",
        {},
        user_text="show me distributor sales in Karachi",
    )["table_spec"]
    out = _dispatch_tool(
        "query_sales",
        {},
        user_text="remove inactive distributors",
        prior_spec=prior,
    )
    assert out["ok"] is True
    assert out["filters"].get("active_only") is True
    parties = {
        str(r.get("party"))
        for r in (out.get("matrix") or {}).get("rows") or []
        if r.get("party") and str(r.get("party")).lower() != "total"
    }
    assert "Gamma Dist" not in parties

    out2 = _dispatch_tool(
        "query_sales",
        {},
        user_text="exclude sample/marketing",
        prior_spec=prior,
    )
    parties2 = {
        str(r.get("party"))
        for r in (out2.get("matrix") or {}).get("rows") or []
        if r.get("party") and str(r.get("party")).lower() != "total"
    }
    assert "Sample for Marketing" not in parties2
