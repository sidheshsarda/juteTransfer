# Populate `jute_mr` Invoice Fields from Seller Sales Invoice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Populate `jute_mr.invoice_no`, `invoice_date`, and `invoice_amount` on every transfer MR (intermediate buyer + return-to-source) from the corresponding `sales_invoice` row created in the same transaction.

**Architecture:** `_create_sales_invoice()` already computes the seller's `invoice_no`, `invoice_date`, and `invoice_amount` internally. We expose them via a return-dict, then thread that dict through `save_transfer_step` into `_create_mr()` (intermediate buyer) and `_update_original_mr()` (return-to-source) so each writes the three columns onto the buyer's `jute_mr` row in the same INSERT/UPDATE. `revert_original_mr()` is updated to clear the columns back to NULL on revert. No backfill, no new files.

**Tech Stack:** Python 3.12, SQLAlchemy Core (`text()` queries), MySQL, Streamlit (UI consumer — no UI changes in this plan).

**Spec:** [docs/superpowers/specs/2026-04-08-jute-mr-invoice-fields-design.md](../specs/2026-04-08-jute-mr-invoice-fields-design.md)

**Baseline: the "final-step challan inheritance" change is already applied in the working tree.**
This plan builds on top of it. Relevant existing state (not to be undone):

- `_update_original_mr(...)` has added optional parameters `challan_no: Optional[str] = None, challan_date: Optional[date] = None` at the end of its signature.
- `_update_original_mr(...)` already uses **conditional SQL building** to append `challan_no = :challan_no, challan_date = :challan_date` when those params are non-None. Variables: `optional_assignments: list`, `optional_params: dict`, `optional_sql: str`. The UPDATE is an f-string: `conn.execute(text(f"UPDATE jute_mr SET ... {optional_sql} WHERE ..."), {**optional_params, ...})`.
- `revert_original_mr(...)` already uses conditional SQL building to **restore** `challan_no`/`challan_date` from Step 1's source MR. Variables: `revert_assignments: list`, `revert_params: dict`, `revert_sql: str`.
- `save_transfer_step(...)` final-step and intermediate-step branches already unpack as `invoice_id, inv_challan_date, inv_challan_no = _create_sales_invoice(...)` and pass `challan_no=inv_challan_no, challan_date=step.challan_date or inv_challan_date` to both `_update_original_mr(...)` and `_create_mr(...)`.

**Style reconciliation:**
- For `_update_original_mr`: we follow the existing conditional-SQL style. Invoice fields are appended to the SAME `optional_assignments` / `optional_params` machinery, gated on `seller_invoice is not None`.
- For `revert_original_mr`: the invoice fields are unconditionally cleared to NULL alongside `branch_mr_no = NULL, bill_pass_no = NULL, bill_pass_date = NULL` in the BASE of the UPDATE (not via the challan restore conditional). This matches the existing pattern for unconditional NULL clears in the same SQL block.
- For `_create_mr`: the INSERT always writes the three new columns. When `seller_invoice is None` (first-step / supplier delivery) they are bound to `None` (→ NULL), preserving current first-step behavior.

**Files touched (one file only):**
- Modify: `src/jutetransfer/transfer.py`
  - `_create_sales_invoice` (~L802) — change return shape from tuple to dict
  - `save_transfer_step` (~L1314) — consume new dict, pass `seller_invoice` to downstream calls (in addition to the challan inheritance params already wired there)
  - `_create_mr` (~L552) — add `seller_invoice` param, extend INSERT with 3 invoice columns
  - `_update_original_mr` (~L1005) — add `seller_invoice` param, extend the existing `optional_assignments`/`optional_params` conditional SQL machinery with 3 invoice columns
  - `revert_original_mr` (~L1137) — add 3 unconditional `NULL` clears to the BASE of the UPDATE (separate from the existing conditional challan restore)

**Testing approach:** This codebase has unit tests only for pure-Python chain helpers (`tests/test_*.py`). `transfer.py` is DB-bound and has no test infrastructure — verification is manual: run a small chain through the Streamlit UI and confirm column values via SQL. Each task ends with the exact SQL to run and the expected result.

---

## Task 1: Change `_create_sales_invoice` return shape from tuple to dict

