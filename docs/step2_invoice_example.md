# Step 2 Invoice - Concrete Example

## Scenario Setup

```
Original MR (Step 0):
  - MR ID: 1001
  - Total Amount: 10,000
  - Claim Amount: 500
  - Line Items: 2 items
    - Item 1: Weight=500 kg, Rate=50 (per 100kg)
    - Item 2: Weight=400 kg, Rate=45 (per 100kg)

Step 1 Company (Company A):
  - Company ID: 1
  - Branch ID: 100
  - Branch Name: "Main"
  - co_prefix: "CA"

Step 2 Company (Company B):
  - Company ID: 2
  - Branch ID: 200
  - Branch Name: "Branch2"
  - co_prefix: "CB"
  - Party Name (as party in Co.A): "CompanyB-Supplier"
  - Party ID: 5001

Step 2 User Input:
  - % Rate Increase: +5%
  - MR Date: 2026-04-02
  - Warehouse: 10
```

---

## Step 1: Calculate in UI (jute_mr_editor.py)

### Line Items Array (from source_mr.line_items):

```python
line_items = [
    {
        "weight": 500,
        "original_rate": 50,
        "accepted_weight": 500,
        "actual_item_id": 101
    },
    {
        "weight": 400,
        "original_rate": 45,
        "accepted_weight": 400,
        "actual_item_id": 102
    }
]
```

### Step 2 Data (step_to_save):

```python
step_to_save = {
    "company": "CB-Branch2",
    "mr_date": date(2026, 4, 2),
    "pct_rate_increase": 5.0,
    "warehouse_id": 10,
    "claim_amount": 500  # From original, unchanged
}
```

### Calculate Total Amount (Line 550-556):

```python
# _calculate_step_total_amount() with step_index=1
cumulative_multiplier = 1.0 * (1.0 + 5.0 / 100.0) = 1.05

# Per item calculation:
# Item 1: 500 * 50 * 1.05 / 100 = 262.5
# Item 2: 400 * 45 * 1.05 / 100 = 189
# Total: 262.5 + 189 = 451.5 ← WAIT, this seems low!
```

Actually, let me recalculate using the CORRECT formula:

```python
# _calculate_step_total_amount() line 172-176
total = 0.0
for line_item in line_items:
    qty = 500  # Item 1
    original_rate = 50
    effective_rate = 50 * 1.05 = 52.5
    total += 500 * 52.5 / 100 = 262.5

for line_item in line_items:
    qty = 400  # Item 2
    original_rate = 45
    effective_rate = 45 * 1.05 = 47.25
    total += 400 * 47.25 / 100 = 189
    
# Total = 262.5 + 189 = 451.5

# Hmm, that's not right. The original total should be preserved...
# Let me check the original calculation
```

Wait, I need to look at the actual total amount calculation. Let me verify:

```
Original amounts:
  Item 1: 500 * 50 / 100 = 250
  Item 2: 400 * 45 / 100 = 180
  Total: 430

Wait, the example said total is 10,000, but that's only 430...
```

Let me use realistic numbers:

---

## Realistic Step 2 Invoice Example

### Original MR (Step 0):

```
MR ID: 1001
Total Amount: 10,000
Claim Amount: 1,000
Line Items:
  - Item 1: Weight=1000 kg, Rate=500 (per 100kg)
    Amount: 1000 * 500 / 100 = 5,000
  - Item 2: Weight=1000 kg, Rate=500 (per 100kg)
    Amount: 1000 * 500 / 100 = 5,000
  - Total: 10,000 ✓
```

### Step 2 User Input:

```
Company: CB-Branch2 (Step 2 company)
% Rate Increase: +10%
MR Date: 2026-04-02
Warehouse: 10
```

### Step 1: Calculate Multiplier

```python
pct_rate_increase = 10.0
rate_multiplier = 1.0 * (1.0 + 10.0 / 100.0) = 1.10
```

### Step 2: Calculate New Amounts

