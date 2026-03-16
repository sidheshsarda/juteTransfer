"""Jute MR Table page for JuteTransfer application."""

import streamlit as st
import pandas as pd
from datetime import datetime, date
from collections import defaultdict

from ..queries import (
    get_companies,
    get_branches_by_company,
    get_company_branch_options,
    get_jute_mr_with_line_items,
    get_next_mr_numbers_batch,
    get_transfer_chain,
    get_warehouses_by_branch,
)
from ..transfer import (
    finalize_transfer_chain,
    save_transfer_step,
    delete_chain_from_step,
    TransferStep,
)

# Maximum number of transfer steps in a chain
MAX_TRANSFERS = 8

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Columns shown in the compact monthly overview table
COMPACT_COLUMNS = [
    "Jute Gate Entry No", "MR DATE", "Jute Supplier", "Party Name",
    "Party Branch Address", "Item Quality",
    "Weight (KG)", "MR Rate", "Total Amount", "Claim Amount", "Net Total",
    "Transfer Chain", "Chain Status",
]


# ---------------------------------------------------------------------------
# Grouping: collapse per-line-item rows into one row per jute_mr_id
# ---------------------------------------------------------------------------

def _group_by_mr(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Group raw per-line-item dataframe into one row per jute_mr_id.

    Returns:
        (grouped_df, line_items_map) where line_items_map is
        {jute_mr_id: [{weight, original_rate, original_claim}, ...]}
    """
    if df is None or df.empty:
        return df, {}

    line_items_map = {}
    grouped_rows = []

    for mr_id, group in df.groupby("jute_mr_id"):
        # Extract per-line-item details
        items = []
        for _, row in group.iterrows():
            items.append({
                "weight": round(float(row.get("Weight (KG)") or 0), 0),
                "original_rate": float(row.get("MR Rate") or 0),
                "original_claim": float(row.get("Claim Amount") or 0),
            })
        line_items_map[int(mr_id)] = items

        # Aggregated row
        first = group.iloc[0].to_dict()
        total_weight = sum(li["weight"] for li in items)
        total_amount = sum(float(r.get("Total Amount") or 0) for _, r in group.iterrows())
        claim_amount = sum(float(r.get("Claim Amount") or 0) for _, r in group.iterrows())

        first["Weight (KG)"] = total_weight
        first["Total Amount"] = total_amount
        first["Claim Amount"] = claim_amount
        first["Net Total"] = total_amount - claim_amount
        first["MR Rate"] = (total_amount / total_weight * 100) if total_weight > 0 else 0.0

        # Combine qualities
        qualities = group["Item Quality"].dropna().unique()
        if len(qualities) > 1:
            first["Item Quality"] = " / ".join(str(q) for q in qualities)

        grouped_rows.append(first)

    grouped_df = pd.DataFrame(grouped_rows)
    return grouped_df, line_items_map


# ---------------------------------------------------------------------------
# Chain reconstruction: order MR records by src_com_id linkage
# ---------------------------------------------------------------------------

def _reconstruct_chain(chain_mrs: list[dict], root_co_id: int) -> list[dict]:
    """Reconstruct transfer chain order from MR records.

    Args:
        chain_mrs: List of MR dicts with jute_mr_id, src_com_id, owner_co_id, branch_id.
                   Must be sorted by jute_mr_id ASC.
        root_co_id: The root/source company's co_id.

    Returns:
        Ordered list of MR dicts representing the chain.
    """
    if not chain_mrs:
        return []

    # Ensure sorted by jute_mr_id for tie-breaking
    sorted_mrs = sorted(chain_mrs, key=lambda m: m["jute_mr_id"])

    visited = set()
    ordered = []
    current_co = root_co_id

    while True:
        # Find unvisited MR where src_com_id == current_co, lowest jute_mr_id first
        candidate = None
        for mr in sorted_mrs:
            if mr["jute_mr_id"] not in visited and mr.get("src_com_id") == current_co:
                candidate = mr
                break

        if candidate is None:
            break

        visited.add(candidate["jute_mr_id"])
        ordered.append(candidate)
        # Derive next current_co from this MR's owner
        current_co = candidate.get("owner_co_id") or candidate.get("co_id")

    return ordered


# ---------------------------------------------------------------------------
# Transfer data helpers
# ---------------------------------------------------------------------------

def _empty_transfer_step() -> dict:
    """Return a blank transfer step dict."""
    return {
        "company": "",
        "mr_no": None,
        "mr_date": None,
        "pct_rate_increase": 0.0,
        "mr_rate": 0.0,
        "total_amount": 0.0,
        "claim_amount": 0.0,
        "net_amount": 0.0,
        "warehouse_id": None,
    }


def _init_transfer_data(df: pd.DataFrame) -> dict:
    """Initialise per-row transfer state from source dataframe.

    Returns a dict keyed by dataframe index with a list of transfer steps.
    """
    transfers = {}
    for idx in range(len(df)):
        step1 = _empty_transfer_step()
        step1["mr_date"] = df.loc[idx, "MR DATE"]
        step1["mr_rate"] = float(df.loc[idx, "MR Rate"] or 0)
        weight = float(df.loc[idx, "Weight (KG)"] or 0)
        step1["total_amount"] = weight * step1["mr_rate"] / 100
        step1["claim_amount"] = float(df.loc[idx, "Claim Amount"] or 0)
        step1["net_amount"] = step1["total_amount"] - step1["claim_amount"]
        transfers[idx] = [step1]
    return transfers


def _build_chain_summary(steps: list) -> str:
    """Build a human-readable transfer chain summary string."""
    companies = [s["company"] for s in steps if s["company"]]
    if not companies:
        return "No transfers"
    return " -> ".join(companies)


def _get_chain_status(steps: list, source_co_branch: str) -> str:
    """Determine chain status based on whether it returns to source."""
    companies = [s["company"] for s in steps if s["company"]]
    if not companies:
        return "-"
    if companies and companies[-1] == source_co_branch:
        return "Complete"
    return f"{len(companies)} step(s)"


def _recalculate_chain(steps: list, line_items: list) -> list:
    """Recalculate all derived values in a transfer chain.

    Args:
        steps: List of transfer step dicts (mutated in place and returned).
        line_items: List of {weight, original_rate, original_claim} per line item.

    Returns:
        The steps list with computed aggregates filled in.
    """
    weights = [round(float(li.get("weight") or 0), 0) for li in line_items]
    original_rates = [float(li.get("original_rate") or 0) for li in line_items]
    claims = [float(li.get("original_claim") or 0) for li in line_items]
    total_weight = sum(weights)

    prev_rates = list(original_rates)

    for i, step in enumerate(steps):
        if not step.get("company"):
            step["total_amount"] = 0
            step["claim_amount"] = 0
            step["net_amount"] = 0
            step["roundoff"] = 0.0
            step["weighted_avg_rate"] = 0.0
            continue

        if i == 0:
            rates_i = list(prev_rates)
        else:
            pct = float(step.get("pct_rate_increase", 0) or 0)
            rates_i = [r * (1 + pct / 100) for r in prev_rates]

        # Line item level: round to 2
        li_totals = [round(weights[j] * rates_i[j] / 100, 2) for j in range(len(line_items))]
        li_claims = [round(claims[j], 2) for j in range(len(line_items))]

        # Header level: round to 0
        raw_total = sum(li_totals)
        step["total_amount"] = round(raw_total, 0)
        step["roundoff"] = round(raw_total - step["total_amount"], 2)
        step["claim_amount"] = round(sum(li_claims), 0)
        step["net_amount"] = step["total_amount"] - step["claim_amount"]
        step["weighted_avg_rate"] = (step["total_amount"] / total_weight * 100) if total_weight > 0 else 0.0

        prev_rates = rates_i

    return steps


def _assign_mr_numbers_batch(transfers: dict, co_branch_mapping: dict):
    """Assign MR numbers in batch — one DB call per unique (co_id, branch_id)."""
    # Count how many MR numbers each (co_id, branch_id) needs
    counts = defaultdict(int)
    requests = []  # (row_idx, step_idx, co_id, branch_id)

    for row_idx, steps in transfers.items():
        for step_idx, step in enumerate(steps):
            co_label = step.get("company", "")
            if co_label and co_label in co_branch_mapping:
                co_id, branch_id = co_branch_mapping[co_label]
                counts[(co_id, branch_id)] += 1
                requests.append((row_idx, step_idx, co_id, branch_id))

    if not counts:
        return

    # Batch fetch
    mr_numbers = get_next_mr_numbers_batch(dict(counts))

    # Track consumption per pair
    consumed = defaultdict(int)
    for row_idx, step_idx, co_id, branch_id in requests:
        key = (co_id, branch_id)
        idx = consumed[key]
        transfers[row_idx][step_idx]["mr_no"] = mr_numbers[key][idx]
        consumed[key] += 1


# ---------------------------------------------------------------------------
# Source company matching (by IDs, not string matching)
# ---------------------------------------------------------------------------

def _find_source_co_branch(selected_company_id, selected_branch_id,
                           co_branch_mapping: dict) -> str | None:
    """Find the co_branch label for the selected source company/branch by IDs."""
    for label, (co_id, branch_id) in co_branch_mapping.items():
        if co_id == selected_company_id and branch_id == selected_branch_id:
            return label
    return None


# ---------------------------------------------------------------------------
# UI: Transfer editor panel
# ---------------------------------------------------------------------------

def _render_transfer_editor(row_idx: int, row: pd.Series, steps: list,
                            co_branch_options: list, source_co_branch: str,
                            co_branch_mapping: dict, filter_key: str,
                            selected_company_id: int = None,
                            selected_branch_id: int = None):
    """Render the transfer editor panel for a selected row."""
    gate_entry = row["Jute Gate Entry No"]
    party = row["Party Name"] or "Unknown"
    quality = row["Item Quality"] or "-"
    weight = float(row["Weight (KG)"] or 0)
    original_rate = float(row["MR Rate"] or 0)
    original_claim = float(row["Claim Amount"] or 0)

    st.markdown("---")
    st.subheader(f"Transfer Chain — Gate Entry #{gate_entry}")
    st.caption(f"**{party}** | Quality: {quality} | Weight: {weight:,.2f} KG")

    # Source card + transfer step cards
    num_steps = len(steps)
    # Columns: source + each step + add button
    col_count = 1 + num_steps + (1 if num_steps < MAX_TRANSFERS else 0)
    cols = st.columns(col_count, gap="small")

    changed = False

    # Source card (read-only)
    with cols[0]:
        st.markdown("**Source**")
        st.markdown(f"**{source_co_branch or 'N/A'}**")
        st.metric("MR Rate", f"{original_rate:,.2f}")
        total = weight * original_rate / 100
        st.metric("Total Amt", f"{total:,.2f}")
        st.metric("Claim", f"{original_claim:,.2f}")
        st.metric("Net", f"{total - original_claim:,.2f}")

    # Transfer step cards
    for i, step in enumerate(steps):
        with cols[1 + i]:
            step_num = i + 1
            st.markdown(f"**Step {step_num}**")

            # Company selector
            current_co = step.get("company", "")
            co_index = 0
            if current_co in co_branch_options:
                co_index = co_branch_options.index(current_co)

            new_co = st.selectbox(
                "Company",
                options=co_branch_options,
                index=co_index,
                key=f"co_{filter_key}_{row_idx}_{i}",
                label_visibility="collapsed",
            )
            if new_co != current_co:
                step["company"] = new_co
                changed = True

            # MR Date
            if i == 0:
                # First step: use original date, read-only display
                mr_date = step.get("mr_date")
                if mr_date:
                    st.text_input("MR Date", value=str(mr_date), disabled=True,
                                  key=f"date_{filter_key}_{row_idx}_{i}")
                else:
                    st.text_input("MR Date", value="-", disabled=True,
                                  key=f"date_{filter_key}_{row_idx}_{i}")
            else:
                current_date = step.get("mr_date")
                if current_date and isinstance(current_date, (date, datetime)):
                    default_date = current_date if isinstance(current_date, date) else current_date.date()
                else:
                    default_date = date.today()
                new_date = st.date_input(
                    "MR Date",
                    value=default_date,
                    key=f"date_{filter_key}_{row_idx}_{i}",
                )
                if new_date != current_date:
                    step["mr_date"] = new_date
                    changed = True

            # % Rate Increase (step 2+)
            if i >= 1:
                current_pct = float(step.get("pct_rate_increase", 0) or 0)
                new_pct = st.number_input(
                    "% Rate Increase",
                    value=current_pct,
                    min_value=-100.0,
                    max_value=1000.0,
                    step=0.1,
                    format="%.2f",
                    key=f"pct_{filter_key}_{row_idx}_{i}",
                )
                if new_pct != current_pct:
                    step["pct_rate_increase"] = new_pct
                    changed = True

            # Calculated fields (read-only display)
            if step.get("company"):
                mr_no = step.get("mr_no")
                st.text_input("MR No", value=str(mr_no or "-"), disabled=True,
                              key=f"mrno_{filter_key}_{row_idx}_{i}")
                st.metric("MR Rate", f"{step['mr_rate']:,.2f}")
                st.metric("Total Amt", f"{step['total_amount']:,.2f}")
                st.metric("Claim", f"{step['claim_amount']:,.2f}")
                st.metric("Net", f"{step['net_amount']:,.2f}")

    # Add Step button
    if num_steps < MAX_TRANSFERS:
        with cols[-1]:
            st.markdown("&nbsp;")  # spacing
            if st.button("+ Add Step", key=f"add_{filter_key}_{row_idx}",
                         use_container_width=True):
                steps.append(_empty_transfer_step())
                changed = True

    # Remove last step button (if more than 1 step)
    if num_steps > 1:
        if st.button("Remove Last Step", key=f"remove_{filter_key}_{row_idx}"):
            steps.pop()
            changed = True

    # Finalize button (visible when chain returns to source)
    companies_in_chain = [s["company"] for s in steps if s["company"]]
    if companies_in_chain and source_co_branch and companies_in_chain[-1] == source_co_branch:
        st.success("Chain returns to source company — ready for finalization.")
        if st.button("Finalize MR", key=f"finalize_{filter_key}_{row_idx}",
                     type="primary", use_container_width=True):
            with st.spinner("Finalizing transfer chain..."):
                try:
                    source_mr_id = int(row.get("jute_mr_id", 0))
                    if not source_mr_id:
                        st.error("Cannot finalize: source MR ID not found.")
                    else:
                        transfer_steps = []
                        for step in steps:
                            co_label = step.get("company", "")
                            if co_label and co_label in co_branch_mapping:
                                co_id, branch_id = co_branch_mapping[co_label]
                                transfer_steps.append(TransferStep(
                                    co_id=co_id,
                                    branch_id=branch_id,
                                    mr_date=step.get("mr_date") or date.today(),
                                    mr_rate=float(step.get("mr_rate", 0)),
                                    total_amount=float(step.get("total_amount", 0)),
                                    claim_amount=float(step.get("claim_amount", 0)),
                                    net_amount=float(step.get("net_amount", 0)),
                                    mr_no=int(step.get("mr_no") or 0),
                                ))

                        if len(transfer_steps) < 2:
                            st.error("Transfer chain must have at least 2 steps.")
                        elif any(s.mr_no == 0 for s in transfer_steps):
                            st.error("All transfer steps must have MR numbers assigned.")
                        else:
                            result = finalize_transfer_chain(
                                source_mr_id=source_mr_id,
                                steps=transfer_steps,
                                source_co_id=selected_company_id,
                                source_branch_id=selected_branch_id,
                                updated_by=1,
                            )
                            st.success(
                                f"Chain finalized! Created {len(result['mr_ids'])} MR(s) "
                                f"and {len(result['invoice_ids'])} invoice(s)."
                            )
                            # Clear transfer state to refresh
                            for key in [f"transfers_{filter_key}", f"source_df_{filter_key}",
                                        f"selected_row_{filter_key}"]:
                                if key in st.session_state:
                                    del st.session_state[key]
                except Exception as e:
                    st.error(f"Finalization failed: {e}")

    # Recalculate if anything changed
    if changed:
        _recalculate_chain(steps, weight, original_rate, original_claim)
        _assign_mr_numbers_batch(
            {row_idx: steps}, co_branch_mapping
        )
        st.session_state[f"transfers_{filter_key}"][row_idx] = steps
        st.rerun()


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

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

        # Fetch MR data
        df = get_jute_mr_with_line_items(
            year=selected_year,
            month=selected_month,
            company_id=selected_company_id,
            branch_id=selected_branch_id,
        )

        if df is None or df.empty:
            filter_info = f"{MONTH_NAMES[selected_month - 1]} {selected_year}"
            if selected_company_name:
                filter_info = f"{selected_company_name} - {filter_info}"
            if selected_branch_name:
                filter_info = f"{selected_branch_name} @ {filter_info}"
            st.warning(f"No data found for {filter_info}")
            return

        # Initialise transfer state if needed
        transfers_key = f"transfers_{filter_key}"
        source_df_key = f"source_df_{filter_key}"

        if transfers_key not in st.session_state:
            st.session_state[transfers_key] = _init_transfer_data(df)
            st.session_state[source_df_key] = df.copy()

        source_df = st.session_state[source_df_key]
        transfers = st.session_state[transfers_key]

        # Build summary columns
        chain_summaries = []
        chain_statuses = []
        for idx in range(len(source_df)):
            steps = transfers.get(idx, [])
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
            steps = transfers.get(row_idx, [_empty_transfer_step()])

            _render_transfer_editor(
                row_idx=row_idx,
                row=row,
                steps=steps,
                co_branch_options=co_branch_options,
                source_co_branch=source_co_branch,
                co_branch_mapping=co_branch_mapping,
                filter_key=filter_key,
                selected_company_id=selected_company_id,
                selected_branch_id=selected_branch_id,
            )

        # Reset button
        st.markdown("---")
        if st.button("Reset All Transfers", use_container_width=True):
            for key in [transfers_key, source_df_key, selected_row_key]:
                if key in st.session_state:
                    del st.session_state[key]
            st.rerun()

    except Exception as e:
        st.error(f"Error loading Jute MR table: {str(e)}")
        st.info("Make sure the database is configured and the jute_mr table exists.")