**Why first:** This is a pure-refactor task. After this task, the app behaves identically — we've only changed the *shape* of an internal return value and updated the one caller. This isolates the risk of the refactor from the new behavior in later tasks.

**Files:**
- Modify: `src/jutetransfer/transfer.py:802-998` (`_create_sales_invoice`)
- Modify: `src/jutetransfer/transfer.py:1314-1319` (call site in `save_transfer_step`, post-challan work)

- [ ] **Step 1: Change the return statement at the end of `_create_sales_invoice`**

Find the current return at the end of the function (currently `return invoice_id, seller_step.mr_date, challan_no`). Replace it with a dict that exposes all the values the caller will need:

```python
    return {
        "invoice_id": invoice_id,
        "invoice_no": invoice_no,            # int (BigInteger from sales_invoice)
        "invoice_date": seller_step.mr_date, # date
        "invoice_amount": invoice_amount,    # float
        "challan_date": seller_step.mr_date,
        "challan_no": challan_no,
    }
```

All three new keys (`invoice_no`, `invoice_date`, `invoice_amount`) are local variables already computed earlier in the function — `invoice_no` at the top (`invoice_no = _get_next_invoice_no(...)`), `invoice_amount` mid-function (`invoice_amount = float(seller_step.total_amount)`), and `invoice_date` is `seller_step.mr_date` which is the same value already used in the INSERT bind.

- [ ] **Step 2: Update the function's docstring and return type annotation**

Find the function signature `def _create_sales_invoice(...) -> tuple[int, date, str]:` and change the return annotation to `dict`. Update the docstring's "Returns" section:

```python
def _create_sales_invoice(conn, seller_step: TransferStep,
                           buyer_party_id: int, buyer_party_branch_id: Optional[int],
                           mr_id: int, source_mr: dict,
                           updated_by: int, rate_multiplier: float,
                           use_new_rounding: bool = False) -> dict:
    """Create a sales invoice from the seller to the buyer.

    Inserts into sales_invoice, sales_invoice_dtl, and sales_invoice_jute.

    When use_new_rounding=True, rounds rates at kg level (2 decimals),
    line item amounts to 2 decimals, and uses roundoff instead of largest-item adjustment.

    Returns dict with keys:
        invoice_id (int): Newly created sales_invoice.invoice_id
        invoice_no (int): Sequential invoice number for the seller's branch in the FY
        invoice_date (date): Invoice date (= seller_step.mr_date)
        invoice_amount (float): Header invoice amount
        challan_date (date): Challan date (= seller_step.mr_date)
        challan_no (str): Generated challan number
    """
```

- [ ] **Step 3: Update the call site in `save_transfer_step` to consume the dict**

Find the current call site (around line 1314, post-challan work):

```python
            invoice_id, inv_challan_date, inv_challan_no = _create_sales_invoice(
                conn, prev_step_for_invoice, buyer_party_id,
                buyer_party_branch_id, prev_mr_id, source_mr,
                updated_by, rate_multiplier,
                use_new_rounding=use_new_rounding,
            )
```

Replace with:

```python
            seller_invoice = _create_sales_invoice(
                conn, prev_step_for_invoice, buyer_party_id,
                buyer_party_branch_id, prev_mr_id, source_mr,
                updated_by, rate_multiplier,
                use_new_rounding=use_new_rounding,
            )
            invoice_id = seller_invoice["invoice_id"]
            inv_challan_date = seller_invoice["challan_date"]
            inv_challan_no = seller_invoice["challan_no"]
```

**Why keep `inv_challan_date` / `inv_challan_no` locals:** The existing final-step and intermediate-step branches already reference these by name (e.g., `challan_no=inv_challan_no`, `challan_date=step.challan_date or inv_challan_date` in the calls to `_update_original_mr` and `_create_mr`). Keeping the locals means this refactor is purely additive — the downstream code that uses them continues working unchanged. Task 2 and Task 3 will reference `seller_invoice` directly.

`invoice_id` is still used downstream (e.g., `logger.info(...)` and the `return {"mr_id": mr_id, "invoice_id": invoice_id}` at the end of `save_transfer_step`).

- [ ] **Step 4: Verify imports and module loads cleanly**