```python
# Per-item calculation in _calculate_step_total_amount()
# Item 1:
qty1 = 1000
original_rate1 = 500
new_rate1 = 500 * 1.10 = 550
amount1 = 1000 * 550 / 100 = 5,500

# Item 2:
qty2 = 1000
original_rate2 = 500
new_rate2 = 500 * 1.10 = 550
amount2 = 1000 * 550 / 100 = 5,500

# Total for Step 2:
step2_total = 5,500 + 5,500 = 11,000 ✓
```

### Step 3: Set step_to_save

```python
# Line 559-560
step_to_save["total_amount"] = round(11000, 0) = 11,000
step_to_save["net_amount"] = round(11000 - 1000, 0) = 10,000
step_to_save["roundoff"] = 0
step_to_save["claim_amount"] = 1,000
```

---

## Step 2: Build TransferStep Object (Line 585-597)

```python
step = TransferStep(
    co_id=2,                    # Company B
    branch_id=200,              # Company B's branch
    mr_date=date(2026, 4, 2),
    mr_rate=0.0,
    total_amount=11000.0,       # CRITICAL: Invoice amount
    claim_amount=1000.0,        # CRITICAL: Claim for invoice
    net_amount=10000.0,
    mr_no=0,
    pct_rate_increase=10.0,
    roundoff=0.0,
    warehouse_id=10
)
```

---

## Step 3: Call save_transfer_step() (Line 583-607)

```python
result = save_transfer_step(
    source_mr_id=1001,
    step=step,
    prev_co_id=1,                    # Company A (Step 1)
    prev_branch_id=100,              # Company A's branch
    source_co_id=1,
    source_branch_id=100,
    root_mr_id=1001,
    updated_by=1,
    rate_multiplier=1.10,            # CRITICAL: Applied to invoice
    is_first_step=False,
    is_final=False
)
```

---

## Step 4: Inside save_transfer_step() - Create Invoice

### Get source MR data:

```python
source_mr = {
    "jute_mr_id": 1001,
    "total_amount": 10000,
    "claim_amount": 1000,
    "mukam_id": 1,
    "jute_supplier_id": 5,
    "unit_conversion": "KG",
    "line_items": [
        {
            "jute_mr_li_id": 2001,
            "accepted_weight": 1000,
            "rate": 500,
            "actual_item_id": 101,
            "claim_rate": 50
        },
        {
            "jute_mr_li_id": 2002,
            "accepted_weight": 1000,
            "rate": 500,
            "actual_item_id": 102,
            "claim_rate": 50
        }
    ]
}
```

### Get buyer party info:

```python
buyer_party_id = 5001      # CompanyB as party in Company A
buyer_party_branch_id = None
```

### Create seller_step for invoice:

```python
seller_step = TransferStep(
    co_id=1,               # Company A (seller)
    branch_id=100,         # Company A's branch
    mr_date=date(2026, 4, 2),
    total_amount=11000,    # Step 2's calculated total
    claim_amount=1000,     # Step 2's claim
    mr_no=0                # Will be assigned
)
```

---

## Step 5: _create_sales_invoice() Execution (Line 696-803)

### Invoice Number Generation (Line 706):

```python
# Get max invoice_no for branch 100 in current FY
# Assuming this is first invoice: max = 0
invoice_no = 1
```

### Calculate Line Amounts (Line 709-719):

```python
line_amounts = []

# Line 1:
accepted_weight = round(1000, 0) = 1000
original_rate = 500
new_rate = 500 * 1.10 = 550
amount = Decimal('1000') * Decimal('550') / Decimal('100') = 5500.00
line_amounts.append(5500.00)

# Line 2:
accepted_weight = round(1000, 0) = 1000
original_rate = 500
new_rate = 500 * 1.10 = 550
amount = Decimal('1000') * Decimal('550') / Decimal('100') = 5500.00
line_amounts.append(5500.00)
```

### Calculate Round-off (Line 721-724):

