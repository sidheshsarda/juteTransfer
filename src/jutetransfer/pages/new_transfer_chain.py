"""
New Vertical Transfer Chain Editor Page
Displays transfer chains in a 3-level hierarchy (filters → MR table → step cards with line items).
"""

from datetime import datetime, date
import pandas as pd
import streamlit as st

from ..queries import (
    get_companies,
    get_branches_by_company,
    get_company_branch_options,
    get_jute_mr_with_line_items,
    get_transfer_chain,
    get_warehouses_by_branch,
    get_invoice_details_by_mr_id,
)
from ..jute_mr_chain_helpers import (
    _group_by_mr,
    _reconstruct_chain,
    _recalculate_chain,
    _empty_transfer_step,
    _cascade_rate,
    _calculate_line_item_amount,
)
from ..transfer import save_transfer_step, delete_chain_from_step, TransferStep

# Known Limitations:
# 1. pct_rate_increase is back-calculated from rounded totals (rounding errors possible)
#    Fix: Add pct_rate_increase DECIMAL(10,4) column to jute_mr table
# 2. Each step saves individually (no bulk save)
# 3. Saved steps are read-only (no unlock/edit feature)
# 4. Line items are non-editable (by design: transfers preserve original items)

# Constants
COMPACT_COLUMNS = ["Jute Gate Entry No", "Jute Supplier", "Party Name", "Total Amount", "Claim Amount", "Net Total"]


def transfer_chain_page():
    """
    Entry point for vertical transfer chain page.

    Flow:
    1. Page title and help
    2. Render filters (company, branch, year, month)
    3. Render monthly MR table
    4. Render chain editor for selected MR
    """
    st.set_page_config(page_title="Transfer Chain Editor", layout="wide")
    st.title("Vertical Transfer Chain Editor")
    st.markdown("""
    **How transfers work:**
    - Step 1 is the source company (receives material at gate entry)
    - Step 2+ are transfer steps (material moves between companies)
    - Each step can increase the rate by a %, which cascades downward
    - Select an MR row to edit its transfer chain
    """)

    # Render filters (this also populates session state keys)
    _render_filters()

    # Build filter key from session state
    filter_key = None
    if "selected_company_id" in st.session_state and "selected_branch_id" in st.session_state:
        filter_key = (
            f"{st.session_state['selected_company_id']}_"
            f"{st.session_state['selected_branch_id']}_"
            f"{st.session_state.get('selected_year', datetime.now().year)}_"
            f"{st.session_state.get('selected_month', datetime.now().month)}"
        )

    # Render table and editor if filter key exists
    if filter_key:
        _render_mr_table(filter_key)
        _render_chain_editor(filter_key)
    else:
        st.info("Select company and branch from filters to continue")


def _render_filters():
    """
    Render dropdown filters for company, branch, year, month.

    Populates session state keys:
    - selected_company_id
    - selected_branch_id
    - selected_year
    - selected_month
    """
    current_year = datetime.now().year
    current_month = datetime.now().month

    # Month name mapping for display
    month_names = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]

    # Row 1: Company + Branch
    col1, col2 = st.columns(2)

    # Fetch companies for dropdown
    try:
        company_options = get_companies()
    except Exception as e:
        st.error(f"Failed to load companies: {str(e)}")
        company_options = {}

    with col1:
        selected_company_name = st.selectbox(
            "Select Company",
            options=list(company_options.keys()),
            index=0 if company_options else None,
            key="company_select",
        )
        selected_company_id = (
            company_options.get(selected_company_name)
            if selected_company_name
            else None
        )

    # Store in session state
    st.session_state["selected_company_id"] = selected_company_id

    # Get branches for selected company
    try:
        branch_options = (
            get_branches_by_company(selected_company_id)
            if selected_company_id
            else {}
        )
    except Exception as e:
        st.error(f"Failed to load branches: {str(e)}")
        branch_options = {}

    with col2:
        selected_branch_name = st.selectbox(
            "Select Branch",
            options=list(branch_options.keys()),
            index=0 if branch_options else None,
            key="branch_select",
        )
        selected_branch_id = (
            branch_options.get(selected_branch_name)
            if selected_branch_name
            else None
        )

    # Store in session state
    st.session_state["selected_branch_id"] = selected_branch_id

    # Row 2: Year + Month
    col3, col4 = st.columns(2)

    with col3:
        selected_year = st.selectbox(
            "Select Year",
            options=list(range(current_year, current_year - 10, -1)),
            index=0,
            key="year_select",
        )

    # Store in session state
    st.session_state["selected_year"] = selected_year

    with col4:
        selected_month = st.selectbox(
            "Select Month",
            options=list(range(1, 13)),
            format_func=lambda x: month_names[x - 1],
            index=current_month - 1,
            key="month_select",
        )

    # Store in session state
    st.session_state["selected_month"] = selected_month


