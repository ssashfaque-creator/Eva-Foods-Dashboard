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


def test_exclude_al_shaheer_same_sentence_not_include_filter() -> None:
    """Bugfix: 'exclude al shaheer' was becoming filters.party (include only)."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            init_db()
            with connect() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO category "
                    "(product, category_1, category_2, packing_category, "
                    "payload_json, updated_at) VALUES (?, ?, ?, ?, '{}', datetime('now'))",
                    [
                        ("P1", "Eva Consumer", "Eva Canola", "Stand up"),
                        ("P2", "Eva Bulk", "Eva Bulk", "Tin"),
                    ],
                )
                for cid, name in (
                    ("1", "AL SHAHEER CORPORATION LIMITED"),
                    ("2", "Other Dist"),
                ):
                    conn.execute(
                        "INSERT OR REPLACE INTO clients "
                        "(client_id, client, type, city_filter, city, inactive, "
                        "payload_json, updated_at) VALUES "
                        "(?, ?, 'Eva Distributors', 'Lahore', 'Lahore', '', "
                        "'{}', datetime('now'))",
                        (cid, name),
                    )
                for i, (dt, party, mt) in enumerate(
                    [
                        ("2026-03-05", "AL SHAHEER CORPORATION LIMITED", 100),
                        ("2026-03-05", "Other Dist", 200),
                        ("2026-04-05", "Other Dist", 50),
                    ]
                ):
                    conn.execute(
                        """
                        INSERT INTO sales (
                          source_file_id, row_hash, imported_at, date, party, product,
                          qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type,
                          payload_json
                        ) VALUES (NULL, ?, datetime('now'), ?, ?, 'P1', ?, 'MT', ?,
                          100, ?, 'Eva Distributors', '{}')
                        """,
                        (f"ex-{i}", dt, party, mt, mt, mt * 100),
                    )
                conn.commit()
            out = execute_query_spec(
                {
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "business_units": ["Eva Consumer", "Eva Bulk"],
                    "filters": {"city": "Lahore"},
                    # Planner mistake: puts excluded name in extracted_entities
                    "extracted_entities": ["al shaheer"],
                },
                user_text="show me lahore Eva sales again but exclude al shaheer",
            )
            assert out.get("ok"), out.get("error")
            filters = (out.get("query_spec") or {}).get("filters") or {}
            excludes = (out.get("query_spec") or {}).get("excludes") or {}
            assert not filters.get("party"), filters
            assert "AL SHAHEER CORPORATION LIMITED" in (excludes.get("party") or [])
            md = out.get("answer_markdown") or ""
            assert "excl. party" in md.lower()
            assert "AL SHAHEER CORPORATION LIMITED" in md
            # Must not be "only al shaheer" — Other Dist volume remains
            assert "200" in md or "250" in md or "Eva Consumer" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_who_is_al_bari_lookup() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            init_db()
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO clients "
                    "(client_id, client, type, city_filter, city, inactive, "
                    "payload_json, updated_at) VALUES "
                    "('1', 'Al Bari Traders', 'Eva Distributors', 'Karachi', "
                    "'Karachi', '', '{}', datetime('now'))"
                )
                conn.commit()
            out = execute_query_spec(
                {
                    "operation": "pivot",
                    "row_dimensions": ["party"],
                    "metrics": ["volume"],
                    "period_type": "MTD",
                    "context_handling": "none",
                    "extracted_entities": ["al bari"],
                },
                user_text="who is al bari",
            )
            assert out.get("ok")
            assert (out.get("query_spec") or {}).get("operation") == "party_lookup"
            assert "Al Bari Traders" in (out.get("answer_markdown") or "")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_who_is_al_shaheer_lookup_not_sales() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            init_db()
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO clients "
                    "(client_id, client, type, city_filter, city, inactive, "
                    "payload_json, updated_at) VALUES "
                    "('1', 'AL SHAHEER CORPORATION LIMITED', 'Eva Distributors', "
                    "'Lahore', 'Lahore', '', '{}', datetime('now'))"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO category "
                    "(product, category_1, category_2, packing_category, "
                    "payload_json, updated_at) VALUES "
                    "('P1', 'Eva Consumer', 'Eva Canola', 'Stand up', '{}', "
                    "datetime('now'))"
                )
                conn.execute(
                    """
                    INSERT INTO sales (
                      source_file_id, row_hash, imported_at, date, party, product,
                      qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type,
                      payload_json
                    ) VALUES (NULL, 'as-1', datetime('now'), '2026-07-01',
                      'AL SHAHEER CORPORATION LIMITED', 'P1', 10, 'MT', 10, 100,
                      1000, 'Eva Distributors', '{}')
                    """
                )
                conn.commit()
            out = execute_query_spec(
                {
                    "operation": "pivot",
                    "row_dimensions": ["party"],
                    "metrics": ["volume"],
                    "period_type": "MTD",
                    "context_handling": "none",
                    "extracted_entities": ["al shaheer"],
                },
                user_text="who is al shaheer",
            )
            assert out.get("ok") is True, out
            assert out.get("mode") != "party_sales"
            md = out.get("answer_markdown") or ""
            assert "AL SHAHEER CORPORATION LIMITED" in md
            assert "Client search" in md or out.get("matches")
            matches = out.get("matches") or []
            assert any(
                "SHAHEER" in str(m.get("client") or "").upper() for m in matches
            )
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_exclude_al_shaheer_keeps_table_grain() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO clients "
                    "(client_id, client, type, city_filter, city, inactive, "
                    "payload_json, updated_at) VALUES "
                    "('8', 'AL SHAHEER CORPORATION LIMITED', 'Eva Distributors', "
                    "'Lahore', 'Lahore', '', '{}', datetime('now'))"
                )
                conn.execute(
                    """
                    INSERT INTO sales (
                      source_file_id, row_hash, imported_at, date, party, product,
                      qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type,
                      payload_json
                    ) VALUES (NULL, 'as-ex', datetime('now'), '2026-07-15',
                      'AL SHAHEER CORPORATION LIMITED', 'P1', 40, 'MT', 40, 100,
                      4000, 'Eva Distributors', '{}')
                    """
                )
                conn.commit()
            base = execute_query_spec(
                {
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "filters": {"city": "Lahore"},
                    "business_units": ["Eva Consumer", "Eva Bulk"],
                },
                user_text="Lahore Eva sales last 6 months",
            )
            assert base.get("ok"), base.get("error")
            prior = prior_context_from_query_state(base.get("query_state"))
            # Planner wrongly changes grain — coerce must restore prior shape
            out = execute_query_spec(
                {
                    "row_dimensions": ["party"],
                    "column_dimensions": ["city"],
                    "metrics": ["volume"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "prior",
                    "clear_filters": [],
                    "filters": {},
                },
                prior=prior,
                user_text="exclude al shaheer",
            )
            assert out.get("ok"), out.get("error")
            spec = out.get("query_spec") or {}
            assert spec.get("row_dimensions") == ["business_unit"]
            assert "month" in (spec.get("column_dimensions") or [])
            ex = spec.get("excludes") or {}
            party_ex = ex.get("party") or []
            like_ex = ex.get("party_like") or []
            blob = " ".join(str(x).lower() for x in party_ex + like_ex)
            assert "shaheer" in blob
            md = (out.get("answer_markdown") or "").lower()
            assert "al shaheer corporation limited" not in md or "excl" in (
                (out.get("answer_markdown") or "").lower()
            )
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_exclude_al_shaheer_never_becomes_party_include() -> None:
    """Exact user ask from prod: Eva Lahore sales but exclude al shaheer.

    Planner often emits filters.party=AL SHAHEER (INCLUDE). That must never
    survive — caption must show excl., totals must exclude Shaheer volume.
    Also covers sticky prior that previously locked party=Al Shaheer.
    """
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            import eva_dashboard.sales_query as sq

            sq._CLIENTS_CACHE = None
            sq._CLIENTS_CACHE_SIG = None
            _seed()
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO clients "
                    "(client_id, client, type, city_filter, city, inactive, "
                    "payload_json, updated_at) VALUES "
                    "('99', 'AL SHAHEER CORPORATION LIMITED', 'Eva Distributors', "
                    "'Lahore', 'Lahore', '', '{}', datetime('now'))"
                )
                conn.execute(
                    """
                    INSERT INTO sales (
                      source_file_id, row_hash, imported_at, date, party, product,
                      qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type,
                      payload_json
                    ) VALUES (NULL, 'shaheer-mar', datetime('now'), '2026-03-05',
                      'AL SHAHEER CORPORATION LIMITED', 'P1', 497, 'MT', 497, 100,
                      49700, 'Eva Distributors', '{}')
                    """
                )
                conn.commit()

            text = "show me Eva sales in lahore but exclude al shaheer"
            # Bad planner draft matching the screenshot (INCLUDE party)
            draft = {
                "operation": "pivot",
                "row_dimensions": ["business_unit"],
                "column_dimensions": ["month"],
                "metrics": ["volume", "ams"],
                "period_type": "LAST_N_MONTHS",
                "months_back": 6,
                "context_handling": "none",
                "filters": {
                    "city": "Lahore",
                    "party": "AL SHAHEER CORPORATION LIMITED",
                },
                "business_units": ["Eva Consumer", "Eva Bulk"],
                "extracted_entities": ["al shaheer", "Lahore"],
            }
            # Prior from a previous mistaken include — must not stick
            prior = {
                "row_dimensions": ["business_unit"],
                "column_dimensions": ["month"],
                "filters": {
                    "city": "Lahore",
                    "party": "AL SHAHEER CORPORATION LIMITED",
                },
                "business_units": ["Eva Consumer", "Eva Bulk"],
                "months_back": 6,
            }
            out = execute_query_spec(draft, prior=prior, user_text=text)
            assert out.get("ok"), out.get("error")
            filters = (out.get("query_spec") or {}).get("filters") or {}
            excludes = (out.get("query_spec") or {}).get("excludes") or {}
            assert not filters.get("party"), filters
            assert not filters.get("party_ilike"), filters
            blob = " ".join(
                str(v).lower()
                for vals in excludes.values()
                for v in (vals or [])
            )
            assert "shaheer" in blob, excludes
            md = out.get("answer_markdown") or ""
            # Must NOT caption as include-only party filter
            assert "· party **AL SHAHEER" not in md
            assert "excl." in md.lower()
            # Shaheer-only March was 497; excluding it must drop that volume
            total = float((out.get("matrix") or {}).get("grand_total_mt") or 0)
            assert total < 497, (total, md[:400])
        finally:
            import eva_dashboard.sales_query as sq

            sq._CLIENTS_CACHE = None
            sq._CLIENTS_CACHE_SIG = None
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_party_like_exclude_works_without_clients_master() -> None:
    """Regression: exclude al shaheer must drop sales parties even when
    they are missing from the clients master (was a silent no-op)."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            import eva_dashboard.sales_query as sq
            from eva_dashboard.sales_query import query_sales

            sq._CLIENTS_CACHE = None
            sq._CLIENTS_CACHE_SIG = None
            init_db()
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO category "
                    "(product, category_1, category_2, packing_category, "
                    "payload_json, updated_at) VALUES "
                    "('P1', 'Eva Consumer', 'Eva Canola', 'Stand up', '{}', "
                    "datetime('now'))"
                )
                # Only OTHER STORE on master — Shaheer variants unmapped
                conn.execute(
                    "INSERT OR REPLACE INTO clients "
                    "(client_id, client, type, city_filter, city, inactive, "
                    "payload_json, updated_at) VALUES "
                    "('10', 'OTHER STORE', 'Eva Distributors', 'Lahore', "
                    "'Lahore', '', '{}', datetime('now'))"
                )
                for i, (dt, party, mt) in enumerate(
                    [
                        ("2026-07-15", "AL SHAHEER CORPORATION LIMITED", 40),
                        ("2026-07-15", "OTHER STORE", 10),
                        ("2026-07-16", "AL SHAHEER TRADERS (LAHORE)", 20),
                    ]
                ):
                    conn.execute(
                        """
                        INSERT INTO sales (
                          source_file_id, row_hash, imported_at, date, party,
                          product, qty, unit, mt_qty, rate, incl_gst_fed_amount,
                          client_type, payload_json
                        ) VALUES (NULL, ?, datetime('now'), ?, ?, 'P1', ?,
                          'MT', ?, 100, ?, 'Eva Distributors', '{}')
                        """,
                        (f"pl-{i}", dt, party, mt, mt, mt * 100),
                    )
                conn.commit()

            # Direct engine path
            r = query_sales(
                period=None,
                columns="month",
                row_dimension="party",
                months_back=6,
                date_from="2026-01-01",
                date_to="2026-07-31",
                excludes={"party_like": ["al shaheer"]},
            )
            parties = [
                str(x.get("party") or "")
                for x in (r.get("matrix") or {}).get("rows") or []
            ]
            assert not any("SHAHEER" in p.upper() for p in parties), parties
            assert "OTHER STORE" in parties

            # Chat follow-up path: remove / exclude must both resolve + apply
            for text in ("remove al shaheer", "exclude al shaheer"):
                rm = resolve_remove_request(
                    text,
                    prior_spec={
                        "row_dimension": "party",
                        "column_dimension": "month",
                        "filters": {},
                        "row_groups": [],
                    },
                )
                assert rm is not None, text
                blob = " ".join(
                    str(v).lower()
                    for vals in (rm.get("excludes") or {}).values()
                    for v in vals
                )
                assert "shaheer" in blob, (text, rm)

                coerced = _coerce_vocab_from_user_text(
                    {
                        "row_dimensions": ["party"],
                        "column_dimensions": ["month"],
                        "metrics": ["volume", "ams"],
                        "filters": {},
                        "excludes": {},
                    },
                    text,
                    prior={
                        "row_dimensions": ["party"],
                        "column_dimensions": ["month"],
                        "filters": {},
                        "excludes": {},
                    },
                )
                ex = coerced.get("excludes") or rm.get("excludes") or {}
                assert "shaheer" in " ".join(
                    str(v).lower()
                    for vals in ex.values()
                    for v in vals
                ), (text, ex)
                r2 = query_sales(
                    period=None,
                    columns="month",
                    row_dimension="party",
                    months_back=6,
                    date_from="2026-01-01",
                    date_to="2026-07-31",
                    excludes=ex,
                )
                parties2 = [
                    str(x.get("party") or "")
                    for x in (r2.get("matrix") or {}).get("rows") or []
                ]
                assert not any("SHAHEER" in p.upper() for p in parties2), (
                    text,
                    parties2,
                )
                assert "OTHER STORE" in parties2
                md = (r2.get("answer_markdown") or "").upper()
                assert "AL SHAHEER CORPORATION LIMITED" not in md or "EXCL" in md
        finally:
            import eva_dashboard.sales_query as sq

            sq._CLIENTS_CACHE = None
            sq._CLIENTS_CACHE_SIG = None
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_exclude_donation_sales_resolves() -> None:
    prior = {
        "row_dimension": "business_unit",
        "column_dimension": "month",
        "filters": {"city": "Lahore"},
        "business_units": ["Eva Consumer", "Eva Bulk"],
    }
    for text in (
        "exclude donation sales",
        "exclude donations",
        "exclude donation",
        "without donation sales",
    ):
        out = resolve_remove_request(text, prior_spec=prior)
        assert out is not None, text
        assert out["mode"] == "exclude_value"
        assert (out.get("excludes") or {}).get("client_type") == ["DONATIONS"]


