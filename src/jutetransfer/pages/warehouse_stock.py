"""Warehouse-marked stock page: Lots / Transfer / Marked Stock tabs.

Lots: quality-wise availability + split/merge of jute_mr_li lines IN PLACE —
no new MRs, jute_lot_src provenance. Transfer: multi-lot whole-lot batch move into a
marked godown with a common % rate change. Marked Stock: mode-1 stock with
balance-based consumption tracking (ERP stock view vw_jute_stock_outstanding;
consumed when remaining balance <= 0).
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
    get_lot_line_provenance,
    get_warehouses_by_branch,
    get_marked_warehouses_by_branch,
    set_warehouse_marked,
)
from ..lot_ops import create_lot, delete_lot_line
from ..lot_helpers import apply_pct, line_price, combine_takes
from ..warehouse_stock_ops import save_marked_batch, delete_marked_move


def _lot_grid(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Multi-select grid over available lots. Returns selected rows as a
    DataFrame (empty if none)."""
    # st-aggrid keeps checkbox state positionally under a fixed key, so a
    # data swap (quality filter / month change) would silently keep or remap
    # selections onto different lines. Deriving the key from the visible
    # line-id set remounts the grid with a clean selection instead.
    ids = tuple(df["jute_mr_li_id"]) if "jute_mr_li_id" in df.columns else ()
    key = f"{key}_{hash(ids) & 0xFFFFFFFF:x}"
    if "mr_date" in df.columns:
        # st_aggrid serialises date objects to JS Dates which AG Grid renders
        # as "[object Object]" — show them as plain ISO strings instead.
        df = df.assign(mr_date=df["mr_date"].astype(str))
    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_default_column(resizable=True, filterable=True, sortable=True)
    # use_checkbox=True would attach the checkbox to the FIRST column def
    # (jute_mr_id, hidden below) and it silently disappears — put it on the
    # first visible column instead; selection is checkbox-only.
    gb.configure_selection("multiple", suppressRowClickSelection=True)
    gb.configure_column(
        "mr_no",
        checkboxSelection=True,
        headerCheckboxSelection=True,
        headerCheckboxSelectionFilteredOnly=True,
    )
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


