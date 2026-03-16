# Jute Transfer — MR Grouping, Chain Tracking, and Partial Saves

## Overview

This spec covers five interrelated changes to the jute transfer system:

1. **MR Grouping**: Group overview table by `jute_mr_id` (one row per MR, not per line item)
2. **Per-Line-Item Rate Cascading**: Apply uniform % rate increase to each item's individual rate
3. **Field Fixes**: `status_id=3`, `po_id=None`, warehouse as input, gate entry recalculated
4. **Chain Tracking**: Repurpose `src_com_id` to mean "received from", derive ownership from `branch_id`
5. **Partial Saves**: Save/edit/delete individual transfer steps; full chain not required upfront

---

## Part 1: MR Grouping + Per-Line-Item Rates

### Data Layer

**Query (`queries.py`)**: `get_jute_mr_with_line_items` unchanged — returns one row per line item. Grouping happens in Python.

**Queries that use `src_com_id` for ownership must be updated** (see Part 4).

### Grouping logic (new helper in `jute_mr.py`)

After fetching the raw dataframe:

1. Group rows by `jute_mr_id`.
2. For each group, produce one aggregated row:
   - `jute_mr_id`: preserved from group key
   - `Weight (KG)`: sum across line items
   - `Total Amount`: sum across line items
   - `Claim Amount`: sum across line items (each computed as `accepted_weight * claim_rate + water_damage_amount - premium_amount`)
   - `Net Total`: `Total Amount - Claim Amount`
   - `MR Rate`: weighted average = `Total Amount / Weight * 100` (display-only; 0.0 if zero weight)
   - Scalar columns (`Jute Gate Entry No`, `MR DATE`, `Party Name`, etc.): from first row
   - `Item Quality`: joined with ` / ` if multiple distinct values
3. Store per-line-item details alongside: `{weight, original_rate, original_claim}` per item.

**Column names**: Grouping uses aliased names from query (`Weight (KG)`, `MR Rate`, etc.).

### UI State

- **Keyed by `jute_mr_id`** instead of dataframe index.
- Each entry: `line_items` (immutable source data) + `steps` (transfer step dicts).
- Each step: `company`, `branch_id`, `mr_date`, `pct_rate_increase`, `warehouse_id`, `mr_no`, computed aggregates.

### Recalculation (`_recalculate_chain`)