def test_execute_exclude_donation_sales() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO clients "
                    "(client_id, client, type, city_filter, city, inactive, "
                    "payload_json, updated_at) VALUES "
                    "('9', 'Donation Lahore', 'DONATIONS', 'Lahore', 'Lahore', "
                    "'', '{}', datetime('now'))"
                )
                conn.execute(
                    """
                    INSERT INTO sales (
                      source_file_id, row_hash, imported_at, date, party, product,
                      qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type,
                      payload_json
                    ) VALUES (NULL, 'don-1', datetime('now'), '2026-07-15',
                      'Donation Lahore', 'P1', 50, 'MT', 50, 100, 5000,
                      'DONATIONS', '{}')
                    """
                )
                conn.commit()
            base = execute_query_spec(
                {
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "filters": {"city": "Lahore"},
                    "business_units": ["Eva Consumer", "Eva Bulk"],
                },
                user_text="Lahore Eva sales last 6 months",
            )
            assert base.get("ok"), base.get("error")
            prior = prior_context_from_query_state(base.get("query_state"))
            out = execute_query_spec(
                {
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume", "ams"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "prior",
                    "clear_filters": [],
                    "filters": {},
                },
                prior=prior,
                user_text="exclude donation sales",
            )
            assert out.get("ok"), out.get("error")
            ex = (out.get("query_spec") or {}).get("excludes") or {}
            assert "DONATIONS" in (ex.get("client_type") or [])
            md = (out.get("answer_markdown") or "").lower()
            # Donation volume should not remain as a visible channel row
            assert "donation lahore" not in md
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