def _render_mr_table(filter_key):
    """
    Load monthly MRs via filters, display in interactive table, handle row selection.

    Caches data in session state by filter_key to avoid re-querying on reruns.

    Session state keys:
    - raw_df_{filter_key} — original query result
    - source_df_{filter_key} — grouped by MR header
    - line_items_{filter_key} — {mr_id: [line_items]}
    - chains_map_{filter_key} — {mr_id: chain_df}
    - selected_row_{filter_key} — selected row index
    """
    st.subheader("Monthly MR Overview")

    # Load data if not cached
    raw_df_key = f"raw_df_{filter_key}"
    if raw_df_key not in st.session_state:
        try:
            raw_df = get_jute_mr_with_line_items(
                year=st.session_state["selected_year"],
                month=st.session_state["selected_month"],
                company_id=st.session_state["selected_company_id"],
                branch_id=st.session_state["selected_branch_id"]
            )

            if raw_df.empty:
                st.info("No MRs found for selected filters")
                return

            # Group by MR header
            grouped_df, line_items_map = _group_by_mr(raw_df)

            # Cache all data
            st.session_state[raw_df_key] = raw_df
            st.session_state[f"source_df_{filter_key}"] = grouped_df
            st.session_state[f"line_items_{filter_key}"] = line_items_map

            # Batch-load all chains
            all_mr_ids = grouped_df["jute_mr_id"].astype(int).tolist()
            chains_dict = {}
            for mr_id in all_mr_ids:
                chain_data = get_transfer_chain(mr_id)
                if chain_data is not None:
                    chains_dict[mr_id] = chain_data
            st.session_state[f"chains_map_{filter_key}"] = chains_dict

        except Exception as e:
            st.error(f"Error loading MRs: {str(e)}")
            return

    # Get cached data
    grouped_df = st.session_state[f"source_df_{filter_key}"]

    # Display table with row selection
    if grouped_df.empty:
        st.info("No MR records to display")
        return

    st.write(f"**{len(grouped_df)} records found**")

    visible_cols = [c for c in COMPACT_COLUMNS if c in grouped_df.columns]
    event = st.dataframe(
        grouped_df[visible_cols] if visible_cols else grouped_df,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row"
    )

    # Store selected row index — guard against missing .selection or .rows attribute
    if not hasattr(event, "selection") or not hasattr(event.selection, "rows"):
        return

    if event.selection.rows:
        st.session_state[f"selected_row_{filter_key}"] = event.selection.rows[0]
    else:
        # Clear selection if user deselects
        if f"selected_row_{filter_key}" in st.session_state:
            del st.session_state[f"selected_row_{filter_key}"]


def _fetch_step_line_items(jute_mr_id: int) -> list:
    """Fetch line items for a specific MR, joined with item_mst for quality names."""
    from ..database import DatabaseConnection
    df = DatabaseConnection.execute_query(
        """
        SELECT li.accepted_weight, li.rate, li.claim_rate, li.warehouse_id,
               COALESCE(im.item_name, CONCAT('Item-', li.actual_item_id)) AS item_quality
        FROM jute_mr_li li
        LEFT JOIN item_mst im ON li.actual_item_id = im.item_id
        WHERE li.jute_mr_id = :mr_id
        """,
        {"mr_id": jute_mr_id},
    )
    if df is None or df.empty:
        return []
    return [
        {
            "weight": float(row.get("accepted_weight", 0) or 0),
            "original_rate": float(row.get("rate", 0) or 0),
            "original_claim": float(row.get("claim_rate", 0) or 0),
            "item_quality": row.get("item_quality", "Item"),
            "warehouse_id": row.get("warehouse_id"),
        }
        for _, row in df.iterrows()
    ]


