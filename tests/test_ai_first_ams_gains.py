"""AI-first AMS gains: model args + vocabulary, not hardcode grown-only."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    _dispatch_tool,
    resolve_forced_tool,
    suggest_preferred_tool,
    system_prompt,
)
from eva_dashboard.db import connect, init_db


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed() -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, payload_json, updated_at) "
            "VALUES ('Eva VTF Pouch', 'Eva Consumer', 'Eva VTF', 'Pouch (ghee)', "
            "'{}', datetime('now'))"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "Alpha Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("2", "Beta Dist", "Eva Distributors", "Karachi", "Karachi"),
            ],
        )
        rows = [
            ("2026-04-05", "Alpha Dist", 20.0),
            ("2026-05-05", "Alpha Dist", 20.0),
            ("2026-06-05", "Alpha Dist", 20.0),
            ("2026-01-05", "Alpha Dist", 10.0),
            ("2026-02-05", "Alpha Dist", 10.0),
            ("2026-03-05", "Alpha Dist", 10.0),
            ("2026-04-05", "Beta Dist", 11.0),
            ("2026-05-05", "Beta Dist", 11.0),
            ("2026-06-05", "Beta Dist", 11.0),
            ("2026-01-05", "Beta Dist", 10.0),
            ("2026-02-05", "Beta Dist", 10.0),
            ("2026-03-05", "Beta Dist", 10.0),
        ]
        for i, (dt, party, mt) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, 'Eva VTF Pouch',
                          ?, 'MT', ?, 'Eva Distributors', '{}')
                """,
                (f"ag-{i}", dt, party, mt, mt),
            )
        conn.commit()


def test_least_ams_gains_inference_not_grown_only() -> None:
    q = "which distributors have the least AMS gains"


def test_least_ams_gains_model_args_trusted() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            q = "which distributors have the least AMS gains"
            # Model-style args (what the taught AI should send)
            out = _dispatch_tool(
                "analyze_parties",
                {
                    "metric": "ams_growth",
                    "sort": "asc",
                    "grown_only": False,
                    "client_type": "Eva Distributors",
                },
                user_text=q,
            )
            assert out["ok"] is True
            assert out["metric"] == "ams_growth"
            md = out["answer_markdown"]
            assert "Smallest AMS gains" in md
            assert "Biggest AMS gains" not in md
            assert "grown only" not in md.lower()
            # Ascending: smallest gain first (Beta ~10% < Alpha ~100%)
            parties = [p["party"] for p in out["parties"]]
            assert parties[0] == "Beta Dist"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_system_prompt_teaches_vocab_and_tools() -> None:
    text = system_prompt()
    assert "VOCABULARY" in text
    assert "TOOL GUIDE" in text
    assert "analyze_parties" in text
    low = text.lower()
    assert (
        "least/lowest" in low
        or "least / lowest" in low
        or "sort_order" in low
        or "sort=asc" in text
        or "sort_order=asc" in low
    )
    assert "plan" in low or "semantic planner" in low
