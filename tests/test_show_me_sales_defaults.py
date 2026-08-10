"""Defaults for 'show me X sales' → months + AMS; regroup follow-ups."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    _dispatch_tool,
    resolve_forced_tool,
    suggest_preferred_tool,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.sales_query import AMS_3_COL, AMS_6_COL


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
                ("Eva VTF Banaspati 16 Kg Tin", "Eva Bulk", "Eva VTF Bulk", "16 ltr / 16 Kg"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "Alpha Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("2", "Beta Store", "Imtiaz Store", "Karachi", "Karachi"),
                ("3", "Gamma Dist", "Eva Distributors", "Karachi", "Karachi"),
                ("4", "Rubina Shaheen (LHR)", "Eva Distributors", "Lahore", "Lahore"),
            ],
        )
        rows = []
        for m, mt in [("03", 10), ("04", 12), ("05", 30), ("06", 30), ("07", 40), ("08", 8)]:
            rows += [
                (f"2026-{m}-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", float(mt), "Eva Distributors"),
                (f"2026-{m}-06", "Beta Store", "Eva Canola Oil (StandUpPouch)", float(mt) * 0.5, "Imtiaz Store"),
                (f"2026-{m}-07", "Gamma Dist", "Eva Canola Oil (StandUpPouch)", float(mt) * 0.3, "Eva Distributors"),
                (f"2026-{m}-08", "Rubina Shaheen (LHR)", "Eva Canola Oil (StandUpPouch)", float(mt) * 0.2, "Eva Distributors"),
            ]
            if m in ("05", "06", "07"):
                rows.append(
                    (f"2026-{m}-09", "Alpha Dist", "Eva VTF Banaspati 16 Kg Tin", 12.0, "Eva Distributors")
                )
        for i, (dt, party, prod, mt, ct) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, '{}')
                """,
                (f"sms-{i}", dt, party, prod, mt, mt, ct),
            )
        conn.commit()


def _assert_month_ams(out: dict) -> None:
    assert out.get("ok") is True
    assert out.get("column_dimension") == "month"
    cols = (out.get("matrix") or {}).get("columns") or []
    assert AMS_3_COL in cols
    assert AMS_6_COL in cols
    assert "Total" in cols
    assert "Average" not in cols
    # AMS headers appear in rendered HTML
    md = out.get("answer_markdown") or ""
    assert "AMS (3 months)" in md
    assert "AMS (6 months)" in md


def _assert_named_month_trend(out: dict) -> None:
    """Named month asks → Volume + AMS + %change (not a 6-month grid)."""
    assert out.get("ok") is True
    assert out.get("column_dimension") != "month"
    trend = out.get("trend") or {}
    cols = trend.get("columns") or []
    assert "volume_mt" in cols
    assert "ams_mt" in cols
    assert "pct_vs_ams" in cols or "pct_vs_expected" in cols
    md = out.get("answer_markdown") or ""
    assert "Volume vs AMS" in md or "AMS (MT)" in md
    assert "AMS (6 months)" not in md
    assert "Mar 2026" not in md