Run:
```bash
python -c "from src.jutetransfer import transfer; print('OK')"
```
Expected: `OK`. If it fails, you have a syntax error — fix it.

- [ ] **Step 5: Verify the existing chain-helper unit tests still pass**

These tests don't touch `transfer.py` directly, but they import from the same package. Confirm nothing was broken at import time:

```bash
python -m pytest tests/ -v
```
Expected: all tests pass (same count as before).

- [ ] **Step 6: Commit**

```bash
git add src/jutetransfer/transfer.py
git commit -m "refactor(transfer): _create_sales_invoice returns dict for invoice fields"
```

---

## Task 2: Wire `seller_invoice` into `_create_mr` for intermediate buyers

**Goal:** When an intermediate buyer's `jute_mr` is created in `_create_mr`, write the seller's `invoice_no`, `invoice_date`, and `invoice_amount` into the new row.

**Files:**
- Modify: `src/jutetransfer/transfer.py:552-748` (`_create_mr`)
- Modify: `src/jutetransfer/transfer.py:1362-1368` (intermediate-buyer call site in `save_transfer_step`, post-challan work)

- [ ] **Step 1: Add the `seller_invoice` parameter to `_create_mr`**

Find the signature (line ~552):

```python
def _create_mr(conn, source_mr: dict, step: TransferStep,
               party_id: int, party_branch_id: Optional[int],
               updated_by: int, rate_multiplier: float,
               prev_co_id: int, root_mr_id: int,
               challan_date: Optional[date] = None,
               challan_no: Optional[str] = None,
               use_new_rounding: bool = False) -> int:
```

Add a new optional parameter `seller_invoice` after `challan_no` and before `use_new_rounding`:

```python
def _create_mr(conn, source_mr: dict, step: TransferStep,
               party_id: int, party_branch_id: Optional[int],
               updated_by: int, rate_multiplier: float,
               prev_co_id: int, root_mr_id: int,
               challan_date: Optional[date] = None,
               challan_no: Optional[str] = None,
               seller_invoice: Optional[dict] = None,
               use_new_rounding: bool = False) -> int:
```

Update the docstring with one extra paragraph:

```python
    """Create a new jute_mr + jute_mr_li records for a transfer step.

    Copies most fields from the source MR, overriding company/branch/party/rate.
    If challan_date/challan_no are provided, uses those instead of source_mr values.

    When use_new_rounding=True, rounds rates at kg level (2 decimals) then *100,
    and rounds line item amounts to 2 decimals (no largest-item adjustment).

    If seller_invoice is provided (intermediate buyer step), writes
    invoice_no/invoice_date/invoice_amount onto the new MR row from the
    just-created sales_invoice. When None (first-step / supplier delivery),
    those columns are written as NULL.

    Returns the new jute_mr_id.
    """
```

- [ ] **Step 2: Add the three columns to the INSERT statement**

Find the `INSERT INTO jute_mr (...)` block (line ~578). Locate the column list — it currently ends with `bill_pass_no, bill_pass_date`. Add the three new columns right after `bill_pass_date`:

```python
    new_mr_id = DatabaseConnection.execute_insert_returning_id(conn, """
        INSERT INTO jute_mr (
            jute_gate_entry_no, branch_mr_no, jute_gate_entry_date,
            jute_mr_date, challan_date, challan_no, challan_weight,
            gross_weight, tare_weight, net_weight, variable_shortage,
            actual_weight, in_time, out_date, out_time, qc_check,
            mukam_id, unit_conversion, mr_weight, remarks, status_id,
            vehicle_no, marketing_slip, transporter, driver_name, frieght_paid,
            updated_by, updated_date_time, po_id, branch_id, party_id,
            party_branch_id, jute_supplier_id, src_com_id,
            total_amount, claim_amount, roundoff, net_total, tds_amount,
            src_jute_mr_id, bill_pass_no, bill_pass_date,
            invoice_no, invoice_date, invoice_amount
        ) VALUES (
            :gate_entry_no, :branch_mr_no, :gate_entry_date,
            :mr_date, :challan_date, :challan_no, :challan_weight,
            :gross_weight, :tare_weight, :net_weight, :variable_shortage,
            :actual_weight, :in_time, :out_date, :out_time, :qc_check,
            :mukam_id, :unit_conversion, :mr_weight, :remarks, :status_id,
            :vehicle_no, :marketing_slip, :transporter, :driver_name, :frieght_paid,
            :updated_by, NOW(), :po_id, :branch_id, :party_id,
            :party_branch_id, :jute_supplier_id, :src_com_id,
            :total_amount, :claim_amount, :roundoff, :net_total, :tds_amount,
            :src_jute_mr_id, :bill_pass_no, :bill_pass_date,
            :invoice_no, :invoice_date, :invoice_amount
        )
    """, {
```

