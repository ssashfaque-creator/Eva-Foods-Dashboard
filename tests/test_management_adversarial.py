"""Adversarial management questions + multi-turn follow-ups.

Simulates how executives actually ask (messy language, short follow-ups,
compares, AMS, price, profile) and asserts sticky/accurate engine behavior.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from eva_dashboard.agent_loop import (
    looks_mixed_party_channel_compare,
    looks_multi_hop,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.metrics_catalog import (
    apply_metric_synonyms_to_spec,
    resolve_metrics_from_text,
    resolve_operation_from_text,
)
from eva_dashboard.query_executor import execute_query_spec
from eva_dashboard.query_spec import (
    prior_context_from_query_state,
    prior_context_payload,
)


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed_rich() -> None:
    """Richer world: two cities, two channels, two parties, mix of SKUs."""
    init_db()
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, '{}', datetime('now'))",
            [
                ("Eva Canola Oil (StandUpPouch)", "Eva Consumer", "Eva Canola", "Stand up"),
                ("Eva Cooking Oil 16 Ltr J/Can", "Eva Consumer", "Eva Cooking", "Jerry Can"),
                ("Eva VTF Banaspati 16 Kg Tin", "Eva Bulk", "Eva VTF Bulk", "Tin"),
                ("Maan Banaspati 16 Kg Tin", "Maan Consumer", "Maan Ghee", "Tin"),
            ],
        )
        clients = [
            ("1", "Al Shaheer Lahore", "Eva Distributors", "Lahore"),
            ("2", "Al Shaheer Karachi", "Eva Distributors", "Karachi"),
            ("3", "Alpha Dist", "Eva Distributors", "Lahore"),
            ("4", "Imtiaz Gulberg", "Imtiaz Store", "Lahore"),
            ("5", "Imtiaz DHA", "Imtiaz Store", "Karachi"),
            ("6", "Metro Habib Outlet", "METRO HABIB", "Lahore"),
        ]
        for cid, name, ctype, city in clients:
            conn.execute(
                "INSERT OR REPLACE INTO clients "
                "(client_id, client, type, city_filter, city, inactive, "
                "payload_json, updated_at) VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
                (cid, name, ctype, city, city),
            )
        # AMS window Apr–Jun; focus July (+ partial Aug)
        rows = [
            # Al Shaheer Lahore
            ("2026-04-10", "Al Shaheer Lahore", "Eva Canola Oil (StandUpPouch)", 30, 100, "Eva Distributors"),
            ("2026-05-10", "Al Shaheer Lahore", "Eva Canola Oil (StandUpPouch)", 30, 100, "Eva Distributors"),
            ("2026-06-10", "Al Shaheer Lahore", "Eva Canola Oil (StandUpPouch)", 30, 100, "Eva Distributors"),
            ("2026-07-05", "Al Shaheer Lahore", "Eva Canola Oil (StandUpPouch)", 40, 110, "Eva Distributors"),
            ("2026-07-08", "Al Shaheer Lahore", "Eva Cooking Oil 16 Ltr J/Can", 10, 90, "Eva Distributors"),
            ("2026-07-12", "Al Shaheer Lahore", "Eva VTF Banaspati 16 Kg Tin", 20, 95, "Eva Distributors"),
            # Al Shaheer Karachi
            ("2026-04-11", "Al Shaheer Karachi", "Eva Canola Oil (StandUpPouch)", 15, 100, "Eva Distributors"),
            ("2026-05-11", "Al Shaheer Karachi", "Eva Canola Oil (StandUpPouch)", 15, 100, "Eva Distributors"),
            ("2026-06-11", "Al Shaheer Karachi", "Eva Canola Oil (StandUpPouch)", 15, 100, "Eva Distributors"),
            ("2026-07-06", "Al Shaheer Karachi", "Eva Canola Oil (StandUpPouch)", 12, 105, "Eva Distributors"),
            # Alpha Dist
            ("2026-04-12", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 20, 100, "Eva Distributors"),
            ("2026-05-12", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 20, 100, "Eva Distributors"),
            ("2026-06-12", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 20, 100, "Eva Distributors"),
            ("2026-07-07", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 25, 112, "Eva Distributors"),
            ("2026-07-20", "Alpha Dist", "Maan Banaspati 16 Kg Tin", 5, 80, "Eva Distributors"),
            # Imtiaz
            ("2026-04-13", "Imtiaz Gulberg", "Eva Canola Oil (StandUpPouch)", 40, 100, "Imtiaz Store"),
            ("2026-05-13", "Imtiaz Gulberg", "Eva Canola Oil (StandUpPouch)", 40, 100, "Imtiaz Store"),
            ("2026-06-13", "Imtiaz Gulberg", "Eva Canola Oil (StandUpPouch)", 40, 100, "Imtiaz Store"),
            ("2026-07-09", "Imtiaz Gulberg", "Eva Canola Oil (StandUpPouch)", 50, 108, "Imtiaz Store"),
            ("2026-07-15", "Imtiaz DHA", "Eva Canola Oil (StandUpPouch)", 22, 107, "Imtiaz Store"),
            # Metro
            ("2026-07-11", "Metro Habib Outlet", "Eva Cooking Oil 16 Ltr J/Can", 8, 88, "METRO HABIB"),
        ]
        for i, (dt, party, prod, mt, rate, ctype) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, ?, ?, '{}')
                """,
                (f"adv-{i}", dt, party, prod, mt, mt, rate, mt * rate, ctype),
            )
        # Minimal factor costs for Price Fetch
        conn.execute(
            """
            INSERT OR REPLACE INTO factor_costs
            (client_type, prod_id, product, unit, product_cost, packing_cost,
             total_factor_cost, updated_at)
            VALUES ('Eva Distributors', 'C1', 'Eva Canola Oil (StandUpPouch)',
                    'Ltrs', 50, 10, 60, datetime('now'))
            """
        )
        conn.commit()


