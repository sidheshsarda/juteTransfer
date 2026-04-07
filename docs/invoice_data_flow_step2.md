# Step 2 Invoice Creation - Data Flow Analysis

## Overview
When **Step 2 is saved**, an invoice is created from **Company A (Step 1)** to **Party selected in Step 2**.

---

## 1. DATA COLLECTED IN UI (jute_mr_editor.py)

### From User Input:
```python
step_to_save = {
    "company": str,           # e.g., "PREFIX-BranchName" (Step 2 company selection)
    "mr_date": date,         # MR date selected by user
    "pct_rate_increase": float,  # % Rate Increase (entered & submitted)
    "warehouse_id": int,     # Warehouse selected for Step 2
    # ... other fields
}
```

### From Calculations (before save):
```python
# Line 550-556: _calculate_step_total_amount is called
calculated_total = _calculate_step_total_amount(
    step_index=1,            # For Step 2 (0-indexed, so index=1)
    line_items=line_items,   # Original MR line items
    step_dict=step_to_save,  # Current step values
    steps=steps,             # All steps for cumulative calculation
    original_total_amount=orig_total  # Original MR total
)

# Line 559-560: Amounts are set
step_to_save["total_amount"] = round(calculated_total, 0)
step_to_save["net_amount"] = round(calculated_total - claim_amount, 0)

# Line 564: Cumulative multiplier derived
if orig_total > 0:
    cumulative_multiplier = calculated_total / orig_total
```

---

## 2. EXACT DATA SENT TO save_transfer_step()

### At Line 583-607 (jute_mr_editor.py):

```python
save_transfer_step(
    # Input data
    source_mr_id=mr_id,                    # Original MR ID (Step 0)
    
    # TransferStep object created here
    step=TransferStep(
        co_id=co_id,                       # Step 2 company's co_id
        branch_id=branch_id,               # Step 2 company's branch_id
        
        # Dates & Rates
        mr_date=step_to_save.get("mr_date"),           # User's date selection
        mr_rate=float(step_to_save.get("weighted_avg_rate", 0)),  # 0 (not used)
        pct_rate_increase=float(step_to_save.get("pct_rate_increase")),  # User's %
        
        # CRITICAL: Amounts for invoice
        total_amount=float(step_to_save.get("total_amount")),    # Calculated total
        claim_amount=float(step_to_save.get("claim_amount")),    # Original claim
        net_amount=float(step_to_save.get("net_amount")),        # Total - Claim
        
        # MR tracking
        mr_no=0,                           # Will be assigned inside transaction
        roundoff=float(step_to_save.get("roundoff", 0)),  # Calculated roundoff
        warehouse_id=step_to_save.get("warehouse_id"),    # Step 2 warehouse
    ),
    
    # Context
    prev_co_id=prev_co_id,                 # Step 1 company co_id (SELLER)
    prev_branch_id=prev_branch_id,         # Step 1 company branch_id (SELLER)
    source_co_id=selected_company_id,      # Original company co_id
    source_branch_id=selected_branch_id,   # Original branch_id
    root_mr_id=mr_id,                      # Original MR ID
    
    # User tracking
    updated_by=1,
    
    # KEY: Rate multiplier for invoice calculation
    rate_multiplier=cumulative_multiplier,  # (1.0 + pct/100) for Step 2
    
    # Flags
    is_first_step=False,                   # Step 2 is NOT first
    is_final=False,                        # Not final unless returns to source
)
```

---

## 3. INSIDE save_transfer_step() → _create_sales_invoice()

### At Line 696-803 (transfer.py):

The **_create_sales_invoice()** function is called with:

```python
def _create_sales_invoice(
    conn,
    seller_step=prev_step_for_invoice,  # Created from prev_co_id + prev_branch_id
    buyer_party_id=buyer_party_id,      # Party from Step 2 company
    buyer_party_branch_id=buyer_party_branch_id,
    mr_id=prev_mr_id,                   # Previous MR ID for linking
    source_mr=source_mr,                # Full source MR with line_items
    updated_by=1,
    rate_multiplier=cumulative_multiplier,  # Same multiplier
)
```