- [ ] **Step 3: Bind the three new parameters from `seller_invoice`**

In the bind dict (the `{...}` arg passed to `execute_insert_returning_id`), find the last entry (`"bill_pass_date": step.mr_date,`). Add the three new bindings right after it:

```python
        "bill_pass_no": new_bill_pass_no,
        "bill_pass_date": step.mr_date,
        "invoice_no": str(seller_invoice["invoice_no"]) if seller_invoice else None,
        "invoice_date": seller_invoice["invoice_date"] if seller_invoice else None,
        "invoice_amount": seller_invoice["invoice_amount"] if seller_invoice else None,
    })
```

The `str()` cast on `invoice_no` is required because `sales_invoice.invoice_no` is `BigInteger` and `jute_mr.invoice_no` is `String(255)`.

- [ ] **Step 4: Update the intermediate-buyer call site in `save_transfer_step`**

Find the `_create_mr(...)` call inside the `else: # Create MR for buyer` branch (around line 1362, post-challan work). It currently looks like:

```python
                mr_id = _create_mr(
                    conn, source_mr, step, seller_party_id, seller_party_branch_id,
                    updated_by, rate_multiplier, prev_co_id, root_mr_id,
                    challan_date=step.challan_date or inv_challan_date,
                    challan_no=inv_challan_no,
                    use_new_rounding=use_new_rounding,
                )
```

Add `seller_invoice=seller_invoice` (the variable bound in Task 1, Step 3):

```python
                mr_id = _create_mr(
                    conn, source_mr, step, seller_party_id, seller_party_branch_id,
                    updated_by, rate_multiplier, prev_co_id, root_mr_id,
                    challan_date=step.challan_date or inv_challan_date,
                    challan_no=inv_challan_no,
                    seller_invoice=seller_invoice,
                    use_new_rounding=use_new_rounding,
                )
```

Note: do NOT add `seller_invoice` to the `is_first_step` branch's `_create_mr(...)` call — that branch creates the MR for a supplier delivery and there's no `sales_invoice` to copy from. The default `None` is correct there.

- [ ] **Step 5: Verify the module loads cleanly**

```bash
python -c "from src.jutetransfer import transfer; print('OK')"
```
Expected: `OK`.

- [ ] **Step 6: Manual integration test — verify intermediate buyer's MR carries invoice fields**

This requires the running Streamlit app and a working DB. If you can't run end-to-end right now, defer this verification step until Task 3 (you can verify both at once) — but **do not skip it before merge**.

