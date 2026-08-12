"""Main-customers follow-up: flat Volume + Avg Price; totals match oil total."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.db import connect, init_db
from eva_dashboard.query_executor import (
    _coerce_vocab_from_user_text,
    _extract_top_n,
    _looks_yoy_compare,
    execute_query_spec,
)
from eva_dashboard.sales_query import _limit_matrix_rows, _pivot_mt
from eva_dashboard.spoken_constraints import extract_exclude_phrases, resolve_exclude_map
import pandas as pd


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed_meal() -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, "
            "payload_json, updated_at) VALUES "
            "('SM1', 'Meal', 'Soya Meal', 'Meal', '{}', datetime('now'))"
        )
        conn.execute(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, "
            "payload_json, updated_at) VALUES "
            "('1', 'Cust A', 'Meal Clients', 'Lahore', 'Lahore', '', "
            "'{}', datetime('now'))"
        )
        for i, (dt, party, mt, rate) in enumerate(
            [
                ("2026-08-05", "Cust A", 1500, 150),
                ("2026-08-06", "Cust B", 500, 160),
                ("2026-08-07", None, 800, 155),  # blank party
                ("2026-08-12", "Cust A", 8, 150),
            ]
        ):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type,
                  payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, 'SM1', ?, 'MT', ?,
                  ?, ?, 'Meal Clients', '{}')
                """,
                (f"m-{i}", dt, party, mt, mt, rate, mt * rate),
            )
        conn.commit()


def test_top_n_and_yoy_year_pair_helpers():
    assert _extract_top_n("top 5 parties in march") == 5
    assert _extract_top_n("the 10 distributors with the highest share") == 10
    assert _looks_yoy_compare("compare distributor sales in July 2025 vs 2026")


def test_exclude_phrase_stops_before_show_me():
    q = "exclude al shaheer and show me Eva sales in central"
    assert extract_exclude_phrases(q) == ["al shaheer"]
    assert resolve_exclude_map(q) == {"party_like": ["al shaheer"]}


def test_null_party_kept_in_pivot_mt():
    df = pd.DataFrame(
        {
            "party": ["A", None, ""],
            "client_type": ["Meal Clients", "Meal Clients", "Bulk Debtors"],
            "mt": [100.0, 800.0, 50.0],
        }
    )
    mat = _pivot_mt(df, "party", "client_type")
    assert float(mat["grand_total_mt"]) == 950.0
    labels = {str(r.get("party")) for r in mat["rows"]}
    assert "(unmapped)" in labels


def test_limit_matrix_rows_top_n():
    mat = {
        "columns": ["Total"],
        "rows": [
            {"party": "A", "Total": 100},
            {"party": "B", "Total": 50},
            {"party": "C", "Total": 25},
            {"party": "Total", "Total": 175, "row_kind": "total"},
        ],
    }
    out = _limit_matrix_rows(mat, 2, row_key="party")
    body = [r for r in out["rows"] if r.get("row_kind") != "total"]
    assert len(body) == 2
    assert body[0]["party"] == "A"
    assert out["truncated"] is True


def test_main_customers_coerce_flat_volume_price():
    prior = {
        "filters": {"oil_type": "Soya Meal", "business_unit": "Meal"},
        "business_units": ["Meal"],
        "row_dimensions": ["business_unit"],
        "metrics": ["volume", "avg_price"],
        "period_type": "SPECIFIC_MONTH",
        "target_month": "2026-08",
    }
    out = _coerce_vocab_from_user_text(
        {
            "row_dimensions": ["party"],
            "column_dimensions": ["client_type"],
            "metrics": ["volume"],
            "period_type": "SPECIFIC_MONTH",
            "target_month": "2026-08",
            "filters": {},
            "state_action": "modify",
            "context_handling": "prior",
        },
        "who were the main customers",
        prior=prior,
    )
    assert out.get("row_dimensions") == ["party"]
    assert out.get("column_dimensions") == []
    assert out.get("metrics") == ["volume", "avg_price"]
    assert out.get("base") == "prior"
    assert (out.get("filters") or {}).get("oil_type") == "Soya Meal"


