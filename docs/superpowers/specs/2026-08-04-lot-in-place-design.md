# Lot split/merge in place (no lot MRs) — design

**Date:** 2026-08-04
**Requested by:** owner — "editing lot size should not make new MRs; lot tracking
should be done directly in that table (jute_mr_li), not outside."
**Supersedes:** the lot-MR mechanism from
`2026-08-03-warehouse-lot-management-design.md` (creation side only; provenance
and guards carry over).

## Problem

`lot_ops.create_lot` manufactures a whole new `jute_mr` header ("lot MR") for
every split/merge: fake gate-entry no, MR no, bill-pass no, header copied from
the primary source. These synthetic MRs pollute the ERP MR screen and the
purchase records. The lot itself is just a `jute_mr_li` row — resizing lots
only needs line-level edits.

## Decision

Split/merge edit `jute_mr_li` in place. **No new `jute_mr` header, ever.**

- **Split:** each take inserts a new line into the *source line's own MR* and
  reduces the source line (weight conserved within the MR; header recompute
  only shifts rounding pennies).
- **Merge:** merged line lands in the MR contributing the largest take
  (`primary_source_mr`). Cross-MR merges therefore shift weight/value between
  real MR headers — accepted practice: marked moves already reduce gate-entry
  MR headers via `_recompute_mr_header`.
- **Provenance unchanged:** one `jute_lot_src` row per source take pointing at
  the new line. `is_lot` detection and marked-move sourcing are line-level
  already and keep working.
- **Undo becomes per-line:** `delete_lot_line(li_id)` replaces
  `delete_lot(mr_id)`. Restores sources exactly from provenance, deletes the
  line + provenance, recomputes headers. Guards: ERP issue entries against the
  line; line feeds a newer lot/marked move (`jute_lot_src.src_jute_mr_li_id`);
  line's MR or any restore-target MR feeds a vertical chain; mode-0 only
  (marked children go through `delete_marked_move`).
- **Legacy lot MRs** already in the sls DB stay valid (ordinary mode-0 MRs).
  Their lines carry provenance, so per-line undo works on them; when an MR is
  left with zero lines after an undo it must be an app-created husk (ERP lines
  can never be app-deleted) and the header is deleted.
- **UI:** "Lot date" input dropped (nothing needs a date). Expander lists
  app-created lot *lines* (per-line provenance + Delete) instead of lot MRs.
- `create_lot` loses its `mr_date` param and returns new line id(s).
- `queries.get_lot_provenance(mr_id)` stays (marked-stock tab uses it); new
  `get_lot_line_provenance(li_id)` seeds the same recursive walk from one line.

## Out of scope

Migrating/cleaning existing lot MRs (they remain usable); any change to marked
moves, chains, or P&L queries.
