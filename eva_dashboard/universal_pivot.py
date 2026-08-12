"""Universal OLAP-style pivot — rows × columns × metrics (esp. avg_price)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from eva_dashboard.db import connect, init_db
from eva_dashboard.data import weighted_avg
from eva_dashboard.sales_query import (
    _ROW_HEADER_LABELS,
    _build_pivot,
    _enrich_month_matrix_with_ams,
    _fetch_lines,
    _filter_blurb,
    _matrix_to_markdown,
    _month_labels,
    _sales_date_bounds,
    resolve_period,
)


ROW_DIMS = {
    "business_unit",
    "client_type",
    "party",
    "city",
    "zone",
    "packing_category",
    "product",
    "oil_type",
}
COL_DIMS = {
    "month",
    "client_type",
    "business_unit",
    "city",
    "oil_type",
    "packing_category",
}
METRICS = {"volume", "avg_price", "ams", "vs_ams", "ams_growth"}


def _fetch_priced_lines(
    *,
    date_from: str,
    date_to: str,
    city: str | None = None,
    zone: str | None = None,
    business_unit: str | None = None,
    business_units: list[str] | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    client_type: str | None = None,
    party: str | None = None,
    parties: list[str] | None = None,
    party_ilike: list[str] | None = None,
    active_only: bool = False,
) -> pd.DataFrame:
    """Volume-dim lines with rate / amount columns (no row-multiplying merge)."""
    base = _fetch_lines(
        date_from=date_from,
        date_to=date_to,
        city=city,
        zone=zone,
        business_unit=business_unit,
        business_units=business_units,
        oil_type=oil_type,
        packing_category=packing_category,
        client_type=client_type,
        party=party,
        parties=parties,
        party_ilike=party_ilike,
        active_only=active_only,
    )
    if base.empty:
        return base

    init_db()
    # Aggregate priced fields by date/party/product so a many-to-many merge
    # cannot inflate MT (Pepsi volume bug).
    sql = """
    SELECT
      s.date,
      s.product,
      s.party,
      SUM(COALESCE(s.rate, 0) * CASE
            WHEN COALESCE(s.mt_qty, 0) <> 0 THEN s.mt_qty
            ELSE COALESCE(s.mes_qty, s.qty, 0)
          END)
        / NULLIF(SUM(CASE
            WHEN COALESCE(s.mt_qty, 0) <> 0 THEN s.mt_qty
            ELSE COALESCE(s.mes_qty, s.qty, 0)
          END), 0) AS rate,
      SUM(COALESCE(s.qty, 0)) AS qty,
      SUM(COALESCE(s.mes_qty, 0)) AS mes_qty,
      SUM(COALESCE(s.incl_gst_fed_amount, 0)) AS incl_gst_fed_amount
    FROM sales s
    WHERE s.date >= ? AND s.date <= ?
    GROUP BY s.date, s.product, s.party
    """
    with connect() as conn:
        priced = pd.read_sql_query(sql, conn, params=[date_from, date_to])
    if priced.empty:
        base = base.copy()
        base["rate"] = None
        base["qty"] = 0.0
        base["mes_qty"] = 0.0
        base["incl_gst_fed_amount"] = 0.0
        return base

    keys = ["date", "party", "product"]
    base = base.copy()
    for k in keys:
        # Keep blank/null party keys (pandas groupby drops NaN by default)
        base[k] = (
            base[k]
            .fillna("")
            .astype(str)
            .str.strip()
            .replace({"nan": "", "None": "", "<NA>": ""})
        )
        priced[k] = (
            priced[k]
            .fillna("")
            .astype(str)
            .str.strip()
            .replace({"nan": "", "None": "", "<NA>": ""})
        )
    # Aggregate base MT first so duplicate line keys cannot double-count
    meta_cols = [c for c in base.columns if c not in keys + ["mt"]]
    agg = {c: "first" for c in meta_cols}
    agg["mt"] = "sum"
    base = base.groupby(keys, as_index=False, dropna=False).agg(agg)
    return base.merge(
        priced[keys + ["rate", "qty", "mes_qty", "incl_gst_fed_amount"]],
        on=keys,
        how="left",
    )


def _pkr_str(val: Any) -> str:
    if val is None or val == "":
        return "—"
    try:
        return f"{float(val):,.2f}"
    except (TypeError, ValueError):
        return str(val)


def _matrix_to_markdown_priced(matrix: dict[str, Any], row_key: str) -> str:
    """HTML matrix with PKR-formatted numeric cells."""
    columns = list(matrix.get("columns") or [])
    rows_in = list(matrix.get("rows") or [])
    if not columns:
        return "_No data._\n"
    formatted = []
    for row in rows_in:
        nr = dict(row)
        for c in columns:
            if isinstance(nr.get(c), (int, float)):
                nr[c] = _pkr_str(nr[c])
            elif nr.get(c) is None:
                nr[c] = "—"
        formatted.append(nr)
    fake = dict(matrix)
    fake["rows"] = formatted
    return _matrix_to_markdown(fake, row_key)


def _agg_avg_price(grp: pd.DataFrame) -> float | None:
    if grp is None or grp.empty:
        return None
    mt_w = grp["mt"].fillna(0).astype(float)
    mes_w = (
        grp["mes_qty"].fillna(0).astype(float)
        if "mes_qty" in grp.columns
        else mt_w
    )
    weights = mt_w.where(mt_w > 0, mes_w)
    rate = weighted_avg(grp["rate"], weights)
    return float(rate) if rate is not None else None


def _fill_row_dims(frame: pd.DataFrame, row_dims: list[str]) -> pd.DataFrame:
    """Map blank/null grain keys to (unmapped) so volume is never dropped."""
    work = frame.copy()
    for d in row_dims:
        if d not in work.columns:
            work[d] = "(unmapped)"
        else:
            s = work[d]
            work[d] = (
                s.fillna("(unmapped)")
                .astype(str)
                .str.strip()
                .replace({"": "(unmapped)", "nan": "(unmapped)", "None": "(unmapped)"})
            )
    return work


def _combined_volume_avg_price_table(
    frame: pd.DataFrame,
    row_dims: list[str],
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """Single Customer|BU × Volume (MT) + Avg Rate table (no client_type crosstab)."""
    from eva_dashboard.fmt import mt_round, mt_str

    leaf = row_dims[-1] if row_dims else "row"
    work = _fill_row_dims(frame, row_dims)
    empty = {
        "row_dimension": leaf,
        "column_dimension": "metrics",
        "columns": ["Volume (MT)", "Avg Rate"],
        "rows": [],
        "hierarchical": False,
        "row_headers": list(row_dims) or [leaf],
        "value_format": "mixed",
        "grand_total_mt": 0.0,
    }
    if work.empty or "mt" not in work.columns:
        return empty

    rows_out: list[dict[str, Any]] = []
    for keys, grp in work.groupby(list(row_dims), sort=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        cell = {d: str(v) for d, v in zip(row_dims, keys)}
        vol = float(grp["mt"].fillna(0).astype(float).sum())
        cell["Volume (MT)"] = mt_round(vol)
        cell["Avg Rate"] = _agg_avg_price(grp)
        cell["_sort_vol"] = vol
        rows_out.append(cell)

    rows_out.sort(key=lambda r: -float(r.get("_sort_vol") or 0))
    full_total = sum(float(r.get("_sort_vol") or 0) for r in rows_out)
    truncated = False
    if limit and limit > 0 and len(rows_out) > limit:
        rows_out = rows_out[: int(limit)]
        truncated = True
    shown_total = sum(float(r.get("_sort_vol") or 0) for r in rows_out)

    for r in rows_out:
        r.pop("_sort_vol", None)

    tot = {leaf: "Total"}
    for d in row_dims[:-1]:
        tot[d] = ""
    tot["Volume (MT)"] = mt_round(shown_total if truncated else full_total)
    # Footer avg = overall MT-weighted avg of the (possibly truncated) set
    if truncated:
        keep = {str(r.get(leaf) or "") for r in rows_out}
        sub = work[work[leaf].astype(str).isin(keep)]
        tot["Avg Rate"] = _agg_avg_price(sub)
    else:
        tot["Avg Rate"] = _agg_avg_price(work)
    tot["row_kind"] = "total"
    rows_out.append(tot)

    return {
        "row_dimension": leaf,
        "column_dimension": "metrics",
        "columns": ["Volume (MT)", "Avg Rate"],
        "rows": rows_out,
        "hierarchical": len(row_dims) > 1,
        "row_headers": list(row_dims),
        "value_format": "mixed",
        "grand_total_mt": float(full_total),
        "shown_total_mt": float(shown_total),
        "truncated": truncated,
        "limit": limit,
        "markdown_hint": (
            f"{leaf} × Volume + Avg Rate"
            + (f" (top {limit}; full total {mt_str(full_total)} MT)" if truncated else "")
        ),
    }


def _combined_table_to_markdown(matrix: dict[str, Any], row_key: str) -> str:
    """Render Volume + Avg Rate side-by-side (MT + PKR)."""
    from eva_dashboard.fmt import mt_str
    from eva_dashboard.sales_query import _html_escape, _matrix_row_css_class

    columns = list(matrix.get("columns") or [])
    rows_in = list(matrix.get("rows") or [])
    if not columns:
        return "_No data._\n"
    label_h = _ROW_HEADER_LABELS.get(row_key, row_key.replace("_", " ").title())
    lines = [
        '<div class="eva-mtx-wrap">',
        '<table class="eva-mtx">',
        "<thead><tr>",
        f"<th>{_html_escape(label_h)}</th>",
    ]
    for c in columns:
        lines.append(f'<th class="num">{_html_escape(c)}</th>')
    lines.append("</tr></thead><tbody>")
    for row in rows_in:
        css = _matrix_row_css_class(row, row_key)
        lines.append(f'<tr class="{css}">' if css else "<tr>")
        lines.append(
            f'<td class="dim">{_html_escape(row.get(row_key, ""))}</td>'
        )
        for c in columns:
            val = row.get(c)
            if c == "Avg Rate":
                text = _pkr_str(val)
            elif val is None:
                text = "—"
            else:
                try:
                    text = mt_str(float(val))
                except (TypeError, ValueError):
                    text = _html_escape(val)
            lines.append(f'<td class="num">{text}</td>')
        lines.append("</tr>")
    lines.append("</tbody></table></div>")
    note = ""
    if matrix.get("truncated") and matrix.get("grand_total_mt") is not None:
        note = (
            f"\n_Showing top {matrix.get('limit')}; "
            f"full scope total = {mt_str(matrix['grand_total_mt'])} MT._\n"
        )
    return "\n".join(lines) + "\n" + note


def _pivot_avg_price(
    frame: pd.DataFrame,
    row_dims: list[str],
    col_dim: str | None,
    *,
    month_labels: list[str] | None = None,
) -> dict[str, Any]:
    work = frame.copy()
    leaf = row_dims[-1] if row_dims else "row"
    empty = {
        "row_dimension": leaf,
        "column_dimension": col_dim or "value",
        "columns": [],
        "rows": [],
        "hierarchical": len(row_dims) > 1,
        "row_headers": list(row_dims) or [leaf],
        "value_format": "pkr",
    }
    if work.empty:
        return empty

    for d in row_dims:
        if d not in work.columns:
            work[d] = "(unmapped)"
        work[d] = work[d].fillna("(unmapped)").astype(str)

    if col_dim == "month":
        work["month"] = work["date"].astype(str).str.slice(0, 7)
        col_key = "month"
        ordered = list(month_labels or sorted(work["month"].unique()))
    elif col_dim:
        col_key = col_dim
        if col_key not in work.columns:
            work[col_key] = "(unmapped)"
        ordered = sorted(str(x) for x in work[col_key].dropna().unique())
    else:
        col_key = None
        ordered = ["Avg Rate"]

    rows_out: list[dict[str, Any]] = []
    if not row_dims:
        cell: dict[str, Any] = {"row": "All"}
        if col_key:
            for c in ordered:
                cell[c] = _agg_avg_price(work[work[col_key].astype(str) == str(c)])
        else:
            cell["Avg Rate"] = _agg_avg_price(work)
        rows_out.append(cell)
        return {
            "row_dimension": "row",
            "column_dimension": col_key or "value",
            "columns": ordered,
            "rows": rows_out,
            "hierarchical": False,
            "value_format": "pkr",
        }

    group_cols = list(row_dims)
    for keys, grp in work.groupby(group_cols, sort=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        cell = {d: str(v) for d, v in zip(row_dims, keys)}
        if col_key:
            for c in ordered:
                cell[c] = _agg_avg_price(grp[grp[col_key].astype(str) == str(c)])
        else:
            cell["Avg Rate"] = _agg_avg_price(grp)
        rows_out.append(cell)

    # Blank repeated parents for rowspan HTML (same pattern as _pivot_hierarchy)
    if len(row_dims) > 1:
        prev: dict[str, str] = {}
        for cell in rows_out:
            for d in row_dims[:-1]:
                cur = str(cell.get(d) or "")
                if prev.get(d) == cur:
                    cell[d] = ""
                else:
                    prev[d] = cur
                    clear = False
                    for dd in row_dims:
                        if dd == d:
                            clear = True
                        elif clear:
                            prev.pop(dd, None)

    return {
        "row_dimension": leaf,
        "column_dimension": col_key or "value",
        "columns": ordered,
        "rows": rows_out,
        "hierarchical": len(row_dims) > 1,
        "row_headers": list(row_dims),
        "value_format": "pkr",
    }


def execute_universal_pivot(
    *,
    row_dimensions: list[str],
    column_dimensions: list[str],
    metrics: list[str],
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    months_back: int = 6,
    city: str | None = None,
    zone: str | None = None,
    business_unit: str | None = None,
    business_units: list[str] | None = None,
    oil_type: str | None = None,
    packing_category: str | None = None,
    client_type: str | None = None,
    party: str | None = None,
    parties: list[str] | None = None,
    party_ilike: list[str] | None = None,
    active_only: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """OLAP pivot for avg_price (PKR) and optional volume matrices."""
    rows = [d for d in row_dimensions if d in ROW_DIMS]
    cols = [d for d in column_dimensions if d in COL_DIMS]
    mets = [m for m in metrics if m in METRICS] or ["volume"]

    mb = int(months_back or 6)
    labels: list[str] = []
    period_info: dict[str, Any]
    d0: str
    d1: str

    if "month" in cols:
        _, max_d = _sales_date_bounds()
        if max_d is None:
            return {"ok": False, "error": "No sales dates in database"}
        labels = _month_labels(max_d, mb)
        start_y, start_m = map(int, labels[0].split("-"))
        d0 = date(start_y, start_m, 1).isoformat()
        d1 = max_d.isoformat()
        period_info = {
            "ok": True,
            "date_from": d0,
            "date_to": d1,
            "label": f"Last {mb} months through {max_d.strftime('%b %Y')}",
            "months_back": mb,
            "month_labels": labels,
        }
    else:
        period_info = resolve_period(period, date_from=date_from, date_to=date_to)
        if period_info.get("ok") is False or not period_info.get("date_from"):
            return {
                "ok": False,
                "error": period_info.get("error") or "Bad period",
                "period": period_info,
            }
        d0 = period_info["date_from"]
        d1 = period_info["date_to"]

    want_price = "avg_price" in mets
    if want_price:
        frame = _fetch_priced_lines(
            date_from=d0,
            date_to=d1,
            city=city,
            zone=zone,
            business_unit=business_unit,
            business_units=business_units,
            oil_type=oil_type,
            packing_category=packing_category,
            client_type=client_type,
            party=party,
            parties=parties,
            party_ilike=party_ilike,
            active_only=active_only,
        )
    else:
        frame = _fetch_lines(
            date_from=d0,
            date_to=d1,
            city=city,
            zone=zone,
            business_unit=business_unit,
            business_units=business_units,
            oil_type=oil_type,
            packing_category=packing_category,
            client_type=client_type,
            party=party,
            parties=parties,
            party_ilike=party_ilike,
            active_only=active_only,
        )

    filters = {
        "city": city,
        "zone": zone,
        "business_unit": business_unit,
        "oil_type": oil_type,
        "packing_category": packing_category,
        "client_type": client_type,
        "party": party,
        "parties": parties,
        "party_ilike": party_ilike,
    }
    blurb = _filter_blurb(filters, period_info, units=business_units)
    col_dim = cols[0] if cols else None
    row_dim = rows[-1] if rows else "business_unit"
    row_groups = rows[:-1] if len(rows) > 1 else None

    parts: list[str] = []
    result: dict[str, Any] = {
        "ok": True,
        "mode": "universal_pivot",
        "period": period_info,
        "filters": filters,
        "business_units": list(business_units or []),
        "row_dimensions": rows,
        "column_dimensions": cols,
        "metrics": mets,
        "row_dimension": row_dim,
        "column_dimension": col_dim,
        "value_format": "pkr" if want_price else "mt",
    }

    if want_price:
        title_rows = " → ".join(
            _ROW_HEADER_LABELS.get(r, r.replace("_", " ").title()) for r in rows
        ) or "All"
        title_cols = (
            "Month"
            if col_dim == "month"
            else _ROW_HEADER_LABELS.get(col_dim or "", (col_dim or "Total").title())
        )
        # Flat volume + avg_price → one Customer|BU table (no client_type grid)
        if "volume" in mets and not col_dim:
            combined = _combined_volume_avg_price_table(
                frame, rows or ["business_unit"], limit=limit
            )
            result["matrix"] = combined
            result["volume_matrix"] = combined
            parts.append(f"### {title_rows} — Volume + Avg price\n")
            parts.append(f"_{blurb}_\n")
            parts.append(_combined_table_to_markdown(combined, row_dim))
            parts.append("\n_Avg Rate is MT-weighted (PKR)._\n")
        else:
            # Volume + average price with a column grain → two matrices
            if "volume" in mets:
                vol_frame = _fill_row_dims(frame, rows) if rows else frame
                vol_col = col_dim
                if not vol_col and "month" not in (labels or []):
                    vol_frame = vol_frame.copy()
                    vol_frame["_metric"] = "Volume (MT)"
                    vol_col = "_metric"
                vol_matrix = _build_pivot(
                    vol_frame,
                    row_dim,
                    vol_col or "client_type",
                    month_labels=labels or None,
                    row_groups=row_groups,
                )
                result["volume_matrix"] = vol_matrix
                parts.append(
                    f"### Volume — {title_rows}"
                    + (f" × {title_cols}" if col_dim else "")
                    + "\n"
                )
                parts.append(f"_{blurb}_\n")
                parts.append(_matrix_to_markdown(vol_matrix, row_dim))
                parts.append("\n")
            price_matrix = _pivot_avg_price(
                _fill_row_dims(frame, rows) if rows else frame,
                rows,
                col_dim,
                month_labels=labels or None,
            )
            result["matrix"] = price_matrix
            parts.append(
                f"### Avg price — {title_rows}"
                + (f" × {title_cols}" if col_dim else "")
                + "\n"
            )
            parts.append(f"_{blurb}_\n")
            parts.append(_matrix_to_markdown_priced(price_matrix, row_dim))
            parts.append("\n_Values are MT-weighted Avg Rate (PKR)._\n")
    else:
        filled = _fill_row_dims(frame, rows) if rows else frame
        primary = _build_pivot(
            filled,
            row_dim,
            col_dim or "client_type",
            month_labels=labels or None,
            row_groups=row_groups,
        )
        if col_dim == "month" and "ams" in mets:
            primary = _enrich_month_matrix_with_ams(
                primary,
                as_of=date.fromisoformat(d1),
                months_back=mb,
                city=city,
                zone=zone,
                business_unit=business_unit,
                business_units=business_units,
                oil_type=oil_type,
                packing_category=packing_category,
                client_type=client_type,
                party=party,
                parties=parties,
                party_ilike=party_ilike,
                lines_frame=frame,
            )
        result["matrix"] = primary
        parts.append(f"_{blurb}_\n")
        parts.append(_matrix_to_markdown(primary, row_dim))

    if not parts:
        parts.append(f"No pivot data for {blurb}.\n")

    result["answer_markdown"] = "\n".join(parts).strip() + "\n"
    result["response_instructions"] = (
        "REQUIRED: Reply with `answer_markdown` verbatim."
    )
    result["table_spec"] = {
        "period_phrase": period,
        "period": {"date_from": d0, "date_to": d1, "label": period_info.get("label")},
        "filters": filters,
        "business_units": list(business_units or []),
        "row_dimensions": rows,
        "column_dimensions": cols,
        "metrics": mets,
        "row_dimension": row_dim,
        "row_groups": row_groups,
        "column_dimension": col_dim,
        "months_back": mb if col_dim == "month" else None,
    }
    return result
