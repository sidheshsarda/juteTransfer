"""Warehouse-marked stock page: Lots / Transfer / Marked Stock tabs.

Lots: quality-wise availability + split/merge into app-created lot MRs
(jute_lot_src provenance). Transfer: multi-lot whole-lot batch move into a
marked godown with a common % rate change. Marked Stock: mode-1 stock with
auto sold detection (raw-jute invoice join).
"""

from datetime import date, datetime

import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode

from ..queries import (
    get_companies,
    get_branches_by_company,
    get_company_branch_options,
    get_available_lots,
    get_quality_availability_summary,
    get_marked_stock_with_balance,
    get_lot_provenance,
    get_warehouses_by_branch,
    get_marked_warehouses_by_branch,
    set_warehouse_marked,
)
from ..lot_ops import create_lot, delete_lot
from ..warehouse_stock_ops import save_marked_batch, delete_marked_move


def _lot_grid(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Multi-select grid over available lots. Returns selected rows as a
    DataFrame (empty if none)."""
    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_default_column(resizable=True, filterable=True, sortable=True)
    gb.configure_selection("multiple", use_checkbox=True)
    gb.configure_column("jute_mr_id", hide=True)
    gb.configure_column("jute_mr_li_id", hide=True)
    resp = AgGrid(
        df,
        gridOptions=gb.build(),
        height=320,
        theme="streamlit",
        update_mode=GridUpdateMode.SELECTION_CHANGED,
        key=key,
    )
    sel = resp.get("selected_rows")
    if sel is None:
        return pd.DataFrame()
    return pd.DataFrame(sel)  # handles list[dict] and DataFrame variants


def _render_lots_tab(co_id: int, branch_id: int, year: int, month: int,
                     user_id: int) -> None:
    summary = get_quality_availability_summary(co_id, branch_id, year, month)
    st.subheader("Available by quality")
    if summary is None or summary.empty:
        st.info("No available stock for this filter.")
        return
    st.dataframe(summary, use_container_width=True, hide_index=True)

    lots = get_available_lots(co_id, branch_id, year, month)
    st.subheader("Lots")
    sel = _lot_grid(lots, key="lots_grid")

    st.markdown("**Create a new lot** — take quantities from the selected lines")
    if sel.empty:
        st.caption("Select one or more lines above.")
    else:
        takes = []
        for _, row in sel.iterrows():
            li_id = int(row["jute_mr_li_id"])
            avail = float(row["remaining_kg"])
            k = f"take_{li_id}"
            if k not in st.session_state:
                st.session_state[k] = avail
            qty = st.number_input(
                f"{row['quality']} — MR {row['mr_no']} — available {avail:,.2f} kg",
                min_value=0.0, max_value=avail, step=100.0, key=k,
            )
            takes.append((li_id, float(qty)))
        lot_date = st.date_input("Lot date", value=date.today(), key="lot_date")
        if st.button("Create lot", type="primary", key="btn_create_lot"):
            try:
                new_id = create_lot(takes, lot_date, user_id)
                for li_id, _ in takes:
                    st.session_state.pop(f"take_{li_id}", None)
                st.success(f"Lot MR {new_id} created.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

    lot_mrs = (
        lots[lots["is_lot"] == 1][["jute_mr_id", "mr_no", "mr_date"]]
        .drop_duplicates("jute_mr_id")
        if not lots.empty else pd.DataFrame()
    )
    with st.expander(f"App-created lot MRs ({len(lot_mrs)})"):
        if lot_mrs.empty:
            st.caption("None in this filter.")
        for _, lr in lot_mrs.iterrows():
            mr_id = int(lr["jute_mr_id"])
            c1, c2 = st.columns([5, 1])
            with c1:
                st.markdown(f"Lot MR **{mr_id}** (no. {lr['mr_no']}, {lr['mr_date']})")
                prov = get_lot_provenance(mr_id)
                if not prov.empty:
                    st.dataframe(prov, use_container_width=True, hide_index=True)
            with c2:
                if st.button("Delete", key=f"del_lot_{mr_id}"):
                    try:
                        delete_lot(mr_id, user_id)
                        st.success("Lot deleted; sources restored.")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))


def _render_transfer_tab(co_id: int, branch_id: int, year: int, month: int,
                         user_id: int) -> None:
    st.info("Transfer tab — implemented in the next task.")


def _render_marked_tab(co_id: int, branch_id: int, year: int, month: int,
                       user_id: int) -> None:
    st.info("Marked Stock tab — implemented in a later task.")


def warehouse_stock_page() -> None:
    st.title("Warehouse-Marked Stock")
    st.caption(
        "Restructure lots, batch-move them into marked godowns at other "
        "companies, and track marked stock until the ERP sells it."
    )

    companies = get_companies()
    if not companies:
        st.info("No companies found.")
        return
    user_id = st.session_state.get("user_id", 1)

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        co_name = st.selectbox("Company", options=list(companies.keys()))
        co_id = companies[co_name]
    with c2:
        branches = get_branches_by_company(co_id)
        br_names = list(branches.keys())
        br_name = st.selectbox("Branch", options=br_names) if br_names else None
        branch_id = branches.get(br_name) if br_name else None
    with c3:
        this_year = datetime.now().year
        year = st.selectbox("Year", options=list(range(this_year, this_year - 6, -1)))
    with c4:
        month = st.selectbox("Month", options=list(range(1, 13)),
                             index=datetime.now().month - 1)
    if not branch_id:
        st.info("Select a branch.")
        return

    tab_lots, tab_transfer, tab_marked = st.tabs(
        ["Lots", "Transfer", "Marked Stock"]
    )
    with tab_lots:
        _render_lots_tab(co_id, branch_id, year, month, user_id)
    with tab_transfer:
        _render_transfer_tab(co_id, branch_id, year, month, user_id)
    with tab_marked:
        _render_marked_tab(co_id, branch_id, year, month, user_id)