Step 0 uses original rates (first buyer doesn't mark up their purchase). Rate increases from step 1 onward.

```
prev_rates = [li.original_rate for each line item]
total_weight = sum(li.weight for each line item)

for each step i:
    if i == 0:
        rates_i = prev_rates
    else:
        pct = step.pct_rate_increase
        rates_i = [r * (1 + pct / 100) for r in prev_rates]

    step.total_amount = sum(weight_j * rates_i[j] / 100 for j in items)
    step.claim_amount = sum(claim_j for j in items)
    step.net_amount = step.total_amount - step.claim_amount
    step.weighted_avg_rate = (step.total_amount / total_weight * 100) if total_weight > 0 else 0.0

    prev_rates = rates_i
```

Rate cascading is cumulative: Step 2's rates are based on Step 1's output rates.

### Transfer Editor UI

Same horizontal card layout:
- **Source card**: total weight, weighted avg rate, total amount, total claim, net.
- **Step cards**: company selector, MR date, **warehouse selector** (filtered by step's branch), % increase (step 2+), aggregated metrics.
- **Overview table**: one row per MR.

---

## Part 2: Field Fixes in `_create_mr` and `_create_sales_invoice`

### status_id

| Location | Current | Fix |
|---|---|---|
| `transfer.py:462` — `_create_mr` | `0` (Pending) | `3` (Approved) |
| `transfer.py:595` — `_create_sales_invoice` | `0` | `3` (Approved) |

### po_id

| Location | Current | Fix |
|---|---|---|
| `transfer.py:469` — `po_id` | `source_mr.get("po_id")` | `None` (PO is company-specific) |
| `transfer.py:521` — `jute_po_li_id` (line items) | `li.get("jute_po_li_id")` | `None` |

### warehouse_id

| Location | Current | Fix |
|---|---|---|
| `transfer.py:542` — line items | `li.get("warehouse_id")` (copied from source) | User-selected per step (passed via `TransferStep`) |

`TransferStep` gains: `warehouse_id: Optional[int] = None`

In the UI, the warehouse selector is filtered by the step's branch: query `warehouse_mst WHERE branch_id = :step_branch_id`.

### jute_gate_entry_no

| Location | Current | Fix |
|---|---|---|
| `transfer.py:442` | `source_mr.get("jute_gate_entry_no")` (copied) | Newly calculated: `MAX(jute_gate_entry_no) + 1` for the target branch |

New helper: `_get_next_gate_entry_no(conn, branch_id)` — queries `MAX(jute_gate_entry_no)` from `jute_mr WHERE branch_id = :bid` within the current FY.

### jute_gate_entry_date

| Location | Current | Fix |
|---|---|---|
| `transfer.py:444` | `source_mr.get("jute_gate_entry_date")` (copied) | `step.mr_date` (the user-selected MR date) |

---

## Part 3: Chain Tracking via `src_com_id`

### Semantic change

`src_com_id` changes from "company that owns this MR" to **"company from which this MR was received"**.

| MR | `src_com_id` (new meaning) | `src_jute_mr_id` |
|---|---|---|
| Original at Company A (id=111) | `NULL` (no source — this is the origin) | `NULL` |
| Transfer to B (id=222) | `A's co_id` (received from A) | `111` |
| Transfer to C (id=333) | `B's co_id` (received from B) | `111` |
| Transfer to D (id=444) | `C's co_id` (received from C) | `111` |

- `src_jute_mr_id` always points to the root MR (star topology — all transfers share the same root).
- `src_com_id` encodes the linked-list ordering.

### Chain reconstruction algorithm

```
1. Find all MRs where src_jute_mr_id = root_id, ORDER BY jute_mr_id ASC
2. Start from root company (derived from root MR's branch_id → branch_mst.co_id)
3. visited = set()
4. current_co = root_company
5. Loop:
   a. Find unvisited MR where src_com_id = current_co
      - If multiple matches (same company sends to different recipients),
        pick the one with the LOWEST jute_mr_id (created first = earlier in chain)
   b. Mark as visited
   c. Derive current_co from this MR's branch_id → branch_mst.co_id
   d. Repeat until no unvisited MR found
6. Chain order = visited list in traversal order
```

**Disambiguation**: When a company appears as sender more than once (e.g., A→B→C→B→D→A where B sends to both C and D), `jute_mr_id` ordering resolves ambiguity since MRs are created sequentially via `save_transfer_step` (auto-increment IDs guarantee creation order = chain order).

### Query updates (ownership derived from `branch_id`)

Since `src_com_id` no longer means "owner", all queries that used it for ownership must derive the owning company from `branch_id → branch_mst.co_id`.

**`get_next_mr_number` / `get_next_mr_numbers_batch`:**
```sql
-- Before:
WHERE src_com_id = :co_id AND branch_id = :branch_id
-- After (branch_id alone is sufficient since it's unique per co+branch):
WHERE branch_id = :branch_id AND jute_mr_date BETWEEN :fy_start AND :fy_end
```

**`get_jute_mr_with_line_items` — party join:**
```sql
-- Before:
LEFT JOIN party_mst pm ON pm.party_id = mr.party_id AND pm.co_id = mr.src_com_id
-- After:
INNER JOIN branch_mst bm ON mr.branch_id = bm.branch_id
LEFT JOIN party_mst pm ON pm.party_id = mr.party_id AND pm.co_id = bm.co_id
```

**`get_jute_mr_with_line_items` — company filter:**
```sql
-- Before:
AND mr.src_com_id = :co_id
-- After:
AND bm.co_id = :co_id
```

**`_ensure_supplier_party` in `transfer.py`:**
```python
# Before:
source_co_id = int(source_mr.get("src_com_id") or 0)
# After: derive from branch_id
source_branch_id = int(source_mr.get("branch_id") or 0)
# then look up co_id from branch_mst, or pass it as a parameter
```

### `_create_mr` update

```python
"src_com_id": prev_step_co_id,  # company from which this MR was received
"src_jute_mr_id": root_mr_id,   # always the original MR's ID
```

For step[0]: `src_com_id = source_co_id` (the original company).
For step[i>0]: `src_com_id = steps[i-1].co_id` (previous step's company).

---

## Part 4: Partial Saves, Edit, and Delete

### Architecture

Replace the monolithic `finalize_transfer_chain` with per-step operations:

#### New function: `save_transfer_step`

```python
def save_transfer_step(
    source_mr_id: int,       # root MR
    step: TransferStep,       # the step being saved
    prev_co_id: int,          # company from which this step receives
    prev_mr_id: int,          # previous MR ID (for invoice creation)
    source_mr: dict,          # source MR data (for copying fields)
    root_mr_id: int,          # for src_jute_mr_id
    updated_by: int,
    rate_multiplier: float,
    is_final: bool = False,   # True when chain returns to source
) -> dict:
    """Save a single transfer step: create MR + invoice.

    Returns: {mr_id, invoice_id}
    """
```

One transaction per step:
1. Ensure party mappings (seller as party in buyer's company).
2. Create sales invoice from seller to buyer.
3. Create MR for buyer (with `src_com_id = prev_co_id`, `src_jute_mr_id = root_mr_id`).
4. If `is_final`: also call `_update_original_mr` and assign `branch_mr_no` to original MR.

#### New function: `delete_transfer_step`

```python
def delete_transfer_step(jute_mr_id: int, updated_by: int) -> None:
    """Delete a transfer MR and its associated invoice.

    Deletion order (within a transaction):
    1. Find invoice via: SELECT sij.invoice_id FROM sales_invoice_jute sij
       WHERE sij.mr_id = :jute_mr_id
       (sales_invoice_jute.mr_id stores the MR that this invoice was created for)
    2. DELETE FROM sales_invoice_jute WHERE invoice_id = :invoice_id
    3. DELETE FROM sales_invoice_dtl WHERE invoice_id = :invoice_id
    4. DELETE FROM sales_invoice WHERE invoice_id = :invoice_id
    5. DELETE FROM jute_mr_li WHERE jute_mr_id = :jute_mr_id
    6. DELETE FROM jute_mr WHERE jute_mr_id = :jute_mr_id
    """
```

**Invoice linkage**: When `_create_sales_invoice` is called, it inserts into `sales_invoice_jute` with `mr_id = prev_mr_id` (the seller's MR). So to find the invoice created when step X was sold to step X+1, query `sales_invoice_jute WHERE mr_id = step_X_mr_id`. However, for the invoice where step X *bought* (the invoice created by step X-1 selling to X), query `sales_invoice_jute WHERE mr_id = step_X-1_mr_id`.

When deleting step X, delete **both**:
- The purchase invoice (step X-1 sold to X): `sales_invoice_jute WHERE mr_id = step_X-1_mr_id` — but only if step X-1 is also being deleted or this is step[0].
- In practice: cascade deletion from edited step onward handles this naturally.

#### Edit flow: delete + recreate

To edit a step in the middle of a chain:
1. Identify all steps from the edited step onward (using chain reconstruction algorithm).
2. Delete them in reverse order (last step first) via `delete_transfer_step`.
3. **If the final step was included (chain was complete)**: revert the original MR — restore original `party_id`, `party_branch_id`, `branch_mr_no = NULL`, and revert each line item's `rate` and `total_price` to the source MR's original values.
4. User rebuilds from the edited point.

#### `finalize_transfer_chain` — kept as convenience wrapper

For cases where the full chain is known upfront, calls `save_transfer_step` in a loop.

### MR number assignment

Moves **inside the transaction** in `save_transfer_step`:
```python
mr_no = _get_next_mr_number_in_txn(conn, step.branch_id)  # MAX(branch_mr_no)+1
```

The UI can show a preview number, but the actual number is assigned at save time (avoids race conditions).

### Chain status detection

No separate chain status table needed. Derive from data:

- **No chain**: `SELECT COUNT(*) FROM jute_mr WHERE src_jute_mr_id = :root_id` returns 0.
- **In progress**: Count > 0, and the original MR has `branch_mr_no = NULL` (not yet finalized).
- **Complete**: The original MR has `branch_mr_no IS NOT NULL` (assigned during final step).

### UI changes

**Overview table** gains a `Chain Status` column derived from DB queries (not just session state).

**Transfer editor** when opening an MR with existing chain:
1. Query chain from DB using reconstruction algorithm.
2. Display completed steps as **read-only** cards (company, date, rate, MR#).
3. Show editable cards for new/unsaved steps.
4. Buttons:
   - **"Save Step"** — saves the next unsaved step via `save_transfer_step`.
   - **"Finalize MR"** — appears only when chain returns to source. Saves the final step + updates original MR.
   - **"Edit Step X"** — deletes step X and all subsequent steps, makes them editable again.
   - **"Delete Step X"** — same as edit but doesn't pre-populate.

### Hydrating existing chain from DB

New query: `get_transfer_chain(root_mr_id)`:
```sql
SELECT mr.jute_mr_id, mr.src_com_id, mr.branch_id, mr.jute_mr_date,
       mr.branch_mr_no, mr.total_amount, mr.claim_amount, mr.net_total,
       bm.co_id AS owner_co_id, bm.branch_name,
       cm.co_name, cm.co_prefix
FROM jute_mr mr
JOIN branch_mst bm ON mr.branch_id = bm.branch_id
JOIN co_mst cm ON bm.co_id = cm.co_id
WHERE mr.src_jute_mr_id = :root_id
```

Then apply the chain reconstruction algorithm in Python to order them.

---

## Part 5: branch_mr_no on Return to Source

The original MR has `branch_mr_no = NULL` (new saves don't populate it at gate entry).

When the chain completes (jute returns to source company), assign a `branch_mr_no`:

```python
# Inside save_transfer_step when is_final=True, after _update_original_mr:
new_mr_no = _get_next_mr_number_in_txn(conn, source_branch_id)
conn.execute(text("""
    UPDATE jute_mr SET branch_mr_no = :mr_no WHERE jute_mr_id = :id
"""), {"mr_no": new_mr_no, "id": source_mr_id})
```

The MR number is scoped to `(branch_id, FY)` using `MAX(branch_mr_no) + 1` where `jute_mr_date BETWEEN fy_start AND fy_end`.

**Cross-FY edge case**: If the return happens in a different FY than the original gate entry, the `branch_mr_no` is generated in the current FY (based on the final step's `mr_date`). Only `jute_mr_date` is updated to the final step's date (for FY consistency with the new `branch_mr_no`). `jute_gate_entry_date` is NOT changed — it remains as the original physical gate entry date so the MR stays visible in its original month view.

---

## TransferStep Dataclass (updated)

```python
@dataclass
class TransferStep:
    co_id: int
    branch_id: int
    mr_date: date
    mr_rate: float              # weighted avg rate (display/header use)
    total_amount: float         # aggregate across line items
    claim_amount: float         # aggregate
    net_amount: float           # aggregate
    mr_no: int
    pct_rate_increase: float = 0.0
    warehouse_id: Optional[int] = None
    gate_entry_no: Optional[int] = None   # newly calculated per branch
```

---

## Files Changed

| File | Change |
|------|--------|
| `src/jutetransfer/pages/jute_mr.py` | Group by `jute_mr_id`; MR-level transfer editor with warehouse selector; partial save/edit/delete buttons; hydrate chains from DB; read-only completed steps |
| `src/jutetransfer/transfer.py` | `TransferStep` gains `pct_rate_increase`, `warehouse_id`, `gate_entry_no`; new `save_transfer_step`, `delete_transfer_step`; update `_create_mr` (status_id=3, po_id=None, warehouse from step, gate_entry calculated, src_com_id=prev company); update `_create_sales_invoice` (status_id=3, rate_multiplier); update `_update_original_mr` (per-item rates, assign branch_mr_no); MR numbers assigned inside transaction |
| `src/jutetransfer/queries.py` | Update all queries using `src_com_id` to derive ownership from `branch_id → branch_mst.co_id`; new `get_transfer_chain` query; `get_next_mr_number` scoped by `branch_id` only |
| `src/jutetransfer/models.py` | Update `src_com_id` column comment to reflect new semantics |

## What stays the same

- Database schema (no new tables, no new columns — reuses existing `src_com_id` and `src_jute_mr_id`)
- Party/supplier lookup and creation logic
- Company-branch options for dropdowns
