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

    # Initialize transfers session state if needed
    transfers_key = f"transfers_{filter_key}"
    if transfers_key not in st.session_state:
        st.session_state[transfers_key] = {}

    transfers = st.session_state[transfers_key]

    # First time loading this MR: initialize from DB chain
    if mr_id not in transfers:
        step0 = _empty_transfer_step()
        step0["mr_date"] = row["MR DATE"] if "MR DATE" in row else date.today()
        transfers[mr_id] = [step0]

        # Load saved chain if exists
        chain_data = chains_map.get(mr_id)
        if chain_data is not None and not chain_data.empty:
            try:
                chain_mrs = chain_data.to_dict("records") if hasattr(chain_data, 'to_dict') else chain_data
                saved_chain = _reconstruct_chain(chain_mrs, selected_company_id=st.session_state["selected_company_id"])

                # Populate steps from saved chain
                prev_total = float(row["Total Amount"])
                for sc in saved_chain:
                    step = _empty_transfer_step()
                    step["company"] = f"{sc.get('co_prefix', 'N/A')}-{sc.get('branch_name', 'N/A')}"
                    step["mr_no"] = sc.get("branch_mr_no", "")
                    step["mr_date"] = sc.get("jute_mr_date", date.today())
                    step["total_amount"] = float(sc.get("total_amount", 0))
                    step["claim_amount"] = float(sc.get("claim_amount", 0))
                    step["net_amount"] = float(sc.get("net_total", 0))
                    step["saved_mr_id"] = sc.get("jute_mr_id")

                    # Back-calculate % rate increase (TODO: use DB column after migration)
                    current_total = float(sc.get("total_amount", 0))
                    if prev_total > 0:
                        step["pct_rate_increase"] = ((current_total - prev_total) / prev_total) * 100
                    else:
                        step["pct_rate_increase"] = 0.0

                    transfers[mr_id].append(step)
                    prev_total = current_total
            except Exception as e:
                st.error(f"Error reconstructing chain: {str(e)}")

        # Add blank step for editing
        transfers[mr_id].append(_empty_transfer_step())

    steps = transfers[mr_id]
    li_data = line_items_map.get(mr_id, [])
    orig_total = float(row["Total Amount"])

    # Render chain header
    st.divider()
    st.subheader(f"Transfer Chain — {row.get('Jute Gate Entry No', 'N/A')} ({row.get('Jute Supplier', 'N/A')})")
    st.write(f"**Original Total:** ₹{orig_total:,.0f}")

    # Render all steps
    for i, step in enumerate(steps):
        _render_step_card(
            step_index=i,
            step=step,
            all_steps=steps,
            line_items=li_data,
            original_total_amount=orig_total,
            mr_id=mr_id,
            filter_key=filter_key
        )


def _render_step_card(step_index, step, all_steps, line_items, original_total_amount, mr_id, filter_key):
    """
    Render individual step card with company/date/% inputs, metrics, line items, and action buttons.

    Inputs:
    - step_index: position in chain (0 = source)
    - step: step dict
    - all_steps: full steps list (for recalculation context)
    - line_items: original line items
    - original_total_amount: source MR total
    - mr_id: parent MR ID
    - filter_key: for session state
    """
    is_saved = "saved_mr_id" in step and step.get("saved_mr_id") is not None
    is_empty = not step.get("company")

    # Card styling
    with st.container(border=True):
        st.markdown(f"### Step {step_index + 1}")

        col1, col2, col3 = st.columns([2, 1, 1])

        # Company selection
        with col1:
            if is_saved:
                st.write(f"**Company:** {step['company']}")
            else:
                co_options, _ = get_company_branch_options()
                co_list = [co["co_id"] for co in co_options] if co_options else []
                step["company"] = st.selectbox(
                    "Company",
                    options=co_list if co_list else [""],
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

        # Status badge
        with col3:
            if is_saved:
                st.write("✓ **Saved**")
            else:
                st.write("● **Editing**")

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
                    min_value=0.0,
                    max_value=100.0,
                    key=f"pct_input_{mr_id}_{step_index}"
                )

                # On change: trigger recalculation
                if abs(new_pct - st.session_state[pct_key]) > 0.0001:
                    all_steps[step_index]["pct_rate_increase"] = new_pct
                    st.session_state[pct_key] = new_pct

                    # Recalculate chain downstream
                    _recalculate_chain(all_steps, line_items, original_total_amount)
                    st.rerun()

        # Summary metrics
        rate = step.get("mr_rate", 0)
        total = step.get("total_amount", 0)
        claim = step.get("claim_amount", 0)
        net = step.get("net_amount", 0)

        st.markdown(f"""
        **Rate:** ₹{rate:.2f} | **Total:** ₹{total:,.0f} | **Claim:** ₹{claim:,.0f} | **Net:** ₹{net:,.0f}
        """)

        # Line items table
        _render_step_line_items(step_index, line_items, all_steps)

        # Action buttons (only for unsaved steps with company set)
        if not is_saved and step.get("company"):
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


