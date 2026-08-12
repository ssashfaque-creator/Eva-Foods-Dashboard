"""Multi-city / multi-channel filters + nested follow-ups — general safety nets."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    extract_regroup_dimension,
    resolve_regroup_request,
)
from eva_dashboard.client_language import extract_all_client_types_from_text
from eva_dashboard.db import connect, init_db
from eva_dashboard.party_analytics import extract_cities_from_text
from eva_dashboard.query_executor import (
    _coerce_vocab_from_user_text,
    execute_query_spec,
)


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
                ("Eva Cooking Oil (StandUpPouch)", "Eva Consumer", "Eva Cooking", "Stand up"),
                ("Eva Cooking Oil (16 Ltr Tin)", "Eva Bulk", "Eva Bulk", "Tin"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "Imtiaz Lahore", "Imtiaz Store", "Lahore", "Lahore"),
                ("2", "Imtiaz Karachi", "Imtiaz Store", "Karachi", "Karachi"),
                ("3", "Imtiaz Islamabad", "Imtiaz Store", "Islamabad", "Islamabad"),
                ("4", "Alpha Dist Lahore", "Eva Distributors", "Lahore", "Lahore"),
                ("5", "Beta Dist Karachi", "Eva Distributors", "Karachi", "Karachi"),
                ("6", "Gamma Dist Multan", "Eva Distributors", "Multan", "Multan"),
            ],
        )
        rows = [
            ("2026-05-10", "Imtiaz Lahore", "Eva Canola Oil (StandUpPouch)", 20.0, "Imtiaz Store"),
            ("2026-06-10", "Imtiaz Lahore", "Eva Canola Oil (StandUpPouch)", 22.0, "Imtiaz Store"),
            ("2026-07-05", "Imtiaz Karachi", "Eva Cooking Oil (StandUpPouch)", 40.0, "Imtiaz Store"),
            ("2026-07-06", "Imtiaz Islamabad", "Eva Cooking Oil (16 Ltr Tin)", 8.0, "Imtiaz Store"),
            ("2026-08-01", "Imtiaz Lahore", "Eva Canola Oil (StandUpPouch)", 10.0, "Imtiaz Store"),
            ("2026-08-02", "Imtiaz Karachi", "Eva Cooking Oil (StandUpPouch)", 12.0, "Imtiaz Store"),
            ("2026-07-01", "Alpha Dist Lahore", "Eva Canola Oil (StandUpPouch)", 30.0, "Eva Distributors"),
            ("2026-08-01", "Alpha Dist Lahore", "Eva Canola Oil (StandUpPouch)", 32.0, "Eva Distributors"),
            ("2026-07-01", "Beta Dist Karachi", "Eva Cooking Oil (StandUpPouch)", 15.0, "Eva Distributors"),
            ("2026-07-01", "Gamma Dist Multan", "Eva Cooking Oil (16 Ltr Tin)", 50.0, "Eva Distributors"),
        ]
        for i, (dt, party, product, mt, ct) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type,
                  payload_json
                ) VALUES (
                  NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, 500, 10000,
                  ?, '{}'
                )
                """,
                (f"mf-{i}-{dt}", dt, party, product, mt, mt, ct),
            )
        conn.commit()


def test_extract_cities_vs_and_lists() -> None:
    assert extract_cities_from_text("Imtiaz sales in lahore vs karachi") == [
        "Lahore",
        "Karachi",
    ]
    assert extract_cities_from_text("Imtiaz sales in lahore and karachi") == [
        "Lahore",
        "Karachi",
    ]
    assert extract_cities_from_text(
        "compare growth in Islamabad, Lahore and Karachi"
    ) == ["Islamabad", "Lahore", "Karachi"]


def test_extract_channels_compare_phrases() -> None:
    assert extract_all_client_types_from_text(
        "compare distributor sales and Imtiaz sales in lahore"
    ) == ["Eva Distributors", "Imtiaz Store"]
    assert extract_all_client_types_from_text(
        "compare Imtiaz vs distributors in Lahore"
    ) == ["Imtiaz Store", "Eva Distributors"]


def test_coerce_multi_city_vs_and() -> None:
    for text in (
        "Imtiaz sales in lahore vs karachi",
        "Imtiaz sales in lahore and karachi",
    ):
        bad = {
            "row_dimensions": ["business_unit"],
            "column_dimensions": ["month"],
            "metrics": ["volume", "ams"],
            "filters": {"client_type": "Imtiaz Store", "city": "Lahore"},
        }
        fixed = _coerce_vocab_from_user_text(bad, text)
        assert fixed["filters"].get("cities") == ["Lahore", "Karachi"]
        assert "city" not in fixed["filters"] or fixed["filters"].get("city") is None
        assert fixed["row_dimensions"][0] == "city"
        assert fixed["filters"].get("client_type") == "Imtiaz Store"


