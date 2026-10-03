# JuteTransfer Development Guide

## Project Overview

**JuteTransfer** is a standalone Streamlit application — a customization built **only for the `sls` tenant** of the VoWERP3 ERP — that manages internal jute transfers between sls's own companies. It replaces an Excel-based workflow.

**Tech Stack:**
- Frontend: Streamlit + streamlit-aggrid
- Backend: Python 3.12+ with SQLAlchemy (raw SQL via `text()`), mysql-connector
- Database: MySQL — connects **directly** to the `sls` tenant DB (credentials in `.env`)
- Auth: local demo auth in `src.jutetransfer.auth` (not connected to VoWERP JWT)

## Ecosystem & Scope (READ FIRST)

Three sibling repos work in tandem:

| Repo | Role | Editable here? |
|------|------|----------------|
| `../vowerp3be` | ERP backend (FastAPI) — owns jute procurement (PO → Gate Entry → MI → MR → Bill Pass) | **NO — context only** |
| `../vowerp3ui` | ERP frontend (Next.js) — portal pages incl. jute purchase + sales | **NO — context only** |
| `juteTransfer` (this repo) | sls-only inter-company transfer app | **YES** |

**Hard scope rule: changes are made only in this repo and only against the `sls` tenant database. Never touch other tenants, vowconsole3, or the vowerp3be/vowerp3ui codebases.**

Full process explanation + evidence: **`docs/TRANSFER_PROCESS_UNDERSTANDING.md`** (canonical companion to this file).

### Database integration facts

- This app bypasses the VoWERP API/auth/tenancy entirely — raw SQL reads/writes into the shared `sls` DB.
- Shared tables: `jute_mr`, `jute_mr_li`, `sales_invoice(+_dtl)`, `sales_invoice_jute(+_dtl)`, `warehouse_mst`, `co_mst`, `branch_mst`, `party_mst`, `party_branch_mst`, `item_mst`, `item_grp_mst`, `status_mst`.
- **juteTransfer-only columns** on `jute_mr`: `src_jute_mr_id`, `transfer_mode` (and the write-path of `src_com_id`). They exist in the sls DB but are absent from vowerp3be's ORM and migrations — treat them as **sls-only**; never assume them on other tenants.
- **juteTransfer-only table** `jute_lot_src` (`lot_src_id, new_jute_mr_li_id, src_jute_mr_li_id, qty_kg, actual_qty_delta, actual_weight_delta, created_by, created_date_time`) — line-level provenance for every app-created line (in-place lot lines, legacy lot-MR lines, and marked child lines), sls-only, created by `scripts/migrate_jute_lot_src.py`.
- `jute_mr.status_id` semantics (owner ruling 2026-09-03, decisions D1/D3): `1` Open, `13` Pending = the ERP hand-off state — **a NEW chain may only start from a root at 13** (`save_transfer_step` preflight; the overview grid shows Status inline and lists Approved rows for reference only), `3` Approved (finalize writes 3 on the root), `48` Returned (lorry sent back, never transferable). Un-finalize and delete-whole-chain both return the root to **13** (never 0, never 1). The ERP freezes a root and refuses Reject/Cancel/Reopen while chain children exist (`src_jute_mr_id` points at it) — roll back here first.
- Gate-entry root MRs are created by VoWERP, never by this app — and since 2026-08-04 the app creates **no mode-0 MRs at all**: lot split/merge edits `jute_mr_li` lines IN PLACE inside existing MRs, traceable via `jute_lot_src`. Legacy app-created **lot MRs** (pre-2026-08-04: `transfer_mode=0`, `status_id=3`, `src_jute_mr_id` NULL) may still exist in the sls DB; their lines carry provenance and per-line undo removes the empty header when the last line goes.
- Legacy caveat: ~10.8k historic `jute_mr` rows carry migration-era `src_com_id` values that do NOT match live `co_mst.co_id` — never key logic on `src_com_id` alone; chain queries must keep filtering on `src_jute_mr_id`.

## The Two Transfer Types

Both live on `jute_mr` and are kept disjoint by `jute_mr.transfer_mode`:

