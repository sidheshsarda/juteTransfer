"""Warehouse-marked stock moves (whole lots, no circular chain).

A "mark move" sells whole lots of purchased stock held at company A to a
marked godown at company B -- the batch transfer IS the inter-company sale
(owner amendment 2026-08-03). Per source MR it inserts one child jute_mr
(transfer_mode=1) at B carrying the moved kg at a possibly-new rate, and books
one seller-side Raw-Jute sales_invoice (invoice_type=5) at A's branch.
Missing masters (party, item/quality) are auto-created on both ends.

The stock leaves A exactly ONCE, the way the ERP books its own raw-jute
sales: each invoice line names the source MR line it was sold from
(sales_invoice_dtl.jute_mr_li_id), and the ERP stock ledger
vw_jute_stock_outstanding subtracts the sold weight from that line's balance
(bal_weight = actual_weight - issued - sold). The source jute_mr_li / jute_mr
rows -- a real supplier delivery: bill pass, PO position, 194Q TDS and the
purchase register read them -- are never changed by a move or its undo.

Until 2026-10-03 the move ALSO drained the source line (accepted / actual
weight, bales, price) and re-wrote the purchase header, so the ERP saw the
jute leave the seller twice (as receipt and as sale; stock report negative by
the moved weight, purchase MR at total 0 / net -claim: sls MR 28253).
Children written that way carry actual_*_delta on their jute_lot_src rows and
an invoice line that names no MR line; delete_marked_move still restores
their sources, and scripts/repair_transfer_data.py `marked-stock` converts
them to the linked model.

Kept deliberately separate from the vertical transfer chain (transfer.py):
no rate cascade, no finalization / return-to-origin, no chain of invoices.
The two worlds are disjoint by jute_mr.transfer_mode (0 = chain, 1 = marked
stock); the guard rails that keep a line out of both must stay.

Run `python -m src.jutetransfer.warehouse_stock_ops` for the pure-math
self-check (no DB required).
"""

import math
from datetime import date
from typing import Tuple

from sqlalchemy import text

from .database import DatabaseConnection, delete_by_ids, named_locks, select_ids
from .lot_helpers import (
    advised_share, apply_pct, line_price, net_rate, production_rate,
    restore_amounts, round_kg, sold_share,
)
from .transfer import (
    CHAIN_SAVE_LOCK,
    CHAIN_SAVE_LOCK_TIMEOUT,
    RAW_JUTE_INVOICE_TYPE,
    _delete_invoice,
    _ensure_company_as_party,
    _ensure_item,
    _erp_money,
    _format_document_no,
    _get_next_challan_no,
    _get_next_gate_entry_no,
    _get_next_invoice_no,
    _get_next_mr_number_in_txn,
    _get_next_bill_pass_no_in_txn,
    _get_seller_prefixes,
    _invoice_ids_for_mr,
)


def split_weights(source: float, moved: float) -> Tuple[float, float]:
    """Split a source weight into (remaining_at_source, moved_to_child).

    Raises ValueError if moved is not in (0, source]. Stock is whole kg: moved
    is rounded to a whole kg first and the remainder is whole kg too, so
    remaining + moved == round_kg(source).
    """
    moved = float(round_kg(moved))
    if moved <= 0:
        raise ValueError(f"moved qty must be > 0, got {moved}")
    if moved > source:
        raise ValueError(f"moved qty {moved} exceeds available {source}")
    return float(round_kg(source - moved)), moved


