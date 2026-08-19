"""Offline magic eval harness — score router + playbooks without OpenAI.

Usage (pytest): tests/test_golden_magic_eval.py
CLI: python -m eva_dashboard.eval_harness

Production money baseline (AMS / volume / Price Fetch) must prefer the
deterministic pivot tool and forbid reinventing those metrics in SQL.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eva_dashboard.playbooks import playbook_ids
from eva_dashboard.tools.intent_router import route_ask, tool_allowed

_EVAL_PATH = Path(__file__).resolve().parent / "golden_magic_eval.json"


def load_golden_cases() -> list[dict[str, Any]]:
    raw = json.loads(_EVAL_PATH.read_text(encoding="utf-8"))
    return list(raw.get("cases") or [])


def score_case(case: dict[str, Any]) -> dict[str, Any]:
    """Score one golden case. Returns ok + details."""
    text = str(case.get("user_text") or "")
    route = route_ask(text)
    kind = str(route.get("kind") or "")
    expect_kinds = [str(k) for k in (case.get("expect_kind") or [])]
    kind_ok = (not expect_kinds) or kind in expect_kinds

    pids = playbook_ids(text)
    expect_pb = [str(x) for x in (case.get("expect_playbooks") or [])]
    pb_ok = all(p in pids for p in expect_pb)

    forbid = [str(t) for t in (case.get("forbid_tools") or [])]
    forbid_ok = True
    forbid_details: list[str] = []
    for tool in forbid:
        allowed, _reason = tool_allowed(tool, route)
        if allowed:
            forbid_ok = False
            forbid_details.append(f"{tool} should be blocked for this ask")

    prefer = [str(t) for t in (case.get("expect_preferred_tools") or [])]
    preferred = list(route.get("preferred_tools") or [])
    prefer_ok = all(t in preferred for t in prefer)
    prefer_details: list[str] = []
    if not prefer_ok:
        prefer_details.append(
            f"expected preferred {prefer}, got {preferred}"
        )

    alias_ok = True
    expect_alias = case.get("expect_alias")
    if expect_alias:
        from eva_dashboard.personal_lexicon import expand_aliases_in_text

        _, expansions = expand_aliases_in_text(text)
        spoken = {e["spoken"] for e in expansions}
        alias_ok = str(expect_alias).lower() in spoken

    money = bool(case.get("money_metric"))
    money_ok = True
    money_details: list[str] = []
    if money:
        # Absolute baseline: money metrics must be standard + pivot-preferred
        if kind != "standard":
            money_ok = False
            money_details.append(f"money metric must route standard, got {kind}")
        if "run_standard_analytics_pivot" not in preferred:
            money_ok = False
            money_details.append("money metric must prefer run_standard_analytics_pivot")
        if "execute_read_only_sql" not in (route.get("blocked_tools") or []):
            # AMS / volume / Price Fetch / growth must hard-block SQL
            if any(
                x in text.lower()
                for x in (
                    "ams",
                    "price fetch",
                    "price_fetch",
                    "grown",
                    "declined",
                    "volume",
                    "tonnage",
                )
            ) or re.search(r"\bsales?\b", text.lower()):
                money_ok = False
                money_details.append("money/AMS/PF/volume ask must block execute_read_only_sql")

    ok = bool(kind_ok and pb_ok and forbid_ok and prefer_ok and alias_ok and money_ok)
    return {
        "id": case.get("id"),
        "ok": ok,
        "kind": kind,
        "kind_ok": kind_ok,
        "playbooks": pids,
        "playbooks_ok": pb_ok,
        "forbid_ok": forbid_ok,
        "forbid_details": forbid_details,
        "prefer_ok": prefer_ok,
        "prefer_details": prefer_details,
        "alias_ok": alias_ok,
        "money_ok": money_ok,
        "money_details": money_details,
        "route": route,
    }


def run_eval() -> dict[str, Any]:
    """Run golden cases against a clean lexicon (no sticky prefs)."""
    import os
    import tempfile

    cases = load_golden_cases()
    prev = os.environ.get("EVA_DATA_DIR")
    tmp = tempfile.mkdtemp(prefix="eva_magic_eval_")
    os.environ["EVA_DATA_DIR"] = tmp
    try:
        results = [score_case(c) for c in cases]
    finally:
        if prev is None:
            os.environ.pop("EVA_DATA_DIR", None)
        else:
            os.environ["EVA_DATA_DIR"] = prev

    passed = sum(1 for r in results if r["ok"])
    money = [
        r
        for r in results
        if any(c.get("id") == r.get("id") and c.get("money_metric") for c in cases)
    ]
    money_pass = sum(1 for r in money if r["ok"])
    return {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": (passed / len(results)) if results else 0.0,
        "money_total": len(money),
        "money_passed": money_pass,
        "results": results,
    }


def main() -> None:
    out = run_eval()
    print(
        f"Magic eval: {out['passed']}/{out['total']} "
        f"({100.0 * out['pass_rate']:.0f}%)"
    )
    if out.get("money_total"):
        print(
            f"Money baseline: {out['money_passed']}/{out['money_total']}"
        )
    for r in out["results"]:
        mark = "OK" if r["ok"] else "FAIL"
        print(
            f"  [{mark}] {r['id']}: kind={r['kind']} "
            f"playbooks={r['playbooks']}"
        )
        if not r["ok"]:
            if not r["kind_ok"]:
                print("           kind mismatch")
            if not r["playbooks_ok"]:
                print("           missing playbook")
            if not r["alias_ok"]:
                print("           missing alias")
            if not r.get("prefer_ok", True):
                for d in r.get("prefer_details") or []:
                    print(f"           {d}")
            if not r.get("money_ok", True):
                for d in r.get("money_details") or []:
                    print(f"           {d}")
            for d in r.get("forbid_details") or []:
                print(f"           {d}")
    raise SystemExit(0 if out["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
