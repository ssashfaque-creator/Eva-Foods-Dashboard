"""Streamlit multi-tab app for Eva Foods data management."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from eva_dashboard import __version__
from eva_dashboard.db import init_db
from eva_dashboard.ingest import (
    DuplicateFileError,
    IngestError,
    clients_count,
    ingest_clients,
    ingest_packing_costs,
    ingest_product_costs,
    ingest_sales,
    list_factor_client_types,
    list_ingested_files,
    load_clients_table,
    load_factor_costs_table,
    load_sales_table,
    sales_count,
)
from eva_dashboard.paths import data_root, db_path


def _save_upload(upload) -> Path:
    suffix = Path(upload.name).suffix or ".xlsx"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(upload.getbuffer())
    tmp.close()
    return Path(tmp.name)


def _fmt_result(result: dict) -> str:
    parts = [f"**{result.get('original_name', 'file')}** saved"]
    if "inserted" in result:
        parts.append(f"{result['inserted']:,} new rows")
    if "upserted" in result:
        parts.append(f"{result['upserted']:,} clients updated")
    if "skipped" in result and result["skipped"]:
        parts.append(f"{result['skipped']:,} skipped/duplicates")
    if "factor_rows" in result:
        parts.append(f"{result['factor_rows']:,} factor-cost rows refreshed")
    parts.append(f"`{result.get('stored_path', '')}`")
    return " · ".join(parts)


def _inject_styles() -> None:
    st.markdown(
        """
        <style>
        .block-container { padding-top: 1.4rem; max-width: 1200px; }
        div[data-testid="stMetric"] {
            background: #f3f7f5;
            border: 1px solid #d7e3dc;
            border-radius: 12px;
            padding: 0.6rem 0.9rem;
        }
        h1, h2, h3 { color: #174a38; }
        .eva-subtle { color: #5b6b64; font-size: 0.92rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def page_sales() -> None:
    st.subheader("Sales data")
    st.markdown(
        '<p class="eva-subtle">Upload each day’s sales workbook (same Excel format). '
        "New rows are appended to the database. Files already imported are skipped by content hash "
        "and archived under <code>data/uploads/sales/</code>.</p>",
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("Sales rows", f"{sales_count():,}")
    files = list_ingested_files("sales")
    c2.metric("Files imported", f"{len(files):,}")
    if not files.empty:
        c3.metric("Last import", str(files.iloc[0]["ingested_at"]))
    else:
        c3.metric("Last import", "—")

    upload = st.file_uploader(
        "Upload sales Excel (.xlsx)",
        type=["xlsx"],
        key="sales_upload",
        help="Sales sheet header on row 5; Category sheet is imported too.",
    )
    if upload is not None and st.button("Import sales file", type="primary", key="sales_btn"):
        tmp = _save_upload(upload)
        try:
            result = ingest_sales(tmp, original_name=upload.name)
            st.success(_fmt_result(result))
            st.rerun()
        except DuplicateFileError as exc:
            st.warning(str(exc))
        except IngestError as exc:
            st.error(str(exc))
        finally:
            tmp.unlink(missing_ok=True)

    st.markdown("#### Browse sales")
    f1, f2, f3, f4 = st.columns([2, 1, 1, 1])
    search = f1.text_input("Search party / product / invoice", "")
    date_from = f2.date_input("From", value=None)
    date_to = f3.date_input("To", value=None)
    limit = f4.selectbox("Show", [500, 1000, 2500, 5000, 10000], index=1)

    frame = load_sales_table(
        search=search.strip(),
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
        limit=int(limit),
    )
    display = frame.drop(columns=["payload_json"], errors="ignore")
    rename = {
        "date": "Date",
        "party": "Party",
        "inv_no": "Inv #",
        "srno": "SRNO",
        "product": "Product",
        "qty": "Qty",
        "unit": "Unit",
        "mes_qty": "Mes Qty",
        "mes_unit": "Mes Unit",
        "mt_qty": "M.T Qty",
        "rate": "Rate",
        "basic_amount": "Basic Amount",
        "incl_gst_fed_amount": "Incl GST/FED",
        "client_type": "Client Type",
        "imported_at": "Imported",
    }
    display = display.rename(columns=rename)
    st.caption(f"{len(display):,} rows (newest first)")
    st.dataframe(display, use_container_width=True, height=480)

    with st.expander("Imported sales files"):
        st.dataframe(list_ingested_files("sales"), use_container_width=True, hide_index=True)


def page_costs() -> None:
    st.subheader("Cost structure")
    st.markdown(
        '<p class="eva-subtle">Upload product cost factors and packing costs. '
        "Each upload is archived; factor costs are refreshed when both file types are present. "
        "All source columns are stored for later use.</p>",
        unsafe_allow_html=True,
    )

    left, right = st.columns(2)
    with left:
        st.markdown("##### Product cost factors")
        pc_upload = st.file_uploader("Upload product costs (.xlsx)", type=["xlsx"], key="pc_upload")
        if pc_upload is not None and st.button("Import product costs", type="primary", key="pc_btn"):
            tmp = _save_upload(pc_upload)
            try:
                result = ingest_product_costs(tmp, original_name=pc_upload.name)
                st.success(_fmt_result(result))
                st.rerun()
            except DuplicateFileError as exc:
                st.warning(str(exc))
            except IngestError as exc:
                st.error(str(exc))
            finally:
                tmp.unlink(missing_ok=True)
        st.dataframe(
            list_ingested_files("product_costs"),
            use_container_width=True,
            hide_index=True,
        )

    with right:
        st.markdown("##### Packing costs")
        pk_upload = st.file_uploader("Upload packing costs (.xlsx)", type=["xlsx"], key="pk_upload")
        if pk_upload is not None and st.button("Import packing costs", type="primary", key="pk_btn"):
            tmp = _save_upload(pk_upload)
            try:
                result = ingest_packing_costs(tmp, original_name=pk_upload.name)
                st.success(_fmt_result(result))
                st.rerun()
            except DuplicateFileError as exc:
                st.warning(str(exc))
            except IngestError as exc:
                st.error(str(exc))
            finally:
                tmp.unlink(missing_ok=True)
        st.dataframe(
            list_ingested_files("packing_costs"),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("#### Current total factor costs")
    types = list_factor_client_types()
    if not types:
        st.info("Import both product cost factors and packing costs to build the current price structure.")
        return

    c1, c2 = st.columns([2, 1])
    client_type = c1.selectbox("Client type", types)
    search = c2.text_input("Filter products", "")
    factors = load_factor_costs_table(client_type)
    if search.strip():
        factors = factors[
            factors["product"].astype(str).str.contains(search.strip(), case=False, na=False)
        ]
    show = factors.rename(
        columns={
            "client_type": "Client Type",
            "prod_id": "ProdID",
            "product": "Product",
            "unit": "Unit",
            "product_cost": "Product Cost",
            "packing_cost": "Packing Cost",
            "total_factor_cost": "Total Factor Cost",
            "product_cost_date": "Product Cost Date",
            "packing_cost_date": "Packing Cost Date",
            "pcfid": "PCFID",
            "updated_at": "Updated",
        }
    )
    st.caption(f"{len(show):,} products for {client_type}")
    st.dataframe(show, use_container_width=True, height=420)


def page_clients() -> None:
    st.subheader("Client list")
    st.markdown(
        '<p class="eva-subtle">Upload the clients workbook to refresh master data. '
        "Clients are upserted by ClientID; all columns are kept in the database.</p>",
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns(2)
    c1.metric("Clients", f"{clients_count():,}")
    files = list_ingested_files("clients")
    c2.metric("Files imported", f"{len(files):,}")

    upload = st.file_uploader("Upload clients Excel (.xlsx)", type=["xlsx"], key="clients_upload")
    if upload is not None and st.button("Import clients file", type="primary", key="clients_btn"):
        tmp = _save_upload(upload)
        try:
            result = ingest_clients(tmp, original_name=upload.name)
            st.success(_fmt_result(result))
            st.rerun()
        except DuplicateFileError as exc:
            st.warning(str(exc))
        except IngestError as exc:
            st.error(str(exc))
        finally:
            tmp.unlink(missing_ok=True)

    st.markdown("#### Browse clients")
    frame_all = load_clients_table()
    type_options = ["All"]
    if not frame_all.empty:
        type_col = "Type" if "Type" in frame_all.columns else None
        if type_col:
            type_options += sorted(
                {str(v) for v in frame_all[type_col].dropna().unique() if str(v).strip()}
            )

    f1, f2 = st.columns([2, 1])
    search = f1.text_input("Search clients", "")
    selected_type = f2.selectbox("Type", type_options)
    frame = load_clients_table(
        search=search.strip(),
        client_type=None if selected_type == "All" else selected_type,
    )
    st.caption(f"{len(frame):,} clients")
    st.dataframe(frame, use_container_width=True, height=480)

    with st.expander("Imported client files"):
        st.dataframe(list_ingested_files("clients"), use_container_width=True, hide_index=True)


def main() -> None:
    st.set_page_config(
        page_title="Eva Foods Dashboard",
        page_icon="🌿",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    init_db()
    _inject_styles()

    st.title("Eva Foods Dashboard")
    st.caption(
        f"v{__version__} · Data folder: `{data_root()}` · Database: `{db_path().name}`"
    )

    tab_sales, tab_costs, tab_clients = st.tabs(
        ["Sales data", "Cost structure", "Client list"]
    )
    with tab_sales:
        page_sales()
    with tab_costs:
        page_costs()
    with tab_clients:
        page_clients()


if __name__ == "__main__":
    main()
