"""Stacked metric cuts + last-N-months vs same span last year (calendar YoY).

Covers a class of advanced party analytics — not a one-query hardcoded path.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from eva_dashboard.db import connect, init_db
from eva_dashboard.metric_filters import (
    looks_yoy_period_compare,
    parse_metric_filters,
)
from eva_dashboard.playbooks import playbook_ids
from eva_dashboard.query_executor import (
    _coerce_vocab_from_user_text,
    _looks_yoy_compare,
    execute_query_spec,
)
from eva_dashboard.query_spec import normalize_query_spec
from eva_dashboard.tools.intent_router import route_ask, tool_allowed


EXAMPLE_Q = (
    "show me all distributors with sales more than 10 MT but less than "
    "5 % growth in AMS last 3 months vs the same 3 months last year"
)
ALT_Q = (
    "customers with volume over 25 MT and yoy below 0 last 6 months "
    "vs the same 6 months last year"
)
LOWEST_YOY_Q = (
    "show me the all distributors with the lowest growth in ams last 6 months "
    "vs the same months last year"
)
HIGHEST_YOY_Q = (
    "which customers had the highest growth last 4 months vs the same months last year"
)
AMS_YOY_CUT_Q = (
    "show me all distributors who have growth less than 5% in AMS last 6 months "
    "vs same period last year (only distributors with AMS>10)"
)
AMS_MT_YOY_Q = (
    "show me all distributors with more than 10 MT AMS that have less than "
    "5% growth in ams last 6 months vs the same period last year"
)


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed_yoy_window() -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO category "
            "(product, category_1, category_2, packing_category, "
            "payload_json, updated_at) VALUES "
            "('Prod A', 'Eva Consumer', 'Eva Canola', 'Stand up', '{}', "
            "datetime('now'))"
        )
        parties = (
            ("1", "KeepMe Dist"),
            ("2", "FastGrow Dist"),
            ("3", "Tiny Dist"),
            ("4", "Shrinker Dist"),
            ("5", "WindowStar Dist"),
            ("6", "TrailingOnly Dist"),
        )
        for cid, name in parties:
            conn.execute(
                "INSERT OR REPLACE INTO clients "
                "(client_id, client, type, city_filter, city, inactive, "
                "payload_json, updated_at) VALUES "
                "(?, ?, 'Eva Distributors', 'Lahore', 'Lahore', '', '{}', "
                "datetime('now'))",
                (cid, name),
            )
        # Last 3 months vs max sales date 2026-08-12 → 2026-06-01..2026-08-12
        # KeepMe: 12+12+12=36 vs LY 12+12+11=35 → ~2.9% YoY, volume>10
        # FastGrow: 15*3=45 vs 10*3=30 → 50% YoY
        # Tiny: 2*3=6 vs 2*3=6 → volume below 10
        # Shrinker: 16+16+16=48 vs 40+40+40=120 → -60% YoY
        # Extra Feb–May 2026 so AMS-window growth is defined when a test
        # asks for ams_growth cuts without last-year language.
        rows = []
        for dt, party, mt in (
            ("2026-02-10", "KeepMe Dist", 8.0),
            ("2026-03-10", "KeepMe Dist", 8.0),
            ("2026-04-10", "KeepMe Dist", 8.0),
            ("2026-05-10", "KeepMe Dist", 12.0),
            ("2026-02-10", "FastGrow Dist", 10.0),
            ("2026-03-10", "FastGrow Dist", 10.0),
            ("2026-04-10", "FastGrow Dist", 10.0),
            ("2026-05-10", "FastGrow Dist", 15.0),
            ("2026-06-10", "KeepMe Dist", 12.0),
            ("2026-07-10", "KeepMe Dist", 12.0),
            ("2026-08-10", "KeepMe Dist", 12.0),
            ("2025-06-10", "KeepMe Dist", 12.0),
            ("2025-07-10", "KeepMe Dist", 12.0),
            ("2025-08-10", "KeepMe Dist", 11.0),
            ("2026-06-10", "FastGrow Dist", 15.0),
            ("2026-07-10", "FastGrow Dist", 15.0),
            ("2026-08-10", "FastGrow Dist", 15.0),
            ("2025-06-10", "FastGrow Dist", 10.0),
            ("2025-07-10", "FastGrow Dist", 10.0),
            ("2025-08-10", "FastGrow Dist", 10.0),
            ("2026-06-10", "Tiny Dist", 2.0),
            ("2026-07-10", "Tiny Dist", 2.0),
            ("2026-08-10", "Tiny Dist", 2.0),
            ("2025-06-10", "Tiny Dist", 2.0),
            ("2025-07-10", "Tiny Dist", 2.0),
            ("2025-08-10", "Tiny Dist", 2.0),
            ("2026-06-10", "Shrinker Dist", 16.0),
            ("2026-07-10", "Shrinker Dist", 16.0),
            ("2026-08-10", "Shrinker Dist", 16.0),
            ("2025-06-10", "Shrinker Dist", 40.0),
            ("2025-07-10", "Shrinker Dist", 40.0),
            ("2025-08-10", "Shrinker Dist", 40.0),
            # WindowStar: period AMS of last 6 months > 10, trailing 3-mo AMS ~2,
            # calendar YoY negative. TrailingOnly: opposite (trailing AMS high,
            # period AMS of last 6 months < 10).
            ("2026-03-10", "WindowStar Dist", 30.0),
            ("2026-04-10", "WindowStar Dist", 30.0),
            ("2026-05-10", "WindowStar Dist", 2.0),
            ("2026-06-10", "WindowStar Dist", 2.0),
            ("2026-07-10", "WindowStar Dist", 2.0),
            ("2026-08-10", "WindowStar Dist", 2.0),
            ("2025-03-10", "WindowStar Dist", 20.0),
            ("2025-04-10", "WindowStar Dist", 20.0),
            ("2025-05-10", "WindowStar Dist", 20.0),
            ("2025-06-10", "WindowStar Dist", 20.0),
            ("2025-07-10", "WindowStar Dist", 20.0),
            ("2025-08-10", "WindowStar Dist", 20.0),
            ("2026-05-10", "TrailingOnly Dist", 15.0),
            ("2026-06-10", "TrailingOnly Dist", 15.0),
            ("2026-07-10", "TrailingOnly Dist", 15.0),
            ("2025-05-10", "TrailingOnly Dist", 15.0),
            ("2025-06-10", "TrailingOnly Dist", 15.0),
            ("2025-07-10", "TrailingOnly Dist", 15.0),
        ):
            rows.append((dt, party, mt))
        for i, (dt, party, mt) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, 'Prod A',
                          ?, 'MT', ?, 'Eva Distributors', '{}')
                """,
                (f"cy-{i}", dt, party, mt, mt),
            )
        conn.commit()


