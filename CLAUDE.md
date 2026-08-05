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
- `jute_mr.status_id` semantics: `1` Open, `3` Approved (eligible for transfer), `13` Pending — documented by vowerp3be itself as "terminal on MR screen, handed off to external system" (= this app). Status 13 on a chain root means the origin company is consuming the jute (chain closed).
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
- Deletion from a middle step cascades downstream (MR + line items + both linked invoices per hop).

### Type 2 core logic (as built)

- Godowns tagged via `warehouse_mst.warehouse_type = 'MARKED'` (`queries.set_warehouse_marked`).
- `save_marked_move`: reduces source `jute_mr_li.accepted_weight`/`total_price` (recomputes source header), INSERTs child MR at target branch/warehouse with `transfer_mode=1`, possibly different rate.
- `save_marked_batch`: also books one seller Raw-Jute `sales_invoice` per child MR at the source branch (buyer = target company's party, auto-created if missing), stamps the child MR with invoice no/date/amount, and links via `sales_invoice_jute.mr_id` = child MR id (deletion linkage — different semantics from Type 1's hop linkage). `delete_marked_move` cascades the invoice.
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
│   ├── company_pl_dashboard.py       # Company P&L dashboard
│   ├── schema_viewer.py              # Schema browser (dev tool)
│   └── __init__.py
├── jute_mr_chain_helpers.py          # Pure Python chain math (no Streamlit/DB imports)
├── lot_helpers.py                    # Pure lot math (no Streamlit/DB imports)
├── transfer.py                       # Type 1 DB writes: save/delete/finalize/revert
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
tests/                                # pytest: chain reconstruction, grouping, recalculation
```

### Critical Dependencies

- **jute_mr_chain_helpers.py** — pure Python core; imports nothing from pages/DB. No circular imports allowed.
- **transfer.py** — type 1 public API (`save_transfer_step`, `delete_transfer_step`, `delete_chain_from_step`, `revert_original_mr`). All writes inside `DatabaseConnection.get_transaction()`.
- **warehouse_stock_ops.py** — type 2 public API (`save_marked_move`, `delete_marked_move`). Keep it independent of chain logic; the `transfer_mode` guard rails must stay.

## Key Implementation Patterns

### 1. The % Rate Increase Widget Bug (SOLVED)

**Root causes:** `nonlocal` doesn't cross Streamlit reruns; `value=` param resets widget state every rerun; closures capture stale loop variables.

**Solution (in `pages/new_transfer_chain.py`):**
```python
# Initialize widget state if missing
if pct_key not in st.session_state:
    st.session_state[pct_key] = current_pct

# Let Streamlit manage widget, don't pass value= param
new_pct = float(st.session_state[pct_key])

# Trigger recalculation if changed
if new_pct != current_pct:
    changed = True
```

**Lesson:** Streamlit's session state is the source of truth; never override it with `value=` parameters when you need persistent input state.

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

Integration checklist when touching chain logic: rate cascade (10% on step 2 → step 3 updates; −5% propagates), save/reload persistence, edge cases (pending root with no transfers, single-step chain). When touching type 2: partial move reduces source and creates mode-1 child; move blocked when line is in a live chain; delete restores source weights.

## Development Workflow

### Before Committing

1. No circular imports (chain helpers import only stdlib/third-party).
2. Test the affected feature (cascade / widget state / query shape / marked-move guards).
3. All multi-statement writes go through `get_transaction()` — never partial commits.
4. Remember the scope rule: sls DB only.

### Known housekeeping debt (don't be surprised by these)

- Live `[DEBUG]` caption in `new_transfer_chain.py` (~line 470) and `debug_transfer.log` append on every save — leftover instrumentation.
- Stray `pages/company_pl_dashboard.py.tmp.*` file.
- Demo auth means `updated_by` is always `1`.
- `docs/invoice_data_flow_step2.md`, `docs/invoice_verification_checklist.md`, `docs/step2_invoice_example.md` and the 2026-03/04 superpowers plans reference the retired `jute_mr.py`/`jute_mr_editor.py` pages — historical only.

## References

- **Process understanding (canonical):** `docs/TRANSFER_PROCESS_UNDERSTANDING.md` — full two-transfer-type explanation, ERP seam, DB evidence, open questions
- **Vertical chain page design:** `docs/NEW_VERTICAL_TRANSFER_CHAIN_PAGE_DESIGN.md`
- **ERP-side context:** `../vowerp3be/CLAUDE.md`, `../vowerp3ui/docs/claude/modules/jute-purchase/`

---

**Last Updated:** 2026-08-04
**Key Constraints:** sls tenant only; `transfer_mode` keeps the two transfer types disjoint; the vertical chain page is the sole chain-editing UI; gate-entry root MRs come from VoWERP, never from this app — lot split/merge edits `jute_mr_li` in place (no new mode-0 MRs), always traceable via `jute_lot_src`
