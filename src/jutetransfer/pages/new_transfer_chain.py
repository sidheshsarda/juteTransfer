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
    get_transfer_chains_batch,
    get_transfer_chain
)
from ..jute_mr_chain_helpers import (
    _group_by_mr,
    _reconstruct_chain,
    _recalculate_chain,
    _calculate_step_total_amount,
    _empty_transfer_step,
)
from ..transfer import save_transfer_step, TransferStep

# Constants
COMPACT_COLUMNS = ["Jute Gate Entry No", "Jute Supplier", "Total Amount", "Claim Amount", "Net Total"]


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
    company_options = get_companies()

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
    st.write(f"**{len(grouped_df)} records found**")

    event = st.dataframe(
        grouped_df[COMPACT_COLUMNS] if COMPACT_COLUMNS else grouped_df,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row"
    )

    # Store selected row index
    if event.selection.rows:
        st.session_state[f"selected_row_{filter_key}"] = event.selection.rows[0]
    else:
        # Clear selection if user deselects
        if f"selected_row_{filter_key}" in st.session_state:
            del st.session_state[f"selected_row_{filter_key}"]


def _render_chain_editor(filter_key):
    """Main editor logic: load chain, reconstruct, render step cards."""
    pass


def _render_step_card(step_index, step, all_steps, line_items, original_total_amount, mr_id, filter_key):
    """Render individual step card with inputs and action buttons."""
    pass


def _render_step_line_items(step_index, line_items, all_steps):
    """Render line items table within step card."""
    pass


def _save_step(step_index, step, all_steps, line_items, original_total_amount, mr_id, filter_key):
    """Save step to database and reload chain."""
    pass
