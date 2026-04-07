"""Streamlit UI component for the transfer editor panel."""

import streamlit as st
import pandas as pd
from datetime import date, datetime

from .jute_mr_chain_helpers import (
    _recalculate_chain,
    _reconstruct_chain,
    _empty_transfer_step,
    _calculate_step_total_amount,
)
from .transfer import (
    save_transfer_step,
    delete_chain_from_step,
    TransferStep,
)
from .queries import (
    get_transfer_chain,
    get_warehouses_by_branch,
)

# Maximum number of transfer steps in a chain
MAX_TRANSFERS = 8

# Placeholder for columns that don't apply in totals row
CALCULATION_CHART_TOTALS_PLACEHOLDER = "-"


def _render_step_calculation_chart(
    step_index: int,
    line_items: list,
    step_dict: dict,
    original_total_amount: float,
    prev_step_rate: float = None,
    steps: list = None
) -> None:
    """Render calculation breakdown chart for an unsaved transfer step.

    Displays line-item details (qty, rate, amount) in a table with totals.
    Different columns shown for Step 1 (no rate increase) vs Step 2+ (with rate increase).

    Args:
        step_index: Current step number (0-indexed)
        line_items: List of dicts with 'weight', 'original_rate', 'original_claim', 'item_quality' keys
        step_dict: Current step's data dict
        original_total_amount: Step 0's total amount (reference)
        prev_step_rate: Previous step's weighted average rate (unused, for backward compatibility)
        steps: List of all step dicts for calculating cumulative rate changes
    """
    # Guard: only render for unsaved steps
    if step_dict.get("saved_mr_id"):
        return

    # Guard: only render if company is selected
    if not step_dict.get("company"):
        return

    # Guard: only render if we have line items
    if not line_items:
        st.info("No line items to display")
        return

    # Get previous step's rate for Step 2+ calculations
    if step_index == 0:
        # Step 1: Use original rates, no % increase
        data = []
        total_qty = 0.0
        total_amount = 0.0

        for line_item in line_items:
            qty = float(line_item.get("weight", 0) or 0)
            rate = round(float(line_item.get("original_rate", 0) or 0), 2)
            amount = round(qty * rate / 100, 2)

            item_name = line_item.get("item_quality", "Item")
            data.append({
                "Item": item_name,
                "Qty (KG)": round(qty, 2),
                "Rate (per quintal)": rate,
                "Amount": amount,
            })

            total_qty += qty
            total_amount += amount

        # Add totals row
        data.append({
            "Item": "TOTAL",
            "Qty (KG)": round(total_qty, 2),
            "Rate (per quintal)": CALCULATION_CHART_TOTALS_PLACEHOLDER,
            "Amount": round(total_amount, 2),
        })

        # Display chart
        st.markdown("**Calculation Breakdown**")
        df = pd.DataFrame(data)
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        # Step 2+: Apply % increase to each item's previous rate
        pct_increase = float(step_dict.get("pct_rate_increase", 0) or 0)

        # Calculate cumulative rate multiplier from previous steps
        # If this is step 2+, multiply by all % increases from step 2 to step_index-1
        cumulative_multiplier = 1.0
        if step_index > 1:
            # Apply cumulative rate changes from steps 1 through step_index-1
            # Note: step 1 has no rate increase (pct=0), so we start from step 2
            for step_i in range(1, step_index):
                step_pct = float(steps[step_i].get("pct_rate_increase", 0) or 0) if steps and step_i < len(steps) else 0.0
                cumulative_multiplier *= (1.0 + step_pct / 100.0)

        data = []
        total_qty = 0.0
        total_amount = 0.0

        for line_item in line_items:
            qty = float(line_item.get("weight", 0) or 0)
            original_rate = float(line_item.get("original_rate", 0) or 0)

            # Old rate: original rate with cumulative multiplier applied (from previous steps)
            old_rate = round(original_rate * cumulative_multiplier, 2)
            # New rate: old rate with current step's % increase applied
            new_rate = round(old_rate * (1.0 + pct_increase / 100.0), 2)
            # Amount: qty * new_rate / 100 (convert from per quintal to per kg)
            amount = round(qty * new_rate / 100, 2)

            item_name = line_item.get("item_quality", "Item")
            data.append({
                "Item": item_name,
                "Qty (KG)": round(qty, 2),
                "Old Rate (per quintal)": old_rate,
                "% Change": f"{pct_increase:.2f}%",
                "New Rate (per quintal)": new_rate,
                "Amount": amount,
            })

            total_qty += qty
            total_amount += amount

        # Add totals row
        data.append({
            "Item": "TOTAL",
            "Qty (KG)": round(total_qty, 2),
            "Old Rate (per quintal)": CALCULATION_CHART_TOTALS_PLACEHOLDER,
            "% Change": CALCULATION_CHART_TOTALS_PLACEHOLDER,
            "New Rate (per quintal)": CALCULATION_CHART_TOTALS_PLACEHOLDER,
            "Amount": round(total_amount, 2),
        })

        # Display chart
        st.markdown("**Calculation Breakdown**")
        df = pd.DataFrame(data)
        st.dataframe(df, use_container_width=True, hide_index=True)


