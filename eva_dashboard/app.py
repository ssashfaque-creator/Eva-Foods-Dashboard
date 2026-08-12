"""Streamlit multi-tab app for Eva Foods data management."""

from __future__ import annotations

import re
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from eva_dashboard import __version__
from eva_dashboard.db import init_db
from eva_dashboard.db_report import (
    available_sales_dates,
    generate_sales_dashboard_pdf,
    list_generated_reports,
)
from eva_dashboard.ingest import (
    DuplicateFileError,
    IngestError,
    category_count,
    clients_count,
    ingest_categories,
    ingest_clients,
    ingest_packing_costs,
    ingest_product_costs,
    ingest_sales,
    list_factor_client_types,
    list_ingested_files,
    load_category_map_from_db,
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


def _cell_str(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def _for_display(df: pd.DataFrame) -> pd.DataFrame:
    """Make a frame Arrow/Streamlit-safe by stringifying every cell."""
    if df is None:
        return pd.DataFrame()
    if df.empty:
        return df.copy()
    data = {col: [_cell_str(v) for v in df[col].tolist()] for col in df.columns}
    return pd.DataFrame(data)


def _dataframe(df: pd.DataFrame, **kwargs) -> None:
    """Display a dataframe safely for Streamlit/Arrow."""
    kwargs.pop("use_container_width", None)
    safe = _for_display(df)
    try:
        st.dataframe(safe, width="stretch", **kwargs)
    except TypeError:
        # Older Streamlit without width=
        st.dataframe(safe, **kwargs)


def _fmt_result(result: dict) -> str:
    parts = [f"**{result.get('original_name', 'file')}** saved"]
    if "inserted" in result:
        parts.append(f"{result['inserted']:,} new rows")
    if "replaced" in result:
        parts.append(f"{result['replaced']:,} products (replaced)")
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

        /* Sales matrices in AI Chat */
        .eva-mtx-wrap { overflow-x: auto; margin: 0.4rem 0 0.9rem; }
        .eva-mtx {
            border-collapse: collapse;
            width: max-content;
            min-width: 100%;
            font-size: 0.9rem;
            line-height: 1.35;
        }
        .eva-mtx th, .eva-mtx td {
            border: 1px solid rgba(128, 128, 128, 0.35);
            padding: 0.38rem 0.6rem;
            vertical-align: middle;
        }
        .eva-mtx th {
            font-weight: 700;
            text-align: left;
            background: rgba(128, 128, 128, 0.14);
            white-space: nowrap;
        }
        .eva-mtx td.num {
            text-align: right;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }
        .eva-mtx td.dim {
            font-weight: 600;
            vertical-align: top;
            white-space: nowrap;
        }
        .eva-mtx td.total-col { font-weight: 700; }
        .eva-mtx th.ams-col, .eva-mtx td.ams-col {
            font-weight: 700;
            background: rgba(23, 74, 56, 0.08);
        }
        .eva-mtx th {
            max-width: 7.5rem;
            white-space: normal;
        }
        .eva-mtx tr.eva-subtotal td {
            font-weight: 700;
            background: rgba(128, 128, 128, 0.16);
        }
        .eva-mtx tr.eva-total td {
            font-weight: 800;
            background: rgba(128, 128, 128, 0.28);
            border-top: 2px solid rgba(128, 128, 128, 0.55);
        }
        .eva-reply-hint {
            font-size: 0.85rem;
            opacity: 0.85;
            margin: 0.15rem 0 0.25rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _chat_markdown(text: str) -> None:
    """Render chat content; allow HTML tables from sales matrices."""
    st.markdown(text or "", unsafe_allow_html=True)


def page_sales() -> None:
    st.subheader("Sales data")
    st.markdown(
        '<p class="eva-subtle">Upload each day’s sales workbook (same Excel format). '
        "New rows are appended to the database. Files already imported are skipped by content hash "
        "and archived under <code>data/uploads/sales/</code>.</p>",
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sales rows", f"{sales_count():,}")
    files = list_ingested_files("sales")
    c2.metric("Files imported", f"{len(files):,}")
    if not files.empty:
        c3.metric("Last import", str(files.iloc[0]["ingested_at"]))
    else:
        c3.metric("Last import", "—")
    try:
        from eva_dashboard.seasonality import seasonality_table

        sea = seasonality_table()
        c4.metric("Seasonality", sea.get("updated_at") or "Not built")
    except Exception:
        c4.metric("Seasonality", "—")

    sales_col, cat_col = st.columns(2)

    with sales_col:
        st.markdown("#### Upload sales")
        upload = st.file_uploader(
            "Sales Excel (.xlsx)",
            type=["xlsx"],
            key="sales_upload",
            help="Sales sheet header on row 5. After import, week-of-month seasonality is rebuilt.",
        )
        if upload is not None and st.button(
            "Import sales file", type="primary", key="sales_btn"
        ):
            tmp = _save_upload(upload)
            try:
                result = ingest_sales(tmp, original_name=upload.name)
                st.success(_fmt_result(result))
                sea = result.get("seasonality") or {}
                if sea.get("ok"):
                    st.info(sea.get("message") or "Seasonality updated.")
                elif sea.get("error"):
                    st.warning(f"Seasonality rebuild failed: {sea['error']}")
                st.rerun()
            except DuplicateFileError as exc:
                st.warning(str(exc))
            except IngestError as exc:
                st.error(str(exc))
            finally:
                tmp.unlink(missing_ok=True)

        with st.expander("Week-of-month seasonality (by packing)", expanded=False):
            st.caption(
                "Average % of monthly volume in weeks 1–4 (days 1–7, 8–14, 15–21, 22+). "
                "Rebuilt automatically after each sales import. Used for expected month close."
            )
            if st.button("Rebuild seasonality now", key="sea_rebuild"):
                from eva_dashboard.seasonality import recompute_seasonality

                out = recompute_seasonality()
                if out.get("ok"):
                    st.success(out.get("message"))
                else:
                    st.error(out.get("error") or "Failed")
            from eva_dashboard.seasonality import seasonality_table

            sea = seasonality_table()
            if sea.get("rows"):
                import pandas as pd

                st.dataframe(pd.DataFrame(sea["rows"]), use_container_width=True, hide_index=True)
            else:
                st.caption("No seasonality yet — import sales to build.")

    with cat_col:
        st.markdown("#### Upload categories")
        st.caption(
            "Columns: **Product**, **Business Unit**, **Oil Type**, **Packing Category**. "
            "New file replaces the previous map."
        )
        cat_cols = st.columns(2)
        cat_cols[0].metric("Products mapped", f"{category_count():,}")
        cat_files = list_ingested_files("categories")
        if not cat_files.empty:
            cat_cols[1].metric("Last import", str(cat_files.iloc[0]["ingested_at"])[:19])
        else:
            cat_cols[1].metric("Last import", "—")

        cat_upload = st.file_uploader(
            "Category Excel (.xlsx / .csv)",
            type=["xlsx", "csv"],
            key="category_upload",
            help="Header: Product, Business Unit, Oil Type, Packing Category.",
        )
        if cat_upload is not None and st.button(
            "Import category file (replace)", type="primary", key="category_btn"
        ):
            tmp = _save_upload(cat_upload)
            try:
                result = ingest_categories(tmp, original_name=cat_upload.name)
                st.success(_fmt_result(result))
                st.rerun()
            except (DuplicateFileError, IngestError, ValueError) as exc:
                st.error(str(exc))
            finally:
                tmp.unlink(missing_ok=True)

    if category_count() > 0:
        with st.expander("Current category map", expanded=False):
            try:
                cmap = load_category_map_from_db().rename(
                    columns={
                        "product": "Product",
                        "business_unit": "Business Unit",
                        "oil_type": "Oil Type",
                        "packing_category": "Packing Category",
                    }
                )
                show_cols = [
                    c
                    for c in (
                        "Product",
                        "Business Unit",
                        "Oil Type",
                        "Packing Category",
                    )
                    if c in cmap.columns
                ]
                _dataframe(cmap[show_cols], height=320, hide_index=True)
            except Exception as exc:
                st.warning(str(exc))

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
    _dataframe(display, height=480)

    with st.expander("Imported sales files"):
        _dataframe(list_ingested_files("sales"), hide_index=True)
    with st.expander("Imported category files"):
        _dataframe(list_ingested_files("categories"), hide_index=True)


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
        _dataframe(
            list_ingested_files("product_costs"),
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
        _dataframe(
            list_ingested_files("packing_costs"),
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
    _dataframe(show, height=420)


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
    _dataframe(frame, height=480)

    with st.expander("Imported client files"):
        _dataframe(list_ingested_files("clients"), hide_index=True)


def page_reports() -> None:
    st.subheader("Reports")
    st.markdown(
        '<p class="eva-subtle">Build reports from data already imported into the app. '
        "No need to re-upload Excel files here.</p>",
        unsafe_allow_html=True,
    )

    st.markdown("#### Sales dashboard PDF")
    st.write(
        "Daily sales summary + detail (category / city tables, Price Fetch, "
        "bulk averages, and line-level Cost Factor / Price Fetch)."
    )

    dates = available_sales_dates()
    if not dates:
        st.info("Import sales data (and ideally clients + costs) before generating a report.")
        return

    if category_count() == 0:
        st.warning(
            "No category file loaded. Upload Product / Business Unit / Oil Type / "
            "Packing Category on the Sales data tab before generating a report."
        )

    c1, c2, c3 = st.columns([2, 1, 1])
    default_idx = len(dates) - 1
    selected = c1.selectbox(
        "Report date",
        options=dates,
        index=default_idx,
        format_func=lambda d: d.strftime("%d %b %Y"),
    )
    c2.metric("Sales rows in DB", f"{sales_count():,}")
    c3.metric("Latest sales date", dates[-1].strftime("%d %b %Y"))

    if st.button(
        "Generate sales dashboard",
        type="primary",
        key="gen_sales_pdf",
        disabled=category_count() == 0,
    ):
        with st.spinner("Building PDF…"):
            try:
                pdf_path = generate_sales_dashboard_pdf(report_date=selected)
                st.success(f"Created `{pdf_path.name}`")
                st.session_state["last_sales_pdf"] = str(pdf_path)
            except Exception as exc:
                import traceback

                st.error(f"{type(exc).__name__}: {exc}")
                st.code(traceback.format_exc())

    last = st.session_state.get("last_sales_pdf")
    if last and Path(last).exists():
        pdf_file = Path(last)
        st.download_button(
            label=f"Download {pdf_file.name}",
            data=pdf_file.read_bytes(),
            file_name=pdf_file.name,
            mime="application/pdf",
            key="download_last_sales_pdf",
        )

    st.markdown("#### Recent generated reports")
    recent = list_generated_reports()
    if not recent:
        st.caption("No reports generated yet.")
        return
    for path in recent:
        cols = st.columns([4, 1])
        cols[0].write(f"`{path.name}`")
        cols[1].download_button(
            "Download",
            data=path.read_bytes(),
            file_name=path.name,
            mime="application/pdf",
            key=f"dl_{path.name}",
        )


def page_chat() -> None:
    st.subheader("AI Chat")
    st.info(
        f"**Engine v{__version__}** — fast exclude follow-ups (no planner wait), "
        f"who-is + AMS/YoY/MoM filters. App file: `{Path(__file__).resolve()}`"
    )
    _ver = str(__version__)
    _path = str(Path(__file__).resolve()).lower()
    if (
        not _ver.startswith("1.2")
        or "sales-dashboard-pdf" in _path
        or "ai-chatbot-data-testing" in _path
    ):
        st.error(
            f"Wrong install (v{_ver}). Stop the app (Ctrl+C), then run:\n\n"
            "`curl -fsSL \"https://raw.githubusercontent.com/ssashfaque-creator/"
            "Eva-Foods-Dashboard/cursor/phase1-single-planner-50eb/scripts/update.sh\" | bash`\n\n"
            "Then launch with the full path printed at the end "
            "(`~/Eva-Foods-Dashboard-new/.venv/bin/eva-dashboard`)."
        )
    st.markdown(
        '<p class="eva-subtle">Answers come from your <b>live Eva database</b> only '
        "(not ChatGPT training memory). The assistant must query SQLite before giving numbers.</p>",
        unsafe_allow_html=True,
    )

    from eva_dashboard.chatbot import (
        DEFAULT_MODEL,
        FOLLOWUP_MARKER,
        chat_completion,
        export_chat_training_csv,
        resolve_api_key,
        sales_overview,
    )

    env_key = resolve_api_key()
    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    api_key_input = c1.text_input(
        "OpenAI API key",
        value="",
        type="password",
        placeholder="sk-… (or set OPENAI_API_KEY)",
        help="Used only for this session unless OPENAI_API_KEY is already set in the environment.",
    )
    model_options = ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"]
    default_idx = (
        model_options.index(DEFAULT_MODEL) if DEFAULT_MODEL in model_options else 0
    )
    model = c2.selectbox(
        "Model",
        options=model_options,
        index=default_idx,
    )
    chat_msgs = st.session_state.get("eva_chat_messages") or []
    has_chat_turns = any(m.get("role") == "user" for m in chat_msgs)
    if has_chat_turns:
        csv_blob = export_chat_training_csv(chat_msgs, model=model or DEFAULT_MODEL)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        c3.download_button(
            "Download chat CSV",
            data=csv_blob,
            file_name=f"eva_chat_training_{stamp}.csv",
            mime="text/csv",
            key="chat_download_csv",
            help="Export Q&A turns with blank comment/rating columns for training notes.",
        )
    else:
        c3.button(
            "Download chat CSV",
            key="chat_download_csv_disabled",
            disabled=True,
            help="Ask at least one question first.",
        )
    if c4.button("Clear chat", key="chat_clear"):
        st.session_state.pop("eva_chat_messages", None)
        st.session_state.pop("eva_reply_to", None)
        st.rerun()

    api_key = resolve_api_key(api_key_input) or env_key
    if env_key and not api_key_input:
        st.caption("Using `OPENAI_API_KEY` from the environment.")

    try:
        overview = sales_overview()
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Sales rows", f"{overview['sales_rows']:,}")
        m2.metric("Category map", f"{overview['products_in_category_map']:,}")
        m3.metric("Clients", f"{overview['clients']:,}")
        m4.metric(
            "Sales dates in DB",
            f"{overview['sales_date_min'] or '—'} → {overview['sales_date_max'] or '—'}",
        )
        if overview.get("months_available"):
            months = ", ".join(m["month"] for m in overview["months_available"])
            st.success(
                f"Live data months the chatbot can see: **{months}**. "
                "Ask about these dates — it will query the database."
            )
    except Exception as exc:
        st.warning(f"Could not load DB overview: {exc}")

    with st.expander("Example questions"):
        st.markdown(
            """
**Matrix** — *what were / show / breakdown*:
- What were Eva Consumer sales in Karachi so far in August? *(rows = Packing)*
- Month-wise breakdown of Eva Consumer sales *(last 6 months + Average)*
- Average sale for Imtiaz store last 6 months *(Client Type filter)*
- Then: Show by product *(rows → Packing Category, same months)*
- Then: SKU wise / dissect further *(rows → Product)*
- Or: Add Eva Bulk to this table *(extends the same month table)*
- Or: Does this include bulk? → then Combine the tables / include bulk

**Analytical** — *how were / evaluate / assess*:
- How are Eva Consumer sales doing in Karachi so far in August?
- Evaluate Stand up pouch sales in July

**Client / parties / price**:
- Who are my distributors in Lahore?
- Top 10 parties by AMS in Karachi
- Which Imtiaz store has the highest share of VTF?
- What percent of VTF sales are in Lahore?
- Which distributors are doing well last 3 months?
- Which distributors grew VTF most vs July last year?
- Who is Al Bari?
- Canola standup price for Distributors last week
- What’s the Price Fetch? *(follow-up)*

Use **↩ Reply** under any answer to mark your next question as a follow-up on that table.
            """
        )

    if "eva_chat_messages" not in st.session_state:
        st.session_state["eva_chat_messages"] = []

    reply_to = st.session_state.get("eva_reply_to")
    if reply_to is not None:
        msgs = st.session_state["eva_chat_messages"]
        target = msgs[reply_to] if 0 <= reply_to < len(msgs) else None
        preview = ""
        if target:
            preview = re.sub(r"\s+", " ", (target.get("content") or "")[:140]).strip()
        b1, b2 = st.columns([5, 1])
        with b1:
            st.info(
                f"**Replying as follow-up** to that answer"
                + (f": {preview}…" if preview else "")
                + " — your next question reuses its filters/table."
            )
        with b2:
            if st.button("Cancel", key="eva_reply_cancel"):
                st.session_state.pop("eva_reply_to", None)
                st.rerun()

    for i, msg in enumerate(st.session_state["eva_chat_messages"]):
        role = msg.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = msg.get("content") or ""
        if not content and msg.get("tool_calls"):
            continue
        # Hide the internal follow-up marker from the displayed user bubble
        display = content
        if role == "user" and content.lstrip().startswith("[FOLLOW-UP"):
            parts = content.split("\n\n", 1)
            display = parts[1] if len(parts) > 1 else content
        with st.chat_message(role):
            _chat_markdown(display)
            if role == "assistant" and content.strip():
                follow = msg.get("_eva_followup") or {}
                plan_debug = follow.get("plan_debug")
                if plan_debug:
                    with st.expander("Show plan (QuerySpec + debug)", expanded=False):
                        st.caption(
                            "Use this to separate LLM planning errors from "
                            "Python execution errors when reporting bugs."
                        )
                        st.json(plan_debug)
                can_export = bool(
                    follow.get("export")
                    or follow.get("table_spec")
                    or follow.get("party_spec")
                )
                st.markdown(
                    '<p class="eva-reply-hint">Follow up or export this table:</p>',
                    unsafe_allow_html=True,
                )
                b_reply, b_xlsx, b_pdf = st.columns([1.2, 1, 1])
                with b_reply:
                    if st.button(
                        "↩ Reply",
                        key=f"eva_reply_btn_{i}",
                        type="primary",
                        help="Your next question reuses this answer’s filters and table layout",
                    ):
                        st.session_state["eva_reply_to"] = i
                        st.rerun()
                if can_export:
                    from eva_dashboard.table_export import (
                        export_excel_from_followup,
                        export_pdf_from_followup,
                    )

                    with b_xlsx:
                        try:
                            xlsx = export_excel_from_followup(follow)
                        except Exception as exc:  # noqa: BLE001
                            xlsx = None
                            st.caption(f"Excel export unavailable: {exc}")
                        if xlsx:
                            st.download_button(
                                "⬇ Excel",
                                data=xlsx[0],
                                file_name=xlsx[1],
                                mime=(
                                    "application/vnd.openxmlformats-officedocument"
                                    ".spreadsheetml.sheet"
                                ),
                                key=f"eva_xlsx_{i}",
                                help="Download this table as a readable Excel file",
                            )
                    with b_pdf:
                        try:
                            pdf = export_pdf_from_followup(follow)
                        except Exception as exc:  # noqa: BLE001
                            pdf = None
                            st.caption(f"PDF export unavailable: {exc}")
                        if pdf:
                            st.download_button(
                                "⬇ PDF",
                                data=pdf[0],
                                file_name=pdf[1],
                                mime="application/pdf",
                                key=f"eva_pdf_{i}",
                                help="Download this table as a landscape PDF",
                            )

    prompt = st.chat_input(
        "Ask about Eva Foods data… (or click ↩ Reply under an answer first)"
    )
    if not prompt:
        return

    if not api_key:
        st.error(
            "Add an OpenAI API key above, or export `OPENAI_API_KEY` before launching the app."
        )
        return

    forced_prior = None
    forced_price = None
    forced_party = None
    forced_state = None
    prompt_for_model = prompt
    reply_idx = st.session_state.get("eva_reply_to")
    if reply_idx is not None:
        msgs = st.session_state["eva_chat_messages"]
        if 0 <= reply_idx < len(msgs):
            meta = (msgs[reply_idx].get("_eva_followup") or {})
            forced_prior = meta.get("table_spec")
            forced_price = meta.get("price_spec")
            forced_party = meta.get("party_spec")
            forced_state = meta.get("query_state")
        prompt_for_model = f"{FOLLOWUP_MARKER}\n\n{prompt}"
        st.session_state.pop("eva_reply_to", None)

    st.session_state["eva_chat_messages"].append(
        {"role": "user", "content": prompt_for_model}
    )

    status = st.empty()
    try:
        answer, updated = chat_completion(
            st.session_state["eva_chat_messages"],
            api_key=api_key,
            model=model or DEFAULT_MODEL,
            on_status=lambda s: status.caption(s),
            forced_prior_spec=forced_prior,
            forced_prior_price_spec=forced_price,
            forced_prior_party_spec=forced_party,
            forced_query_state=forced_state,
        )
        status.empty()
        # Keep only user/assistant visible turns + latest tool transcript for continuity
        st.session_state["eva_chat_messages"] = updated
        # Rerun so the new answer appears in the history loop with ↩ Reply under it
        st.rerun()
    except Exception as exc:
        status.empty()
        st.error(str(exc))
        # Still show the user turn that failed
        with st.chat_message("user"):
            st.markdown(prompt)


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
        f"v{__version__} · Data: `{data_root()}` · DB: `{db_path().name}` · "
        f"Update: `eva-dashboard update` · App: `{Path(__file__).resolve()}`"
    )

    tab_sales, tab_costs, tab_clients, tab_reports, tab_chat = st.tabs(
        ["Sales data", "Cost structure", "Client list", "Reports", "AI Chat"]
    )
    with tab_sales:
        page_sales()
    with tab_costs:
        page_costs()
    with tab_clients:
        page_clients()
    with tab_reports:
        page_reports()
    with tab_chat:
        page_chat()


if __name__ == "__main__":
    main()