def _render_chain_editor(filter_key):
    """
    Load transfer chain for selected MR, reconstruct order, render step cards.

    Session state keys:
    - transfers_{filter_key} — {mr_id: [steps]} for editing
    """
    selected_row_key = f"selected_row_{filter_key}"
    if selected_row_key not in st.session_state:
        return  # No row selected

    row_idx = st.session_state[selected_row_key]
    grouped_df = st.session_state[f"source_df_{filter_key}"]

    if row_idx >= len(grouped_df):
        return

    row = grouped_df.iloc[row_idx]
    mr_id = int(row["jute_mr_id"])
    line_items_map = st.session_state[f"line_items_{filter_key}"]
    chains_map = st.session_state[f"chains_map_{filter_key}"]

    # Safely read Total Amount (could be None/NaN)
    raw_total = row.get("Total Amount")
    if raw_total is None or (isinstance(raw_total, float) and pd.isna(raw_total)):
        raw_total = 0.0
    else:
        try:
            raw_total = float(raw_total)
        except (ValueError, TypeError):
            raw_total = 0.0

    # Safely read MR DATE
    raw_mr_date = row.get("MR DATE") if "MR DATE" in row.index else None
    if raw_mr_date is None or (isinstance(raw_mr_date, float) and pd.isna(raw_mr_date)):
        raw_mr_date = date.today()

    # Initialize transfers session state if needed
    transfers_key = f"transfers_{filter_key}"
    if transfers_key not in st.session_state:
        st.session_state[transfers_key] = {}

    transfers = st.session_state[transfers_key]

    # Detect finalization (chain completed back to origin). When finalized, the
    # return-to-origin step is the in-place updated source MR — there is no
    # separate jute_mr row for it, so we synthesize one. Always initialize this
    # so downstream code can rely on it.
    # NOTE: `branch_mr_no` is an Integer column; pandas surfaces DB NULL as
    # float('nan'), and bool(NaN) is True in Python — hence the pd.notna guard.
    _raw_ejm = row.get("EJM MR No.")
    is_finalized = pd.notna(_raw_ejm) and bool(_raw_ejm)

    # First time loading this MR: initialize from DB chain
    if mr_id not in transfers:
        transfers[mr_id] = []

        # Load saved chain if exists
        chain_data = chains_map.get(mr_id)
        if chain_data is not None and not chain_data.empty:
            try:
                chain_mrs = chain_data.to_dict("records") if hasattr(chain_data, 'to_dict') else chain_data
                saved_chain = _reconstruct_chain(chain_mrs, st.session_state["selected_company_id"])

                # If finalized, append a synthetic return-to-origin step built
                # from the (in-place updated) source MR's row. saved_mr_id is
                # set to the root mr_id so delete_chain_from_step's
                # "from_mr_id == root_mr_id" branch handles unfinalize cleanly.
                if is_finalized:
                    _, _co_branch_mapping = get_company_branch_options()
                    sel_co = st.session_state.get("selected_company_id")
                    sel_br = st.session_state.get("selected_branch_id")
                    src_label = next(
                        (lbl for lbl, (cid, bid) in _co_branch_mapping.items()
                         if cid == sel_co and bid == sel_br),
                        "",
                    )
                    if src_label and "-" in src_label:
                        _src_co_prefix, _src_branch_name = src_label.split("-", 1)
                    else:
                        _src_co_prefix, _src_branch_name = src_label, ""
                    saved_chain.append({
                        "jute_mr_id": mr_id,
                        "co_prefix": _src_co_prefix,
                        "branch_name": _src_branch_name,
                        "branch_mr_no": row.get("EJM MR No."),
                        "jute_mr_date": row.get("MR DATE"),
                        "challan_date": row.get("Challan Date"),
                        "total_amount": float(row.get("Total Amount") or 0),
                        "claim_amount": float(row.get("Claim Amount") or 0),
                        "net_total": float(row.get("Net Total") or 0),
                        "is_final_return": True,
                    })

                # Populate steps from saved chain. Seed prev_total from Step 1
                # (saved_chain[0]) when present — Step 1 is a frozen snapshot
                # unaffected by finalization, so it's the true Source amount.
                if saved_chain and not saved_chain[0].get("is_final_return"):
                    prev_total = float(saved_chain[0].get("total_amount") or 0)
                else:
                    prev_total = raw_total
                for sc in saved_chain:
                    step = _empty_transfer_step()
                    step["company"] = f"{sc.get('co_prefix', 'N/A')}-{sc.get('branch_name', 'N/A')}"
                    step["mr_no"] = sc.get("branch_mr_no", "")
                    step["mr_date"] = sc.get("jute_mr_date", date.today())
                    step["total_amount"] = float(sc.get("total_amount", 0))
                    step["claim_amount"] = float(sc.get("claim_amount", 0))
                    step["net_amount"] = float(sc.get("net_total", 0))
                    step["saved_mr_id"] = sc.get("jute_mr_id")
                    step["is_final_return"] = bool(sc.get("is_final_return"))

                    # Back-calculate % rate increase (TODO: use DB column after migration)
                    current_total = float(sc.get("total_amount", 0))
                    if prev_total > 0:
                        step["pct_rate_increase"] = ((current_total - prev_total) / prev_total) * 100
                    else:
                        step["pct_rate_increase"] = 0.0

                    # Load invoice details (LC/contract) for saved steps
                    saved_mr_id = sc.get("jute_mr_id")
                    if saved_mr_id:
                        inv_details = get_invoice_details_by_mr_id(saved_mr_id)
                        if inv_details:
                            step["lc_reference_no"] = inv_details.get("consignment_no") or ""
                            step["lc_date"] = inv_details.get("consignment_date")
                            step["po_no_for_lc"] = str(inv_details.get("contract_no") or "")
                            step["order_date_for_lc"] = inv_details.get("contract_date")

                    transfers[mr_id].append(step)
                    prev_total = current_total
            except Exception as e:
                st.error(f"Error reconstructing chain: {str(e)}")

        # Fetch line items for each saved step's MR (with item names)
        step_line_items_key = f"step_line_items_{filter_key}_{mr_id}"
        if step_line_items_key not in st.session_state:
            step_li_map = {}
            for idx, s in enumerate(transfers[mr_id]):
                saved_id = s.get("saved_mr_id")
                if saved_id:
                    step_li = _fetch_step_line_items(saved_id)
                    if step_li:
                        step_li_map[idx] = step_li
            st.session_state[step_line_items_key] = step_li_map

        # Populate warehouse_id on each saved step from its line items
        step_li_map_local = st.session_state.get(step_line_items_key, {})
        for idx, s in enumerate(transfers[mr_id]):
            if s.get("saved_mr_id") and not s.get("warehouse_id"):
                li_list = step_li_map_local.get(idx, [])
                if li_list:
                    s["warehouse_id"] = li_list[0].get("warehouse_id")

        # Add blank step for new entry — but NOT when the chain is finalized
        # (the synthetic return-to-origin step is the terminal state).
        if not is_finalized:
            new_step = _empty_transfer_step()
            new_step["mr_date"] = raw_mr_date
            transfers[mr_id].append(new_step)

    steps = transfers[mr_id]
    li_data = line_items_map.get(mr_id, [])
    step_li_map = st.session_state.get(f"step_line_items_{filter_key}_{mr_id}", {})

    # Source (Step 0) display total: prefer Step 1's frozen snapshot when it
    # exists (the source MR's row Total Amount is overwritten in-place during
    # finalization, so it can no longer be trusted for "original" display).
    if steps and steps[0].get("saved_mr_id") and not steps[0].get("is_final_return"):
        orig_total = float(steps[0].get("total_amount") or 0)
    else:
        orig_total = raw_total

    # Ensure all step totals are up-to-date before rendering.
    # _recalculate_chain skips saved steps (uses DB total) and fills in unsaved steps.
    _recalculate_chain(steps, li_data, orig_total, use_new_rounding=True)

    # Render chain header
    st.divider()
    st.subheader(f"Transfer Chain — {row.get('Jute Gate Entry No', 'N/A')} ({row.get('Jute Supplier', 'N/A')})")
    st.write(f"**Original Total:** ₹{orig_total:,.0f}")
    # DEBUG: show step_li_map keys and saved_mr_ids
    _debug_saved_ids = {i: s.get("saved_mr_id") for i, s in enumerate(steps) if s.get("saved_mr_id")}
    _debug_li_map_keys = list(step_li_map.keys())
    st.caption(f"[DEBUG] saved_mr_ids: {_debug_saved_ids} | step_li_map keys: {_debug_li_map_keys}")

    # Render all steps
    for i, step in enumerate(steps):
        if step.get("saved_mr_id") and i in step_li_map:
            # Saved step: use its own line items (rates are authoritative)
            step_line_items = step_li_map[i]
            base_step_index = 0  # not used for saved steps
            _li_source_debug = f"own MR #{step['saved_mr_id']} (in step_li_map)"
        else:
            # Unsaved step: use the nearest preceding saved step's line items
            # so that displayed rates reflect the previous step's actual rates,
            # not the original source MR rates.
            step_line_items = li_data
            base_step_index = 0
            _li_source_debug = f"root MR #{mr_id} (fallback to li_data)"
            for j in range(i - 1, -1, -1):
                if steps[j].get("saved_mr_id") and j in step_li_map:
                    step_line_items = step_li_map[j]
                    base_step_index = j
                    _li_source_debug = f"step {j+1} MR #{steps[j]['saved_mr_id']} (prev saved)"
                    break
        _render_step_card(
            step_index=i,
            step=step,
            all_steps=steps,
            line_items=step_line_items,
            original_total_amount=orig_total,
            mr_id=mr_id,
            filter_key=filter_key,
            source_row=row,
            base_step_index=base_step_index,
            li_source_debug=_li_source_debug,
        )


