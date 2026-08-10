"""Adversarial natural-language follow-ups — complete asks must not stick prior filters."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from eva_dashboard.chatbot import (
    FOLLOWUP_MARKER,
    _dispatch_tool,
    _looks_channel_growth_ask,
    _looks_complete_sales_ask,
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
                ("P1", "Eva Consumer", "Eva Canola", "Stand up"),
                ("P2", "Maan Consumer", "Maan Canola", "Stand up"),
                ("P3", "Eva Bulk", "Eva VTF", "Tin (oil)"),
            ],
        )
        clients = [
            ("1", "Alpha Dist", "Eva Distributors", "Karachi"),
            ("2", "Beta Dealer", "Dealer", "Karachi"),
            ("3", "Gamma Dist", "Eva Distributors", "Lahore"),
            ("4", "Delta Imtiaz", "Imtiaz Store", "Karachi"),
        ]
        for cid, name, ctype, city in clients:
            conn.execute(
                "INSERT OR REPLACE INTO clients "
                "(client_id, client, type, city_filter, city, inactive, "
                "payload_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
                (cid, name, ctype, city, city),
            )
        rows = [
            ("2026-03-01", "Alpha Dist", "P1", 10, "Eva Distributors"),
            ("2026-04-01", "Alpha Dist", "P1", 12, "Eva Distributors"),
            ("2026-05-01", "Alpha Dist", "P1", 14, "Eva Distributors"),
            ("2026-06-01", "Alpha Dist", "P1", 16, "Eva Distributors"),
            ("2026-07-01", "Alpha Dist", "P1", 18, "Eva Distributors"),
            ("2026-07-01", "Alpha Dist", "P2", 4, "Eva Distributors"),
            ("2026-07-01", "Beta Dealer", "P1", 9, "Dealer"),
            ("2026-07-01", "Delta Imtiaz", "P1", 11, "Imtiaz Store"),
            ("2026-06-01", "Gamma Dist", "P1", 20, "Eva Distributors"),
            ("2026-07-01", "Gamma Dist", "P1", 22, "Eva Distributors"),
            ("2026-07-01", "Gamma Dist", "P3", 8, "Eva Distributors"),
        ]
        for i, (dt, party, prod, mt, ct) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, '{}')
                """,
                (f"nf-{i}", dt, party, prod, mt, mt, ct),
            )
        conn.commit()


def _maan_july_prior() -> dict[str, Any]:
    return {
        "filters": {
            "city": "Karachi",
            "business_unit": "Maan Consumer",
            "client_type": "Eva Distributors",
        },
        "business_units": ["Maan Consumer"],
        "column_dimension": "month",
        "row_dimension": "party",
        "months_back": 6,
        "period_phrase": "July",
    }


@pytest.fixture()
def seeded():
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        _seed()
        yield
        if previous is None:
            os.environ.pop("EVA_DATA_DIR", None)
        else:
            os.environ["EVA_DATA_DIR"] = previous


def test_complete_ask_detector() -> None:
    assert _looks_complete_sales_ask(
        "can you show channel wise sales for karachi"
    )
    assert _looks_complete_sales_ask("show me Eva sales in Lahore")
    assert _looks_complete_sales_ask("how are distributor sales nationally")
    assert not _looks_complete_sales_ask("channel wise")
    assert not _looks_complete_sales_ask("city wise")
    assert not _looks_complete_sales_ask("remove BU")
    assert not _looks_complete_sales_ask("same format for Imtiaz")


def test_channel_wise_karachi_drops_sticky_maan(seeded) -> None:
    q = "can you show channel wise sales for karachi"
    out = _dispatch_tool(
        "query_sales",
        {"business_unit": "Maan Consumer", "period": "July"},
        user_text=q,
        prior_spec=_maan_july_prior(),
    )
    assert out["ok"] is True
    filters = out.get("filters") or {}
    assert filters.get("city") == "Karachi"
    assert not filters.get("business_unit"), filters
    assert not out.get("business_units"), out.get("business_units")
    assert out.get("row_dimension") == "client_type"
    assert out.get("column_dimension") == "month"
    md = out.get("answer_markdown") or ""
    assert "Client Type × Month" in md
    assert "Maan Consumer" not in md.split("###")[0]
    # Multiple channels, not a Maan-only sliver
    assert "Eva Distributors" in md or "Dealer" in md or "Imtiaz" in md


