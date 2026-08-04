"""Warehouse-marked stock moves (partial-quantity, no circular chain).

A "mark move" takes part of a purchased stock line at company A and moves it
into a marked godown at company B: it reduces the source line's accepted_weight
(the balance stays at A) and inserts a child jute_mr (transfer_mode=1) at B
carrying only the moved quantity at a possibly-new rate. The batch transfer is
an inter-company SALE (owner amendment 2026-08-03): one seller-side Raw-Jute
sales_invoice (invoice_type=5) is created at the source branch per child MR,
and missing masters (party, item/quality) are auto-created on both ends.

Kept deliberately separate from the vertical transfer chain (transfer.py):
no rate cascade, no finalization / return-to-origin, no chain of invoices.
The two worlds are disjoint by jute_mr.transfer_mode (0 = chain, 1 = marked
stock). The legacy single-line save_marked_move still creates no invoice
(dead code kept for compatibility; the page only calls save_marked_batch).

Run `python -m src.jutetransfer.warehouse_stock_ops` for the split_weights
self-check (no DB required).
"""

from datetime import date
from typing import Tuple

from sqlalchemy import text

from .database import DatabaseConnection
from .lot_helpers import apply_pct, line_price, reduce_amounts, restore_amounts
from .transfer import (
    RAW_JUTE_INVOICE_TYPE,
    _ensure_company_as_party,
    _ensure_item,
    _format_document_no,
    _get_next_challan_no,
    _get_next_gate_entry_no,
    _get_next_invoice_no,
    _get_next_mr_number_in_txn,
    _get_next_bill_pass_no_in_txn,
    _get_seller_prefixes,
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
    """Recompute total_amount/roundoff/net_total/mr_weight from the MR's lines.

    Mirrors transfer._update_original_mr's SUM(total_price) pattern; claim_amount
    is left as stored (matches chain behaviour). net_total = ROUND(SUM,0) - claim.
    mr_weight = ROUND(SUM(accepted_weight)), the same rule (and integer
    format) the ERP's MR-edit endpoint applies — keeps the MR-screen weight
    truthful after app-side reductions/additions.
    """
    conn.execute(text("""
        UPDATE jute_mr SET
            total_amount = (SELECT ROUND(COALESCE(SUM(total_price),0),0) FROM jute_mr_li WHERE jute_mr_id = :id),
            roundoff = (SELECT ROUND(COALESCE(SUM(total_price),0),0) FROM jute_mr_li WHERE jute_mr_id = :id) -
                       (SELECT COALESCE(SUM(total_price),0) FROM jute_mr_li WHERE jute_mr_id = :id),
            net_total = (SELECT ROUND(COALESCE(SUM(total_price),0),0) FROM jute_mr_li WHERE jute_mr_id = :id)
                        - COALESCE(claim_amount,0),
            mr_weight = (SELECT ROUND(COALESCE(SUM(accepted_weight),0)) FROM jute_mr_li WHERE jute_mr_id = :id),
            updated_by = :updated_by,
            updated_date_time = NOW()
        WHERE jute_mr_id = :id
    """), {"id": jute_mr_id, "updated_by": updated_by})


def _parent_header(conn, jute_mr_id: int) -> dict:
    """Header fields copied from a source MR onto app-created MRs so the ERP
    MR screen renders them fully (supplier, mukam, challan, vehicle...).

    Walks src_jute_mr_id ancestry to fill NULLs: legacy app-created mode-1
    parents (pre header-copy fix) have these fields empty, but their mode-0
    origin always carries them."""
    fields = ["challan_no", "mukam_id", "jute_supplier_id", "unit_conversion",
              "vehicle_no", "transporter", "driver_name"]
    out = dict.fromkeys(fields)
    mr_id, found = jute_mr_id, False
    for _ in range(10):  # ancestry is short; hard cap against id cycles
        row = conn.execute(text(f"""
            SELECT {', '.join(fields)}, src_jute_mr_id
            FROM jute_mr WHERE jute_mr_id = :id
        """), {"id": mr_id}).fetchone()
        if not row:
            break
        found = found or mr_id == jute_mr_id
        m = row._mapping
        for k in fields:
            if out[k] is None:
                out[k] = m[k]
        if all(out[k] is not None for k in fields) or m["src_jute_mr_id"] is None:
            break
        mr_id = int(m["src_jute_mr_id"])
    if not found:
        raise ValueError(f"Source MR {jute_mr_id} not found")
    return out


_BALANCE_SQL = """
    SELECT bal_weight FROM vw_jute_stock_outstanding WHERE jute_mr_li_id = :id
"""

_CHAIN_CHILD_SQL = """
    SELECT 1 FROM jute_mr
    WHERE src_jute_mr_id = :sid AND transfer_mode = 0 AND jute_mr_id <> :sid
    LIMIT 1
"""


def _available_kg(conn, li_id: int, accepted: float) -> float:
    """Balance-aware available kg: LEAST(view balance, accepted_weight).

    The view row can be missing (e.g. freshly created line inside this
    transaction) — fall back to accepted_weight.

    # ponytail: reads vw_jute_stock_outstanding without a lock, so a
    # concurrent ERP issue between this read and the caller's UPDATE can
    # shrink the true balance in between (TOCTOU). Accepted risk: queries
    # clamp with GREATEST(0), so the worst case is a stale-high available
    # figure for one save, not a negative/corrupt balance. Upgrade path if
    # this ever bites: SELECT ... FOR UPDATE on the view's underlying rows,
    # or move the check inside the same locked transaction as the UPDATE.
    """
    row = conn.execute(text(_BALANCE_SQL), {"id": li_id}).fetchone()
    bal = row._mapping["bal_weight"] if row else None
    return round(min(float(bal), accepted), 3) if bal is not None else accepted


def _reduce_source_line(conn, r: dict, qty: float, available: float,
                        keep_accepted: bool = False) -> tuple:
    """Reduce a locked source line by qty kg across accepted AND actual fields.

    keep_accepted=True (mode-1 resale) drains only the actual fields — the
    stock-view balance moves to the buyer while accepted_weight/total_price
    (and thus the holder's purchase net_total in the P&L) stay intact, exactly
    as an ERP sale of the stock would have left them.

    Returns (actual_qty_delta, actual_weight_delta) for provenance storage.
    Raises ValueError (via reduce_amounts) if actual_weight is missing or
    less than qty — moving more than the line's actual on-hand weight would
    mint balance the ERP stock view doesn't have."""
    new_accepted, new_actual_w, new_actual_q, aq_delta, aw_delta = reduce_amounts(
        r["accepted_weight"], r["actual_weight"], r["actual_qty"], qty, available
    )
    if keep_accepted:
        conn.execute(text("""
            UPDATE jute_mr_li
            SET actual_weight = :aw, actual_qty = :aq, updated_date_time = NOW()
            WHERE jute_mr_li_id = :id
        """), {
            "aw": new_actual_w,
            "aq": new_actual_q,
            "id": int(r["jute_mr_li_id"]),
        })
        return aq_delta, aw_delta
    conn.execute(text("""
        UPDATE jute_mr_li
        SET accepted_weight = :w, total_price = :p,
            actual_weight = :aw, actual_qty = :aq,
            updated_date_time = NOW()
        WHERE jute_mr_li_id = :id
    """), {
        "w": new_accepted,
        "p": line_price(new_accepted, float(r["rate"] or 0)),
        "aw": new_actual_w,
        "aq": new_actual_q,
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


def _create_marked_sales_invoice(conn, child_mr_id: int, src_mr_id: int,
                                 src_branch_id: int,
                                 buyer_party_id: int, buyer_party_branch_id,
                                 inv_lines: list, mr_date: date,
                                 updated_by: int, hdr_fallback: dict = None) -> dict:
    """Seller-side Raw-Jute invoice for one marked child MR (owner amendment
    2026-08-03): the batch transfer IS the inter-company sale, so the source
    branch bills the target company at the child's (marked-up) rates. One
    invoice per child MR; claim-free by design.

    sales_invoice_jute.mr_id stores the CHILD MR id — the deletion linkage
    for delete_marked_move. NOTE: Type 1 stores the seller MR id there; the
    differing semantics are safe because mode-1 MRs never enter a chain.
    Line data (kg/rate/price/item id) is passed in by the caller
    (save_marked_batch) — item ids are the ORIGINAL source-company ids the
    caller already had in scope, not re-derived here.
    """
    lines = inv_lines
    line_sum = round(sum(float(l["price"] or 0) for l in lines), 2)
    invoice_amount = float(round(line_sum, 0))
    round_off = round(invoice_amount - line_sum, 2)

    invoice_no = _get_next_invoice_no(conn, src_branch_id, mr_date)
    challan_no = _get_next_challan_no(conn, src_branch_id, mr_date)
    co_prefix, branch_prefix = _get_seller_prefixes(conn, src_branch_id)
    invoice_no_formatted = _format_document_no(
        invoice_no, co_prefix, branch_prefix, mr_date, document_type="SI",
    )

    src = dict(conn.execute(text("""
        SELECT branch_mr_no, mukam_id, unit_conversion
        FROM jute_mr WHERE jute_mr_id = :id
    """), {"id": src_mr_id}).fetchone()._mapping)
    # Legacy mode-1 parents (pre header-copy fix) have NULL mukam/unit; the
    # caller's ancestry-walked header fills them so the invoice prints whole.
    for k in ("mukam_id", "unit_conversion"):
        if src[k] is None and hdr_fallback:
            src[k] = hdr_fallback.get(k)

    invoice_id = DatabaseConnection.execute_insert_returning_id(conn, """
        INSERT INTO sales_invoice (
            invoice_no, invoice_date, invoice_type, invoice_amount,
            party_id, billing_to_id, shipping_to_id, branch_id,
            challan_date, challan_no,
            active, status_id, round_off, updated_by, updated_date_time
        ) VALUES (
            :invoice_no, :invoice_date, :invoice_type, :invoice_amount,
            :party_id, :billing_to_id, :shipping_to_id, :branch_id,
            :challan_date, :challan_no,
            1, 3, :round_off, :updated_by, NOW()
        )
    """, {
        "invoice_no": invoice_no,
        "invoice_date": mr_date,
        "invoice_type": RAW_JUTE_INVOICE_TYPE,
        "invoice_amount": invoice_amount,
        "party_id": buyer_party_id,
        "billing_to_id": buyer_party_branch_id,
        "shipping_to_id": buyer_party_branch_id,
        "branch_id": src_branch_id,
        "challan_date": mr_date,
        "challan_no": challan_no,
        "round_off": round_off,
        "updated_by": updated_by,
    })

    for l in lines:
        kg = float(l["kg"] or 0)
        rate_kg = round(float(l["rate"] or 0) / 100.0, 2)
        item_id = l["item_id"]
        qty = l["actual_qty"] or ""
        unit = l["unit_conversion"] or ""
        dtl_id = DatabaseConnection.execute_insert_returning_id(conn, """
            INSERT INTO sales_invoice_dtl (
                invoice_id, item_id, hsn_code, quantity, sales_weight,
                uom_id, rate, amount_without_tax, total_amount, remarks
            ) VALUES (
                :invoice_id, :item_id, NULL, :kg, :kg,
                163, :rate, :amount, :amount, :remarks
            )
        """, {
            "invoice_id": invoice_id,
            "item_id": item_id,
            "kg": kg,
            "rate": rate_kg,
            "amount": float(l["price"] or 0),
            "remarks": f"Raw Jute - {qty} {unit}".strip(),
        })
        try:
            qty_unit_conv = int(float(l["actual_qty"] or 0))
        except (TypeError, ValueError):
            qty_unit_conv = 0
        conn.execute(text("""
            INSERT INTO sales_invoice_jute_dtl (
                invoice_line_item_id, claim_desc, claim_rate,
                claim_amount_dtl, unit_conversion, qty_untit_conversion
            ) VALUES (:dtl_id, NULL, 0, 0, :unit, :qty)
        """), {"dtl_id": dtl_id, "unit": l["unit_conversion"],
               "qty": qty_unit_conv})

    conn.execute(text("""
        INSERT INTO sales_invoice_jute (
            invoice_id, mr_no, mr_id, mukam_id, claim_amount, unit_conversion
        ) VALUES (:invoice_id, :mr_no, :mr_id, :mukam_id, 0, :unit)
    """), {
        "invoice_id": invoice_id,
        "mr_no": str(src["branch_mr_no"] or ""),
        "mr_id": child_mr_id,
        "mukam_id": src["mukam_id"],
        "unit": src["unit_conversion"],
    })

    return {
        "invoice_id": invoice_id,
        "invoice_no": invoice_no,
        "invoice_no_formatted": invoice_no_formatted,
        "invoice_amount": invoice_amount,
    }


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
        chain_child = conn.execute(
            text(_CHAIN_CHILD_SQL), {"sid": source_mr_id}
        ).fetchone()
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
            "gate_no": _get_next_gate_entry_no(conn, target_branch_id, mr_date),
            "mr_no": _get_next_mr_number_in_txn(conn, target_branch_id, mr_date),
            "mr_date": mr_date,
            "updated_by": updated_by,
            "branch_id": target_branch_id,
            "party_id": party_id,
            "party_branch_id": party_branch_id,
            "src_com_id": source_co_id,
            "src_jute_mr_id": source_mr_id,  # direct parent (origin, not a chain root)
            "total": child_total,
            "bill_pass_no": _get_next_bill_pass_no_in_txn(conn, target_branch_id, mr_date),
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


def save_marked_batch(
    lot_li_ids: list,
    pct_change: float,
    target_co_id: int,
    target_branch_id: int,
    warehouse_id: int,
    mr_date: date,
    updated_by: int,
) -> list:
    """Move N whole lots into a marked godown in one transaction.

    Sources may be normal mode-0 stock or mode-1 marked stock held here
    (resale: each onward hop creates its own child MR + seller invoice at the
    current holder's branch; src_jute_mr_id chains to the direct parent, and
    delete_marked_move enforces leaf-first undo).

    Selected lines are grouped by source MR; each source MR gets ONE child MR
    (transfer_mode=1) holding its selected lines at rate * (1 + pct/100).
    The moved amount per line is the balance-aware available kg
    (LEAST(view balance, accepted_weight)) — weight already issued to
    production stays behind. Provenance rows are written to jute_lot_src so
    deletion restores sources exactly.

    Also creates one seller-side Raw-Jute sales invoice per child MR at the
    source branch (buyer = target company, auto-created in the source's
    party_mst if missing) and stamps the child MR with the formatted invoice
    no/date/amount. Returns a list of dicts:
    {"child_mr_id": int, "invoice_no": str, "invoice_amount": float}.
    """
    if not lot_li_ids:
        raise ValueError("no lots selected")
    ids = sorted({int(x) for x in lot_li_ids})

    with DatabaseConnection.get_transaction() as conn:
        rows = []
        for li_id in ids:  # ascending lock order
            row = conn.execute(text("""
                SELECT li.jute_mr_li_id, li.accepted_weight, li.rate,
                       li.actual_item_id, li.actual_quality, li.challan_quality_id,
                       li.marka, li.crop_year, li.unit_conversion,
                       li.actual_qty, li.actual_weight, li.actual_rate,
                       li.jute_mr_id, mr.branch_id AS src_branch_id,
                       mr.transfer_mode, mr.status_id, mr.src_jute_mr_id,
                       bm.co_id AS src_co_id
                FROM jute_mr_li li
                JOIN jute_mr mr ON mr.jute_mr_id = li.jute_mr_id
                JOIN branch_mst bm ON bm.branch_id = mr.branch_id
                WHERE li.jute_mr_li_id = :id
                FOR UPDATE
            """), {"id": li_id}).fetchone()
            if not row:
                raise ValueError(f"Source line {li_id} not found")
            r = dict(row._mapping)
            mode = int(r["transfer_mode"] or 0)
            if mode not in (0, 1):
                raise ValueError(
                    "Can only mark-move normal (mode 0) or marked (mode 1) stock"
                )
            if int(r["status_id"] or 0) != 3:
                raise ValueError("Can only mark-move Approved (status 3) MRs")
            # Mode-0 rows with src_jute_mr_id set are chain hops — blocked.
            # Mode-1 rows legitimately carry their direct parent there and may
            # be resold onward (each hop books its own seller invoice).
            if mode == 0 and r["src_jute_mr_id"] is not None:
                raise ValueError(
                    f"MR {int(r['jute_mr_id'])} is a chain-hop MR "
                    "(src_jute_mr_id set); re-lot disabled"
                )
            r["moved_kg"] = _available_kg(
                conn, li_id, float(r["accepted_weight"] or 0)
            )
            if r["moved_kg"] <= 0:
                raise ValueError(f"Line {li_id} has no available weight")
            rows.append(r)

        checked = set()
        for r in rows:
            mr_id = int(r["jute_mr_id"])
            if mr_id in checked:
                continue
            chain_child = conn.execute(
                text(_CHAIN_CHILD_SQL), {"sid": mr_id}
            ).fetchone()
            if chain_child:
                raise ValueError(
                    f"MR {mr_id} is part of a vertical transfer chain; "
                    "mark-move is disabled to avoid corrupting it"
                )
            checked.add(mr_id)

        by_mr = {}
        for r in rows:
            by_mr.setdefault(int(r["jute_mr_id"]), []).append(r)

        child_ids = []
        for src_mr_id in sorted(by_mr):
            grp = by_mr[src_mr_id]
            src_co_id = int(grp[0]["src_co_id"])
            src_branch_id = int(grp[0]["src_branch_id"])
            party_id, party_branch_id = _ensure_company_as_party(
                conn, src_co_id, src_branch_id, target_co_id, updated_by
            )
            # ERP-visibility columns: the MR screen hard-filters
            # out_time IS NOT NULL, the issue screen needs out_date
            # (view inward_date), and qc_check=1 keeps app rows off the
            # pending-QC list. Supplier/mukam/challan/vehicle copied from
            # the parent so the ERP detail renders fully.
            hdr = _parent_header(conn, src_mr_id)
            moved_total = round(sum(float(r["moved_kg"]) for r in grp), 3)
            child_mr_id = DatabaseConnection.execute_insert_returning_id(conn, """
                INSERT INTO jute_mr (
                    jute_gate_entry_no, branch_mr_no, jute_gate_entry_date, jute_mr_date,
                    status_id, transfer_mode, updated_by, updated_date_time,
                    branch_id, party_id, party_branch_id, src_com_id, src_jute_mr_id,
                    total_amount, claim_amount, roundoff, net_total,
                    bill_pass_no, bill_pass_date,
                    challan_no, challan_date, challan_weight,
                    gross_weight, tare_weight, net_weight, variable_shortage,
                    actual_weight, in_time, out_date, out_time, qc_check,
                    mukam_id, jute_supplier_id, unit_conversion,
                    vehicle_no, transporter, driver_name,
                    tds_amount, marketing_slip, remarks
                ) VALUES (
                    :gate_no, :mr_no, :mr_date, :mr_date,
                    3, 1, :updated_by, NOW(),
                    :branch_id, :party_id, :party_branch_id, :src_com_id, :src_jute_mr_id,
                    0, 0, 0, 0,
                    :bill_pass_no, :mr_date,
                    :challan_no, :mr_date, :moved_kg,
                    :moved_kg, 0, :moved_kg, 0,
                    :moved_kg, NOW(), :mr_date, NOW(), 1,
                    :mukam_id, :jute_supplier_id, :unit_conversion,
                    :vehicle_no, :transporter, :driver_name,
                    0, 0, :remarks
                )
            """, {
                "gate_no": _get_next_gate_entry_no(conn, target_branch_id, mr_date),
                "mr_no": _get_next_mr_number_in_txn(conn, target_branch_id, mr_date),
                "mr_date": mr_date,
                "updated_by": updated_by,
                "branch_id": target_branch_id,
                "party_id": party_id,
                "party_branch_id": party_branch_id,
                "src_com_id": src_co_id,
                "src_jute_mr_id": src_mr_id,  # direct parent (mode-1 semantics)
                "bill_pass_no": _get_next_bill_pass_no_in_txn(conn, target_branch_id, mr_date),
                "challan_no": hdr["challan_no"],
                "moved_kg": moved_total,
                "mukam_id": hdr["mukam_id"],
                "jute_supplier_id": hdr["jute_supplier_id"],
                "unit_conversion": hdr["unit_conversion"],
                "vehicle_no": hdr["vehicle_no"],
                "transporter": hdr["transporter"],
                "driver_name": hdr["driver_name"],
                "remarks": f"Inter-company transfer from MR {src_mr_id}",
            })

            inv_lines = []
            for r in grp:
                moved = float(r["moved_kg"])
                new_rate = apply_pct(float(r["rate"] or 0), pct_change)
                src_item_id = r["actual_item_id"]
                target_item_id = (
                    _ensure_item(conn, int(src_item_id), target_co_id, updated_by)
                    if src_item_id else None
                )
                aq_delta, aw_delta = _reduce_source_line(
                    conn, r, moved, moved,
                    keep_accepted=(int(r["transfer_mode"] or 0) == 1),
                )
                child_li_id = DatabaseConnection.execute_insert_returning_id(
                    conn, _LI_INSERT_SQL, {
                        "mr_id": child_mr_id,
                        "actual_item_id": target_item_id,
                        "actual_quality": r["actual_quality"],
                        "challan_quality_id": r["challan_quality_id"],
                        "w": moved,
                        "rate": new_rate,
                        "price": line_price(moved, new_rate),
                        "warehouse_id": warehouse_id,
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
                """), {"new_li": child_li_id,
                       "src_li": int(r["jute_mr_li_id"]),
                       "qty": moved, "aq": aq_delta, "aw": aw_delta,
                       "by": updated_by})
                # Original source-company item id, taken BEFORE the
                # target-company remap above — used as-is on the invoice
                # line so the invoice never round-trips through _ensure_item.
                inv_lines.append({
                    "kg": moved,
                    "rate": new_rate,
                    "price": line_price(moved, new_rate),
                    "item_id": (int(src_item_id) if src_item_id else None),
                    "actual_qty": aq_delta,
                    "unit_conversion": r["unit_conversion"],
                })

            _recompute_mr_header(conn, src_mr_id, updated_by)
            _recompute_mr_header(conn, child_mr_id, updated_by)

            # Seller-side inter-company sale (owner amendment 2026-08-03):
            # the source branch bills the target company for this child MR.
            buyer_party_id, buyer_party_branch_id = _ensure_company_as_party(
                conn, target_co_id, target_branch_id, src_co_id, updated_by
            )
            invoice = _create_marked_sales_invoice(
                conn, child_mr_id, src_mr_id, src_branch_id,
                buyer_party_id, buyer_party_branch_id, inv_lines,
                mr_date, updated_by, hdr_fallback=hdr,
            )
            conn.execute(text("""
                UPDATE jute_mr
                SET invoice_no = :ino, invoice_date = :idate,
                    invoice_amount = :iamt, updated_date_time = NOW()
                WHERE jute_mr_id = :id
            """), {"ino": invoice["invoice_no_formatted"],
                   "idate": mr_date,
                   "iamt": invoice["invoice_amount"],
                   "id": child_mr_id})
            child_ids.append({
                "child_mr_id": child_mr_id,
                "invoice_no": invoice["invoice_no_formatted"],
                "invoice_amount": invoice["invoice_amount"],
            })

        return child_ids


def delete_marked_move(child_mr_id: int, updated_by: int) -> None:
    """Reverse a marked move: delete the linked seller invoice, return the
    child's weight to the matching source line(s), recompute the source
    header(s), and delete the child MR + line items.

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

        issued = conn.execute(text("""
            SELECT 1 FROM jute_issue ji
            JOIN jute_mr_li li ON li.jute_mr_li_id = ji.jute_mr_li_id
            WHERE li.jute_mr_id = :id AND COALESCE(ji.status_id, 0) <> 4
            LIMIT 1
        """), {"id": child_mr_id}).fetchone()
        if issued:
            raise ValueError(
                "This marked MR has ERP issue entries (consumption started); "
                "cannot delete"
            )

        # Cascade-delete the seller invoice created at transfer time
        # (sales_invoice_jute.mr_id = child MR id; absent on pre-amendment
        # rows, so this is a no-op for legacy marked MRs).
        inv_rows = conn.execute(text(
            "SELECT invoice_id FROM sales_invoice_jute WHERE mr_id = :id"
        ), {"id": child_mr_id}).fetchall()
        for (inv_id,) in inv_rows:
            conn.execute(text("""
                DELETE sijd FROM sales_invoice_jute_dtl sijd
                JOIN sales_invoice_dtl sid
                  ON sid.invoice_line_item_id = sijd.invoice_line_item_id
                WHERE sid.invoice_id = :id
            """), {"id": inv_id})
            conn.execute(text(
                "DELETE FROM sales_invoice_jute WHERE invoice_id = :id"
            ), {"id": inv_id})
            conn.execute(text(
                "DELETE FROM sales_invoice_dtl WHERE invoice_id = :id"
            ), {"id": inv_id})
            conn.execute(text(
                "DELETE FROM sales_invoice WHERE invoice_id = :id"
            ), {"id": inv_id})

        source_mr_id = c["src_jute_mr_id"]

        prov = conn.execute(text("""
            SELECT ls.src_jute_mr_li_id, ls.qty_kg,
                   ls.actual_qty_delta, ls.actual_weight_delta,
                   ls.new_jute_mr_li_id
            FROM jute_lot_src ls
            JOIN jute_mr_li li ON li.jute_mr_li_id = ls.new_jute_mr_li_id
            WHERE li.jute_mr_id = :id
            FOR UPDATE
        """), {"id": child_mr_id}).fetchall()
        if prov:
            ordered_prov = sorted(
                prov, key=lambda p: int(p._mapping["src_jute_mr_li_id"])
            )

            # Lock + fetch all source lines first, and chain-guard every source
            # MR BEFORE any UPDATE runs (fail-fast; the transaction would roll
            # back anyway, but this avoids partial mutation attempts).
            srcs = {}
            for p in ordered_prov:
                m = p._mapping
                src_li_id = int(m["src_jute_mr_li_id"])
                src = conn.execute(text("""
                    SELECT li.jute_mr_li_id, li.jute_mr_id, li.accepted_weight,
                           li.rate, li.actual_qty, li.actual_weight,
                           mr.transfer_mode
                    FROM jute_mr_li li
                    JOIN jute_mr mr ON mr.jute_mr_id = li.jute_mr_id
                    WHERE li.jute_mr_li_id = :id FOR UPDATE
                """), {"id": src_li_id}).fetchone()
                if not src:
                    raise ValueError(
                        f"Source line {src_li_id} vanished; cannot restore"
                    )
                srcs[src_li_id] = src._mapping

            checked_mrs = set()
            for s in srcs.values():
                mr_id = int(s["jute_mr_id"])
                if mr_id in checked_mrs:
                    continue
                if conn.execute(text(_CHAIN_CHILD_SQL), {"sid": mr_id}).fetchone():
                    raise ValueError(
                        f"Source MR {mr_id} now feeds a vertical chain; "
                        "cannot restore weights onto it"
                    )
                checked_mrs.add(mr_id)

            src_mr_ids = set()
            for p in ordered_prov:
                m = p._mapping
                s = srcs[int(m["src_jute_mr_li_id"])]
                qty = float(m["qty_kg"] or 0)
                new_w, new_aw, new_aq = restore_amounts(
                    s["accepted_weight"], s["actual_weight"], s["actual_qty"],
                    qty, m["actual_qty_delta"], m["actual_weight_delta"],
                )
                if int(s["transfer_mode"] or 0) == 1:
                    # Resale undo: accepted/total_price were never reduced
                    # (keep_accepted) — restore only the actual fields.
                    conn.execute(text("""
                        UPDATE jute_mr_li
                        SET actual_weight = :aw, actual_qty = :aq,
                            updated_date_time = NOW()
                        WHERE jute_mr_li_id = :id
                    """), {"aw": new_aw, "aq": new_aq,
                           "id": int(s["jute_mr_li_id"])})
                else:
                    conn.execute(text("""
                        UPDATE jute_mr_li
                        SET accepted_weight = :w, total_price = :p,
                            actual_weight = :aw, actual_qty = :aq,
                            updated_date_time = NOW()
                        WHERE jute_mr_li_id = :id
                    """), {"w": new_w,
                           "p": _round2(new_w * float(s["rate"] or 0) / 100.0),
                           "aw": new_aw,
                           "aq": new_aq,
                           "id": int(s["jute_mr_li_id"])})
                src_mr_ids.add(int(s["jute_mr_id"]))
            for mr_id in sorted(src_mr_ids):
                _recompute_mr_header(conn, mr_id, updated_by)
            conn.execute(text("""
                DELETE ls FROM jute_lot_src ls
                JOIN jute_mr_li li ON li.jute_mr_li_id = ls.new_jute_mr_li_id
                WHERE li.jute_mr_id = :id
            """), {"id": child_mr_id})
            conn.execute(text("DELETE FROM jute_mr_li WHERE jute_mr_id = :id"),
                         {"id": child_mr_id})
            conn.execute(text("DELETE FROM jute_mr WHERE jute_mr_id = :id"),
                         {"id": child_mr_id})
            return

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