def _run(plan: dict[str, Any], user_text: str, prior=None) -> dict[str, Any]:
    return execute_query_spec(plan, prior=prior, user_text=user_text)


def _party(out: dict[str, Any]) -> str | None:
    f = (out.get("query_spec") or {}).get("filters") or {}
    return f.get("party") or (out.get("party"))


def test_management_conversation_shaheer_chain() -> None:
    """Profile → price → % AMS → last purchase → SKU wise."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed_rich()

            # 1) Messy profile ask — branch family aggregates (not a pick list)
            p1 = _run(
                {
                    "operation": "pivot",
                    "row_dimensions": ["party"],
                    "metrics": ["volume"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "extracted_entities": ["al shaheer"],
                },
                "tell me about al shaheer how are they doing in july",
            )
            assert p1.get("ok") is True, p1
            assert p1.get("mode") == "party_profile"
            assert p1.get("volume_mt") == 82.0  # Lahore 70 + Karachi 12
            assert len(p1.get("parties") or []) >= 2
            assert "Branches" in (p1.get("answer_markdown") or "")
            fam_scope = ((p1.get("party_spec") or {}).get("filters") or {})
            assert fam_scope.get("party_ilike") or fam_scope.get("parties")

            # Force exact branch for deterministic follow-ups
            profile = _run(
                {
                    "operation": "party_profile",
                    "filters": {"party": "Al Shaheer Lahore"},
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "row_dimensions": ["party"],
                    "metrics": ["volume", "ams", "vs_ams"],
                },
                "customer profile for Al Shaheer Lahore in July",
            )
            assert profile.get("ok") is True, profile
            assert profile.get("volume_mt") == 70.0  # 40+10+20
            assert profile.get("last_sale") == "2026-07-12"
            assert profile.get("pct_vs_ams") is not None
            prior = prior_context_from_query_state(profile.get("query_state"))
            assert prior and (prior.get("party_scope") or {}).get("party") == "Al Shaheer Lahore"

            # 2) what's the price?
            price = _run(
                {
                    "row_dimensions": ["product"],
                    "metrics": [],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                },
                "what's the price?",
                prior=prior,
            )
            assert price.get("ok") is True, price
            assert _party(price) == "Al Shaheer Lahore"
            assert "avg_price" in ((price.get("query_spec") or {}).get("metrics") or [])

            # 3) % of AMS
            ams = _run(
                {
                    "row_dimensions": ["party"],
                    "metrics": ["volume"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                },
                "what % of their AMS is this",
                prior=prior,
            )
            assert ams.get("ok") is True, ams
            assert _party(ams) == "Al Shaheer Lahore"
            assert "vs_ams" in ((ams.get("query_spec") or {}).get("metrics") or [])

            # 4) last purchase date (short) → promote to profile, keep party
            last = _run(
                {
                    "operation": "pivot",
                    "row_dimensions": ["party"],
                    "metrics": ["volume"],
                    "period_type": "MTD",
                    "context_handling": "none",
                    "filters": {},
                },
                "last purchase date?",
                prior=prior,
            )
            assert last.get("ok") is True, last
            assert last.get("mode") == "party_profile"
            assert last.get("last_sale") == "2026-07-12"
            assert _party(last) == "Al Shaheer Lahore" or (
                (last.get("party_spec") or {}).get("filters") or {}
            ).get("party") == "Al Shaheer Lahore"

            # 5) SKU wise breakup
            sku = _run(
                {
                    "row_dimensions": ["product"],
                    "metrics": ["volume"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "prior",
                    "clear_filters": [],
                },
                "SKU wise",
                prior=prior,
            )
            assert sku.get("ok") is True, sku
            assert _party(sku) == "Al Shaheer Lahore"
            assert (sku.get("query_spec") or {}).get("row_dimensions") == ["product"]
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_management_imtiaz_vs_distributors_and_cities() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed_rich()
            # Channel compare
            out = _run(
                {
                    "row_dimensions": ["client_type"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "filters": {
                        "client_types": ["Imtiaz Store", "Eva Distributors"],
                        "city": "Lahore",
                    },
                },
                "compare Imtiaz vs distributors in Lahore last 6 months",
            )
            assert out.get("ok") is True, out
            md = out.get("answer_markdown") or ""
            assert "Imtiaz" in md or "Distributors" in md

            # City compare for Imtiaz
            cities = _run(
                {
                    "row_dimensions": ["city"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "filters": {
                        "client_type": "Imtiaz Store",
                        "cities": ["Lahore", "Karachi"],
                    },
                },
                "Imtiaz sales in Lahore vs Karachi",
            )
            assert cities.get("ok") is True, cities
            assert (cities.get("query_spec") or {}).get("row_dimensions") == ["city"]

            # Mixed party vs channel → investigation
            assert looks_mixed_party_channel_compare(
                "compare al shaheer growth with Imtiaz"
            )
            mixed = _run(
                {
                    "row_dimensions": ["party"],
                    "metrics": ["ams_growth"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "filters": {"party": "Al Shaheer Lahore"},
                },
                "compare al shaheer growth with Imtiaz",
            )
            assert mixed.get("ok") is True, mixed
            assert "INVESTIGATION" in (mixed.get("response_instructions") or "")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_management_product_slang_and_price_fetch() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed_rich()
            # Canola standup volume
            out = _run(
                {
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "filters": {
                        "oil_type": "Eva Canola",
                        "packing_category": "Stand up",
                        "client_type": "Eva Distributors",
                    },
                },
                "canola standup sales for distributors last 6 months",
            )
            assert out.get("ok") is True, out

            # Price Fetch
            pf = _run(
                {
                    "row_dimensions": ["product"],
                    "metrics": [],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "filters": {
                        "oil_type": "Eva Canola",
                        "packing_category": "Stand up",
                        "client_type": "Eva Distributors",
                    },
                },
                "Price Fetch for canola standup Distributors in July",
            )
            assert pf.get("ok") is True, pf
            mets = (pf.get("query_spec") or {}).get("metrics") or []
            assert "price_fetch" in mets
            md = pf.get("answer_markdown") or ""
            assert "Price Fetch" in md or "Avg" in md or pf.get("mode")

            # VTF bulk scoped
            vtf = _run(
                {
                    "row_dimensions": ["party"],
                    "metrics": ["volume"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "filters": {
                        "oil_type": "Eva VTF Bulk",
                        "city": "Lahore",
                    },
                },
                "who bought VTF bulk in Lahore in July",
            )
            assert vtf.get("ok") is True, vtf
            md = vtf.get("answer_markdown") or ""
            assert "Shaheer" in md or "Al Shaheer" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_management_followup_grain_changes_keep_scope() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed_rich()
            base = _run(
                {
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "filters": {"city": "Lahore", "client_type": "Eva Distributors"},
                    "business_units": ["Eva Consumer", "Eva Bulk"],
                },
                "show me Eva distributor sales in Lahore last 6 months",
            )
            assert base.get("ok") is True, base
            prior = prior_context_payload(table_spec=base.get("table_spec"))
            # Also stamp from query_state if present
            if base.get("query_state"):
                prior = prior_context_from_query_state(base["query_state"])

            # product wise (packing)
            pw = _run(
                {
                    "row_dimensions": ["packing_category"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "column_dimensions": ["month"],
                    "context_handling": "prior",
                    "clear_filters": [],
                },
                "show this product wise",
                prior=prior,
            )
            assert pw.get("ok") is True, pw
            f = (pw.get("query_spec") or {}).get("filters") or {}
            assert f.get("city") == "Lahore"
            assert f.get("client_type") == "Eva Distributors"

            # nationally / clear city
            nat = _run(
                {
                    "row_dimensions": ["city"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "column_dimensions": ["month"],
                    "context_handling": "prior",
                    "clear_filters": ["city"],
                },
                "show this nationally city wise",
                prior=prior,
            )
            assert nat.get("ok") is True, nat
            nf = (nat.get("query_spec") or {}).get("filters") or {}
            assert not nf.get("city")
            assert nf.get("client_type") == "Eva Distributors"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_management_spoken_synonyms_coverage() -> None:
    cases = [
        ("what percent of AMS are we at", "vs_ams"),
        ("are they falling behind on AMS", "vs_ams"),
        ("oil price fetched for this", "price_fetch"),
        ("apply the cost factor", "price_fetch"),
        ("what's the average rate", "avg_price"),
        ("biggest gains this month", "ams_growth"),
    ]
    for text, metric in cases:
        got = resolve_metrics_from_text(text)
        assert metric in got, f"{text} → {got} missing {metric}"

    assert resolve_operation_from_text("give me a rundown on Alpha Dist") == "party_profile"
    assert resolve_operation_from_text("when did they last buy") == "party_profile"
    assert looks_multi_hop("show Lahore and then dig into packing")


def test_management_underperformers_and_metro() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed_rich()
            # Lowest performing distributors July
            out = _run(
                {
                    "row_dimensions": ["party"],
                    "metrics": ["volume"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "filters": {"client_type": "Eva Distributors", "city": "Lahore"},
                    "sort_order": "asc",
                },
                "lowest performing distributors in Lahore July vs AMS",
            )
            assert out.get("ok") is True, out
            mets = (out.get("query_spec") or {}).get("metrics") or []
            assert "vs_ams" in mets
            # sort should be asc for lowest
            assert (out.get("query_spec") or {}).get("sort") == "asc"

            metro = _run(
                {
                    "row_dimensions": ["business_unit"],
                    "metrics": ["volume", "ams"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "filters": {"party": "metro"},
                },
                "metro sales in July",
            )
            assert metro.get("ok") is True, metro
            f = (metro.get("query_spec") or {}).get("filters") or {}
            assert f.get("client_type") == "METRO HABIB"
            assert not f.get("party")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_management_alpha_maan_exclude_and_include_check_style() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed_rich()
            # Alpha July includes Maan line (5 MT)
            profile = _run(
                {
                    "operation": "party_profile",
                    "filters": {"party": "Alpha Dist"},
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "row_dimensions": ["party"],
                    "metrics": ["volume", "vs_ams"],
                },
                "tell me about Alpha Dist July",
            )
            assert profile.get("volume_mt") == 30.0  # 25+5

            # Eva-only filter via business units
            eva_only = _run(
                {
                    "row_dimensions": ["product"],
                    "metrics": ["volume"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "filters": {"party": "Alpha Dist"},
                    "business_units": ["Eva Consumer", "Eva Bulk"],
                },
                "Alpha Dist July Eva only SKU wise",
            )
            assert eva_only.get("ok") is True, eva_only
            assert _party(eva_only) == "Alpha Dist"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_synonym_apply_does_not_steal_complete_sales_ask() -> None:
    """Bare 'sales' must not force weird metrics on a complete BU ask."""
    spec = apply_metric_synonyms_to_spec(
        {
            "row_dimensions": ["business_unit"],
            "column_dimensions": ["month"],
            "metrics": ["volume", "ams"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 6,
            "operation": "pivot",
        },
        "Show me Lahore sales last 6 months",
    )
    assert spec.get("metrics") == ["volume", "ams"]
    assert spec.get("operation") == "pivot"


def test_management_random_standup_questions() -> None:
    """Assorted executive one-liners that should resolve cleanly."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed_rich()

            # Days since last buy for Alpha
            days = _run(
                {
                    "operation": "pivot",
                    "row_dimensions": ["party"],
                    "metrics": ["volume"],
                    "period_type": "MTD",
                    "context_handling": "none",
                    "filters": {"party": "Alpha Dist"},
                },
                "when did they last buy?",
            )
            assert days.get("mode") == "party_profile"
            assert days.get("last_sale") == "2026-07-20"

            # Family follow-up price after al shaheer family profile
            fam = _run(
                {
                    "operation": "party_profile",
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "extracted_entities": ["al shaheer"],
                    "row_dimensions": ["party"],
                    "metrics": ["volume", "ams", "vs_ams"],
                },
                "give me the full picture for al shaheer in July",
            )
            assert fam.get("mode") == "party_profile"
            assert fam.get("volume_mt") == 82.0
            prior = prior_context_from_query_state(fam.get("query_state"))
            assert (prior.get("party_scope") or {}).get("party_ilike") or (
                prior.get("filters") or {}
            ).get("party_ilike")

            price = _run(
                {
                    "row_dimensions": ["product"],
                    "metrics": [],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                },
                "what's the average rate?",
                prior=prior,
            )
            assert price.get("ok") is True, price
            f = (price.get("query_spec") or {}).get("filters") or {}
            assert f.get("party_ilike") or f.get("parties") or f.get("party")
            assert "avg_price" in ((price.get("query_spec") or {}).get("metrics") or [])

            from eva_dashboard.party_match import party_matches_look_like_branches

            assert not party_matches_look_like_branches(
                "alpha", ["Alpha Dist", "Imtiaz Gulberg"]
            )

            # Who bought cooking oil jerry can in July
            cooking = _run(
                {
                    "row_dimensions": ["party"],
                    "metrics": ["volume"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "filters": {
                        "oil_type": "Eva Cooking",
                        "packing_category": "Jerry Can",
                    },
                },
                "who bought cooking oil jerry can in July",
            )
            assert cooking.get("ok") is True, cooking
            md = cooking.get("answer_markdown") or ""
            assert "Shaheer" in md or "Metro" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