def _quality_filter(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Quality multiselect over the lots grid; empty selection = all."""
    if df is None or df.empty:
        return df
    quals = sorted(df["quality"].dropna().unique().tolist())
    chosen = st.multiselect("Quality", options=quals, key=key,
                            help="Leave empty to show all qualities")
    return df[df["quality"].isin(chosen)] if chosen else df


def _render_lots_tab(co_id: int, branch_id: int, year: int, month: int,
                     user_id: int) -> None:
    summary = get_quality_availability_summary(co_id, branch_id, year, month)
    st.subheader("Available by quality")
    if summary is None or summary.empty:
        st.info("No available stock for this filter.")
        return
    st.dataframe(summary, use_container_width=True, hide_index=True)

    all_lots = get_available_lots(co_id, branch_id, year, month)
    lots = _quality_filter(all_lots, key="lots_qual")
    st.subheader("Lots")
    sel = _lot_grid(lots, key="lots_grid")

    st.markdown("**Split lots** — take quantities from the selected lines "
                "into new lines within their own MRs")
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
        if st.button("Split into new lot lines", type="primary", key="btn_create_lot"):
            try:
                new_ids = create_lot(takes, user_id)
                for li_id, _ in takes:
                    st.session_state.pop(f"take_{li_id}", None)
                st.success(f"{len(new_ids)} lot line(s) created.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

        st.markdown("**Merge selected into one lot** — drains each selected "
                    "line fully into a single combined line")
        if len(sel) < 2:
            st.caption("Select at least two lines of the same quality to merge.")
        elif sel["quality"].nunique(dropna=False) != 1:
            st.caption("Merging requires all selected lines to share one quality.")
        elif sel["warehouse"].nunique(dropna=False) != 1:
            st.caption("Merging requires all selected lines in the same godown.")
        else:
            merges = [(int(row["jute_mr_li_id"]), float(row["remaining_kg"]))
                      for _, row in sel.iterrows()]
            parts = [(float(row["remaining_kg"]),
                      float(row["rate"]) if pd.notna(row["rate"]) else 0.0)
                     for _, row in sel.iterrows()]
            kg, price, avg_rate = combine_takes(parts)
            st.caption(
                f"{len(sel)} lines → one {sel['quality'].iloc[0]} line: "
                f"{kg:,.2f} kg @ avg rate {avg_rate:,.2f} (value {price:,.2f})"
            )
            if st.button("Merge into one line", key="btn_merge_lot"):
                try:
                    new_ids = create_lot(merges, user_id, merge=True)
                    for li_id, _ in merges:
                        st.session_state.pop(f"take_{li_id}", None)
                    st.success(f"Merged into line {new_ids[0]}.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    lot_lines = (
        all_lots[all_lots["is_lot"] == 1]
        if not all_lots.empty else pd.DataFrame()
    )
    with st.expander(f"App-created lot lines ({len(lot_lines)})"):
        if lot_lines.empty:
            st.caption("None in this filter.")
        for _, lr in lot_lines.iterrows():
            li_id = int(lr["jute_mr_li_id"])
            c1, c2 = st.columns([5, 1])
            with c1:
                st.markdown(
                    f"Line **{li_id}** — {lr['quality']} — MR {lr['mr_no']} "
                    f"({lr['mr_date']}) — {float(lr['remaining_kg']):,.2f} kg"
                )
                prov = get_lot_line_provenance(li_id)
                if not prov.empty:
                    st.dataframe(prov, use_container_width=True, hide_index=True)
            with c2:
                if st.button("Delete", key=f"del_lot_{li_id}"):
                    try:
                        delete_lot_line(li_id, user_id)
                        st.success("Lot line deleted; sources restored.")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))


def _render_transfer_tab(co_id: int, branch_id: int, year: int, month: int,
                         user_id: int) -> None:
    lots = get_available_lots(co_id, branch_id, year, month, include_marked=True)
    if lots is None or lots.empty:
        st.info("No available lots for this filter.")
        return
    lots = _quality_filter(lots, key="xfer_qual")
    st.subheader("Select lots to transfer (whole lots — split first for partials; "
                 "marked stock held here can be resold onward)")
    sel = _lot_grid(lots, key="transfer_grid")
    if sel.empty:
        st.caption("Select one or more lots above.")
        return

    cb_options, cb_map = get_company_branch_options()
    t1, t2, t3, t4 = st.columns(4)
    with t1:
        cb_label = st.selectbox("To company/branch", options=cb_options,
                                key="xfer_cb")
        tgt = cb_map.get(cb_label)
    with t2:
        if tgt:
            tgt_co, tgt_br = tgt
            mwh = get_marked_warehouses_by_branch(tgt_br)
            wh_name = st.selectbox("Marked godown",
                                   options=list(mwh.keys()) or ["(none tagged)"],
                                   key="xfer_wh")
            wh_id = mwh.get(wh_name)
        else:
            tgt_co = tgt_br = wh_id = None
            st.selectbox("Marked godown", options=["(select company)"],
                         key="xfer_wh")
    with t3:
        move_date = st.date_input("Date", value=date.today(), key="xfer_dt")
    with t4:
        if "xfer_pct" not in st.session_state:
            st.session_state["xfer_pct"] = 0.0
        pct = st.number_input("% rate change (+/-)", step=0.5, key="xfer_pct")

    prev = sel.copy()
    prev["new_rate"] = prev["rate"].map(lambda r: apply_pct(float(r or 0), pct))
    prev["value"] = prev.apply(
        lambda r: line_price(float(r["remaining_kg"]), float(r["rate"] or 0)), axis=1)
    prev["new_value"] = prev.apply(
        lambda r: line_price(float(r["remaining_kg"]), float(r["new_rate"])), axis=1)
    show = prev[["mr_no", "quality", "remaining_kg", "rate", "new_rate",
                 "value", "new_value"]]
    st.subheader("Preview")
    st.dataframe(show, use_container_width=True, hide_index=True)
    st.markdown(
        f"**Total: {prev['remaining_kg'].sum():,.2f} kg — "
        f"value {prev['value'].sum():,.2f} → {prev['new_value'].sum():,.2f}**"
    )

    n_src_mrs = prev["jute_mr_id"].nunique()
    st.caption(
        f"Will create {n_src_mrs} MR(s) at the target and {n_src_mrs} seller "
        f"invoice(s) at the source (one per source MR)."
    )
    can_save = bool(tgt) and bool(wh_id)
    if st.button("Transfer selected lots", type="primary",
                 disabled=not can_save, key="btn_batch_move"):
        try:
            li_ids = [int(x) for x in prev["jute_mr_li_id"]]
            created = save_marked_batch(
                li_ids, float(pct), int(tgt_co), int(tgt_br), int(wh_id),
                move_date, user_id,
            )
            st.success(
                "Transferred. " + "; ".join(
                    f"MR {c['child_mr_id']} — invoice {c['invoice_no']} "
                    f"({c['invoice_amount']:,.0f})"
                    for c in created
                )
            )
            st.rerun()
        except Exception as e:
            st.error(str(e))


def _render_marked_tab(co_id: int, branch_id: int, year: int, month: int,
                       user_id: int) -> None:
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

    mk = get_marked_stock_with_balance(co_id, branch_id, year, month)
    st.subheader("Marked stock here")
    if mk is None or mk.empty:
        st.info("No marked stock for this filter.")
        return

    in_stock_val = mk.loc[mk["consumed"] == 0, "value"].sum()
    st.markdown(
        f"**Remaining marked value: {in_stock_val:,.2f}** "
        f"(balance-priced; consumed lots drop out of P&L automatically)"
    )

    for mr_id, grp in mk.groupby("jute_mr_id", sort=True):
        mr_id = int(mr_id)
        consumed = bool((grp["consumed"] == 1).all())
        resold = bool((grp["resold"] == 1).any())
        partially = bool((grp["balance_kg"] < grp["kg"]).any()) and not consumed
        src_raw = grp["src_jute_mr_id"].iloc[0]
        src_label = int(src_raw) if pd.notna(src_raw) else "-"
        head = (
            f"MR {mr_id} (no. {grp['mr_no'].iloc[0]}, {grp['mr_date'].iloc[0]}) "
            f"— source MR {src_label}"
        )
        if partially:
            head += (" — **partially resold**" if resold
                     else " — **partially consumed**")
        c1, c2 = st.columns([5, 1])
        with c1:
            if consumed:
                st.markdown("~~" + head + "~~ "
                            + ("**RESOLD**" if resold else "**CONSUMED**"))
            else:
                st.markdown(head)
            st.dataframe(
                grp[["quality", "kg", "balance_kg", "rate", "value", "godown"]],
                use_container_width=True, hide_index=True,
            )
            prov = get_lot_provenance(mr_id)
            if not prov.empty:
                with st.expander("Source provenance"):
                    st.dataframe(prov, use_container_width=True, hide_index=True)
        with c2:
            # A resold MR can't be deleted until its resale children are
            # (leaf-first guard) — disable rather than offer a doomed button.
            can_delete = (bool((grp["balance_kg"] >= grp["kg"]).all())
                          and not resold)
            if st.button("Delete", key=f"del_mk_{mr_id}", disabled=not can_delete):
                try:
                    delete_marked_move(mr_id, user_id)
                    st.success("Deleted; source restored.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))


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