def test_main_customers_total_matches_meal_volume():
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        _seed_meal()
        prior = {
            "filters": {"oil_type": "Soya Meal", "business_unit": "Meal"},
            "business_units": ["Meal"],
            "row_dimensions": ["business_unit"],
            "metrics": ["volume", "avg_price"],
            "period_type": "SPECIFIC_MONTH",
            "target_month": "2026-08",
        }
        out = execute_query_spec(
            {
                "row_dimensions": ["party"],
                "column_dimensions": ["client_type"],
                "metrics": ["volume"],
                "period_type": "SPECIFIC_MONTH",
                "target_month": "2026-08",
                "filters": {},
                "state_action": "modify",
                "context_handling": "prior",
                "base": "prior",
                "clear_filters": [],
            },
            prior=prior,
            user_text="who were the main customers",
        )
        assert out.get("ok"), out.get("plan_errors") or out.get("error")
        qs = out.get("query_spec") or {}
        assert qs.get("column_dimensions") == []
        assert qs.get("metrics") == ["volume", "avg_price"]
        mat = out.get("matrix") or {}
        assert mat.get("columns") == ["Volume (MT)", "Avg Rate"]
        assert mat.get("grand_total_mt") == 2808
        md = out.get("answer_markdown") or ""
        assert "Volume (MT)" in md
        assert "Avg Rate" in md
        assert "Meal Clients" not in md  # no client_type crosstab
        assert "(unmapped)" in md  # blank-party volume kept


def test_july_2025_vs_2026_sets_yoy():
    out = _coerce_vocab_from_user_text(
        {
            "row_dimensions": ["client_type"],
            "column_dimensions": ["month"],
            "metrics": ["volume", "ams"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 13,
            "filters": {},
            "state_action": "clear",
        },
        "compare distributor sales in July 2025 vs 2026",
    )
    assert out.get("compare") == "yoy"
    assert out.get("target_month") == "2026-07"
    assert out.get("period_type") == "SPECIFIC_MONTH"
    assert (out.get("filters") or {}).get("client_type") == "Eva Distributors"
    assert out.get("row_dimensions") == ["business_unit"]


def test_north_ams_threshold_and_vtf_share_coerce():
    north = _coerce_vocab_from_user_text(
        {
            "row_dimensions": ["business_unit"],
            "metrics": ["volume"],
            "filters": {},
        },
        "sales by distributor in North but only distributors with AMS > 20",
    )
    assert (north.get("filters") or {}).get("zone") == "NORTH"
    assert (north.get("filters") or {}).get("client_type") == "Eva Distributors"
    assert north.get("intent") == "party_rank"
    assert north.get("metric") == "ams"
    assert any(
        f.get("metric") == "ams" and f.get("value") == 20.0
        for f in (north.get("metric_filters") or [])
    )

    vtf = _coerce_vocab_from_user_text(
        {
            "row_dimensions": ["party"],
            "metrics": ["volume"],
            "filters": {},
        },
        "the 10 distributors with the highest share of their sales being VTF",
    )
    assert vtf.get("metric") == "segment_mix"
    assert vtf.get("limit") == 10
    assert (vtf.get("filters") or {}).get("oil_type") == "Eva VTF"


def test_exclude_fresh_keeps_bu_grain():
    out = _coerce_vocab_from_user_text(
        {
            "row_dimensions": ["party"],
            "column_dimensions": ["month"],
            "metrics": ["volume"],
            "filters": {},
        },
        "exclude al shaheer and show me Eva sales in central",
    )
    assert out.get("row_dimensions") == ["business_unit"]
    assert out.get("column_dimensions") == ["month"]
    assert (out.get("excludes") or {}).get("party_like") == ["al shaheer"]
    assert "Eva Consumer" in (out.get("business_units") or [])
