"""Regressions from eva_chat_training_20260808 export feedback."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.chatbot import (
    FOLLOWUP_MARKER,
    _dispatch_tool,
    _looks_hide_sku,
    _looks_national_scope,
    _looks_same_format,
    resolve_forced_tool,
    resolve_row_dimension_request,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.party_analytics import infer_party_analytics_from_text
from eva_dashboard.sales_query import (
    AMS_GROWTH_COL,
    AMS_PRIOR_3_COL,
    query_sales,
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
                ("Eva Canola Oil (5 Ltr Bottle)", "Eva Consumer", "Eva Canola", "Pet bottle"),
                ("Maan Canola Oil", "Maan Consumer", "Maan Canola", "Stand up"),
                ("Eva VTF Banaspati 16 Kg Tin", "Eva Bulk", "Eva VTF", "16 ltr / 16 Kg"),
                ("Eva VTF Pouch", "Eva Consumer", "Eva VTF", "Pouch (ghee)"),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [
                ("1", "Alpha Dist", "Eva Distributors", "Karachi", "Karachi"),
                ("2", "Gamma Dist", "Eva Distributors", "Karachi", "Karachi"),
                ("3", "Beta Store", "Imtiaz Store", "Lahore", "Lahore"),
                ("4", "Epsilon Dist", "Eva Distributors", "Lahore", "Lahore"),
                ("5", "Zeta Dist", "Eva Distributors", "Islamabad", "Islamabad"),
            ],
        )
        rows = [
            ("2026-03-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30, "Eva Distributors"),
            ("2026-04-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30, "Eva Distributors"),
            ("2026-05-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30, "Eva Distributors"),
            ("2026-06-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30, "Eva Distributors"),
            ("2026-07-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 40, "Eva Distributors"),
            ("2026-04-10", "Gamma Dist", "Maan Canola Oil", 8, "Eva Distributors"),
            ("2026-07-06", "Gamma Dist", "Maan Canola Oil", 12, "Eva Distributors"),
            ("2026-07-07", "Alpha Dist", "Maan Canola Oil", 3, "Eva Distributors"),
            ("2026-07-08", "Alpha Dist", "Eva VTF Banaspati 16 Kg Tin", 10, "Eva Distributors"),
            ("2026-07-09", "Beta Store", "Eva Canola Oil (StandUpPouch)", 18, "Imtiaz Store"),
            ("2026-07-10", "Epsilon Dist", "Eva Canola Oil (StandUpPouch)", 15, "Eva Distributors"),
            ("2026-07-11", "Zeta Dist", "Eva VTF Pouch", 20, "Eva Distributors"),
            ("2025-07-11", "Zeta Dist", "Eva VTF Pouch", 5, "Eva Distributors"),
            ("2026-04-11", "Zeta Dist", "Eva VTF Pouch", 8, "Eva Distributors"),
            ("2026-05-11", "Zeta Dist", "Eva VTF Pouch", 8, "Eva Distributors"),
            ("2026-06-11", "Zeta Dist", "Eva VTF Pouch", 8, "Eva Distributors"),
            ("2026-02-11", "Zeta Dist", "Eva VTF Pouch", 4, "Eva Distributors"),
            ("2026-03-11", "Zeta Dist", "Eva VTF Pouch", 4, "Eva Distributors"),
            ("2026-01-11", "Zeta Dist", "Eva VTF Pouch", 4, "Eva Distributors"),
        ]
        for i, (dt, party, prod, mt, ct) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, ?, ?, '{}')
                """,
                (f"tr-{i}", dt, party, prod, mt, mt, 500.0, mt * 500000, ct),
            )
        conn.commit()