def test_coerce_channel_compare_in_city() -> None:
    bad = {
        "row_dimensions": ["party"],
        "column_dimensions": ["month"],
        "metrics": ["volume"],
        "filters": {"client_type": "Eva Distributors", "city": "Lahore"},
    }
    fixed = _coerce_vocab_from_user_text(
        bad, "compare distributor sales and Imtiaz sales in lahore"
    )
    assert fixed["row_dimensions"] == ["client_type"]
    assert set(fixed["filters"].get("client_types") or []) == {
        "Eva Distributors",
        "Imtiaz Store",
    }
    assert fixed["filters"].get("city") == "Lahore"


def test_coerce_product_wise_nests_under_prior_city() -> None:
    prior = {
        "row_dimensions": ["city"],
        "column_dimensions": ["month"],
        "filters": {
            "client_type": "Imtiaz Store",
            "cities": ["Lahore", "Karachi"],
        },
    }
    bad = {
        "row_dimensions": ["packing_category"],
        "column_dimensions": ["month"],
        "metrics": ["volume"],
        "filters": {},
        "context_handling": "prior",
    }
    fixed = _coerce_vocab_from_user_text(
        bad, "show this product wise", prior=prior
    )
    assert fixed["row_dimensions"] == ["city", "packing_category"]
    assert fixed["filters"].get("cities") == ["Lahore", "Karachi"]
    assert fixed["filters"].get("client_type") == "Imtiaz Store"


def test_regroup_product_wise_nests_under_city() -> None:
    assert extract_regroup_dimension("show this product wise") == "packing_category"
    prior = {
        "row_dimension": "city",
        "column_dimension": "month",
        "filters": {
            "client_type": "Imtiaz Store",
            "cities": ["Lahore", "Karachi"],
        },
        "months_back": 6,
    }
    out = resolve_regroup_request("show this product wise", prior_spec=prior)
    assert out is not None
    assert out["row_dimension"] == "packing_category"
    assert out["row_groups"] == ["city"]
    assert "city" not in (out.get("clear_filters") or [])
    assert "cities" not in (out.get("clear_filters") or [])


def test_regroup_sku_under_zone() -> None:
    prior = {
        "row_dimension": "zone",
        "column_dimension": "month",
        "filters": {"client_type": "Imtiaz Store"},
    }
    out = resolve_regroup_request("sku wise", prior_spec=prior)
    assert out is not None
    assert out["row_dimension"] == "product"
    assert out["row_groups"] == ["zone"]


def test_execute_multi_city_imtiaz_only_named_cities() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            result = execute_query_spec(
                {
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "filters": {"client_type": "Imtiaz Store", "city": "Lahore"},
                },
                user_text="Imtiaz sales in lahore and karachi",
            )
            assert result.get("ok"), result.get("error")
            spec = result.get("query_spec") or {}
            assert (spec.get("filters") or {}).get("cities") == ["Lahore", "Karachi"]
            assert spec.get("row_dimensions") == ["city"]
            matrix = result.get("matrix") or {}
            rows = [
                str(
                    r.get("city")
                    or r.get("label")
                    or r.get("row")
                    or ""
                )
                for r in (matrix.get("rows") or [])
                if str(r.get("row_kind") or "") != "total"
            ]
            # Must not include Islamabad when only Lahore+Karachi named
            joined = " | ".join(rows).lower()
            assert "lahore" in joined
            assert "karachi" in joined
            assert "islamabad" not in joined
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_execute_channel_compare_lahore() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            result = execute_query_spec(
                {
                    "row_dimensions": ["party"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "filters": {"client_type": "Eva Distributors", "city": "Lahore"},
                },
                user_text="compare distributor sales and Imtiaz sales in lahore",
            )
            assert result.get("ok"), result.get("error")
            spec = result.get("query_spec") or {}
            assert spec.get("row_dimensions") == ["client_type"]
            cts = set((spec.get("filters") or {}).get("client_types") or [])
            assert cts == {"Eva Distributors", "Imtiaz Store"}
            matrix = result.get("matrix") or {}
            labels = [
                str(
                    r.get("client_type")
                    or r.get("label")
                    or r.get("row")
                    or ""
                ).lower()
                for r in (matrix.get("rows") or [])
                if str(r.get("row_kind") or "") != "total"
            ]
            blob = " ".join(labels)
            assert "imtiaz" in blob
            # Eva Distributors stays its own reporting channel
            assert "distributor" in blob
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_execute_product_wise_keeps_city_filter_layer() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            prior = {
                "row_dimensions": ["city"],
                "column_dimensions": ["month"],
                "filters": {
                    "client_type": "Imtiaz Store",
                    "cities": ["Lahore", "Karachi"],
                },
                "row_dimension": "city",
                "months_back": 6,
            }
            result = execute_query_spec(
                {
                    "row_dimensions": ["packing_category"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "filters": {},
                    "context_handling": "prior",
                    "clear_filters": [],
                },
                prior=prior,
                user_text="show this product wise",
            )
            assert result.get("ok"), result.get("error")
            spec = result.get("query_spec") or {}
            assert spec.get("row_dimensions") == ["city", "packing_category"]
            assert (spec.get("filters") or {}).get("cities") == ["Lahore", "Karachi"]
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
