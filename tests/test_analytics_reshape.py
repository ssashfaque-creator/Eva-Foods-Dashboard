"""Dynamic analytics reshape: city league follow-ups clear sticky geography."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.analytics_reshape import (
    resolve_analytics_reshape,
    wants_city_grain,
    wants_geo_expand,
)
from eva_dashboard.chatbot import _dispatch_tool, suggest_preferred_tool
from eva_dashboard.db import connect, init_db
from eva_dashboard.party_analytics import infer_party_analytics_from_text


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed() -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, payload_json, updated_at) "
            "VALUES ('P', 'Eva Consumer', 'Eva VTF', 'Pouch', '{}', datetime('now'))"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "A Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("2", "B Dist", "Eva Distributors", "Karachi", "Karachi"),
                ("3", "C Dist", "Eva Distributors", "Islamabad", "Islamabad"),
            ],
        )
        # Prior AMS window Feb–Apr; current May–Jul (as_of ~ Aug)
        rows = [
            # Lahore declined
            ("2026-02-05", "A Dist", 30),
            ("2026-03-05", "A Dist", 30),
            ("2026-04-05", "A Dist", 30),
            ("2026-05-05", "A Dist", 10),
            ("2026-06-05", "A Dist", 10),
            ("2026-07-05", "A Dist", 10),
            # Karachi grew
            ("2026-02-05", "B Dist", 5),
            ("2026-03-05", "B Dist", 5),
            ("2026-04-05", "B Dist", 5),
            ("2026-05-05", "B Dist", 20),
            ("2026-06-05", "B Dist", 20),
            ("2026-07-05", "B Dist", 20),
            # Islamabad soft
            ("2026-02-05", "C Dist", 8),
            ("2026-03-05", "C Dist", 8),
            ("2026-04-05", "C Dist", 8),
            ("2026-05-05", "C Dist", 9),
            ("2026-06-05", "C Dist", 9),
            ("2026-07-05", "C Dist", 9),
        ]
        for i, (dt, party, mt) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, 'P', ?, 'MT', ?,
                          'Eva Distributors', '{}')
                """,
                (f"rs-{i}", dt, party, mt, mt),
            )
        conn.commit()


def test_reshape_other_cities_clears_sticky_and_keeps_metric() -> None:
    q = "How is this growth compared to other cities"
    assert wants_city_grain(q)
    assert wants_geo_expand(q)
    assert suggest_preferred_tool(q) == "analyze_parties"
    inf = infer_party_analytics_from_text(q)
    prior = {
        "kind": "analyze_parties",
        "metric": "ams_growth",
        "group_by": "party",
        "filters": {"city": "Lahore", "client_type": "Eva Distributors"},
    }
    reshape = resolve_analytics_reshape(
        q,
        arguments={"city": "Lahore", "group_by": "party", "metric": "ams_growth"},
        inferred=inf,
        prior_party_spec=prior,
        prior_ctx={"city": "Lahore", "client_type": "Eva Distributors"},
    )
    assert reshape["group_by"] == "city"
    assert reshape["clear_city"] is True
    assert reshape["metric"] == "ams_growth"
    assert reshape["title_mode"] == "by_growth"
    assert reshape["client_type"] == "Eva Distributors"


def test_dispatch_growth_vs_other_cities() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            q = "How is this growth compared to other cities"
            prior_party = {
                "kind": "analyze_parties",
                "metric": "ams_growth",
                "group_by": "party",
                "filters": {
                    "city": "Lahore",
                    "client_type": "Eva Distributors",
                },
                "period": {
                    "date_from": "2026-03-01",
                    "date_to": "2026-08-05",
                    "label": "Mar–Aug",
                },
            }
            # Sticky model args that previously caused the bug
            out = _dispatch_tool(
                "analyze_parties",
                {
                    "city": "Lahore",
                    "group_by": "party",
                    "metric": "ams_growth",
                    "sort": "desc",
                },
                user_text=q,
                prior_party_spec=prior_party,
            )
            assert out["ok"] is True
            assert out["metric"] == "ams_growth"
            assert out["filters"].get("city") is None
            assert out["filters"].get("client_type") == "Eva Distributors"
            assert out["filters"].get("group_by") == "city"
            md = out["answer_markdown"]
            assert "Cities by AMS growth %" in md
            assert "Biggest AMS gains" not in md
            assert "city **Lahore**" not in md.lower()
            cities = [p.get("city") for p in out["parties"]]
            assert "Karachi" in cities
            assert "Lahore" in cities
            # City AMS growth — not party names in the City column
            assert "A Dist" not in cities
            # Karachi grew hardest
            assert cities[0] == "Karachi"
            assert float(out["parties"][0]["ams_growth_pct"]) > 0
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_city_ams_growth_uses_geo_prior_not_party_keys() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = _dispatch_tool(
                "analyze_parties",
                {
                    "metric": "ams_growth",
                    "group_by": "city",
                    "client_type": "Eva Distributors",
                },
                user_text="city league AMS growth for distributors",
            )
            assert out["ok"] is True
            cities = [p.get("city") for p in out["parties"]]
            assert set(cities) >= {"Karachi", "Lahore", "Islamabad"}
            # No phantom -100% party-key leakage
            for p in out["parties"]:
                assert p.get("city") in {"Karachi", "Lahore", "Islamabad"}
                assert p.get("ams_growth_pct") is not None
                assert float(p["ams_growth_pct"]) > -100.0 or p["city"] == "Lahore"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