| | **Type 1 — Vertical Transfer Chain** | **Type 2 — Marked Warehouse Stock** |
|---|---|---|
| `transfer_mode` | 0 | 1 |
| Status | **Built, tested, working** | **Built:** lot management (split/merge of `jute_mr_li` lines in place — no new MRs), batch transfer with common % change, balance-based consumption via `vw_jute_stock_outstanding` (ERP issue entries reduce balance; consumed = balance ≤ 0) |
| Shape | Circular: gate entry at Co A → sold B → C → … → back to A; % markup per hop; manual finalize | One-shot: partial qty of a purchased line moved to a MARKED godown at another company; stays there until sold |
| Per hop | New `jute_mr`+`jute_mr_li` at buyer + Raw-Jute `sales_invoice` (`invoice_type=5`); final hop UPDATEs the root in place | Single child `jute_mr`+`jute_mr_li` **+ seller Raw-Jute `sales_invoice` (`invoice_type=5`) at the source branch**; no chain, no return leg |
| `src_jute_mr_id` | Always the chain ROOT (star topology); hops linked via `src_com_id` | The DIRECT parent MR (different semantics!) |
| Tracking | Full chain reconstruction + rate cascade | No chain to reconstruct; the transfer itself books the inter-company sale (invoice created at move time, source recorded via `src_jute_mr_id`); the *onward* sale/consumption at the target still happens in the ERP (issue entries reduce the `vw_jute_stock_outstanding` balance) |
| UI | `pages/new_transfer_chain.py` (sole chain editor) | `pages/warehouse_stock.py` |
| Ops | `transfer.py` | `warehouse_stock_ops.py` + `lot_ops.py` |

Mutual exclusion is enforced in code: a line feeding a live chain can't be mark-moved and vice versa.

### Type 1 core logic

- Chain reconstruction: `get_transfer_chain` filters `src_jute_mr_id = root AND transfer_mode = 0`; `_reconstruct_chain` walks `src_com_id` as a received-from linked list from the root company.
- Rates cascade multiplicatively hop-to-hop (`_cascade_rate`: round at kg level each hop, ×100 back to quintal).
- **Claims never cascade** — every step's `claim_amount` is the flat sum of the original per-line claims. (Older phrasing "claim breaks" in this file was misleading; no such flag exists.)
- Finalization = buyer's company+branch equals root's → `_update_original_mr` UPDATEs the root row in place (no new row). `revert_original_mr` undoes it.
- Deletion from a middle step cascades downstream (MR + line items + both linked invoices per hop). The whole cascade (and un-finalize) runs in ONE transaction with the root row locked; a finalized chain is always reverted, also when deleting from step 1. Un-finalize puts back the root's party / party branch / MR date (exactly from the Final PO's marker, else derived by name from step 1).
- `save_transfer_step` locks the root row first and refuses a second step 1, any step after finalize, and a step that does not continue the chain's newest hop (stale tab / double save). Posted hop rates use exact Decimal (`jute_mr_chain_helpers.hop_rate`) = the screen's `_cascade_rate`. Finalize / un-finalize write the root's money exactly as the ERP does (`transfer._erp_recompute_money` mirrors vowerp3be `recompute_mr_money`, incl. 194Q TDS; TDS 0 on un-finalize). Deletes go by primary key after a plain read (`database.select_ids` / `delete_by_ids`) — never a DELETE filtered on an unindexed column (whole-table locks).

### Transfer POs (2026-10-01 — spec `docs/superpowers/specs/2026-10-01-transfer-po-design.md`)

