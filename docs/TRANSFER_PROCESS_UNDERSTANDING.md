# Jute Transfer — Process Understanding

**Date:** 2026-07-23
**Status:** DRAFT — written to confirm understanding BEFORE any coding. Corrections go here first.
**Sources:** full read of juteTransfer repo, vowerp3be `src/juteProcurement/` + `src/models/jute.py`, vowerp3ui jutePurchase/sales pages, and a read-only probe of the live `sls` database.

---

## 1. System Landscape

Three repos work in tandem:

| Repo | Role | Editable from this workspace? |
|------|------|-------------------------------|
| `vowerp3be` | Multi-tenant ERP backend (FastAPI). Owns jute procurement: PO → Gate Entry → Material Inspection → MR → Bill Pass. | **NO — context only** |
| `vowerp3ui` | ERP frontend (Next.js). Portal pages for the above chain + sales (Sales Order / Sales Invoice / Jute Tally). | **NO — context only** |
| `juteTransfer` | Standalone Streamlit app. sls-tenant-only customization for **internal transfers between sls's own companies** (procurement-related). | **YES** |

**Scope rule for all work here: edits touch only the juteTransfer repo and the `sls` tenant database. Never other tenants, never vowerp3be/vowerp3ui code, never `vowconsole3`.**

### How juteTransfer connects

- Streamlit app with its own `.env` → direct MySQL connection to database **`sls`** (same host as the VoWERP tenant DBs). It bypasses the VoWERP API, JWT auth, and tenant routing entirely — raw SQL reads/writes into the shared tenant DB.
- Its own auth is a hardcoded demo login (`auth.py`); `updated_by` is effectively always `1`.
- The `sls` tenant genuinely hosts many companies: `co_mst` has 88 companies, `branch_mst` 106 branches — the multi-company premise of the app is structurally real.

### The shared seam (tables both systems touch)

- **`jute_mr` / `jute_mr_li`** — the merged Gate-Entry+MR table. Created and driven by VoWERP (gate entry creates the row, status `1` = IN; inspection fills item/warehouse/marka/rate; MR approval → status `3`; bill pass stamps `bill_pass_no` directly on the row — no separate bill-pass table).
- **`sales_invoice` + `sales_invoice_dtl` + `sales_invoice_jute` + `sales_invoice_jute_dtl`** — juteTransfer inserts one Raw-Jute invoice (`invoice_type=5`) per chain hop; VoWERP sales reports and the Jute Tally Download page read the same tables. `sales_invoice_jute.mr_id` is the existing sale→MR linkage both systems share.
- **`warehouse_mst`** — per-branch godown master (802 rows). juteTransfer adds the tag `warehouse_type='MARKED'` for type-2 godowns.
- **Status semantics on `jute_mr`:** `1` Open, `3` Approved (eligible for transfers), `13` Pending — vowerp3be's own `constants.py` documents 13 as *"terminal state on MR screen, handed off to external system"* — i.e. the ERP explicitly hands off to juteTransfer; juteTransfer reads 13 on a chain root as "origin company is consuming this jute, chain closed".

### Columns that exist ONLY for juteTransfer

`jute_mr.src_jute_mr_id`, `jute_mr.transfer_mode` (and the write-path of `jute_mr.src_com_id`) exist in the live `sls` DB and in juteTransfer's models, but are **absent from vowerp3be's ORM (`src/models/jute.py`) and from `dbqueries/migrations/`**. vowerp3be only ever SELECTs `src_com_id`; it never writes any of the three. The single frontend trace is `MRHeaderForm.tsx:18` — if `src_com_id` is set, a read-only "Gate Entry Date" field is shown. Nothing else in the ERP knows juteTransfer exists.

**Consequence:** these columns must be treated as sls-only. Never assume they exist on other tenants; never add them to other tenants from this workspace.

---

## 2. Transfer Type 1 — Vertical Transfer Chain (BUILT, TESTED, WORKING)

Lives in `pages/new_transfer_chain.py` + `jute_mr_chain_helpers.py` (pure math) + `transfer.py` (DB writes). This is the sole chain-editing UI.

**Business shape:** jute is gate-entered at Company A (the root MR, created by VoWERP procurement — juteTransfer never creates roots). The physical jute never moves; *paper ownership* circulates A → B → C → … → back to A, with a % rate markup at each hop. When it returns to A, the user manually finalizes.

**Mechanics per hop (one DB transaction each, `save_transfer_step`):**

