# Group MR Overview by Header with Per-Line-Item Rate Cascading

## Problem

The current UI shows one row per line item (from `jute_mr` JOIN `jute_mr_li`). When a user sets a % rate increase on a transfer step, it only affects the selected line item's chain. Other line items of the same MR are not updated. During finalization, header-level totals reflect only one line item's values, causing incorrect postings. The % increase should apply uniformly to all line items of the same MR.

## Solution

Group the overview table by `jute_mr_id` so each row represents one MR header. The transfer editor operates at the MR level. A single % rate increase is applied to each line item's individual rate during recalculation and finalization.

## Data Layer

### Query (`queries.py`)

`get_jute_mr_with_line_items` remains unchanged — it returns one row per line item. Grouping happens in Python after fetching.

### Grouping logic (new helper in `jute_mr.py`)

After fetching the raw dataframe:

1. Group rows by `jute_mr_id`.
2. For each group, produce one aggregated row:
   - `jute_mr_id`: preserved from the group key (required for finalization bridge)
   - `Weight (KG)`: sum of `Weight (KG)` column across line items
   - `Total Amount`: sum of `Total Amount` column across line items
   - `Claim Amount`: sum of `Claim Amount` column across line items (each computed as `accepted_weight * claim_rate + water_damage_amount - premium_amount` per the query)
   - `Net Total`: `Total Amount - Claim Amount`
   - `MR Rate`: weighted average = `Total Amount / Weight * 100` (display-only; 0.0 if total weight is zero)
   - Scalar columns (`Jute Gate Entry No`, `MR DATE`, `Party Name`, `Party Branch Address`, `Jute Supplier`, etc.): taken from the first row
   - `Item Quality`: joined with ` / ` if multiple distinct values exist
3. Store the per-line-item details alongside for recalculation (see UI State).

**Column names**: The grouping logic works with the aliased column names from the query (`Weight (KG)`, `MR Rate`, `Claim Amount`, etc.), not raw DB column names.

## UI State

### Transfer state structure

Key changes from current design:

- **Keyed by `jute_mr_id`** instead of dataframe row index.
- Each entry stores:
  - `line_items`: list of `{weight, original_rate, original_claim}` — immutable source data per line item. Extracted from the raw per-line-item rows before grouping. `original_claim` is the already-computed claim amount per item (from the query formula).
  - `steps`: list of transfer step dicts.
- Each step dict stores:
  - `company`, `mr_date`, `pct_rate_increase` (authoritative input)
  - `mr_no` (assigned via batch)
  - Computed aggregates: `total_amount`, `claim_amount`, `net_amount`, `weighted_avg_rate` (all derived from line items + cumulative %)

**Single line item MRs**: Work identically to the current behavior — the `line_items` list has one entry, and all aggregations are trivially that item's values. No special casing needed.

### Recalculation (`_recalculate_chain`)

**Step 0 rationale**: Step 0 is the first buyer (company B) receiving jute at the original supplier's rate. There is no % increase at step 0 because the original rate is the purchase price — the first buyer doesn't mark up their own purchase. Rate increases happen from step 1 onward (when the first buyer sells to the next company).

```
prev_rates = [li.original_rate for each line item]
total_weight = sum(li.weight for each line item)

for each step i:
    if i == 0:
        rates_i = prev_rates  # first step uses original rates
    else:
        pct = step.pct_rate_increase
        rates_i = [r * (1 + pct / 100) for r in prev_rates]

    step.total_amount = sum(weight_j * rates_i[j] / 100 for j in items)
    step.claim_amount = sum(claim_j for j in items)  # sum of all items' original claims
    step.net_amount = step.total_amount - step.claim_amount
    step.weighted_avg_rate = (step.total_amount / total_weight * 100) if total_weight > 0 else 0.0

    prev_rates = rates_i
```

Rate cascading is cumulative: Step 2's rates are based on Step 1's rates, not the originals.

**Edge case**: If total weight is zero, `weighted_avg_rate` defaults to 0.0 (display-only, not used in calculations).

## Transfer Editor UI

Same horizontal card layout. Changes:

- **Source card**: shows total weight, weighted avg rate, total amount, total claim, net.
- **Step cards**: company selector, MR date, % increase (step 2+), and aggregated metrics (weighted avg rate, total amount, claim, net). No per-line-item editing.
- **Overview table**: one row per MR. `Item Quality` shows combined qualities if multiple.