1. Start the app: `streamlit run app.py`
2. Open an existing pending MR with a chain of at least 2 transfer steps (A→B→C). If none exists, create one: gate entry at Company A, save Step 1 (A→B), then Step 2 (B→C). Do **not** finalize yet.
3. Note the `jute_mr_id` of C's MR row from the editor or page.
4. Run this SQL against the database:

   ```sql
   SELECT
     mr.jute_mr_id,
     mr.invoice_no,
     mr.invoice_date,
     mr.invoice_amount,
     si.invoice_no       AS si_invoice_no,
     si.invoice_date     AS si_invoice_date,
     si.invoice_amount   AS si_invoice_amount
   FROM jute_mr mr
   LEFT JOIN sales_invoice_jute sij ON sij.mr_id = (
     -- the previous MR in the chain (B's MR)
     SELECT jute_mr_id FROM jute_mr
     WHERE src_jute_mr_id = mr.src_jute_mr_id
       AND branch_id != mr.branch_id
       AND jute_mr_id < mr.jute_mr_id
     ORDER BY jute_mr_id DESC LIMIT 1
   )
   LEFT JOIN sales_invoice si ON si.invoice_id = sij.invoice_id
   WHERE mr.jute_mr_id = <C_jute_mr_id>;
   ```

   Expected:
   - `mr.invoice_no` = `CAST(si.invoice_no AS CHAR)` (string form of seller's invoice number)
   - `mr.invoice_date` = `si.invoice_date`
   - `mr.invoice_amount` = `si.invoice_amount`

5. **Negative check** — confirm B's MR (the first-step buyer) still has NULL invoice fields:

   ```sql
   SELECT jute_mr_id, invoice_no, invoice_date, invoice_amount
   FROM jute_mr WHERE jute_mr_id = <B_jute_mr_id>;
   ```
   Expected: all three columns NULL.

- [ ] **Step 7: Commit**

```bash
git add src/jutetransfer/transfer.py
git commit -m "feat(transfer): write seller invoice fields onto intermediate buyer MR"
```

---

## Task 3: Wire `seller_invoice` into `_update_original_mr` for finalization

**Goal:** When the chain returns to source and `_update_original_mr` updates the root MR in place, write `invoice_no`, `invoice_date`, and `invoice_amount` from the final seller's `sales_invoice`.

**Style note:** The existing `_update_original_mr` already uses **conditional SQL building** via `optional_assignments: list` / `optional_params: dict` / `optional_sql: str` (added by the challan inheritance work). This task extends that same machinery — we do NOT add the invoice columns to the base (always-set) part of the UPDATE.

**Files:**
- Modify: `src/jutetransfer/transfer.py:1005-1126` (`_update_original_mr`)
- Modify: `src/jutetransfer/transfer.py:1343-1352` (final-step call site in `save_transfer_step`)

- [ ] **Step 1: Add the `seller_invoice` parameter to `_update_original_mr`**

Find the current signature (it ends with `challan_date: Optional[date] = None`):

```python
def _update_original_mr(conn, jute_mr_id: int, rate_multiplier: float,
                         final_party_id: int, final_party_branch_id: Optional[int],
                         source_mr: dict, branch_id: int,
                         mr_date: date, updated_by: int,
                         target_total: Optional[float] = None,
                         rate_source_line_items: Optional[list] = None,
                         use_new_rounding: bool = False,
                         challan_no: Optional[str] = None,
                         challan_date: Optional[date] = None) -> None:
```

Add `seller_invoice` at the very end of the signature:

```python
def _update_original_mr(conn, jute_mr_id: int, rate_multiplier: float,
                         final_party_id: int, final_party_branch_id: Optional[int],
                         source_mr: dict, branch_id: int,
                         mr_date: date, updated_by: int,
                         target_total: Optional[float] = None,
                         rate_source_line_items: Optional[list] = None,
                         use_new_rounding: bool = False,
                         challan_no: Optional[str] = None,
                         challan_date: Optional[date] = None,
                         seller_invoice: Optional[dict] = None) -> None:
```

Add a new entry to the docstring's `Args:` block, right after the `challan_date` entry:

```python
        seller_invoice: Dict from _create_sales_invoice (the last-hop invoice
            for the final step). When provided, invoice_no/invoice_date/
            invoice_amount on the root MR are set from these values
            (via the same conditional SQL machinery as challan_no/challan_date).
            When None, those columns are left untouched.
```

- [ ] **Step 2: Extend the existing conditional SQL machinery to cover the three invoice fields**

Find the block that builds `optional_assignments` / `optional_params` (currently only handles `challan_no` and `challan_date`). It looks like:

```python
    # Recompute header totals from line items; assign bill_pass_no and bill_pass_date.
    # challan_no / challan_date are only written when the caller passes them —
    # this keeps the "mukam_id untouched" and "leave columns alone when None"
    # invariants explicit. mukam_id is never in this UPDATE.
    optional_assignments = []
    optional_params: dict = {}
    if challan_no is not None:
        optional_assignments.append("challan_no = :challan_no")
        optional_params["challan_no"] = challan_no
    if challan_date is not None:
        optional_assignments.append("challan_date = :challan_date")
        optional_params["challan_date"] = challan_date

    optional_sql = (", " + ", ".join(optional_assignments)) if optional_assignments else ""
```

Extend it to also handle `seller_invoice`. After the `if challan_date is not None:` block and before `optional_sql = ...`, add:

```python
    if seller_invoice is not None:
        optional_assignments.append("invoice_no = :invoice_no")
        optional_params["invoice_no"] = str(seller_invoice["invoice_no"])
        optional_assignments.append("invoice_date = :invoice_date")
        optional_params["invoice_date"] = seller_invoice["invoice_date"]
        optional_assignments.append("invoice_amount = :invoice_amount")
        optional_params["invoice_amount"] = seller_invoice["invoice_amount"]
```

Also update the comment at the top of this block so future readers understand what "optional" covers:

```python
    # Recompute header totals from line items; assign bill_pass_no and bill_pass_date.
    # challan_no / challan_date / invoice_no / invoice_date / invoice_amount are
    # only written when the caller passes them — this keeps the "mukam_id
    # untouched" and "leave columns alone when None" invariants explicit.
    # mukam_id is never in this UPDATE.
```

The existing `conn.execute(text(f"""...{optional_sql}..."""), {**optional_params, ...})` call does not need any change — the extended `optional_assignments`/`optional_params` will flow through automatically.

The `str()` cast on `invoice_no` is required because `sales_invoice.invoice_no` is `BigInteger` and `jute_mr.invoice_no` is `String(255)`.

- [ ] **Step 3: Update the final-step call site in `save_transfer_step`**

Find the `_update_original_mr(...)` call in the `if is_final:` branch (around line 1343). It currently looks like:

```python
                _update_original_mr(
                    conn, root_mr_id_for_update, rate_multiplier,
                    last_seller_party_id, last_seller_party_branch_id,
                    root_mr, source_branch_id, step.mr_date, updated_by,
                    target_total=float(step.total_amount),
                    rate_source_line_items=rate_source_lis,
                    use_new_rounding=use_new_rounding,
                    challan_no=inv_challan_no,
                    challan_date=step.challan_date or inv_challan_date,
                )
```

Add `seller_invoice=seller_invoice`:

```python
                _update_original_mr(
                    conn, root_mr_id_for_update, rate_multiplier,
                    last_seller_party_id, last_seller_party_branch_id,
                    root_mr, source_branch_id, step.mr_date, updated_by,
                    target_total=float(step.total_amount),
                    rate_source_line_items=rate_source_lis,
                    use_new_rounding=use_new_rounding,
                    challan_no=inv_challan_no,
                    challan_date=step.challan_date or inv_challan_date,
                    seller_invoice=seller_invoice,
                )
```

- [ ] **Step 4: Verify module loads cleanly**

```bash
python -c "from src.jutetransfer import transfer; print('OK')"
```
Expected: `OK`.

- [ ] **Step 5: Manual integration test — finalize a chain and verify root MR**

1. Continue with the chain from Task 2 (A→B→C). Save the final step (C→A) using the editor's "Finalize" button.
2. Find the root MR's `jute_mr_id` (it's the original gate-entry MR at Company A — the one with `src_jute_mr_id IS NULL` and matching `jute_gate_entry_no`).
3. Run:

   ```sql
   SELECT
     mr.jute_mr_id,
     mr.invoice_no,
     mr.invoice_date,
     mr.invoice_amount,
     si.invoice_no       AS si_invoice_no,
     si.invoice_date     AS si_invoice_date,
     si.invoice_amount   AS si_invoice_amount
   FROM jute_mr mr
   LEFT JOIN sales_invoice_jute sij ON sij.mr_id = (
     -- the last seller's MR in the chain (C's MR)
     SELECT jute_mr_id FROM jute_mr
     WHERE src_jute_mr_id = mr.jute_mr_id
     ORDER BY jute_mr_id DESC LIMIT 1
   )
   LEFT JOIN sales_invoice si ON si.invoice_id = sij.invoice_id
   WHERE mr.jute_mr_id = <root_jute_mr_id>;
   ```

   Expected:
   - `mr.invoice_no` = `CAST(si.invoice_no AS CHAR)` (last seller's invoice number, as string)
   - `mr.invoice_date` = `si.invoice_date`
   - `mr.invoice_amount` = `si.invoice_amount`

4. **Cross-check via the monthly grid:** open the page filtered to the relevant month and confirm the `Invoice No` and `Invoice Date` columns now display values for the finalized root row that were previously blank.

- [ ] **Step 6: Commit**

```bash
git add src/jutetransfer/transfer.py
git commit -m "feat(transfer): write seller invoice fields onto root MR on finalization"
```

---

## Task 4: Clear invoice columns in `revert_original_mr`

**Goal:** When finalization is reverted, the root MR returns to a clean pre-finalization state — including clearing the three invoice columns Task 3 wrote.

**Style note:** `revert_original_mr` already has conditional SQL building (`revert_assignments` / `revert_params` / `revert_sql`) for the challan **restore** path (restore Step 1's challan, added by the challan inheritance work). Our invoice clears are *unconditional* (always write NULL), so they go into the BASE part of the UPDATE alongside `branch_mr_no = NULL`, `bill_pass_no = NULL`, `bill_pass_date = NULL` — NOT into the `revert_assignments` list.

**Files:**
- Modify: `src/jutetransfer/transfer.py:1137-1218` (`revert_original_mr`)

- [ ] **Step 1: Add the three unconditional NULL clears to the base UPDATE**

Find the `UPDATE jute_mr SET …` block (around line 1204, currently an f-string). The base list of always-set columns includes `branch_mr_no = NULL, bill_pass_no = NULL, bill_pass_date = NULL, status_id = 0` etc. The f-string appends `{revert_sql}` at the end for the conditional challan restore.

Add three NULL clears right after `bill_pass_date = NULL,`. The f-string, revert_assignments/revert_params, and the bind-dict splat all stay untouched — we're only adding three unconditional column assignments to the SQL template:

```python
    # Restore header: clear branch_mr_no/bill_pass_*, clear invoice_no/date/amount
    # (which finalization wrote from the last hop's sales_invoice), status back
    # to Pending, recompute totals. Do NOT touch party_id/party_branch_id or
    # mukam_id. Also restore challan_no / challan_date from Step 1's MR: Step 1
    # preserved the original gate-entry challan because _create_mr falls back
    # to source_mr's values when no override is supplied. Finalization
    # overwrote them with the last hop's invoice challan, so we need to put
    # the original back here.
    step1_challan_no = step1_source_mr.get("challan_no")
    step1_challan_date = step1_source_mr.get("challan_date")

    revert_assignments = []
    revert_params: dict = {"updated_by": updated_by, "mr_id": jute_mr_id}
    if step1_challan_no is not None:
        revert_assignments.append("challan_no = :challan_no")
        revert_params["challan_no"] = step1_challan_no
    if step1_challan_date is not None:
        revert_assignments.append("challan_date = :challan_date")
        revert_params["challan_date"] = step1_challan_date

    revert_sql = (", " + ", ".join(revert_assignments)) if revert_assignments else ""

    conn.execute(text(f"""
        UPDATE jute_mr SET
            branch_mr_no = NULL,
            bill_pass_no = NULL,
            bill_pass_date = NULL,
            invoice_no = NULL,
            invoice_date = NULL,
            invoice_amount = NULL,
            status_id = 0,
            total_amount = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id),
            roundoff = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id) -
                       (SELECT COALESCE(SUM(total_price), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id),
            net_total = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li WHERE jute_mr_id = :mr_id) - claim_amount,
            updated_by = :updated_by,
            updated_date_time = NOW()
            {revert_sql}
        WHERE jute_mr_id = :mr_id
    """), revert_params)
```

The block above is the full replacement for the existing block. Only the three new `NULL` lines and the block comment are changed — everything else matches the existing state.

- [ ] **Step 2: Update the docstring**

Find the docstring at the top of `revert_original_mr` (around line 1138). It currently reads:

```python
    """Revert the original MR to its pre-finalization state.

    Restores line item rates from Step 1 (the first transferred MR's snapshot,
    which is unaffected by finalization), clears branch_mr_no/bill_pass_*,
    sets status_id back to Pending. Does NOT touch party_id/party_branch_id —
    the original supplier party is not reliably recoverable, and leaving the
    current party in place is safe (a re-finalize will overwrite it correctly).
```

Extend the second sentence so the new behavior is explicit:

```python
    """Revert the original MR to its pre-finalization state.

    Restores line item rates from Step 1 (the first transferred MR's snapshot,
    which is unaffected by finalization), clears branch_mr_no/bill_pass_*,
    clears invoice_no/invoice_date/invoice_amount (which finalization wrote
    from the last seller's sales_invoice), sets status_id back to Pending.
    Does NOT touch party_id/party_branch_id — the original supplier party is
    not reliably recoverable, and leaving the current party in place is safe
    (a re-finalize will overwrite it correctly).
```

- [ ] **Step 3: Verify module loads cleanly**

```bash
python -c "from src.jutetransfer import transfer; print('OK')"
```
Expected: `OK`.

- [ ] **Step 4: Manual integration test — revert a finalized chain**

1. Continue with the finalized chain from Task 3 (root MR has `invoice_no/date/amount` populated).
2. Confirm starting state via SQL:

   ```sql
   SELECT jute_mr_id, status_id, branch_mr_no, bill_pass_no,
          invoice_no, invoice_date, invoice_amount
   FROM jute_mr WHERE jute_mr_id = <root_jute_mr_id>;
   ```
   Expected: `status_id=1`, `invoice_no` non-NULL, etc.

3. In the editor, click "Revert finalization" (or whatever the existing UI calls it — the action that calls `revert_original_mr`).

4. Re-run the SQL, this time also selecting `challan_no` / `challan_date` so we co-verify that the existing challan-restore behavior (from the challan inheritance work) isn't regressed:

   ```sql
   SELECT jute_mr_id, status_id, branch_mr_no, bill_pass_no,
          invoice_no, invoice_date, invoice_amount,
          challan_no, challan_date
   FROM jute_mr WHERE jute_mr_id = <root_jute_mr_id>;
   ```
   Expected:
   - `status_id` = 0 (Pending)
   - `branch_mr_no` = NULL
   - `bill_pass_no` = NULL
   - **`invoice_no` = NULL** *(new in this task)*
   - **`invoice_date` = NULL** *(new in this task)*
   - **`invoice_amount` = NULL** *(new in this task)*
   - `challan_no` = Step 1's original gate-entry challan_no *(existing challan-restore behavior — co-verifying no regression)*
   - `challan_date` = Step 1's original gate-entry challan_date *(existing challan-restore behavior — co-verifying no regression)*

5. **Re-finalize check (smoke test):** finalize the chain again. Verify the root MR's invoice columns repopulate correctly (Task 3's behavior is idempotent across revert/re-finalize cycles) AND the challan columns update to the last-hop invoice challan again (existing behavior, idempotent).

- [ ] **Step 5: Sanity check — delete an intermediate step still works**

This plan does not modify `delete_transfer_step`, but the spec calls for a regression check to ensure the new INSERT/UPDATE columns don't interfere with the existing cascade-delete logic.

1. With the chain currently in a non-final state (revert again from Step 4 if needed), pick any intermediate buyer's MR (e.g., C's) and delete it via the editor's delete button.
2. Confirm the row is gone:

   ```sql
   SELECT jute_mr_id FROM jute_mr WHERE jute_mr_id = <C_jute_mr_id>;
   ```
   Expected: zero rows.

3. Confirm the linked `sales_invoice` rows for C are also gone (the existing cascade — `delete_transfer_step` deletes invoices linked to both this MR and the previous one):

   ```sql
   SELECT invoice_id FROM sales_invoice_jute WHERE mr_id = <C_jute_mr_id>;
   ```
   Expected: zero rows.

If either query returns rows, the new INSERT broke something — investigate before proceeding.

- [ ] **Step 6: Run the existing test suite one last time**

```bash
python -m pytest tests/ -v
```
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/jutetransfer/transfer.py
git commit -m "fix(transfer): clear invoice fields on revert_original_mr"
```

---

## Done

All four tasks complete. Behavior summary:

| Step type | `invoice_no` / `invoice_date` / `invoice_amount` source |
|---|---|
| First step (supplier → first buyer) | NULL (no sales_invoice exists) |
| Intermediate step (B → C) | The B→C `sales_invoice` row created in the same txn |
| Final step (C → A, in-place) | The C→A `sales_invoice` row created in the same txn |
| Revert finalization | Cleared to NULL |
| Pre-existing rows | Untouched (no backfill) |