1. **Step 1 (A → B):** ensure the original supplier exists as a party in B's company, then INSERT a brand-new `jute_mr` + `jute_mr_li` set at B with `transfer_mode=0`, `src_jute_mr_id = root id` (**always the root — star topology, not a linked list of ids**), `src_com_id = A's co_id` (received-from company), `status_id=3`. No invoice on step 1.
2. **Intermediate hops (B → C, …):** ensure buyer exists as a party in seller's company; INSERT a Raw-Jute `sales_invoice` (+dtl +jute +jute_dtl, `invoice_type=5`) from seller to buyer at rate = previous saved rate × (1 + pct/100); INSERT the buyer's new `jute_mr` row carrying that invoice's no/date/amount.
3. **Final hop (back to A):** detected automatically when the buyer's company+branch equals the root's company+branch. **No new row** — the ORIGINAL root `jute_mr`/`jute_mr_li` rows are UPDATEd in place with the final rate, party, `branch_mr_no`, `bill_pass_no`, and the last invoice's fields. `revert_original_mr` undoes this (restores step-1 rates, clears bill-pass/invoice fields, status back to 0).
4. **Chain reconstruction** walks `src_com_id` as a received-from linked list starting at the root company (`get_transfer_chain` filters `src_jute_mr_id = root AND transfer_mode = 0`).
5. **Rate cascade:** multiplicative and cumulative, rounded at the kg level at each hop boundary. **Claims do NOT cascade at all** — every step's `claim_amount` is the flat sum of the original per-line claims (the old CLAUDE.md's "claim breaks" phrasing was misleading; there is no claim-break flag).
6. **Deletion:** deleting from a middle step cascades downstream deletes (MR + line items + both linked invoices per hop); deleting from the root reverts the root.

**P&L wiring (company_pl_dashboard.py):** Purchases = SUM(`jute_mr.net_total`) per owning company; Sales = SUM(invoice_amount − claim) of `invoice_type=5` invoices per seller branch; Unsold chain stock = transfer_mode=0 MRs at status 3 whose root isn't closed (root status NOT IN 1,13) and not yet invoiced forward; Adjusted P&L = (Sales − Purchases) + stock.

---

## 3. Transfer Type 2 — Marked Goods in a Designated Warehouse (PARTIALLY BUILT)

**What the user described:** transfers of *specific marked goods and qualities* which will live in a *specified warehouse*. The impact shows in P&L, but it does **not** need the hop-by-hop tracking of type 1. Once a sale is completed, the original source just needs to be mentioned in `src_mr_id`.

**What already exists in the repo** (`pages/warehouse_stock.py` + `warehouse_stock_ops.py` — deliberately disjoint from the chain, keyed by `jute_mr.transfer_mode`):

- Godowns can be tagged/untagged as MARKED (`warehouse_mst.warehouse_type='MARKED'`).
- `save_marked_move` moves a **partial quantity** of an existing purchased line into a marked godown at another company: reduces the source `jute_mr_li.accepted_weight`/`total_price` (recomputing the source header) and INSERTs one child `jute_mr` (+ one `jute_mr_li`) at the target branch/warehouse with `transfer_mode=1`, `src_jute_mr_id = the DIRECT PARENT MR` (note: different semantics from type 1, where it's always the chain root), possibly at a different rate. **No sales invoice, no chain, no return leg.**
- Mutual exclusion is enforced: a line already feeding a live vertical chain cannot be mark-moved (raises), and chain queries filter `transfer_mode=0` so marked rows never enter chains.
- P&L already counts marked stock: SUM(net_total) of `transfer_mode=1` MRs at status 3.

**The gap between "described" and "built" — CONFIRMED with the user (2026-07-23):**

1. The move itself exists and is the intended foundation. The remaining work is the **sale-completion step**: nothing today records "this marked stock was sold".
2. **Confirmed decisions:**
   - **`src_mr_id` = the existing `jute_mr.src_jute_mr_id` on the marked child MR, stamped at move time — already sufficient.** No new column, no stamping on the sale record. On sale completion the child just needs to be marked consumed.
   - **The sale is recorded in the main ERP portal** as a Raw-Jute Sales Invoice (`invoice_type=5`); **"sale completed" = that invoice reaching Approved (`status_id=3`)**. juteTransfer only reacts/backfills — it gets no sale-entry UI for marked stock.
   - **"Marked" keys on the MARKED-godown tag as built** (`warehouse_mst.warehouse_type='MARKED'`), not on item-level `marka`/quality attributes.
   - **Type 2 extends the existing `warehouse_stock` page/ops** — no fresh design.
3. Implementation implication (to be designed before coding): marked-stock consumption can be detected by joining `sales_invoice_jute.mr_id` → marked child `jute_mr_id` where the invoice is Approved (mirroring how `get_company_wise_unsold_stock` detects chain stock being invoiced forward), and/or by moving the consumed child to a closed status; `get_company_wise_marked_stock` must then exclude consumed rows so P&L stock drops when the sale completes.

---

## 4. Evidence-Based Findings (things worth knowing before coding)

1. **The sls DB currently has ZERO live transfer rows.** Across 24,954 `jute_mr` rows: `src_jute_mr_id` is populated 0 times and `transfer_mode` is 0 everywhere. The chain feature is code-complete and tested, but no chain (or marked move) currently exists in the live data. Either testing happened elsewhere / was cleaned up, or production use hasn't started. → confirm expectation.
2. **`src_com_id` is polluted by legacy data.** 10,846 of 24,954 rows have `src_com_id` set to small ints (0,1,2,3,4,6,7,8…) that do NOT match live `co_mst.co_id` values (1,2,3,25,26,27,45…) — almost certainly a carried-over code from the pre-VoWERP data migration (sls has ~70 leftover `staging_`/`_map_`/`tmp_` migration tables). Chain reconstruction is safe (it filters on `src_jute_mr_id = root` first), but (a) the ERP frontend shows its extra "Gate Entry Date" field on all those legacy rows, and (b) any future logic keying on `src_com_id` alone would misread them.
3. **Schema-authority gap:** `src_jute_mr_id`/`transfer_mode` live outside vowerp3be's ORM and migration history. Per the workspace rule they stay sls-only; this document is now their only cross-repo documentation.
4. **`marka` on `jute_mr_li` is real but nearly unused** (9 of 40,560 rows; values like "NO MARK", "PRAMOD"). "Marked goods" in type 2 is currently implemented as *marked godowns* (`warehouse_type='MARKED'`), not via the `marka` column. If the user's "specific marked goods and qualities" is meant to key on `marka`/quality item-level attributes rather than (or in addition to) the godown tag, that changes the type-2 design. → open question.
5. **`proc_transfer` / `proc_transfer_dtl`** exist in sls with from/to-warehouse columns but are completely empty — dormant generic-transfer scaffolding, unrelated; do not confuse with this feature.
6. Housekeeping in juteTransfer (not fixed yet, listed for later): live `[DEBUG]` caption in `new_transfer_chain.py:470`, `debug_transfer.log` file-append on every save, a stray `company_pl_dashboard.py.tmp.*` file, hardcoded demo auth writing `updated_by=1`, and three older docs referencing files that no longer exist (`jute_mr.py`/`jute_mr_editor.py`).

---

## 5. Decisions & Remaining Open Questions

**Answered by the user 2026-07-23** (recorded in §3.2): `src_mr_id` = existing `src_jute_mr_id` on the marked child MR, already stamped at move time — sufficient; sale = ERP Raw-Jute Sales Invoice reaching Approved (3), juteTransfer only reacts; "marked" = the MARKED-godown tag as built; type 2 extends the existing warehouse_stock page.

**Still open:**

1. **Zero live transfer rows in sls** — `src_jute_mr_id` populated 0/24,954, `transfer_mode` all 0. Expected at this stage (tested elsewhere / production use not started), or worth investigating?
2. ~~**Consumption mechanics detail**~~ — **CLOSED 2026-08-03**, see §6 below.

---

## 6. 2026-08-03: Lot management & batch transfer

Built on top of everything above. Two things landed: (a) a lot-management layer for type-2 stock, and (b) the resolution of open question 2 (how to detect that marked stock was sold).

### 6.1 Lot MRs — a controlled exception to "roots come only from VoWERP"

§1 established that `jute_mr` roots are always VoWERP gate-entry rows. **Lot MRs relax that**, deliberately: a lot MR is an app-created `jute_mr`/`jute_mr_li` row that re-allocates quantity out of one or more existing mode-0, status-3, same-branch lines — splitting a big line into smaller ones, or merging several lines into one — without moving anything to another company. Mechanically (`src/jutetransfer/lot_ops.py::create_lot`):

- Source lines are locked (`FOR UPDATE`, ascending id order), validated same-branch, `transfer_mode=0`, `status_id=3`, and not already feeding a live vertical chain (`src_jute_mr_id = <mr> AND transfer_mode = 0` guard, same shape as the type-1/type-2 mutual-exclusion check).
- Each source line's `accepted_weight`/`total_price`/`actual_weight`/`actual_qty` is reduced by the taken quantity; the source MR header is recomputed.
- One new `jute_mr` is inserted: `transfer_mode=0`, `status_id=3`, **`src_jute_mr_id` NULL, `src_com_id` NULL** — this is what makes it structurally a "root" to every downstream chain/marked-stock query, even though the app created it. Numbering (`jute_gate_entry_no`, `branch_mr_no`, `bill_pass_no`) reuses the same in-transaction helpers `transfer.py` uses for chain hops (`_get_next_gate_entry_no`, `_get_next_mr_number_in_txn`, `_get_next_bill_pass_no_in_txn`). Party/party-branch are copied from the **primary source MR** (`lot_helpers.primary_source_mr`: the source contributing the largest taken qty, ties broken by lowest `jute_mr_id`) — same company, so no party remap is needed.
- One `jute_mr_li` per take, with item/quality/warehouse copied from its source line, rate/price via `lot_helpers.line_price` (rate is per-quintal).
- **One `jute_lot_src` row per take** — the provenance record — capturing `new_jute_mr_li_id`, `src_jute_mr_li_id`, `qty_kg`, and the `actual_qty_delta`/`actual_weight_delta` moved (so `actual_*` fields, not just `accepted_weight`, stay reconcilable). This is the mechanism that keeps every app-created line traceable back to a real gate-entry ancestor despite the relaxed root rule.

`delete_lot` reverses this exactly from `jute_lot_src` (restores accepted **and** actual amounts on every source line), and blocks if: the lot has been consumed/transferred onward (`src_jute_mr_id` pointing at it), an ERP issue entry exists against it (`jute_issue.status_id <> 4`), or one of its lines has itself already fed a newer lot (`jute_lot_src.src_jute_mr_li_id` match) — delete newest-first.

`jute_lot_src` also covers **marked child lines** (type-2 moves), not just lot-MR lines — it is the general "how did this app-created line come to exist" ledger, exposed via the recursive-CTE query `get_lot_provenance(jute_mr_id)` for drill-down in the UI.

### 6.2 Whole-lot batch transfer

The warehouse page's Transfer tab (`save_marked_batch` in `warehouse_stock_ops.py`) moves **whole lots only** — if a partial quantity needs to move, split it into its own lot first via the Lots tab, then transfer that lot whole. Batch transfer lets the user multi-select several source MRs at once, applies one common `%` rate change (`lot_helpers.apply_pct`) across all of them, and creates **one mode-1 child MR per source MR** (not one merged child) — preserving per-source traceability the same way chain hops preserve per-hop traceability. Each child's `src_jute_mr_id` is stamped to its direct parent, matching the existing type-2 semantics documented in §3.

### 6.3 Sold-detection: closing open question 2

§3.3 flagged two options for detecting that marked stock had sold: joining `sales_invoice_jute.mr_id` to Approved Raw-Jute invoices, or flipping the child MR to a closed status. Before building either, `scripts/verify_sold_detection.py` (run via `python -m scripts.verify_sold_detection`) checked whether `mr_id`-based detection was even viable against real data:

- **`sales_invoice_jute.mr_id` is unusable as a detection key**: 0 of 2,980 active `invoice_type=5` (Raw-Jute) invoice rows in FY2025-26 carried a usable link. The ERP UI only exposes a free-text `mrNo` field on that screen — it was never wired to actually populate `mr_id`.
- **The owner ruled (2026-08-03)** to abandon invoice-linkage detection entirely and instead detect consumption from stock balance: the existing ERP view `vw_jute_stock_outstanding` (confirmed to exist and return real data — 39,230 rows, 34,574 with positive balance) gives `bal_weight = actual_weight − SUM(jute_issue.weight WHERE status_id <> 4)` per `jute_mr_li_id`, for MRs at status 3/13.
- **Resulting rule, as built:** a marked line's availability is `LEAST(COALESCE(bal_weight, accepted_weight), accepted_weight) > 0`; consumed = balance ≤ 0. This is automatic — no button, no status flip on the child MR, no new column. `get_marked_stock_with_balance` and `get_company_wise_marked_stock` (P&L) both revalue marked stock from the remaining balance rather than the original moved quantity, so P&L drops as ERP-side issues consume the stock.
- This same `vw_jute_stock_outstanding`-based availability check also gates lot-MR creation (`_available_kg` in `warehouse_stock_ops.py`, shared by `lot_ops.create_lot`) — a line partially issued in the ERP can't be over-allocated into a new lot or marked move.

**Open question 2 is closed**: consumption detection is balance-based via `vw_jute_stock_outstanding`, not invoice-linkage-based. No further design work needed here; `sales_invoice_jute.mr_id` should be treated as unreliable for any future feature in this app.
