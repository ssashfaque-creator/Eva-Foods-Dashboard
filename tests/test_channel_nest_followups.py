"""Channel nesting, city layers, excludes, YoY — management follow-ups."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    extract_regroup_dimension,
    resolve_regroup_request,
    resolve_remove_request,
)
from eva_dashboard.client_type_map import map_client_type
from eva_dashboard.db import connect, init_db
from eva_dashboard.query_executor import (
    _coerce_vocab_from_user_text,
    execute_query_spec,
)
from eva_dashboard.query_spec import prior_context_from_query_state


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
                ("P2", "Eva Bulk", "Eva VTF Bulk", "Tin"),
                ("P3", "Cusine King", "Cusine King", "Tin"),
            ],
        )
        for cid, name, ctype, city in (
            ("1", "Metro A", "METRO HABIB", "Lahore"),
            ("2", "Chase A", "CHASE UP", "Lahore"),
            ("3", "North LMT A", "NORTH LMT", "Lahore"),
            ("4", "Dist Lahore", "Eva Distributors", "Lahore"),
            ("5", "Dist Karachi", "Eva Distributors", "Karachi"),
            ("6", "Cosine King Traders", "Eva Distributors", "Lahore"),
        ):
            conn.execute(
                "INSERT OR REPLACE INTO clients "
                "(client_id, client, type, city_filter, city, inactive, "
                "payload_json, updated_at) VALUES "
                "(?, ?, ?, ?, ?, '', '{}', datetime('now'))",
                (cid, name, ctype, city, city),
            )
        rows = [
            ("2025-07-05", "Dist Lahore", "P1", 10, "Eva Distributors"),
            ("2026-05-05", "Dist Lahore", "P1", 20, "Eva Distributors"),
            ("2026-06-05", "Dist Lahore", "P1", 22, "Eva Distributors"),
            ("2026-07-05", "Dist Lahore", "P1", 30, "Eva Distributors"),
            ("2026-07-06", "Dist Lahore", "P2", 8, "Eva Distributors"),
            ("2026-07-07", "Dist Lahore", "P3", 5, "Eva Distributors"),
            ("2026-07-08", "Dist Karachi", "P1", 15, "Eva Distributors"),
            ("2026-07-09", "Metro A", "P1", 12, "METRO HABIB"),
            ("2026-07-10", "Chase A", "P1", 9, "CHASE UP"),
            ("2026-07-11", "North LMT A", "P1", 7, "NORTH LMT"),
            ("2026-07-12", "Cosine King Traders", "P1", 4, "Eva Distributors"),
        ]
        for i, (dt, party, prod, mt, ct) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, 100, ?, ?, '{}')
                """,
                (f"cn-{i}", dt, party, prod, mt, mt, mt * 100, ct),
            )
        conn.commit()


def test_ops_mapping_sheet_semantics() -> None:
    assert map_client_type("METRO HABIB") == "IMT"
    assert map_client_type("Eva Distributors") == "Direct Customers"
    assert map_client_type("NORTH LMT") == "LMT"


def test_regroup_bu_then_city_nests() -> None:
    assert extract_regroup_dimension("add cities") == "city"
    assert extract_regroup_dimension("show this by city") == "city"
    prior = {
        "row_dimension": "business_unit",
        "column_dimension": "month",
        "filters": {"client_type": "Eva Distributors"},
        "months_back": 6,
    }
    out = resolve_regroup_request("show by city", prior_spec=prior)
    assert out is not None
    assert out["row_dimension"] == "business_unit"
    assert out["row_groups"] == ["city"]
    # clear city only when a sticky city filter existed (none here)


def test_regroup_bu_then_channel_nests() -> None:
    assert extract_regroup_dimension("show this by channel") == "client_type"
    prior = {
        "row_dimension": "business_unit",
        "column_dimension": "month",
        "filters": {"city": "Lahore"},
        "months_back": 6,
    }
    out = resolve_regroup_request("can you show this by channel", prior_spec=prior)
    assert out is not None
    assert out["row_dimension"] == "business_unit"
    assert out["row_groups"] == ["client_type"]


def test_coerce_nest_city_under_bu() -> None:
    prior = {
        "row_dimensions": ["business_unit"],
        "column_dimensions": ["month"],
        "filters": {"client_type": "Eva Distributors"},
    }
    fixed = _coerce_vocab_from_user_text(
        {
            "row_dimensions": ["business_unit"],
            "column_dimensions": ["month"],
            "metrics": ["volume", "ams"],
            "filters": {},
        },
        "add cities",
        prior=prior,
    )
    assert fixed["row_dimensions"] == ["city", "business_unit"]


def test_coerce_sales_by_channel() -> None:
    fixed = _coerce_vocab_from_user_text(
        {
            "row_dimensions": ["business_unit"],
            "column_dimensions": ["month"],
            "metrics": ["volume"],
            "filters": {"client_type": "METRO HABIB"},
        },
        "show sales by channel",
    )
    assert fixed["row_dimensions"][0] == "client_type"
    assert "client_type" in (fixed.get("clear_filters") or fixed.get("clear") or [])


