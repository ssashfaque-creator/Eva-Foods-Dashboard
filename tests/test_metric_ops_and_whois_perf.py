"""Metric ops (AMS/volume/least growth) + fast who-is / ordinal paths."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from eva_dashboard.client_language import lookup_party
from eva_dashboard.db import connect, init_db
from eva_dashboard.ordinal_parties import (
    extract_ordinal_indices,
    resolve_ordinal_party_names,
)
from eva_dashboard.query_executor import _coerce_vocab_from_user_text, execute_query_spec


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed_clients(n: int = 80) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, "
            "payload_json, updated_at) VALUES "
            "('P1', 'Eva Consumer', 'Eva Canola', 'Stand up', '{}', datetime('now'))"
        )
        for i in range(n):
            name = f"Client {i:03d} Traders"
            if i == 7:
                name = "Al Shaheer Lahore"
            if i == 8:
                name = "Al Shaheer Karachi"
            conn.execute(
                "INSERT OR REPLACE INTO clients "
                "(client_id, client, type, city_filter, city, inactive, "
                "payload_json, updated_at) VALUES "
                "(?, ?, 'Eva Distributors', 'Lahore', 'Lahore', '', "
                "'{}', datetime('now'))",
                (str(i), name),
            )
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), '2026-07-10', ?, 'P1',
                  5, 'MT', 5, 'Eva Distributors', '{}')
                """,
                (f"s-{i}", name),
            )
        conn.commit()


def test_volume_and_least_growth_coerce():
    vol = _coerce_vocab_from_user_text(
        {"row_dimensions": ["business_unit"], "metrics": ["volume"], "filters": {}},
        "only show customers with volume greater than 20",
    )
    assert vol.get("intent") == "party_rank"
    assert vol.get("metric") == "volume"
    assert any(
        f.get("metric") == "volume" and f.get("value") == 20.0
        for f in (vol.get("metric_filters") or [])
    )

    least = _coerce_vocab_from_user_text(
        {"row_dimensions": ["party"], "metrics": ["ams_growth"], "filters": {}},
        "distributors with the least growth",
    )
    assert least.get("intent") == "party_rank"
    assert least.get("metric") == "ams_growth"
    assert least.get("sort") == "asc"
    assert least.get("title_mode") == "smallest_gains"
    assert (least.get("filters") or {}).get("client_type") == "Eva Distributors"


def test_ordinal_extract_show_ams_for_1_and_2():
    assert extract_ordinal_indices("show ams for 1 and 2") == [1, 2]
    prior = {
        "matches": [
            {"client": "A", "ams_3m": 10},
            {"client": "B", "ams_3m": 8},
            {"client": "C", "ams_3m": 1},
        ]
    }
    assert resolve_ordinal_party_names("show ams for 1 and 2", prior) == ["A", "B"]


def test_whois_short_circuit_stamps_matches_for_ordinals():
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        _seed_clients(40)
        out = execute_query_spec(
            {"operation": "party_lookup", "filters": {}},
            user_text="who is Al Shaheer",
        )
        assert out.get("ok")
        matches = out.get("matches") or []
        assert len(matches) >= 2
        assert (out.get("party_spec") or {}).get("matches")
        assert (out.get("table_spec") or {}).get("matches")
        qs = out.get("query_state") or {}
        assert qs.get("matches")

        # Ordinal follow-up resolves from stamped prior
        names = resolve_ordinal_party_names(
            "show ams for 1 and 2",
            {
                "matches": qs.get("matches"),
                "row_dimensions": ["party"],
                "metrics": ["volume", "ams"],
            },
        )
        assert len(names) == 2


def test_lookup_party_shortlist_is_fast():
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        _seed_clients(120)
        t0 = time.perf_counter()
        out = lookup_party("Al Shaheer", limit=10)
        elapsed = time.perf_counter() - t0
        assert out.get("ok")
        assert out.get("matches")
        # Should be well under a second on a small seeded DB (was minutes with JOIN)
        assert elapsed < 2.0, f"lookup_party too slow: {elapsed:.2f}s"
