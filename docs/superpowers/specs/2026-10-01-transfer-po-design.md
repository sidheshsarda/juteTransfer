# Transfer POs for vertical-chain transfers — design

**Date:** 2026-10-01 (updated 2026-10-02)
**Requested by:** owner — "when the transfers happen for the MRs … we would also like the corresponding POs to transfer as well, the forwarding companies"; "the POs will need to be rounded to the closest 50"; "make a second screen to show the original POs made, and also the final PO made, such that we can track properly".
**Scope:** Type 1 vertical chains only (`transfer_mode = 0`). Marked-godown moves and lot split/merge are unchanged.

## Owner decisions (2026-10-01)

| # | Question | Answer |
|---|---|---|
| O1 | Which companies get a PO | The forwarding company gets a PO (first hop: on the original supplier); when the chain returns, the mill also gets a new **Final PO** on the forwarding company at the marked-up rate. **The mill's MR stays linked to its original ERP PO**, so ERP PO balances are not disturbed. |
| O2 | Grouping | **One PO per transferred lorry (MR)**, built from what was actually received on that lorry. |
| O3 | "Rounded to the closest 50" | The PO **rate per quintal** goes to the nearest ₹50. MRs and invoices keep their exact rates. |
| O4 | Marked-godown transfers | Not in scope. |
| O5 | Existing chains | Backfill by script after a dry-run, starting with a one-lorry pilot the owner checks in the ERP. |

