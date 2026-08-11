"""Incl GST price per SKU unit (not per kg) for Price Fetch."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.data import (
    incl_gst_per_unit,
    rate_unit_count,
    unit_pack_size,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.sales_query import query_price


def test_rate_unit_count_matches_qty_when_basic_aligns() -> None:
    # BakeRight sample: 187 Ctn × 6496 = 1,214,752
    assert rate_unit_count(187, 6496, 1_214_752) == 187.0


def test_rate_unit_count_from_basic_when_qty_is_cartons() -> None:
    # Maan sample: Qty shown as 30 Ctn but Rate is per 5 Ltr unit
    # 120 × 2141.53 ≈ 256,983.6
    units = rate_unit_count(30, 2141.53, 256_983.6)
    assert units is not None
    assert abs(units - 120.0) < 0.05


def test_unit_pack_size_kg_and_ltr() -> None:
    size, measure = unit_pack_size(2992, "Kgs", 187)
    assert size == 16.0
    assert measure == "Kgs"
    size, measure = unit_pack_size(600, "Ltrs", 120)
    assert size == 5.0
    assert measure == "Ltrs"


def test_incl_gst_per_unit_bakeright() -> None:
    # 1,433,407 / 187 ≈ 7665.28
    assert abs(incl_gst_per_unit(1_433_407, 187) - 7665.27807486631) < 0.01


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def test_price_fetch_shows_incl_gst_per_unit_not_per_kg() -> None:
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
                    "('BakeRight Shortening 16 Kgs Ctn', 'Shortening', "
                    "'Shortening', 'Ctn', '{}', datetime('now'))"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO category "
                    "(product, category_1, category_2, packing_category, "
                    "payload_json, updated_at) VALUES "
                    "('Maan Cooking Oil 5 Ltr Pet Bottle', 'Maan Consumer', "
                    "'Maan Oil', 'Pet bottle', '{}', datetime('now'))"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO clients "
                    "(client_id, client, type, city_filter, city, inactive, "
                    "payload_json, updated_at) VALUES "
                    "('1', 'ISMAIL INDUSTRIES', 'Oil Clients', 'Karachi', "
                    "'Karachi', '', '{}', datetime('now'))"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO clients "
                    "(client_id, client, type, city_filter, city, inactive, "
                    "payload_json, updated_at) VALUES "
                    "('2', 'Ahmed Kalim', 'Eva Distributors', 'Jhelum', "
                    "'Jhelum', '', '{}', datetime('now'))"
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO factor_costs
                    (client_type, prod_id, product, unit, product_cost,
                     packing_cost, total_factor_cost, updated_at)
                    VALUES ('Oil Clients', 1,
                            'BakeRight Shortening 16 Kgs Ctn', 'Kgs',
                            400, 20, 420.0, datetime('now'))
                    """
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO factor_costs
                    (client_type, prod_id, product, unit, product_cost,
                     packing_cost, total_factor_cost, updated_at)
                    VALUES ('Eva Distributors', 2,
                            'Maan Cooking Oil 5 Ltr Pet Bottle', 'Ltrs',
                            100, 50, 150.0, datetime('now'))
                    """
                )
                # BakeRight: 1 unit = 16 Kgs
                conn.execute(
                    """
                    INSERT INTO sales (
                      source_file_id, row_hash, imported_at, date, party,
                      product, qty, unit, mes_qty, mes_unit, mt_qty, rate,
                      basic_amount, incl_gst_fed_amount, client_type,
                      payload_json
                    ) VALUES (
                      NULL, 'pu-br', datetime('now'), '2026-07-09',
                      'ISMAIL INDUSTRIES',
                      'BakeRight Shortening 16 Kgs Ctn',
                      187, 'Ctn', 2992, 'Kgs', 2.992, 6496,
                      1214752, 1433407, 'Oil Clients', '{}'
                    )
                    """
                )
                # Maan: Qty=30 cartons but Rate per 5 Ltr unit (120 units)
                conn.execute(
                    """
                    INSERT INTO sales (
                      source_file_id, row_hash, imported_at, date, party,
                      product, qty, unit, mes_qty, mes_unit, mt_qty, rate,
                      basic_amount, incl_gst_fed_amount, client_type,
                      payload_json
                    ) VALUES (
                      NULL, 'pu-maan', datetime('now'), '2026-07-29',
                      'Ahmed Kalim',
                      'Maan Cooking Oil 5 Ltr Pet Bottle',
                      30, 'Ctn', 600, 'Ltrs', 0.549, 2141.53,
                      256983.6, 303240, 'Eva Distributors', '{}'
                    )
                    """
                )
                conn.commit()

            br = query_price(
                period="July 2026",
                product="BakeRight Shortening 16 Kgs Ctn",
                include_price_fetch=True,
            )
            assert br["ok"] is True, br
            assert br["pack_label"] == "16 Kgs"
            assert br["incl_gst_per_unit"] is not None
            assert abs(br["incl_gst_per_unit"] - 7665.28) < 0.05
            assert br["cost_unit"] == "Kgs"
            assert br["price_fetch"] is not None
            md = br["answer_markdown"]
            assert "Incl GST / unit (16 Kgs)" in md
            assert "Cost Factor (Kgs)" in md
            assert "Price Fetch" in md
            assert "Amount / kg" not in md
            assert "per kg (Incl" not in md

            maan = query_price(
                period="July 2026",
                product="Maan Cooking Oil 5 Ltr Pet Bottle",
                include_price_fetch=True,
            )
            assert maan["ok"] is True, maan
            assert maan["pack_label"] == "5 Ltrs"
            assert maan["incl_gst_per_unit"] is not None
            # 303240 / 120 = 2527
            assert abs(maan["incl_gst_per_unit"] - 2527.0) < 0.05
            assert maan["cost_unit"] == "Ltrs"
            md2 = maan["answer_markdown"]
            assert "Incl GST / unit (5 Ltrs)" in md2
            assert "Cost Factor (Ltrs)" in md2
            assert "Amount / kg" not in md2
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
