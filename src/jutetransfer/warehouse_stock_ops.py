"""Warehouse-marked stock moves (partial-quantity, no circular chain).

A "mark move" takes part of a purchased stock line at company A and moves it
into a marked godown at company B: it reduces the source line's accepted_weight
(the balance stays at A) and inserts a child jute_mr (transfer_mode=1) at B
carrying only the moved quantity at a possibly-new rate. No sales invoice is
created — the markup surfaces only as extra stock value at B.

Kept deliberately separate from the vertical transfer chain (transfer.py):
no rate cascade, no finalization / return-to-origin, no invoices. The two
worlds are disjoint by jute_mr.transfer_mode (0 = chain, 1 = marked stock).

Run `python -m src.jutetransfer.warehouse_stock_ops` for the split_weights
self-check (no DB required).
"""

from datetime import date
from typing import Tuple

from sqlalchemy import text

from .database import DatabaseConnection
from .lot_helpers import apply_pct, line_price
from .transfer import (
    _ensure_company_as_party,
    _ensure_item,
    _get_next_gate_entry_no,
    _get_next_mr_number_in_txn,
    _get_next_bill_pass_no_in_txn,
)


def split_weights(source: float, moved: float) -> Tuple[float, float]:
    """Split a source weight into (remaining_at_source, moved_to_child).

    Raises ValueError if moved is not in (0, source]. Weight is conserved:
    remaining + moved == source (to 3dp).
    """
    if moved <= 0:
        raise ValueError(f"moved qty must be > 0, got {moved}")
    if moved > source:
        raise ValueError(f"moved qty {moved} exceeds available {source}")
    return round(source - moved, 3), round(moved, 3)


def _round2(x: float) -> float:
    return float(round(x, 2))


def _recompute_mr_header(conn, jute_mr_id: int, updated_by: int) -> None:
    """Recompute total_amount/roundoff/net_total from the MR's line items.

    Mirrors transfer._update_original_mr's SUM(total_price) pattern; claim_amount
    is left as stored (matches chain behaviour). net_total = ROUND(SUM,0) - claim.
    """
    conn.execute(text("""
        UPDATE jute_mr SET
            total_amount = (SELECT ROUND(COALESCE(SUM(total_price),0),0) FROM jute_mr_li WHERE jute_mr_id = :id),
            roundoff = (SELECT ROUND(COALESCE(SUM(total_price),0),0) FROM jute_mr_li WHERE jute_mr_id = :id) -
                       (SELECT COALESCE(SUM(total_price),0) FROM jute_mr_li WHERE jute_mr_id = :id),
            net_total = (SELECT ROUND(COALESCE(SUM(total_price),0),0) FROM jute_mr_li WHERE jute_mr_id = :id)
                        - COALESCE(claim_amount,0),
            updated_by = :updated_by,
            updated_date_time = NOW()
        WHERE jute_mr_id = :id
    """), {"id": jute_mr_id, "updated_by": updated_by})


_BALANCE_SQL = """
    SELECT bal_weight FROM vw_jute_stock_outstanding WHERE jute_mr_li_id = :id
"""


def _available_kg(conn, li_id: int, accepted: float) -> float:
    """Balance-aware available kg: LEAST(view balance, accepted_weight).

    The view row can be missing (e.g. freshly created line inside this
    transaction) — fall back to accepted_weight."""
    row = conn.execute(text(_BALANCE_SQL), {"id": li_id}).fetchone()
    bal = row._mapping["bal_weight"] if row else None
    return round(min(float(bal), accepted), 3) if bal is not None else accepted


def _reduce_source_line(conn, r: dict, qty: float, available: float) -> tuple:
    """Reduce a locked source line by qty kg across accepted AND actual fields.

    Returns (actual_qty_delta, actual_weight_delta) for provenance storage."""
    accepted = float(r["accepted_weight"] or 0)
    actual_w = float(r["actual_weight"] or 0)
    actual_q = float(r["actual_qty"] or 0)
    frac = qty / available if available > 0 else 1.0
    aw_delta = round(min(qty, actual_w), 3)
    aq_delta = round(actual_q * frac, 3)
    new_accepted = round(accepted - qty, 3)
    conn.execute(text("""
        UPDATE jute_mr_li
        SET accepted_weight = :w, total_price = :p,
            actual_weight = :aw, actual_qty = :aq,
            updated_date_time = NOW()
        WHERE jute_mr_li_id = :id
    """), {
        "w": new_accepted,
        "p": line_price(new_accepted, float(r["rate"] or 0)),
        "aw": round(max(0.0, actual_w - aw_delta), 3),
        "aq": round(max(0.0, actual_q - aq_delta), 3),
        "id": int(r["jute_mr_li_id"]),
    })
    return aq_delta, aw_delta