Names used in every UI text: **Original PO** (the mill's ERP PO), **Forwarding PO**, **Final PO**, together **Transfer PO**; the page is **PO Tracker**; a chain not yet back at the mill is **Awaiting return**.

## Facts the design rests on (sls, verified 2026-10-01)

- Live chains are all `mill → forwarder → mill`: Limelight (br 87, `LCPL`) → Greeting Marketing (br 33, `GMPL`), Empire Jute (br 29, `EJM`/`F`) → Jagrati Trade Services (br 20, `JTSPL`). Every root has an ERP PO; every hop MR had `po_id` NULL.
- A `jute_po` has no `co_id` (company = `branch_mst.co_id`), no FK, unique key or trigger. PO number = `MAX(po_no)+1` per branch within the April–March FY of `po_date`; the printed number `{co_prefix}/{branch_prefix}/JPO/{YY-YY}/{po_no:05d}` is built at read time.
- A PO line has no weight: `quantity` is bales (150 kg) or loose units (48 kg); the ERP PO page re-derives weight from percentage × lorry capacity, or — when every percentage is NULL ("quantity mode") — from `quantity × 150|48`.
- The ERP settlement engine (`vowerp3be/src/juteProcurement/po_settlement.py`) only ever reopens a CLOSED PO whose `close_type` is `AUTO`; any other close type is left alone and shown as plain "Closed". A status-3 PO is offered at the buyer's QC page and listed in the Pending-PO / Outstanding-Position reports; a status-5 PO is not, and is read-only.
- ERP reports match MR lines to PO lines by item id, not by `jute_mr_li.jute_po_li_id`. Accounting reads `jute_po.credit_term` through `jute_mr.po_id` for a bill's due date; the Tally export prints the linked PO's number and date.
- `jute_mr.src_jute_mr_id` has no index: chain queries start from the hop rows and join the root by primary key.

## The two transfer POs

| | Forwarding PO (marker role `FORWARD`) | Final PO (role `FINAL`) |
|---|---|---|
| When | With every hop MR the app inserts | When the chain returns to the mill (final step) |
| Branch | the hop's | the root's |
| Party / supplier | the hop's party / the hop's jute supplier | the root's party after finalize (the last forwarder as a party of the mill) / the supplier that party is already mapped under in the mill's `jute_supp_party_map` (lookup only) |
| Built from | the hop's active lines with accepted weight, read back in the same transaction | the root's active lines after finalize |
| MR link | `jute_mr.po_id` + `jute_mr_li.jute_po_li_id` on the hop | none — the root keeps its original PO; found by its marker |
| Removed by | deleting that hop | un-finalize |

Rules (`po_helpers.py`):
- **Rate:** `ROUND_HALF_UP(rate / 50) × 50` in Decimal (12,914 → 12,900; 12,925 → 12,950).
- **Quantity mode:** `quantity = max(1, ROUND_HALF_UP(accepted kg / unit kg))`, unit = 150 (BALE) or 48 (LOOSE) from the MR's `unit_conversion` (else the original PO's, else LOOSE); line kg = quantity × unit; value = line kg / 100 × rate; header weight / value = sums. One PO line per MR line. So the stored figures equal what the ERP PO page shows.
- **Header:** `po_date` = the MR's `jute_mr_date`; `vehicle_quantity` 1; `vehicle_type_id` NULL (a transfer PO is one lorry's actual receipt; with a lorry type set the ERP page paints the weight red when it is > 5 % off that capacity); `channel_code` from the original PO; `credit_term` / `delivery_days` from the original PO **only on the first hop** (the PO on the outside supplier), NULL elsewhere; `remarks` = one line saying it is a transfer PO made by Jute Transfer (shown on the ERP form and print).
- **State:** `status_id = 5`, `close_type = 'TRANSFER'`, `closed_date` set, `closed_by` NULL (the app's demo user id is not an sls user; the ERP would show "User #1"), `close_remark` explains the PO in words (lorry GE no and date, mill, original PO number, "Do not reopen"), plus one `jute_po_status_log` row `(old 3 → new 5, 'TRANSFER', source 'BACKEND')` so a manual ERP reopen lands on Approved, not Open. Lines: `percentage` NULL, `active` 1, `status_id` 21.
- **Masters:** on the FIRST hop the exact (company, supplier, party) row of `jute_supp_party_map` is inserted at the forwarding company when missing (the ERP PO form resolves the party only through it). A later hop (party = a sister company) takes the supplier that sister company is already mapped under there, as the Final PO does, and never adds a map row. No other master row is created. Crop years go in as the two-digit start year (2026 → 26), as the ERP stores them.

## Provenance marker (no schema change)

`jute_po.internal_note`:
```
JT|FORWARD|root=<root MR>|mr=<hop MR>|srcpo=<original PO or 0>|
JT|FINAL|root=<root MR>|mr=<root MR>|srcpo=<original PO or 0>|orig=<party>/<party branch>/<yyyy-mm-dd>|
```
The marker — not the close type — identifies an app-created PO (an ERP reopen clears the close columns but keeps the note). The FINAL marker's `orig=` field remembers the mill MR's party, party branch and MR date from before finalize (empty = NULL), so un-finalize can put them back.

## Lifecycle (`transfer.py`, `po_ops.py`)

- `save_transfer_step` locks the root row first; refuses a first step when the root already has a chain, any later step when the root is already finalized or when the step does not continue the chain's newest hop (stale tab / double save); creates the Forwarding PO after the hop MR, the Final PO after finalize — all in the step's one transaction. Returns `{mr_id, invoice_id, po}`.
- Every DELETE (and every line UPDATE) on the delete paths goes by primary key after a plain read (`database.select_ids` / `delete_by_ids`): `jute_po_li.jute_po_id` and `sales_invoice_jute_dtl` have no index, and a filtered DELETE would lock the whole table under REPEATABLE READ.
- A transfer PO is deleted only by `po_ops.delete_transfer_po(po_id, role, mr_id)`: its marker must name that role and MR and no other MR may be linked; a hop's PO is found through `jute_mr.po_id` and, if the ERP re-pointed the hop, through its marker. A Final PO is created only for a root that is actually finalized (status 3 with its MR number).
- `delete_chain_from_step` runs reads, guards and deletes in **one** transaction with the root locked; all issue-entry guards run before the first DELETE; a finalized chain is always reverted (also when deleting from step 1). Returns what it removed.
- Deleting a hop removes its invoices (all four `sales_invoice*` tables) and its Forwarding PO; un-finalize removes the Final PO. A transfer PO is only deleted if its marker names that very MR, and never while another MR is linked to it (the delete refuses and changes nothing).
- `po_ops` is two-phase: `plan_*` only reads (the backfill dry-run uses it), `create_*` = plan + insert.
- Kill switch: env `JT_TRANSFER_PO=0` stops PO creation (deletes still run); both pages show a banner when it is off.

## Bug fixes that ride along (owner OK 2026-10-02)

- **Finalize** writes the mill MR's money exactly as the ERP's approve would (`transfer._erp_recompute_money`, a mirror of vowerp3be `recompute_mr_money` + `calculate_tds_amount` + `compute_jute_totals`): line totals to the paisa, total, claim from `claim_rate`, 194Q TDS (0.1 % above ₹50 lakh of the party's approved MRs in the FY), roundoff, net to the rupee. It used to leave claim and net NULL, so the P&L counted those purchases as 0. Un-finalize recomputes the same way with TDS 0 (a Pending hand-off).
- **Posted rates** use exact Decimal arithmetic (`jute_mr_chain_helpers.hop_rate`), equal to what the screen showed (13,100 at 0.5 % posts 13,166, not 13,165).
- **Un-finalize** puts back the mill MR's party, party branch and MR date (exactly from the Final PO, else the party derived by name from step 1 and the Pending MR date NULL); it used to leave the forwarder as party, and a restarted chain then copied the sister company as the jute supplier. It also restores line totals in paise (the ERP's own `ROUND(accepted / 100 × rate, 2)`), not whole rupees; the header's money columns are recomputed from the restored lines (total, roundoff, the lines' claim, net) rather than returned to the hand-off's NULLs.
- **"Save godown tags"** only writes godowns whose tag changes and restores the jute type `J` on untag; it used to blank the ERP type of every godown of the branch. Store godowns (`S`) are no longer offered.
- "Delete from step 1" on a finalized chain now un-finalizes the mill MR; deleting an invoice also removes its `sales_invoice_jute_dtl` rows; a hop's previous MR is found by id order, not by "another branch".

## Screens

