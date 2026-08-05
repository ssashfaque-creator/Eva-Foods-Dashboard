"""Tests for spoken product language → exact SKU resolution."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import system_prompt
from eva_dashboard.db import connect, init_db
from eva_dashboard.product_language import resolve_product_language


def _seed_categories(products: list[tuple[str, str, str]]) -> None:
    init_db()
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, payload_json, updated_at) "
            "VALUES (?, ?, ?, '{}', datetime('now'))",
            products,
        )
        conn.commit()


def test_resolve_core_aliases() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")
        try:
            _seed_categories(
                [
                    ("Eva Canola Oil (StandUpPouch)", "Eva Consumer", "Canola"),
                    ("Eva VTF Banaspati 16 Kg Tin", "Eva Consumer", "VTF"),
                    ("Eva VTF Banaspati 1x5 Pouch", "Eva Consumer", "VTF"),
                    ("BakeRight Shortening 16 Kgs Ctn", "Shortening", ""),
                    ("Cuisine King (16 Ltr Tin)", "Cusine King", ""),
                    ("Maan Banaspati 16 Kgs Tin", "Maan Bulk", "Ghee"),
                    ("Maan Cooking Oil 16 Ltrs. Tin", "Maan Bulk", "Oil"),
                    ("Eva Cooking Oil 1x5 Pillow Pouch", "Eva Consumer", "Cooking"),
                    ("Eva Sunflower Oil 5 Ltr Pet Bottle", "Eva Consumer", "Sunflower"),
                ]
            )

            cases = [
                ("vtf bulk", "Eva VTF Banaspati 16 Kg Tin"),
                ("canola standup pouch", "Eva Canola Oil (StandUpPouch)"),
                ("flagship", "Eva Canola Oil (StandUpPouch)"),
                ("shortening", "BakeRight Shortening 16 Kgs Ctn"),
                ("cusine king", "Cuisine King (16 Ltr Tin)"),
                ("cooking pillow", "Eva Cooking Oil 1x5 Pillow Pouch"),
                ("sun 5 ltr pet", "Eva Sunflower Oil 5 Ltr Pet Bottle"),
                ("vtf pouch", "Eva VTF Banaspati 1x5 Pouch"),
                ("maan 16 kg tin", "Maan Banaspati 16 Kgs Tin"),
            ]
            for phrase, expected in cases:
                result = resolve_product_language(phrase, limit=5)
                assert result["top_product"] == expected, (
                    f"{phrase!r} → {result['top_product']!r}, expected {expected!r}"
                )
                top = result["matches"][0]
                assert top.get("category1") is not None
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_16_ltr_vs_16_kg_rule() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")
        try:
            _seed_categories(
                [
                    ("Maan Banaspati 16 Kgs Tin", "Maan Bulk", "Ghee"),
                    ("Maan Cooking Oil 16 Ltrs. Tin", "Maan Bulk", "Oil"),
                    ("Eva Cooking Oil (16 Ltr Tin)", "Eva Consumer", "Cooking"),
                    ("Eva Canola Oil 16 Ltr Tin", "Eva Consumer", "Canola"),
                ]
            )
            maan_kg = resolve_product_language("maan 16 kg", limit=5)
            assert "Banaspati" in (maan_kg["top_product"] or "")
            assert "Cooking" not in (maan_kg["top_product"] or "")

            maan_ltr = resolve_product_language("maan 16 ltr tin", limit=5)
            assert "Cooking Oil" in (maan_ltr["top_product"] or "")

            eva_ltr = resolve_product_language("eva cooking 16 tin", limit=5)
            assert eva_ltr["top_product"] == "Eva Cooking Oil (16 Ltr Tin)"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_system_prompt_includes_product_glossary() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")
        try:
            init_db()
            text = system_prompt()
            assert "PRODUCT LANGUAGE GLOSSARY" in text
            assert "VTF bulk" in text or "vtf bulk" in text
            assert "markdown TABLES" in text or "markdown tables" in text.lower()
            assert "resolve_product_language" in text
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