# TODO: Extract to jute_mr_page_helpers.py (shared with jute_mr.py)
def _render_step_card(step_index, step, all_steps, line_items, original_total_amount, mr_id, filter_key, source_row=None, base_step_index=0, li_source_debug=""):
    """
    Render individual step card with company/date/% inputs, metrics, line items, and action buttons.

    Inputs:
    - step_index: position in chain (0 = source)
    - step: step dict
    - all_steps: full steps list (for recalculation context)
    - line_items: line items for rate display (may be from a saved predecessor)
    - original_total_amount: source MR total
    - mr_id: parent MR ID
    - filter_key: for session state
    - source_row: source MR row (for challan_date, PO no. display)
    - base_step_index: index of the step whose line items are being used as base
                       (for unsaved steps following a saved step)
    - li_source_debug: debug string showing which MR's line items are used
    """
    is_saved = "saved_mr_id" in step and step.get("saved_mr_id") is not None
    is_empty = not step.get("company")

    # Card styling
    with st.container(border=True):
        st.markdown(f"### Step {step_index + 1}")
        # DEBUG: show which MR's line items are being used for rate display
        if li_source_debug:
            st.caption(f"[DEBUG] Line items from: {li_source_debug}")

        col1, col2, col3 = st.columns([2, 1, 1])

        # Company selection
        with col1:
            if is_saved:
                st.write(f"**Company:** {step['company']}")
            else:
                co_options, _ = get_company_branch_options()
                step["company"] = st.selectbox(
                    "Company",
                    options=co_options,
                    key=f"company_{mr_id}_{step_index}",
                    disabled=is_saved
                )

        # Date input
        with col2:
            current_date = step.get("mr_date", date.today())
            if isinstance(current_date, str):
                try:
                    current_date = datetime.strptime(current_date, "%Y-%m-%d").date()
                except:
                    current_date = date.today()

            if is_saved:
                st.write(f"**Date:** {current_date}")
            else:
                step["mr_date"] = st.date_input(
                    "Date",
                    value=current_date,
                    key=f"date_{mr_id}_{step_index}",
                    disabled=is_saved
                )

        # Status badge + % rate increase
        with col3:
            if is_saved:
                pct = step.get("pct_rate_increase", 0)
                if pct and abs(pct) > 0.001:
                    st.write(f"✓ **Saved** — ↑ {pct:.2f}%")
                else:
                    st.write("✓ **Saved**")
            else:
                st.write("● **Editing**")

        # Row A: Challan Date, PO No., Warehouse, Transport toggle
        col_ch, col_po, col_wh, col_tr = st.columns(4)

        with col_ch:
            challan_dt = source_row.get("Challan Date") if source_row is not None else None
            if challan_dt is not None and not (isinstance(challan_dt, float) and pd.isna(challan_dt)):
                st.write(f"**Challan Date:** {challan_dt}")
            else:
                st.write("**Challan Date:** —")

        with col_po:
            po_no = source_row.get("PO.No.") if source_row is not None else None
            if po_no is not None and not (isinstance(po_no, float) and pd.isna(po_no)):
                st.write(f"**PO No.:** {po_no}")
            else:
                st.write("**PO No.:** —")

        with col_wh:
            if is_saved:
                # Display warehouse name for saved steps (look up from warehouse_id)
                wh_id = step.get("warehouse_id")
                if wh_id:
                    # Resolve company label to branch_id for warehouse lookup
                    _, co_branch_mapping = get_company_branch_options()
                    company_label = step.get("company", "")
                    if company_label in co_branch_mapping:
                        _, step_branch_id = co_branch_mapping[company_label]
                        warehouses = get_warehouses_by_branch(step_branch_id)
                        wh_name = next((n for n, wid in warehouses.items() if wid == wh_id), str(wh_id))
                        st.write(f"**Warehouse:** {wh_name}")
                    else:
                        st.write(f"**Warehouse:** ID {wh_id}")
                else:
                    st.write("**Warehouse:** —")
            else:
                # Editable warehouse selectbox
                company_label = step.get("company", "")
                _, co_branch_mapping = get_company_branch_options()
                if company_label and company_label in co_branch_mapping:
                    _, step_branch_id = co_branch_mapping[company_label]
                    warehouses = get_warehouses_by_branch(step_branch_id)
                    wh_names = [""] + list(warehouses.keys()) if warehouses else [""]
                    current_wh_id = step.get("warehouse_id")
                    current_wh_name = ""
                    if warehouses:
                        for wn, wid in warehouses.items():
                            if wid == current_wh_id:
                                current_wh_name = wn
                                break
                    wh_index = wh_names.index(current_wh_name) if current_wh_name in wh_names else 0
                    new_wh = st.selectbox(
                        "Warehouse",
                        options=wh_names,
                        index=wh_index,
                        key=f"wh_{mr_id}_{step_index}",
                    )
                    if new_wh and warehouses and new_wh in warehouses:
                        step["warehouse_id"] = warehouses[new_wh]
                else:
                    st.write("**Warehouse:** —")

        with col_tr:
            if is_saved:
                st.write("**Transport:** Copied" if step.get("transfer_transport", True) else "**Transport:** Hand cart")
            else:
                transport_key = f"transport_{mr_id}_{step_index}"
                step["transfer_transport"] = st.checkbox(
                    "Transfer transport details",
                    value=step.get("transfer_transport", True),
                    key=transport_key,
                )

        # Row B: LC/Contract inputs (step 2+ only, since step 0 has no invoice)
        if step_index > 0:
            if is_saved:
                # Display read-only LC/contract values
                lc_ref = step.get("lc_reference_no", "")
                lc_dt = step.get("lc_date")
                po_lc = step.get("po_no_for_lc", "")
                od_lc = step.get("order_date_for_lc")
                col_lc1, col_lc2, col_lc3, col_lc4 = st.columns(4)
                with col_lc1:
                    st.write(f"**LC Ref No.:** {lc_ref or '—'}")
                with col_lc2:
                    st.write(f"**LC Date:** {lc_dt or '—'}")
                with col_lc3:
                    st.write(f"**PO No. (LC):** {po_lc or '—'}")
                with col_lc4:
                    st.write(f"**Order Date (LC):** {od_lc or '—'}")
            elif step.get("company"):
                # Editable LC/contract inputs
                col_lc1, col_lc2, col_lc3, col_lc4 = st.columns(4)
                with col_lc1:
                    step["lc_reference_no"] = st.text_input(
                        "LC Reference No.",
                        value=step.get("lc_reference_no", ""),
                        key=f"lc_ref_{mr_id}_{step_index}",
                    )
                with col_lc2:
                    lc_date_val = step.get("lc_date")
                    if isinstance(lc_date_val, str):
                        try:
                            lc_date_val = datetime.strptime(lc_date_val, "%Y-%m-%d").date()
                        except:
                            lc_date_val = None
                    step["lc_date"] = st.date_input(
                        "LC Date",
                        value=lc_date_val,
                        key=f"lc_date_{mr_id}_{step_index}",
                    )
                with col_lc3:
                    step["po_no_for_lc"] = st.text_input(
                        "PO No. for LC",
                        value=step.get("po_no_for_lc", ""),
                        key=f"po_lc_{mr_id}_{step_index}",
                    )
                with col_lc4:
                    od_val = step.get("order_date_for_lc")
                    if isinstance(od_val, str):
                        try:
                            od_val = datetime.strptime(od_val, "%Y-%m-%d").date()
                        except:
                            od_val = None
                    step["order_date_for_lc"] = st.date_input(
                        "Order Date for LC",
                        value=od_val,
                        key=f"od_lc_{mr_id}_{step_index}",
                    )

        # % Rate Increase input (for step 2+, unsaved only)
        if step_index > 0 and not is_saved and step.get("company"):
            pct_key = f"pct_{mr_id}_{step_index}"
            if pct_key not in st.session_state:
                st.session_state[pct_key] = float(step.get("pct_rate_increase", 0) or 0)

            col_pct, col_space = st.columns([1, 3])
            with col_pct:
                new_pct = st.number_input(
                    "% Rate Increase",
                    value=st.session_state[pct_key],
                    step=0.01,
                    min_value=-100.0,
                    max_value=100.0,
                    key=f"pct_input_{mr_id}_{step_index}"
                )

                # On change: trigger recalculation
                if abs(new_pct - st.session_state[pct_key]) > 0.0001:
                    all_steps[step_index]["pct_rate_increase"] = new_pct
                    st.session_state[pct_key] = new_pct

                    # Recalculate chain downstream
                    _recalculate_chain(all_steps, line_items, original_total_amount, use_new_rounding=True)
                    st.rerun()

        # Summary metrics — use step dict values (from _recalculate_chain for unsaved,
        # from DB header for saved). Recomputing from per-item rates diverges due to
        # intermediate rounding at each step.
        total = step.get("total_amount", 0)
        claim = step.get("claim_amount", 0)
        net = step.get("net_amount", 0)

        st.markdown(f"""
        **Total:** ₹{total:,.0f} | **Claim:** ₹{claim:,.0f} | **Net:** ₹{net:,.0f}
        """)

        # Line items table — saved steps with own line items use rates directly
        _render_step_line_items(step_index, line_items, all_steps, is_saved=is_saved, base_step_index=base_step_index)

        # Action buttons
        if is_saved:
            # Saved steps: offer "Delete from here" (deletes this step and all subsequent)
            if st.button(
                f"Delete from Step {step_index + 1} onward",
                key=f"delete_saved_{mr_id}_{step_index}",
            ):
                saved_mr_id = step.get("saved_mr_id")
                if saved_mr_id:
                    with st.spinner("Deleting steps and refreshing..."):
                        try:
                            delete_chain_from_step(
                                root_mr_id=mr_id,
                                from_mr_id=saved_mr_id,
                                updated_by=st.session_state.get("user_id", 1),
                            )
                            st.success(f"Deleted steps from step {step_index + 1} onward.")
                            for key in [
                                f"transfers_{filter_key}",
                                f"source_df_{filter_key}",
                                f"line_items_{filter_key}",
                                f"selected_row_{filter_key}",
                                f"raw_df_{filter_key}",
                                f"chains_map_{filter_key}",
                                f"step_line_items_{filter_key}_{mr_id}",
                            ]:
                                if key in st.session_state:
                                    del st.session_state[key]
                            st.rerun()
                        except Exception as e:
                            st.error(f"Delete failed: {e}")
                else:
                    st.error("Could not find saved MR ID for this step.")

        elif step.get("company"):
            # Unsaved steps with company set: save, clear, or remove
            col_save, col_clear, col_delete = st.columns(3)

            with col_save:
                if st.button("Save Step", key=f"save_{mr_id}_{step_index}"):
                    _save_step(step_index, step, all_steps, line_items, original_total_amount, mr_id, filter_key)

            with col_clear:
                if st.button("Clear", key=f"clear_{mr_id}_{step_index}"):
                    all_steps[step_index] = _empty_transfer_step()
                    st.rerun()

            with col_delete:
                if st.button("Delete", key=f"delete_{mr_id}_{step_index}"):
                    all_steps.pop(step_index)
                    st.rerun()

        st.divider()