def test_parse_stacked_volume_and_yoy_not_ams_growth() -> None:
    got = parse_metric_filters(EXAMPLE_Q)
    assert {"metric": "volume", "op": "gt", "value": 10.0} in got
    assert {"metric": "yoy", "op": "lt", "value": 5.0} in got
    assert not any(f.get("metric") == "ams_growth" for f in got)

    alt = parse_metric_filters(ALT_Q)
    assert {"metric": "volume", "op": "gt", "value": 25.0} in alt
    assert {"metric": "yoy", "op": "lt", "value": 0.0} in alt

    # Without last-year language, "growth" stays AMS-window growth.
    plain = parse_metric_filters("growth more than 30%")
    assert plain == [{"metric": "ams_growth", "op": "gt", "value": 30.0}]

    # "less than 5% in AMS" is growth, never an AMS-tonnage cut (ams < 5).
    cut = parse_metric_filters(AMS_YOY_CUT_Q)
    assert {"metric": "yoy", "op": "lt", "value": 5.0} in cut
    assert {"metric": "ams", "op": "gt", "value": 10.0} in cut
    assert not any(f.get("metric") == "ams" and f.get("op") == "lt" for f in cut)
    assert not any(f.get("metric") == "ams_growth" for f in cut)
    pct_only = parse_metric_filters("growth less than 5% in AMS")
    assert pct_only == [{"metric": "ams_growth", "op": "lt", "value": 5.0}]

    # Unit before AMS is still AMS, not a volume cut ('10 MT AMS').
    mt_ams = parse_metric_filters("more than 10 MT AMS")
    assert mt_ams == [{"metric": "ams", "op": "gt", "value": 10.0}]
    phrasing = parse_metric_filters(AMS_MT_YOY_Q)
    assert {"metric": "ams", "op": "gt", "value": 10.0} in phrasing
    assert {"metric": "yoy", "op": "lt", "value": 5.0} in phrasing
    assert not any(f.get("metric") == "volume" for f in phrasing)


