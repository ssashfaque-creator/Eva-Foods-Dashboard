"""Executive question bank — offline routing + dispatch smoke for all channels."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from eva_dashboard.chatbot import (
    _dispatch_tool,
    _looks_channel_growth_ask,
    _looks_which_parties_ask,
    resolve_forced_tool,
    suggest_preferred_tool,
)
from eva_dashboard.client_language import CLIENT_TYPE_ALIASES, extract_client_type_from_text
from eva_dashboard.db import connect, init_db


def _env(tmp: str) -> None:
    os.environ["EVA_DATA_DIR"] = str(Path(tmp) / "data")


def _seed_exec() -> None:
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
            ],
        )
        clients = [
            ("1", "Alpha Dist", "Eva Distributors", "Karachi"),
            ("2", "Gamma Dist", "Eva Distributors", "Karachi"),
            ("3", "Beta Store", "Imtiaz Store", "Lahore"),
            ("4", "Delta Store", "Imtiaz Store", "Lahore"),
            ("5", "Metro Khi", "METRO HABIB", "Karachi"),
            ("6", "Metro Lhr", "METRO HABIB", "Lahore"),
            ("7", "Chase Up Gul", "CHASE UP", "Karachi"),
            ("8", "CSD Depot", "Canteen Store Department", "Lahore"),
            ("9", "SPAR Outlet", "SPAR - IMT", "Karachi"),
            ("10", "Panda Hub", "FOOD PANDA", "Lahore"),
            ("11", "Gelani Main", "GELANI MART", "Karachi"),
            ("12", "Online Cust A", "Online Customer", "Karachi"),
            ("13", "North LMT 1", "NORTH LMT", "Lahore"),
            ("14", "Maan Dist A", "Maan Distributors", "Karachi"),
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_id, client, type, city_filter, city, inactive, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '{}', datetime('now'))",
            [(i, n, t, c, c) for i, n, t, c in clients],
        )
        rows = [
            ("2026-04-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30, "Eva Distributors"),
            ("2026-05-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30, "Eva Distributors"),
            ("2026-06-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 30, "Eva Distributors"),
            ("2026-07-05", "Alpha Dist", "Eva Canola Oil (StandUpPouch)", 40, "Eva Distributors"),
            ("2026-07-06", "Gamma Dist", "Maan Canola Oil", 12, "Eva Distributors"),
            ("2026-07-07", "Alpha Dist", "Maan Canola Oil", 3, "Eva Distributors"),
            ("2026-07-08", "Beta Store", "Eva VTF Banaspati 16 Kg Tin", 25, "Imtiaz Store"),
            ("2026-07-09", "Delta Store", "Eva VTF Banaspati 16 Kg Tin", 10, "Imtiaz Store"),
            ("2026-07-10", "Metro Khi", "Eva Canola Oil (StandUpPouch)", 18, "METRO HABIB"),
            ("2026-07-11", "Metro Lhr", "Eva VTF Banaspati 16 Kg Tin", 22, "METRO HABIB"),
            ("2026-07-12", "Chase Up Gul", "Eva Canola Oil (StandUpPouch)", 14, "CHASE UP"),
            ("2026-07-13", "CSD Depot", "Eva Canola Oil (StandUpPouch)", 9, "Canteen Store Department"),
            ("2026-07-14", "SPAR Outlet", "Eva Canola Oil (StandUpPouch)", 7, "SPAR - IMT"),
            ("2026-07-15", "Panda Hub", "Eva Canola Oil (StandUpPouch)", 5, "FOOD PANDA"),
            ("2026-07-16", "Gelani Main", "Eva Canola Oil (StandUpPouch)", 6, "GELANI MART"),
            ("2026-07-17", "Online Cust A", "Eva Canola Oil (StandUpPouch)", 4, "Online Customer"),
            ("2026-07-18", "North LMT 1", "Eva Canola Oil (StandUpPouch)", 11, "NORTH LMT"),
            ("2026-07-19", "Maan Dist A", "Maan Canola Oil", 8, "Maan Distributors"),
        ]
        for i, (dt, party, prod, mt, ct) in enumerate(rows):
            conn.execute(
                """
                INSERT INTO sales (
                  source_file_id, row_hash, imported_at, date, party, product,
                  qty, unit, mt_qty, client_type, payload_json
                ) VALUES (NULL, ?, datetime('now'), ?, ?, ?, ?, 'MT', ?, ?, '{}')
                """,
                (f"ex-{i}", dt, party, prod, mt, mt, ct),
            )
        conn.commit()


def _run(user_text: str, prior: dict | None = None, tool: str = "query_sales") -> dict:
    return _dispatch_tool(tool, {}, user_text=user_text, prior_spec=prior)


def _md(out: dict) -> str:
    return out.get("answer_markdown") or ""


# (id, query, prior|None, checks)
# checks: list of callables(out) -> None (assert) or str error
Case = tuple[str, str, dict | None, list[Callable[[dict], None]]]


def _expect_mode(*modes: str) -> Callable[[dict], None]:
    def _check(out: dict) -> None:
        assert out.get("ok") is not False
        assert out.get("mode") in modes, f"mode={out.get('mode')} not in {modes}"

    return _check


def _expect_filter(key: str, value: Any) -> Callable[[dict], None]:
    def _check(out: dict) -> None:
        got = (out.get("filters") or {}).get(key)
        assert got == value, f"filters[{key}]={got!r} != {value!r}"

    return _check


def _expect_md_has(*needles: str) -> Callable[[dict], None]:
    def _check(out: dict) -> None:
        md = _md(out)
        for n in needles:
            assert n in md, f"missing {n!r} in markdown"

    return _check


def _expect_row_dim(dim: str) -> Callable[[dict], None]:
    def _check(out: dict) -> None:
        assert out.get("row_dimension") == dim, f"row_dimension={out.get('row_dimension')}"

    return _check


def _expect_not_month_grid() -> Callable[[dict], None]:
    def _check(out: dict) -> None:
        assert out.get("column_dimension") != "month" or out.get("mode") == "trend"
        assert "AMS (6 months)" not in _md(out)

    return _check


def _expect_excludes_maan() -> Callable[[dict], None]:
    def _check(out: dict) -> None:
        excl = (out.get("excludes") or {}).get("business_unit") or []
        assert "Maan Consumer" in excl and "Maan Bulk" in excl

    return _check


def _expect_city_rows() -> Callable[[dict], None]:
    def _check(out: dict) -> None:
        headers = (out.get("matrix") or {}).get("row_headers") or [
            out.get("row_dimension")
        ]
        assert headers[0] == "city" or out.get("row_dimension") == "city"

    return _check


PRIOR_KHI_DIST = {
    "filters": {"city": "Karachi", "client_type": "Eva Distributors"},
    "column_dimension": "month",
    "months_back": 6,
    "period": {
        "date_from": "2026-03-01",
        "date_to": "2026-08-07",
        "label": "Last 6 months",
    },
}

PRIOR_EVA_JULY = {
    "filters": {"business_unit": "Eva Consumer", "city": None, "client_type": None},
    "period_phrase": "July 2026",
    "period": {
        "date_from": "2026-07-01",
        "date_to": "2026-07-31",
        "label": "Jul 2026",
    },
    "row_dimension": "packing_category",
    "column_dimension": "city",
    "business_units": ["Eva Consumer"],
}


EXEC_CASES: list[Case] = [
    # --- Sales matrices / named month ---
    (
        "sales-dist-khi-july",
        "Show me Eva distributor sales in Karachi for July",
        None,
        [
            _expect_mode("trend"),
            _expect_filter("city", "Karachi"),
            _expect_filter("client_type", "Eva Distributors"),
            _expect_not_month_grid(),
        ],
    ),
    (
        "sales-imtiaz-bare",
        "Show me Imtiaz sales",
        None,
        [
            _expect_mode("matrix", "party_sales"),
            _expect_filter("client_type", "Imtiaz Store"),
        ],
    ),
    (
        "sales-lahore",
        "Show me Lahore sales",
        None,
        [_expect_filter("city", "Lahore")],
    ),
    # --- Channels (= client types) ---
    (
        "channels-grew-declined",
        "which channels grew sales and which declined",
        PRIOR_EVA_JULY,
        [
            _expect_mode("trend"),
            _expect_row_dim("client_type"),
            _expect_filter("business_unit", "Eva Consumer"),
        ],
    ),
    # --- which/what across ALL client types ---
    (
        "which-dist-selling-maan",
        "which distributor is selling maan",
        PRIOR_KHI_DIST,
        [
            _expect_mode("matrix"),
            _expect_row_dim("party"),
            _expect_filter("city", "Karachi"),
            _expect_filter("client_type", "Eva Distributors"),
            _expect_filter("business_unit", "Maan Consumer"),
            _expect_md_has("Gamma Dist"),
        ],
    ),
    (
        "imtiaz-most-vtf",
        "what Imtiaz store sells the most VTF",
        PRIOR_KHI_DIST,
        [
            _expect_filter("client_type", "Imtiaz Store"),
            _expect_filter("oil_type", "Eva VTF"),
            _expect_filter("city", None),
            _expect_md_has("Beta Store"),
        ],
    ),
    (
        "metro-most-vtf",
        "what Metro sells the most VTF",
        None,
        [
            _expect_filter("client_type", "METRO HABIB"),
            _expect_filter("oil_type", "Eva VTF"),
            _expect_md_has("Metro Lhr"),
        ],
    ),
    (
        "chase-up-active-khi",
        "which Chase Up are active in Karachi",
        None,
        [
            _expect_mode("list_clients"),
            _expect_filter("client_type", "CHASE UP"),
            _expect_filter("city", "Karachi"),
            _expect_md_has("Chase Up Gul"),
        ],
    ),
    (
        "csd-who-are",
        "who are the CSD stores",
        None,
        [
            _expect_mode("list_clients"),
            _expect_filter("client_type", "Canteen Store Department"),
            _expect_md_has("CSD Depot"),
        ],
    ),
    (
        "spar-selling-canola",
        "which SPAR is selling canola",
        None,
        [
            _expect_filter("client_type", "SPAR - IMT"),
            # oil filter → analyze_parties volume
            _expect_md_has("SPAR Outlet"),
        ],
    ),
    (
        "foodpanda-active",
        "what Food Panda are active",
        None,
        [
            _expect_mode("list_clients"),
            _expect_filter("client_type", "FOOD PANDA"),
            _expect_md_has("Panda Hub"),
        ],
    ),
    (
        "gelani-most",
        "which Gelani sells the most",
        None,
        [
            _expect_filter("client_type", "GELANI MART"),
            _expect_md_has("Gelani Main"),
        ],
    ),
    (
        "online-active-khi",
        "which online customers are active in Karachi",
        None,
        [
            _expect_mode("list_clients"),
            _expect_filter("client_type", "Online Customers"),
            _expect_filter("city", "Karachi"),
            _expect_md_has("Online Cust A"),
        ],
    ),
    (
        "north-lmt-active",
        "what North LMT are active",
        None,
        [
            _expect_mode("list_clients"),
            _expect_filter("client_type", "NORTH LMT"),
            _expect_md_has("North LMT 1"),
        ],
    ),
    (
        "maan-dist-selling-maan",
        "which Maan distributors are selling maan",
        None,
        [
            _expect_mode("list_clients"),
            _expect_filter("client_type", "Maan Distributors"),
            _expect_filter("business_unit", "Maan Consumer"),
            _expect_md_has("Maan Dist A"),
        ],
    ),
    (
        "active-dist-lahore",
        "what distributors are active in Lahore",
        PRIOR_KHI_DIST,
        [
            _expect_mode("list_clients"),
            _expect_filter("city", "Lahore"),
            _expect_filter("client_type", "Eva Distributors"),
        ],
    ),
    # --- Follow-up / table ops ---
    (
        "remove-maan",
        "remove maan consumer and maan bulk",
        {
            **PRIOR_KHI_DIST,
            "business_units": [],
            "row_dimension": "business_unit",
        },
        [_expect_excludes_maan()],
    ),
    (
        "group-by-city",
        "group by city",
        {
            "filters": {"client_type": "Eva Distributors"},
            "column_dimension": "month",
            "months_back": 6,
            "row_dimension": "business_unit",
        },
        [_expect_city_rows()],
    ),
]


def test_all_alias_client_types_extractable() -> None:
    """Every alias in CLIENT_TYPE_ALIASES must resolve via extract."""
    # Unique canonical targets
    targets = sorted(set(CLIENT_TYPE_ALIASES.values()))
    for canon in targets:
        # find a short alias that maps to it
        alias = next(a for a, c in CLIENT_TYPE_ALIASES.items() if c == canon)
        q = f"which {alias} are active"
        assert extract_client_type_from_text(q) == canon, q
        assert _looks_which_parties_ask(q), q


def test_executive_bank_dispatch() -> None:
    previous = os.environ.get("EVA_DATA_DIR")
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        try:
            _seed_exec()
            for case_id, query, prior, checks in EXEC_CASES:
                try:
                    out = _run(query, prior)
                    for check in checks:
                        check(out)
                except AssertionError as exc:
                    failures.append(f"{case_id}: {exc} | q={query!r}")
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{case_id}: EXC {type(exc).__name__}: {exc} | q={query!r}")
        finally:
            if previous is None:
                os.environ.pop("EVA_DATA_DIR", None)
            else:
                os.environ["EVA_DATA_DIR"] = previous
    assert not failures, "Executive eval failures:\n" + "\n".join(failures)


def test_executive_routing_hints() -> None:
    """Forced / preferred tools for a slice of executive asks."""
    rows = [
        ("which distributor is selling maan", "list_clients"),
        ("what Metro sells the most VTF", "analyze_parties"),
        ("which Chase Up are active in Karachi", "list_clients"),
        ("who are the CSD stores", "list_clients"),
        ("which channels grew sales and which declined", "query_sales"),
        ("Show me Eva distributor sales in Karachi for July", "query_sales"),
        ("Which distributors are falling behind on AMS?", "analyze_parties"),
        ("Which cities declined more than 20% YoY?", "advanced_query"),
    ]
    failures: list[str] = []
    for q, preferred in rows:
        got_pref = suggest_preferred_tool(q)
        got_force = resolve_forced_tool(q)
        if got_pref != preferred:
            failures.append(f"preferred {got_pref!r} != {preferred!r}: {q}")
        if got_force not in {"required", preferred}:
            failures.append(f"forced {got_force!r} not in required|{preferred}: {q}")
        if preferred == "query_sales" and _looks_channel_growth_ask(q):
            assert got_force == "query_sales"
    assert not failures, "Routing hint failures:\n" + "\n".join(failures)
