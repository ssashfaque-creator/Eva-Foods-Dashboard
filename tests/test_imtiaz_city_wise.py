"""Fresh 'Imtiaz city wise' must return City × Month, not BU × Month."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import extract_regroup_dimension
from eva_dashboard.db import connect, init_db
from eva_dashboard.query_executor import (
    _coerce_vocab_from_user_text,
    _spoken_wise_dimension,
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
            ],
        )
        rows = [
            ("2026-05-10", "Imtiaz Lahore", "Eva Canola Oil (StandUpPouch)", 20.0),
            ("2026-06-10", "Imtiaz Lahore", "Eva Canola Oil (StandUpPouch)", 22.0),
            ("2026-07-05", "Imtiaz Karachi", "Eva Cooking Oil (StandUpPouch)", 40.0),
            ("2026-07-06", "Imtiaz Islamabad", "Eva Cooking Oil (16 Ltr Tin)", 8.0),
            ("2026-08-01", "Imtiaz Lahore", "Eva Canola Oil (StandUpPouch)", 10.0),
            ("2026-08-02", "Imtiaz Karachi", "Eva Cooking Oil (StandUpPouch)", 12.0),
        ]
        for i, (dt, party, product, mt) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type,
                  payload_json
                ) VALUES (
                  NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, 500, 10000,
                  'Imtiaz Store', '{}'
                )
                """,
                (f"cw-{i}-{dt}", dt, party, product, mt, mt),
            )
        conn.commit()


def test_spoken_city_wise_aliases() -> None:
    assert _spoken_wise_dimension("sales for Imtiaz city wise") == "city"
    assert _spoken_wise_dimension("imtiaz citywide") == "city"
    assert _spoken_wise_dimension("show me city wide sales") == "city"
    assert extract_regroup_dimension("can you show me sales for Imtiaz city wise") == (
        "city"
    )


def test_coerce_overrides_bu_month_plan() -> None:
    bad = {
        "row_dimensions": ["business_unit"],
        "column_dimensions": ["month"],
        "metrics": ["volume"],
        "filters": {"client_type": "Imtiaz Store"},
    }
    fixed = _coerce_vocab_from_user_text(
        bad, "can you show me sales for Imtiaz city wise"
    )
    assert fixed["row_dimensions"] == ["city"]
    assert "month" in fixed["column_dimensions"]
    assert fixed["filters"]["client_type"] == "Imtiaz Store"


def test_execute_imtiaz_city_wise_not_bu_month() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            # Simulate a bad LLM plan (BU × Month) for a city-wise ask
            result = execute_query_spec(
                {
                    "operation": "pivot",
                    "row_dimensions": ["business_unit"],
                    "column_dimensions": ["month"],
                    "metrics": ["volume"],
                    "period_type": "LAST_N_MONTHS",
                    "period_n": 6,
                    "filters": {"client_type": "Imtiaz Store"},
                    "context_handling": "replace",
                },
                user_text="can you show me sales for Imtiaz city wise",
            )
            assert result.get("ok") is True, result
            headers = (result.get("matrix") or {}).get("row_headers") or [
                result.get("row_dimension")
            ]
            assert headers[0] == "city", headers
            assert result.get("column_dimension") == "month"
            assert (result.get("filters") or {}).get("client_type") == "Imtiaz Store"
            # Cities should appear as rows
            row_labels = [
                str((r.get("city") or r.get("City") or next(iter(r.values()), "")))
                for r in ((result.get("matrix") or {}).get("rows") or [])
                if not str(r.get("city") or "").lower().startswith("total")
            ]
            assert any("Lahore" in x for x in row_labels) or any(
                "Karachi" in x for x in row_labels
            ), row_labels
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
