"""Tests for client lists and party analytics."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    _dispatch_tool,
    _looks_client_list,
    _looks_party_analytics,
    _looks_party_lookup,
    _looks_sales_matrix,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.party_analytics import (
    analyze_parties,
    infer_party_analytics_from_text,
    list_clients,
)
from eva_dashboard.sales_query import resolve_period


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
                ("Eva VTF Banaspati 1x5 Pouch", "Eva Consumer", "Eva VTF", "Pouch"),
                ("Eva Cooking Oil (16 Ltr Tin)", "Eva Bulk", "Eva Bulk", "Tin"),
                ("Eva Cooking Oil Pillow 1L", "Eva Consumer", "Eva Cooking", "Pillow"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "Alpha Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("2", "Beta Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("3", "Gamma Dist", "Eva Distributors", "Karachi", "Karachi"),
                ("4", "Imtiaz A", "Imtiaz Store", "Lahore", "Lahore"),
                ("5", "Imtiaz B", "Imtiaz Store", "Karachi", "Karachi"),
                ("6", "Other Guy", "Other Clients", "Lahore", "Lahore"),
                ("7", "Newbie Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("8", "Silent Dist", "Eva Distributors", "Multan", "Multan"),
            ],
        )
        # Prior AMS months May–July; current Aug partial
        rows = []
        for month, day in [("05", 10), ("06", 10), ("07", 10)]:
            rows += [
                (f"2026-{month}-{day}", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30.0, f"INV-A-{month}"),
                (f"2026-{month}-{day}", "Beta Dist", "Eva Canola Oil (StandUpPouch)", 10.0, f"INV-B-{month}"),
                (f"2026-{month}-{day}", "Gamma Dist", "Eva Canola Oil (StandUpPouch)", 20.0, f"INV-G-{month}"),
                (f"2026-{month}-{day}", "Imtiaz A", "Eva VTF Banaspati 1x5 Pouch", 15.0, f"INV-IA-{month}"),
                (f"2026-{month}-{day}", "Imtiaz B", "Eva VTF Banaspati 1x5 Pouch", 5.0, f"INV-IB-{month}"),
                (f"2026-{month}-{day}", "Imtiaz A", "Eva Canola Oil (StandUpPouch)", 5.0, f"INV-IA2-{month}"),
                (f"2026-{month}-{day}", "Silent Dist", "Eva Canola Oil (StandUpPouch)", 18.0, f"INV-S-{month}"),
            ]
        # July last year for YoY
        rows += [
            ("2025-07-10", "Alpha Dist", "Eva VTF Banaspati 1x5 Pouch", 8.0, "INV-Y1"),
            ("2025-07-10", "Beta Dist", "Eva VTF Banaspati 1x5 Pouch", 20.0, "INV-Y2"),
        ]
        # Current July VTF for YoY compare target + pillow for packing rank
        rows += [
            ("2026-07-15", "Alpha Dist", "Eva VTF Banaspati 1x5 Pouch", 25.0, "INV-JV1"),
            ("2026-07-15", "Beta Dist", "Eva VTF Banaspati 1x5 Pouch", 12.0, "INV-JV2"),
            ("2026-07-20", "Alpha Dist", "Eva Cooking Oil Pillow 1L", 40.0, "INV-P1"),
            ("2026-07-20", "Gamma Dist", "Eva Cooking Oil Pillow 1L", 10.0, "INV-P2"),
        ]
        # New party first sale in last 6 months (March 2026)
        rows += [
            ("2026-03-05", "Newbie Dist", "Eva Canola Oil (StandUpPouch)", 7.0, "INV-NEW"),
            ("2026-07-01", "Newbie Dist", "Eva Canola Oil (StandUpPouch)", 3.0, "INV-NEW2"),
        ]
        # August (Silent Dist has AMS but zero here → lost)
        rows += [
            ("2026-08-01", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 12.0, "INV-A8a"),
            ("2026-08-01", "Alpha Dist", "Eva VTF Banaspati 1x5 Pouch", 4.0, "INV-A8b"),
            ("2026-08-02", "Beta Dist", "Eva Canola Oil (StandUpPouch)", 2.0, "INV-B8"),
            ("2026-08-02", "Imtiaz A", "Eva VTF Banaspati 1x5 Pouch", 6.0, "INV-IA8"),
            ("2026-08-03", "Other Guy", "Eva Canola Oil (StandUpPouch)", 1.0, "INV-O8"),
            ("2026-08-03", "Newbie Dist", "Eva Canola Oil (StandUpPouch)", 2.0, "INV-N8"),
        ]
        for i, (dt, party, product, mt, inv) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, inv_no, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, '', '{}')
                """,
                (f"pa-{i}-{dt}-{party}", dt, party, product, mt, mt, inv),
            )
        conn.commit()