def _recompute_mr_header(conn, jute_mr_id: int, updated_by: int) -> dict:
    """Write the header the ERP itself would write for this MR's lines.

    Money as mr.recompute_mr_money: ACTIVE lines only, total = sum of
    accepted / 100 x rate and claim = sum of accepted / 100 x claim_rate (both
    2 dp), the stored 194Q TDS kept, net a whole rupee with roundoff the
    difference (transfer._erp_money). MR weight as totals.sync_mr_weight_
    from_lines: the active lines' accepted kg. A soft-deleted line (active =
    0, ERP QC edit -- the weights stay on the row) counts for nothing, as in
    the ERP. Used for app-created MRs (marked children) and for MRs whose
    lines a lot split / merge or an undo has just changed; never for the
    source of a marked move, which a move does not touch."""
    row = conn.execute(text(
        "SELECT tds_amount FROM jute_mr WHERE jute_mr_id = :id"
    ), {"id": jute_mr_id}).fetchone()
    if not row:
        raise ValueError(f"MR {jute_mr_id} not found")
    money = _erp_money(conn, jute_mr_id, tds_amount=float(row[0] or 0))
    conn.execute(text("""
        UPDATE jute_mr SET
            total_amount = :total_amount, claim_amount = :claim_amount,
            tds_amount = :tds_amount, roundoff = :roundoff, net_total = :net_total,
            mr_weight = (SELECT ROUND(COALESCE(SUM(COALESCE(accepted_weight, actual_weight, 0)), 0))
                         FROM jute_mr_li
                         WHERE jute_mr_id = :id AND (active = 1 OR active IS NULL)),
            updated_by = :updated_by,
            updated_date_time = NOW()
        WHERE jute_mr_id = :id
    """), {**money, "id": jute_mr_id, "updated_by": updated_by})
    return money


def _parent_header(conn, jute_mr_id: int) -> dict:
    """Header fields copied from a source MR onto app-created MRs so the ERP
    MR screen renders them fully (supplier, mukam, challan, vehicle...).

    Walks src_jute_mr_id ancestry to fill NULLs: legacy app-created mode-1
    parents (pre header-copy fix) have these fields empty, but their mode-0
    origin always carries them."""
    fields = ["challan_no", "challan_date", "mukam_id", "jute_supplier_id",
              "unit_conversion", "vehicle_no", "transporter", "driver_name"]
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
    """Balance-aware available kg: LEAST(ERP stock-view balance, accepted_weight),
    whole kg, FLOORED (a legacy 4811.65 kg balance offers 4811, never 4812,
    so a full take can't over-draw the view by the rounding fraction).

    The view's balance already nets ERP issues and every approved raw-jute
    sale that names the line -- including this app's own marked moves. A line
    the view does not list is NOT stock (soft-deleted line, removed line
    status, MR cancelled / rejected / returned): 0, never accepted_weight. A
    consistent read sees this transaction's own inserts, so a line created a
    moment ago is listed too.

    # ponytail: reads vw_jute_stock_outstanding without a lock, so a
    # concurrent ERP issue between this read and the caller's INSERTs can
    # shrink the true balance in between (TOCTOU) and the line ends
    # over-sold by that issue. Accepted risk, same window as the ERP's own
    # issue screen; the invoice link means it can at worst overdraw this one
    # line, never mint or double-book stock.
    """
    row = conn.execute(text(_BALANCE_SQL), {"id": li_id}).fetchone()
    if row is None or row._mapping["bal_weight"] is None:
        return 0.0
    avail = min(float(row._mapping["bal_weight"]), float(accepted or 0))
    return float(math.floor(avail + 1e-9))


def _assert_stock_line(r: dict) -> None:
    """A soft-deleted (active = 0) or removed (status 4 / 6) jute_mr_li row
    still carries its weights -- the ERP QC edit only flips the flag -- but is
    not stock. Moving or re-lotting it would mint stock that was never
    received (audit-02 V3)."""
    li_id = int(r["jute_mr_li_id"])
    if r.get("active") is not None and int(r["active"]) != 1:
        raise ValueError(f"Line {li_id} is soft-deleted in the ERP (active = 0); not stock")
    if str(r.get("status") or "") in ("4", "6"):
        raise ValueError(f"Line {li_id} is removed in the ERP (status {r['status']}); not stock")


