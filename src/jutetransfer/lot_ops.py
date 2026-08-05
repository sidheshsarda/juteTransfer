"""Lot restructuring ops: split/merge jute_mr_li lines IN PLACE.

Splits and merges never create jute_mr headers: new lines are inserted into
an EXISTING MR (split -> the source line's own MR; merge -> the MR of the
largest take) and source lines are reduced in place (weight conserved
overall). Line-level provenance lives in jute_lot_src; delete_lot_line
restores it exactly.

Kept separate from warehouse_stock_ops (marked moves) and transfer (chains).
"""

from sqlalchemy import text

from .database import DatabaseConnection
from .lot_helpers import (
    validate_takes, line_price, primary_source_mr, restore_amounts, combine_takes,
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

_PROV_INSERT_SQL = """
    INSERT INTO jute_lot_src
        (new_jute_mr_li_id, src_jute_mr_li_id, qty_kg,
         actual_qty_delta, actual_weight_delta,
         created_by, created_date_time)
    VALUES (:new_li, :src_li, :qty, :aq, :aw, :by, NOW())
"""


def create_lot(takes, updated_by: int, merge: bool = False) -> list:
    """Split/merge (src_jute_mr_li_id, qty_kg) takes into new lines in place.

    No jute_mr is created: split lines land inside their source line's own MR;
    a merged line lands inside the MR contributing the largest take. All
    sources must be mode-0, status-3, same-branch lines not feeding a live
    chain. Returns the new jute_mr_li ids (a single-element list for merge).

    merge=True combines all takes into ONE line (join lots): sources must
    share a single quality (actual_item_id) and godown; the merged line
    carries the summed kg at the weighted-average rate (value conserved).
    One jute_lot_src row per source take either way.
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

        if merge:
            if len(norm) < 2:
                raise ValueError("Merging needs at least two source lines")
            if any(rows[li]["actual_item_id"] is None for li, _ in norm):
                raise ValueError(
                    "Cannot merge lines without an assigned quality (item)"
                )
            if len({rows[li]["actual_item_id"] for li, _ in norm}) != 1:
                raise ValueError("Can only merge lines of a single quality")
            if len({rows[li]["warehouse_id"] for li, _ in norm}) != 1:
                raise ValueError("Can only merge lines in the same godown")

        branches = {int(r["branch_id"]) for r in rows.values()}
        if len(branches) != 1:
            raise ValueError("All source lines must belong to the same branch")

        checked = set()
        for r in rows.values():
            if int(r["transfer_mode"] or 0) != 0:
                raise ValueError("Can only re-lot normal (transfer_mode=0) stock")
            if int(r["status_id"] or 0) != 3:
                raise ValueError("Can only re-lot Approved (status 3) MRs")
            mr_id = int(r["jute_mr_id"])
            if r["src_jute_mr_id"] is not None:
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

        if merge:
            # ONE combined line inside the MR contributing the largest take:
            # summed kg, weighted-average rate (value conserved); attributes
            # from the largest take's line. Cross-MR merges shift weight
            # between real MR headers — same practice as marked moves.
            target_mr_id = primary_source_mr(mr_totals)
            big_li = max(norm, key=lambda t: t[1])[0]
            r = rows[big_li]
            parts = [(qty, float(rows[li]["rate"] or 0)) for li, qty in norm]
            kg, price, avg_rate = combine_takes(parts)
            merged_aq = round(sum(deltas[li][0] for li, _ in norm), 3)
            merged_li_id = DatabaseConnection.execute_insert_returning_id(
                conn, _LI_INSERT_SQL, {
                    "mr_id": target_mr_id,
                    "actual_item_id": r["actual_item_id"],
                    "actual_quality": r["actual_quality"],
                    "challan_quality_id": r["challan_quality_id"],
                    "w": kg,
                    "rate": avg_rate,
                    "price": price,
                    "warehouse_id": r["warehouse_id"],
                    "actual_qty": merged_aq,
                    "marka": r["marka"],
                    "crop_year": r["crop_year"],
                    "unit_conversion": r["unit_conversion"],
                })
            for li_id, qty in norm:
                aq_delta, aw_delta = deltas[li_id]
                conn.execute(text(_PROV_INSERT_SQL), {
                    "new_li": merged_li_id, "src_li": li_id, "qty": qty,
                    "aq": aq_delta, "aw": aw_delta, "by": updated_by})
            new_ids = [merged_li_id]
        else:
            # One new line per take inside its own source MR (weight conserved
            # per MR; header recompute only settles rounding).
            new_ids = []
            for li_id, qty in norm:
                r = rows[li_id]
                aq_delta, aw_delta = deltas[li_id]
                new_li_id = DatabaseConnection.execute_insert_returning_id(
                    conn, _LI_INSERT_SQL, {
                        "mr_id": int(r["jute_mr_id"]),
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
                conn.execute(text(_PROV_INSERT_SQL), {
                    "new_li": new_li_id, "src_li": li_id, "qty": qty,
                    "aq": aq_delta, "aw": aw_delta, "by": updated_by})
                new_ids.append(new_li_id)

        for mr_id in sorted(mr_totals):
            _recompute_mr_header(conn, mr_id, updated_by)
        return new_ids


def delete_lot_line(li_id: int, updated_by: int) -> None:
    """Undo one app-created lot line: restore every source exactly from
    jute_lot_src, delete provenance + the line, recompute headers. An MR left
    with zero lines must be an app-created husk (legacy lot MR — ERP lines can
    never be app-deleted) and its header is deleted too.

    Blocks if the line has ERP issue entries, feeds a newer lot or marked
    move, is not mode-0, or its MR / any restore-target MR feeds a chain."""
    with DatabaseConnection.get_transaction() as conn:
        row = conn.execute(text(_LOCK_LINE_SQL), {"id": li_id}).fetchone()
        if not row:
            raise ValueError(f"Line {li_id} not found")
        r = dict(row._mapping)
        if int(r["transfer_mode"] or 0) != 0:
            raise ValueError(
                "Not a mode-0 line; undo marked moves from the Marked Stock tab"
            )

        prov = conn.execute(text("""
            SELECT lot_src_id, src_jute_mr_li_id, qty_kg,
                   actual_qty_delta, actual_weight_delta
            FROM jute_lot_src WHERE new_jute_mr_li_id = :id
            FOR UPDATE
        """), {"id": li_id}).fetchall()
        if not prov:
            raise ValueError(f"Line {li_id} is not an app-created lot line")

        issued = conn.execute(text("""
            SELECT 1 FROM jute_issue
            WHERE jute_mr_li_id = :id AND COALESCE(status_id, 0) <> 4
            LIMIT 1
        """), {"id": li_id}).fetchone()
        if issued:
            raise ValueError(
                "Line has issue entries against it in the ERP; cannot delete"
            )

        if conn.execute(text("""
            SELECT 1 FROM jute_lot_src WHERE src_jute_mr_li_id = :id LIMIT 1
        """), {"id": li_id}).fetchone():
            raise ValueError(
                "Line feeds a newer lot or marked move; undo that first"
            )

        mr_id = int(r["jute_mr_id"])
        if conn.execute(text(_CHAIN_CHILD_SQL), {"sid": mr_id}).fetchone():
            raise ValueError(
                f"MR {mr_id} feeds a vertical transfer chain; cannot undo"
            )

        restore = sorted(
            ((int(p._mapping["src_jute_mr_li_id"]), float(p._mapping["qty_kg"]),
              float(p._mapping["actual_qty_delta"] or 0),
              float(p._mapping["actual_weight_delta"] or 0))
             for p in prov),
            key=lambda t: t[0],
        )

        # Lock + fetch all source lines first, and chain-guard every source MR
        # BEFORE any UPDATE runs (fail-fast; the transaction would roll back
        # anyway, but this avoids partial mutation attempts).
        srcs = {}
        for src_li_id, _qty, _aq, _aw in restore:
            src = conn.execute(text("""
                SELECT jute_mr_li_id, jute_mr_id, accepted_weight, rate,
                       actual_qty, actual_weight
                FROM jute_mr_li WHERE jute_mr_li_id = :id FOR UPDATE
            """), {"id": src_li_id}).fetchone()
            if not src:
                raise ValueError(f"Source line {src_li_id} vanished; cannot restore")
            srcs[src_li_id] = src._mapping

        for src_mr_id in {int(s["jute_mr_id"]) for s in srcs.values()}:
            if src_mr_id == mr_id:
                continue  # already guarded above
            if conn.execute(text(_CHAIN_CHILD_SQL), {"sid": src_mr_id}).fetchone():
                raise ValueError(
                    f"Source MR {src_mr_id} now feeds a vertical chain; "
                    "cannot restore weights onto it"
                )

        touched = {mr_id}
        for src_li_id, qty, aq_delta, aw_delta in restore:
            s = srcs[src_li_id]
            new_w, new_aw, new_aq = restore_amounts(
                s["accepted_weight"], s["actual_weight"], s["actual_qty"],
                qty, aq_delta, aw_delta,
            )
            conn.execute(text("""
                UPDATE jute_mr_li
                SET accepted_weight = :w, total_price = :p,
                    actual_weight = :aw, actual_qty = :aq,
                    updated_date_time = NOW()
                WHERE jute_mr_li_id = :id
            """), {"w": new_w, "p": line_price(new_w, float(s["rate"] or 0)),
                   "aw": new_aw,
                   "aq": new_aq,
                   "id": src_li_id})
            touched.add(int(s["jute_mr_id"]))

        conn.execute(text(
            "DELETE FROM jute_lot_src WHERE new_jute_mr_li_id = :id"
        ), {"id": li_id})
        conn.execute(text(
            "DELETE FROM jute_mr_li WHERE jute_mr_li_id = :id"
        ), {"id": li_id})
        for m in sorted(touched):
            _recompute_mr_header(conn, m, updated_by)
        if not conn.execute(text(
            "SELECT 1 FROM jute_mr_li WHERE jute_mr_id = :id LIMIT 1"
        ), {"id": mr_id}).fetchone():
            conn.execute(text("DELETE FROM jute_mr WHERE jute_mr_id = :id"),
                         {"id": mr_id})