# TODO: Extract to jute_mr_page_helpers.py (shared with jute_mr.py)
def _render_step_line_items(step_index, line_items, all_steps, is_saved=False, base_step_index=0):
    """Render line items table showing items with rates for this step.

    Args:
        step_index: Position in chain (0 = source, 1+ = transfer steps)
        line_items: Line items for this step. For saved steps these come from
                    that step's own jute_mr_li (rates are the purchase price).
                    For unsaved steps these come from the nearest preceding
                    saved step (or source MR if no saved predecessor).
        all_steps: Full chain for cumulative multiplier calculation
        is_saved: True when line_items come from this step's own MR (use rates directly)
        base_step_index: Index of the step whose line items are being used as
                         base rates. The cumulative multiplier only accumulates
                         from steps AFTER this index.

    Display logic:
        - Saved steps or source step (0): amount = weight × rate / 100 (rate used as-is)
        - Unsaved transfer steps (1+): amount = weight × base_rate × multiplier / 100
          where multiplier only includes pcts from (base_step_index+1) to step_index
    """
    if not line_items:
        st.write("*(No line items)*")
        return

    # Build table rows
    rows = []
    total_amount = 0.0

    def _is_missing(v):
        """True for None or NaN."""
        return v is None or (isinstance(v, float) and v != v)

    for li in line_items:
        try:
            raw_weight = li.get("weight")
            raw_rate = li.get("original_rate")
            quality = li.get("item_quality", "Item")

            weight_missing = _is_missing(raw_weight)
            rate_missing = _is_missing(raw_rate)

            # Coerce to numbers for math (None/NaN -> 0)
            weight = 0.0 if weight_missing else round(float(raw_weight), 0)
            orig_rate = 0.0 if rate_missing else float(raw_rate)

            # Use shared _cascade_rate for round-then-cascade consistency
            if is_saved or step_index == 0:
                effective_rate = orig_rate
            else:
                # Build a sub-chain from base_step_index to step_index
                # _cascade_rate expects steps[1..up_to_index] to have pct_rate_increase
                sub_steps = [{}]  # dummy step 0
                start = max(1, base_step_index + 1)
                for i in range(start, step_index + 1):
                    if i < len(all_steps):
                        sub_steps.append(all_steps[i])
                effective_rate = _cascade_rate(orig_rate, sub_steps, len(sub_steps) - 1)

            # Calculate amount (only meaningful when both weight and rate are present)
            amount_known = not (weight_missing or rate_missing)
            amount = _calculate_line_item_amount(weight, effective_rate) if amount_known else 0.0

            warehouse_name = li.get("warehouse_name") or li.get("Warehouse") or "—"
            rows.append({
                "Quality": quality,
                "Weight (KG)": "—" if weight_missing else int(weight),
                "Warehouse": warehouse_name,
                "Rate (per quintal)": "—" if rate_missing else f"₹{effective_rate:,.0f}",
                "Amount": "—" if not amount_known else f"₹{amount:,.2f}",
            })
            if amount_known:
                total_amount += amount

        except (ValueError, TypeError) as e:
            st.warning(f"Error processing line item: {str(e)}")
            continue

    # Add total row (skip missing weights so '—' rows don't poison the sum)
    valid_weights = [
        int(float(li.get("weight"))) for li in line_items
        if not _is_missing(li.get("weight"))
    ]
    total_weight = sum(valid_weights) if valid_weights else 0

    rows.append({
        "Quality": "**TOTAL**",
        "Weight (KG)": total_weight if valid_weights else "—",
        "Warehouse": "—",
        "Rate (per quintal)": "—",
        "Amount": f"**₹{total_amount:,.2f}**" if total_amount > 0 else "—",
    })

    # Display table
    st.markdown("**Line Items**")
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