_LI_INSERT_SQL = """
    INSERT INTO jute_mr_li (
        jute_mr_id, actual_item_id, actual_quality, challan_quality_id,
        accepted_weight, rate, claim_rate, total_price, warehouse_id,
        actual_qty, actual_weight, actual_rate,
        marka, crop_year, active, updated_date_time, unit_conversion
    ) VALUES (
        :mr_id, :actual_item_id, :actual_quality, :challan_quality_id,
        :w, :rate, 0, :price, :warehouse_id,
        :actual_qty, :w, :rate,
        :marka, :crop_year, 1, NOW(), :unit_conversion
    )
"""
# actual_weight = accepted kg and actual_rate = rate on app-created lines, so
# the ERP stock view computes balances for them (bal = actual_weight - issued).


def save_marked_move(
    source_mr_li_id: int,
    moved_qty: float,
    rate: float,
    target_co_id: int,
    target_branch_id: int,
    warehouse_id: int,
    mr_date: date,
    updated_by: int,
) -> int:
    """Move ``moved_qty`` from a source stock line into a marked godown at the
    target company. Reduces the source line (balance stays) and inserts a child
    jute_mr (transfer_mode=1). Returns the child jute_mr_id.

    Raises ValueError on over-transfer, a non-normal source, or a source that is
    a live vertical-chain lot (shrinking it would corrupt the chain).
    """
    with DatabaseConnection.get_transaction() as conn:
        # Lock + read the source line (FOR UPDATE serialises concurrent marks).
        row = conn.execute(text("""
            SELECT li.accepted_weight, li.rate, li.actual_item_id, li.actual_quality,
                   li.challan_quality_id, li.marka, li.crop_year, li.unit_conversion,
                   li.jute_mr_id, mr.branch_id AS src_branch_id, mr.transfer_mode,
                   bm.co_id AS src_co_id
            FROM jute_mr_li li
            JOIN jute_mr mr ON mr.jute_mr_id = li.jute_mr_id
            JOIN branch_mst bm ON bm.branch_id = mr.branch_id
            WHERE li.jute_mr_li_id = :id
            FOR UPDATE
        """), {"id": source_mr_li_id}).fetchone()
        if not row:
            raise ValueError(f"Source line {source_mr_li_id} not found")
        r = row._mapping

        available = float(r["accepted_weight"] or 0)
        source_rate = float(r["rate"] or 0)
        source_mr_id = int(r["jute_mr_id"])
        source_co_id = int(r["src_co_id"])
        source_branch_id = int(r["src_branch_id"])

        # Guards
        if int(r["transfer_mode"] or 0) != 0:
            raise ValueError("Can only mark-move from normal (transfer_mode=0) stock")
        new_source_weight, moved = split_weights(available, float(moved_qty))
        chain_child = conn.execute(text("""
            SELECT 1 FROM jute_mr
            WHERE src_jute_mr_id = :sid AND transfer_mode = 0 AND jute_mr_id <> :sid
            LIMIT 1
        """), {"sid": source_mr_id}).fetchone()
        if chain_child:
            raise ValueError(
                "This MR is part of a vertical transfer chain; "
                "mark-move is disabled to avoid corrupting it"
            )

        # 1. Reduce the source line + recompute its header (balance stays at A).
        new_source_price = _round2(new_source_weight * source_rate / 100.0)
        conn.execute(text("""
            UPDATE jute_mr_li
            SET accepted_weight = :w, total_price = :p, updated_date_time = NOW()
            WHERE jute_mr_li_id = :id
        """), {"w": new_source_weight, "p": new_source_price, "id": source_mr_li_id})
        _recompute_mr_header(conn, source_mr_id, updated_by)

        # 2. Party = source company represented in the target company's party_mst.
        party_id, party_branch_id = _ensure_company_as_party(
            conn, source_co_id, source_branch_id, target_co_id, updated_by
        )

        # 3. Insert the child jute_mr (transfer_mode=1, active stock at B).
        child_price = _round2(moved * float(rate) / 100.0)
        child_total = float(round(child_price, 0))
        child_mr_id = DatabaseConnection.execute_insert_returning_id(conn, """
            INSERT INTO jute_mr (
                jute_gate_entry_no, branch_mr_no, jute_gate_entry_date, jute_mr_date,
                status_id, transfer_mode, updated_by, updated_date_time,
                branch_id, party_id, party_branch_id, src_com_id, src_jute_mr_id,
                total_amount, claim_amount, roundoff, net_total,
                bill_pass_no, bill_pass_date
            ) VALUES (
                :gate_no, :mr_no, :mr_date, :mr_date,
                3, 1, :updated_by, NOW(),
                :branch_id, :party_id, :party_branch_id, :src_com_id, :src_jute_mr_id,
                :total, 0, 0, :total,
                :bill_pass_no, :mr_date
            )
        """, {
            "gate_no": _get_next_gate_entry_no(conn, target_branch_id),
            "mr_no": _get_next_mr_number_in_txn(conn, target_branch_id, mr_date),
            "mr_date": mr_date,
            "updated_by": updated_by,
            "branch_id": target_branch_id,
            "party_id": party_id,
            "party_branch_id": party_branch_id,
            "src_com_id": source_co_id,
            "src_jute_mr_id": source_mr_id,  # direct parent (origin, not a chain root)
            "total": child_total,
            "bill_pass_no": _get_next_bill_pass_no_in_txn(conn, target_branch_id),
        })

        # 4. Insert the child line item: moved qty at the new rate, claim-free
        #    (value of marked stock = qty * rate). Item id is remapped to the
        #    target company; quality (jute_qlty_id) is copied unchanged.
        src_item_id = r["actual_item_id"]
        target_item_id = (
            _ensure_item(conn, int(src_item_id), target_co_id, updated_by)
            if src_item_id else None
        )
        conn.execute(text("""
            INSERT INTO jute_mr_li (
                jute_mr_id, actual_item_id, actual_quality, challan_quality_id,
                accepted_weight, rate, claim_rate, total_price, warehouse_id,
                marka, crop_year, active, updated_date_time, unit_conversion
            ) VALUES (
                :mr_id, :actual_item_id, :actual_quality, :challan_quality_id,
                :w, :rate, 0, :price, :warehouse_id,
                :marka, :crop_year, 1, NOW(), :unit_conversion
            )
        """), {
            "mr_id": child_mr_id,
            "actual_item_id": target_item_id,
            "actual_quality": r["actual_quality"],
            "challan_quality_id": r["challan_quality_id"],
            "w": moved,
            "rate": float(rate),
            "price": child_price,
            "warehouse_id": warehouse_id,
            "marka": r["marka"],
            "crop_year": r["crop_year"],
            "unit_conversion": r["unit_conversion"],
        })

        return child_mr_id