```python
sum_amounts = 5500.00 + 5500.00 = 11000.00
rounded_total = Decimal('11000.00')
round_off = 11000.00 - 11000.00 = 0.00
```

### Insert sales_invoice (Line 726-747):

```sql
INSERT INTO sales_invoice (
    invoice_no,              1
    invoice_date,            2026-04-02
    invoice_type,            <RAW_JUTE_TYPE>
    invoice_amount,          11000.0
    party_id,                5001
    billing_to_id,           NULL
    shipping_to_id,          NULL
    branch_id,               100
    active,                  1
    status_id,               3
    round_off,               0.00
    updated_by,              1
    updated_date_time        NOW()
)
-- Returns: invoice_id = 5001
```

### Insert sales_invoice_dtl - Line 1 (Line 765-783):

```sql
INSERT INTO sales_invoice_dtl (
    invoice_id,              5001
    item_id,                 101
    hsn_code,                NULL
    quantity,                1000
    sales_weight,            1000
    uom_id,                  163
    rate,                    550
    amount_without_tax,      5500.00
    total_amount,            5500.00
    remarks                  "Raw Jute - 1000 KG"
)
```

### Insert sales_invoice_dtl - Line 2:

```sql
INSERT INTO sales_invoice_dtl (
    invoice_id,              5001
    item_id,                 102
    hsn_code,                NULL
    quantity,                1000
    sales_weight,            1000
    uom_id,                  163
    rate,                    550
    amount_without_tax,      5500.00
    total_amount,            5500.00
    remarks                  "Raw Jute - 1000 KG"
)
```

### Insert sales_invoice_jute (Line 786-801):

```sql
INSERT INTO sales_invoice_jute (
    invoice_id,              5001
    mr_no,                   (Step 1's branch_mr_no)
    mr_id,                   1001
    mukam_id,                1
    claim_amount,            1000
    unit_conversion,         "KG"
)
```

---

## Final Result in Database

### sales_invoice:

```
invoice_id: 5001
invoice_no: 1
invoice_date: 2026-04-02
invoice_amount: 11000
party_id: 5001          ← Company B
branch_id: 100          ← Company A
status_id: 3
round_off: 0.00
```

### sales_invoice_dtl (2 lines):

```
Line 1:
  quantity: 1000
  rate: 550 (500 * 1.10)
  amount_without_tax: 5500.00

Line 2:
  quantity: 1000
  rate: 550 (500 * 1.10)
  amount_without_tax: 5500.00

Total: 11000.00
```

### sales_invoice_jute:

```
invoice_id: 5001
mr_id: 1001           ← Links to original MR
mr_no: (Step 1 mr_no)
claim_amount: 1000
```

---

## Verification Formulas

### Rate Multiplier Verification:

```
Expected: 1 + (10 / 100) = 1.10 ✓

In database:
  Original Line Rate: 500
  Invoice Line Rate: 550
  Ratio: 550 / 500 = 1.10 ✓
```

### Total Verification:

```
Expected: 10000 * 1.10 = 11000 ✓

In database:
  Invoice Amount: 11000 ✓
  Line 1 + Line 2: 5500 + 5500 = 11000 ✓
```

### Claim Verification:

```
Expected: 1000 (unchanged) ✓

In database:
  sales_invoice_jute.claim_amount: 1000 ✓
```

---

## What Could Go Wrong in This Example?

1. **If rate_multiplier was 1.0 instead of 1.10:**
   - Invoice amount would be 10,000 (should be 11,000)
   - Line rates would be 500 (should be 550)

2. **If total_amount not calculated:**
   - invoice_amount might be 0 or NULL
   - Invoice header would be invalid

3. **If party_id not found:**
   - Could be NULL or wrong party
   - Invoice would link to wrong buyer

4. **If line items not copied:**
   - sales_invoice_dtl could be empty
   - Invoice totals wouldn't match header

5. **If rate applied incorrectly:**
   - Amounts could be 5,000 + 5,000 = 10,000 instead of 11,000
   - Would indicate multiplier wasn't applied to line rates

