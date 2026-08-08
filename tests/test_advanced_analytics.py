"""Tests for advanced analytics, seasonality, and whole-number MT."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.advanced_analytics import (
    compare_segments,
    detect_dumping,
    filter_entities,
    not_ordered,
    silent_parties,
)
from eva_dashboard.advanced_routing import infer_advanced_from_text, looks_advanced
from eva_dashboard.chatbot import _dispatch_tool
from eva_dashboard.db import connect, init_db
from eva_dashboard.sales_query import query_sales
from eva_dashboard.seasonality import expected_month_close, recompute_seasonality


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
                ("Eva Cooking Oil Pillow 1L", "Eva Consumer", "Eva Cooking", "Pillow"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "Alpha Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("2", "Beta Dist", "Eva Distributors", "Karachi", "Karachi"),
                ("3", "Silent Guy", "Eva Distributors", "Lahore", "Lahore"),
                ("4", "Isb Dist", "Eva Distributors", "Islamabad", "Islamabad"),
                ("5", "Metro Lhr", "METRO HABIB", "Lahore", "Lahore"),
                ("6", "Chase Up Khi", "CHASE UP", "Karachi", "Karachi"),
                ("7", "Imtiaz Lhr", "Imtiaz Store", "Lahore", "Lahore"),
            ],
        )
        rows = []
        for month in ("05", "06", "07"):
            rows += [
                (f"2026-{month}-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30.0, "A"),
                (f"2026-{month}-05", "Beta Dist", "Eva Canola Oil (StandUpPouch)", 20.0, "B"),
                (f"2026-{month}-05", "Silent Guy", "Eva Canola Oil (StandUpPouch)", 15.0, "S"),
                (f"2026-{month}-05", "Isb Dist", "Eva Canola Oil (StandUpPouch)", 12.0, "I"),
                (f"2026-{month}-12", "Alpha Dist", "Eva Cooking Oil Pillow 1L", 8.0, "P"),
                (f"2026-{month}-12", "Metro Lhr", "Eva Canola Oil (StandUpPouch)", 18.0, "M"),
                (f"2026-{month}-12", "Chase Up Khi", "Eva Canola Oil (StandUpPouch)", 14.0, "C"),
                (f"2026-{month}-12", "Imtiaz Lhr", "Eva Canola Oil (StandUpPouch)", 22.0, "Z"),
            ]
        rows += [
            ("2026-08-01", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 10.0, "A8"),
            ("2026-08-04", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 90.0, "DUMP"),
            ("2025-08-02", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 10.0, "Y"),
            ("2025-08-02", "Beta Dist", "Eva Canola Oil (StandUpPouch)", 30.0, "Y2"),
            ("2025-08-02", "Isb Dist", "Eva Canola Oil (StandUpPouch)", 8.0, "Y3"),
        ]
        for i, (dt, party, product, mt, inv) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, inv_no, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, '', '{}')
                """,
                (f"advt-{i}", dt, party, product, mt, mt, inv),
            )
        conn.commit()