def _render_step_line_items(step_index, line_items, all_steps):
    """Render line items table showing original items with calculated amounts for this step.

    Args:
        step_index: Position in chain (0 = source, 1+ = transfer steps)
        line_items: List of original line items, each with:
                    - weight (float): item weight in KG
                    - original_rate (float): original rate per quintal
                    - item_quality (str): item quality/description
        all_steps: Full chain for cumulative multiplier calculation

    Display logic:
        - Source step (0): amount = weight × original_rate / 100
        - Transfer steps (1+): amount = weight × original_rate × cumulative_multiplier / 100
        - Cumulative multiplier is product of all (1 + pct/100) from steps 1 to current
    """
    if not line_items:
        st.write("*(No line items)*")
        return

    # Build table rows
    rows = []
    total_amount = 0.0

    for li in line_items:
        try:
            weight = float(li.get("weight", 0) or 0)
            orig_rate = float(li.get("original_rate", 0) or 0)
            quality = li.get("item_quality", "Item")

            # Calculate amount for this step using cumulative multiplier
            if step_index == 0:
                # Source step: just weight × original rate / 100 (convert per quintal to per kg)
                amount = weight * orig_rate / 100
            else:
                # Transfer step: apply cumulative % increases from step 1 to current step
                multiplier = 1.0
                for i in range(1, step_index + 1):
                    if i < len(all_steps):
                        pct = float(all_steps[i].get("pct_rate_increase", 0) or 0)
                        multiplier *= (1.0 + pct / 100.0)

                # Amount = weight × original_rate × multiplier / 100
                amount = weight * orig_rate * multiplier / 100

            rows.append({
                "Quality": quality,
                "Weight (KG)": int(weight),
                "Original Rate (per quintal)": f"₹{orig_rate:.2f}",
                "Amount": f"₹{amount:,.0f}"
            })
            total_amount += amount

        except (ValueError, TypeError) as e:
            st.warning(f"Error processing line item: {str(e)}")
            continue

    # Add total row
    try:
        total_weight = sum(int(float(li.get("weight", 0) or 0)) for li in line_items)
    except (ValueError, TypeError):
        total_weight = 0

    rows.append({
        "Quality": "**TOTAL**",
        "Weight (KG)": total_weight,
        "Original Rate (per quintal)": "—",
        "Amount": f"**₹{total_amount:,.0f}**"
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

        # Calculate cumulative rate multiplier from step 1 to current step
        rate_multiplier = 1.0
        for i in range(1, step_index + 1):
            if i < len(all_steps):
                pct = float(all_steps[i].get("pct_rate_increase", 0) or 0)
                rate_multiplier *= (1.0 + pct / 100.0)

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
            warehouse_id=None,
            mr_no=0,  # Assigned inside save_transfer_step transaction
        )

        # Determine if this is the final step (returns to source company)
        is_final = (step_co_id == source_co_id and step_branch_id == source_branch_id)

        # Call save_transfer_step from transfer.py
        result = save_transfer_step(
            source_mr_id=mr_id,
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
        )

        # Clear cache to force reload on next render
        cache_keys_to_clear = [
            f"raw_df_{filter_key}",
            f"source_df_{filter_key}",
            f"line_items_{filter_key}",
            f"transfers_{filter_key}",
            f"chains_map_{filter_key}",
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
