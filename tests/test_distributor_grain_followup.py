"""Distributor-wise follow-ups via explicit QuerySpecs (no heuristic planner)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.ai_guide import tool_guide_for_prompt, vocabulary_for_prompt
from eva_dashboard.client_language import (
    extract_client_type_from_text,
    is_distributor_party_grain,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.party_analytics import analyze_parties
from eva_dashboard.query_executor import execute_query_spec


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
                ("Eva Cooking Oil (16 Ltr Tin)", "Eva Bulk", "Eva Bulk", "Tin"),
                ("Shortening A", "Shortening", "Shortening", "Tin"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "Alpha Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("2", "Beta Store", "Imtiaz Store", "Karachi", "Karachi"),
                ("3", "Gamma New", "Other Clients", "Islamabad", "Islamabad"),
            ],
        )
        rows = [
            ("2026-04-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30.0, "Eva Distributors"),
            ("2026-05-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30.0, "Eva Distributors"),
            ("2026-06-10", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30.0, "Eva Distributors"),
            ("2026-04-11", "Beta Store", "Eva Cooking Oil (16 Ltr Tin)", 10.0, "Imtiaz Store"),
            ("2026-05-11", "Beta Store", "Eva Cooking Oil (16 Ltr Tin)", 10.0, "Imtiaz Store"),
            ("2026-06-11", "Beta Store", "Eva Cooking Oil (16 Ltr Tin)", 10.0, "Imtiaz Store"),
            ("2026-07-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 20.0, "Eva Distributors"),
            ("2026-07-06", "Beta Store", "Eva Cooking Oil (16 Ltr Tin)", 5.0, "Imtiaz Store"),
            ("2026-07-07", "Gamma New", "Eva Canola Oil (StandUpPouch)", 100.0, "Other Clients"),
            ("2026-07-08", "Alpha Dist", "Shortening A", 50.0, "Eva Distributors"),
        ]
        for i, (dt, party, product, mt, ctype) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, 100, 1000, ?, '{}')
                """,
                (f"dg-{i}-{dt}", dt, party, product, mt, mt, ctype),
            )
        conn.commit()


def test_grain_language_does_not_mean_eva_distributors_channel() -> None:
    q = (
        "can you show this distributor wise identifying the "
        "lowest performing distributors"
    )
    assert is_distributor_party_grain(q)
    assert extract_client_type_from_text(q) is None
    assert (
        extract_client_type_from_text("Canola price for Distributors last week")
        == "Eva Distributors"
    )


def test_prompt_teaches_distributor_grain_vs_channel() -> None:
    vocab = vocabulary_for_prompt()
    guide = tool_guide_for_prompt()
    assert "distributor-wise" in vocab.lower() or "distributor wise" in vocab.lower()
    assert "vs_ams" in vocab
    assert "Eva Distributors" in guide


def test_lowest_performing_via_explicit_query_spec() -> None:
    """Mock QuerySpec: prior Eva BUs + distributor-wise + vs_ams asc."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            prior = {
                "source": "sales",
                "business_units": ["Eva Consumer", "Eva Bulk"],
                "filters": {},
                "period_phrase": "July",
            }
            plan = {
                "intent": "party_rank",
                "context_handling": "prior",
                "clear_filters": ["client_type"],
                "period_type": "NAMED_MONTH",
                "named_month": "July",
                "group_by": "party",
                "ranking_metric": "vs_ams",
                "sort_order": "asc",
                "title_mode": "underperformers",
                "business_units": ["Eva Consumer", "Eva Bulk"],
            }
            out = execute_query_spec(plan, prior=prior)
            assert out.get("ok") is True, out
            md = out.get("answer_markdown") or ""
            assert "Eva Distributors" not in md.split("\n")[0]
            assert "Lowest" in md
            assert "Top parties by AMS" not in md
            for row in out.get("parties") or []:
                assert (row.get("ams_mt") or 0) > 0
            assert any(
                p.get("party") == "Beta Store" for p in (out.get("parties") or [])
            ) or "Beta Store" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_ams_asc_excludes_zero_baseline() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = analyze_parties(
                period="July",
                metric="ams",
                sort="asc",
                brand="eva",
                limit=10,
            )
            assert out.get("ok") is True
            for row in out.get("parties") or []:
                assert (row.get("ams_mt") or 0) > 0
            assert "Lowest" in (out.get("answer_markdown") or "")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