def test_routing_client_list_vs_name_lookup() -> None:
    assert _looks_client_list("Who are my distributors in Lahore")
    assert not _looks_party_lookup("Who are my distributors in Lahore")
    assert _looks_party_lookup("Who is Al Bari?")
    assert _looks_party_analytics("Top 10 parties by AMS in Karachi")
    assert _looks_party_analytics("Which distributors grew VTF vs July last year")
    assert _looks_party_analytics("Who were the top distributors in this")


def test_top_distributors_in_this_followup() -> None:
    """After a Consumer×Lahore×July matrix, 'top distributors in this' ranks parties."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            q = "Who were the top distributors in this"
            assert _looks_party_analytics(q)
            assert not _looks_client_list(q)
            assert not _looks_sales_matrix(q)

            inferred = infer_party_analytics_from_text(q)
            assert inferred["metric"] == "volume"
            assert inferred["client_type"] == "Eva Distributors"

            prior = {
                "period_phrase": "July 2026",
                "period": {
                    "date_from": "2026-07-01",
                    "date_to": "2026-07-31",
                    "label": "Jul 2026",
                },
                "filters": {
                    "city": "Lahore",
                    "business_unit": "Eva Consumer",
                    "oil_type": None,
                    "packing_category": None,
                    "client_type": None,
                },
                "business_units": ["Eva Consumer"],
                "column_dimension": "client_type",
                "row_dimension": "packing_category",
            }
            out = _dispatch_tool(
                "analyze_parties",
                {},
                user_text=q,
                prior_spec=prior,
            )
            assert out["ok"] is True
            assert out["metric"] == "volume"
            assert out["filters"]["city"] == "Lahore"
            assert out["filters"]["client_type"] == "Eva Distributors"
            assert out["filters"]["business_unit"] == "Eva Consumer"
            assert out["period"]["date_from"].startswith("2026-07")
            parties = [p["party"] for p in out["parties"]]
            assert "Alpha Dist" in parties
            assert "Imtiaz A" not in parties
            # Alpha has more July Consumer volume in Lahore than Beta
            assert parties[0] == "Alpha Dist"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_list_distributors_in_lahore() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = _dispatch_tool(
                "lookup_party",
                {"query": "Lahore"},
                user_text="Who are my distributors in Lahore",
            )
            assert out["ok"] is True
            assert out["mode"] == "list_clients"
            names = [c["client"] for c in out["clients"]]
            assert "Alpha Dist" in names
            assert "Beta Dist" in names
            assert "Other Guy" not in names
            assert "Gamma Dist" not in names
            assert all(c["client_type"] == "Eva Distributors" for c in out["clients"])
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_top_parties_ams_karachi() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = analyze_parties(
                period="August so far",
                city="Karachi",
                metric="ams",
                limit=5,
            )
            assert out["ok"] is True
            parties = [p["party"] for p in out["parties"]]
            assert "Gamma Dist" in parties or "Imtiaz B" in parties
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_imtiaz_vtf_share_and_geo_pct() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            share = analyze_parties(
                period="July 2026",
                client_type="Imtiaz Store",
                oil_type="Eva VTF",
                metric="share_of_segment",
                limit=5,
            )
            assert share["ok"] is True
            assert share["parties"][0]["party"] == "Imtiaz A"

            geo = analyze_parties(
                period="July 2026",
                oil_type="Eva VTF",
                metric="geo_share",
                share_city="Lahore",
            )
            assert geo["ok"] is True
            assert geo["share_pct"] is not None
            assert geo["share_pct"] > 50  # Imtiaz A VTF dominates in Lahore
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_distributors_vs_ams_and_yoy_vtf() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            well = analyze_parties(
                period="last 3 months",
                client_type="Eva Distributors",
                metric="vs_ams",
                limit=10,
            )
            assert well["ok"] is True
            assert well["parties"]

            yoy = analyze_parties(
                period="July 2026",
                compare_period="July last year",
                client_type="Eva Distributors",
                oil_type="Eva VTF",
                metric="yoy",
                limit=5,
            )
            assert yoy["ok"] is True
            # Alpha 25 vs 8 → bigger growth than Beta 12 vs 20
            assert yoy["parties"][0]["party"] == "Alpha Dist"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_resolve_last_quarter_and_last_year_month() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            q = resolve_period("last quarter")
            assert q["date_to"] == "2026-08-03"
            assert q["date_from"] == "2026-06-01"
            ly = resolve_period("July last year")
            assert ly["date_from"] == "2025-07-01"
            assert ly["date_to"] == "2025-07-31"
            six = resolve_period("last 6 months")
            assert six["ok"] is not False
            assert six["date_from"] <= "2026-03-01"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_infer_new_lost_poor_mix_rank_defaults() -> None:
    new = infer_party_analytics_from_text("New distributors in last 6 months")
    assert new["metric"] == "new_parties"
    assert new["period"] == "last 6 months"
    assert new["client_type"] == "Eva Distributors"

    lost = infer_party_analytics_from_text("Lost parties this month")
    assert lost["metric"] == "lost_parties"
    assert lost["period"] == "this month"
    assert lost["client_type"] is None  # all clients unless specified

    poor = infer_party_analytics_from_text(
        "Which distributors are performing poorly in Lahore"
    )
    assert poor["metric"] == "vs_ams"
    assert poor["sort"] == "asc"
    assert poor["city"] == "Lahore"
    assert poor["client_type"] == "Eva Distributors"

    behind = infer_party_analytics_from_text(
        "Which Imtiaz store is falling behind on average sales"
    )
    assert behind["metric"] == "vs_ams"
    assert behind["sort"] == "asc"
    assert behind["client_type"] == "Imtiaz Store"

    mix = infer_party_analytics_from_text("What's the product mix for Imtiaz")
    assert mix["metric"] == "packing_mix"
    assert mix["mix_dimension"] == "packing_category"
    assert mix["client_type"] == "Imtiaz Store"

    sku = infer_party_analytics_from_text("SKU wise breakdown for distributors in Lahore")
    assert sku["metric"] == "product_mix"
    assert sku["mix_dimension"] == "product"

    rank = infer_party_analytics_from_text("Top 5 distributors for Eva VTF")
    assert rank["metric"] == "ams"
    assert rank["limit"] == 5
    assert rank["oil_type"] == "Eva VTF"
    assert rank["client_type"] == "Eva Distributors"

    pillow = infer_party_analytics_from_text("Top 5 distributors for Pillow pouch")
    assert pillow["metric"] == "ams"
    assert pillow["packing_category"] is not None

    growth = infer_party_analytics_from_text(
        "Show me distributors by top sales growth in July"
    )
    assert growth["metric"] == "yoy"
    assert growth["period"] == "July" or (
        growth["period"] and "july" in growth["period"].lower()
    )

    cities = infer_party_analytics_from_text("City league table top 10 cities")
    assert cities["group_by"] == "city"

    inv = infer_party_analytics_from_text("Most invoices for distributors")
    assert inv["metric"] == "invoices"

    assert _looks_party_analytics("New parties last 6 months")
    assert not _looks_client_list("New parties last 6 months")
    assert _looks_party_analytics("Product mix for Imtiaz")
    assert not _looks_client_list("Show me product mix for Imtiaz")
    assert _looks_party_analytics("Top Imtiaz stores selling cooking oil")


def test_new_and_lost_parties() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            new = analyze_parties(
                period="last 6 months",
                client_type="Eva Distributors",
                metric="new_parties",
                limit=20,
            )
            assert new["ok"] is True
            names = [p["party"] for p in new["parties"]]
            assert "Newbie Dist" in names
            assert "Alpha Dist" not in names  # first sale before window

            lost = analyze_parties(
                period="this month",
                client_type="Eva Distributors",
                metric="lost_parties",
                limit=20,
            )
            assert lost["ok"] is True
            lost_names = [p["party"] for p in lost["parties"]]
            assert "Silent Dist" in lost_names
            assert "Alpha Dist" not in lost_names
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_underperformers_packing_mix_invoices_city_rank() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            poor = analyze_parties(
                period="August so far",
                client_type="Eva Distributors",
                city="Lahore",
                metric="vs_ams",
                sort="asc",
                limit=5,
            )
            assert poor["ok"] is True
            assert poor["parties"]
            # Beta (2 vs AMS ~10) worse than Alpha (12 vs AMS ~30) on %?
            # Just ensure ascending % vs AMS
            pcts = [
                p["pct_vs_ams"]
                for p in poor["parties"]
                if p.get("pct_vs_ams") is not None
            ]
            assert pcts == sorted(pcts)

            mix = analyze_parties(
                period="August so far",
                client_type="Imtiaz Store",
                metric="packing_mix",
                limit=10,
            )
            assert mix["ok"] is True
            assert mix["mix_dimension"] == "packing_category"
            assert mix["rows"]

            sku = analyze_parties(
                period="August so far",
                city="Lahore",
                client_type="Eva Distributors",
                metric="product_mix",
                limit=10,
            )
            assert sku["ok"] is True
            assert sku["mix_dimension"] == "product"

            inv = analyze_parties(
                period="last 3 months",
                client_type="Eva Distributors",
                metric="invoices",
                limit=5,
            )
            assert inv["ok"] is True
            assert inv["parties"][0]["invoices"] >= 1

            cities = analyze_parties(
                period="July 2026",
                packing_category="Pillow",
                metric="ams",
                group_by="city",
                limit=5,
            )
            assert cities["ok"] is True
            assert cities["filters"]["group_by"] == "city"

            top_vtf = _dispatch_tool(
                "analyze_parties",
                {},
                user_text="Top 5 distributors for Eva VTF",
            )
            assert top_vtf["ok"] is True
            assert top_vtf["metric"] == "ams"
            assert top_vtf["filters"]["client_type"] == "Eva Distributors"
            assert top_vtf["filters"]["oil_type"] == "Eva VTF"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
