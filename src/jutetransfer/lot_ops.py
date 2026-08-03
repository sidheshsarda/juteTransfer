"""Lot restructuring ops: create/delete app-created lot MRs.

A lot MR re-allocates quantities from existing mode-0 lines at the SAME
company/branch. Line-level provenance lives in jute_lot_src; source lines are
reduced in place (weight conserved). Lot MRs have transfer_mode=0, status 3,
src_jute_mr_id NULL — downstream transfer logic treats them as ordinary MRs.

Kept separate from warehouse_stock_ops (marked moves) and transfer (chains).
"""

from datetime import date

from sqlalchemy import bindparam, text

from .database import DatabaseConnection
from .lot_helpers import validate_takes, line_price, primary_source_mr
from .transfer import (
    _get_next_gate_entry_no,
    _get_next_mr_number_in_txn,
    _get_next_bill_pass_no_in_txn,
)
from .warehouse_stock_ops import (
    _recompute_mr_header,
    _available_kg,
    _reduce_source_line,
    _LI_INSERT_SQL,
)

_LOCK_LINE_SQL = """
    SELECT li.jute_mr_li_id, li.accepted_weight, li.rate, li.actual_item_id,
           li.actual_quality, li.challan_quality_id, li.marka, li.crop_year,
           li.unit_conversion, li.warehouse_id, li.jute_mr_id,
           li.actual_qty, li.actual_weight, li.actual_rate,
           mr.branch_id, mr.transfer_mode, mr.status_id, mr.src_jute_mr_id,
           mr.party_id, mr.party_branch_id, bm.co_id
    FROM jute_mr_li li
    JOIN jute_mr mr ON mr.jute_mr_id = li.jute_mr_id
    JOIN branch_mst bm ON bm.branch_id = mr.branch_id
    WHERE li.jute_mr_li_id = :id
    FOR UPDATE
"""

_CHAIN_CHILD_SQL = """
    SELECT 1 FROM jute_mr
    WHERE src_jute_mr_id = :sid AND transfer_mode = 0 AND jute_mr_id <> :sid
    LIMIT 1
"""


