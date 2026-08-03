# Warehouse Marked Page v2 — Lot Management, Batch Transfer, Auto Sold Detection

**Date:** 2026-08-03
**Status:** Approved by owner (design review 2026-08-03)
**Scope:** juteTransfer repo only, sls tenant DB only. No vowerp3be / vowerp3ui changes.

## 1. Problem

The current warehouse-marked page (`pages/warehouse_stock.py`) moves one line at a
time, shows no quality-wise availability, cannot batch-transfer several MRs/lots,
and cannot restructure lots (split TD4 6000 kg into 4000 + 2000 so the remainder
can join a different onward transfer). Sold marked stock never leaves the stock
list.

## 2. Core concept

- **A lot is a `jute_mr_li` row.**
- **Re-lotting creates new "lot MRs"** — app-created `jute_mr` rows at the same
  company/branch carrying re-allocated quantities, with line-level provenance in a
  new sls-only table. Source lines are reduced in place; total weight and value
  are conserved.
- Downstream flows (batch transfer, marked stock, P&L) operate on ordinary MRs —
  no special lot handling after a lot MR exists.
- Transfers move **whole lots only**. Partial quantity = split first in the Lots
  tab. (Replaces today's per-line partial-qty entry on the transfer form.)

### Constraint change

CLAUDE.md currently states "root MRs are created by VoWERP gate entry, never by
this app". Relaxed by owner decision to: *gate-entry roots* come only from
VoWERP; *lot MRs* are app-created, always carry `jute_lot_src` provenance rows,
and always have `src_jute_mr_id` NULL and `transfer_mode` 0. CLAUDE.md is updated
as part of this work.

## 3. Data model (sls only — one new table, zero column changes)

```sql
CREATE TABLE jute_lot_src (
    lot_src_id         BIGINT PRIMARY KEY AUTO_INCREMENT,
    new_jute_mr_li_id  BIGINT NOT NULL,   -- line in the app-created lot MR
    src_jute_mr_li_id  BIGINT NOT NULL,   -- source line (gate-entry MR or earlier lot MR)
    qty_kg             DECIMAL(12,3) NOT NULL,
    created_by         INT,
    created_date_time  DATETIME,
    KEY idx_lot_src_new (new_jute_mr_li_id),
    KEY idx_lot_src_src (src_jute_mr_li_id)
);
```

- Split: one `src_jute_mr_li_id` appears in many rows (feeds several lot lines).
- Cross-MR merge: one `new_jute_mr_li_id` appears in many rows (fed by several
  source lines).
- Tally/provenance query: walk `jute_lot_src` recursively (lot MRs can be re-lotted)
  back to gate-entry lines.
- An MR is a **lot MR** iff any of its lines appear as `new_jute_mr_li_id`.

Lot MR header: `transfer_mode = 0`, `status_id = 3`, `src_jute_mr_id` NULL,
`branch_id` = source branch, gate-entry/MR/bill-pass numbers via the existing
`_get_next_*_in_txn` helpers (same pattern as `save_marked_move`), party fields
copied from the source MR contributing the largest take qty (ties: lowest
`jute_mr_id`), totals recomputed from lines.

## 4. Page structure — 3 tabs (`st.tabs`)

Filters above the tabs: Company, Branch, Year, Month (as today).

### Tab 1 — Lots

1. **Availability summary** (quality-wise): quality | lot count | total kg |
   weighted-avg rate. Eligibility = `transfer_mode = 0`, `status_id = 3`,
   `accepted_weight > 0`, not part of a live vertical chain.
2. **Lot grid** (st-aggrid, multi-select): MR no, MR date, quality, remaining kg,
   rate, warehouse, supplier/party, lot-MR badge (provenance exists).
3. **Create new lot**: select source lines, enter qty to take from each (default
   full line). One click → single transaction:
   - Reduce each source line's `accepted_weight`/`total_price`; recompute each
     source header (`_recompute_mr_header`).
   - Insert one new lot MR + one line per (source line, qty) take, carrying the
     source line's rate, quality, marka, crop year, warehouse.
   - Insert one `jute_lot_src` row per take.
4. **Delete lot MR** (undo): allowed only if no child MR references it
   (`src_jute_mr_id` = lot MR id, any mode) and none of its lines feed a newer
   lot. Restores every source line exactly from `jute_lot_src` (no quality
   matching), recomputes source headers, deletes provenance rows, lines, header.

Guards for create: every source line's MR must be `transfer_mode = 0`,
`status_id = 3`, not a live-chain MR (reuse the `chain_child` check from
`save_marked_move`); take qty in (0, remaining]; all rows locked `FOR UPDATE`.

### Tab 2 — Transfer

1. Same eligible-lot grid, multi-select N lots across any MRs of the filtered
   company/branch.
2. One shared target: company/branch (from `get_company_branch_options`), marked
   godown (`get_marked_warehouses_by_branch`), date, and **one common % rate
   change** (positive or negative float; `new_rate = round(rate * (1 + pct/100), 2)`).
3. **Preview grid** before save: per lot — quality, kg, old rate, new rate, old
   value, new value; totals row.
4. **Save** — single transaction, whole-lot moves:
   - Group selected lines by source `jute_mr_id`.
   - Per source MR: create **one child MR** (`transfer_mode = 1`, `status_id = 3`,
     `src_jute_mr_id` = source MR id) with one line per selected lot at the new
     rate — the multi-line generalisation of `save_marked_move` (party via
     `_ensure_company_as_party`, item remap via `_ensure_item`).
   - Source lines go to `accepted_weight = 0`, `total_price = 0`; headers
     recomputed. Zero-weight lines remain as audit trail and disappear from all
     availability views (kg > 0 filter).
5. Existing guards keep working: cannot move from mode-1 stock, cannot move a
   line of a live-chain MR.

### Tab 3 — Marked Stock

1. Grid of mode-1 MRs for the filtered company/branch: MR, quality, kg, rate,
   value, godown, source MR (via `src_jute_mr_id`) with provenance drill-down,
   and **Sold** status.
2. **Balance-based consumption detection** (owner ruling 2026-08-03, replaces
   the original invoice-join design): stock truth is the ERP's own
   `vw_jute_stock_outstanding` view (verified present at sls, dev3 definition:
   `bal_weight = actual_weight − SUM(jute_issue.weight WHERE status_id <> 4)`
   per `jute_mr_li_id`, MRs status 3/13). A marked line is *consumed* when its
   view balance ≤ 0; partially consumed lines show their remaining balance. A
   marked MR leaves the stock list when no line has balance > 0. No manual
   button, no status flip. Rationale: the ERP invoice UI never populates
   `sales_invoice_jute.mr_id` (0/2,980 rows in 2025-26 — Task 1 verification),
   while `jute_issue` is line-linked on 100% of rows; at sls, jute leaves
   stock via issue entries, covering both production use and sale.
   - Fully consumed MRs: greyed/badged, excluded from availability, and
     excluded from `get_company_wise_marked_stock` (P&L values remaining
     balance, not header net_total).
3. **Delete marked move**: keep `delete_marked_move`; additionally block
   deletion when any `jute_issue` row (status_id <> 4) references one of the
   child's lines — consumption has started, reversal would corrupt the ERP
   issue trail.
4. Godown tagging expander moves here unchanged.

## 5. Queries (new/changed in `queries.py`)

- `get_available_lots(co_id, branch_id, year, month)` — replaces the page's use
  of `get_jute_mr_with_line_items`: proper `status_id = 3` filter, kg > 0,
  chain exclusion, lot-MR badge column, correct status semantics (1 Open /
  3 Approved / 13 Pending). **Available kg per line =
  `LEAST(COALESCE(v.bal_weight, li.accepted_weight), li.accepted_weight)`**
  via LEFT JOIN `vw_jute_stock_outstanding v ON v.jute_mr_li_id =
  li.jute_mr_li_id` — production-issued weight is not movable.
  `get_jute_mr_with_line_items` itself is left untouched for other callers.
- `get_quality_availability_summary(co_id, branch_id, year, month)` — the Tab 1
  aggregate.
- `get_marked_stock_with_balance(co_id, branch_id, year, month)` — Tab 3 grid:
  per-line original kg, remaining balance kg (view join), consumed flag
  (balance ≤ 0), value = remaining balance × rate / 100, source MR join.
- `get_lot_provenance(jute_mr_id)` — recursive walk of `jute_lot_src` to
  gate-entry origins (MySQL 8 recursive CTE).
- `get_company_wise_marked_stock` — revalue from remaining balance:
  `SUM(GREATEST(COALESCE(v.bal_weight, li.accepted_weight), 0) * li.rate / 100)`
  over mode-1 status-3 MR lines (replaces header `net_total` sum, so partially
  and fully consumed marked stock drops out of P&L correctly).

## 6. Ops (new module functions)

New sibling module `lot_ops.py` (keeps `warehouse_stock_ops.py` focused on
marked moves):

- `lot_ops.create_lot(takes: list[(src_jute_mr_li_id, qty)], mr_date, updated_by) -> int`
- `lot_ops.delete_lot(lot_mr_id, updated_by) -> None`

In `warehouse_stock_ops.py`:

- `save_marked_batch(lot_li_ids: list[int], pct_change: float, target_co_id,
  target_branch_id, warehouse_id, mr_date, updated_by) -> list[int]`
  (returns created child MR ids; supersedes single `save_marked_move` on the
  page — the old function stays for compatibility/tests)
- `delete_marked_move` — add sold-invoice block.

All writes inside `DatabaseConnection.get_transaction()`, `FOR UPDATE` locks on
every touched line.

**Actual-weight maintenance (required by the balance mechanism):** the ERP view
computes balances from `actual_weight`/`actual_qty`, so every line this app
creates or reduces must keep those in step:

- Lines created (lot lines, marked child lines): `actual_weight` = moved kg,
  `actual_rate` = line rate, `actual_qty` = source `actual_qty` ×
  (take ÷ available kg shown), rounded 3 dp (Float column — fractional OK).
- Source reductions: `accepted_weight` −= take; `actual_weight` = GREATEST(0,
  actual_weight − take); `actual_qty` reduced by the same proportion, 3 dp.
- Restores (delete lot / delete marked move): add the same amounts back.
- The legacy `save_marked_move` is NOT retrofitted (page no longer calls it).

Pure-python allocation math (take validation, weight conservation, rate rounding)
lives in a DB-free helper section/module mirroring `split_weights`, unit-tested.

## 7. Verification task — RESOLVED 2026-08-03

Verification ran (`scripts/verify_sold_detection.py`, commit 06c9d65) and
**tripped the stop gate**: `sales_invoice_jute.mr_id` is 0/2,980 on 2025-26
invoices (the ERP invoice UI has only a free-text MR No field; `mrId` is never
sent). Owner ruled: use the ERP's own stock-balance mechanism instead —
`vw_jute_stock_outstanding` (verified present at sls; `jute_issue` is
dev3-style, `jute_mr_li_id` set on 47,795/47,795 rows). Consumption = view
balance ≤ 0 per line. Full probe: `.superpowers/sdd/…/db-probe-stock-view.md`
(session artifact) and `docs/TRANSFER_PROCESS_UNDERSTANDING.md` addendum.

## 8. Edge cases

- Take = full remaining weight → source line hits 0 kg, stays as audit row.
- Same-quality duplicate lines: never matched by quality — lot ops use exact
  line ids via `jute_lot_src`; legacy `delete_marked_move` quality-matching
  remains only for pre-existing marked MRs.
- Rounding: kg at 3 dp (`split_weights` convention), money at 2 dp, header
  totals via `_recompute_mr_header`. Weight conservation asserted in helpers.
- Concurrent operators: `FOR UPDATE` on all source lines before mutation
  (deadlock-safe ordering: lock lines in ascending `jute_mr_li_id`).
- Re-lotting a lot MR is allowed (recursive provenance).
- A lot MR with some lines transferred (mode-1 child exists) cannot be deleted.
- Empty marked-godown list at target → save disabled (as today).

## 9. Testing

- Unit (pytest, no DB): allocation validation, weight conservation across
  split/merge, % rate change rounding, provenance-walk assembly from rows.
- Ops self-check (`__main__`) style guards preserved.
- Integration checklist: create lot (split 6000→4000+2000), cross-MR merge into
  one lot, batch transfer 6 lots from 4 MRs → 4 child MRs with common %, undo
  lot, undo marked move, sold invoice hides lot + drops P&L marked stock,
  chain-MR line blocked from lot/transfer, mode-1 line blocked as lot source.

## 10. Out of scope

- Manual mark-consumed button (owner chose auto-only; balance mechanism).
- Re-transfer of marked (mode-1) stock onward (stays terminal until consumed).
- Any change to vertical-chain (Type 1) logic or pages.
- Retrofitting `actual_weight` maintenance onto legacy `save_marked_move`.
