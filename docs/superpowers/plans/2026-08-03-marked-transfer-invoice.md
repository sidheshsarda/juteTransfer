# Marked Transfer Seller Invoice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Type 2 batch transfer also creates the seller-side Raw-Jute sales invoice at the source branch (owner amendment 2026-08-03 to the warehouse-lot-management spec §4 Tab 2 step 5 / §6), auto-populating any missing masters (party, item/quality) on both ends, and undo deletes the invoice.

**Architecture:** One `sales_invoice` (+`_dtl`, `_jute`, `_jute_dtl`) per child MR, created inside the same `save_marked_batch` transaction after the child's header recompute. Buyer party = target company in the source company's `party_mst`. Deletion linkage: `sales_invoice_jute.mr_id` = **child** MR id (different from Type 1's seller-MR semantics — safe because mode-1 MRs never enter chains). `delete_marked_move` cascades the invoice before restoring sources, in both provenance and legacy paths.

**Tech Stack:** Python 3.12, SQLAlchemy raw SQL via `text()`, Streamlit, pytest.

## Global Constraints

- sls tenant DB only; changes only in the juteTransfer repo.
- All multi-statement writes inside `DatabaseConnection.get_transaction()`; never partial commits.
- `transfer_mode` keeps the two transfer types disjoint; no change to Type 1 (chain) logic or pages.
- Invoice fields copy Type 1 conventions exactly: `invoice_type = 5` (`RAW_JUTE_INVOICE_TYPE`), `status_id = 3`, `active = 1`, `uom_id = 163`, kg rate = `round(quintal_rate / 100, 2)`, formatted no `<co>/<branch>/SI/<FY>/<n>`, FY-window numbering derived from the document date.
- Marked moves are claim-free: all claim fields on the invoice are 0.
- Masters auto-populate if missing (owner requirement): party via `_ensure_company_as_party` (both directions), item/quality via `_ensure_item` (quality IS the item in this app — `item_mst.item_name` via `actual_item_id`; `jute_quality_mst` is deprecated in the ERP; the deprecated `actual_quality`/`challan_quality_id` ids are copied unchanged, same as Type 1).
- Spec (already amended, do not edit): `docs/superpowers/specs/2026-08-03-warehouse-lot-management-design.md`.

---

### Task 1: Ops — invoice creation in `save_marked_batch`, cascade delete in `delete_marked_move`

**Files:**
- Modify: `src/jutetransfer/warehouse_stock_ops.py`

**Interfaces:**
- Consumes (from `.transfer`, no circular import — `transfer.py` never imports this module): `RAW_JUTE_INVOICE_TYPE`, `_format_document_no`, `_get_next_challan_no`, `_get_next_invoice_no`, `_get_seller_prefixes`, plus already-imported `_ensure_company_as_party`, `_ensure_item`.
- Produces: `save_marked_batch(...) -> list[dict]` where each dict is `{"child_mr_id": int, "invoice_no": str, "invoice_amount": float}` (**return type changes** from `list[int]`; the page — sole caller — is updated in Task 2). New private `_create_marked_sales_invoice(conn, child_mr_id, src_mr_id, src_co_id, src_branch_id, buyer_party_id, buyer_party_branch_id, mr_date, updated_by) -> dict`.

- [ ] **Step 1: Extend the `.transfer` import block**

Replace the existing `from .transfer import (...)` at the top of `warehouse_stock_ops.py` with:

```python
from .transfer import (
    RAW_JUTE_INVOICE_TYPE,
    _ensure_company_as_party,
    _ensure_item,
    _format_document_no,
    _get_next_challan_no,
    _get_next_gate_entry_no,
    _get_next_invoice_no,
    _get_next_mr_number_in_txn,
    _get_next_bill_pass_no_in_txn,
    _get_seller_prefixes,
)
```

- [ ] **Step 2: Update the module docstring**

Replace the whole module docstring with (content change: transfer = inter-company sale; legacy `save_marked_move` still invoice-free):

```python
"""Warehouse-marked stock moves (partial-quantity, no circular chain).

A "mark move" takes part of a purchased stock line at company A and moves it
into a marked godown at company B: it reduces the source line's accepted_weight
(the balance stays at A) and inserts a child jute_mr (transfer_mode=1) at B
carrying only the moved quantity at a possibly-new rate. The batch transfer is
an inter-company SALE (owner amendment 2026-08-03): one seller-side Raw-Jute
sales_invoice (invoice_type=5) is created at the source branch per child MR,
and missing masters (party, item/quality) are auto-created on both ends.

Kept deliberately separate from the vertical transfer chain (transfer.py):
no rate cascade, no finalization / return-to-origin, no chain of invoices.
The two worlds are disjoint by jute_mr.transfer_mode (0 = chain, 1 = marked
stock). The legacy single-line save_marked_move still creates no invoice
(dead code kept for compatibility; the page only calls save_marked_batch).

Run `python -m src.jutetransfer.warehouse_stock_ops` for the split_weights
self-check (no DB required).
"""
```

- [ ] **Step 3: Add `_create_marked_sales_invoice` after the `_LI_INSERT_SQL` block**

```python
def _create_marked_sales_invoice(conn, child_mr_id: int, src_mr_id: int,
                                 src_co_id: int, src_branch_id: int,
                                 buyer_party_id: int, buyer_party_branch_id,
                                 mr_date: date, updated_by: int) -> dict:
    """Seller-side Raw-Jute invoice for one marked child MR (owner amendment
    2026-08-03): the batch transfer IS the inter-company sale, so the source
    branch bills the target company at the child's (marked-up) rates. One
    invoice per child MR; claim-free by design.

    sales_invoice_jute.mr_id stores the CHILD MR id — the deletion linkage
    for delete_marked_move. NOTE: Type 1 stores the seller MR id there; the
    differing semantics are safe because mode-1 MRs never enter a chain.
    Invoice line item ids are remapped back to the SOURCE company via
    _ensure_item (child lines carry target-company item ids; normally a
    no-op lookup since the item originates at the source).
    """
    lines = [
        dict(r._mapping) for r in conn.execute(text("""
            SELECT accepted_weight, rate, total_price, actual_item_id,
                   actual_qty, unit_conversion
            FROM jute_mr_li WHERE jute_mr_id = :id
            ORDER BY jute_mr_li_id
        """), {"id": child_mr_id}).fetchall()
    ]
    line_sum = round(sum(float(l["total_price"] or 0) for l in lines), 2)
    invoice_amount = float(round(line_sum, 0))
    round_off = round(invoice_amount - line_sum, 2)

    invoice_no = _get_next_invoice_no(conn, src_branch_id, mr_date)
    challan_no = _get_next_challan_no(conn, src_branch_id, mr_date)
    co_prefix, branch_prefix = _get_seller_prefixes(conn, src_branch_id)
    invoice_no_formatted = _format_document_no(
        invoice_no, co_prefix, branch_prefix, mr_date, document_type="SI",
    )

    src = conn.execute(text("""
        SELECT branch_mr_no, mukam_id, unit_conversion
        FROM jute_mr WHERE jute_mr_id = :id
    """), {"id": src_mr_id}).fetchone()._mapping

    invoice_id = DatabaseConnection.execute_insert_returning_id(conn, """
        INSERT INTO sales_invoice (
            invoice_no, invoice_date, invoice_type, invoice_amount,
            party_id, billing_to_id, shipping_to_id, branch_id,
            challan_date, challan_no,
            active, status_id, round_off, updated_by, updated_date_time
        ) VALUES (
            :invoice_no, :invoice_date, :invoice_type, :invoice_amount,
            :party_id, :billing_to_id, :shipping_to_id, :branch_id,
            :challan_date, :challan_no,
            1, 3, :round_off, :updated_by, NOW()
        )
    """, {
        "invoice_no": invoice_no,
        "invoice_date": mr_date,
        "invoice_type": RAW_JUTE_INVOICE_TYPE,
        "invoice_amount": invoice_amount,
        "party_id": buyer_party_id,
        "billing_to_id": buyer_party_branch_id,
        "shipping_to_id": buyer_party_branch_id,
        "branch_id": src_branch_id,
        "challan_date": mr_date,
        "challan_no": challan_no,
        "round_off": round_off,
        "updated_by": updated_by,
    })

    for l in lines:
        kg = float(l["accepted_weight"] or 0)
        rate_kg = round(float(l["rate"] or 0) / 100.0, 2)
        item_id = l["actual_item_id"]
        if item_id:
            item_id = _ensure_item(conn, int(item_id), src_co_id, updated_by)
        qty = l["actual_qty"] or ""
        unit = l["unit_conversion"] or ""
        dtl_id = DatabaseConnection.execute_insert_returning_id(conn, """
            INSERT INTO sales_invoice_dtl (
                invoice_id, item_id, hsn_code, quantity, sales_weight,
                uom_id, rate, amount_without_tax, total_amount, remarks
            ) VALUES (
                :invoice_id, :item_id, NULL, :kg, :kg,
                163, :rate, :amount, :amount, :remarks
            )
        """, {
            "invoice_id": invoice_id,
            "item_id": item_id,
            "kg": kg,
            "rate": rate_kg,
            "amount": float(l["total_price"] or 0),
            "remarks": f"Raw Jute - {qty} {unit}".strip(),
        })
        try:
            qty_unit_conv = int(float(l["actual_qty"] or 0))
        except (TypeError, ValueError):
            qty_unit_conv = 0
        conn.execute(text("""
            INSERT INTO sales_invoice_jute_dtl (
                invoice_line_item_id, claim_desc, claim_rate,
                claim_amount_dtl, unit_conversion, qty_untit_conversion
            ) VALUES (:dtl_id, NULL, 0, 0, :unit, :qty)
        """), {"dtl_id": dtl_id, "unit": l["unit_conversion"],
               "qty": qty_unit_conv})

    conn.execute(text("""
        INSERT INTO sales_invoice_jute (
            invoice_id, mr_no, mr_id, mukam_id, claim_amount, unit_conversion
        ) VALUES (:invoice_id, :mr_no, :mr_id, :mukam_id, 0, :unit)
    """), {
        "invoice_id": invoice_id,
        "mr_no": str(src["branch_mr_no"] or ""),
        "mr_id": child_mr_id,
        "mukam_id": src["mukam_id"],
        "unit": src["unit_conversion"],
    })

    return {
        "invoice_id": invoice_id,
        "invoice_no": invoice_no,
        "invoice_no_formatted": invoice_no_formatted,
        "invoice_amount": invoice_amount,
    }
```

- [ ] **Step 4: Wire into `save_marked_batch`**

In `save_marked_batch`, replace the group-closing lines (currently:
`_recompute_mr_header(conn, src_mr_id, updated_by)` /
`_recompute_mr_header(conn, child_mr_id, updated_by)` /
`child_ids.append(child_mr_id)`) with:

```python
            _recompute_mr_header(conn, src_mr_id, updated_by)
            _recompute_mr_header(conn, child_mr_id, updated_by)

            # Seller-side inter-company sale (owner amendment 2026-08-03):
            # the source branch bills the target company for this child MR.
            buyer_party_id, buyer_party_branch_id = _ensure_company_as_party(
                conn, target_co_id, target_branch_id, src_co_id, updated_by
            )
            invoice = _create_marked_sales_invoice(
                conn, child_mr_id, src_mr_id, src_co_id, src_branch_id,
                buyer_party_id, buyer_party_branch_id, mr_date, updated_by,
            )
            conn.execute(text("""
                UPDATE jute_mr
                SET invoice_no = :ino, invoice_date = :idate,
                    invoice_amount = :iamt, updated_date_time = NOW()
                WHERE jute_mr_id = :id
            """), {"ino": invoice["invoice_no_formatted"],
                   "idate": mr_date,
                   "iamt": invoice["invoice_amount"],
                   "id": child_mr_id})
            child_ids.append({
                "child_mr_id": child_mr_id,
                "invoice_no": invoice["invoice_no_formatted"],
                "invoice_amount": invoice["invoice_amount"],
            })
```

Update the `save_marked_batch` docstring: replace `Returns the created child MR ids.` with:

```
    Also creates one seller-side Raw-Jute sales invoice per child MR at the
    source branch (buyer = target company, auto-created in the source's
    party_mst if missing) and stamps the child MR with the formatted invoice
    no/date/amount. Returns a list of dicts:
    {"child_mr_id": int, "invoice_no": str, "invoice_amount": float}.
```

- [ ] **Step 5: Cascade-delete the invoice in `delete_marked_move`**

In `delete_marked_move`, immediately after the `issued` guard block (the `raise ValueError("This marked MR has ERP issue entries ...")` statement's enclosing `if`) and before `source_mr_id = c["src_jute_mr_id"]`, insert:

```python
        # Cascade-delete the seller invoice created at transfer time
        # (sales_invoice_jute.mr_id = child MR id; absent on pre-amendment
        # rows, so this is a no-op for legacy marked MRs).
        inv_rows = conn.execute(text(
            "SELECT invoice_id FROM sales_invoice_jute WHERE mr_id = :id"
        ), {"id": child_mr_id}).fetchall()
        for (inv_id,) in inv_rows:
            conn.execute(text("""
                DELETE sijd FROM sales_invoice_jute_dtl sijd
                JOIN sales_invoice_dtl sid
                  ON sid.invoice_line_item_id = sijd.invoice_line_item_id
                WHERE sid.invoice_id = :id
            """), {"id": inv_id})
            conn.execute(text(
                "DELETE FROM sales_invoice_jute WHERE invoice_id = :id"
            ), {"id": inv_id})
            conn.execute(text(
                "DELETE FROM sales_invoice_dtl WHERE invoice_id = :id"
            ), {"id": inv_id})
            conn.execute(text(
                "DELETE FROM sales_invoice WHERE invoice_id = :id"
            ), {"id": inv_id})
```

Also update `delete_marked_move`'s docstring first paragraph to: `Reverse a marked move: delete the linked seller invoice, return the child's weight to the matching source line(s), recompute the source header(s), and delete the child MR + line items.`

- [ ] **Step 6: Verify**

Run:
```bash
python -c "from src.jutetransfer import warehouse_stock_ops, lot_ops, transfer; from src.jutetransfer.pages import warehouse_stock; print('OK')"
python -m src.jutetransfer.warehouse_stock_ops
python -m pytest tests/ -q
```
Expected: `OK`, self-check passes, all 32 tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/jutetransfer/warehouse_stock_ops.py
git commit -m "feat(warehouse): create seller sales invoice per marked child MR; cascade delete on undo"
```

---

### Task 2: Page message + docs sweep

**Files:**
- Modify: `src/jutetransfer/pages/warehouse_stock.py` (transfer tab caption + success message)
- Modify: `CLAUDE.md` (Type 2 table + core-logic bullets)
- Modify: `docs/TRANSFER_PROCESS_UNDERSTANDING.md` (Type 2 "no invoice" claims)

**Interfaces:**
- Consumes: `save_marked_batch(...) -> list[dict]` with keys `child_mr_id`, `invoice_no`, `invoice_amount` (Task 1).

- [ ] **Step 1: Update the transfer tab in `pages/warehouse_stock.py`**

Replace:
```python
    n_src_mrs = prev["jute_mr_id"].nunique()
    st.caption(f"Will create {n_src_mrs} MR(s) at the target (one per source MR).")
```
with:
```python
    n_src_mrs = prev["jute_mr_id"].nunique()
    st.caption(
        f"Will create {n_src_mrs} MR(s) at the target and {n_src_mrs} seller "
        f"invoice(s) at the source (one per source MR)."
    )
```

Replace:
```python
            child_ids = save_marked_batch(
                li_ids, float(pct), int(tgt_co), int(tgt_br), int(wh_id),
                move_date, user_id,
            )
            st.success(f"Transferred. Created MR(s): {', '.join(map(str, child_ids))}")
```
with:
```python
            created = save_marked_batch(
                li_ids, float(pct), int(tgt_co), int(tgt_br), int(wh_id),
                move_date, user_id,
            )
            st.success(
                "Transferred. " + "; ".join(
                    f"MR {c['child_mr_id']} — invoice {c['invoice_no']} "
                    f"({c['invoice_amount']:,.0f})"
                    for c in created
                )
            )
```

- [ ] **Step 2: Update `CLAUDE.md`**

In the Two Transfer Types table, Type 2 column:
- "Per hop" row: change ``Single child `jute_mr`+`jute_mr_li`; **no invoice, no chain, no return leg**`` to ``Single child `jute_mr`+`jute_mr_li` **+ seller Raw-Jute `sales_invoice` (`invoice_type=5`) at the source branch**; no chain, no return leg``.
- "Tracking" row: rephrase so the transfer itself books the inter-company sale (invoice created at transfer time); the *onward* sale/consumption at the target still happens in the ERP (issue entries reduce the stock-view balance).

In "Type 2 core logic (as built)": extend the batch bullet with: one seller invoice per child MR at the source branch (buyer = target company party, auto-created if missing), child MR stamped with invoice no/date/amount, `sales_invoice_jute.mr_id` = child MR id (deletion linkage — different semantics from Type 1), `delete_marked_move` cascades the invoice. Keep wording tight (2-3 lines).

- [ ] **Step 3: Update `docs/TRANSFER_PROCESS_UNDERSTANDING.md`**

Grep the file for `no invoice`, `No invoice`, `no sales invoice` (case-insensitive) claims about Type 2 and amend each to reflect the 2026-08-03 amendment (seller invoice per child MR at transfer time; undo deletes it; masters auto-populated). Add one short "Amendment 2026-08-03" paragraph where the Type 2 flow is described.

- [ ] **Step 4: Verify**

```bash
python -c "from src.jutetransfer.pages import warehouse_stock; print('OK')"
python -m pytest tests/ -q
```
Expected: `OK`, all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/jutetransfer/pages/warehouse_stock.py CLAUDE.md docs/TRANSFER_PROCESS_UNDERSTANDING.md
git commit -m "feat(warehouse): show seller invoice in transfer flow; document inter-company sale amendment"
```
