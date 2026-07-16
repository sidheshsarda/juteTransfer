"""Warehouse-marked stock page.

Move partial quantities of purchased stock into marked godowns across
companies. Separate from the vertical transfer chain: the balance stays at the
source company, no circular return, no invoices. Stock value = qty * rate.
"""

from datetime import date, datetime

import pandas as pd
import streamlit as st

from ..queries import (
    get_companies,
    get_branches_by_company,
    get_company_branch_options,
    get_jute_mr_with_line_items,
    get_warehouses_by_branch,
    get_marked_warehouses_by_branch,
    set_warehouse_marked,
)
from ..warehouse_stock_ops import save_marked_move, delete_marked_move


def warehouse_stock_page() -> None:
    st.title("Warehouse-Marked Stock")
    st.caption(
        "Move part of a purchased lot into a marked godown at another company. "
        "The balance stays at the source; value of marked stock = quantity x rate. "
        "These MRs are kept out of the vertical transfer chain."
    )

    companies = get_companies()
    if not companies:
        st.info("No companies found.")
        return

    user_id = st.session_state.get("user_id", 1)
    this_year = datetime.now().month and datetime.now().year

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
        year = st.selectbox("Year", options=list(range(this_year, this_year - 6, -1)))
    with c4:
        month = st.selectbox("Month", options=list(range(1, 13)),
                             index=datetime.now().month - 1)

    if not branch_id:
        st.info("Select a branch.")
        return

    with st.expander("Tag godowns as marked"):
        all_wh = get_warehouses_by_branch(branch_id)
        marked_wh = get_marked_warehouses_by_branch(branch_id)
        if not all_wh:
            st.write("No godowns for this branch.")
        else:
            chosen = st.multiselect(
                "Marked godowns (this branch)",
                options=list(all_wh.keys()),
                default=list(marked_wh.keys()),
            )
            if st.button("Save godown tags"):
                chosen_ids = {int(all_wh[n]) for n in chosen}
                for _name, wid in all_wh.items():
                    set_warehouse_marked(int(wid), int(wid) in chosen_ids)
                st.success("Godown tags updated.")
                st.rerun()

    st.markdown("---")
    st.subheader("Source lots (available to mark)")
    src_df = get_jute_mr_with_line_items(year, month, co_id, branch_id, transfer_mode=0)
    if src_df is None or src_df.empty:
        st.info("No source lots for this filter.")
    else:
        cb_options, cb_map = get_company_branch_options()
        for _, r in src_df.iterrows():
            if "jute_mr_li_id" not in r or pd.isna(r["jute_mr_li_id"]):
                continue
            li_id = int(r["jute_mr_li_id"])
            quality = r.get("Item Quality") or "Item"
            available = float(r.get("Weight (KG)") or 0)
            src_rate = float(r.get("MR Rate") or 0)
            with st.container(border=True):
                st.markdown(
                    f"**{quality}** - available **{available:,.2f}** kg @ {src_rate:,.2f}"
                )
                f1, f2, f3 = st.columns(3)
                with f1:
                    cb_label = st.selectbox("To company/branch", options=cb_options,
                                            key=f"cb_{li_id}")
                    tgt = cb_map.get(cb_label)
                with f2:
                    if tgt:
                        tgt_co, tgt_br = tgt
                        mwh = get_marked_warehouses_by_branch(tgt_br)
                        wh_name = st.selectbox("Marked godown",
                                               options=list(mwh.keys()) or ["(none tagged)"],
                                               key=f"wh_{li_id}")
                        wh_id = mwh.get(wh_name)
                    else:
                        tgt_co = tgt_br = wh_id = None
                        st.selectbox("Marked godown", options=["(select company)"],
                                     key=f"wh_{li_id}")
                with f3:
                    move_date = st.date_input("Date", value=date.today(), key=f"dt_{li_id}")
                g1, g2, g3 = st.columns(3)
                with g1:
                    qty = st.number_input("Qty to move", min_value=0.0,
                                          max_value=available if available > 0 else 0.0,
                                          value=0.0, step=1.0, key=f"qty_{li_id}")
                with g2:
                    rate = st.number_input("Rate", min_value=0.0, value=src_rate,
                                           step=1.0, key=f"rate_{li_id}")
                with g3:
                    st.write("")
                    can_move = bool(tgt_br) and bool(wh_id) and qty > 0
                    if st.button("Move to godown", key=f"mv_{li_id}", disabled=not can_move):
                        try:
                            save_marked_move(li_id, float(qty), float(rate),
                                             int(tgt_co), int(tgt_br), int(wh_id),
                                             move_date, user_id)
                            st.success("Moved.")
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))

    st.markdown("---")
    st.subheader("Marked stock here")
    mk_df = get_jute_mr_with_line_items(year, month, co_id, branch_id, transfer_mode=1)
    if mk_df is None or mk_df.empty:
        st.info("No marked stock for this filter.")
    else:
        for _, r in mk_df.iterrows():
            mr_id = int(r["jute_mr_id"])
            quality = r.get("Item Quality") or "Item"
            wt = float(r.get("Weight (KG)") or 0)
            rate = float(r.get("MR Rate") or 0)
            wh = r.get("Warehouse") or "-"
            cc1, cc2 = st.columns([5, 1])
            with cc1:
                st.markdown(
                    f"MR {mr_id} - **{quality}** - {wt:,.2f} kg @ {rate:,.2f} "
                    f"-> godown **{wh}** - value {wt * rate / 100:,.2f}"
                )
            with cc2:
                if st.button("Delete", key=f"del_{mr_id}"):
                    try:
                        delete_marked_move(mr_id, user_id)
                        st.success("Deleted.")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