_LI_INSERT_SQL = """
    INSERT INTO jute_mr_li (
        jute_mr_id, actual_item_id, actual_quality, challan_quality_id,
        challan_item_id, challan_weight, challan_quantity,
        allowable_moisture, actual_moisture,
        accepted_weight, rate, claim_rate, total_price, warehouse_id,
        actual_qty, actual_weight, actual_rate,
        marka, crop_year, active, updated_date_time, unit_conversion
    ) VALUES (
        :mr_id, :actual_item_id, :actual_quality, :challan_quality_id,
        :challan_item_id, :challan_weight, :challan_quantity,
        :allowable_moisture, :actual_moisture,
        :w, :rate, :claim_rate, :price, :warehouse_id,
        :actual_qty, :w, :actual_rate,
        :marka, :crop_year, 1, NOW(), :unit_conversion
    )
"""
# actual_weight = accepted kg on app-created lines, so the ERP stock view
# computes balances for them (bal = actual_weight - issued - sold). actual_rate
# is the SOURCE line's production rate (see lot_helpers.production_rate) --
# never the marked-up transfer rate, which belongs to `rate` (accounting) only.
# claim_rate: split/merge lines keep the source claim (the MR's line-level
# claim total stays whole); marked children pass 0 -- their `rate` is already
# post-claim (lot_helpers.net_rate).
# actual_qty on a marked child = the bales the stock view books as sold on
# the source line for the moved kg (lot_helpers.sold_share), so source balance
# and child add up to the source's bales.
# challan_* = the supplier's ADVISED item/weight/bales (MR print "Advised
# weight"). Marked children carry their moved share (advised_share); the
# source keeps its own -- it is the gate record, and the ERP only displays
# challan_weight (no stock/accounting sums), like the copied challan_no.
# Split/merge lines leave advised weight on the source line (0 here) so the
# MR's advised total stays whole.


