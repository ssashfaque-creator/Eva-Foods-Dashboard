"""Polarity layer: INCLUDE vs EXCLUDE for any entity (not one-off names)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.db import connect, init_db
from eva_dashboard.query_executor import execute_query_spec
from eva_dashboard.spoken_constraints import (
    apply_spoken_constraints,
    extract_exclude_phrases,
    resolve_exclude_map,
)


def test_exclude_phrases_generic() -> None:
    cases = {
        "show Eva sales in lahore but exclude al shaheer": ["al shaheer"],
        "Lahore sales without donations": ["donations"],
        "Karachi volume except Maan Bulk": ["Maan Bulk"],
        "remove inactive": ["inactive"],
        "filter out sample parties": ["sample parties"],
        "metro sales but not chase up": ["chase up"],
    }
    for text, expect in cases.items():
        got = [p.lower() for p in extract_exclude_phrases(text)]
        for e in expect:
            assert any(e.lower() in g for g in got), (text, got)


def test_resolve_exclude_map_dimensions() -> None:
    m = resolve_exclude_map("Lahore Eva sales but exclude al shaheer")
    blob = " ".join(str(v).lower() for vals in m.values() for v in vals)
    assert "shaheer" in blob

    m2 = resolve_exclude_map("show sales without donations")
    assert (m2.get("client_type") or [None])[0] == "DONATIONS"

    m3 = resolve_exclude_map("national sales except Eva Bulk")
    assert "Eva Bulk" in (m3.get("business_unit") or [])


def test_apply_strips_inverted_party_include() -> None:
    spec = {
        "filters": {
            "city": "Lahore",
            "party": "AL SHAHEER CORPORATION LIMITED",
        },
        "business_units": ["Eva Consumer", "Eva Bulk"],
        "extracted_entities": ["al shaheer"],
        "excludes": {},
    }
    out = apply_spoken_constraints(
        spec, user_text="show me Eva sales in lahore but exclude al shaheer"
    )
    assert not (out.get("filters") or {}).get("party")
    assert not (out.get("filters") or {}).get("party_ilike")
    ex = out.get("excludes") or {}
    assert "shaheer" in " ".join(
        str(v).lower() for vals in ex.values() for v in vals
    )


def test_execute_polarity_for_several_entities() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")
        try:
            import eva_dashboard.sales_query as sq

            sq._CLIENTS_CACHE = None
            sq._CLIENTS_CACHE_SIG = None
            init_db()
            with connect() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO category "
                    "(product, category_1, category_2, packing_category, "
                    "payload_json, updated_at) VALUES (?, ?, ?, ?, '{}', "
                    "datetime('now'))",
                    [
                        ("P1", "Eva Consumer", "Eva Canola", "Stand up"),
                        ("P2", "Eva Bulk", "Eva VTF Bulk", "Tin"),
                    ],
                )
                for cid, name, ctype, city in (
                    ("1", "AL SHAHEER CORPORATION LIMITED", "Eva Distributors", "Lahore"),
                    ("2", "OTHER STORE", "Eva Distributors", "Lahore"),
                    ("3", "DONATE CO", "DONATIONS", "Lahore"),
                ):
                    conn.execute(
                        "INSERT OR REPLACE INTO clients "
                        "(client_id, client, type, city_filter, city, inactive, "
                        "payload_json, updated_at) VALUES "
                        "(?, ?, ?, ?, ?, '', '{}', datetime('now'))",
                        (cid, name, ctype, city, city),
                    )
                rows = [
                    ("2026-07-05", "AL SHAHEER CORPORATION LIMITED", "P1", 100, "Eva Distributors"),
                    ("2026-07-06", "OTHER STORE", "P1", 50, "Eva Distributors"),
                    ("2026-07-07", "DONATE CO", "P1", 25, "DONATIONS"),
                    ("2026-07-08", "OTHER STORE", "P2", 10, "Eva Distributors"),
                ]
                for i, (dt, party, prod, mt, ct) in enumerate(rows):
                    conn.execute(
                        """
                        INSERT INTO sales (
                          source_file_id, row_hash, imported_at, date, party,
                          product, qty, unit, mt_qty, rate, incl_gst_fed_amount,
                          client_type, payload_json
                        ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?,
                          100, ?, ?, '{}')
                        """,
                        (f"pc-{i}", dt, party, prod, mt, mt, mt * 100, ct),
                    )
                conn.commit()

            # Bad planner: party INCLUDE inverted — polarity must fix
            texts = [
                "show me Eva sales in lahore but exclude al shaheer",
                "Lahore Eva sales without al shaheer",
                "Eva sales Lahore except al shaheer",
            ]
            for text in texts:
                out = execute_query_spec(
                    {
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
                        "extracted_entities": ["al shaheer"],
                    },
                    user_text=text,
                )
                assert out.get("ok"), (text, out.get("error"))
                filters = (out.get("query_spec") or {}).get("filters") or {}
                assert not filters.get("party"), (text, filters)
                md = out.get("answer_markdown") or ""
                assert "· party **AL SHAHEER" not in md, text
                assert "excl." in md.lower(), text
                total = float((out.get("matrix") or {}).get("grand_total_mt") or 0)
                assert total < 100 or "OTHER" in md.upper() or total == 60, (
                    text,
                    total,
                    md[:300],
                )

            # Channel exclude
            out2 = execute_query_spec(
                {
                    "operation": "pivot",
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "context_handling": "none",
                    "filters": {"city": "Lahore"},
                    "business_units": ["Eva Consumer", "Eva Bulk"],
                },
                user_text="Lahore Eva sales without donations",
            )
            assert out2.get("ok"), out2.get("error")
            ex2 = (out2.get("query_spec") or {}).get("excludes") or {}
            assert "DONATIONS" in (ex2.get("client_type") or [])
        finally:
            import eva_dashboard.sales_query as sq

            sq._CLIENTS_CACHE = None
            sq._CLIENTS_CACHE_SIG = None
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
