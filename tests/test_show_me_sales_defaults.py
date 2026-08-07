"""Defaults for 'show me X sales' → months + AMS; regroup follow-ups."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import _dispatch_tool, resolve_forced_tool
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
                    user_text="Show me Eva distributor sales for july",
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
            assert resolve_forced_tool("Show me Alpha Dist sales") == "lookup_party"
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


def test_wrong_tool_redirects_scoped_sales_to_month_ams() -> None:
    """Under required tool_choice, wrong tool names still get the sales matrix."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            for tool in ("list_clients", "analyze_parties", "advanced_query"):
                out = _dispatch_tool(tool, {}, user_text="Show me distributor sales")
                assert out.get("filters", {}).get("client_type") == "Eva Distributors", tool
                _assert_month_ams(out)

            imtiaz = _dispatch_tool(
                "lookup_party", {}, user_text="Show me Imtiaz sales"
            )
            assert imtiaz.get("filters", {}).get("client_type") == "Imtiaz Store"
            _assert_month_ams(imtiaz)
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