---

## 4. DATA USED FOR INVOICE CREATION

### 4.1 Line-by-Line Calculation (Lines 709-719):

For each line item from **source_mr["line_items"]**:

```python
for li in source_mr.get("line_items", []):
    # WEIGHTS
    accepted_weight = round(float(li.get("accepted_weight") or 0), 0)
    
    # RATES (per-item)
    original_rate = float(li.get("rate") or 0)
    new_rate = original_rate * rate_multiplier  # Applied HERE
    
    # AMOUNT CALCULATION (in Decimal for precision)
    amount = (accepted_weight * new_rate / 100).quantize(2 decimals)
    
    # Result added to line_amounts[]
```

**Key Variables Used from source_mr.line_items:**
- `accepted_weight` → rounded to integer
- `rate` → original per-item rate
- Other fields: `actual_item_id`, `unit_conversion`, `challan_item_id`, etc.

### 4.2 Round-off Calculation (Lines 721-724):

```python
sum_amounts = sum(line_amounts)  # Sum of all line amounts
rounded_total = sum_amounts.quantize(2 decimals)
round_off = sum_amounts - rounded_total
```

---

## 5. DATA WRITTEN TO DATABASE

### 5.1 sales_invoice (header) - Line 726-747

```sql
INSERT INTO sales_invoice (
    invoice_no,          -- Generated: max(invoice_no) + 1 for branch/FY
    invoice_date,        -- seller_step.mr_date (Step 2's mr_date)
    invoice_type,        -- RAW_JUTE_INVOICE_TYPE constant
    invoice_amount,      -- seller_step.total_amount (Step 2's total_amount)
    party_id,            -- buyer_party_id (Step 2's party)
    billing_to_id,       -- buyer_party_branch_id (Step 2's party branch)
    shipping_to_id,      -- buyer_party_branch_id (Step 2's party branch)
    branch_id,           -- seller_step.branch_id (Step 1's branch)
    active,              -- 1 (hardcoded)
    status_id,           -- 3 (Approved, hardcoded)
    round_off,           -- Calculated roundoff
    updated_by,          -- 1
    updated_date_time    -- NOW()
)
```

### 5.2 sales_invoice_dtl (line items) - Line 765-783

**For each source line item:**

```sql
INSERT INTO sales_invoice_dtl (
    invoice_id,          -- Generated invoice_id
    item_id,             -- _ensure_item(actual_item_id, seller_co_id)
    hsn_code,            -- NULL
    quantity,            -- accepted_weight (rounded integer)
    sales_weight,        -- accepted_weight (rounded integer)
    uom_id,              -- 163 (hardcoded for raw jute)
    rate,                -- new_rate (original_rate * rate_multiplier)
    amount_without_tax,  -- calculated amount
    total_amount,        -- same as amount_without_tax
    remarks              -- "Raw Jute - {qty} {unit_conversion}"
)
```

### 5.3 sales_invoice_jute (jute-specific) - Line 786-801

```sql
INSERT INTO sales_invoice_jute (
    invoice_id,          -- Generated invoice_id
    mr_no,               -- seller_step.mr_no (Step 1's MR number)
    mr_id,               -- prev_mr_id (Step 1's MR ID)
    mukam_id,            -- source_mr.mukam_id (Original)
    claim_amount,        -- seller_step.claim_amount (Step 2's claim)
    unit_conversion      -- source_mr.unit_conversion (Original)
)
```

---

## 6. DATA TRANSFORMATION SUMMARY

### For Step 2 Invoice (Seller = Company A, Buyer = Step 2 Party):

