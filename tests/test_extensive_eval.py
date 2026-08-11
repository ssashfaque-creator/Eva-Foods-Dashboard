"""Broad offline eval bank — routing + dispatch smoke across key ask families."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any

from eva_dashboard.chatbot import (
    FOLLOWUP_MARKER,
    _dispatch_tool,
    resolve_forced_tool,
)
from eva_dashboard.db import connect, init_db
from eva_dashboard.sales_query import query_sales


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
                ("Maan Canola Oil", "Maan Consumer", "Maan Canola", "Stand up"),
                ("Eva VTF Banaspati 16 Kg Tin", "Eva Bulk", "Eva VTF", "16 ltr / 16 Kg"),
                ("Eva Canola Oil (5 Ltr Bottle)", "Eva Consumer", "Eva Canola", "Pet bottle"),
            ],
        )
        clients = [
            ("1", "Alpha Dist", "Eva Distributors", "Karachi"),
            ("2", "Gamma Dist", "Eva Distributors", "Karachi"),
            ("3", "Beta Store", "Imtiaz Store", "Lahore"),
            ("4", "Metro Khi", "METRO HABIB", "Karachi"),
            ("5", "Chase Up Gul", "CHASE UP", "Karachi"),
            ("6", "Epsilon Dist", "Eva Distributors", "Lahore"),
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [(i, n, t, c, c) for i, n, t, c in clients],
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO factor_costs
            (client_type, prod_id, product, unit, product_cost, packing_cost,
             total_factor_cost, updated_at)
            VALUES ('Eva Distributors', 1, 'Eva Canola Oil (StandUpPouch)', 'Ltrs',
                    100, 50, 150.0, datetime('now'))
            """
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
            ("2026-07-08", "Beta Store", "Eva VTF Banaspati 16 Kg Tin", 25, "Imtiaz Store"),
            ("2026-07-09", "Metro Khi", "Eva Canola Oil (StandUpPouch)", 18, "METRO HABIB"),
            ("2026-07-10", "Chase Up Gul", "Eva Canola Oil (StandUpPouch)", 14, "CHASE UP"),
            ("2026-07-11", "Epsilon Dist", "Eva Canola Oil (StandUpPouch)", 15, "Eva Distributors"),
            ("2026-07-12", "Alpha Dist", "Eva VTF Banaspati 16 Kg Tin", 10, "Eva Distributors"),
            ("2025-07-05", "Alpha Dist", "Eva VTF Banaspati 16 Kg Tin", 4, "Eva Distributors"),
            ("2026-07-15", "Alpha Dist", "Eva Canola Oil (5 Ltr Bottle)", 6, "Eva Distributors"),
        ]
        for i, (dt, party, prod, mt, ct) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, ?, ?, '{}')
                """,
                (f"ee-{i}", dt, party, prod, mt, mt, 500.0, mt * 500000, ct),
            )
        conn.commit()


def test_extensive_offline_eval_bank() -> None:
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
            assert base["ok"] is True
            prior = base["table_spec"]
            failures: list[str] = []

            def run(
                q: str,
                *,
                forced_exp: str,
                tool: str,
                prior_spec: dict[str, Any] | None = None,
                prior_price_spec: dict[str, Any] | None = None,
                args: dict[str, Any] | None = None,
            ) -> dict[str, Any]:
                is_fu = "[FOLLOW-UP" in q
                forced = resolve_forced_tool(
                    q,
                    prior_table_spec=prior_spec if is_fu or prior_spec is prior else prior_spec,
                    explicit_followup=is_fu,
                )
                if forced_exp != "required" and forced != forced_exp:
                    raise AssertionError(f"forced={forced} expected={forced_exp}")
                t0 = time.time()
                out = _dispatch_tool(
                    tool,
                    args or {},
                    user_text=q,
                    prior_spec=prior_spec,
                    prior_price_spec=prior_price_spec,
                )
                elapsed = time.time() - t0
                if elapsed >= 2.5:
                    raise AssertionError(f"too slow ({elapsed:.2f}s)")
                if out.get("ok") is not True:
                    raise AssertionError(out.get("error") or str(out)[:200])
                return out

            try:
                out = run(
                    "show me Karachi distributor sales",
                    forced_exp="required",
                    tool="query_sales",
                )
                assert "Karachi" in (out.get("answer_markdown") or "")
            except Exception as exc:
                failures.append(f"karachi sales: {exc}")

            try:
                q = f"{FOLLOWUP_MARKER}\n\nwhich distributors are selling maan"
                out = run(
                    q,
                    forced_exp="required",
                    tool="list_clients",
                    prior_spec=prior,
                )
                assert out.get("filters", {}).get("business_unit") == "Maan Consumer"
                assert out.get("filters", {}).get("city") == "Karachi"
                assert out.get("row_dimension") == "party"
                assert out.get("column_dimension") == "month"
                md = out.get("answer_markdown") or ""
                assert "Gamma Dist" in md
            except Exception as exc:
                failures.append(f"reply selling maan: {exc}")

            try:
                out = run(
                    "which distributors have grown VTF sales since last year",
                    forced_exp="required",
                    tool="analyze_parties",
                    args={
                        "metric": "ams_growth",
                        "oil_type": "Eva VTF",
                        "client_type": "Eva Distributors",
                        "grown_only": True,
                    },
                )
                assert out.get("metric") == "ams_growth"
                md = out.get("answer_markdown") or ""
                assert "AMS" in md
                assert "AMS current" in md or "AMS growth" in md or "AMS gains" in md
            except Exception as exc:
                failures.append(f"grown VTF: {exc}")

            try:
                out = run(
                    "show individual distributor sales for VTF with growth "
                    "vs AMS and VS last year",
                    forced_exp="required",
                    tool="analyze_parties",
                    args={
                        "metric": "yoy_ams",
                        "oil_type": "Eva VTF",
                        "client_type": "Eva Distributors",
                    },
                )
                assert out.get("metric") == "yoy_ams"
                md = out.get("answer_markdown") or ""
                assert "YoY" in md and ("AMS" in md or "% vs AMS" in md)
            except Exception as exc:
                failures.append(f"yoy+ams: {exc}")

            try:
                out = run(
                    "Compare Lahore vs Karachi vs Islamabad for July",
                    forced_exp="required",
                    tool="advanced_query",
                    args={
                        "mode": "compare_cities",
                        "entities": ["Lahore", "Karachi", "Islamabad"],
                        "period": "July",
                    },
                )
                assert len(out.get("entities") or []) == 3
            except Exception as exc:
                failures.append(f"3-city: {exc}")

            try:
                out = run(
                    "Imtiaz vs Metro vs Chase Up this month",
                    forced_exp="required",
                    tool="advanced_query",
                    args={
                        "mode": "compare_client_types",
                        "entities": ["Imtiaz Store", "METRO HABIB", "CHASE UP"],
                        "period": "this month",
                    },
                )
                assert len(out.get("entities") or []) == 3
                names = {e.get("name") for e in (out.get("entities") or [])}
                assert names == {"Imtiaz Store", "METRO HABIB", "CHASE UP"}
            except Exception as exc:
                failures.append(f"3-type: {exc}")

            try:
                out = run(
                    "can you show product wise",
                    forced_exp="required",
                    tool="query_sales",
                    prior_spec=prior,
                )
                assert out.get("row_dimension") == "packing_category"
            except Exception as exc:
                failures.append(f"product wise: {exc}")

            try:
                pack_prior = dict(prior)
                pack_prior["row_dimension"] = "packing_category"
                out = run(
                    "SKU wise",
                    forced_exp="required",
                    tool="query_sales",
                    prior_spec=pack_prior,
                )
                assert out.get("row_dimension") == "product"
            except Exception as exc:
                failures.append(f"sku wise: {exc}")

            try:
                out = run(
                    "Canola standup price for Distributors in July",
                    forced_exp="required",
                    tool="query_price",
                )
                assert "Avg Rate" in (out.get("answer_markdown") or "")
            except Exception as exc:
                failures.append(f"avg rate: {exc}")

            try:
                price_prior = {
                    "filters": {
                        "client_type": "Eva Distributors",
                        "oil_type": "Eva Canola",
                        "packing_category": "Stand up",
                    },
                    "period_phrase": "July 2026",
                    "period": {
                        "date_from": "2026-07-01",
                        "date_to": "2026-07-31",
                        "label": "July 2026",
                    },
                }
                out = run(
                    "what's the Price Fetch?",
                    forced_exp="required",
                    tool="query_price",
                    prior_price_spec=price_prior,
                )
                md = out.get("answer_markdown") or ""
                assert "Price Fetch" in md
                assert "Cost Factor" in md
            except Exception as exc:
                failures.append(f"price fetch+factor: {exc}")

            try:
                out = run(
                    "show factor breakdown for distributors canola standup",
                    forced_exp="required",
                    tool="query_price",
                )
                assert out.get("mode") == "factor_costs"
                assert "Packing Cost" in (out.get("answer_markdown") or "")
            except Exception as exc:
                failures.append(f"factor breakdown: {exc}")

            try:
                out = run(
                    "which Chase Up is active in Karachi",
                    forced_exp="required",
                    tool="list_clients",
                )
                names = [c["client"] for c in out.get("clients") or []]
                assert "Chase Up Gul" in names
            except Exception as exc:
                failures.append(f"chase up active: {exc}")

            assert not failures, "Eval failures:\n" + "\n".join(failures)
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_reply_selling_maan_fast_and_filtered() -> None:
    """Regression: Reply + selling maan must not hang and must filter BU."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed()
            with connect() as conn:
                extra = [
                    (
                        str(1000 + i),
                        f"Noise Dist {i}",
                        "Eva Distributors",
                        "Karachi",
                        "Karachi",
                    )
                    for i in range(200)
                ]
                conn.executemany(
                    "INSERT OR REPLACE INTO clients "
                    "(client_id, client, type, city_filter, city, inactive, "
                    "payload_json, updated_at) VALUES "
                    "(?, ?, ?, ?, ?, '', '{}', datetime('now'))",
                    extra,
                )
                conn.commit()

            prior = query_sales(
                city="Karachi",
                client_type="Eva Distributors",
                columns="month",
                months_back=6,
            )["table_spec"]
            q = f"{FOLLOWUP_MARKER}\n\nwhich distributors are selling maan"
            assert (
                resolve_forced_tool(q, prior_table_spec=prior, explicit_followup=True)
                == "required"
            )
            t0 = time.time()
            out = _dispatch_tool("list_clients", {}, user_text=q, prior_spec=prior)
            assert time.time() - t0 < 1.5
            assert out["ok"] is True
            assert out["filters"]["business_unit"] == "Maan Consumer"
            assert out["filters"]["city"] == "Karachi"
            assert out["row_dimension"] == "party"
            assert out["column_dimension"] == "month"
            md = out.get("answer_markdown") or ""
            assert "Gamma Dist" in md
            assert "Alpha Dist" in md
            assert "Noise Dist" not in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
