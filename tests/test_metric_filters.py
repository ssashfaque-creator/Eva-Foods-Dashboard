"""Spoken metric thresholds (AMS > 10, growth > x, …)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.db import connect, init_db
from eva_dashboard.metric_filters import (
    apply_metric_filters,
    parse_metric_filters,
)
from eva_dashboard.query_executor import execute_query_spec
from eva_dashboard.spoken_constraints import apply_spoken_constraints


def test_parse_ams_more_than() -> None:
    got = parse_metric_filters(
        "only show Eva consumer and only show customers with ams more than 10"
    )
    assert got == [{"metric": "ams", "op": "gt", "value": 10.0}]


def test_parse_exclude_less_than_ams_inverts() -> None:
    assert parse_metric_filters("exclude all customers with less than 10 ams") == [
        {"metric": "ams", "op": "gte", "value": 10.0}
    ]
    assert parse_metric_filters("more than 10 ams") == [
        {"metric": "ams", "op": "gt", "value": 10.0}
    ]


def test_parse_growth_and_volume() -> None:
    assert parse_metric_filters("growth more than 30%") == [
        {"metric": "ams_growth", "op": "gt", "value": 30.0}
    ]
    assert parse_metric_filters("volume at least 5") == [
        {"metric": "volume", "op": "gte", "value": 5.0}
    ]
    assert parse_metric_filters("dropped more than 20%") == [
        {"metric": "ams_growth", "op": "lt", "value": -20.0}
    ]


def test_apply_metric_filters_rows() -> None:
    rows = [
        {"party": "A", "ams_mt": 1.0},
        {"party": "B", "ams_mt": 11.0},
        {"party": "C", "ams_mt": 10.0},
    ]
    kept = apply_metric_filters(rows, [{"metric": "ams", "op": "gt", "value": 10}])
    assert [r["party"] for r in kept] == ["B"]


def test_spoken_constraints_merge_metric_filters() -> None:
    out = apply_spoken_constraints(
        {"metrics": ["ams_growth"], "row_dimensions": ["party"]},
        user_text="only show customers with ams more than 10",
    )
    assert out.get("metric_filters") == [
        {"metric": "ams", "op": "gt", "value": 10.0}
    ]


def test_execute_ams_threshold_filters_parties() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")
        try:
            import eva_dashboard.sales_query as sq

            sq._CLIENTS_CACHE = None
            init_db()
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO category "
                    "(product, category_1, category_2, packing_category, "
                    "payload_json, updated_at) VALUES "
                    "('Prod A', 'Eva Consumer', 'Eva Canola', 'Stand up', "
                    "'{}', datetime('now'))"
                )
                for cid, party, city in (
                    ("1", "Big Buyer", "Multan"),
                    ("2", "Small Buyer", "Multan"),
                ):
                    conn.execute(
                        "INSERT OR REPLACE INTO clients "
                        "(client_id, client, type, city_filter, city, inactive, "
                        "payload_json, updated_at) VALUES "
                        "(?, ?, 'Direct Customers', ?, ?, '', '{}', "
                        "datetime('now'))",
                        (cid, party, city, city),
                    )
                # AMS current window for last-6-months ending ~Aug 2026 is
                # May-Jul 2026. Seed Big Buyer with ~15 MT/month, Small with ~2.
                n = 0
                for party, monthly in (("Big Buyer", 15.0), ("Small Buyer", 2.0)):
                    for dt in (
                        "2026-02-15",
                        "2026-03-15",
                        "2026-04-15",
                        "2026-05-15",
                        "2026-06-15",
                        "2026-07-15",
                        "2026-08-05",
                    ):
                        n += 1
                        conn.execute(
                            """
                            INSERT INTO sales (
                              source_file_id, row_hash, imported_at, date, party,
                              product, qty, unit, mt_qty, rate,
                              incl_gst_fed_amount, client_type, payload_json
                            ) VALUES (NULL, ?, datetime('now'), ?, ?, 'Prod A',
                              ?, 'MT', ?, 100, ?, 'Direct Customers', '{}')
                            """,
                            (f"mf-{n}", dt, party, monthly, monthly, monthly * 100),
                        )
                conn.commit()

            out = execute_query_spec(
                {
                    "operation": "pivot",
                    "row_dimensions": ["party"],
                    "metrics": ["ams_growth"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "sort": "desc",
                    "context_handling": "none",
                    "filters": {
                        "city": "Multan",
                        "business_unit": "Eva Consumer",
                    },
                    "limit": 25,
                },
                user_text=(
                    "only show Eva consumer and only show customers with "
                    "ams more than 10"
                ),
            )
            assert out.get("ok"), out.get("error")
            md = out.get("answer_markdown") or ""
            assert "ams > 10" in md.lower() or "ams > 10" in str(
                out.get("query_spec")
            )
            # Small Buyer (AMS ~2) must not appear; Big Buyer must
            assert "Small Buyer" not in md
            assert "Big Buyer" in md
            rows = out.get("rows") or []
            if rows:
                assert all(float(r.get("ams_mt") or 0) > 10 for r in rows)
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