def test_execute_channel_wise_uses_groups() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "row_dimensions": ["client_type"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "filters": {},
                },
                user_text="show sales by channel for July",
            )
            assert out.get("ok"), out.get("error")
            labels = {
                str(r.get("client_type") or r.get("label") or "")
                for r in ((out.get("matrix") or {}).get("rows") or [])
                if not r.get("is_total")
            }
            assert "IMT" in labels
            assert "LMT" in labels
            assert "Direct Customers" in labels
            assert "METRO HABIB" not in labels
            assert "Eva Distributors" not in labels
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_execute_metro_filter_is_raw_not_whole_imt() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "filters": {"client_type": "metro"},
                },
                user_text="metro sales in July",
            )
            assert out.get("ok"), out.get("error")
            f = (out.get("query_spec") or {}).get("filters") or {}
            assert f.get("client_type") == "METRO HABIB"
            # Chase (also IMT) must not be included — only Metro Habib parties
            md = (out.get("answer_markdown") or "").lower()
            # Volume should be Metro only (12)
            matrix = out.get("matrix") or {}
            total = 0.0
            for row in matrix.get("rows") or []:
                if row.get("is_total") or str(row.get("business_unit") or "").lower() in {
                    "total",
                    "grand total",
                }:
                    for k, v in row.items():
                        if isinstance(v, (int, float)) and k.lower() in {
                            "total",
                            "2026-07",
                            "jul 2026",
                        }:
                            total = max(total, float(v))
            assert total == 12.0 or "12" in (out.get("answer_markdown") or "")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_execute_add_city_under_bu() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            base = execute_query_spec(
                {
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "filters": {"client_type": "Eva Distributors"},
                },
                user_text="distributor sales in July by business unit",
            )
            assert base.get("ok"), base.get("error")
            prior = prior_context_from_query_state(base.get("query_state")) or {
                "row_dimensions": ["business_unit"],
                "column_dimensions": ["month"],
                "filters": {"client_type": "Eva Distributors"},
            }
            nested = execute_query_spec(
                {
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "prior",
                    "clear_filters": ["city"],
                    "filters": {},
                },
                prior=prior,
                user_text="add cities",
            )
            assert nested.get("ok"), nested.get("error")
            spec = nested.get("query_spec") or {}
            assert spec.get("row_dimensions") == ["city", "business_unit"]
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_remove_cosine_king_bu_and_party() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            prior_spec = {
                "row_dimension": "business_unit",
                "column_dimension": "month",
                "filters": {"city": "Lahore"},
                "business_units": [],
            }
            # BU typo → Cusine King
            rem = resolve_remove_request(
                "remove cosine king", prior_spec=prior_spec
            )
            assert rem is not None
            assert rem["mode"] == "exclude_value"
            assert "Cusine King" in (rem.get("excludes") or {}).get(
                "business_unit", []
            ) or "Cosine King Traders" in (rem.get("excludes") or {}).get(
                "party", []
            )

            base = execute_query_spec(
                {
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "filters": {"city": "Lahore"},
                },
                user_text="Lahore sales July",
            )
            prior = prior_context_from_query_state(base.get("query_state"))
            out = execute_query_spec(
                {
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "prior",
                    "clear_filters": [],
                    "filters": {},
                },
                prior=prior,
                user_text="remove cosine king",
            )
            assert out.get("ok"), out.get("error")
            ex = (out.get("query_spec") or {}).get("excludes") or {}
            assert ex.get("business_unit") or ex.get("party")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_compare_with_karachi_and_yoy_coerce() -> None:
    prior = {
        "row_dimensions": ["business_unit"],
        "column_dimensions": ["month"],
        "filters": {"city": "Lahore", "client_type": "Eva Distributors"},
    }
    fixed = _coerce_vocab_from_user_text(
        {
            "row_dimensions": ["business_unit"],
            "metrics": ["volume", "ams"],
            "column_dimensions": ["month"],
            "filters": {},
        },
        "compare with Karachi",
        prior=prior,
    )
    assert fixed["filters"].get("cities") == ["Lahore", "Karachi"]
    assert fixed["row_dimensions"][0] == "city"

    yoy = _coerce_vocab_from_user_text(
        {
            "row_dimensions": ["business_unit"],
            "metrics": ["volume"],
            "filters": {},
        },
        "compare with same period last year",
        prior=prior,
    )
    assert yoy.get("compare") == "yoy"

    drivers = _coerce_vocab_from_user_text(
        {
            "row_dimensions": ["business_unit"],
            "metrics": ["volume"],
            "filters": {},
        },
        "which products led the growth",
        prior=prior,
    )
    assert drivers.get("compare") == "yoy"
    assert drivers.get("row_dimensions") == ["packing_category"]


def test_zero_volume_rows_omitted() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = execute_query_spec(
                {
                    "row_dimensions": ["party"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-07",
                    "context_handling": "none",
                    "filters": {"client_type": "Eva Distributors", "city": "Lahore"},
                },
                user_text="distributor parties in Lahore July",
            )
            assert out.get("ok"), out.get("error")
            for row in (out.get("matrix") or {}).get("rows") or []:
                if row.get("is_total"):
                    continue
                nums = [
                    v
                    for k, v in row.items()
                    if isinstance(v, (int, float))
                    and k.lower() not in {"is_total"}
                ]
                if nums:
                    assert any(v != 0 for v in nums), row
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