def test_mt_whole_numbers_and_seasonality_expected() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            mat = query_sales(
                period="August so far",
                business_unit="Eva Consumer",
                city="Lahore",
            )
            assert mat["ok"] is True
            for row in mat["matrix"]["rows"]:
                for k, v in row.items():
                    if k in {
                        "packing_category",
                        "business_unit",
                        "product",
                        "oil_type",
                        "row_kind",
                        "city",
                        "client_type",
                    }:
                        continue
                    assert isinstance(v, int), (k, v, type(v))
            assert mat["matrix"].get("hierarchical") is True
            sea = recompute_seasonality()
            assert sea["ok"] is True
            exp = expected_month_close(business_unit="Eva Consumer")
            assert exp["ok"] is True
            assert isinstance(exp["seasonality_projection_mt"], int)
            assert "### Analysis" in exp["answer_markdown"]
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_compare_silent_not_ordered_dumping_routing() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            cmp = compare_segments(
                segment="city",
                left="Lahore",
                right="Karachi",
                period="August so far",
                business_unit="Eva Consumer",
                metric="growth",
            )
            assert cmp["ok"] is True
            assert cmp["left"]["volume_mt"] > cmp["right"]["volume_mt"]

            sil = silent_parties(grain="week", client_type="Eva Distributors")
            names = [p["party"] for p in sil["parties"]]
            assert "Silent Guy" in names

            no = not_ordered(
                packing_category="Pillow",
                client_type="Eva Distributors",
                period="this month",
            )
            assert no["parties"]
            assert no["parties"][0]["ams_mt"] >= no["parties"][-1]["ams_mt"]

            dump = detect_dumping(period="this month")
            assert dump["case_count"] >= 1
            assert dump["cases"][0]["party"] == "Alpha Dist"

            assert looks_advanced("Compare growth in Karachi and Lahore")
            multi_city = infer_advanced_from_text(
                "Compare Lahore vs Karachi vs Islamabad last month"
            )
            assert multi_city["mode"] == "compare_cities"
            assert multi_city["entities"] == ["Lahore", "Karachi", "Islamabad"]
            multi_type = infer_advanced_from_text(
                "Imtiaz vs Metro vs Chase Up this month"
            )
            assert multi_type["mode"] == "compare_client_types"
            # Metro + Chase Up both remap to IMT
            assert multi_type["entities"] == ["Imtiaz Store", "IMT"]
            pairwise = infer_advanced_from_text(
                "Compare Imtiaz vs distributors growth last month"
            )
            assert pairwise["mode"] == "compare_client_types"
            assert pairwise["entities"] == ["Imtiaz Store", "Eva Distributors"]

            city3 = compare_segments(
                segment="city",
                entities=["Lahore", "Karachi", "Islamabad"],
                period="July",
                business_unit="Eva Consumer",
            )
            assert city3["ok"] is True
            assert len(city3["entities"]) == 3
            assert "Lahore vs Karachi vs Islamabad" in city3["answer_markdown"]

            type3 = compare_segments(
                segment="client_type",
                entities=["Imtiaz Store", "METRO HABIB", "CHASE UP"],
                period="July",
            )
            assert type3["ok"] is True
            # METRO HABIB + CHASE UP collapse to IMT
            assert len(type3["entities"]) == 2
            names = {e["name"] for e in type3["entities"]}
            assert names == {"Imtiaz Store", "IMT"}

            disp = _dispatch_tool(
                "advanced_query",
                {},
                user_text="Compare Lahore vs Karachi vs Islamabad for July",
            )
            assert disp["ok"] is True
            assert len(disp["entities"]) == 3

            assert infer_advanced_from_text(
                "What customers have not had any sale this week"
            )["mode"] == "silent_week"
            assert infer_advanced_from_text(
                "Can you identify any dumping cases"
            )["mode"] == "dumping"

            out = _dispatch_tool(
                "advanced_query",
                {},
                user_text="Which distributors have not ordered Stand up this month",
            )
            assert out["ok"] is True
            assert out["mode"] == "not_ordered"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_filter_entities_volume_yoy_mom_routing() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()

            vol = filter_entities(
                entity="party",
                metric="volume",
                op="gt",
                threshold=10,
                period="this month",
                client_type="Eva Distributors",
            )
            assert vol["ok"] is True
            names = [r["entity"] for r in vol["rows"]]
            assert "Alpha Dist" in names
            assert all(r["volume_mt"] > 10 for r in vol["rows"])

            yoy = filter_entities(
                entity="party",
                metric="yoy",
                op="declined",
                threshold=0,
                period="this month",
                client_type="Eva Distributors",
            )
            assert yoy["ok"] is True
            declined = [r["entity"] for r in yoy["rows"]]
            assert "Beta Dist" in declined
            assert "Alpha Dist" not in declined

            grown = filter_entities(
                entity="party",
                metric="yoy",
                op="grown",
                threshold=10,
                period="this month",
            )
            assert any(r["entity"] == "Alpha Dist" for r in grown["rows"])

            mom = filter_entities(
                entity="party",
                metric="mom",
                op="grown",
                threshold=0,
                period="this month",
            )
            assert mom["ok"] is True
            # Alpha Aug (100) > July (38)
            assert any(r["entity"] == "Alpha Dist" for r in mom["rows"])

            prod = filter_entities(
                entity="product",
                metric="volume",
                op="gt",
                threshold=50,
                period="this month",
            )
            assert prod["rows"]
            assert all(r["volume_mt"] > 50 for r in prod["rows"])

            inf = infer_advanced_from_text(
                "Distributors in Lahore where sales have declined"
            )
            assert inf["mode"] == "filter_entities"
            assert inf["op"] == "declined"
            assert inf["metric"] == "yoy"
            assert inf["city"] == "Lahore"
            assert inf["client_type"] == "Eva Distributors"

            inf2 = infer_advanced_from_text("sales more than 10 tons")
            assert inf2["mode"] == "filter_entities"
            assert inf2["metric"] == "volume"
            assert inf2["op"] == "gt"
            assert inf2["threshold"] == 10.0

            inf3 = infer_advanced_from_text(
                "products that declined more than 10%"
            )
            assert inf3["mode"] == "filter_entities"
            assert inf3["entity"] == "product"
            assert inf3["op"] == "declined"
            assert inf3["threshold"] == 10.0

            inf4 = infer_advanced_from_text(
                "more sales this month than last month"
            )
            assert inf4["mode"] == "filter_entities"
            assert inf4["metric"] == "mom"
            assert inf4["op"] == "grown"

            out = _dispatch_tool(
                "advanced_query",
                {},
                user_text="Customers that have grown sales",
            )
            assert out["ok"] is True
            assert out["mode"] == "filter_entities"
            assert out["op"] == "grown"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
