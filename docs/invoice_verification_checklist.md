# Invoice Verification Checklist - Step 2 Debug Guide

## Quick Reference: What Data Should Be in Invoice

### For an example: Original MR total = 10,000, Step 2 with +5% increase

| Field | Expected Value | How to Verify |
|---|---|---|
| **Invoice Header** | | |
| `invoice_no` | Sequential per branch/FY | Should be unique, incrementing |
| `invoice_date` | Step 2's mr_date | What date did you enter? |
| `invoice_amount` | 10,500 (10,000 × 1.05) | Total after % increase |
| `branch_id` | Step 1's branch ID | Company A's branch |
| `party_id` | Step 2's selected party | Company/party from Step 2 |
| `status_id` | 3 | Hardcoded (Approved) |
| `round_off` | Small value (< 1.00) | Decimal precision artifact |
| **Line Items (per line)** | | |
| `quantity` | Accepted weight | Original weight rounded |
| `rate` | Original × 1.05 | Original rate × (1 + 5/100) |
| `amount_without_tax` | qty × new_rate / 100 | Check calculation |
| `total_amount` | Same as amount | No tax applied |
| `uom_id` | 163 | Hardcoded for raw jute |
| **Jute Info** | | |
| `mr_id` | Step 1's jute_mr_id | Points to previous MR |
| `mr_no` | Step 1's branch_mr_no | Previous MR's number |
| `claim_amount` | Original claim | Should cascade unchanged |
| `unit_conversion` | Original unit | From source MR |

---

## Debug SQL Queries

### 1. Check Invoice Header

```sql
SELECT 
    invoice_id,
    invoice_no,
    invoice_date,
    invoice_amount,
    party_id,
    branch_id,
    status_id,
    round_off,
    updated_by,
    updated_date_time
FROM sales_invoice
WHERE invoice_id = <YOUR_INVOICE_ID>
```

**What to look for:**
- Is `invoice_amount` correct?
- Is `party_id` the Step 2 party?
- Is `branch_id` the Step 1 branch?
- Is `status_id = 3`?

---

### 2. Check Invoice Line Items

```sql
SELECT 
    invoice_line_item_id,
    invoice_id,
    item_id,
    quantity,
    sales_weight,
    rate,
    amount_without_tax,
    total_amount,
    remarks
FROM sales_invoice_dtl
WHERE invoice_id = <YOUR_INVOICE_ID>
```

**What to look for:**
- Does line count match source MR lines?
- Is `quantity = accepted_weight` from source?
- Is `rate` the new rate (original × rate_multiplier)?
- Is `amount = qty * rate / 100`?
- Do all remarks follow "Raw Jute - {qty} {unit_conversion}"?

---

### 3. Check Jute-Specific Invoice Data

```sql
SELECT 
    sales_invoice_jute_id,
    invoice_id,
    mr_no,
    mr_id,
    mukam_id,
    claim_amount,
    unit_conversion
FROM sales_invoice_jute
WHERE invoice_id = <YOUR_INVOICE_ID>
```

**What to look for:**
- Is `mr_id` pointing to the Step 1 MR?
- Is `mr_no` the Step 1's branch_mr_no?
- Is `claim_amount` the original claim (unchanged)?
- Is `unit_conversion` from source?

---

### 4. Cross-Check with Source MR

```sql
-- Compare original MR line items with invoice line items
SELECT 
    'Source' AS source,
    jmli.accepted_weight AS qty,
    jmli.rate AS original_rate,
    (jmli.accepted_weight * jmli.rate / 100) AS original_amount
FROM jute_mr_li jmli
WHERE jmli.jute_mr_id = <ORIGINAL_MR_ID>

UNION ALL

SELECT 
    'Invoice' AS source,
    sid.quantity AS qty,
    sid.rate AS invoice_rate,
    sid.amount_without_tax AS invoice_amount
FROM sales_invoice_dtl sid
WHERE sid.invoice_id = <YOUR_INVOICE_ID>
ORDER BY source
```

**What to look for:**
- Line count should match
- Quantities should be same (accepted_weight)
- Invoice rate should be: original_rate × rate_multiplier
- Invoice amount should be: qty × new_rate / 100

---

### 5. Verify Rate Multiplier Application

```sql
-- Calculate what the multiplier should have been
SELECT 
    (SELECT total_amount FROM jute_mr WHERE jute_mr_id = <ORIGINAL_MR_ID>) AS original_total,
    (SELECT invoice_amount FROM sales_invoice WHERE invoice_id = <YOUR_INVOICE_ID>) AS invoice_amount,
    ROUND(
        (SELECT invoice_amount FROM sales_invoice WHERE invoice_id = <YOUR_INVOICE_ID>) /
        (SELECT total_amount FROM jute_mr WHERE jute_mr_id = <ORIGINAL_MR_ID>),
        4
    ) AS calculated_multiplier,
    CONCAT(ROUND(
        ((SELECT invoice_amount FROM sales_invoice WHERE invoice_id = <YOUR_INVOICE_ID>) /
         (SELECT total_amount FROM jute_mr WHERE jute_mr_id = <ORIGINAL_MR_ID>) - 1) * 100,
        2
    ), '%') AS equivalent_percentage
```