def _create_marked_sales_invoice(conn, child_mr_id: int, src_mr_id: int,
                                 src_branch_id: int,
                                 buyer_party_id: int, buyer_party_branch_id,
                                 inv_lines: list, mr_date: date,
                                 updated_by: int, hdr_fallback: dict = None) -> dict:
    """Seller-side Raw-Jute invoice for one marked child MR (owner amendment
    2026-08-03): the batch transfer IS the inter-company sale, so the source
    branch bills the target company at the child's (marked-up) rates. One
    invoice per child MR; claim-free by design.

    Every invoice line names the source MR line it sells
    (sales_invoice_dtl.jute_mr_li_id = inv_lines[i]["src_li_id"]): that link
    is the stock-out. vw_jute_stock_outstanding subtracts the line's
    sales_weight from the source line's balance, exactly as for a raw-jute
    sale entered in the ERP, and the ERP stock report nets the sale against
    the receipt that stays on the purchase MR.

    sales_invoice_jute.mr_id stores the CHILD MR id -- the deletion linkage
    for delete_marked_move. NOTE: Type 1 stores the seller MR id there; the
    differing semantics are safe because mode-1 MRs never enter a chain.
    Line data (kg/rate/price/item id/source line) is passed in by the caller
    (save_marked_batch) -- item ids are the ORIGINAL source-company ids the
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
                uom_id, rate, amount_without_tax, total_amount, remarks,
                jute_mr_li_id
            ) VALUES (
                :invoice_id, :item_id, NULL, :kg, :kg,
                163, :rate, :amount, :amount, :remarks,
                :src_li_id
            )
        """, {
            "invoice_id": invoice_id,
            "item_id": item_id,
            "kg": kg,
            "rate": rate_kg,
            "amount": float(l["price"] or 0),
            "remarks": f"Raw Jute - {qty} {unit}".strip(),
            "src_li_id": int(l["src_li_id"]),
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
    (transfer_mode=1) holding its selected lines at the post-claim rate
    (rate - claim_rate) * (1 + pct/100); the child line itself is claim-free.
    The moved amount per line is the balance-aware available kg
    (LEAST(view balance, accepted_weight)) -- weight already issued to
    production or sold stays behind.

    The source lines and their MR header are NOT changed. The stock-out is
    the seller invoice: one Raw-Jute sales invoice per child MR at the source
    branch (buyer = target company, auto-created in the source's party_mst if
    missing) whose lines name the source MR lines, so the ERP stock ledger
    moves the balance to the child by itself. The child MR is stamped with
    the formatted invoice no/date/amount. One jute_lot_src row per line
    (qty_kg = moved kg, actual_*_delta NULL: nothing was taken off the
    source row) keeps the provenance. Returns a list of dicts:
    {"child_mr_id": int, "invoice_no": str, "invoice_amount": float}.
    """
    if not lot_li_ids:
        raise ValueError("no lots selected")
    ids = sorted({int(x) for x in lot_li_ids})

    # The app's own saves one at a time (see transfer.CHAIN_SAVE_LOCK): the
    # child MR / gate-entry / invoice numbers below are MAX+1 reads, and the
    # lock is taken BEFORE the transaction so those reads are fresh.
    with named_locks({CHAIN_SAVE_LOCK: CHAIN_SAVE_LOCK_TIMEOUT}), \
            DatabaseConnection.get_transaction() as conn:
        rows = []
        for li_id in ids:  # ascending lock order
            row = conn.execute(text("""
                SELECT li.jute_mr_li_id, li.accepted_weight, li.rate,
                       li.claim_rate, li.actual_item_id, li.actual_quality, li.challan_quality_id,
                       li.challan_item_id, li.challan_weight, li.challan_quantity,
                       li.allowable_moisture, li.actual_moisture,
                       li.marka, li.crop_year, li.unit_conversion,
                       li.actual_qty, li.actual_weight, li.actual_rate,
                       li.active, li.status,
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
            # Mode-0 rows with src_jute_mr_id set are chain hops -- blocked.
            # Mode-1 rows legitimately carry their direct parent there and may
            # be resold onward (each hop books its own seller invoice).
            if mode == 0 and r["src_jute_mr_id"] is not None:
                raise ValueError(
                    f"MR {int(r['jute_mr_id'])} is a chain-hop MR "
                    "(src_jute_mr_id set); re-lot disabled"
                )
            _assert_stock_line(r)
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
            moved_total = float(round_kg(sum(float(r["moved_kg"]) for r in grp)))
            for r in grp:
                r["adv_w"], r["adv_q"] = advised_share(
                    r["challan_weight"], r["challan_quantity"],
                    r["moved_kg"], r["accepted_weight"])
            # ponytail: sources with no advised data keep the old header
            # figure (moved kg) rather than showing 0
            advised_total = sum(r["adv_w"] for r in grp) or moved_total
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
                    :challan_no, :challan_date, :challan_weight,
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
                "challan_date": hdr["challan_date"] or mr_date,
                "challan_weight": advised_total,
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
                # Child + invoice are claim-free, so the source claim must be
                # netted into the rate here or the buyer pays for it.
                new_rate = apply_pct(net_rate(r), pct_change)
                src_item_id = r["actual_item_id"]
                target_item_id = (
                    _ensure_item(conn, int(src_item_id), target_co_id, updated_by)
                    if src_item_id else None
                )
                # Advised item remapped like actual_item_id (usually the same).
                challan_item_id = r["challan_item_id"]
                if challan_item_id == src_item_id:
                    challan_item_id = target_item_id
                elif challan_item_id:
                    challan_item_id = _ensure_item(
                        conn, int(challan_item_id), target_co_id, updated_by)
                # The bales that travel with the moved kg: the stock view's
                # own pro-rata of a sale against the line (sold_qty), so the
                # seller's bal_qty and this child line add up to the source's.
                moved_bales = sold_share(r["actual_qty"], r["actual_weight"], moved)
                child_li_id = DatabaseConnection.execute_insert_returning_id(
                    conn, _LI_INSERT_SQL, {
                        "mr_id": child_mr_id,
                        "actual_item_id": target_item_id,
                        "actual_quality": r["actual_quality"],
                        "challan_quality_id": r["challan_quality_id"],
                        "challan_item_id": challan_item_id,
                        "challan_weight": r["adv_w"],
                        "challan_quantity": r["adv_q"],
                        "allowable_moisture": r["allowable_moisture"],
                        "actual_moisture": r["actual_moisture"],
                        "w": moved,
                        "rate": new_rate,
                        "claim_rate": 0,
                        "actual_rate": production_rate(r),
                        "price": line_price(moved, new_rate),
                        "warehouse_id": warehouse_id,
                        "actual_qty": moved_bales,
                        "marka": r["marka"],
                        "crop_year": r["crop_year"],
                        "unit_conversion": r["unit_conversion"],
                    })
                # Provenance. NULL deltas: nothing was taken off the source
                # row -- the stock-out is the invoice line below. (Rows with
                # deltas are pre-2026-10-03 moves that drained the source.)
                conn.execute(text("""
                    INSERT INTO jute_lot_src
                        (new_jute_mr_li_id, src_jute_mr_li_id, qty_kg,
                         actual_qty_delta, actual_weight_delta,
                         created_by, created_date_time)
                    VALUES (:new_li, :src_li, :qty, NULL, NULL, :by, NOW())
                """), {"new_li": child_li_id,
                       "src_li": int(r["jute_mr_li_id"]),
                       "qty": moved, "by": updated_by})
                # Original source-company item id, taken BEFORE the
                # target-company remap above -- used as-is on the invoice
                # line so the invoice never round-trips through _ensure_item.
                inv_lines.append({
                    "kg": moved,
                    "rate": new_rate,
                    "price": line_price(moved, new_rate),
                    "item_id": (int(src_item_id) if src_item_id else None),
                    "actual_qty": moved_bales,
                    "unit_conversion": r["unit_conversion"],
                    "src_li_id": int(r["jute_mr_li_id"]),
                })

            _recompute_mr_header(conn, child_mr_id, updated_by)

            # Seller-side inter-company sale (owner amendment 2026-08-03):
            # the source branch bills the target company for this child MR;
            # each invoice line names its source MR line (the stock-out).
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


def _restore_drained_sources(conn, prov: list, updated_by: int) -> None:
    """Undo of a move written before 2026-10-03, which took the moved kg off
    the source row: put accepted / actual weight, bales and price back from
    the provenance deltas (a mode-1 source -- a resale -- had only its actual
    fields drained) and rewrite the source headers by the ERP's rule.

    Locks and chain-guards every source line / MR BEFORE the first UPDATE
    (fail-fast; the transaction would roll back anyway)."""
    ordered = sorted(prov, key=lambda m: int(m["src_jute_mr_li_id"]))
    srcs = {}
    for m in ordered:
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
            raise ValueError(f"Source line {src_li_id} vanished; cannot restore")
        srcs[src_li_id] = dict(src._mapping)

    for mr_id in sorted({int(s["jute_mr_id"]) for s in srcs.values()}):
        if conn.execute(text(_CHAIN_CHILD_SQL), {"sid": mr_id}).fetchone():
            raise ValueError(
                f"Source MR {mr_id} now feeds a vertical chain; "
                "cannot restore weights onto it"
            )

    touched = set()
    for m in ordered:
        s = srcs[int(m["src_jute_mr_li_id"])]
        qty = float(m["qty_kg"] or 0)
        new_w, new_aw, new_aq = restore_amounts(
            s["accepted_weight"], s["actual_weight"], s["actual_qty"],
            qty, m["actual_qty_delta"], m["actual_weight_delta"],
        )
        if int(s["transfer_mode"] or 0) == 1:
            # Resale undo: accepted/total_price were never reduced
            # (keep_accepted) -- restore only the actual fields.
            conn.execute(text("""
                UPDATE jute_mr_li
                SET actual_weight = :aw, actual_qty = :aq,
                    updated_date_time = NOW()
                WHERE jute_mr_li_id = :id
            """), {"aw": new_aw, "aq": new_aq, "id": int(s["jute_mr_li_id"])})
        else:
            conn.execute(text("""
                UPDATE jute_mr_li
                SET accepted_weight = :w, total_price = :p,
                    actual_weight = :aw, actual_qty = :aq,
                    updated_date_time = NOW()
                WHERE jute_mr_li_id = :id
            """), {"w": new_w,
                   "p": line_price(new_w, float(s["rate"] or 0)),
                   "aw": new_aw,
                   "aq": new_aq,
                   "id": int(s["jute_mr_li_id"])})
        touched.add(int(s["jute_mr_id"]))
    for mr_id in sorted(touched):
        _recompute_mr_header(conn, mr_id, updated_by)


def delete_marked_move(child_mr_id: int, updated_by: int) -> None:
    """Undo one marked move: delete the seller invoice booked with it -- the
    stock-out, so the source line's balance comes back by itself -- and the
    child MR with its lines and provenance. The source MR is not written.

    A child written before 2026-10-03 (its jute_lot_src rows carry
    actual_*_delta and its invoice line names no MR line: the move drained
    the source row) gets its source lines restored from the provenance and
    the source headers rewritten by the ERP's rule -- until
    scripts/repair_transfer_data.py `marked-stock` has converted it.

    Blocks, leaving nothing orphaned: a later marked move out of this child
    (leaf-first), ERP issue entries on its lines, an ERP sales invoice line
    drawn on its lines, a child without provenance.
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
        # A raw-jute sale entered in the ERP against this child's stock names
        # its lines (sales_invoice_dtl.jute_mr_li_id); deleting the lines
        # would orphan that sale. (This move's own invoice names the SOURCE
        # lines, a resale's the child's -- the leaf-first guard above covers
        # the resale.)
        sold = conn.execute(text("""
            SELECT 1 FROM sales_invoice_dtl sid
            JOIN jute_mr_li li ON li.jute_mr_li_id = sid.jute_mr_li_id
            WHERE li.jute_mr_id = :id
            LIMIT 1
        """), {"id": child_mr_id}).fetchone()
        if sold:
            raise ValueError(
                "This marked MR's stock has been sold in the ERP (a sales "
                "invoice line names it); cannot delete"
            )

        # This move's own seller invoice(s): sales_invoice_jute.mr_id = child
        # MR id (absent on pre-amendment rows: then nothing to delete).
        own_invoices = _invoice_ids_for_mr(conn, child_mr_id)
        linked_src = set()
        for inv_id in own_invoices:
            linked_src.update(select_ids(conn, """
                SELECT jute_mr_li_id FROM sales_invoice_dtl
                WHERE invoice_id = :id AND jute_mr_li_id IS NOT NULL
            """, {"id": inv_id}))

        prov = [dict(p._mapping) for p in conn.execute(text("""
            SELECT ls.lot_src_id, ls.src_jute_mr_li_id, ls.qty_kg,
                   ls.actual_qty_delta, ls.actual_weight_delta
            FROM jute_lot_src ls
            JOIN jute_mr_li li ON li.jute_mr_li_id = ls.new_jute_mr_li_id
            WHERE li.jute_mr_id = :id
            FOR UPDATE
        """), {"id": child_mr_id}).fetchall()]
        if not prov:
            raise ValueError(
                f"Marked MR {child_mr_id} has no line provenance (jute_lot_src); "
                "it predates provenance and cannot be undone by the app"
            )
        # Only a pre-2026-10-03 move took anything off the source row:
        # deltas on the provenance AND an invoice that does not name the
        # source line. Everything else is undone by deleting the invoice.
        drained = [m for m in prov
                   if m["actual_weight_delta"] is not None
                   and int(m["src_jute_mr_li_id"]) not in linked_src]
        if drained:
            _restore_drained_sources(conn, drained, updated_by)

        for inv_id in own_invoices:
            _delete_invoice(conn, inv_id)
        delete_by_ids(conn, "jute_lot_src", "lot_src_id", [m["lot_src_id"] for m in prov])
        delete_by_ids(conn, "jute_mr_li", "jute_mr_li_id", select_ids(
            conn, "SELECT jute_mr_li_id FROM jute_mr_li WHERE jute_mr_id = :id",
            {"id": child_mr_id}))
        conn.execute(text("DELETE FROM jute_mr WHERE jute_mr_id = :id"),
                     {"id": child_mr_id})


if __name__ == "__main__":
    # Self-check: weight conservation + over-transfer / non-positive rejection.
    assert split_weights(100.0, 30.0) == (70.0, 30.0)
    assert split_weights(100.0, 100.0) == (0.0, 100.0)
    # whole kg: moved rounds to 10, remainder 40.5 rounds half-up to 41
    assert split_weights(50.5, 10.25) == (41.0, 10.0)
    for bad in (0, -5, 101):
        try:
            split_weights(100.0, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for moved={bad}")
    # bales travel with the moved kg the way the stock view books a sale
    assert sold_share(66, 9700, 9700) == 66.0
    assert sold_share(66, 9700, 9312) == 63.36
    assert sold_share(0, 0, 100) == 0.0
    print("warehouse_stock_ops self-check OK")
