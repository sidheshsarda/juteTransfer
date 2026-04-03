# Calculation Chart for Transfer Steps — Design Spec

**Date:** 2026-04-02  
**Feature:** Add a calculation breakdown chart below transfer step cards  
**Purpose:** Show users how line-item calculations are computed for the current transfer step being edited

---

## Overview

When a user is actively editing a transfer step in the Jute MR application, display a calculation chart that breaks down the per-line-item details (quantity, rate, amount) and shows the total quantities and amounts. This provides transparency on how the system calculates amounts based on % rate increases.

---

## Business Context

**Problem:** Users need clarity on how calculations work when entering rate increases for transfer steps.

**Solution:** Show a data-driven table with line-item details and totals, making the calculation transparent.

---

## Feature Scope

### What's Included
- Display calculation chart **only for unsaved/editable steps** being actively worked on
- Show **line-item breakdown** with appropriate columns based on step number
- Display **totals row** with sum of quantities and amounts
- Round all amounts and rates to **2 decimal places**
- Position chart **directly below the step cards** (before action buttons)

### What's NOT Included
- Charts for saved/finalized steps (read-only display only)
- Historical comparison across steps
- Charts for Step 0 (source/gate entry) — source card already shows this

---

## Data Display

### Step 1 (No Rate Increase)
**Columns:** `Line Item # | Qty (KG) | Rate (per quintal) | Amount`

**Calculation:**
- Qty: From line_items[i]["weight"]
- Rate: From line_items[i]["original_rate"] (per quintal)
- Amount: `Qty × Rate`, rounded to 2 decimals

**Example:**
```
Line Item | Qty (KG) | Rate (per quintal) | Amount
    1     |  1000    |      250.00        | 2500.00
    2     |  500     |      250.00        | 1250.00
    ──────────────────────────────────────────────
    TOTAL |  1500    |        -            | 3750.00
```

---

### Step 2+ (With Rate Increase)
**Columns:** `Line Item # | Qty (KG) | % Increase | New Rate (per quintal) | Amount`

**Calculation:**
- Qty: From line_items[i]["weight"]
- % Increase: From step["pct_rate_increase"]
- New Rate: `round(Previous Rate × (1 + % Increase / 100), 2)`
  - Calculate new rate, then round to 2 decimals
  - Previous Rate is from the immediately preceding step (also rounded to 2 decimals)
- Amount: `round(Qty × round(New Rate, 2), 2)`
  - Use the rounded New Rate for amount calculation
  - Round final amount to 2 decimals

**Example (with 10% increase, previous rate was 250.00):**
```
Line Item | Qty (KG) | % Increase | New Rate (per quintal) | Amount
    1     |  1000    |   10.00%   |       275.00           | 2750.00
         (250.00 × 1.10 = 275.00, 1000 × 275.00 = 2750.00)
    2     |  500     |   10.00%   |       275.00           | 1375.00
         (250.00 × 1.10 = 275.00, 500 × 275.00 = 1375.00)
    ─────────────────────────────────────────────────────────────
    TOTAL |  1500    |    -       |         -              | 4125.00
```

---

## Implementation Details

### Function Signature
```python
def _render_step_calculation_chart(
    step_index: int,
    line_items: list,
    step_dict: dict,
    original_total_amount: float,
    prev_step_amount: float = None
) -> None:
```

### Parameters
- `step_index`: Current step number (0-indexed)
- `line_items`: List of dicts with keys: `weight`, `original_rate`, `original_claim`
- `step_dict`: Current step's data dict (contains `pct_rate_increase`, `total_amount`, etc.)
- `original_total_amount`: Step 0's total amount (for reference)
- `prev_step_amount`: Previous step's total amount (for rate calculations in Step 2+)

### Logic Flow
1. **Guard:** Only render if step is unsaved (check `step_dict.get("saved_mr_id")` is None/falsy)
2. **Build data:**
   - For each line item: calculate Qty, Rate, % Increase, New Rate, Amount
   - Round amounts and rates to 2 decimals using `round(value, 2)`
3. **Build totals row:**
   - Sum of Qty
   - Sum of Amount
   - Other columns: dash or empty
4. **Display:** Use `st.dataframe()` for clean table rendering
5. **Positioning:** Call this function in `_render_transfer_editor()` after the step card loop, inside the action buttons section

---

## Rate Conversion (Downstream)
**Note:** Rates are stored and displayed in the application as **per quintal** (100 kg units).

When saving for:
- **MR (database):** Keep rate per quintal (no conversion)
- **Invoice (downstream):** Divide rate by 100 to convert to per kg

This conversion happens during save/invoice generation, not in the chart.

---

## Placement in Code

**File:** `src/jutetransfer/jute_mr_editor.py`

**Location:** After the step card rendering loop (around line 309-310 in current code), before the "Add Step" button

```python
# Render calculation chart for unsaved/editable step being worked on
if not is_saved and step.get("company"):
    _render_step_calculation_chart(
        step_index=i,
        line_items=line_items,
        step_dict=step,
        original_total_amount=float(row.get("Total Amount") or 0),
        prev_step_amount=float(steps[i-1].get("total_amount", 0)) if i > 0 else None
    )
```

---

## Error Handling

- If line_items is empty or None: Render empty message "No line items to display"
- If weights are missing: Treat as 0
- If rates are missing: Treat as 0
- Division by zero: Guard with conditional checks (e.g., `if prev_rate > 0`)

---

## Testing Checklist

- [ ] Step 1 chart renders correctly with Qty, Rate, Amount
- [ ] Step 2+ chart shows % Increase and recalculated rates
- [ ] Totals row sums quantities and amounts correctly
- [ ] All values round to 2 decimals
- [ ] Chart only shows for unsaved steps
- [ ] Chart displays below step cards, above action buttons
- [ ] Empty line_items handled gracefully
- [ ] Multiple line items aggregate correctly
- [ ] Rate cascading through multiple steps shows correct values

---

## Success Criteria

✅ Users can see per-line-item calculations for the step they're editing  
✅ Totals are clearly visible and match system calculations  
✅ Chart only appears when actively editing (not for saved steps)  
✅ All numbers are rounded consistently to 2 decimals  
✅ Chart is positioned logically in the UI flow