def create_lot(takes, mr_date: date, updated_by: int) -> int:
    """Create one lot MR from (src_jute_mr_li_id, qty_kg) takes.

    All sources must be mode-0, status-3, same-branch lines not feeding a live
    chain. Returns the new lot MR id.
    """
    if not takes:
        raise ValueError("no lots selected")
    with DatabaseConnection.get_transaction() as conn:
        rows = {}
        for li_id in sorted(int(t[0]) for t in takes):  # ascending lock order
            row = conn.execute(text(_LOCK_LINE_SQL), {"id": li_id}).fetchone()
            if not row:
                raise ValueError(f"Source line {li_id} not found")
            rows[li_id] = dict(row._mapping)

        available = {
            i: _available_kg(conn, i, float(r["accepted_weight"] or 0))
            for i, r in rows.items()
        }
        norm = validate_takes(takes, available)

        branches = {int(r["branch_id"]) for r in rows.values()}
        if len(branches) != 1:
            raise ValueError("All source lines must belong to the same branch")
        branch_id = branches.pop()

        checked = set()
        for r in rows.values():
            if int(r["transfer_mode"] or 0) != 0:
                raise ValueError("Can only re-lot normal (transfer_mode=0) stock")
            if int(r["status_id"] or 0) != 3:
                raise ValueError("Can only re-lot Approved (status 3) MRs")
            mr_id = int(r["jute_mr_id"])
            if int(r["transfer_mode"] or 0) == 0 and r["src_jute_mr_id"] is not None:
                raise ValueError(
                    f"MR {mr_id} is a chain-hop MR (src_jute_mr_id set); re-lot disabled"
                )
            if mr_id not in checked:
                if conn.execute(text(_CHAIN_CHILD_SQL), {"sid": mr_id}).fetchone():
                    raise ValueError(
                        f"MR {mr_id} feeds a vertical transfer chain; re-lot disabled"
                    )
                checked.add(mr_id)

        # Reduce each source line (accepted + actual fields); collect per-MR
        # take totals and the per-take actual deltas for provenance.
        mr_totals, deltas = {}, {}
        for li_id, qty in norm:
            r = rows[li_id]
            deltas[li_id] = _reduce_source_line(conn, r, qty, available[li_id])
            mr_totals[int(r["jute_mr_id"])] = (
                mr_totals.get(int(r["jute_mr_id"]), 0.0) + qty
            )
        for mr_id in sorted(mr_totals):
            _recompute_mr_header(conn, mr_id, updated_by)

        # Lot MR header: party copied from the primary source MR (same company,
        # no remap needed); src_com_id / src_jute_mr_id NULL — this is a
        # re-allocation at the same company, not a transfer.
        primary_mr_id = primary_source_mr(mr_totals)
        primary = next(
            rows[li] for li, _ in norm
            if int(rows[li]["jute_mr_id"]) == primary_mr_id
        )
        lot_mr_id = DatabaseConnection.execute_insert_returning_id(conn, """
            INSERT INTO jute_mr (
                jute_gate_entry_no, branch_mr_no, jute_gate_entry_date, jute_mr_date,
                status_id, transfer_mode, updated_by, updated_date_time,
                branch_id, party_id, party_branch_id, src_com_id, src_jute_mr_id,
                total_amount, claim_amount, roundoff, net_total,
                bill_pass_no, bill_pass_date
            ) VALUES (
                :gate_no, :mr_no, :mr_date, :mr_date,
                3, 0, :updated_by, NOW(),
                :branch_id, :party_id, :party_branch_id, NULL, NULL,
                0, 0, 0, 0,
                :bill_pass_no, :mr_date
            )
        """, {
            "gate_no": _get_next_gate_entry_no(conn, branch_id),
            "mr_no": _get_next_mr_number_in_txn(conn, branch_id, mr_date),
            "mr_date": mr_date,
            "updated_by": updated_by,
            "branch_id": branch_id,
            "party_id": primary["party_id"],
            "party_branch_id": primary["party_branch_id"],
            "bill_pass_no": _get_next_bill_pass_no_in_txn(conn, branch_id),
        })

        # One lot line + one provenance row per take. Same company: item ids
        # copy over unchanged.
        for li_id, qty in norm:
            r = rows[li_id]
            aq_delta, aw_delta = deltas[li_id]
            new_li_id = DatabaseConnection.execute_insert_returning_id(
                conn, _LI_INSERT_SQL, {
                    "mr_id": lot_mr_id,
                    "actual_item_id": r["actual_item_id"],
                    "actual_quality": r["actual_quality"],
                    "challan_quality_id": r["challan_quality_id"],
                    "w": qty,
                    "rate": float(r["rate"] or 0),
                    "price": line_price(qty, float(r["rate"] or 0)),
                    "warehouse_id": r["warehouse_id"],
                    "actual_qty": aq_delta,
                    "marka": r["marka"],
                    "crop_year": r["crop_year"],
                    "unit_conversion": r["unit_conversion"],
                })
            conn.execute(text("""
                INSERT INTO jute_lot_src
                    (new_jute_mr_li_id, src_jute_mr_li_id, qty_kg,
                     actual_qty_delta, actual_weight_delta,
                     created_by, created_date_time)
                VALUES (:new_li, :src_li, :qty, :aq, :aw, :by, NOW())
            """), {"new_li": new_li_id, "src_li": li_id, "qty": qty,
                   "aq": aq_delta, "aw": aw_delta, "by": updated_by})

        _recompute_mr_header(conn, lot_mr_id, updated_by)
        return lot_mr_id