def test_yoy_period_language_is_calendar_not_ams_window() -> None:
    assert looks_yoy_period_compare(EXAMPLE_Q)
    assert looks_yoy_period_compare(ALT_Q)
    assert looks_yoy_period_compare(LOWEST_YOY_Q)
    assert looks_yoy_period_compare(AMS_YOY_CUT_Q)
    assert looks_yoy_period_compare(AMS_MT_YOY_Q)
    assert looks_yoy_period_compare("vs the same months last year")
    assert looks_yoy_period_compare(
        "last 4 months versus the same 4 months last year"
    )
    assert _looks_yoy_compare(EXAMPLE_Q)
    assert not looks_yoy_period_compare("which distributors have grown sales")


def test_coerce_complete_ask_clears_memory_and_ranks_yoy() -> None:
    prior = {
        "filters": {"client_type": "Eva Distributors"},
        "row_dimensions": ["party"],
        "column_dimensions": ["month"],
        "metrics": ["volume", "vs_ams"],
        "period_type": "LAST_N_MONTHS",
        "months_back": 12,
    }
    # Mimic the failed ReAct spec: volume+ams_growth cuts, keep, month grain.
    spec = _coerce_vocab_from_user_text(
        {
            "state_action": "keep",
            "row_dimensions": ["party"],
            "column_dimensions": ["month"],
            "metrics": ["volume", "ams_growth"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 3,
            "filters": {"client_type": "Eva Distributors"},
            "metric_filters": [
                {"metric": "volume", "op": "gt", "value": 10},
                {"metric": "ams_growth", "op": "lt", "value": 5},
            ],
        },
        EXAMPLE_Q,
        prior=prior,
    )
    assert spec.get("intent") == "party_rank"
    assert spec.get("metric") == "yoy"
    assert spec.get("compare") == "yoy"
    assert spec.get("state_action") == "clear"
    assert spec.get("base") == "none"
    assert "month" not in (spec.get("column_dimensions") or [])
    mfs = spec.get("metric_filters") or []
    assert any(f.get("metric") == "volume" and f.get("value") == 10.0 for f in mfs)
    assert any(f.get("metric") == "yoy" and f.get("op") == "lt" for f in mfs)
    assert not any(f.get("metric") == "ams_growth" for f in mfs)
    assert spec.get("limit") == 200
    assert (spec.get("filters") or {}).get("client_type") == "Eva Distributors"


def test_normalize_prefers_yoy_over_volume() -> None:
    spec = normalize_query_spec(
        {
            "row_dimensions": ["party"],
            "metrics": ["volume", "yoy"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 3,
        }
    )
    assert spec.get("metric") == "yoy"
    spec2 = normalize_query_spec(
        {
            "row_dimensions": ["party"],
            "metrics": ["volume", "ams_growth"],
            "period_type": "MTD",
        }
    )
    assert spec2.get("metric") == "ams_growth"


def test_router_and_playbooks_for_stacked_yoy() -> None:
    route = route_ask(EXAMPLE_Q)
    assert route["kind"] == "standard"
    assert "run_standard_analytics_pivot" in (route.get("preferred_tools") or [])
    ok, _ = tool_allowed("execute_read_only_sql", route)
    assert ok is False
    pids = playbook_ids(EXAMPLE_Q)
    assert "yoy_compare" in pids
    assert "compound_metric_rank" in pids
    assert "distributors_grown" not in pids


def test_execute_stacked_volume_and_yoy_filters() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            import eva_dashboard.sales_query as sq

            sq._CLIENTS_CACHE = None
            _seed_yoy_window()
            # Same shape the model actually sent (volume + ams_growth cuts).
            out = execute_query_spec(
                {
                    "state_action": "clear",
                    "row_dimensions": ["party"],
                    "metrics": ["volume", "ams_growth"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 3,
                    "filters": {"client_type": "Eva Distributors"},
                    "metric_filters": [
                        {"metric": "volume", "op": "gt", "value": 10},
                        {"metric": "ams_growth", "op": "lt", "value": 5},
                    ],
                    "limit": 10,
                },
                user_text=EXAMPLE_Q,
            )
            assert out.get("ok"), out.get("error") or out.get("plan_errors")
            qs = out.get("query_spec") or {}
            assert qs.get("metric") == "yoy"
            assert "month" not in (qs.get("column_dimensions") or [])
            names = {
                str(r.get("party") or "")
                for r in (out.get("rows") or out.get("parties") or [])
            }
            md = out.get("answer_markdown") or ""
            blob = md + " " + " ".join(sorted(names))
            assert "KeepMe Dist" in blob
            assert "Shrinker Dist" in blob
            assert "FastGrow Dist" not in blob
            assert "Tiny Dist" not in blob
            # YoY columns must be present so the cut is visible, not empty.
            assert "yoy" in md.lower() or any(
                r.get("yoy_pct") is not None
                for r in (out.get("rows") or out.get("parties") or [])
            )
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_engine_computes_ams_growth_when_filter_needs_it() -> None:
    """Volume rank + ams_growth cut must not drop every row (None growth)."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            import eva_dashboard.sales_query as sq

            sq._CLIENTS_CACHE = None
            _seed_yoy_window()
            out = execute_query_spec(
                {
                    "state_action": "clear",
                    "intent": "party_rank",
                    "row_dimensions": ["party"],
                    "metrics": ["volume"],
                    "metric": "volume",
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 3,
                    "filters": {"client_type": "Eva Distributors"},
                    "metric_filters": [
                        {"metric": "volume", "op": "gt", "value": 10},
                        {"metric": "ams_growth", "op": "lt", "value": 500},
                    ],
                    "limit": 50,
                },
                user_text="distributors with sales more than 10 MT and growth less than 500%",
            )
            assert out.get("ok"), out.get("error") or out.get("plan_errors")
            md = out.get("answer_markdown") or ""
            assert "No results" not in md
            assert "KeepMe Dist" in md or "FastGrow Dist" in md
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_lowest_growth_vs_last_year_is_yoy_not_ams_window() -> None:
    spec = _coerce_vocab_from_user_text(
        {
            "state_action": "clear",
            "row_dimensions": ["party"],
            "metrics": ["yoy"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 6,
            "filters": {"client_type": "Eva Distributors"},
            "sort_order": "asc",
        },
        LOWEST_YOY_Q,
    )
    assert spec.get("metric") == "yoy"
    assert spec.get("compare") == "yoy"
    assert spec.get("sort") == "asc"
    assert spec.get("title_mode") != "smallest_gains"
    assert spec.get("intent") == "party_rank"
    assert "month" not in (spec.get("column_dimensions") or [])
    assert spec.get("limit") == 200
    assert spec.get("metrics") == ["volume", "yoy"]

    high = _coerce_vocab_from_user_text(
        {
            "row_dimensions": ["party"],
            "metrics": ["ams_growth"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 4,
            "filters": {},
        },
        HIGHEST_YOY_Q,
    )
    assert high.get("metric") == "yoy"
    assert high.get("sort") == "desc"


def test_router_lowest_growth_vs_last_year_is_standard() -> None:
    route = route_ask(LOWEST_YOY_Q)
    assert route["kind"] == "standard"
    assert "run_standard_analytics_pivot" in (route.get("preferred_tools") or [])
    ok, _ = tool_allowed("execute_read_only_sql", route)
    assert ok is False
    pids = playbook_ids(LOWEST_YOY_Q)
    assert "yoy_compare" in pids
    assert "distributors_grown" not in pids
    # Rate discovery must still work
    rate = route_ask(
        "what was the lowest rate Pepsi was sold at, and who was the buyer?"
    )
    assert rate["kind"] == "discovery"


def test_execute_lowest_yoy_not_smallest_ams_gains() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            import eva_dashboard.sales_query as sq

            sq._CLIENTS_CACHE = None
            _seed_yoy_window()
            out = execute_query_spec(
                {
                    "state_action": "clear",
                    "row_dimensions": ["party"],
                    "metrics": ["yoy"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "filters": {"client_type": "Eva Distributors"},
                    "sort_order": "asc",
                },
                user_text=LOWEST_YOY_Q,
            )
            assert out.get("ok"), out.get("error") or out.get("plan_errors")
            qs = out.get("query_spec") or {}
            assert qs.get("metric") == "yoy"
            md = out.get("answer_markdown") or ""
            assert "Smallest AMS gains" not in md
            assert "AMS current" not in md
            assert "YoY" in md or "yoy" in md.lower()
            parties = out.get("parties") or out.get("rows") or []
            names = [str(r.get("party") or "") for r in parties]
            assert "Shrinker Dist" in names
            # Lowest calendar YoY first (Shrinker declined vs last year)
            assert names[0] == "Shrinker Dist"
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous


def test_coerce_ams_size_cut_stays_calendar_yoy() -> None:
    """Agent-shaped spec: yoy + AMS>10 must not flip ranking to yoy_ams."""
    spec = _coerce_vocab_from_user_text(
        {
            "state_action": "clear",
            "row_dimensions": ["party"],
            "metrics": ["yoy", "ams"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 6,
            "filters": {"client_type": "Eva Distributors"},
            "metric_filters": [
                {"metric": "yoy", "op": "lt", "value": 5},
                {"metric": "ams", "op": "gt", "value": 10},
            ],
            "compare": "yoy",
        },
        AMS_YOY_CUT_Q,
    )
    assert spec.get("metric") == "yoy"
    assert spec.get("compare") == "yoy"
    assert spec.get("intent") == "party_rank"
    assert spec.get("limit") == 200
    assert "month" not in (spec.get("column_dimensions") or [])
    mfs = spec.get("metric_filters") or []
    assert any(f.get("metric") == "yoy" and f.get("op") == "lt" for f in mfs)
    assert any(f.get("metric") == "ams" and f.get("op") == "gt" for f in mfs)
    assert not any(f.get("metric") == "ams" and f.get("op") == "lt" for f in mfs)
    assert not any(f.get("metric") == "volume" for f in mfs)

    same = _coerce_vocab_from_user_text(
        {
            "state_action": "clear",
            "row_dimensions": ["party"],
            "metrics": ["yoy"],
            "period_type": "LAST_N_MONTHS",
            "months_back": 6,
            "filters": {"client_type": "Eva Distributors"},
            "metric_filters": [
                {"metric": "ams", "op": "gt", "value": 10},
                {"metric": "yoy", "op": "lt", "value": 5},
            ],
            "compare": "yoy",
        },
        AMS_MT_YOY_Q,
    )
    same_mfs = same.get("metric_filters") or []
    assert same.get("metric") == "yoy"
    assert any(f.get("metric") == "ams" and f.get("op") == "gt" for f in same_mfs)
    assert any(f.get("metric") == "yoy" and f.get("op") == "lt" for f in same_mfs)
    assert not any(f.get("metric") == "volume" for f in same_mfs)


def test_router_ams_yoy_cut_is_standard() -> None:
    route = route_ask(AMS_YOY_CUT_Q)
    assert route["kind"] == "standard"
    assert "run_standard_analytics_pivot" in (route.get("preferred_tools") or [])
    ok, _ = tool_allowed("execute_read_only_sql", route)
    assert ok is False
    pids = playbook_ids(AMS_YOY_CUT_Q)
    assert "yoy_compare" in pids
    assert "compound_metric_rank" in pids
    assert "distributors_grown" not in pids


def test_execute_yoy_lt_5_uses_trailing_ams_kpi() -> None:
    """AMS>10 is the 3-month AMS KPI; last-N volume/N is not AMS."""
    previous = os.environ.get("EVA_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            import eva_dashboard.sales_query as sq

            sq._CLIENTS_CACHE = None
            _seed_yoy_window()
            out = execute_query_spec(
                {
                    "state_action": "clear",
                    "row_dimensions": ["party"],
                    "metrics": ["yoy"],
                    "period_type": "LAST_N_MONTHS",
                    "months_back": 6,
                    "filters": {"client_type": "Eva Distributors"},
                    "metric_filters": [
                        {"metric": "ams", "op": "gt", "value": 10},
                        {"metric": "yoy", "op": "lt", "value": 5},
                    ],
                    "compare": "yoy",
                },
                user_text=AMS_MT_YOY_Q,
            )
            assert out.get("ok"), out.get("error") or out.get("plan_errors")
            qs = out.get("query_spec") or {}
            assert qs.get("metric") == "yoy"
            mfs = qs.get("metric_filters") or []
            assert not any(f.get("metric") == "volume" for f in mfs)
            md = out.get("answer_markdown") or ""
            assert "No results" not in md
            names = {
                str(r.get("party") or "")
                for r in (out.get("rows") or out.get("parties") or [])
            }
            blob = md + " " + " ".join(sorted(names))
            # Trailing 3-mo AMS ~15, calendar YoY 0%.
            assert "TrailingOnly Dist" in blob
            # Trailing 3-mo AMS ~2 even though last-6 volume/N > 10.
            assert "WindowStar Dist" not in blob
            assert "Tiny Dist" not in blob
            assert "FastGrow Dist" not in blob
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
