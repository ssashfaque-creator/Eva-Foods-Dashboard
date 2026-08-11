"""Answer-level golden eval — execute QuerySpecs and assert numbers/structure."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from eva_dashboard.db import connect, init_db
from eva_dashboard.query_executor import execute_query_spec

GOLDEN_PATH = Path(__file__).resolve().parent / "eval_answer_golden.json"


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed_answer_bank() -> None:
    """Shared fixture for answer goldens (Alpha Dist / Lahore / Eva Consumer)."""
    init_db()
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, '{}', datetime('now'))",
            [
                ("P1", "Eva Consumer", "Eva Canola", "Stand up"),
                ("P2", "Eva Consumer", "Eva Cooking", "Tin"),
                ("P3", "Eva Bulk", "Eva Bulk", "Tin"),
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES ('1', 'Alpha Dist', 'Eva Distributors', 'Lahore', 'Lahore', '', "
            "'{}', datetime('now'))"
        )
        rows = [
            ("2026-04-05", "P1", 10, 100),
            ("2026-05-05", "P1", 12, 105),
            ("2026-06-05", "P1", 14, 110),
            ("2026-07-01", "P1", 18, 115),
            ("2026-07-10", "P2", 6, 90),
            ("2026-07-20", "P1", 4, 120),
        ]
        for i, (dt, prod, mt, rate) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, rate, incl_gst_fed_amount, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, 'Alpha Dist', ?,
                          ?, 'MT', ?, ?, ?, 'Eva Distributors', '{}')
                """,
                (f"ag-{i}", dt, prod, mt, mt, rate, mt * rate),
            )
        conn.commit()


def _matrix_total_mt(result: dict[str, Any]) -> float | None:
    matrix = result.get("matrix") or {}
    rows = list(matrix.get("rows") or [])
    for row in reversed(rows):
        if str(row.get("business_unit") or row.get("label") or "").lower() in {
            "total",
            "grand total",
        } or row.get("is_total"):
            for key in ("total", "Total", "mt", "volume_mt", "Average"):
                if key in row and isinstance(row[key], (int, float)):
                    return float(row[key])
    # Fallback: sum numeric total column
    total = 0.0
    found = False
    for row in rows:
        if row.get("is_total"):
            continue
        val = row.get("total")
        if isinstance(val, (int, float)):
            total += float(val)
            found = True
    return total if found else None


def _check_case(case: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    cid = case.get("id") or "?"
    out = execute_query_spec(
        dict(case.get("plan") or {}),
        prior=case.get("prior"),
        user_text=str(case.get("user_text") or ""),
    )
    expect = dict(case.get("expect") or {})

    if expect.get("ok") is True and not out.get("ok"):
        failures.append(f"{cid}: expected ok, got {out.get('error') or out}")
        return failures
    if expect.get("ok") is False and out.get("ok"):
        failures.append(f"{cid}: expected ok=False, got success")

    if expect.get("has_plan_errors"):
        if not out.get("plan_errors"):
            failures.append(f"{cid}: expected plan_errors, got none")

    if "mode" in expect and out.get("mode") != expect["mode"]:
        failures.append(f"{cid}: mode {out.get('mode')!r} != {expect['mode']!r}")

    if "party" in expect and out.get("party") != expect["party"]:
        failures.append(f"{cid}: party {out.get('party')!r} != {expect['party']!r}")

    if "volume_mt" in expect:
        got = out.get("volume_mt")
        if got != expect["volume_mt"]:
            failures.append(f"{cid}: volume_mt {got!r} != {expect['volume_mt']!r}")

    if "last_sale" in expect and out.get("last_sale") != expect["last_sale"]:
        failures.append(
            f"{cid}: last_sale {out.get('last_sale')!r} != {expect['last_sale']!r}"
        )

    qs = out.get("query_spec") or {}
    filters = dict(qs.get("filters") or {})
    if "filters_party" in expect and filters.get("party") != expect["filters_party"]:
        failures.append(
            f"{cid}: filters.party {filters.get('party')!r} != {expect['filters_party']!r}"
        )
    if "filters_city" in expect and filters.get("city") != expect["filters_city"]:
        failures.append(
            f"{cid}: filters.city {filters.get('city')!r} != {expect['filters_city']!r}"
        )

    if "operation" in expect and qs.get("operation") != expect["operation"]:
        failures.append(
            f"{cid}: operation {qs.get('operation')!r} != {expect['operation']!r}"
        )

    if "metrics_include" in expect:
        metrics = set(qs.get("metrics") or [])
        for m in expect["metrics_include"]:
            if m not in metrics:
                failures.append(f"{cid}: missing metric {m!r} in {sorted(metrics)}")

    if "business_units_include" in expect:
        bus = set(qs.get("business_units") or filters.get("business_units") or [])
        for b in expect["business_units_include"]:
            if b not in bus and filters.get("business_unit") != b:
                failures.append(f"{cid}: missing business_unit {b!r} in {bus}")

    if "query_state_party" in expect:
        state = out.get("query_state") or {}
        party = (state.get("party_scope") or {}).get("party") or (
            (state.get("filters") or {}).get("party")
        )
        if party != expect["query_state_party"]:
            failures.append(
                f"{cid}: query_state party {party!r} != {expect['query_state_party']!r}"
            )

    if "contains" in expect:
        md = out.get("answer_markdown") or ""
        for needle in expect["contains"]:
            if needle not in md:
                failures.append(f"{cid}: answer missing {needle!r}")

    if "matrix_total_mt_min" in expect:
        total = _matrix_total_mt(out)
        if total is None or total < float(expect["matrix_total_mt_min"]):
            failures.append(
                f"{cid}: matrix total {total!r} < {expect['matrix_total_mt_min']!r}"
            )

    return failures


def test_answer_golden_bank() -> None:
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    cases = list(data.get("cases") or [])
    assert cases, "eval_answer_golden.json has no cases"

    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed_answer_bank()
            failures: list[str] = []
            for case in cases:
                failures.extend(_check_case(case))
            assert not failures, "Answer golden failures:\n" + "\n".join(failures)
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