| Data Element | Source | Value Sent to Invoice | Calculation |
|---|---|---|---|
| **Invoice Header** | | | |
| invoice_date | UI (mr_date) | Step 2's mr_date | User input |
| invoice_amount | UI (total_amount) | Step 2's total_amount | `orig_total * (1 + pct/100)` |
| branch_id (seller) | Step 1 | Company A's branch_id | Context parameter |
| party_id (buyer) | Step 2 | Step 2's selected party | Ensured in DB |
| **Line Items** | | | |
| quantity | source_mr | accepted_weight (rounded) | `round(weight, 0)` |
| rate per item | source_mr | new_rate | `original_rate * rate_multiplier` |
| amount | Calculated | `qty * new_rate / 100` | Decimal precision |
| **Jute Info** | | | |
| mr_id (linking) | Step 1 MR | Step 1's jute_mr_id | From chain |
| mr_no (linking) | Step 1 MR | Step 1's branch_mr_no | From chain |
| claim_amount | Step 2 | Step 2's claim_amount | Same as original |
| unit_conversion | source_mr | Original unit_conversion | From source |

---

## 7. CRITICAL QUESTIONS FOR DEBUGGING

1. **Is the rate_multiplier calculated correctly?**
   - Should be: `(1.0 + pct_rate_increase / 100.0)`
   - Check: `cumulative_multiplier` value before save

2. **Are the line_items from source_mr populated?**
   - Check: `source_mr.line_items` has all required fields
   - Required: `accepted_weight`, `rate`, `actual_item_id`

3. **Is the invoice_amount matching the step total_amount?**
   - `invoice_amount` should = `step_to_save["total_amount"]`
   - Check: Is calculated_total being rounded correctly?

4. **Is the party_id valid?**
   - `buyer_party_id` must exist in party_mst for seller's co_id
   - Check: _ensure_company_as_party() returned valid party

5. **Are the per-item rates being applied to line items?**
   - Each line should have: `rate = original_rate * rate_multiplier`
   - Check: Decimal precision in calculation

6. **Is the roundoff calculation correct?**
   - `round_off = sum_amounts - rounded_total`
   - Should be small value (< 1.00)

7. **Are the MR link fields correct?**
   - `mr_id` in sales_invoice_jute should point to Step 1 MR
   - `mr_no` should match Step 1's branch_mr_no

---

## 8. EXECUTION FLOW (Step 2 Save)

```
User selects Step 2 company → enters MR date → enters % Rate Increase (ENTER)
    ↓
jute_mr_editor.py calculates: total_amount, net_amount, roundoff
    ↓
save_transfer_step() called with:
  - rate_multiplier = (1.0 + pct/100)
  - step.total_amount = calculated total
  - step.claim_amount = original claim
    ↓
Inside transaction:
  1. _ensure_company_as_party() → get buyer_party_id
  2. _create_sales_invoice(
       seller_co = Step 1
       buyer_party = Step 2 party
       source_mr = original MR with line_items
       rate_multiplier = (1.0 + pct/100)
     )
       ↓
       For each line:
         new_rate = original_rate * rate_multiplier
         amount = qty * new_rate / 100
       ↓
       INSERT sales_invoice, sales_invoice_dtl, sales_invoice_jute
  3. _create_mr() → create new MR for Step 2
  4. Commit transaction
```

---

## 9. WHAT COULD BE WRONG WITH INVOICE

### Possible Issues:

| Issue | Symptom | Check |
|---|---|---|
| Wrong rate_multiplier | Invoice amounts don't match expected % increase | Verify pct_rate_increase value & calculation |
| Wrong line items | Missing or duplicate line items in invoice | Check source_mr.line_items population |
| Wrong party | Invoice links to wrong buyer | Verify buyer_party_id & ensure_company_as_party() |
| Wrong MR linking | Invoice doesn't link to correct previous MR | Check prev_mr_id assignment |
| Rounding errors | Total amount off by small amounts | Check Decimal precision in calculations |
| Missing data | Null values in critical fields | Check source_mr fields & step_to_save data |
| Status/Type | Invoice has wrong status or type | Check hardcoded status_id=3, invoice_type constant |