## Finalization Changes

### `TransferStep` dataclass

Add field: `pct_rate_increase: float = 0.0`

The existing `mr_rate`, `total_amount`, `claim_amount`, `net_amount` remain as aggregate values for header-level use (MR header INSERT).

### UI finalization bridge (`jute_mr.py`)

The code that constructs `TransferStep` objects must also pass `pct_rate_increase` from the step dict:
```python
TransferStep(
    ...,
    pct_rate_increase=float(step.get("pct_rate_increase", 0)),
)
```

### `_create_mr` (`transfer.py`)

Updated signature: `_create_mr(conn, source_mr, step, party_id, party_branch_id, updated_by, rate_multiplier: float)`

**Header INSERT**: Uses `step.total_amount`, `step.claim_amount`, `step.net_amount` (pre-computed aggregates from recalculation). These are consistent with per-line-item sums because `_recalculate_chain` computes them that way.

**Line item loop**: Each line item's rate is computed from its source rate:
```python
original_rate = float(li.get("rate") or 0)
new_rate = original_rate * rate_multiplier
total_price = accepted_weight * new_rate / 100
```

### `_create_sales_invoice` (`transfer.py`)

Updated signature: `_create_sales_invoice(conn, seller_step, buyer_party_id, buyer_party_branch_id, mr_id, source_mr, updated_by, rate_multiplier: float)`

Invoice detail line item loop:
```python
original_rate = float(li.get("rate") or 0)
new_rate = original_rate * rate_multiplier
amount = accepted_weight * new_rate / 100
```

The `source_mr["line_items"]` rates are the original untouched rates (read at the start of the transaction before any updates). This is safe because `_update_original_mr` runs last.

### `_update_original_mr` (`transfer.py`)

Compute absolute rates in Python, not relative multiplies in SQL (avoids double-application if re-run):

```python
def _update_original_mr(conn, jute_mr_id, rate_multiplier, final_party_id,
                         final_party_branch_id, source_mr, updated_by):
    # Update each line item with its computed absolute rate
    for li in source_mr["line_items"]:
        li_id = li["jute_mr_li_id"]
        original_rate = float(li.get("rate") or 0)
        new_rate = original_rate * rate_multiplier
        accepted_weight = float(li.get("accepted_weight") or 0)
        new_total_price = accepted_weight * new_rate / 100
        # UPDATE jute_mr_li SET rate=:rate, total_price=:tp WHERE jute_mr_li_id=:id

    # Recompute header totals as sums
    # UPDATE jute_mr SET total_amount=(SELECT SUM(total_price) FROM jute_mr_li WHERE ...),
    #   claim_amount=(SELECT SUM(...) FROM jute_mr_li WHERE ...),
    #   net_total = total_amount - claim_amount, ...
```

### `finalize_transfer_chain` orchestrator

Tracks cumulative multiplier through the chain:
```python
cumulative_multiplier = 1.0
for i, step in enumerate(steps):
    if i > 0:
        cumulative_multiplier *= (1 + step.pct_rate_increase / 100)
    # pass cumulative_multiplier to _create_mr / _create_sales_invoice
```

**Final step**: The last step (return to source) calls `_update_original_mr` with the full `cumulative_multiplier` (which includes the final step's own `pct_rate_increase`).

**Execution order**: `_create_mr` and `_create_sales_invoice` for intermediate steps run first, reading `source_mr["line_items"]` original rates. `_update_original_mr` runs last, so it does not affect the rates read by earlier operations.

## Files Changed

| File | Change |
|------|--------|
| `src/jutetransfer/pages/jute_mr.py` | Group dataframe by `jute_mr_id`; rewrite transfer state init, recalculation, and editor to work at MR level; pass `pct_rate_increase` in `TransferStep` construction |
| `src/jutetransfer/transfer.py` | Add `pct_rate_increase` to `TransferStep`; add `rate_multiplier` param to `_create_mr` and `_create_sales_invoice`; rewrite `_update_original_mr` to compute absolute per-item rates; update `finalize_transfer_chain` to track cumulative multiplier |

## What stays the same

- Query in `queries.py` (still fetches per-line-item rows)
- Transfer chain logic (circular chain, company selection)
- MR number batch assignment
- Party/supplier lookup and creation
- Database schema (no migrations)