def _render_transfer_editor(mr_id: int, row: pd.Series, steps: list,
                            line_items: list, co_branch_options: list,
                            source_co_branch: str, co_branch_mapping: dict,
                            filter_key: str,
                            selected_company_id: int = None,
                            selected_branch_id: int = None,
                            chains_map: dict = None):
    """Render the transfer editor panel for a selected MR.

    Args:
        mr_id: The jute_mr_id of the selected MR
        row: The grouped row from the overview table
        steps: List of transfer step dicts (from session state)
        line_items: List of {weight, original_rate, original_claim} per line item
        co_branch_options: List of "CoPrefix-BranchName" labels for dropdowns
        source_co_branch: The source company-branch label
        co_branch_mapping: Maps co_branch label to (co_id, branch_id)
        filter_key: Session state key prefix
        selected_company_id: Source company ID
        selected_branch_id: Source branch ID
    """
    gate_entry = row["Jute Gate Entry No"]
    party = row["Party Name"] or "Unknown"
    quality = row["Item Quality"] or "-"
    total_weight = sum(float(li.get("weight") or 0) for li in line_items) if line_items else float(row["Weight (KG)"] or 0)

    st.markdown("---")
    st.subheader(f"Transfer Chain — Gate Entry #{gate_entry}")
    st.caption(f"**{party}** | Quality: {quality} | Weight: {total_weight:,.0f} KG | Items: {len(line_items)}")

    # Check for existing saved chain steps (from cached batch data)
    chain_df = chains_map.get(mr_id) if chains_map else get_transfer_chain(mr_id)
    saved_chain = []
    if chain_df is not None and not chain_df.empty:
        chain_mrs = chain_df.to_dict("records")
        saved_chain = _reconstruct_chain(chain_mrs, selected_company_id or 0)

    # Determine how many steps are already saved vs editable
    num_saved = len(saved_chain)
    editable_steps = steps[num_saved:] if len(steps) > num_saved else [_empty_transfer_step()]

    # Ensure steps list reflects saved + editable
    if num_saved > 0 and len(steps) <= num_saved:
        steps.clear()
        prev_total = float(row.get("Total Amount") or 0)  # Source (Step 0) amount
        for sc in saved_chain:
            saved_step = _empty_transfer_step()
            label = f"{sc.get('co_prefix', '')}-{sc.get('branch_name', '')}"
            saved_step["company"] = label
            saved_step["mr_no"] = sc.get("branch_mr_no")
            saved_step["mr_date"] = sc.get("jute_mr_date")
            saved_step["challan_date"] = sc.get("challan_date")
            current_total = float(sc.get("total_amount") or 0)
            saved_step["total_amount"] = current_total
            saved_step["claim_amount"] = float(sc.get("claim_amount") or 0)
            saved_step["net_amount"] = float(sc.get("net_total") or 0)
            # Calculate pct_rate_increase from the difference between consecutive amounts
            if prev_total > 0:
                saved_step["pct_rate_increase"] = ((current_total - prev_total) / prev_total) * 100
            else:
                saved_step["pct_rate_increase"] = 0.0
            saved_step["saved_mr_id"] = sc.get("jute_mr_id")
            steps.append(saved_step)
            prev_total = current_total
        steps.append(_empty_transfer_step())
        editable_steps = steps[num_saved:]
    elif num_saved > 0 and len(steps) > num_saved:
        # Update existing saved steps with correct pct_rate_increase values
        prev_total = float(row.get("Total Amount") or 0)
        for i in range(num_saved):
            current_total = float(steps[i].get("total_amount") or 0)
            if prev_total > 0:
                steps[i]["pct_rate_increase"] = ((current_total - prev_total) / prev_total) * 100
            else:
                steps[i]["pct_rate_increase"] = 0.0
            prev_total = current_total

    # Source card + step cards
    total_steps = len(steps)
    col_count = 1 + total_steps + (1 if total_steps < MAX_TRANSFERS else 0)
    cols = st.columns(col_count, gap="small")

    changed = False

    # Source card (read-only)
    with cols[0]:
        st.markdown("**Source**")
        st.markdown(f"**{source_co_branch or 'N/A'}**")
        orig_total = float(row.get("Total Amount") or 0)
        orig_claim = float(row.get("Claim Amount") or 0)
        st.metric("Gate Entry Value", f"{orig_total:,.0f}")
        st.caption("📦 Initial recorded value when jute entered system")
        st.metric("Claim", f"{orig_claim:,.0f}")
        st.metric("Net", f"{orig_total - orig_claim:,.0f}")

    # Render each step card
    for i, step in enumerate(steps):
        is_saved = i < num_saved
        with cols[1 + i]:
            step_num = i + 1
            if is_saved:
                st.markdown(f"**Step {step_num}** (saved)")
            else:
                st.markdown(f"**Step {step_num}**")

            if is_saved:
                # Read-only display for saved steps
                st.text_input("Company", value=step.get("company", ""), disabled=True,
                              key=f"co_{filter_key}_{mr_id}_{i}")
                st.text_input("MR Date", value=str(step.get("mr_date") or "-"), disabled=True,
                              key=f"date_{filter_key}_{mr_id}_{i}")
                st.text_input("Challan Date", value=str(step.get("challan_date") or "-"), disabled=True,
                              key=f"challan_date_{filter_key}_{mr_id}_{i}")
                mr_no = step.get("mr_no")
                st.text_input("MR No", value=str(mr_no or "-"), disabled=True,
                              key=f"mrno_{filter_key}_{mr_id}_{i}")

                step_amount = float(step.get('total_amount', 0))
                st.metric("💰 Purchase Price", f"{step_amount:,.0f}")

                if i == 0:
                    st.caption("Baseline price (Step 1)")
                else:
                    # Subsequent steps - compare to previous company's selling price
                    prev_amount = float(steps[i-1].get('total_amount', 0))
                    prev_company = steps[i-1].get("company", "Previous")

                    pct_change = 0.0
                    if prev_amount > 0:
                        pct_change = ((step_amount - prev_amount) / prev_amount) * 100

                    if pct_change > 0:
                        st.caption(f"📈 {pct_change:+.2f}% markup from {prev_company}")
                    elif pct_change < 0:
                        st.caption(f"📉 {pct_change:.2f}% discount from {prev_company}")
                    else:
                        st.caption(f"= Same as {prev_company}")

                st.metric("Claim", f"{step.get('claim_amount', 0):,.0f}")
                st.metric("Net", f"{step.get('net_amount', 0):,.0f}")
            else:
                # Editable step
                current_co = step.get("company", "")
                co_index = 0
                if current_co in co_branch_options:
                    co_index = co_branch_options.index(current_co)

                new_co = st.selectbox(
                    "Company",
                    options=co_branch_options,
                    index=co_index,
                    key=f"co_{filter_key}_{mr_id}_{i}",
                    label_visibility="collapsed",
                )
                if new_co != current_co:
                    step["company"] = new_co
                    changed = True

                # MR Date
                current_date = step.get("mr_date")
                if current_date and isinstance(current_date, (date, datetime)):
                    default_date = current_date if isinstance(current_date, date) else current_date.date()
                else:
                    default_date = date.today()
                new_date = st.date_input(
                    "MR Date",
                    value=default_date,
                    key=f"date_{filter_key}_{mr_id}_{i}",
                )
                if new_date != current_date:
                    step["mr_date"] = new_date
                    changed = True

                # Challan Date — defaults to previous step's MR date (or source MR date for step 1)
                current_challan = step.get("challan_date")
                if current_challan and isinstance(current_challan, (date, datetime)):
                    default_challan = current_challan if isinstance(current_challan, date) else current_challan.date()
                else:
                    # Default: previous step's mr_date, or source MR date for step 1
                    if i > 0:
                        prev_mr_date = steps[i - 1].get("mr_date")
                        if prev_mr_date and isinstance(prev_mr_date, (date, datetime)):
                            default_challan = prev_mr_date if isinstance(prev_mr_date, date) else prev_mr_date.date()
                        else:
                            default_challan = new_date
                    else:
                        # Step 1: use source MR date from the row
                        src_mr_date = row.get("MR DATE")
                        if src_mr_date and isinstance(src_mr_date, (date, datetime)):
                            default_challan = src_mr_date if isinstance(src_mr_date, date) else src_mr_date.date()
                        else:
                            default_challan = new_date
                new_challan = st.date_input(
                    "Challan Date",
                    value=default_challan,
                    key=f"challan_date_{filter_key}_{mr_id}_{i}",
                )
                if new_challan != current_challan:
                    step["challan_date"] = new_challan
                    changed = True

                # Warehouse selector
                if new_co and new_co in co_branch_mapping:
                    _, step_branch_id = co_branch_mapping[new_co]
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
                        key=f"wh_{filter_key}_{mr_id}_{i}",
                    )
                    if new_wh and warehouses and new_wh in warehouses:
                        if warehouses[new_wh] != current_wh_id:
                            step["warehouse_id"] = warehouses[new_wh]
                            changed = True
                    if not warehouses:
                        st.error(f"❌ No warehouses found for Branch ID: {step_branch_id}")
                        st.caption("💡 Check if warehouses are configured for this branch in warehouse_mst")
                        with st.expander("🔍 Debug Info"):
                            st.write(f"Selected: {new_co}")
                            st.write(f"Branch ID: {step_branch_id}")
                else:
                    if new_co:
                        st.warning(f"⚠️ Company-branch mapping issue for '{new_co}'")

                # % Rate Change (step 2+) — ENTER-KEY SUBMISSION REQUIRED
                if i >= 1:
                    pct_key = f"pct_{filter_key}_{mr_id}_{i}"
                    pct_submitted_key = f"pct_submitted_{filter_key}_{mr_id}_{i}"
                    current_pct = float(step.get("pct_rate_increase", 0) or 0)

                    # Initialize widget state only on first render
                    if pct_key not in st.session_state:
                        st.session_state[pct_key] = f"{current_pct:.2f}"
                    if pct_submitted_key not in st.session_state:
                        st.session_state[pct_submitted_key] = (current_pct != 0)

                    def on_pct_enter():
                        """Callback fired when user presses Enter or tabs away."""
                        try:
                            val_str = st.session_state.get(pct_key, "")
                            float(val_str) if val_str else 0.0  # Validate format
                            st.session_state[pct_submitted_key] = True
                        except (ValueError, TypeError):
                            st.session_state[pct_submitted_key] = False

                    st.text_input(
                        "% Rate Change",
                        value=st.session_state[pct_key],
                        key=pct_key,
                        on_change=on_pct_enter,
                        help="Enter positive for increase, negative for decrease (e.g. -5.00). Press Enter to apply.",
                        placeholder="e.g. 5.00 or -3.50",
                    )

                    # Process if submitted via Enter/focus-loss
                    is_submitted = st.session_state.get(pct_submitted_key, False)
                    if is_submitted:
                        try:
                            new_pct = float(st.session_state[pct_key]) if st.session_state[pct_key] else 0.0
                            if new_pct != current_pct:
                                step["pct_rate_increase"] = new_pct
                                changed = True
                                # IMMEDIATELY recalculate totals so metrics display correct values
                                try:
                                    orig_total = float(row.get("Total Amount", 0))
                                except (ValueError, TypeError):
                                    orig_total = 0.0
                                _recalculate_chain(steps, line_items, orig_total)
                        except ValueError:
                            st.error(f"Invalid percentage in Step {i + 1}")
                            st.session_state[pct_submitted_key] = False
                    else:
                        st.caption("⚠️ Press Enter to apply % Rate Change")

                # Calculated fields (read-only display)
                if step.get("company"):
                    step_amount = float(step.get('total_amount', 0))
                    st.metric("💰 Purchase Price", f"{step_amount:,.0f}")

                    if i == 0:
                        st.caption("Baseline price (Step 1)")
                    else:
                        prev_company = steps[i-1].get("company", "Previous")
                        if not step.get("saved_mr_id"):
                            # Unsaved: show the user-entered % directly
                            pct_display = float(step.get("pct_rate_increase", 0) or 0)
                            if pct_display > 0:
                                st.caption(f"📈 {pct_display:+.2f}% markup from {prev_company}")
                            elif pct_display < 0:
                                st.caption(f"📉 {pct_display:.2f}% discount from {prev_company}")
                            else:
                                st.caption(f"= Same as {prev_company}")
                        else:
                            # Saved step: back-calculate from stored totals (ground truth)
                            prev_amount = float(steps[i-1].get('total_amount', 0))
                            pct_change = 0.0
                            if prev_amount > 0:
                                pct_change = ((step_amount - prev_amount) / prev_amount) * 100
                            if pct_change > 0:
                                st.caption(f"📈 {pct_change:+.2f}% markup from {prev_company}")
                            elif pct_change < 0:
                                st.caption(f"📉 {pct_change:.2f}% discount from {prev_company}")
                            else:
                                st.caption(f"= Same as {prev_company}")

                    st.metric("Claim", f"{step.get('claim_amount', 0):,.0f}")
                    st.metric("Net", f"{step.get('net_amount', 0):,.0f}")
                    if step.get("roundoff"):
                        st.caption(f"Roundoff: {step['roundoff']:,.2f}")

    # Render calculation chart for the currently-being-edited step
    # Find the first unsaved step with a company selected
    for i, step in enumerate(steps):
        is_saved = i < num_saved
        if not is_saved and step.get("company"):
            # This is the active step being edited
            prev_step_rate = None
            if i > 0 and i - 1 < len(steps):
                try:
                    prev_step_rate = float(steps[i-1].get("weighted_avg_rate", 0))
                except (ValueError, TypeError):
                    prev_step_rate = 0.0

            try:
                original_amount = float(row.get("Total Amount", 0))
            except (ValueError, TypeError):
                original_amount = 0.0

            _render_step_calculation_chart(
                step_index=i,
                line_items=line_items,
                step_dict=step,
                original_total_amount=original_amount,
                prev_step_rate=prev_step_rate,
                steps=steps,
            )
            break  # Only show chart for the first unsaved step with company

    st.markdown("---")  # Visual separator before action buttons

    # Add Step button
    if total_steps < MAX_TRANSFERS:
        with cols[-1]:
            st.markdown("&nbsp;")
            if st.button("+ Add Step", key=f"add_{filter_key}_{mr_id}",
                         use_container_width=True):
                steps.append(_empty_transfer_step())
                changed = True

    # Remove last step (only unsaved steps)
    if len(steps) > num_saved + 1:
        if st.button("Remove Last Step", key=f"remove_{filter_key}_{mr_id}"):
            steps.pop()
            changed = True

    # Action buttons
    col_actions = st.columns(3)

    # Save Step button — save the first unsaved step that has a company selected
    unsaved_with_company = [
        (i, s) for i, s in enumerate(steps)
        if i >= num_saved and s.get("company")
    ]

    # Check if all % Rate Increase fields have been submitted (required for steps 2+)
    all_pct_submitted = True
    if unsaved_with_company:
        step_idx, step_to_save = unsaved_with_company[0]
        # For the step being saved, check all % fields up to this step
        for check_idx in range(1, step_idx + 1):
            pct_submitted_key = f"pct_submitted_{filter_key}_{mr_id}_{check_idx}"
            if not st.session_state.get(pct_submitted_key, False):
                all_pct_submitted = False
                break

    if unsaved_with_company:
        step_idx, step_to_save = unsaved_with_company[0]
        with col_actions[0]:
            save_btn = st.button(f"Save Step {step_idx + 1}", key=f"save_step_{filter_key}_{mr_id}",
                                 type="primary", disabled=not all_pct_submitted)
            if not all_pct_submitted and step_idx >= 1:
                st.caption("⚠️ Confirm % Rate Change (press Enter) before saving")

            if save_btn:
                with st.spinner("Saving transfer step..."):
                    try:
                        # Validate and sync all percentage values before saving
                        orig_total = float(row.get("Total Amount") or 0)
                        validation_errors = []
                        for idx in range(len(steps)):
                            if idx >= 1 and steps[idx].get("company"):
                                pct_key = f"pct_{filter_key}_{mr_id}_{idx}"
                                pct_value = st.session_state.get(pct_key)
                                if pct_value is not None:
                                    try:
                                        steps[idx]["pct_rate_increase"] = float(pct_value)
                                    except (ValueError, TypeError):
                                        validation_errors.append(f"Step {idx + 1}: Invalid % value")

                        if validation_errors:
                            st.error("Cannot save: " + "; ".join(validation_errors))
                        else:
                            # Recalculate totals with validated pct values using the shared calculation function
                            _recalculate_chain(steps, line_items, orig_total)
                            st.session_state[f"transfers_{filter_key}"][mr_id] = steps

                            co_label = step_to_save["company"]
                            if co_label not in co_branch_mapping:
                                st.error(f"Invalid company: {co_label}")
                            else:
                                co_id, branch_id = co_branch_mapping[co_label]

                                # Use _calculate_step_total_amount to ensure the saved amount matches the chart
                                # This guarantees consistency between display and database
                                calculated_total = _calculate_step_total_amount(
                                    step_index=step_idx,
                                    line_items=line_items,
                                    step_dict=step_to_save,
                                    steps=steps,
                                    original_total_amount=orig_total
                                )

                                # Update step_to_save with the calculated amount to ensure consistency
                                step_to_save["total_amount"] = round(calculated_total, 0)
                                step_to_save["net_amount"] = round(calculated_total - float(step_to_save.get("claim_amount", 0)), 0)

                                # Single-step multiplier: only this step's pct.
                                # The source MR (prev step) already has correct rates.
                                pct_rate = float(step_to_save.get("pct_rate_increase", 0) or 0)
                                single_step_multiplier = 1.0 + pct_rate / 100.0

                                # Use previous step's saved MR as rate base
                                effective_source_mr_id = mr_id
                                if step_idx > 0:
                                    prev_saved_id = steps[step_idx - 1].get("saved_mr_id")
                                    if prev_saved_id:
                                        effective_source_mr_id = prev_saved_id

                                # Determine prev company
                                if step_idx == 0:
                                    prev_co_id = selected_company_id
                                    prev_branch_id = selected_branch_id
                                else:
                                    prev_label = steps[step_idx - 1].get("company", "")
                                    if prev_label in co_branch_mapping:
                                        prev_co_id, prev_branch_id = co_branch_mapping[prev_label]
                                    else:
                                        st.error("Previous step has no valid company.")
                                        return

                                # Check if this is the final step (returns to source)
                                is_final = (co_label == source_co_branch)

                                result = save_transfer_step(
                                    source_mr_id=effective_source_mr_id,
                                    step=TransferStep(
                                        co_id=co_id,
                                        branch_id=branch_id,
                                        mr_date=step_to_save.get("mr_date") or date.today(),
                                        mr_rate=float(step_to_save.get("weighted_avg_rate", 0) or 0),
                                        total_amount=float(step_to_save.get("total_amount", 0)),
                                        claim_amount=float(step_to_save.get("claim_amount", 0)),
                                        net_amount=float(step_to_save.get("net_amount", 0)),
                                        mr_no=0,
                                        pct_rate_increase=pct_rate,
                                        roundoff=float(step_to_save.get("roundoff", 0) or 0),
                                        challan_date=step_to_save.get("challan_date"),
                                        warehouse_id=step_to_save.get("warehouse_id"),
                                    ),
                                    prev_co_id=prev_co_id,
                                    prev_branch_id=prev_branch_id,
                                    source_co_id=selected_company_id,
                                    source_branch_id=selected_branch_id,
                                    root_mr_id=mr_id,
                                    updated_by=1,
                                    rate_multiplier=single_step_multiplier,
                                    is_first_step=(step_idx == 0),
                                    is_final=is_final,
                                    original_source_mr_id=mr_id,
                                )

                                if is_final:
                                    st.success("Chain finalized! Original MR updated.")
                                else:
                                    st.success(f"Step {step_idx + 1} saved! MR #{result.get('mr_id')} created.")

                                # Clear session state to refresh from DB
                                for key in [f"transfers_{filter_key}", f"source_df_{filter_key}",
                                            f"line_items_{filter_key}", f"selected_row_{filter_key}",
                                            f"raw_df_{filter_key}", f"chains_map_{filter_key}"]:
                                    if key in st.session_state:
                                        del st.session_state[key]
                                st.rerun()
                    except Exception as e:
                        st.error(f"Save failed: {e}")

    # Edit from Step X — delete saved steps from a point onward
    if num_saved > 0:
        with col_actions[1]:
            edit_step = st.number_input(
                "Edit from step",
                min_value=1, max_value=num_saved, value=num_saved,
                key=f"edit_from_{filter_key}_{mr_id}",
            )
            if st.button(f"Edit from Step {edit_step}", key=f"do_edit_{filter_key}_{mr_id}"):
                with st.spinner("Deleting steps and refreshing..."):
                    try:
                        # Get the MR ID of the step to edit from
                        from_mr_id = steps[edit_step - 1].get("saved_mr_id")
                        if from_mr_id:
                            delete_chain_from_step(
                                root_mr_id=mr_id,
                                from_mr_id=from_mr_id,
                                source_mr=None,
                                updated_by=1,
                            )
                            st.success(f"Deleted steps from step {edit_step} onward.")
                            for key in [f"transfers_{filter_key}", f"source_df_{filter_key}",
                                        f"line_items_{filter_key}", f"selected_row_{filter_key}",
                                        f"raw_df_{filter_key}", f"chains_map_{filter_key}"]:
                                if key in st.session_state:
                                    del st.session_state[key]
                            st.rerun()
                        else:
                            st.error("Could not find saved MR ID for that step.")
                    except Exception as e:
                        st.error(f"Edit failed: {e}")

    # Recalculate if anything changed (no explicit rerun — Streamlit reruns naturally)
    if changed:
        orig_total = float(row.get("Total Amount") or 0)
        _recalculate_chain(steps, line_items, orig_total)
        st.session_state[f"transfers_{filter_key}"][mr_id] = steps
