# Populate `jute_mr` Invoice Fields from Seller Sales Invoice

**Date:** 2026-04-08
**Status:** Approved (pending implementation)
**Scope:** `src/jutetransfer/transfer.py`

## Problem

When a transfer step is saved, the seller's `sales_invoice` row is created with a freshly generated `invoice_no`, `invoice_date`, and `invoice_amount` ([transfer.py:802-998](../../../src/jutetransfer/transfer.py#L802-L998)). The buyer's corresponding `jute_mr` row is then created by `_create_mr()` (intermediate step) or updated by `_update_original_mr()` (return-to-source / final step) — but neither path writes those three columns. They remain NULL on every transfer MR.

The monthly grid in `jute_mr.py` already displays `Invoice No` and `Invoice Date` ([queries.py:178-179](../../../src/jutetransfer/queries.py#L178-L179)), so the columns appear blank for every transfer row today. Operators cannot reconcile a buyer's MR back to the seller's invoice without leaving the app.

## Goal

Populate `jute_mr.invoice_no`, `jute_mr.invoice_date`, and `jute_mr.invoice_amount` on every **transfer** MR (intermediate buyer + final return-to-source) using values from the corresponding `sales_invoice` row created in the same transaction.

## Non-Goals

- Backfilling existing `jute_mr` rows that were saved before this change. They keep their current NULL values.
- Populating these fields on first-step MRs (supplier delivery — see Edge Cases).
- Touching `payment_due_date`, `invoice_received_date`, `invoice_upload`, or any other invoice-related column on `jute_mr`.
- Changing how `sales_invoice` rows themselves are created.

## Design

### Data Flow

```
save_transfer_step (intermediate / final branch)
  ├─ _create_sales_invoice  →  returns dict with invoice_no, invoice_date, invoice_amount, …
  └─ _create_mr OR _update_original_mr
       └─ writes invoice_no/date/amount onto buyer's jute_mr row in same INSERT/UPDATE
```

The seller invoice details are produced once (inside `_create_sales_invoice`) and flow as a single dict through the call chain into the buyer-side write. No extra DB roundtrips; the buyer's MR row is correct on its first INSERT (or first UPDATE in the final-step case).

### Component Changes

#### 1. `_create_sales_invoice()` — return dict instead of tuple

**File:** `src/jutetransfer/transfer.py` (~line 802)

Change the return shape:

```python
# Before
return invoice_id, seller_step.mr_date, challan_no

# After
return {
    "invoice_id": invoice_id,
    "invoice_no": invoice_no,            # int (BigInteger from sales_invoice)
    "invoice_date": seller_step.mr_date, # date
    "invoice_amount": invoice_amount,    # float
    "challan_date": seller_step.mr_date,
    "challan_no": challan_no,
}
```

All three new values are already computed locally inside the function (current lines 816, 855, 882). This is purely an exposure change.

#### 2. `_create_mr()` — accept seller invoice, write to INSERT

**File:** `src/jutetransfer/transfer.py` (~line 552)

Add one optional parameter:

```python
def _create_mr(conn, source_mr, step, party_id, party_branch_id,
               updated_by, rate_multiplier, prev_co_id, root_mr_id,
               challan_date=None, challan_no=None,
               seller_invoice: Optional[dict] = None,   # NEW
               use_new_rounding=False) -> int:
```

In the existing `INSERT INTO jute_mr (...)` block, add three columns: `invoice_no`, `invoice_date`, `invoice_amount`. Bind values from `seller_invoice`:

- `invoice_no`: `str(seller_invoice["invoice_no"]) if seller_invoice else None`
  (cast required: `sales_invoice.invoice_no` is `BigInteger`, `jute_mr.invoice_no` is `String(255)`)
- `invoice_date`: `seller_invoice["invoice_date"] if seller_invoice else None`
- `invoice_amount`: `seller_invoice["invoice_amount"] if seller_invoice else None`

When `seller_invoice` is `None` (the first-step / supplier-delivery case), all three columns are written as NULL — preserving today's behavior for that path.

#### 3. `_update_original_mr()` — accept seller invoice, write to UPDATE

**File:** `src/jutetransfer/transfer.py` (~line 1005)

Add the same optional parameter:

```python
def _update_original_mr(conn, jute_mr_id, rate_multiplier,
                         final_party_id, final_party_branch_id,
                         source_mr, branch_id, mr_date, updated_by,
                         target_total=None,
                         rate_source_line_items=None,
                         seller_invoice: Optional[dict] = None,   # NEW
                         use_new_rounding=False) -> None:
```

Extend the existing `UPDATE jute_mr SET …` block (current line 1079) with three columns. Always-set form (because `save_transfer_step` always passes `seller_invoice` for the final step):

```sql
UPDATE jute_mr SET
    party_id = :party_id,
    ...
    invoice_no = :invoice_no,
    invoice_date = :invoice_date,
    invoice_amount = :invoice_amount,
    ...
WHERE jute_mr_id = :mr_id
```

Bind same way as `_create_mr`. If `seller_invoice` is `None` (defensive), pass NULL — but the contract is that the final-step caller always provides it.

#### 4. `save_transfer_step()` — wire it through

**File:** `src/jutetransfer/transfer.py` (~lines 1248-1323)

Two call-site changes inside the existing `else` branch:

**a)** Replace the tuple unpack of `_create_sales_invoice`:

```python
# Before
invoice_id, challan_date, challan_no = _create_sales_invoice(
    conn, prev_step_for_invoice, buyer_party_id,
    buyer_party_branch_id, prev_mr_id, source_mr,
    updated_by, rate_multiplier,
    use_new_rounding=use_new_rounding,
)

# After
seller_invoice = _create_sales_invoice(
    conn, prev_step_for_invoice, buyer_party_id,
    buyer_party_branch_id, prev_mr_id, source_mr,
    updated_by, rate_multiplier,
    use_new_rounding=use_new_rounding,
)
invoice_id = seller_invoice["invoice_id"]
challan_date = seller_invoice["challan_date"]
challan_no = seller_invoice["challan_no"]
```

**b)** Pass `seller_invoice=seller_invoice` to both downstream calls:

- `_update_original_mr(..., seller_invoice=seller_invoice, use_new_rounding=...)` (final-step branch)
- `_create_mr(..., seller_invoice=seller_invoice, use_new_rounding=...)` (intermediate-buyer branch)

The `is_first_step` branch is untouched.

#### 5. `revert_original_mr()` — clear invoice columns on revert

**File:** `src/jutetransfer/transfer.py` (~line 1115)

When finalization is reverted, the root MR is restored to its pre-finalization state. Since finalization now writes `invoice_no/date/amount`, revert must clear them so the row is symmetric. Add the three columns to the existing `UPDATE jute_mr SET …` block (current line 1163):

```sql
UPDATE jute_mr SET
    branch_mr_no = NULL,
    bill_pass_no = NULL,
    bill_pass_date = NULL,
    invoice_no = NULL,
    invoice_date = NULL,
    invoice_amount = NULL,
    status_id = 0,
    ...
WHERE jute_mr_id = :mr_id
```

No new parameters needed — these are unconditional NULL writes.

### Edge Cases

| Case | Behavior |
|---|---|
| First step (`is_first_step=True`, supplier delivery) | No `sales_invoice` is created on this step. `seller_invoice` is not passed to `_create_mr`, so `invoice_no/date/amount` are written as NULL. Matches today. |
| Intermediate step (B→C) | `seller_invoice` from B's just-created sales_invoice is passed to `_create_mr` for C's MR. C's MR row gets B's invoice details. |
| Final step (chain returns to A) | `seller_invoice` from the last seller's sales_invoice is passed to `_update_original_mr` for the root MR. Root MR gets the final seller's invoice details. |
| Revert finalization | `revert_original_mr` clears `invoice_no/date/amount` back to NULL. |
| Delete a transfer step | No change. Existing logic deletes the buyer's `jute_mr` row and the linked `sales_invoice` rows together; the new columns disappear with the row. |
| `invoice_no` type mismatch | `sales_invoice.invoice_no` is `BigInteger`; `jute_mr.invoice_no` is `String(255)`. Cast with `str()` at the bind site. No prefix added — matches how the value is already displayed in the monthly grid. |
| Backfill | Out of scope. Existing rows stay NULL. |

## Testing

Manual integration test (no automated test infra exists for this path today):

1. **Three-step chain (A→B→C→A):**
   - Create gate entry at Company A.
   - Save Step 1 (A→B). Verify B's `jute_mr.invoice_no/date/amount` are NULL (first step, supplier delivery — no inbound sales_invoice exists, expected).
   - Save Step 2 (B→C). This creates the B→C `sales_invoice` and C's `jute_mr` row. Verify C's `jute_mr.invoice_no/date/amount` match the B→C `sales_invoice` row (lookup: `branch_id = B.branch_id`, `invoice_date = step2.mr_date`).
   - Save Step 3 (C→A, final). This creates the C→A `sales_invoice` and updates the root MR in place. Verify root MR's `invoice_no/date/amount` now match the C→A `sales_invoice` row.

2. **Monthly grid display:** Open the main page filtered to the relevant month. Confirm `Invoice No` and `Invoice Date` columns now show values for the transfer rows that were previously blank. (`invoice_amount` is not yet shown in the grid — out of scope to add.)

3. **Revert finalization:** From the editor, revert the finalized chain. Verify the root MR's `invoice_no/date/amount` clear back to NULL.

4. **Delete intermediate step:** Delete Step 2. Verify C's MR row is gone and the related `sales_invoice` rows are deleted (existing behavior — sanity check we didn't break it).

## Open Questions

None. All scope decisions confirmed:
- Fields: `invoice_no` + `invoice_date` + `invoice_amount`.
- Backfill: not done.
- Final step: yes, root MR gets the last seller's invoice details.
- Revert: yes, clears the three columns back to NULL.

## File Touch List

- `src/jutetransfer/transfer.py` — only file modified.
  - `_create_sales_invoice` (~L802): change return type.
  - `_create_mr` (~L552): add `seller_invoice` param; extend INSERT.
  - `_update_original_mr` (~L1005): add `seller_invoice` param; extend UPDATE.
  - `save_transfer_step` (~L1248): wire dict through; pass to both downstream calls.
  - `revert_original_mr` (~L1115): clear invoice columns in UPDATE.