def _save_step(step_index, step, all_steps, line_items, original_total_amount, mr_id, filter_key):
    """
    Save step to database via save_transfer_step().
    Clear cache to force reload, then rerun.
    """
    # Validate required fields before saving
    if not step.get("company"):
        st.error("Company is required before saving.")
        return
    if not step.get("mr_date"):
        st.error("Date is required before saving.")
        return

    try:
        # Get source row for context
        source_df = st.session_state[f"source_df_{filter_key}"]
        row_idx = st.session_state[f"selected_row_{filter_key}"]
        source_row = source_df.iloc[row_idx]

        # Resolve company label to (co_id, branch_id) via mapping
        company_label = step.get("company", "")
        _, co_branch_mapping = get_company_branch_options()
        if company_label not in co_branch_mapping:
            st.error(f"Unknown company selection: {company_label}")
            return
        step_co_id, step_branch_id = co_branch_mapping[company_label]

        # Determine previous step's co_id and branch_id
        if step_index > 0 and step_index - 1 < len(all_steps):
            prev_step = all_steps[step_index - 1]
            prev_label = prev_step.get("company", "")
            if prev_label in co_branch_mapping:
                prev_co_id, prev_branch_id = co_branch_mapping[prev_label]
            else:
                # Fallback to source company/branch
                prev_co_id = st.session_state.get("selected_company_id", 1)
                prev_branch_id = st.session_state.get("selected_branch_id", 1)
        else:
            prev_co_id = st.session_state.get("selected_company_id", 1)
            prev_branch_id = st.session_state.get("selected_branch_id", 1)

        source_co_id = st.session_state.get("selected_company_id", 1)
        source_branch_id = st.session_state.get("selected_branch_id", 1)

        # Single-step rate multiplier: only this step's pct increase.
        # The source MR (set below) already has the previous step's rates baked in.
        pct_rate = float(step.get("pct_rate_increase", 0) or 0)
        rate_multiplier = 1.0 + pct_rate / 100.0

        # Use previous step's saved MR as rate base (matches display path).
        # For step 0, use root MR (no previous step).
        effective_source_mr_id = mr_id
        if step_index > 0:
            prev_saved_id = all_steps[step_index - 1].get("saved_mr_id")
            if prev_saved_id:
                effective_source_mr_id = prev_saved_id

        # Parse step values
        pct_rate = float(step.get("pct_rate_increase", 0) or 0)
        total_amt = float(step.get("total_amount", 0) or 0)
        claim_amt = float(step.get("claim_amount", 0) or 0)
        net_amt = float(step.get("net_amount", 0) or 0)
        mr_rate = float(step.get("mr_rate", 0) or 0)
        mr_date_val = step.get("mr_date", date.today())

        # Prepare TransferStep dataclass
        transfer_step = TransferStep(
            co_id=step_co_id,
            branch_id=step_branch_id,
            mr_date=mr_date_val,
            mr_rate=mr_rate,
            pct_rate_increase=pct_rate,
            total_amount=total_amt,
            claim_amount=claim_amt,
            net_amount=net_amt,
            warehouse_id=step.get("warehouse_id"),
            mr_no=0,  # Assigned inside save_transfer_step transaction
            lc_reference_no=step.get("lc_reference_no", ""),
            lc_date=step.get("lc_date"),
            po_no_for_lc=step.get("po_no_for_lc", ""),
            order_date_for_lc=step.get("order_date_for_lc"),
            transfer_transport=step.get("transfer_transport", True),
        )

        # Determine if this is the final step (returns to source company)
        is_final = (step_co_id == source_co_id and step_branch_id == source_branch_id)

        # Debug log to file (Streamlit swallows stdout)
        import os
        _log_path = os.path.join(os.path.dirname(__file__), "..", "..", "debug_transfer.log")
        with open(_log_path, "a") as _f:
            _f.write(f"[{datetime.now().isoformat()}] _save_step: step_index={step_index}, "
                     f"step_co_id={step_co_id}, step_branch_id={step_branch_id}, "
                     f"source_co_id={source_co_id}, source_branch_id={source_branch_id}, "
                     f"is_final={is_final}, is_first_step={step_index == 0}\n")

        # Call save_transfer_step from transfer.py
        result = save_transfer_step(
            source_mr_id=effective_source_mr_id,
            step=transfer_step,
            prev_co_id=prev_co_id,
            prev_branch_id=prev_branch_id,
            source_co_id=source_co_id,
            source_branch_id=source_branch_id,
            root_mr_id=mr_id,
            updated_by=st.session_state.get("user_id", 1),
            rate_multiplier=rate_multiplier,
            is_first_step=(step_index == 0),
            is_final=is_final,
            original_source_mr_id=mr_id,
            use_new_rounding=True,
        )

        # Clear cache to force reload on next render
        cache_keys_to_clear = [
            f"raw_df_{filter_key}",
            f"source_df_{filter_key}",
            f"line_items_{filter_key}",
            f"transfers_{filter_key}",
            f"chains_map_{filter_key}",
            f"step_line_items_{filter_key}_{mr_id}",
        ]
        for key in cache_keys_to_clear:
            if key in st.session_state:
                del st.session_state[key]

        st.success(f"Step {step_index + 1} saved!")
        st.rerun()

    except Exception as e:
        st.error(f"Error saving step: {str(e)}")
        import traceback
        st.write(traceback.format_exc())
