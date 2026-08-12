"""Training-CSV comment regressions (Aug 2026 chat export)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.client_language import lookup_party
from eva_dashboard.db import connect, init_db
from eva_dashboard.ordinal_parties import (
    extract_ordinal_indices,
    resolve_ordinal_party_names,
)
from eva_dashboard.query_executor import (
    _coerce_vocab_from_user_text,
    _looks_drop_party_grain,
    execute_query_spec,
)
from eva_dashboard.sales_query import _ROW_HEADER_LABELS


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed() -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, payload_json, updated_at) "
            "VALUES ('P1', 'Eva Consumer', 'Eva Canola', 'Stand up', '{}', datetime('now'))"
        )
        for cid, name, city, ctype in (
            ("1", "AL SHAHEER CORPORATION LIMITED", "Lahore", "Eva Distributors"),
            ("2", "Other Dist", "Lahore", "Eva Distributors"),
            ("3", "AL Bari Traders (Old City)", "Karachi", "Eva Distributors"),
            ("4", "AL Bari Traders (DHA/Clifton)", "Karachi", "Eva Distributors"),
        ):
            conn.execute(
                "INSERT OR REPLACE INTO clients "
                "(client_id, client, type, city_filter, city, inactive, "
                "payload_json, updated_at) VALUES "
                "(?, ?, ?, ?, ?, '', '{}', datetime('now'))",
                (cid, name, ctype, city, city),
            )
        for i, (dt, party, mt) in enumerate(
            [
                ("2026-03-05", "AL SHAHEER CORPORATION LIMITED", 100),
                ("2026-03-05", "Other Dist", 200),
                ("2026-04-05", "Other Dist", 50),
                ("2026-03-05", "AL Bari Traders (Old City)", 10),
                ("2026-03-05", "AL Bari Traders (DHA/Clifton)", 20),
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
                (f"csv-{i}", dt, party, mt, mt, mt * 100),
            )
        conn.commit()


def test_party_header_is_customer_not_distributor():
    assert _ROW_HEADER_LABELS["party"] == "Customer"


def test_exclude_keeps_bu_grain_not_party():
    prior = {
        "filters": {"city": "Lahore"},
        "business_units": ["Eva Consumer", "Eva Bulk"],
        "row_dimensions": ["business_unit"],
        "column_dimensions": ["month"],
        "metrics": ["volume", "ams"],
        "months_back": 6,
    }
    out = _coerce_vocab_from_user_text(
        {
            "row_dimensions": ["party"],  # bad planner guess
            "column_dimensions": ["month"],
            "metrics": ["volume", "ams"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 6,
            "context_handling": "none",
            "filters": {"city": "Lahore"},
            "business_units": ["Eva Consumer", "Eva Bulk"],
        },
        "show me Eva sales in lahore excluding al shaheer",
        prior=prior,
    )
    assert out.get("row_dimensions") == ["business_unit"], out.get("row_dimensions")
    assert out.get("base") == "prior"
    assert out.get("excludes")


def test_customer_wise_promotes_prior_filters():
    prior = {
        "filters": {"city": "Lahore"},
        "business_units": ["Eva Consumer", "Eva Bulk"],
        "row_dimensions": ["business_unit"],
        "column_dimensions": ["month"],
        "metrics": ["volume", "ams"],
    }
    out = _coerce_vocab_from_user_text(
        {
            "row_dimensions": ["party"],
            "metrics": ["volume"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 6,
            "context_handling": "none",
            "filters": {},
        },
        "show me a breakup customer wise",
        prior=prior,
    )
    assert out.get("base") == "prior"
    # Merge happens later; coerce must at least promote base
    assert "party" in (out.get("row_dimensions") or [])


def test_drop_party_grain_language():
    assert _looks_drop_party_grain("remove the distributor layer")
    assert _looks_drop_party_grain(
        "include distributors but don't show customers in the table"
    )
    assert _looks_drop_party_grain(
        "show overall by business unit I don't need the individual customer names"
    )
    assert not _looks_drop_party_grain("exclude distributors")

    prior = {
        "filters": {"city": "Lahore"},
        "business_units": ["Eva Consumer", "Eva Bulk"],
        "row_dimensions": ["business_unit", "party"],
        "column_dimensions": ["month"],
        "metrics": ["volume", "ams"],
    }
    out = _coerce_vocab_from_user_text(
        {
            "row_dimensions": ["client_type"],
            "metrics": ["volume"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 6,
            "filters": {},
        },
        "include distributors but don't show customers in the table",
        prior=prior,
    )
    assert "party" not in (out.get("row_dimensions") or [])
    assert "business_unit" in (out.get("row_dimensions") or [])


def test_ordinal_first_two():
    assert extract_ordinal_indices("can you show volumes for the first 2 last 6 months") == [
        1,
        2,
    ]
    prior = {
        "matches": [
            {"client": "AL Bari Traders (Old City)", "ordinal": 1},
            {"client": "AL Bari Traders (DHA/Clifton)", "ordinal": 2},
            {"client": "Al Qadri", "ordinal": 3},
        ]
    }
    names = resolve_ordinal_party_names(
        "can you show volumes for the first 2 last 6 months",
        prior,
    )
    assert names == [
        "AL Bari Traders (Old City)",
        "AL Bari Traders (DHA/Clifton)",
    ]


def test_ordinal_followup_clears_unrelated_channel_city():
    prior = {
        "filters": {"city": "Lahore", "client_type": "Imtiaz Store"},
        "matches": [
            {"client": "AL Bari Traders (Old City)"},
            {"client": "AL Bari Traders (DHA/Clifton)"},
        ],
        "row_dimensions": ["party"],
        "metrics": ["volume", "ams"],
    }
    out = _coerce_vocab_from_user_text(
        {
            "row_dimensions": ["party"],
            "metrics": ["volume"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 6,
            "filters": {"city": "Lahore", "client_type": "Imtiaz Store"},
            "context_handling": "prior",
        },
        "can you show volumes for the first 2 last 6 months",
        prior=prior,
    )
    filters = out.get("filters") or {}
    assert filters.get("parties") == [
        "AL Bari Traders (Old City)",
        "AL Bari Traders (DHA/Clifton)",
    ]
    assert not filters.get("city")
    assert not filters.get("client_type")


def test_who_is_table_has_ams_zone_ordinal():
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        _seed()
        out = lookup_party("al Bari", limit=10)
        assert out.get("ok")
        md = out.get("answer_markdown") or ""
        assert "| # |" in md
        assert "AMS (3m)" in md
        assert "Zone" in md
        matches = out.get("matches") or []
        assert matches
        assert matches[0].get("ordinal") == 1


def test_exclude_execute_keeps_bu_not_party_rows():
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        _seed()
        prior = {
            "filters": {"city": "Lahore"},
            "business_units": ["Eva Consumer", "Eva Bulk"],
            "row_dimensions": ["business_unit"],
            "column_dimensions": ["month"],
            "metrics": ["volume", "ams"],
            "months_back": 6,
        }
        out = execute_query_spec(
            {
                "row_dimensions": ["party"],
                "column_dimensions": ["month"],
                "metrics": ["volume", "ams"],
                "period_type": "LAST_N_MONTHS",
                "months_back": 6,
                "state_action": "clear",
                "filters": {"city": "Lahore"},
                "business_units": ["Eva Consumer", "Eva Bulk"],
                "extracted_entities": ["al shaheer"],
            },
            prior=prior,
            user_text="show me Eva sales in lahore excluding al shaheer",
        )
        assert out.get("ok"), out.get("plan_errors") or out.get("error")
        qs = out.get("query_spec") or {}
        assert qs.get("row_dimensions") == ["business_unit"], qs.get("row_dimensions")
        md = (out.get("answer_markdown") or "").lower()
        assert "customer" in md or "business unit" in md
        assert "distributor" not in md.split("\n")[0:20] or "eva distributors" in md