def test_lahore_prior_does_not_override_spoken_karachi(seeded) -> None:
    prior = _maan_july_prior()
    prior["filters"]["city"] = "Lahore"
    out = _dispatch_tool(
        "query_sales",
        {},
        user_text="channel wise sales for karachi",
        prior_spec=prior,
    )
    assert (out.get("filters") or {}).get("city") == "Karachi"
    assert not (out.get("filters") or {}).get("business_unit")


FOLLOWUP_CASES: list[tuple[str, dict[str, Any]]] = [
    (
        "can you show channel wise sales for karachi",
        {
            "city": "Karachi",
            "no_bu": True,
            "row": "client_type",
            "col": "month",
        },
    ),
    (
        "show me all channels in Karachi",
        {"city": "Karachi", "no_bu": True, "row": "client_type"},
    ),
    (
        "what about Eva sales in Karachi instead",
        {
            "city": "Karachi",
            "bus_has": {"Eva Consumer", "Eva Bulk"},
            "bus_not": {"Maan Consumer"},
        },
    ),
    (
        "how are distributor sales in Lahore",
        {
            "city": "Lahore",
            "client_type": "Eva Distributors",
            "no_bu": True,
        },
    ),
    (
        "show me Karachi sales all over — wait nationally",
        {"city": None, "no_bu": True},
    ),
    (
        "Imtiaz sales in Karachi",
        {"city": "Karachi", "client_type": "Imtiaz Store", "no_bu": True},
    ),
]


@pytest.mark.parametrize("query,expect", FOLLOWUP_CASES)
def test_random_complete_followups_reset_sticky(seeded, query, expect) -> None:
    assert _looks_complete_sales_ask(query) or "national" in query.lower()
    out = _dispatch_tool(
        "query_sales",
        {},
        user_text=query,
        prior_spec=_maan_july_prior(),
    )
    assert out.get("ok") is True, out
    filters = out.get("filters") or {}
    if "city" in expect:
        assert filters.get("city") == expect["city"], filters
    if expect.get("no_bu"):
        assert not filters.get("business_unit"), filters
        # Complete asks should not keep prior Maan list either
        bus = set(out.get("business_units") or [])
        assert "Maan Consumer" not in bus or expect.get("bus_has"), bus
    if expect.get("bus_has"):
        bus = set(out.get("business_units") or [])
        if filters.get("business_unit"):
            bus.add(filters["business_unit"])
        assert expect["bus_has"].issubset(bus), bus
    if expect.get("bus_not"):
        bus = set(out.get("business_units") or [])
        if filters.get("business_unit"):
            bus.add(filters["business_unit"])
        assert expect["bus_not"].isdisjoint(bus), bus
    if expect.get("client_type"):
        assert filters.get("client_type") == expect["client_type"], filters
    if expect.get("row"):
        assert out.get("row_dimension") == expect["row"], out.get("row_dimension")
    if expect.get("col"):
        assert out.get("column_dimension") == expect["col"], out.get(
            "column_dimension"
        )


def test_bare_channel_wise_still_mutates_prior(seeded) -> None:
    """Short 'channel wise' keeps period/city but drops sticky BU."""
    assert not _looks_complete_sales_ask("channel wise")
    out = _dispatch_tool(
        "query_sales",
        {},
        user_text="channel wise",
        prior_spec=_maan_july_prior(),
    )
    assert out["ok"] is True
    assert out.get("row_dimension") == "client_type"
    # BU cleared for channel-wise regroup
    assert not (out.get("filters") or {}).get("business_unit")


def test_reply_prefix_not_channel_growth() -> None:
    q = FOLLOWUP_MARKER + "\ncan you show channel wise sales for karachi"
    assert not _looks_channel_growth_ask(q)
    assert _looks_complete_sales_ask(q)
    assert resolve_forced_tool(q) == "query_sales"
    assert suggest_preferred_tool(q) == "query_sales"


def test_wrong_tool_still_channel_matrix(seeded) -> None:
    q = "can you show channel wise sales for karachi"
    for tool in ("analyze_parties", "list_clients", "query_sales"):
        out = _dispatch_tool(
            tool,
            {},
            user_text=q,
            prior_spec=_maan_july_prior(),
        )
        assert out.get("ok") is True
        assert out.get("row_dimension") == "client_type" or out.get("mode") in {
            "matrix",
            "analytical",
            "trend",
        }
        assert not (out.get("filters") or {}).get("business_unit")