Every chain transfer also writes ERP purchase orders (`jute_po` + `jute_po_li` + one `jute_po_status_log` row), in the same transaction as the step:
- **Forwarding PO** with every hop MR (hop's branch and party; linked via `jute_mr.po_id` / `jute_mr_li.jute_po_li_id`), and a **Final PO** at the mill when the chain is finalized (root's party after finalize; NOT linked from the root, which keeps its original ERP PO).
- One PO per lorry, from the MR's own active lines: rate rounded to the nearest ₹50 (PO only), ERP "quantity mode" (whole bales of 150 kg / loose units of 48 kg, `percentage` NULL), `vehicle_quantity` 1, no lorry type, credit terms only on the first hop's PO.
- Inserted CLOSED (`status_id = 5`, `close_type = 'TRANSFER'`) so the ERP never offers them at QC or lists them as outstanding; `close_remark` explains the PO in words.
- Identified by a marker in `jute_po.internal_note`: `JT|FORWARD|root=…|mr=…|srcpo=…|` / `JT|FINAL|root=…|mr=…|srcpo=…|orig=<party>/<branch>/<mr date>|` (`po_helpers.build_marker` / `parse_marker`). Only a PO whose marker names the MR is ever deleted, and never while another MR is linked to it.
- Code: `po_helpers.py` (pure math + marker), `po_ops.py` (`plan_*` read-only / `create_*` / `delete_*`, caller's transaction), `po_queries.py` (reads), `pages/po_tracker.py` + `po_tracker_helpers.py` (read-only PO Tracker page). Kill switch: env `JT_TRANSFER_PO=0` (default in `po_ops._ENABLED_DEFAULT`).
- Existing chains: `scripts/backfill_transfer_pos.py` (dry-run by default; writes need `--apply` + ONE scope `--all` / `--root N` / `--limit N` + `--expect N`; stops at the first failed PO or duplicate number; exit 0 only when all planned POs exist and the after-run check is clean; `--undo <log>`). Damaged rows from the fixed bugs: `scripts/repair_transfer_data.py` (dry-run by default; writes need `--apply --only <repair> --expect N`; the plan is printed and logged before the first write). Its `finalized-net` repair counts 194Q TDS in MR-date order (`_erp_money(cumulative_previous=…)`): a recompute today would count every later MR too. Both write production — owner's OK first. Dry-runs plan in a `START TRANSACTION READ ONLY` transaction — never `SET SESSION ... READ ONLY` (it sticks to the pooled connection).
- **Where this code runs:** the staff's daily copy of the app is on another server (`srv1836306.hstgr.cloud`) and runs what is merged to `main`; this VM only serves the owner's preview (port 8501). Code that is not deployed there does not exist for the staff, and the old code there never deletes a transfer PO — so: deploy first (PO creation OFF), then backfill, then switch ON, then repairs (spec, "Rollout order").

### Type 2 core logic (as built)

- Godowns tagged via `warehouse_mst.warehouse_type = 'MARKED'` (`queries.set_warehouse_marked`). That column is ERP master data ('J' jute godown, 'S' store …): tagging writes only godowns whose tag changes, untagging restores 'J', store godowns are not offered (it used to blank the type of every godown of the branch). Types compare case-insensitively (as MySQL does), and the godown lookups in `queries.py` return `{warehouse_id: label}` — the pages key every godown widget by id, because a branch can have two godowns of one name (sls branch 87: `LCPL_JUTE` 244 and 360).
- `save_marked_batch` (the only move entry point; there is no single-lot `save_marked_move` any more): per source MR INSERTs one child MR (`transfer_mode=1`) at the target branch/godown with the moved kg at a possibly different rate, and books one seller Raw-Jute `sales_invoice` at the source branch (buyer = target company's party, auto-created if missing), stamps the child MR with invoice no/date/amount, and links via `sales_invoice_jute.mr_id` = child MR id (deletion linkage — different semantics from Type 1's hop linkage). **Since 2026-10-03 a move does not change the source line or its MR header**: the stock-out is the invoice line, which names the source MR line (`sales_invoice_dtl.jute_mr_li_id`), so the ERP stock view `vw_jute_stock_outstanding` nets the moved kg off that line's balance by itself. `delete_marked_move` deletes the invoice (the balance comes back) and the child; moves written by the older code (which drained the source row) are still undone by restoring the source from `jute_lot_src`'s deltas until `scripts/repair_transfer_data.py marked-stock` converts them.
- P&L counts marked stock: `transfer_mode=1` MRs at status 3 (`get_company_wise_marked_stock`).
- **Resale (2026-08-04):** marked stock held at a company can be resold onward — `get_available_lots(include_marked=True)` lists mode-1 lines on the Transfer tab and `save_marked_batch` accepts mode-1 sources. Each hop creates a new mode-1 child MR + seller invoice at the current holder's branch; `src_jute_mr_id` = direct parent per hop; `delete_marked_move` enforces leaf-first undo. Split/merge (lot ops) remain mode-0 only, so resale always moves a line's full remaining balance.

### P&L dashboard (`pages/company_pl_dashboard.py`)

Per company per FY month: Purchases = SUM(`jute_mr.net_total`); Sales = SUM(invoice − claim) of `invoice_type=5` invoices by seller branch; Stock = unsold chain stock (mode 0, open root) + marked stock (mode 1); Adjusted P&L = (Sales − Purchases) + Stock.

## Architecture

### Module Organization

```
src/jutetransfer/
├── pages/
│   ├── new_transfer_chain.py         # Type 1: vertical chain page (sole chain-editing UI)
│   ├── warehouse_stock.py            # Type 2: marked-godown stock page
│   ├── po_tracker.py                 # PO Tracker: Original / Forwarding / Final PO per lorry (read-only)
│   ├── company_pl_dashboard.py       # Company P&L dashboard
│   ├── schema_viewer.py              # Schema browser (dev tool)
│   └── __init__.py
├── jute_mr_chain_helpers.py          # Pure Python chain math (no Streamlit/DB imports)
├── lot_helpers.py                    # Pure lot math (no Streamlit/DB imports)
├── transfer.py                       # Type 1 DB writes: save/delete/finalize/revert
├── po_helpers.py                     # Pure transfer-PO math + provenance marker (no Streamlit/DB imports)
├── po_ops.py                         # Transfer PO writes (plan/create/delete), caller's transaction
├── po_queries.py                     # Transfer PO reads (step cards, PO Tracker)
├── po_tracker_helpers.py             # Pure PO Tracker shaping (status model, reconciliation, CSV)
├── warehouse_stock_ops.py            # Type 2 DB writes: save/delete marked moves
├── lot_ops.py                        # In-place lot split/merge on jute_mr_li (provenance + per-line undo)
├── queries.py                        # All read queries + P&L aggregations
├── models.py                         # ORM mirror of sls tables (reference only — queries use raw SQL)
├── database.py                       # Cached engine, execute helpers, get_transaction()
├── config.py                         # .env-driven DB config
├── auth.py                           # Demo auth
├── schemas.py                        # Schema introspection cache (schema viewer only)
├── data.py                           # Fake data for demo pages (unused by real pages)
└── __init__.py

app.py                                # Streamlit entry point
tests/                                # pytest (no DB): chain math, PO math, tracker helpers, and
                                      # end-to-end flows on an in-memory SQLite stand-in (fake_mysql.py)
scripts/                              # backfill_transfer_pos.py, repair_transfer_data.py (dry-run by default)
```

### Critical Dependencies

- **jute_mr_chain_helpers.py** — pure Python core; imports nothing from pages/DB. No circular imports allowed.
- **transfer.py** — type 1 public API (`save_transfer_step`, `delete_chain_from_step`, `revert_original_mr`; a single hop is only ever removed through `delete_chain_from_step`). All writes inside `DatabaseConnection.get_transaction()`; a save takes its named locks first (`database.named_locks`: `jt_chain_save:<db>` plus the ERP's `jute_po_no:<db>:<branch>` per branch that may receive a PO) and releases them after the commit.
- **warehouse_stock_ops.py** — type 2 public API (`save_marked_batch`, `delete_marked_move`). Keep it independent of chain logic; the `transfer_mode` guard rails must stay.

## Key Implementation Patterns

### 1. The % Rate Increase Widget Bug (SOLVED — twice)

**Root causes (2026-03):** `nonlocal` doesn't cross Streamlit reruns; `value=` param resets widget state every rerun; closures capture stale loop variables.

**Second round (2026-10-03, review P1):** the fix had seeded the box with `value=st.session_state["pct_<root>_<step>"]` — a plain shadow key — and compared the box with that shadow. Streamlit drops a widget's key when the widget is not rendered (another lorry shown), but the shadow key lived on; after a save or delete on another lorry of the month the step dicts were rebuilt (pct 0) while the shadow still said 0.5, so the re-created box showed 0.50 and Save posted 0 % (MR, invoice and Final PO).

**Solution (in `pages/new_transfer_chain.py`, `_render_step_card`):**
```python
# the box is a keyed widget with NO value=, seeded from the step dict only
if pct_input_key not in st.session_state:
    st.session_state[pct_input_key] = dict_pct
new_pct = float(st.number_input("% Rate Increase", key=pct_input_key, ...) or 0)
# the dict follows the box, in the same run, and the chain is recomputed
if abs(new_pct - dict_pct) > 0.0001:
    step["pct_rate_increase"] = new_pct
    _recalculate_chain(all_steps, root_line_items, ...)   # no st.rerun()
```
`_drop_chain_state` (after a save / delete) forgets only the saved lorry's step dicts and every `pct_input_*` key; other lorries' typed steps are kept and rebuilt by `_chain_changed` when the database shows their chain changed. The company box takes `index=` from the kept dict, so a step survives a lorry switch. Reload keeps every widget and re-applies the typed % to the fresh dicts.

**Lessons:** a widget is only ever seeded from, and compared with, the model it edits (the step dict) — never a second copy of its own value; no `st.rerun()` inside a widget handler (it swallowed a Save tapped in the same run); `tests/test_pages.py` holds the headless regression.

### 2. Chain Recalculation

`jute_mr_chain_helpers._recalculate_chain()` is the math engine: takes a step with modified rate/pct, propagates totals forward through subsequent steps (round-then-cascade at kg level), recomputes claim as flat original total per step.

### 3. Session State Management

Use `st.session_state` for widget values, cached data, intermediate workflow state.
Do NOT use: `nonlocal` in callbacks, closure captures of loop variables, module-level globals.

## Testing

```bash
# Import validation
python -c "from src.jutetransfer import jute_mr_chain_helpers, transfer, warehouse_stock_ops; from src.jutetransfer.pages import new_transfer_chain, warehouse_stock, schema_viewer, company_pl_dashboard; print('OK')"

# Unit tests
pytest tests/ -v

# Full app (requires MySQL access + .env)
streamlit run app.py
```

Integration checklist when touching chain logic: rate cascade (10% on step 2 → step 3 updates; −5% propagates), save/reload persistence, edge cases (pending root with no transfers, single-step chain). When touching type 2: a move creates the mode-1 child and its seller invoice and leaves the source line untouched (its view balance drops by the moved kg); move blocked when line is in a live chain; delete removes the invoice and the balance comes back.

The pages have headless tests too (`tests/test_pages.py`, `streamlit.testing.v1.AppTest` on in-memory frames with every write function stubbed): the chain page's % box / Save consistency across lorries, Reload, godown-follows-company, delete confirmation, error paths.

## Development Workflow

### Before Committing

1. No circular imports (chain helpers import only stdlib/third-party).
2. Test the affected feature (cascade / widget state / query shape / marked-move guards).
3. All multi-statement writes go through `get_transaction()` — never partial commits.
4. Remember the scope rule: sls DB only.

### Known housekeeping debt (don't be surprised by these)

- The `[DEBUG]` captions and the `debug_transfer.log` append are gone (2026-10-03); a stale gitignored `src/debug_transfer.log` may still sit on a checkout — it corresponds to no database write and can be deleted. Errors on the pages now go through `logging` (the server log, `.logs/jt.log` on the dev VM), never onto the screen.
- Stray `pages/company_pl_dashboard.py.tmp.*` file.
- Demo auth means `updated_by` is always `1`.
- `docs/invoice_data_flow_step2.md`, `docs/invoice_verification_checklist.md`, `docs/step2_invoice_example.md` and the 2026-03/04 superpowers plans reference the retired `jute_mr.py`/`jute_mr_editor.py` pages — historical only.

## References

- **Process understanding (canonical):** `docs/TRANSFER_PROCESS_UNDERSTANDING.md` — full two-transfer-type explanation, ERP seam, DB evidence, open questions
- **Vertical chain page design:** `docs/NEW_VERTICAL_TRANSFER_CHAIN_PAGE_DESIGN.md`
- **ERP-side context:** `../vowerp3be/CLAUDE.md`, `../vowerp3ui/docs/claude/modules/jute-purchase/`

---

**Last Updated:** 2026-10-02
**Key Constraints:** sls tenant only; `transfer_mode` keeps the two transfer types disjoint; the vertical chain page is the sole chain-editing UI; gate-entry root MRs come from VoWERP, never from this app — lot split/merge edits `jute_mr_li` in place (no new mode-0 MRs), always traceable via `jute_lot_src`