def delete_lot(lot_mr_id: int, updated_by: int) -> None:
    """Undo a lot MR: restore every source line exactly from jute_lot_src,
    then delete provenance rows, lines, and header.

    Blocks if any MR references the lot as source (marked move or chain), or
    if any lot line feeds a newer lot."""
    with DatabaseConnection.get_transaction() as conn:
        header = conn.execute(text("""
            SELECT jute_mr_id, transfer_mode FROM jute_mr
            WHERE jute_mr_id = :id FOR UPDATE
        """), {"id": lot_mr_id}).fetchone()
        if not header:
            raise ValueError(f"MR {lot_mr_id} not found")
        if int(header._mapping["transfer_mode"] or 0) != 0:
            raise ValueError("Not a lot MR (transfer_mode != 0)")

        prov = conn.execute(text("""
            SELECT ls.lot_src_id, ls.new_jute_mr_li_id, ls.src_jute_mr_li_id,
                   ls.qty_kg, ls.actual_qty_delta, ls.actual_weight_delta
            FROM jute_lot_src ls
            JOIN jute_mr_li li ON li.jute_mr_li_id = ls.new_jute_mr_li_id
            WHERE li.jute_mr_id = :id
            FOR UPDATE
        """), {"id": lot_mr_id}).fetchall()
        if not prov:
            raise ValueError(f"MR {lot_mr_id} is not an app-created lot MR")

        issued = conn.execute(text("""
            SELECT 1 FROM jute_issue ji
            JOIN jute_mr_li li ON li.jute_mr_li_id = ji.jute_mr_li_id
            WHERE li.jute_mr_id = :id AND COALESCE(ji.status_id, 0) <> 4
            LIMIT 1
        """), {"id": lot_mr_id}).fetchone()
        if issued:
            raise ValueError(
                "Lot has issue entries against it in the ERP; cannot delete"
            )

        if conn.execute(text("""
            SELECT 1 FROM jute_mr
            WHERE src_jute_mr_id = :id AND jute_mr_id <> :id LIMIT 1
        """), {"id": lot_mr_id}).fetchone():
            raise ValueError("Lot has dependent MRs (transfer exists); delete those first")

        lot_line_ids = sorted({int(p._mapping["new_jute_mr_li_id"]) for p in prov})
        feeds_stmt = text("""
            SELECT 1 FROM jute_lot_src
            WHERE src_jute_mr_li_id IN :ids LIMIT 1
        """).bindparams(bindparam("ids", expanding=True))
        if conn.execute(feeds_stmt, {"ids": lot_line_ids}).fetchone():
            raise ValueError("Lot lines feed a newer lot; delete that lot first")

        # Restore sources exactly from provenance (no quality matching), in
        # ascending line order; put back accepted AND actual amounts.
        restore = sorted(
            ((int(p._mapping["src_jute_mr_li_id"]), float(p._mapping["qty_kg"]),
              float(p._mapping["actual_qty_delta"] or 0),
              float(p._mapping["actual_weight_delta"] or 0))
             for p in prov),
            key=lambda t: t[0],
        )
        src_mr_ids = set()
        for src_li_id, qty, aq_delta, aw_delta in restore:
            src = conn.execute(text("""
                SELECT jute_mr_li_id, jute_mr_id, accepted_weight, rate,
                       actual_qty, actual_weight
                FROM jute_mr_li WHERE jute_mr_li_id = :id FOR UPDATE
            """), {"id": src_li_id}).fetchone()
            if not src:
                raise ValueError(f"Source line {src_li_id} vanished; cannot restore")
            s = src._mapping
            new_w = round(float(s["accepted_weight"] or 0) + qty, 3)
            conn.execute(text("""
                UPDATE jute_mr_li
                SET accepted_weight = :w, total_price = :p,
                    actual_weight = :aw, actual_qty = :aq,
                    updated_date_time = NOW()
                WHERE jute_mr_li_id = :id
            """), {"w": new_w, "p": line_price(new_w, float(s["rate"] or 0)),
                   "aw": round(float(s["actual_weight"] or 0) + aw_delta, 3),
                   "aq": round(float(s["actual_qty"] or 0) + aq_delta, 3),
                   "id": src_li_id})
            src_mr_ids.add(int(s["jute_mr_id"]))
        for mr_id in sorted(src_mr_ids):
            _recompute_mr_header(conn, mr_id, updated_by)

        del_prov = text(
            "DELETE FROM jute_lot_src WHERE new_jute_mr_li_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        conn.execute(del_prov, {"ids": lot_line_ids})
        conn.execute(text("DELETE FROM jute_mr_li WHERE jute_mr_id = :id"),
                     {"id": lot_mr_id})
        conn.execute(text("DELETE FROM jute_mr WHERE jute_mr_id = :id"),
                     {"id": lot_mr_id})