def test_hide_sku_and_include_bulk_keep_packing() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            assert _looks_hide_sku("don't show individual sku")
            assert (
                resolve_row_dimension_request(
                    "don't show individual sku", prior_row_dimension="product"
                )
                == "packing_category"
            )
            pack = query_sales(
                city="Karachi",
                client_type="Eva Distributors",
                columns="month",
                months_back=6,
                business_units=["Eva Consumer"],
                row_dimension="packing_category",
            )
            assert pack["ok"] is True
            prior = pack["table_spec"]
            assert prior["row_dimension"] == "packing_category"

            # include Eva Bulk must NOT jump to SKU
            out = _dispatch_tool(
                "query_sales",
                {"row_dimension": "product"},
                user_text="include Eva Bulk",
                prior_spec=prior,
            )
            assert out["ok"] is True
            assert out["row_dimension"] == "packing_category"
            assert set(out.get("business_units") or []) >= {
                "Eva Consumer",
                "Eva Bulk",
            }

            # escalate to SKU then hide it
            sku = _dispatch_tool(
                "query_sales",
                {},
                user_text="SKU wise",
                prior_spec=out["table_spec"],
            )
            assert sku["row_dimension"] == "product"
            hide = _dispatch_tool(
                "query_sales",
                {"row_dimension": "product"},
                user_text="don't show individual sku",
                prior_spec=sku["table_spec"],
            )
            assert hide["ok"] is True
            assert hide["row_dimension"] == "packing_category"
            assert resolve_forced_tool(
                "don't show individual sku",
                prior_table_spec=sku["table_spec"],
            ) == "query_sales"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_exclude_maan_product_keeps_months_and_eva_bulk() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            base = query_sales(
                city="Karachi",
                client_type="Eva Distributors",
                columns="month",
                months_back=6,
            )
            prior = base["table_spec"]
            q = f"{FOLLOWUP_MARKER}\n\nbreak down by product and exclude maan"
            out = _dispatch_tool(
                "query_sales", {}, user_text=q, prior_spec=prior
            )
            assert out["ok"] is True
            assert out["row_dimension"] == "packing_category"
            assert out["column_dimension"] == "month"
            ex = (out.get("excludes") or {}).get("business_unit") or []
            assert "Maan Consumer" in ex
            names = {
                str(r.get("business_unit") or "")
                for r in (out.get("matrix") or {}).get("rows") or []
                if r.get("business_unit")
            }
            assert "Eva Consumer" in names
            assert "Eva Bulk" in names
            assert "Maan Consumer" not in names
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_selling_maan_party_month_matrix() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            prior = query_sales(
                city="Karachi",
                client_type="Eva Distributors",
                columns="month",
                months_back=6,
            )["table_spec"]
            q = "what distributors are selling maan"
            assert (
                resolve_forced_tool(q, prior_table_spec=prior, explicit_followup=True)
                == "required"
            )
            out = _dispatch_tool(
                "list_clients", {}, user_text=q, prior_spec=prior
            )
            assert out["ok"] is True
            assert out["row_dimension"] == "party"
            assert out["column_dimension"] == "month"
            assert out["filters"]["business_unit"] == "Maan Consumer"
            assert out["filters"]["city"] == "Karachi"
            md = out.get("answer_markdown") or ""
            assert "Gamma Dist" in md
            assert "AMS prior" in md or AMS_PRIOR_3_COL in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_same_format_preserves_packing_for_imtiaz() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            assert _looks_same_format(
                "can you show Imtiaz sale in lahore in the same format"
            )
            pack = query_sales(
                city="Lahore",
                client_type="Eva Distributors",
                columns="month",
                months_back=6,
                row_dimension="packing_category",
            )
            prior = pack["table_spec"]
            q = "can you show Imtiaz sale in lahore in the same format"
            out = _dispatch_tool(
                "query_sales",
                {"row_dimension": "business_unit"},
                user_text=q,
                prior_spec=prior,
            )
            assert out["ok"] is True
            assert out["row_dimension"] == "packing_category"
            assert out["filters"]["client_type"] == "Imtiaz Store"
            assert out["filters"]["city"] == "Lahore"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_pakistan_growth_clears_city_and_ranks_ams_growth() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            assert _looks_national_scope(
                "which distributors have grown vtf sales the most all over pakistan"
            )
            q = (
                "can you compare which distributors have grown vtf sales "
                "the most all over pakistan"
            )
            inf = infer_party_analytics_from_text(q)
            assert inf["metric"] == "ams_growth"
            assert inf["oil_type"] == "Eva VTF"
            prior = {
                "filters": {
                    "city": "Lahore",
                    "client_type": "Eva Distributors",
                },
                "column_dimension": "month",
                "row_dimension": "packing_category",
            }
            out = _dispatch_tool(
                "analyze_parties",
                {"period": "July 2026"},
                user_text=q,
                prior_spec=prior,
            )
            assert out["ok"] is True
            assert out["metric"] == "ams_growth"
            assert out["filters"].get("city") is None
            assert out["filters"].get("business_unit") is None
            assert out["filters"]["oil_type"] == "Eva VTF"
            md = out.get("answer_markdown") or ""
            assert "AMS growth" in md or "AMS gains" in md
            assert "AMS current (" in md
            assert "Zeta Dist" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_month_matrix_has_prior_ams_and_growth() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            out = query_sales(
                city="Karachi",
                client_type="Eva Distributors",
                columns="month",
                months_back=6,
            )
            assert out["ok"] is True
            cols = (out.get("matrix") or {}).get("columns") or []
            assert AMS_PRIOR_3_COL in cols
            assert AMS_GROWTH_COL in cols
            md = out.get("answer_markdown") or ""
            assert "AMS prior" in md
            assert "AMS growth" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
