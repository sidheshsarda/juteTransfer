# Calculation Chart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display a breakdown table below transfer step cards showing line-item quantities, rates, and calculated amounts, with totals.

**Architecture:** Create a single helper function `_render_step_calculation_chart()` in `jute_mr_editor.py` that builds a pandas DataFrame from line items and step data, then renders it using Streamlit. The function calculates rates and amounts with proper rounding (2 decimals) and displays appropriate columns based on step number. Call this function from `_render_transfer_editor()` after rendering step cards for unsaved/editable steps only.

**Tech Stack:** Streamlit, pandas, Python 3.x

---

## File Structure

| File | Responsibility |
|------|-----------------|
| `src/jutetransfer/jute_mr_editor.py` | Add `_render_step_calculation_chart()` function; call it from `_render_transfer_editor()` |

---

## Task 1: Create the Calculation Chart Rendering Function

**Files:**
- Modify: `src/jutetransfer/jute_mr_editor.py` (add new function)

- [ ] **Step 1: Add helper function skeleton at top of file (after imports)**

Add this function after the imports but before `_render_transfer_editor()`:

```python
def _render_step_calculation_chart(
    step_index: int,
    line_items: list,
    step_dict: dict,
    original_total_amount: float,
    prev_step_amount: float = None
) -> None:
    """Render calculation breakdown chart for an unsaved transfer step.
    
    Displays line-item details (qty, rate, amount) in a table with totals.
    Different columns shown for Step 1 (no rate increase) vs Step 2+ (with rate increase).
    
    Args:
        step_index: Current step number (0-indexed)
        line_items: List of dicts with 'weight', 'original_rate', 'original_claim' keys
        step_dict: Current step's data dict
        original_total_amount: Step 0's total amount (reference)
        prev_step_amount: Previous step's total amount (for Step 2+ rate calculations)
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
    
    # Will be implemented in next steps
    pass
```

- [ ] **Step 2: Implement Step 1 calculation logic (no rate increase)**

Replace the `pass` statement with Step 1 logic:

```python
    # Get previous step's rate for Step 2+ calculations
    if step_index == 0:
        # Step 1: Use original rates, no % increase
        data = []
        total_qty = 0.0
        total_amount = 0.0
        
        for idx, line_item in enumerate(line_items, start=1):
            qty = float(line_item.get("weight", 0) or 0)
            rate = float(line_item.get("original_rate", 0) or 0)
            amount = round(qty * rate, 2)
            
            data.append({
                "Line Item": idx,
                "Qty (KG)": round(qty, 2),
                "Rate (per quintal)": round(rate, 2),
                "Amount": amount,
            })
            
            total_qty += qty
            total_amount += amount
        
        # Add totals row
        data.append({
            "Line Item": "TOTAL",
            "Qty (KG)": round(total_qty, 2),
            "Rate (per quintal)": "-",
            "Amount": round(total_amount, 2),
        })
        
        # Display chart
        st.markdown("**Calculation Breakdown**")
        df = pd.DataFrame(data)
        st.dataframe(df, use_container_width=True, hide_index=True)
```

- [ ] **Step 3: Implement Step 2+ calculation logic (with rate increase)**

Add an `else` block after the Step 1 logic:

```python
    else:
        # Step 2+: Apply % increase to previous step's rate
        pct_increase = float(step_dict.get("pct_rate_increase", 0) or 0)
        
        # Get previous rate per kg from previous step's total amount
        if prev_step_amount and len(line_items) > 0:
            # Calculate previous rate per kg from previous total amount
            total_prev_qty = sum(float(li.get("weight", 0) or 0) for li in line_items)
            if total_prev_qty > 0:
                prev_rate_per_kg = prev_step_amount / total_prev_qty
            else:
                prev_rate_per_kg = 0
        else:
            prev_rate_per_kg = 0
        
        data = []
        total_qty = 0.0
        total_amount = 0.0
        
        for idx, line_item in enumerate(line_items, start=1):
            qty = float(line_item.get("weight", 0) or 0)
            
            # Calculate new rate with % increase applied
            new_rate = round(prev_rate_per_kg * (1.0 + pct_increase / 100.0), 2)
            amount = round(qty * new_rate, 2)
            
            data.append({
                "Line Item": idx,
                "Qty (KG)": round(qty, 2),
                "% Increase": f"{pct_increase:.2f}%",
                "New Rate (per quintal)": new_rate,
                "Amount": amount,
            })
            
            total_qty += qty
            total_amount += amount
        
        # Add totals row
        data.append({
            "Line Item": "TOTAL",
            "Qty (KG)": round(total_qty, 2),
            "% Increase": "-",
            "New Rate (per quintal)": "-",
            "Amount": round(total_amount, 2),
        })
        
        # Display chart
        st.markdown("**Calculation Breakdown**")
        df = pd.DataFrame(data)
        st.dataframe(df, use_container_width=True, hide_index=True)
```