**Example calculation:**
- Original total: 10,000
- Invoice amount: 10,500
- Multiplier: 10,500 / 10,000 = 1.05
- Percentage: (1.05 - 1) × 100 = 5%

---

## Step-by-Step Verification

### Before Saving Step 2:

1. ✅ In UI, verify:
   - [ ] Company is selected for Step 2
   - [ ] MR Date is entered
   - [ ] Warehouse is selected
   - [ ] % Rate Increase is entered and ENTER key pressed
   - [ ] "Save Step 2" button is enabled (not grayed out)

2. ✅ Check calculation chart display:
   - [ ] Shows correct original total
   - [ ] Shows correct % increase you entered
   - [ ] Shows correct new total amount
   - [ ] Line-by-line amounts add up correctly

### After Saving Step 2:

1. ✅ Database verification:
   - [ ] New MR created for Step 2 (jute_mr)
   - [ ] New MR line items created (jute_mr_li)
   - [ ] Invoice created (sales_invoice)
   - [ ] Invoice line items created (sales_invoice_dtl)
   - [ ] Jute invoice metadata created (sales_invoice_jute)

2. ✅ Data correctness:
   - [ ] Invoice amount = Step 2 total shown in UI
   - [ ] Invoice party = Step 2's selected party
   - [ ] Invoice branch = Step 1's branch
   - [ ] Each line item rate = original rate × (1 + pct/100)
   - [ ] Each line item amount = qty × new_rate / 100

---

## Common Issues & Solutions

### Issue: Invoice amount is 0 or missing

**Check:**
```sql
SELECT total_amount, net_amount FROM jute_mr WHERE jute_mr_id = <STEP2_MR_ID>
```

**Likely cause:** step.total_amount not set before save
**Solution:** Verify _calculate_step_total_amount() is being called

---

### Issue: Invoice line items have wrong quantity

**Check:**
```sql
SELECT accepted_weight FROM jute_mr_li WHERE jute_mr_id = <ORIGINAL_MR_ID>
SELECT quantity FROM sales_invoice_dtl WHERE invoice_id = <YOUR_INVOICE_ID>
```

**Likely cause:** Line items not being copied correctly
**Solution:** Check source_mr.line_items population

---

### Issue: Invoice rates don't reflect % increase

**Check:**
```sql
SELECT rate FROM jute_mr_li WHERE jute_mr_id = <ORIGINAL_MR_ID>
SELECT rate FROM sales_invoice_dtl WHERE invoice_id = <YOUR_INVOICE_ID>
```

**Ratio should be:** invoice_rate / original_rate = rate_multiplier = (1 + pct/100)

**Likely cause:** rate_multiplier not calculated or applied correctly
**Solution:** Verify cumulative_multiplier before save

---

### Issue: Invoice linked to wrong MR

**Check:**
```sql
SELECT mr_id, mr_no FROM sales_invoice_jute WHERE invoice_id = <YOUR_INVOICE_ID>
```

**Expected:** Should link to Step 1's MR ID
**Likely cause:** prev_mr_id not found correctly
**Solution:** Check MR chain reconstruction logic

---

### Issue: Party doesn't match Step 2 selection

**Check:**
```sql
SELECT party_id FROM sales_invoice WHERE invoice_id = <YOUR_INVOICE_ID>
SELECT supp_name FROM party_mst WHERE party_id = <PARTY_ID>
```

**Expected:** Should match Step 2's selected company/party
**Likely cause:** _ensure_company_as_party() created new party or used wrong one
**Solution:** Check if party needs to be manually verified in DB

---

## Log Messages to Look For

After saving, check application logs for:

1. **Success:**
   ```
   "Transfer step saved for MR {source_mr_id}: mr_id={mr_id}, invoice_id={invoice_id}"
   ```

2. **Rate multiplier calculation:**
   ```
   Check what cumulative_multiplier value was used
   Should see: (1.0 + pct_rate_increase / 100)
   ```

3. **Invoice creation:**
   ```
   Should log: invoice_no, party_id, invoice_amount
   ```

---

## Test Case Template

Fill in your values and verify:

```
Original MR ID: ___________
Original Total: ___________
Original Claim: ___________

Step 2 Company: ___________
Step 2 Party: ___________
Step 2 % Increase: ___________%

Expected Calculations:
- New Rate Multiplier: 1 + (____% / 100) = _______
- New Total: _________ × _______ = _________
- New Claim: _________ (unchanged)
- New Net: _________ - _________ = _________

Actual in Database:
- Invoice Amount: _________
- Invoice Party ID: _________
- First Line Rate: _________
- First Line Amount: _________
- Claim in Jute Info: _________
```

