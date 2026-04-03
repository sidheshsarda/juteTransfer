"""Jute MR Table page for JuteTransfer application."""

import streamlit as st
from datetime import datetime

from ..queries import (
    get_companies,
    get_branches_by_company,
    get_company_branch_options,
    get_jute_mr_with_line_items,
    get_transfer_chains_batch,
)
from ..jute_mr_chain_helpers import (
    _group_by_mr,
    _empty_transfer_step,
    _build_chain_summary,
    _get_chain_status,
    _find_source_co_branch,
    COMPACT_COLUMNS,
)
from ..jute_mr_editor import _render_transfer_editor

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def jute_mr_table_page():
    """Display Jute MR table with compact overview and expandable transfer editor."""
    st.title("Jute MR Table")

    # Inline help
    with st.expander("How transfers work"):
        st.markdown(
            "**Transfer Flow:** Gate entry at Company A creates a pending MR. "
            "Company B (first buyer) purchases the jute, then may sell to C, C to D, etc. "
            "The chain continues until jute returns to Company A, at which point "
            "the MR is finalized.\n\n"
            "**Rate Cascading:** Each transfer step can apply a % rate increase on top of "
            "the previous step's rate.\n\n"
            "**Claim Amount:** Cascades unchanged through all transfer steps.\n\n"
            "**To edit transfers:** Select a row in the table below, then use the "
            "transfer editor panel that appears."
        )

    try:
        current_year = datetime.now().year
        current_month = datetime.now().month

        # Fetch companies for dropdown
        company_options = get_companies()

        # Filters — row 1: Company + Branch
        col1, col2 = st.columns(2)

        with col1:
            selected_company_name = st.selectbox(
                "Select Company",
                options=list(company_options.keys()),
                index=0 if company_options else None,
            )
            selected_company_id = (
                company_options.get(selected_company_name)
                if selected_company_name
                else None
            )

        branch_options = (
            get_branches_by_company(selected_company_id)
            if selected_company_id
            else {}
        )

        with col2:
            selected_branch_name = st.selectbox(
                "Select Branch",
                options=list(branch_options.keys()),
                index=0 if branch_options else None,
            )
            selected_branch_id = (
                branch_options.get(selected_branch_name)
                if selected_branch_name
                else None
            )

        # Filters — row 2: Year + Month
        col3, col4 = st.columns(2)

        with col3:
            selected_year = st.selectbox(
                "Select Year",
                options=list(range(current_year, current_year - 10, -1)),
                index=0,
            )

        with col4:
            selected_month = st.selectbox(
                "Select Month",
                options=list(range(1, 13)),
                format_func=lambda x: MONTH_NAMES[x - 1],
                index=current_month - 1,
            )

        # Fetch company-branch options for transfer dropdowns
        co_branch_options, co_branch_mapping = get_company_branch_options()

        # Determine source company-branch label by IDs
        source_co_branch = _find_source_co_branch(
            selected_company_id, selected_branch_id, co_branch_mapping
        )

        # Session state key based on filters
        filter_key = f"{selected_company_id}_{selected_branch_id}_{selected_year}_{selected_month}"

        # Fetch MR data (cached in session state per filter)
        raw_df_key = f"raw_df_{filter_key}"
        if raw_df_key not in st.session_state:
            st.session_state[raw_df_key] = get_jute_mr_with_line_items(
                year=selected_year,
                month=selected_month,
                company_id=selected_company_id,
                branch_id=selected_branch_id,
            )
        df = st.session_state[raw_df_key]

        if df is None or df.empty:
            filter_info = f"{MONTH_NAMES[selected_month - 1]} {selected_year}"
            if selected_company_name:
                filter_info = f"{selected_company_name} - {filter_info}"
            if selected_branch_name:
                filter_info = f"{selected_branch_name} @ {filter_info}"
            st.warning(f"No data found for {filter_info}")
            return

        # Session keys
        transfers_key = f"transfers_{filter_key}"
        source_df_key = f"source_df_{filter_key}"
        line_items_key = f"line_items_{filter_key}"
        chains_map_key = f"chains_map_{filter_key}"

        if transfers_key not in st.session_state:
            # Group by MR header (only on first load for this filter)
            grouped_df, line_items_map = _group_by_mr(df)

            # Initialize transfer state keyed by jute_mr_id
            transfers = {}
            for _, row in grouped_df.iterrows():
                mid = int(row["jute_mr_id"])
                step0 = _empty_transfer_step()
                step0["mr_date"] = row["MR DATE"]
                transfers[mid] = [step0]
            st.session_state[transfers_key] = transfers
            st.session_state[source_df_key] = grouped_df.copy()
            st.session_state[line_items_key] = line_items_map

        source_df = st.session_state[source_df_key]
        transfers = st.session_state[transfers_key]
        line_items_map = st.session_state[line_items_key]

        # Batch-fetch all chains (cached in session state)
        if chains_map_key not in st.session_state:
            all_mr_ids = source_df["jute_mr_id"].astype(int).tolist()
            st.session_state[chains_map_key] = get_transfer_chains_batch(all_mr_ids)
        chains_map = st.session_state[chains_map_key]

        chain_summaries = []
        chain_statuses = []
        for _, srow in source_df.iterrows():
            mid = int(srow["jute_mr_id"])
            chain_df = chains_map.get(mid)
            if chain_df is not None and not chain_df.empty:
                chain_cos = chain_df["co_prefix"].tolist()
                chain_summaries.append(" -> ".join(chain_cos))
                has_mr_no = srow.get("EJM MR No.") is not None
                chain_statuses.append("Complete" if has_mr_no else f"{len(chain_cos)} step(s)")
            else:
                steps = transfers.get(mid, [])
                chain_summaries.append(_build_chain_summary(steps))
                chain_statuses.append(_get_chain_status(steps, source_co_branch))

        # Create display dataframe with summary columns
        display_df = source_df.copy()
        display_df["Transfer Chain"] = chain_summaries
        display_df["Chain Status"] = chain_statuses

        # Show only compact columns (filter to those that exist)
        visible_cols = [c for c in COMPACT_COLUMNS if c in display_df.columns]
        compact_df = display_df[visible_cols]

        # Header info
        filter_info = f"{MONTH_NAMES[selected_month - 1]} {selected_year}"
        if selected_company_name:
            filter_info = f"{selected_company_name} - {filter_info}"
        if selected_branch_name:
            filter_info = f"{selected_branch_name} @ {filter_info}"
        st.success(f"Loaded {len(compact_df)} records for {filter_info}")

        st.markdown("---")
        st.subheader("Monthly MR Overview")
        st.caption("Select a row to edit its transfer chain below.")

        # Interactive dataframe with row selection
        event = st.dataframe(
            compact_df,
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            height=min(400, 50 + len(compact_df) * 35),
            column_config={
                "Weight (KG)": st.column_config.NumberColumn(format="%.2f"),
                "MR Rate": st.column_config.NumberColumn(format="%.2f"),
                "Total Amount": st.column_config.NumberColumn(format="%.2f"),
                "Claim Amount": st.column_config.NumberColumn(format="%.2f"),
                "Net Total": st.column_config.NumberColumn(format="%.2f"),
            },
        )

        # Transfer editor panel for selected row
        selected_rows = event.selection.rows
        selected_row_key = f"selected_row_{filter_key}"

        if selected_rows:
            st.session_state[selected_row_key] = selected_rows[0]
            row_idx = selected_rows[0]
        elif selected_row_key in st.session_state:
            row_idx = st.session_state[selected_row_key]
        else:
            row_idx = None

        if row_idx is not None and row_idx < len(source_df):
            row = source_df.iloc[row_idx]
            mid = int(row["jute_mr_id"])
            steps = transfers.get(mid, [_empty_transfer_step()])
            li_data = line_items_map.get(mid, [])

            _render_transfer_editor(
                mr_id=mid,
                row=row,
                steps=steps,
                line_items=li_data,
                co_branch_options=co_branch_options,
                source_co_branch=source_co_branch,
                co_branch_mapping=co_branch_mapping,
                filter_key=filter_key,
                selected_company_id=selected_company_id,
                selected_branch_id=selected_branch_id,
                chains_map=chains_map,
            )

        # Reset / Refresh button
        st.markdown("---")
        col_reset, col_refresh = st.columns(2)
        with col_reset:
            if st.button("Reset All Transfers", use_container_width=True):
                for key in [transfers_key, source_df_key, line_items_key,
                            selected_row_key, raw_df_key, chains_map_key]:
                    if key in st.session_state:
                        del st.session_state[key]
                st.rerun()
        with col_refresh:
            if st.button("Refresh Data", use_container_width=True):
                for key in [transfers_key, source_df_key, line_items_key,
                            selected_row_key, raw_df_key, chains_map_key]:
                    if key in st.session_state:
                        del st.session_state[key]
                st.rerun()

    except Exception as e:
        st.error(f"Error loading Jute MR table: {str(e)}")