- [ ] **Step 4: Test the function manually**

Navigate to the Jute MR page in the Streamlit app and:
- Select an MR with line items
- Edit a transfer step
- Verify the chart appears below the step cards (even though it won't be called yet)

Test output: No errors, function is syntactically correct

---

## Task 2: Integrate Calculation Chart into Transfer Editor

**Files:**
- Modify: `src/jutetransfer/jute_mr_editor.py` (lines ~309-312)

- [ ] **Step 1: Locate the "Add Step" button section**

Find this code block around line 309-312:

```python
    # Add Step button
    if total_steps < MAX_TRANSFERS:
        with cols[-1]:
            st.markdown("&nbsp;")
            if st.button("+ Add Step", key=f"add_{filter_key}_{mr_id}",
                         use_container_width=True):
                steps.append(_empty_transfer_step())
                changed = True
```

- [ ] **Step 2: Add chart rendering after the step card loop**

Insert this code BEFORE the "Add Step button" section (right after the step card loop closes):

```python
    # Render calculation chart for the currently-being-edited step
    # Find the first unsaved step with a company selected
    for i, step in enumerate(steps):
        is_saved = i < num_saved
        if not is_saved and step.get("company"):
            # This is the active step being edited
            prev_step_amt = None
            if i > 0:
                prev_step_amt = float(steps[i-1].get("total_amount", 0))
            
            _render_step_calculation_chart(
                step_index=i,
                line_items=line_items,
                step_dict=step,
                original_total_amount=float(row.get("Total Amount") or 0),
                prev_step_amount=prev_step_amt,
            )
            break  # Only show chart for the first unsaved step with company
    
    st.markdown("---")  # Visual separator before action buttons
```

- [ ] **Step 3: Test the integration**

Run the Streamlit app and:
- Select an MR with line items
- Click to edit it
- Edit Step 1 (no rate change): Verify chart shows with columns `Line Item | Qty (KG) | Rate (per quintal) | Amount` and totals
- Add Step 2 and enter a % increase: Verify chart updates to show `Line Item | Qty (KG) | % Increase | New Rate (per quintal) | Amount`
- Verify chart appears below step cards but before action buttons

Expected result: Chart displays correctly for both Step 1 and Step 2+ with appropriate columns

- [ ] **Step 4: Verify number formatting**

Check the displayed values:
- All amounts are rounded to 2 decimals (e.g., 2750.00)
- All rates are rounded to 2 decimals (e.g., 275.00)
- Quantities show with 2 decimal places (e.g., 1000.00)
- % Increase shows with 2 decimals and % sign (e.g., 10.00%)

- [ ] **Step 5: Test edge cases**

Test with:
- Single line item: Totals should match the single item
- Multiple line items with different weights: Totals should sum correctly
- Empty line items: Should show "No line items to display"
- Step 2 with 0% increase: Should show amounts equal to previous step

---

## Task 3: Commit Implementation

**Files:**
- Modified: `src/jutetransfer/jute_mr_editor.py`

- [ ] **Step 1: Stage changes**

```bash
git add src/jutetransfer/jute_mr_editor.py
```

- [ ] **Step 2: Commit**

```bash
git commit -m "feat: add calculation breakdown chart for transfer steps

- Add _render_step_calculation_chart() function to display line-item calculations
- Show different columns for Step 1 (qty, rate, amount) vs Step 2+ (qty, % increase, new rate, amount)
- Display totals row with sum of quantities and amounts
- All values rounded to 2 decimal places
- Chart appears below step cards, only for unsaved/editable steps"
```

- [ ] **Step 3: Verify commit**

```bash
git log --oneline -1
```

Expected: Shows the commit message for the calculation chart feature

---

## Self-Review Checklist

✅ **Spec Coverage:**
- Display calculation chart for unsaved steps only → Task 2, Step 2
- Step 1 shows Qty, Rate, Amount → Task 1, Step 2
- Step 2+ shows Qty, % Increase, New Rate, Amount → Task 1, Step 3
- Show totals for Qty and Amount → Task 1, Steps 2-3
- Round amounts and rates to 2 decimals → Task 1, Steps 2-3
- Place below step cards → Task 2, Step 2
- New Rate calculation with rounding → Task 1, Step 3 (line: `new_rate = round(prev_rate_per_kg * (1.0 + pct_increase / 100.0), 2)`)

✅ **Placeholder Scan:**
- No "TBD" or "TODO" statements
- All code is complete and functional
- All test cases are concrete and verifiable
- No vague references like "similar to Task N"

✅ **Type Consistency:**
- Function signature defined in Task 1, Step 1
- Called with correct arguments in Task 2, Step 2
- Parameter names match: `step_index`, `line_items`, `step_dict`, `original_total_amount`, `prev_step_amount`

✅ **No Placeholders:**
- Every step contains actual code or exact commands
- All calculations are explicit (not "add rounding")
- Test expectations are concrete and verifiable
