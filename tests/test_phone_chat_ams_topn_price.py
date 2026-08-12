"""Phone-chat regressions: AMS exclude, top-N trend, priced volume, price fetch."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.db import connect, init_db
from eva_dashboard.metric_filters import (
    looks_like_metric_threshold_phrase,
    parse_metric_filters,
)
from eva_dashboard.metrics_catalog import apply_metric_synonyms_to_spec
from eva_dashboard.query_executor import (
    _coerce_vocab_from_user_text,
    execute_query_spec,
)
from eva_dashboard.sales_query import _trend_table, query_sales
from eva_dashboard.spoken_constraints import apply_spoken_constraints, extract_exclude_phrases
from eva_dashboard.universal_pivot import _fetch_priced_lines, execute_universal_pivot
import pandas as pd


def test_exclude_less_than_10_ams_is_metric_not_party() -> None:
    q = "exclude all customers with less than 10 ams"
    assert parse_metric_filters(q) == [{"metric": "ams", "op": "gte", "value": 10.0}]
    assert extract_exclude_phrases(q) == []
    assert looks_like_metric_threshold_phrase("all customers with less than 10 ams")
    out = apply_spoken_constraints(
        {
            "excludes": {"party_like": ["all customers with less than 10 ams"]},
            "metrics": ["volume", "ams"],
            "row_dimensions": ["party"],
        },
        user_text=q,
    )
    assert not (out.get("excludes") or {}).get("party_like")
    assert out.get("metric_filters") == [{"metric": "ams", "op": "gte", "value": 10.0}]


def test_price_fetch_spoken_beats_sticky_avg_price() -> None:
    spec = apply_metric_synonyms_to_spec(
        {
            "metrics": ["avg_price"],
            "column_dimensions": ["month"],
            "row_dimensions": ["client_type"],
            "filters": {"client_type": "Eva Distributors"},
        },
        "what's the price fetch",
    )
    assert "price_fetch" in (spec.get("metrics") or [])
    assert "avg_price" not in (spec.get("metrics") or [])
    assert (spec.get("price_flags") or {}).get("include_price_fetch") is True

    coerced = _coerce_vocab_from_user_text(
        {
            "metrics": ["avg_price"],
            "column_dimensions": ["month"],
            "row_dimensions": ["client_type"],
        },
        "what's the price fetch",
    )
    assert "price_fetch" in (coerced.get("metrics") or [])


def test_trend_table_applies_limit_and_drops_zero_volume() -> None:
    frame = pd.DataFrame(
        {
            "party": ["A", "B", "C", "D", "E", "F"],
            "mt": [50.0, 40.0, 30.0, 20.0, 10.0, 5.0],
        }
    )
    # Fake AMS-only party G with no volume by enriching via empty — we only
    # pass volume keys through period_frame, so zero-volume drop is implicit.
    period = {
        "date_from": "2026-03-01",
        "date_to": "2026-03-31",
        "partial_month": False,
        "days_elapsed": 31,
        "days_in_month": 31,
    }
    # Monkeypatch AMS/prior via empty DB — use TemporaryDirectory
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")
        try:
            import eva_dashboard.sales_query as sq

            sq._CLIENTS_CACHE = None
            init_db()
            trend = _trend_table(
                frame,
                row_dim="party",
                period=period,
                city=None,
                business_unit=None,
                oil_type=None,
                packing_category=None,
                limit=5,
                metric_filters=[{"metric": "volume", "op": "gte", "value": 10}],
            )
            body = [
                r
                for r in (trend.get("rows") or [])
                if str(r.get("party") or "").lower() != "total"
            ]
            assert len(body) == 5
            assert all(float(r["volume_mt"]) >= 10 for r in body)
            assert {r["party"] for r in body} == {"A", "B", "C", "D", "E"}
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_priced_lines_do_not_inflate_mt() -> None:
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
                    "('Oil1', 'Eva Consumer', 'Eva Canola', 'Stand up', "
                    "'{}', datetime('now'))"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO clients "
                    "(client_id, client, type, city_filter, city, inactive, "
                    "payload_json, updated_at) VALUES "
                    "('1', 'PEPSI CO', 'Direct Customers', 'Lahore', 'Lahore', "
                    "'', '{}', datetime('now'))"
                )
                # Two sales lines same date/party/product — duplicate rate rows
                # used to multiply MT on merge.
                for i, mt in enumerate((100.0, 50.0)):
                    conn.execute(
                        """
                        INSERT INTO sales (
                          source_file_id, row_hash, imported_at, date, party,
                          product, qty, unit, mt_qty, rate, incl_gst_fed_amount,
                          client_type, payload_json
                        ) VALUES (NULL, ?, datetime('now'), '2026-03-15',
                          'PEPSI CO', 'Oil1', ?, 'MT', ?, 500, ?,
                          'Direct Customers', '{}')
                        """,
                        (f"p-{i}", mt, mt, mt * 500),
                    )
                conn.commit()

            frame = _fetch_priced_lines(
                date_from="2026-03-01",
                date_to="2026-03-31",
                party="PEPSI CO",
            )
            assert abs(float(frame["mt"].sum()) - 150.0) < 0.01

            out = execute_universal_pivot(
                row_dimensions=["party"],
                column_dimensions=["month"],
                metrics=["volume", "avg_price"],
                months_back=6,
                party="PEPSI CO",
            )
            assert out.get("ok"), out.get("error")
            md = out.get("answer_markdown") or ""
            assert "150" in md or "150.0" in md
            # Must not show inflated ~300 from a doubling merge
            assert "300" not in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_execute_exclude_ams_routes_to_metric_filter() -> None:
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
                for cid, party in (("1", "Big Buyer"), ("2", "Tiny Buyer")):
                    conn.execute(
                        "INSERT OR REPLACE INTO clients "
                        "(client_id, client, type, city_filter, city, inactive, "
                        "payload_json, updated_at) VALUES "
                        "(?, ?, 'Direct Customers', 'Lahore', 'Lahore', '', "
                        "'{}', datetime('now'))",
                        (cid, party),
                    )
                n = 0
                for party, monthly in (("Big Buyer", 20.0), ("Tiny Buyer", 2.0)):
                    for dt in (
                        "2026-01-15",
                        "2026-02-15",
                        "2026-03-15",
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
                            (f"x-{n}", dt, party, monthly, monthly, monthly * 100),
                        )
                conn.commit()

            out = execute_query_spec(
                {
                    "operation": "pivot",
                    "row_dimensions": ["party"],
                    "metrics": ["volume", "ams", "vs_ams"],
                    "period_type": "SPECIFIC_MONTH",
                    "target_month": "2026-03",
                    "sort": "desc",
                    "limit": 5,
                    "context_handling": "none",
                    "filters": {"city": "Lahore", "business_unit": "Eva Consumer"},
                    # LLM bug: treated AMS cut as party_like exclude
                    "excludes": {
                        "party_like": ["all customers with less than 10 ams"]
                    },
                },
                user_text=(
                    "who are the top 5 customers in march — "
                    "exclude all customers with less than 10 ams"
                ),
            )
            assert out.get("ok"), out.get("error")
            md = (out.get("answer_markdown") or "").lower()
            assert "all customers with less than 10 ams" not in md
            assert "tiny buyer" not in md
            assert "big buyer" in md
            qs = out.get("query_spec") or {}
            mfs = qs.get("metric_filters") or out.get("metric_filters") or []
            assert any(
                str(f.get("metric")) == "ams" and float(f.get("value") or 0) == 10
                for f in mfs
            ) or "ams" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