def test_fetch_lines_fast_with_large_client_master() -> None:
    """Regression: expression clients JOIN scanned all clients per sales row."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            init_db()
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO category "
                    "(product, category_1, category_2, packing_category, "
                    "payload_json, updated_at) VALUES "
                    "('Eva Canola Oil (StandUpPouch)', 'Eva Consumer', "
                    "'Eva Canola', 'Stand up', '{}', datetime('now'))"
                )
                clients = [
                    (
                        str(i),
                        f"Dist {i}",
                        "Eva Distributors" if i % 3 else "Imtiaz Store",
                        "Lahore" if i % 2 == 0 else "Karachi",
                        "Lahore" if i % 2 == 0 else "Karachi",
                    )
                    for i in range(2000)
                ]
                conn.executemany(
                    "INSERT OR REPLACE INTO clients "
                    "(client_id, client, type, city_filter, city, inactive, "
                    "payload_json, updated_at) VALUES "
                    "(?,?,?,?,?,'','{}', datetime('now'))",
                    clients,
                )
                rows = []
                for i in range(8000):
                    month = 1 + (i % 8)
                    ci = i % 2000
                    rows.append(
                        (
                            f"big-{i}",
                            f"2026-{month:02d}-05",
                            clients[ci][1],
                            "Eva Canola Oil (StandUpPouch)",
                            2.0,
                            2.0,
                            clients[ci][2],
                        )
                    )
                conn.executemany(
                    """
                    INSERT INTO sales (
                      source_file_id, row_hash, imported_at, date, party, product,
                      qty, unit, mt_qty, client_type, payload_json
                    ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, '{}')
                    """,
                    rows,
                )
                conn.commit()

            import time

            t0 = time.time()
            out = _dispatch_tool(
                "query_sales",
                {},
                user_text="Show me Eva distributor sales for july",
            )
            elapsed = time.time() - t0
            _assert_named_month_trend(out)
            assert out.get("filters", {}).get("client_type") == "Eva Distributors"
            # Old join was ~2 minutes at this scale; fast path should be well under 5s
            assert elapsed < 5.0, f"query_sales too slow: {elapsed:.2f}s"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_month_ams_uses_single_fetch() -> None:
    """Regression: AMS enrichment must not scan the DB once per prior month."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            import eva_dashboard.sales_query as sq

            calls = {"n": 0}
            orig = sq._fetch_lines

            def _counted(*args, **kwargs):
                calls["n"] += 1
                return orig(*args, **kwargs)

            sq._fetch_lines = _counted  # type: ignore[assignment]
            try:
                out = _dispatch_tool(
                    "query_sales",
                    {},
                    user_text="Show me Eva distributor sales",
                )
            finally:
                sq._fetch_lines = orig  # type: ignore[assignment]
            _assert_month_ams(out)
            # One extended window fetch for matrix + AMS (was ~19 before v0.4.5)
            assert calls["n"] <= 2, f"too many _fetch_lines calls: {calls['n']}"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_show_me_party_client_type_city_default_months_ams() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()

            party = _dispatch_tool(
                "lookup_party", {}, user_text="Show me Alpha Dist sales"
            )
            assert resolve_forced_tool("Show me Alpha Dist sales") == "required"
            assert suggest_preferred_tool("Show me Alpha Dist sales") == "lookup_party"
            assert party.get("mode") == "party_sales"
            assert party.get("filters", {}).get("party") == "Alpha Dist"
            _assert_month_ams(party)
            assert float((party.get("matrix") or {}).get("grand_total_mt") or 0) > 8

            imtiaz = _dispatch_tool(
                "query_sales", {}, user_text="Show me Imtiaz sales"
            )
            assert imtiaz.get("filters", {}).get("client_type") == "Imtiaz Store"
            _assert_month_ams(imtiaz)

            lahore = _dispatch_tool(
                "query_sales", {}, user_text="Show me Lahore sales"
            )
            assert lahore.get("filters", {}).get("city") == "Lahore"
            _assert_month_ams(lahore)

            dist = _dispatch_tool(
                "query_sales", {}, user_text="Show me distributor sales"
            )
            assert dist.get("filters", {}).get("client_type") == "Eva Distributors"
            _assert_month_ams(dist)
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_show_me_ignores_stale_prior_and_thin_args() -> None:
    """v0.4.2: fresh show-me sales keeps months+AMS despite prior / thin GPT args."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            stale_prior = {
                "filters": {"client_type": "Imtiaz Store"},
                "column_dimension": "client_type",
                "period_phrase": "July",
                "months_back": 1,
            }
            lahore = _dispatch_tool(
                "query_sales",
                {
                    "columns": "client_type",
                    "business_unit": "Eva Consumer",
                    "months_back": 1,
                },
                user_text="Show me Lahore sales",
                prior_spec=stale_prior,
            )
            assert lahore.get("filters", {}).get("city") == "Lahore"
            assert not lahore.get("filters", {}).get("business_unit")
            assert lahore.get("months_back") == 6 or (
                (lahore.get("table_spec") or {}).get("months_back") == 6
            )
            _assert_month_ams(lahore)
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_scoped_sales_via_query_sales_month_ams() -> None:
    """AI-first: preferred query_sales builds the months + AMS matrix."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = _dispatch_tool(
                "query_sales", {}, user_text="Show me distributor sales"
            )
            assert out.get("filters", {}).get("client_type") == "Eva Distributors"
            _assert_month_ams(out)

            imtiaz = _dispatch_tool(
                "query_sales", {}, user_text="Show me Imtiaz sales"
            )
            assert imtiaz.get("filters", {}).get("client_type") == "Imtiaz Store"
            _assert_month_ams(imtiaz)
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_remove_multiple_business_units_not_inverted() -> None:
    """'remove Maan… and Cusine King' must exclude them — not filter TO them."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            from eva_dashboard.chatbot import resolve_remove_request

            first = _dispatch_tool(
                "query_sales", {}, user_text="Show me distributor sales"
            )
            prior = first["table_spec"]
            q = "remove maan consumer and maan bulk items and cuisine king"
            plan = resolve_remove_request(q, prior_spec=prior)
            assert plan is not None
            assert set(plan["excludes"]["business_unit"]) == {
                "Maan Consumer",
                "Maan Bulk",
                "Cusine King",
            }

            out = _dispatch_tool(
                "query_sales", {}, user_text=q, prior_spec=prior
            )
            assert out.get("ok") is True
            bus = set(out.get("business_units") or [])
            # Must not be filtered TO the removed brands
            assert "Maan Bulk" not in bus
            assert "Maan Consumer" not in bus
            assert "Cusine King" not in bus
            excl = (out.get("excludes") or {}).get("business_unit") or []
            assert "Maan Bulk" in excl and "Cusine King" in excl
            # Remaining rows should not include removed BUs as leaf parents
            md = out.get("answer_markdown") or ""
            assert "Maan Bulk Total" not in md
            assert "Cusine King Total" not in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_group_by_city_keeps_month_and_ams() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            first = _dispatch_tool(
                "query_sales", {}, user_text="Show me distributor sales"
            )
            _assert_month_ams(first)
            prior = first["table_spec"]

            by_city = _dispatch_tool(
                "query_sales",
                {},
                user_text="group by city",
                prior_spec=prior,
            )
            assert by_city.get("column_dimension") == "month"
            cols = (by_city.get("matrix") or {}).get("columns") or []
            assert AMS_3_COL in cols and AMS_6_COL in cols
            headers = (by_city.get("matrix") or {}).get("row_headers") or [
                by_city.get("row_dimension")
            ]
            assert headers[0] == "city"
            assert by_city.get("filters", {}).get("client_type") == "Eva Distributors"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_named_month_uses_volume_ams_pct_not_six_month_grid() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = _dispatch_tool(
                "query_sales",
                {},
                user_text="Show me Eva distributor sales in Karachi for July",
            )
            _assert_named_month_trend(out)
            assert out.get("filters", {}).get("city") == "Karachi"
            assert out.get("filters", {}).get("client_type") == "Eva Distributors"
            assert (out.get("period") or {}).get("date_from", "").startswith("2026-07")
            # No-month ask still gets the 6-month AMS grid
            bare = _dispatch_tool(
                "query_sales", {}, user_text="Show me Eva distributor sales in Karachi"
            )
            _assert_month_ams(bare)
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_sold_to_followup_filters_business_unit() -> None:
    """Ad-hoc 'which distributor was BU sold to' keeps prior scope + BU filter."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO category "
                    "(product, category_1, category_2, packing_category, "
                    "payload_json, updated_at) VALUES "
                    "('Maan Canola Oil', 'Maan Consumer', 'Maan Canola', "
                    "'Stand up', '{}', datetime('now'))"
                )
                conn.execute(
                    """
                    INSERT INTO sales (
                      source_file_id, row_hash, imported_at, date, party, product,
                      qty, unit, mt_qty, client_type, payload_json
                    ) VALUES (NULL, 'maan-khi', datetime('now'), '2026-07-12',
                      'Gamma Dist', 'Maan Canola Oil', 5, 'MT', 5,
                      'Eva Distributors', '{}')
                    """
                )
                conn.commit()

            from eva_dashboard.chatbot import (
                _looks_sold_to_parties,
                resolve_forced_tool,
            )

            q = "Which distributor was the maan consumer sold to"
            assert _looks_sold_to_parties(q)
            prior = {
                "filters": {
                    "city": "Karachi",
                    "client_type": "Eva Distributors",
                },
                "period_phrase": "July 2026",
                "column_dimension": "month",
                "business_units": [],
            }
            assert resolve_forced_tool(q, prior_table_spec=prior) == "required"
            assert suggest_preferred_tool(q, prior_table_spec=prior) == "query_sales"
            # Prefer list_clients / which-parties path for sold-to identity+matrix
            out = _dispatch_tool(
                "list_clients",
                {},
                user_text=q,
                prior_spec=prior,
            )
            assert out.get("ok") is True
            assert out.get("row_dimension") == "party"
            assert out.get("column_dimension") == "month"
            assert out.get("filters", {}).get("business_unit") == "Maan Consumer"
            assert out.get("filters", {}).get("city") == "Karachi"
            assert out.get("filters", {}).get("client_type") == "Eva Distributors"
            md = out.get("answer_markdown") or ""
            assert "Gamma Dist" in md
            assert "Maan Consumer" in md or "BU" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