- **Transfer Chain:** the chain header shows the Original PO; each saved step shows its Forwarding / Final PO (number, date, kg, value), or "not created" for chains saved before the feature; save / delete results survive the page refresh; the hand-typed "PO No. for LC" is now "LC order ref".
- **PO Tracker** (`pages/po_tracker.py`, read-only): one row per transferred lorry (Original PO · Forwarding PO · Final PO · status), an Original-POs view, drill-down with a line-level reconciliation (MR kg / PO kg, MR rate / PO rate, the difference split into "whole bales" and "rate to nearest 50"), search, CSV.

## Existing chains and data repairs

**Rollout order — the staff's copy of the app is not this server.** The app the sls staff use every day runs on another host (`srv1836306.hstgr.cloud`) from the committed code; `claudehost` only serves the owner's preview. Until that copy has this code, it deletes and re-saves chains without removing their transfer POs (orphans, a stale Final PO after an un-finalize), blanks godown types again on every "Save godown tags", and leaves claim / net NULL on every finalize. Therefore: (1) pilot checked by the owner; (2) commit, PRs `claudehost` → `stock-transfer` → `main`, deploy on the staff's server with PO creation still OFF (the new delete paths remove transfer POs whatever the switch says); (3) the backfill, at a time when nobody raises POs at the mill and nobody saves chain steps; (4) PO creation ON (`JT_TRANSFER_PO=1` there, or the default in `po_ops._ENABLED_DEFAULT`); (5) the data repairs.

- `scripts/backfill_transfer_pos.py` — dry-run by default (per-company counts, number ranges, totals, exceptions with the supplier-party map rows it would add, sample POs, how to undo). Writes only with `--apply`, ONE scope (`--all`, `--root N`, or `--limit N` = the next N lorries per branch that still need a PO) and `--expect N` = the count the dry-run showed. Oldest lorry first per branch, one transaction per PO with the chain root locked and the PO planned again under that lock. The JSON log holds the plan before the first write and every PO as it is committed. The run STOPS at the first failed PO and at the first PO whose number turns out to be used twice (the ERP and the app both number `MAX+1` without a lock and there is no unique key), so no later lorry takes a number before an earlier one; a PO the fresh plan no longer wants is reported with its reason. After the run a read-only check lists hops / finalized roots still without a PO, numbers used twice and transfer POs whose MR is gone or no longer theirs; exit code 0 only when everything planned was created and that check is clean. A `--root` / `--limit` run that would jump older lorries of the same branch warns. `--undo <log> [--apply --expect N]` deletes exactly the logged PO ids (supplier-party map rows stay) and writes its own record. Pilot (applied 2026-10-02): `--apply --root 28052 --expect 2`.
- `scripts/repair_transfer_data.py` — dry-run by default; repairs rows the fixed bugs damaged; writes only with `--apply --only <repair> --expect N` and the owner's OK. An apply run prints every row it is about to write and saves the whole plan (old and new values) in its JSON log before the first write; each row is its own transaction, re-checked against the plan.
  - `finalized-net` (21 finalized roots with NULL claim / net): the ERP money rule incl. 194Q TDS, counted in MR-date order (then MR number): only what the party had been paid BEFORE a lorry is in its ₹50 lakh count (`_erp_money(cumulative_previous=…)`), so the first ₹50 lakh carry no TDS — ₹25,474 in total, where recomputing all 21 against each other today gives ₹30,474. The planned TDS is written as planned. The rows keep their `updated_by` / `updated_date_time` (the only record of when each lorry was finalized). Stops at the first row that fails. Known difference: the ERP's Bill Pass save derives TDS again from ALL the party's approved MRs of the year, so a Bill Pass save on one of the first five lorries raises its TDS to the full 0.1 % (+₹4,999.99 over the five) — an ERP rule, the owner's call.
  - `unfinalized-party` (root 28173): only Pending chain roots whose party is one of the group's own companies; puts back the party derived from step 1 and the Pending hand-off MR date (NULL).
  - `godowns` (104): godowns without a type whose last real type change in the ERP's audit log is J → NULL; back to 'J'. The old code blanks them again on its next "Save godown tags" — run after the staff's copy is updated.

## ERP-visible effects

- Forwarding companies get one Closed PO per transferred lorry (numbers from 00001); mills get one Closed Final PO per returned lorry, in the mill's own PO series, shown with supplier "others" and 0 lorries received (the lorry stays on the original PO).
- Hop MRs show a PO number / date in the ERP (MR list, page, print, bill pass, Tally Order No / Date); a hop's bill due date follows the original PO's credit days.
- PO weight is in whole bales / loose units (≤ 75 kg per BALE line from the MR weight); PO rate differs from the bill by up to ₹25 per quintal.

## Open risks

1. `'TRANSFER'` is inert in the settlement engine by fall-through, not by contract — add it to `STICKY_CLOSE_TYPES` in vowerp3be when convenient.
2. PO numbering shares the ERP's unlocked `MAX+1`; a simultaneous ERP PO on the same branch could take the same number.
3. A user with ERP edit access can reopen a transfer PO (→ Approved, pickable at QC). The marker still identifies it.
