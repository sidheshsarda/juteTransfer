"""Edit mode: change a SAVED transfer in place (owner ruling 2026-11-17).

A correction used to mean delete + save again, which hands out new MR /
invoice / PO numbers at the top of each series. Here the rows are UPDATEd:
ids and every number stay as they are.

What can be edited (no money changes, so nothing has to be re-cascaded):

  MR date   the MR's date (and, for a hop or a marked child, its gate entry
            and bill pass dates), the invoice booked for that step, the
            step's transfer PO, and the MR's 194Q TDS when the new date
            moves it in the party's sequence.
  Godown    the godown on the MR's lines (hops and marked children; the
            mill's own godown on a finalized root belongs to the ERP).

Limits, each refused in words:
  * the new date must stay in the same calendar month as the old one: the
    challan number carries the month and every number series the year, so a
    date in another month is a different number -- delete and save again;
  * not after today;
  * between the dates of the neighbouring MR numbers of the branch (the date
    order rule), unless the admin ticks "out of order";
  * not before where the lorry came from (the previous transfer, or the gate
    entry for the first one) and not after where it went next;
  * a godown of the MR's own branch (a marked child: a MARKED godown), and
    not once the ERP has issued from those lines.

Admins only, a reason is required, and every edit is written to
jt_change_log (old and new values) in the same transaction.
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import text

from . import access, audit, po_ops
from .database import DatabaseConnection, named_locks
from .jute_mr_chain_helpers import _reconstruct_chain, edit_date_problem, received_date_problem
from .queries import _get_financial_year_bounds
from .transfer import (
    CHAIN_SAVE_LOCK, CHAIN_SAVE_LOCK_TIMEOUT, HOP_TDS_AMOUNT, MR_DATE_EXCLUDED_STATUSES,
    _chain_rows, _erp_jute_totals, _erp_money, _invoice_ids_for_mr,
)

ACTION_EDIT_STEP = "edit-step"
ACTION_EDIT_MARKED = "edit-marked-move"

KIND_HOP, KIND_FINAL, KIND_MARKED = "hop", "final", "marked"


def _as_date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):          # an aggregate (MIN / MAX) can come back as text
        return date.fromisoformat(value[:10])
    return value


def _neighbour_dates(conn, branch_id: int, mr_no: int, ref_date: date, mr_id: int):
    """(latest date among lower MR numbers, earliest date among higher ones)
    of the branch's series in ref_date's financial year."""
    fy_start, fy_end = _get_financial_year_bounds(ref_date)
    params = {"bid": branch_id, "no": mr_no, "id": mr_id,
              "fy_start": fy_start.strftime("%Y-%m-%d"), "fy_end": fy_end.strftime("%Y-%m-%d")}
    base = f"""FROM jute_mr WHERE branch_id = :bid AND branch_mr_no IS NOT NULL
               AND jute_mr_id <> :id
               AND jute_mr_date BETWEEN :fy_start AND :fy_end
               AND COALESCE(status_id, 0) NOT IN ({", ".join(map(str, MR_DATE_EXCLUDED_STATUSES))})"""
    lo = conn.execute(text(
        f"SELECT jute_mr_date {base} AND branch_mr_no < :no ORDER BY jute_mr_date DESC LIMIT 1"),
        params).fetchone()
    hi = conn.execute(text(
        f"SELECT jute_mr_date {base} AND branch_mr_no > :no ORDER BY jute_mr_date ASC LIMIT 1"),
        params).fetchone()
    return (_as_date(lo[0]) if lo else None, _as_date(hi[0]) if hi else None)


def _mr_dates(conn, mr_id: int) -> tuple:
    row = conn.execute(text(
        "SELECT jute_mr_date, jute_gate_entry_date FROM jute_mr WHERE jute_mr_id = :id"),
        {"id": mr_id}).fetchone()
    return (_as_date(row[0]), _as_date(row[1])) if row else (None, None)


def _check_chain_dates(new_date: Optional[date], came_from, went_to) -> None:
    """A step stays between the date of where the lorry came from and the
    date of where it went next (owner rule 2026-11-17)."""
    if new_date is None:
        return
    problem = received_date_problem(new_date, came_from, "where the lorry came from")
    if problem:
        raise ValueError(problem)
    if went_to is not None and new_date > went_to:
        raise ValueError(
            f"The date {new_date:%d-%m-%Y} is after the next transfer of this lorry "
            f"({went_to:%d-%m-%Y}). Use {went_to:%d-%m-%Y} or earlier, or re-date the next "
            "transfer first.")


def _edit_mr(conn, mr_id: int, kind: str, invoice_ids: list, po_ids: list,
             new_date: Optional[date], new_warehouse_id: Optional[int],
             allow_out_of_order: bool, updated_by: int) -> dict:
    """Apply the edit to one MR and what hangs off it; returns {field: [old, new]}."""
    hdr = conn.execute(text("""
        SELECT branch_id, branch_mr_no, jute_mr_date, tds_amount, total_amount, claim_amount
        FROM jute_mr WHERE jute_mr_id = :id FOR UPDATE
    """), {"id": mr_id}).fetchone()
    if not hdr:
        raise ValueError(f"MR {mr_id} not found")
    branch_id, mr_no, old_date = int(hdr[0]), hdr[1], _as_date(hdr[2])
    changed = {}

    if new_date is not None and new_date != old_date:
        if old_date is None or mr_no is None:
            raise ValueError(f"MR {mr_id} has no MR number or date yet; nothing to re-date")
        lo, hi = _neighbour_dates(conn, branch_id, int(mr_no), old_date, mr_id)
        problem, out_of_order = edit_date_problem(new_date, old_date, lo, hi, today=date.today())
        if problem and not (out_of_order and allow_out_of_order):
            raise ValueError(problem)

        own_dates = "jute_mr_date = :d, bill_pass_date = CASE WHEN bill_pass_date IS NULL THEN NULL ELSE :d END"
        if kind != KIND_FINAL:       # the mill keeps the lorry's real gate entry date
            own_dates += ", jute_gate_entry_date = :d"
        if invoice_ids:              # the invoice booked for this step is re-dated below
            own_dates += ", invoice_date = CASE WHEN invoice_date IS NULL THEN NULL ELSE :d END"
            if kind != KIND_MARKED:  # a marked child keeps its source's challan date
                own_dates += ", challan_date = CASE WHEN challan_date = :old THEN :d ELSE challan_date END"
        conn.execute(text(
            f"UPDATE jute_mr SET {own_dates}, updated_by = :u, updated_date_time = NOW() "
            "WHERE jute_mr_id = :id"), {"d": new_date, "old": old_date, "u": updated_by, "id": mr_id})
        for inv_id in invoice_ids:
            conn.execute(text(
                "UPDATE sales_invoice SET invoice_date = :d, "
                "challan_date = CASE WHEN challan_date IS NULL THEN NULL ELSE :d END, "
                "updated_by = :u, updated_date_time = NOW() WHERE invoice_id = :id"),
                {"d": new_date, "u": updated_by, "id": inv_id})
        for po_id in po_ids:
            conn.execute(text(
                "UPDATE jute_po SET po_date = :d, updated_by = :u, updated_date_time = NOW() "
                "WHERE jute_po_id = :id"), {"d": new_date, "u": updated_by, "id": po_id})
        changed["mr_date"] = [old_date, new_date]
        if invoice_ids:
            changed["invoice_ids"] = list(invoice_ids)
        if po_ids:
            changed["po_ids"] = list(po_ids)

        # 194Q TDS follows the MR's place in the party's sequence (date, no,
        # id). Only the TDS, roundoff and net are rewritten, from the STORED
        # total and claim: a date edit never changes a rate or an amount.
        if kind != KIND_MARKED:
            old_tds = round(float(hdr[3] or 0), 2)
            new_tds = _erp_money(conn, mr_id,
                                 HOP_TDS_AMOUNT if kind == KIND_HOP else None)["tds_amount"]
            if abs(new_tds - old_tds) >= 0.005:
                roundoff, net = _erp_jute_totals(hdr[4], hdr[5], new_tds)
                conn.execute(text(
                    "UPDATE jute_mr SET tds_amount = :t, roundoff = :r, net_total = :n "
                    "WHERE jute_mr_id = :id"), {"t": new_tds, "r": roundoff, "n": net, "id": mr_id})
                changed["tds_amount"] = [old_tds, new_tds]

    if new_warehouse_id is not None:
        if kind == KIND_FINAL:
            raise ValueError("The godown of the mill's own MR is set in the ERP, not here")
        lines = conn.execute(text(
            "SELECT jute_mr_li_id, warehouse_id FROM jute_mr_li "
            "WHERE jute_mr_id = :id AND (active = 1 OR active IS NULL)"), {"id": mr_id}).fetchall()
        old_ids = sorted({r[1] for r in lines if r[1] is not None})
        if old_ids != [int(new_warehouse_id)]:
            wh = conn.execute(text(
                "SELECT branch_id, warehouse_type FROM warehouse_mst WHERE warehouse_id = :w"),
                {"w": new_warehouse_id}).fetchone()
            if not wh or int(wh[0] or 0) != branch_id:
                raise ValueError("That godown does not belong to this MR's branch")
            if kind == KIND_MARKED and (wh[1] or "").upper() != "MARKED":
                raise ValueError("Marked stock can only sit in a MARKED godown")
            issued = conn.execute(text(
                "SELECT 1 FROM jute_issue ji JOIN jute_mr_li li ON li.jute_mr_li_id = ji.jute_mr_li_id "
                "WHERE li.jute_mr_id = :id AND COALESCE(ji.status_id, 0) <> 4 LIMIT 1"),
                {"id": mr_id}).fetchone()
            if issued:
                raise ValueError("The ERP has already issued from this MR; its godown cannot "
                                 "be changed here")
            for line_id in [r[0] for r in lines]:
                conn.execute(text(
                    "UPDATE jute_mr_li SET warehouse_id = :w WHERE jute_mr_li_id = :id"),
                    {"w": new_warehouse_id, "id": line_id})
            changed["warehouse_id"] = [old_ids, int(new_warehouse_id)]
    return changed


def _check_request(reason: str, updated_by: int, new_date, new_warehouse_id) -> str:
    access.require_admin(updated_by)
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required to edit a saved transfer")
    if new_date is None and new_warehouse_id is None:
        raise ValueError("Nothing to change")
    return reason


def edit_chain_step(root_mr_id: int, step_mr_id: int, *, reason: str, updated_by: int,
                    new_date: Optional[date] = None, new_warehouse_id: Optional[int] = None,
                    allow_out_of_order: bool = False) -> dict:
    """Edit one saved step of a vertical chain in place. step_mr_id == root_mr_id
    is the finalized return step (the mill's own MR). Returns what changed,
    {field: [old, new]} -- empty when the values were already those."""
    reason = _check_request(reason, updated_by, new_date, new_warehouse_id)
    with named_locks({CHAIN_SAVE_LOCK: CHAIN_SAVE_LOCK_TIMEOUT}), \
            DatabaseConnection.get_transaction() as conn:
        root = conn.execute(text(
            "SELECT branch_id, branch_mr_no FROM jute_mr WHERE jute_mr_id = :id FOR UPDATE"),
            {"id": root_mr_id}).fetchone()
        if not root:
            raise ValueError(f"Root MR {root_mr_id} not found")
        co = conn.execute(text("SELECT co_id FROM branch_mst WHERE branch_id = :b"),
                          {"b": root[0]}).fetchone()
        ordered = _reconstruct_chain(_chain_rows(conn, root_mr_id), co[0] if co else 0)
        ids = [int(m["jute_mr_id"]) for m in ordered]
        if not ids:
            raise ValueError(f"MR {root_mr_id} has no transfer chain")

        if int(step_mr_id) == int(root_mr_id):
            if root[1] is None:
                raise ValueError("This chain is not finalized; refresh the page")
            kind = KIND_FINAL
            invoice_ids = _invoice_ids_for_mr(conn, ids[-1])        # booked by the last seller
            po_ids = po_ops.find_final_po_ids(conn, root_mr_id)
            came_from, went_to = _mr_dates(conn, ids[-1])[0], None
        else:
            if int(step_mr_id) not in ids:
                raise ValueError("That step is no longer part of this chain; refresh the page")
            kind = KIND_HOP
            at = ids.index(int(step_mr_id))
            invoice_ids = _invoice_ids_for_mr(conn, ids[at - 1]) if at > 0 else []
            po_ids = po_ops.find_forward_po_ids(conn, int(step_mr_id))
            # from: the previous hop, or the lorry's gate entry for the first
            came_from = (_mr_dates(conn, ids[at - 1])[0] if at > 0
                         else _mr_dates(conn, root_mr_id)[1])
            # to: the next hop, or the finalized mill MR after the last one
            if at + 1 < len(ids):
                went_to = _mr_dates(conn, ids[at + 1])[0]
            else:
                went_to = _mr_dates(conn, root_mr_id)[0] if root[1] is not None else None
        if len(invoice_ids) > 1:
            raise ValueError("This step has more than one invoice; it cannot be edited here")
        _check_chain_dates(new_date, came_from, went_to)

        changed = _edit_mr(conn, int(step_mr_id), kind, invoice_ids, po_ids,
                           new_date, new_warehouse_id, allow_out_of_order, updated_by)
        if changed:
            audit.log(conn, ACTION_EDIT_STEP, jute_mr_id=int(step_mr_id),
                      detail={"root_mr_id": int(root_mr_id), "kind": kind, **changed},
                      reason=reason, user_id=updated_by)
    return changed


def edit_marked_move(child_mr_id: int, *, reason: str, updated_by: int,
                     new_date: Optional[date] = None, new_warehouse_id: Optional[int] = None,
                     allow_out_of_order: bool = False) -> dict:
    """Edit one marked move (its child MR and seller invoice) in place."""
    reason = _check_request(reason, updated_by, new_date, new_warehouse_id)
    with named_locks({CHAIN_SAVE_LOCK: CHAIN_SAVE_LOCK_TIMEOUT}), \
            DatabaseConnection.get_transaction() as conn:
        child = conn.execute(text(
            "SELECT src_jute_mr_id, transfer_mode FROM jute_mr WHERE jute_mr_id = :id FOR UPDATE"),
            {"id": child_mr_id}).fetchone()
        if not child or child[0] is None or int(child[1] or 0) != 1:
            raise ValueError(f"MR {child_mr_id} is not a marked move")
        invoice_ids = _invoice_ids_for_mr(conn, child_mr_id)
        if len(invoice_ids) > 1:
            raise ValueError("This move has more than one invoice; it cannot be edited here")
        onward = conn.execute(text(
            "SELECT MIN(jute_mr_date) FROM jute_mr WHERE src_jute_mr_id = :id AND transfer_mode = 1"),
            {"id": child_mr_id}).scalar()
        _check_chain_dates(new_date, _mr_dates(conn, int(child[0]))[0], _as_date(onward))
        changed = _edit_mr(conn, int(child_mr_id), KIND_MARKED, invoice_ids, [],
                           new_date, new_warehouse_id, allow_out_of_order, updated_by)
        if changed:
            audit.log(conn, ACTION_EDIT_MARKED, jute_mr_id=int(child_mr_id),
                      detail=changed, reason=reason, user_id=updated_by)
    return changed