def delete_marked_move(child_mr_id: int, updated_by: int) -> None:
    """Reverse a marked move: return the child's weight to the matching source
    line, recompute the source header, and delete the child MR + line item.

    Blocks if the child is itself the source of another marked move (leaf-first).
    """
    with DatabaseConnection.get_transaction() as conn:
        child = conn.execute(text("""
            SELECT jute_mr_id, src_jute_mr_id, transfer_mode
            FROM jute_mr WHERE jute_mr_id = :id FOR UPDATE
        """), {"id": child_mr_id}).fetchone()
        if not child:
            raise ValueError(f"Marked MR {child_mr_id} not found")
        c = child._mapping
        if int(c["transfer_mode"] or 0) != 1:
            raise ValueError("Not a warehouse-marked MR")

        grandchild = conn.execute(text("""
            SELECT 1 FROM jute_mr
            WHERE src_jute_mr_id = :id AND transfer_mode = 1 AND jute_mr_id <> :id
            LIMIT 1
        """), {"id": child_mr_id}).fetchone()
        if grandchild:
            raise ValueError("Delete dependent marked moves first")

        source_mr_id = c["src_jute_mr_id"]

        child_li = conn.execute(text("""
            SELECT COALESCE(SUM(accepted_weight),0) AS w, MIN(actual_quality) AS q
            FROM jute_mr_li WHERE jute_mr_id = :id
        """), {"id": child_mr_id}).fetchone()
        moved = float(child_li._mapping["w"] or 0)
        child_quality = child_li._mapping["q"]

        if source_mr_id and moved:
            # Match the source line by quality (jute_qlty_id copied unchanged);
            # fall back to the first line if quality is null.
            src_line = conn.execute(text("""
                SELECT jute_mr_li_id, accepted_weight, rate
                FROM jute_mr_li
                WHERE jute_mr_id = :id AND (actual_quality <=> :q)
                ORDER BY jute_mr_li_id LIMIT 1 FOR UPDATE
            """), {"id": int(source_mr_id), "q": child_quality}).fetchone()
            if src_line is None:
                src_line = conn.execute(text("""
                    SELECT jute_mr_li_id, accepted_weight, rate
                    FROM jute_mr_li WHERE jute_mr_id = :id
                    ORDER BY jute_mr_li_id LIMIT 1 FOR UPDATE
                """), {"id": int(source_mr_id)}).fetchone()
            if src_line is not None:
                s = src_line._mapping
                new_w = float(s["accepted_weight"] or 0) + moved
                new_p = _round2(new_w * float(s["rate"] or 0) / 100.0)
                conn.execute(text("""
                    UPDATE jute_mr_li
                    SET accepted_weight = :w, total_price = :p, updated_date_time = NOW()
                    WHERE jute_mr_li_id = :id
                """), {"w": new_w, "p": new_p, "id": int(s["jute_mr_li_id"])})
                _recompute_mr_header(conn, int(source_mr_id), updated_by)

        conn.execute(text("DELETE FROM jute_mr_li WHERE jute_mr_id = :id"), {"id": child_mr_id})
        conn.execute(text("DELETE FROM jute_mr WHERE jute_mr_id = :id"), {"id": child_mr_id})


if __name__ == "__main__":
    # Self-check: weight conservation + over-transfer / non-positive rejection.
    assert split_weights(100.0, 30.0) == (70.0, 30.0)
    assert split_weights(100.0, 100.0) == (0.0, 100.0)
    rem, mv = split_weights(50.5, 10.25)
    assert round(rem + mv, 3) == 50.5, (rem, mv)
    for bad in (0, -5, 100.001):
        try:
            split_weights(100.0, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for moved={bad}")
    print("warehouse_stock_ops self-check OK")
