"""Weekly-within-month seasonality and expected month close."""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any

import pandas as pd

from eva_dashboard.client_language import (
    normalize_client_type,
    normalize_oil_type,
    normalize_packing_category,
)
from eva_dashboard.data import _prior_three_month_ranges
from eva_dashboard.db import connect, init_db
from eva_dashboard.fmt import mt_round, pct_round
from eva_dashboard.sales_query import _normalize_business_unit, resolve_period

_MT_EXPR = """
CASE
  WHEN COALESCE(s.mt_qty, 0) <> 0 THEN s.mt_qty
  WHEN lower(trim(COALESCE(s.unit,''))) IN ('kg','kgs')
    THEN COALESCE(s.qty,0)/1000.0
  WHEN lower(trim(COALESCE(s.unit,''))) IN
       ('mt','m.t','m.t.','ton','tons','tonne','tonnes')
    THEN COALESCE(s.qty,0)
  ELSE 0
END
"""


def week_of_month(d: date) -> int:
    """1–4: days 1–7, 8–14, 15–21, 22+."""
    if d.day <= 7:
        return 1
    if d.day <= 14:
        return 2
    if d.day <= 21:
        return 3
    return 4


def ensure_seasonality_table() -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seasonality_weekly (
              packing_category TEXT NOT NULL,
              week_of_month INTEGER NOT NULL,
              share_pct REAL NOT NULL,
              sample_months INTEGER NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (packing_category, week_of_month)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seasonality_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def recompute_seasonality() -> dict[str, Any]:
    """Average % of monthly MT falling in weeks 1–4, by packing category."""
    ensure_seasonality_table()
    init_db()
    sql = f"""
    SELECT
      s.date,
      COALESCE(NULLIF(trim(c.packing_category), ''), '(unmapped)') AS packing_category,
      {_MT_EXPR} AS mt
    FROM sales s
    LEFT JOIN category c ON c.product = s.product
    WHERE s.date IS NOT NULL AND trim(s.date) != ''
    """
    with connect() as conn:
        frame = pd.read_sql_query(sql, conn)
    if frame.empty:
        return {"ok": True, "rows": 0, "packings": 0, "message": "No sales to analyze"}

    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"])
    frame["ym"] = frame["date"].dt.strftime("%Y-%m")
    frame["wom"] = frame["date"].dt.day.map(
        lambda d: 1 if d <= 7 else 2 if d <= 14 else 3 if d <= 21 else 4
    )

    # Monthly totals per packing
    month_pack = (
        frame.groupby(["ym", "packing_category"], as_index=False)["mt"].sum()
        .rename(columns={"mt": "month_mt"})
    )
    week_pack = (
        frame.groupby(["ym", "packing_category", "wom"], as_index=False)["mt"].sum()
        .rename(columns={"mt": "week_mt"})
    )
    merged = week_pack.merge(month_pack, on=["ym", "packing_category"], how="left")
    merged = merged[merged["month_mt"] > 0].copy()
    merged["share"] = merged["week_mt"] / merged["month_mt"] * 100.0

    avg = (
        merged.groupby(["packing_category", "wom"], as_index=False)
        .agg(share_pct=("share", "mean"), sample_months=("ym", "nunique"))
    )

    now = date.today().isoformat()
    with connect() as conn:
        conn.execute("DELETE FROM seasonality_weekly")
        for _, r in avg.iterrows():
            conn.execute(
                """
                INSERT INTO seasonality_weekly
                  (packing_category, week_of_month, share_pct, sample_months, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(r["packing_category"]),
                    int(r["wom"]),
                    float(r["share_pct"]),
                    int(r["sample_months"]),
                    now,
                ),
            )
        conn.execute(
            """
            INSERT OR REPLACE INTO seasonality_meta (key, value, updated_at)
            VALUES ('last_recompute', ?, ?)
            """,
            (now, now),
        )
        conn.commit()

    # Overall (all packing) profile for fallback
    overall = (
        merged.groupby("wom", as_index=False)
        .agg(share_pct=("share", "mean"), sample_months=("ym", "nunique"))
    )
    with connect() as conn:
        for _, r in overall.iterrows():
            conn.execute(
                """
                INSERT OR REPLACE INTO seasonality_weekly
                  (packing_category, week_of_month, share_pct, sample_months, updated_at)
                VALUES ('__ALL__', ?, ?, ?, ?)
                """,
                (int(r["wom"]), float(r["share_pct"]), int(r["sample_months"]), now),
            )
        conn.commit()

    return {
        "ok": True,
        "rows": len(avg),
        "packings": int(avg["packing_category"].nunique()) if not avg.empty else 0,
        "updated_at": now,
        "message": (
            f"Seasonality rebuilt: {int(avg['packing_category'].nunique())} packings, "
            f"{len(avg)} week cells."
        ),
    }


def seasonality_table(
    packing_category: str | None = None,
) -> dict[str, Any]:
    ensure_seasonality_table()
    pack = normalize_packing_category(packing_category) if packing_category else None
    with connect() as conn:
        if pack:
            rows = conn.execute(
                """
                SELECT packing_category, week_of_month, share_pct, sample_months
                FROM seasonality_weekly
                WHERE packing_category = ?
                ORDER BY week_of_month
                """,
                (pack,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT packing_category, week_of_month, share_pct, sample_months
                FROM seasonality_weekly
                WHERE packing_category != '__ALL__'
                ORDER BY packing_category, week_of_month
                """
            ).fetchall()
        meta = conn.execute(
            "SELECT value FROM seasonality_meta WHERE key = 'last_recompute'"
        ).fetchone()
    data = [
        {
            "packing_category": r["packing_category"],
            "week_of_month": int(r["week_of_month"]),
            "share_pct": pct_round(r["share_pct"]),
            "sample_months": int(r["sample_months"]),
        }
        for r in rows
    ]
    return {
        "ok": True,
        "updated_at": meta["value"] if meta else None,
        "rows": data,
    }


def _week_shares(packing: str | None = None) -> dict[int, float]:
    ensure_seasonality_table()
    key = packing or "__ALL__"
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT week_of_month, share_pct FROM seasonality_weekly
            WHERE packing_category = ?
            """,
            (key,),
        ).fetchall()
        if not rows and key != "__ALL__":
            rows = conn.execute(
                """
                SELECT week_of_month, share_pct FROM seasonality_weekly
                WHERE packing_category = '__ALL__'
                """
            ).fetchall()
    shares = {int(r["week_of_month"]): float(r["share_pct"]) for r in rows}
    if not shares:
        # Flat fallback
        shares = {1: 25.0, 2: 25.0, 3: 25.0, 4: 25.0}
    # Normalize to 100
    tot = sum(shares.values()) or 100.0
    return {k: v / tot * 100.0 for k, v in shares.items()}


def cumulative_share_through_day(day: int, packing: str | None = None) -> float:
    """% of month typically done by end of ``day`` (using week buckets)."""
    shares = _week_shares(packing)
    cum = 0.0
    for w in (1, 2, 3, 4):
        w_end = 7 if w < 4 else 31
        w_start = 1 if w == 1 else (8 if w == 2 else (15 if w == 3 else 22))
        share = shares.get(w, 25.0)
        if day >= w_end or (w == 4 and day >= 22):
            cum += share
        elif day >= w_start:
            # Partial week: linear within week span
            span = max(1, (7 if w < 4 else 10))
            frac = min(1.0, (day - w_start + 1) / span)
            cum += share * frac
            break
        else:
            break
    return min(100.0, max(0.0, cum))


def expected_month_close(
    *,
    period: str | None = None,
    city: str | None = None,
    client_type: str | None = None,
    business_unit: str | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    exclude_client_types: list[str] | None = None,
) -> dict[str, Any]:
    """Project full-month MT from MTD using seasonality + AMS baseline."""
    from eva_dashboard.advanced_analytics import _fetch_filtered_mt

    pack = normalize_packing_category(packing_category)
    ctype = normalize_client_type(client_type)
    bu = _normalize_business_unit(business_unit)
    oil = normalize_oil_type(oil_type)

    if not period:
        period = "this month"
    period_info = resolve_period(period)
    if period_info.get("ok") is False:
        return {"ok": False, "error": period_info.get("error")}

    d0, d1 = period_info["date_from"], period_info["date_to"]
    as_of = date.fromisoformat(d1)
    mtd = _fetch_filtered_mt(
        date_from=d0,
        date_to=d1,
        city=city,
        client_type=ctype,
        business_unit=bu,
        oil_type=oil,
        packing_category=pack,
        exclude_client_types=exclude_client_types,
    )

    ranges = _prior_three_month_ranges(as_of.replace(day=1))
    monthly = []
    for start, end in ranges:
        monthly.append(
            _fetch_filtered_mt(
                date_from=start.isoformat(),
                date_to=end.isoformat(),
                city=city,
                client_type=ctype,
                business_unit=bu,
                oil_type=oil,
                packing_category=pack,
                exclude_client_types=exclude_client_types,
            )
        )
    ams = sum(monthly) / 3.0 if monthly else 0.0

    cum_share = cumulative_share_through_day(as_of.day, pack)
    if cum_share > 1e-6:
        seasonality_proj = mtd / (cum_share / 100.0)
    else:
        seasonality_proj = mtd

    dim = calendar.monthrange(as_of.year, as_of.month)[1]
    linear_proj = (mtd / as_of.day * dim) if as_of.day else mtd
    expected = seasonality_proj
    lines = [
        f"Expected month close — {period_info.get('label')} "
        f"(through day {as_of.day}/{dim}).\n",
        "| Measure | MT |",
        "| --- | --- |",
        f"| MTD so far | {mt_round(mtd)} |",
        f"| AMS (3 prior months) | {mt_round(ams)} |",
        f"| Linear run-rate (days) | {mt_round(linear_proj)} |",
        f"| **Seasonality projection** | **{mt_round(expected)}** |",
        f"| Typical week shares used | W1–W4 cum ≈ {cum_share:.0f}% by day {as_of.day} |",
        "",
        "### Analysis",
        f"- Seasonality says ~**{cum_share:.0f}%** of a normal month is done by day {as_of.day}; "
        f"implied full-month ≈ **{mt_round(expected)} MT**.",
    ]
    if ams:
        vs = (expected - ams) / ams * 100.0
        lines.append(
            f"- Vs AMS ({mt_round(ams)} MT): projection is **{vs:+.0f}%** "
            + ("ahead of a typical month." if vs >= 0 else "below a typical month.")
        )
    if abs(expected - linear_proj) / max(expected, 1) > 0.1:
        lines.append(
            f"- Linear run-rate ({mt_round(linear_proj)} MT) differs from seasonality — "
            "early/late month pace is uneven for this mix."
        )

    return {
        "ok": True,
        "mode": "expected_month",
        "period": period_info,
        "filters": {
            "city": city,
            "client_type": ctype,
            "business_unit": bu,
            "oil_type": oil,
            "packing_category": pack,
            "exclude_client_types": exclude_client_types,
        },
        "mtd_mt": mt_round(mtd),
        "ams_mt": mt_round(ams),
        "linear_projection_mt": mt_round(linear_proj),
        "seasonality_projection_mt": mt_round(expected),
        "cumulative_seasonality_pct": pct_round(cum_share),
        "answer_markdown": "\n".join(lines) + "\n",
        "response_instructions": "REQUIRED: Use answer_markdown verbatim.",
    }
