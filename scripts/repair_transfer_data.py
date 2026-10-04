"""Repair rows damaged by transfer-app bugs that are now fixed in code (sls only).

DRY-RUN BY DEFAULT: lists what each repair would change and writes nothing
(the planning runs in a READ ONLY transaction). Writes only with --apply,
--only <repair> and --expect <the count the dry-run showed>, after the owner
has seen the list and said yes. An --apply run prints every row it is about
to write and saves the whole plan (old and new values) in its JSON log
BEFORE the first write; every repaired row is its own transaction, re-checked
against the plan.

    python -m scripts.repair_transfer_data                 # show all repairs
    python -m scripts.repair_transfer_data --only godowns  # one repair
    python -m scripts.repair_transfer_data --apply --only finalized-net --expect 21

Repairs:
  finalized-net   Finalized chain roots whose claim_amount / net_total were
                  left NULL by finalize (the P&L read those purchases as 0).
                  Recomputes the root's money exactly as the ERP's approve
                  would have at the time (transfer._erp_recompute_money: line
                  totals, total, claim, 194Q TDS for the party, roundoff, net)
                  -- the same code finalize now runs. TDS is counted in MR-date
                  order (then MR number): only what the party had been paid
                  BEFORE a lorry is in its Rs 50 lakh count, so the first
                  Rs 50 lakh carry no TDS. (The ERP's Bill Pass save derives
                  TDS again from ALL the party's approved MRs of the year.)
                  The rows keep their updated_by / updated_date_time -- the
                  only record of when each lorry was finalized. Stops at the
                  first row that fails: later lorries count the earlier ones.
  unfinalized-party
                  Pending (13) chain roots that an earlier un-finalize left on
                  the last forwarding company as party (and with the
                  finalize's MR date): only roots whose party is one of the
                  group's own companies. Restores the party the chain derives
                  from its first step and the Pending hand-off MR date (NULL).
  godowns         Godowns whose ERP type ('J') the old "Save godown tags"
                  blanked: the ERP audit log shows J -> NULL as the godown's
                  last real type change. Sets them back to 'J'.
  marked-stock    Marked moves written before 2026-10-03, which took the moved
                  kg off the source line AND booked the seller invoice, so the
                  ERP saw the jute leave the seller twice and the purchase MR
                  was left at total 0 / net -claim (sls MR 28253). Recognised
                  by the deltas on the child's jute_lot_src rows. Per child:
                  the source lines get their accepted / actual weight, bales
                  and price back from the provenance (a mode-1 source -- a
                  resale -- only its actual fields), the touched headers are
                  rewritten by the ERP's rule (transfer._erp_money with the
                  stored TDS, MR weight from the active lines), the invoice
                  lines are made to name the source lines
                  (sales_invoice_dtl.jute_mr_li_id -- the stock-out the ERP
                  stock view then nets) and the deltas are blanked, which is
                  what save_marked_batch writes today. One transaction per
                  child; updated_by stays, updated_date_time is stamped.
  invoice-links   Chain (Type 1) raw-jute invoices written before 2026-10-03,
                  whose lines name no MR line (sales_invoice_dtl.jute_mr_li_id
                  NULL): the ERP's stock view (vw_jute_stock_outstanding)
                  never took the sale off the forwarding company, so every
                  closed chain's lorry still shows as stock there (owner
                  decision 2026-10-03, option A of the stock-view paper; today
                  the 21 finalized chains' return invoices, 43 lines). Each
                  positive invoice line is matched to exactly one active line
                  of the SELLER hop (sales_invoice_jute.mr_id) by item and kg
                  and made to name it -- what _create_sales_invoice writes
                  today. An invoice with a line that matches no hop line, or
                  more than one, is reported and left out. Zero-weight lines
                  stay unlinked. The mill's own lines are never linked. One
                  transaction per invoice; nothing else on the rows changes.
                  Scope: the finalized chains' invoices; a forward invoice of a
                  chain still out (a second hop's seller invoice -- 32 of
                  them appeared on 2026-10-03, GMPL -> MBG) is counted and
                  left out unless --open-chains is given.
  hop-tds         Chain hop MRs (the lorry as received at a forwarding
                  company) written before 2026-10-03 carry the browser's
                  totals and TDS 0. The ERP's approve would have put 194Q TDS
                  on them once the forwarder had bought Rs 50 lakh from that
                  supplier in the year (owner ruling 2026-10-03: new hops get
                  it; the old ones are LISTED here first). Rewrites each hop's
                  header by the ERP's rule (transfer._erp_money: total and
                  claim from the lines, TDS from the party's earlier approved
                  MRs in MR-date order, roundoff, net); nothing else on the
                  row changes. Stops at the first row that fails. Run only
                  after accounts have seen the list.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from src.jutetransfer.database import DatabaseConnection
from src.jutetransfer.queries import _get_financial_year_bounds, get_source_mr_full
from src.jutetransfer.lot_helpers import line_price, restore_amounts
from src.jutetransfer.transfer import (
    _derive_original_party, _erp_jute_totals, _erp_money, _erp_recompute_money,
    mr_sequence_key,
)

DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / ".logs"
UPDATED_BY = 1  # same demo user id the app writes
REPAIRS = ("finalized-net", "unfinalized-party", "godowns", "marked-stock", "invoice-links",
           "hop-tds")

# Chain roots, driven from the hop rows: jute_mr.src_jute_mr_id has no index.
_CHAIN_ROOTS_SQL = """
    SELECT DISTINCT h.src_jute_mr_id AS root_id
    FROM jute_mr h
    WHERE h.transfer_mode = 0 AND h.src_jute_mr_id IS NOT NULL
"""


def _rows(conn, sql, **params):
    return [dict(r._mapping) for r in conn.execute(text(sql), params).fetchall()]


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _write_json(path: Path, data: dict) -> None:
    """Replace the file in one step: a kill mid-write must not lose the old
    values."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=_json_default))
    os.replace(tmp, path)


def _norm(name) -> str:
    return " ".join(str(name or "").lower().replace(".", " ").split())


# ---------------------------------------------------------------------------
# Plans (read-only)
# ---------------------------------------------------------------------------

def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10]) if value else date.min


def _approval_order(mr) -> tuple:
    """The ERP's MR sequence -- MR date, then MR number, then id
    (transfer.mr_sequence_key, the Python side of the SQL rule finalize
    uses). (The 21 rows of 2026 give the same money in MR-number and in
    finalize-time order too: the Rs 50 lakh is crossed at the fifth lorry in
    each.)"""
    return mr_sequence_key(mr["jute_mr_date"], mr["branch_mr_no"], mr["jute_mr_id"])


def _approved_before(conn, mr, repaired_total: dict, peers: dict) -> float:
    """What the ERP's 194Q count stood at when this MR was finalized: the
    total_amount of the party's approved MRs of the same financial year that
    come before it (an MR repaired earlier in this run counts with its
    repaired total)."""
    if not mr["party_id"] or not mr["jute_mr_date"]:
        return 0.0
    fy_start, fy_end = _get_financial_year_bounds(_as_date(mr["jute_mr_date"]))
    key = (str(mr["party_id"]), fy_start)
    if key not in peers:
        peers[key] = _rows(conn, """
            SELECT jute_mr_id, jute_mr_date, branch_mr_no, total_amount
            FROM jute_mr
            WHERE party_id = :party AND status_id = 3
              AND jute_mr_date IS NOT NULL
              AND jute_mr_date BETWEEN :fy_start AND :fy_end
        """, party=str(mr["party_id"]), fy_start=fy_start.strftime("%Y-%m-%d"),
            fy_end=fy_end.strftime("%Y-%m-%d"))
    mine = _approval_order(mr)
    return sum(float(repaired_total.get(int(p["jute_mr_id"]), p["total_amount"]) or 0)
               for p in peers[key]
               if int(p["jute_mr_id"]) != int(mr["jute_mr_id"]) and _approval_order(p) < mine)


def plan_finalized_net(conn) -> list:
    roots = [r["root_id"] for r in _rows(conn, _CHAIN_ROOTS_SQL)]
    todo = []
    for root_id in sorted(roots):
        mr = _rows(conn, """
            SELECT jute_mr_id, status_id, branch_mr_no, party_id, jute_mr_date,
                   total_amount, claim_amount, tds_amount, roundoff, net_total,
                   updated_by, updated_date_time
            FROM jute_mr WHERE jute_mr_id = :id
        """, id=root_id)[0]
        if int(mr["status_id"] or 0) != 3 or mr["branch_mr_no"] is None:
            continue
        if mr["claim_amount"] is not None and mr["net_total"] is not None:
            continue
        todo.append(mr)
    todo.sort(key=_approval_order)          # MR-date order; also the order they are repaired in
    repaired_total, peers, out = {}, {}, []
    for mr in todo:
        before = _approved_before(conn, mr, repaired_total, peers)
        new = _erp_money(conn, mr["jute_mr_id"], cumulative_previous=before)
        repaired_total[int(mr["jute_mr_id"])] = new["total_amount"]
        out.append({
            "jute_mr_id": mr["jute_mr_id"],
            "mr_no": mr["branch_mr_no"], "mr_date": mr["jute_mr_date"],
            "party_id": mr["party_id"], "approved_before": round(before, 2),
            "old": {k: mr[k] for k in ("total_amount", "claim_amount", "tds_amount",
                                       "roundoff", "net_total")},
            "new": new,
            # left as they are: the only record of when the lorry was finalized
            "stamp": {"updated_by": mr["updated_by"], "updated_date_time": mr["updated_date_time"]},
        })
    return out


def plan_unfinalized_party(conn) -> list:
    roots = [r["root_id"] for r in _rows(conn, _CHAIN_ROOTS_SQL)]
    group = {_norm(r["co_name"]) for r in _rows(conn, "SELECT co_name FROM co_mst")}
    out = []
    for root_id in sorted(roots):
        mr = _rows(conn, """
            SELECT r.jute_mr_id, r.status_id, r.branch_mr_no, r.party_id,
                   r.party_branch_id, r.jute_mr_date, r.updated_by, r.updated_date_time,
                   p.supp_name
            FROM jute_mr r LEFT JOIN party_mst p ON p.party_id = r.party_id
            WHERE r.jute_mr_id = :id
        """, id=root_id)[0]
        if int(mr["status_id"] or 0) != 13 or mr["branch_mr_no"] is not None:
            continue
        if _norm(mr["supp_name"]) not in group:
            continue  # not on one of the group's own companies: not this bug's damage
        step1_id = _rows(conn, """
            SELECT jute_mr_id FROM jute_mr
            WHERE src_jute_mr_id = :id AND transfer_mode = 0 ORDER BY jute_mr_id LIMIT 1
        """, id=root_id)
        if not step1_id:
            continue
        step1 = get_source_mr_full(step1_id[0]["jute_mr_id"], conn=conn)
        derived = _derive_original_party(conn, root_id, step1)
        if not derived:
            continue
        party, branch = derived
        party_changes = str(mr["party_id"]).strip() != party
        if not party_changes:
            continue  # the party is right; a stray MR date alone is left alone
        name = _rows(conn, "SELECT supp_name FROM party_mst WHERE party_id = :p", p=int(party))
        out.append({
            "jute_mr_id": root_id,
            "old": {"party_id": mr["party_id"], "party_branch_id": mr["party_branch_id"],
                    "jute_mr_date": mr["jute_mr_date"], "updated_by": mr["updated_by"],
                    "updated_date_time": mr["updated_date_time"]},
            "new": {"party_id": party, "party_branch_id": branch, "jute_mr_date": None},
            "old_party_name": mr["supp_name"],
            "new_party_name": name[0]["supp_name"] if name else None,
        })
    return out


def plan_godowns(conn) -> list:
    """Godowns without a type today whose LAST real type change in the ERP's
    own audit log is J -> NULL (a later MARKED or other type would mean
    somebody chose that on purpose)."""
    rows = _rows(conn, """
        SELECT w.warehouse_id, w.branch_id, b.branch_name, c.co_name, w.warehouse_name,
               a.change_time,
               JSON_TYPE(JSON_EXTRACT(a.old_value, '$.warehouse_type')) AS old_kind,
               JSON_UNQUOTE(JSON_EXTRACT(a.old_value, '$.warehouse_type')) AS old_type,
               JSON_TYPE(JSON_EXTRACT(a.new_value, '$.warehouse_type')) AS new_kind,
               JSON_UNQUOTE(JSON_EXTRACT(a.new_value, '$.warehouse_type')) AS new_type
        FROM warehouse_mst w
        JOIN trigger_audit_logs a
          ON a.table_name = 'warehouse_mst' AND a.primary_key_value = w.warehouse_id
        LEFT JOIN branch_mst b ON b.branch_id = w.branch_id
        LEFT JOIN co_mst c ON c.co_id = b.co_id
        WHERE w.warehouse_type IS NULL
        ORDER BY w.branch_id, w.warehouse_id, a.change_time
    """)

    def kind(json_kind, value):
        return None if json_kind in (None, "NULL") else value

    last_change = {}                       # warehouse_id -> its last REAL type change
    for r in rows:
        old, new = kind(r["old_kind"], r["old_type"]), kind(r["new_kind"], r["new_type"])
        if old != new:
            last_change[r["warehouse_id"]] = (old, new, r)
    return [{
        "warehouse_id": r["warehouse_id"], "branch_id": r["branch_id"],
        "branch_name": r["branch_name"], "co_name": r["co_name"],
        "warehouse_name": r["warehouse_name"],
        "blanked_at": r["change_time"],
        "old": {"warehouse_type": None}, "new": {"warehouse_type": "J"},
    } for old, new, r in last_change.values() if old == "J" and new is None]


_HEADER_COLS = ("total_amount", "claim_amount", "tds_amount", "roundoff", "net_total", "mr_weight")
_LINE_COLS = ("accepted_weight", "actual_weight", "actual_qty", "total_price")
# the tables a marked-stock plan row touches and their primary keys
_MARKED_TABLES = {"jute_mr_li": "jute_mr_li_id", "jute_mr": "jute_mr_id",
                  "sales_invoice_dtl": "invoice_line_item_id", "jute_lot_src": "lot_src_id"}


def _num(value):
    return None if value is None else float(value)


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) <= 0.005
    except (TypeError, ValueError):
        return str(a) == str(b)


def _erp_header(lines: list, tds_amount) -> dict:
    """The header the ERP writes for these ACTIVE lines (mr.recompute_mr_money
    + totals.sync_mr_weight_from_lines), from the values given -- planned or
    stored -- not from the database."""
    total = round(sum(float(l["accepted_weight"] if l["accepted_weight"] is not None
                            else l["actual_weight"] or 0) / 100 * float(l["rate"] or 0)
                      for l in lines), 2)
    claim = round(sum(float(l["accepted_weight"] if l["accepted_weight"] is not None
                            else l["actual_weight"] or 0) / 100 * float(l["claim_rate"] or 0)
                      for l in lines), 2)
    tds = round(float(tds_amount or 0), 2)
    roundoff, net = _erp_jute_totals(total, claim, tds)
    weight = sum(float(l["accepted_weight"] if l["accepted_weight"] is not None
                       else l["actual_weight"] or 0) for l in lines)
    return {"total_amount": total, "claim_amount": claim, "tds_amount": tds,
            "roundoff": roundoff, "net_total": net, "mr_weight": float(round(weight))}


def plan_marked_stock(conn) -> list:
    """Marked children whose provenance rows carry actual_*_delta: the move
    drained the source line and the seller invoice names no MR line. One plan
    row per child, keyed by the child's jute_mr_id, with every row it changes
    under old / new as "<table>:<primary key>" -> {column: value}:
      jute_mr_li        the drained source lines, restored from the deltas
      jute_mr           every touched header (sources, the child itself),
                        rewritten by the ERP's rule -- only when it changes
      sales_invoice_dtl the child's invoice lines, naming their source line
      jute_lot_src      the deltas blanked (the provenance of a linked move)
    A child whose invoice lines cannot be matched one-to-one to its source
    lines is reported on stdout and left out.

    Rows are applied in this order, each in its own transaction, so they are
    planned cumulatively: a header two children touch (a resale's holder is
    the first move's child) is planned from every line restored so far, and
    a later row's old values are the earlier row's new ones."""
    children = _rows(conn, """
        SELECT DISTINCT li.jute_mr_id AS child_mr_id
        FROM jute_lot_src ls
        JOIN jute_mr_li li ON li.jute_mr_li_id = ls.new_jute_mr_li_id
        JOIN jute_mr mr ON mr.jute_mr_id = li.jute_mr_id
        WHERE mr.transfer_mode = 1 AND ls.actual_weight_delta IS NOT NULL
        ORDER BY li.jute_mr_id
    """)
    out = []
    restored_lines = {}                      # source line id -> planned values, all rows so far
    planned_headers = {}                     # mr id -> the header an earlier row writes
    for c in children:
        child_id = int(c["child_mr_id"])
        child = _rows(conn, """
            SELECT mr.jute_mr_id, mr.branch_id, mr.branch_mr_no, mr.jute_mr_date, mr.invoice_no,
                   mr.tds_amount, mr.total_amount, mr.claim_amount, mr.roundoff, mr.net_total,
                   mr.mr_weight, b.branch_name, co.co_name
            FROM jute_mr mr
            LEFT JOIN branch_mst b ON b.branch_id = mr.branch_id
            LEFT JOIN co_mst co ON co.co_id = b.co_id
            WHERE mr.jute_mr_id = :id
        """, id=child_id)[0]
        prov = _rows(conn, """
            SELECT ls.lot_src_id, ls.new_jute_mr_li_id, ls.src_jute_mr_li_id, ls.qty_kg,
                   ls.actual_qty_delta, ls.actual_weight_delta
            FROM jute_lot_src ls
            JOIN jute_mr_li li ON li.jute_mr_li_id = ls.new_jute_mr_li_id
            WHERE li.jute_mr_id = :id
            ORDER BY ls.lot_src_id
        """, id=child_id)
        invoices = [r["invoice_id"] for r in _rows(conn, """
            SELECT sij.invoice_id FROM sales_invoice_jute sij
            JOIN sales_invoice si ON si.invoice_id = sij.invoice_id
            WHERE sij.mr_id = :id ORDER BY sij.invoice_id
        """, id=child_id)]
        dtl = []
        for inv_id in invoices:
            dtl += _rows(conn, """
                SELECT invoice_line_item_id, invoice_id, item_id, quantity, sales_weight,
                       jute_mr_li_id
                FROM sales_invoice_dtl WHERE invoice_id = :id ORDER BY invoice_line_item_id
            """, id=inv_id)
        if not invoices:
            print(f"  marked-stock: child MR {child_id} has no seller invoice (pre-amendment "
                  "move): the drain is its only stock-out -- left as is")
            continue

        old, new, sources, notes = {}, {}, {}, []
        planned_lines = {}                    # source line id -> planned values
        for p in prov:
            if p["actual_weight_delta"] is None:
                continue                      # a linked-model line of the same child
            src_li = int(p["src_jute_mr_li_id"])
            src = _rows(conn, """
                SELECT li.jute_mr_li_id, li.jute_mr_id, li.accepted_weight, li.actual_weight,
                       li.actual_qty, li.rate, li.claim_rate, li.total_price, li.actual_item_id,
                       li.active, mr.transfer_mode
                FROM jute_mr_li li JOIN jute_mr mr ON mr.jute_mr_id = li.jute_mr_id
                WHERE li.jute_mr_li_id = :id
            """, id=src_li)
            if not src:
                notes.append(f"source line {src_li} is gone")
                continue
            src = src[0]
            qty = float(p["qty_kg"] or 0)
            new_w, new_aw, new_aq = restore_amounts(
                src["accepted_weight"], src["actual_weight"], src["actual_qty"], qty,
                p["actual_qty_delta"], p["actual_weight_delta"])
            if int(src["transfer_mode"] or 0) == 1:
                # a resale drained the holder's actual fields only (keep_accepted)
                new_w, new_price = _num(src["accepted_weight"]), _num(src["total_price"])
            else:
                new_price = line_price(new_w, float(src["rate"] or 0))
            values = {"accepted_weight": new_w, "actual_weight": new_aw, "actual_qty": new_aq,
                      "total_price": new_price}
            # the invoice line that sold this source line: same item, same kg, not yet linked
            matches = [d for d in dtl if d["jute_mr_li_id"] is None
                       and _same(d["item_id"], src["actual_item_id"]) and _same(d["quantity"], qty)]
            if len(matches) != 1:
                notes.append(f"source line {src_li}: {len(matches)} invoice line(s) match "
                             f"item {src['actual_item_id']} / {qty:g} kg")
                continue
            d = matches[0]
            dtl.remove(d)
            key = f"jute_mr_li:{src_li}"
            old[key] = {k: _num(src[k]) for k in _LINE_COLS}
            new[key] = values
            old[f"sales_invoice_dtl:{d['invoice_line_item_id']}"] = {"jute_mr_li_id": None}
            new[f"sales_invoice_dtl:{d['invoice_line_item_id']}"] = {"jute_mr_li_id": src_li}
            old[f"jute_lot_src:{p['lot_src_id']}"] = {
                "actual_qty_delta": _num(p["actual_qty_delta"]),
                "actual_weight_delta": _num(p["actual_weight_delta"])}
            new[f"jute_lot_src:{p['lot_src_id']}"] = {"actual_qty_delta": None,
                                                      "actual_weight_delta": None}
            planned_lines[src_li] = {**src, **values}
            sources.setdefault(int(src["jute_mr_id"]), []).append(src_li)
        if notes or not planned_lines:
            print(f"  marked-stock: child MR {child_id} left out: " + "; ".join(notes or ["nothing to restore"]))
            continue

        restored_lines.update(planned_lines)
        # headers: the sources (with every line restored so far) and the child
        # itself, by the ERP's rule; "old" is what the row before leaves behind
        for mr_id in sorted(sources) + [child_id]:
            hdr = _rows(conn, """
                SELECT total_amount, claim_amount, tds_amount, roundoff, net_total, mr_weight
                FROM jute_mr WHERE jute_mr_id = :id
            """, id=mr_id)[0]
            lines = _rows(conn, """
                SELECT jute_mr_li_id, accepted_weight, actual_weight, rate, claim_rate
                FROM jute_mr_li WHERE jute_mr_id = :id AND (active = 1 OR active IS NULL)
                ORDER BY jute_mr_li_id
            """, id=mr_id)
            lines = [{**l, **restored_lines.get(int(l["jute_mr_li_id"]), {})} for l in lines]
            wanted = _erp_header(lines, hdr["tds_amount"])
            current = planned_headers.get(mr_id) or {k: _num(hdr[k]) for k in _HEADER_COLS}
            if any(not _same(current[k], wanted[k]) for k in _HEADER_COLS):
                old[f"jute_mr:{mr_id}"] = current
                new[f"jute_mr:{mr_id}"] = wanted
                planned_headers[mr_id] = wanted
        out.append({
            "jute_mr_id": child_id,
            "child": {"branch_id": child["branch_id"], "branch_name": child["branch_name"],
                      "co_name": child["co_name"], "mr_no": child["branch_mr_no"],
                      "mr_date": child["jute_mr_date"], "invoice_no": child["invoice_no"],
                      "invoice_ids": invoices},
            "sources": {str(mr): lis for mr, lis in sorted(sources.items())},
            "old": old, "new": new,
        })
    return out


def _line_kg(dtl) -> float:
    """The kg an invoice line sells, as the ERP stock view reads it:
    COALESCE(NULLIF(sales_weight, 0), quantity, 0)."""
    weight = _num(dtl.get("sales_weight"))
    if not weight:
        weight = _num(dtl.get("quantity"))
    return float(weight or 0)


# invoice-links scope: the finalized chains' invoices (the owner's ruling of
# 2026-10-03 covers closed chains). --open-chains adds the forward invoices of
# chains still out (a second hop's seller invoice), which the new save links
# the same way; the dry run says how many it left out.
INVOICE_LINKS_OPEN_CHAINS = False


def plan_invoice_links(conn, include_open: bool = None) -> list:
    """Chain invoices whose lines name no MR line -- one plan row per
    invoice: the seller hop (sales_invoice_jute.mr_id, a transfer_mode-0 MR
    with a root), the chain's root and its state, and under old / new one
    "sales_invoice_dtl:<id>" -> {jute_mr_li_id} per line to link; `lines`
    lists the pairs with item and kg. A positive line is matched to the ONE
    active hop line of the same item (actual_item_id) and the same kg
    (rounded, as both were written); an invoice with a line that matches
    none or several is printed and left out. Lines already linked, and
    zero-kg lines, are left as they are. Invoices of chains awaiting return
    are planned only with include_open (INVOICE_LINKS_OPEN_CHAINS /
    --open-chains); otherwise they are counted and printed."""
    if include_open is None:
        include_open = INVOICE_LINKS_OPEN_CHAINS
    invoices = _rows(conn, """
        SELECT si.invoice_id, si.invoice_no, si.invoice_date, si.branch_id, si.status_id,
               h.jute_mr_id AS hop_id, h.src_jute_mr_id AS root_id,
               r.status_id AS root_status, r.branch_mr_no AS root_mr_no,
               r.jute_gate_entry_no AS ge_no, r.branch_id AS root_branch
        FROM jute_mr h
        JOIN sales_invoice_jute sij ON sij.mr_id = h.jute_mr_id
        JOIN sales_invoice si ON si.invoice_id = sij.invoice_id
        JOIN jute_mr r ON r.jute_mr_id = h.src_jute_mr_id
        WHERE h.transfer_mode = 0 AND h.src_jute_mr_id IS NOT NULL
          AND si.invoice_type = 5
          AND EXISTS (SELECT 1 FROM sales_invoice_dtl d
                      WHERE d.invoice_id = si.invoice_id AND d.jute_mr_li_id IS NULL)
        ORDER BY si.invoice_id
    """)
    out, open_chain = [], []
    for inv in invoices:
        invoice_id, hop_id = int(inv["invoice_id"]), int(inv["hop_id"])
        finalized = int(inv["root_status"] or 0) == 3 and inv["root_mr_no"] is not None
        if not finalized and not include_open:
            open_chain.append(inv)
            continue
        dtl = _rows(conn, """
            SELECT invoice_line_item_id, item_id, quantity, sales_weight, jute_mr_li_id
            FROM sales_invoice_dtl WHERE invoice_id = :id ORDER BY invoice_line_item_id
        """, id=invoice_id)
        pool = _rows(conn, """
            SELECT jute_mr_li_id, actual_item_id, accepted_weight
            FROM jute_mr_li WHERE jute_mr_id = :id AND (active = 1 OR active IS NULL)
            ORDER BY jute_mr_li_id
        """, id=hop_id)
        taken = {int(d["jute_mr_li_id"]) for d in dtl if d["jute_mr_li_id"] is not None}
        pool = [p for p in pool if int(p["jute_mr_li_id"]) not in taken]
        old, new, lines, notes = {}, {}, [], []
        for d in dtl:
            if d["jute_mr_li_id"] is not None:
                continue
            kg = round(_line_kg(d))
            if kg <= 0:
                continue                      # a zero-kg line (pre soft-delete fix): stays NULL
            matches = [p for p in pool
                       if _same(p["actual_item_id"], d["item_id"])
                       and round(_num(p["accepted_weight"]) or 0) == kg]
            if len(matches) != 1:
                notes.append(f"invoice line {d['invoice_line_item_id']} (item {d['item_id']}, "
                             f"{kg} kg): {len(matches)} hop line(s) match")
                continue
            m = matches[0]
            pool.remove(m)
            key = f"sales_invoice_dtl:{d['invoice_line_item_id']}"
            old[key] = {"jute_mr_li_id": None}
            new[key] = {"jute_mr_li_id": int(m["jute_mr_li_id"])}
            lines.append({"invoice_line_item_id": int(d["invoice_line_item_id"]),
                          "jute_mr_li_id": int(m["jute_mr_li_id"]),
                          "item_id": d["item_id"], "kg": kg})
        if notes or not lines:
            print(f"  invoice-links: invoice {invoice_id} (MR {hop_id}) left out: "
                  + "; ".join(notes or ["no positive line to link"]))
            continue
        out.append({
            "invoice_id": invoice_id, "invoice_no": inv["invoice_no"],
            "invoice_date": inv["invoice_date"], "branch_id": inv["branch_id"],
            "invoice_status": inv["status_id"],
            "hop_mr_id": hop_id, "root_mr_id": int(inv["root_id"]), "ge_no": inv["ge_no"],
            "root_finalized": finalized,
            "lines": lines, "old": old, "new": new,
        })
    if open_chain:
        n_lines = sum(int(_rows(conn, """
            SELECT COUNT(*) AS n FROM sales_invoice_dtl
            WHERE invoice_id = :id AND jute_mr_li_id IS NULL
              AND COALESCE(NULLIF(sales_weight, 0), quantity, 0) > 0
        """, id=int(i["invoice_id"]))[0]["n"]) for i in open_chain)
        branches = sorted({int(i["branch_id"]) for i in open_chain})
        print(f"  invoice-links: {len(open_chain)} invoice(s) of chains still awaiting return "
              f"({n_lines} unlinked line(s), seller branch(es) {branches}) NOT planned -- a second "
              "hop's seller invoice; the owner's ruling covers closed chains. --open-chains plans "
              "them too (the new save links them the same way).")
    return out


_CHAIN_HOPS_SQL = """
    SELECT h.jute_mr_id, h.branch_id, h.party_id, h.jute_mr_date, h.branch_mr_no,
           h.status_id, h.total_amount, h.claim_amount, h.tds_amount, h.roundoff, h.net_total,
           h.src_jute_mr_id AS root_id, r.jute_gate_entry_no AS ge_no, c.co_name,
           p.supp_name
    FROM jute_mr h
    JOIN jute_mr r ON r.jute_mr_id = h.src_jute_mr_id
    JOIN branch_mst b ON b.branch_id = h.branch_id
    LEFT JOIN co_mst c ON c.co_id = b.co_id
    LEFT JOIN party_mst p ON p.party_id = h.party_id
    WHERE h.transfer_mode = 0 AND h.src_jute_mr_id IS NOT NULL AND h.status_id = 3
"""
_MONEY = ("total_amount", "claim_amount", "tds_amount", "roundoff", "net_total")


def plan_hop_tds(conn) -> list:
    """Every chain hop whose header is not what the ERP's approve would have
    written, in MR-date order (a later hop's TDS counts the earlier ones with
    their repaired totals)."""
    hops = sorted(_rows(conn, _CHAIN_HOPS_SQL), key=_approval_order)
    repaired_total, peers, out = {}, {}, []
    for mr in hops:
        before = _approved_before(conn, mr, repaired_total, peers)
        new = _erp_money(conn, mr["jute_mr_id"], cumulative_previous=before)
        repaired_total[int(mr["jute_mr_id"])] = new["total_amount"]
        old = {k: mr[k] for k in _MONEY}
        if all(_same(old[k], new[k]) for k in _MONEY):
            continue
        out.append({
            "jute_mr_id": mr["jute_mr_id"], "root_mr_id": mr["root_id"], "ge_no": mr["ge_no"],
            "branch_id": mr["branch_id"], "co_name": mr["co_name"], "mr_no": mr["branch_mr_no"],
            "mr_date": mr["jute_mr_date"], "party_id": mr["party_id"], "party_name": mr["supp_name"],
            "approved_before": round(before, 2), "old": old, "new": new,
        })
    return out


PLANNERS = {
    "finalized-net": plan_finalized_net,
    "unfinalized-party": plan_unfinalized_party,
    "godowns": plan_godowns,
    "marked-stock": plan_marked_stock,
    "invoice-links": plan_invoice_links,
    "hop-tds": plan_hop_tds,
}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report(plans: dict, applying: bool = False) -> None:
    print("=" * 78)
    print("TRANSFER DATA REPAIRS — " + ("APPLYING: these rows are about to be written" if applying
                                        else "DRY RUN (nothing is written)"))
    print("=" * 78)
    if "finalized-net" in plans:
        p = plans["finalized-net"]
        print(f"\n[finalized-net] {len(p)} finalized root MR(s) with NULL claim / net total"
              " -> the ERP's own money rule (as its approve would have written it)")
        print("  TDS (194Q, 0.1%) is counted in MR-date order: nothing on the first 50,00,000"
              " bought from a party in the year.")
        print("  (An ERP Bill Pass save on one of these MRs derives its TDS again from ALL the"
              " party's approved MRs of the year.)")
        for r in p:
            o, n = r["old"], r["new"]
            print(f"  MR {r['jute_mr_id']} (MR no {r['mr_no']}, {_as_date(r['mr_date']):%d-%m-%Y}):"
                  f" total {o['total_amount']} -> {n['total_amount']:,.2f}"
                  f" | claim {o['claim_amount']} -> {n['claim_amount']:,.2f}"
                  f" | bought before {r['approved_before']:,.2f}"
                  f" | TDS {o['tds_amount']} -> {n['tds_amount']:,.2f}"
                  f" | net {o['net_total']} -> {n['net_total']:,.2f}")
        if p:
            print(f"  Purchases the P&L will now count: "
                  f"{sum(r['new']['net_total'] for r in p):,.2f} "
                  f"(TDS {sum(r['new']['tds_amount'] for r in p):,.2f})")
    if "unfinalized-party" in plans:
        p = plans["unfinalized-party"]
        print(f"\n[unfinalized-party] {len(p)} pending root MR(s) still on the forwarder as party")
        for r in p:
            print(f"  MR {r['jute_mr_id']}: party {r['old']['party_id']} "
                  f"({r['old_party_name']}) / branch {r['old']['party_branch_id']} -> "
                  f"{r['new']['party_id']} ({r['new_party_name']}) / branch "
                  f"{r['new']['party_branch_id']}; MR date {r['old']['jute_mr_date']} -> NULL")
    if "godowns" in plans:
        p = plans["godowns"]
        by_branch = {}
        for r in p:
            by_branch.setdefault(r["branch_id"], []).append(r)
        print(f"\n[godowns] {len(p)} godown(s) blanked by 'Save godown tags', back to 'J'")
        for b, rs in sorted(by_branch.items()):
            last = max(r["blanked_at"] for r in rs)
            names = ", ".join(r["warehouse_name"] for r in rs[:5])
            print(f"  {rs[0].get('co_name') or '?'} / {rs[0].get('branch_name') or '?'} (branch {b}): "
                  f"{len(rs)} (last blanked {last}) e.g. {names}" + (" ..." if len(rs) > 5 else ""))
    if "marked-stock" in plans:
        p = plans["marked-stock"]
        print(f"\n[marked-stock] {len(p)} marked child MR(s) whose move drained the source line"
              " -> source lines back from the provenance, headers by the ERP's rule,"
              " invoice lines naming the source line, deltas blanked")

        def fmt(v):
            return "NULL" if v is None else (f"{v:,.2f}" if isinstance(v, float) else str(v))

        for r in p:
            c = r["child"]
            print(f"  child MR {r['jute_mr_id']} ({c.get('co_name') or '?'} / "
                  f"{c.get('branch_name') or '?'}, MR no {c['mr_no']}, "
                  f"{_as_date(c['mr_date']):%d-%m-%Y}, invoice {c['invoice_no']}) <- source MR(s) "
                  + ", ".join(f"{mr} line(s) {lis}" for mr, lis in r["sources"].items()))
            for key in sorted(r["new"], key=lambda k: (list(_MARKED_TABLES).index(k.split(":")[0]), k)):
                table, pk = key.split(":")
                changes = ", ".join(f"{col} {fmt(r['old'][key][col])} -> {fmt(val)}"
                                    for col, val in r["new"][key].items()
                                    if not _same(r["old"][key][col], val))
                if changes:
                    print(f"      {table} {pk}: {changes}")
    if "invoice-links" in plans:
        p = plans["invoice-links"]
        n_lines = sum(len(r["lines"]) for r in p)
        print(f"\n[invoice-links] {len(p)} chain invoice(s), {n_lines} line(s) to make name the"
              " seller hop's MR line (sales_invoice_dtl.jute_mr_li_id) -> the ERP stock view"
              " then takes the sale off the forwarding company")
        for r in p:
            state = "finalized" if r["root_finalized"] else "awaiting return"
            print(f"  invoice {r['invoice_id']} (no {r['invoice_no']}, "
                  f"{_as_date(r['invoice_date']):%d-%m-%Y}, branch {r['branch_id']}): "
                  f"seller hop MR {r['hop_mr_id']}, lorry GE {r['ge_no']} of root "
                  f"{r['root_mr_id']} ({state})")
            for l in r["lines"]:
                print(f"      line {l['invoice_line_item_id']}: item {l['item_id']}, {l['kg']:g} kg"
                      f" -> MR line {l['jute_mr_li_id']}")
    if "hop-tds" in plans:
        p = plans["hop-tds"]
        tds_add = sum(float(r["new"]["tds_amount"]) - float(r["old"]["tds_amount"] or 0) for r in p)
        print(f"\n[hop-tds] {len(p)} chain hop MR(s) whose header is not the ERP's approve money"
              f" -> TDS to add {tds_add:,.2f} in total (194Q, 0.1% above 50,00,000 per supplier"
              " per forwarding company, in MR-date order)")
        by_co = {}
        for r in p:
            by_co.setdefault(r["co_name"], []).append(r)
        for co, rs in sorted(by_co.items(), key=str):
            print(f"  {co}: {len(rs)} hop(s), TDS to add "
                  f"{sum(float(r['new']['tds_amount']) - float(r['old']['tds_amount'] or 0) for r in rs):,.2f}")
        for r in p:
            o, n = r["old"], r["new"]
            print(f"  MR {r['jute_mr_id']} (GE {r['ge_no']} of root {r['root_mr_id']}, "
                  f"{_as_date(r['mr_date']):%d-%m-%Y}, {r['co_name']}, from {r['party_name']}):"
                  f" total {o['total_amount']} -> {n['total_amount']:,.2f}"
                  f" | claim {o['claim_amount']} -> {n['claim_amount']:,.2f}"
                  f" | bought before {r['approved_before']:,.2f}"
                  f" | TDS {o['tds_amount']} -> {n['tds_amount']:,.2f}"
                  f" | net {o['net_total']} -> {n['net_total']:,.2f}")
    if not applying:
        print("\nNothing was written. After the owner's OK, one repair at a time:")
        for name, items in plans.items():
            print(f"    --apply --only {name} --expect {len(items)}")
        print("Each run prints its rows and saves old and new values in "
              ".logs/jt_repair_<time>.json before it writes.")


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _repair_finalized_net(conn, it) -> None:
    cur = conn.execute(text("""
        SELECT status_id, branch_mr_no, claim_amount, net_total
        FROM jute_mr WHERE jute_mr_id = :id FOR UPDATE
    """), {"id": it["jute_mr_id"]}).fetchone()
    if (not cur or int(cur[0] or 0) != 3 or cur[1] is None
            or (cur[2] is not None and cur[3] is not None)):
        raise RuntimeError("no longer a finalized root with a NULL claim / net")
    # The planned TDS is written as planned: derived again here it would
    # count every MR approved since, not only those before this lorry.
    got = _erp_recompute_money(conn, it["jute_mr_id"], tds_amount=it["new"]["tds_amount"])
    if any(abs(float(got[k]) - float(it["new"][k])) > 0.005 for k in got):
        raise RuntimeError(f"money changed since the dry run: {got}")
    # updated_by / updated_date_time stay: they are the only record of when
    # the lorry was finalized. The JSON log is the record of this repair.


def _repair_hop_tds(conn, it) -> None:
    cur = conn.execute(text(f"""
        SELECT status_id, src_jute_mr_id, {', '.join(_MONEY)}
        FROM jute_mr WHERE jute_mr_id = :id FOR UPDATE
    """), {"id": it["jute_mr_id"]}).fetchone()
    if not cur or int(cur[0] or 0) != 3 or cur[1] is None:
        raise RuntimeError("no longer an approved chain hop")
    if any(not _same(cur[i + 2], it["old"][k]) for i, k in enumerate(_MONEY)):
        raise RuntimeError("header changed since the dry run")
    # The planned TDS is written as planned (its sequence was fixed in the plan).
    got = _erp_recompute_money(conn, it["jute_mr_id"], tds_amount=it["new"]["tds_amount"])
    if any(abs(float(got[k]) - float(it["new"][k])) > 0.005 for k in got):
        raise RuntimeError(f"money changed since the dry run: {got}")
    # updated_by / updated_date_time stay (when the hop was written).


def _repair_unfinalized_party(conn, it) -> None:
    res = conn.execute(text("""
        UPDATE jute_mr SET party_id = :party, party_branch_id = :pbr,
               jute_mr_date = NULL,
               updated_by = :by, updated_date_time = NOW()
        WHERE jute_mr_id = :id AND status_id = 13
          AND branch_mr_no IS NULL AND party_id = :old_party
    """), {"party": it["new"]["party_id"], "pbr": it["new"]["party_branch_id"],
           "by": UPDATED_BY, "id": it["jute_mr_id"],
           "old_party": str(it["old"]["party_id"])})
    if res.rowcount != 1:
        raise RuntimeError(f"expected 1 row, changed {res.rowcount} (row changed since the dry run?)")


def _repair_godown(conn, it) -> None:
    res = conn.execute(text("""
        UPDATE warehouse_mst SET warehouse_type = 'J'
        WHERE warehouse_id = :id AND warehouse_type IS NULL
    """), {"id": it["warehouse_id"]})
    if res.rowcount != 1:
        raise RuntimeError(f"expected 1 row, changed {res.rowcount} (row changed since the dry run?)")


def _repair_marked_stock(conn, it) -> None:
    """One child: every planned row is locked, checked against the plan's
    old values and then written -- source lines, headers, invoice links,
    provenance -- in this one transaction."""
    child = conn.execute(text(
        "SELECT transfer_mode FROM jute_mr WHERE jute_mr_id = :id FOR UPDATE"
    ), {"id": it["jute_mr_id"]}).fetchone()
    if not child or int(child[0] or 0) != 1:
        raise RuntimeError("no longer a marked child MR")
    order = list(_MARKED_TABLES)
    for key in sorted(it["new"], key=lambda k: (order.index(k.split(":")[0]), k)):
        table, pk_value = key.split(":")
        pk, cols = _MARKED_TABLES[table], list(it["new"][key])
        cur = conn.execute(text(
            f"SELECT {', '.join(cols)} FROM {table} WHERE {pk} = :id FOR UPDATE"
        ), {"id": int(pk_value)}).fetchone()
        if not cur:
            raise RuntimeError(f"{table} {pk_value} is gone")
        for col, now in zip(cols, cur):
            if not _same(now, it["old"][key][col]):
                raise RuntimeError(f"{table} {pk_value}.{col} changed since the dry run: "
                                   f"{now!r} (planned from {it['old'][key][col]!r})")
        sets = ", ".join(f"{col} = :{col}" for col in cols)
        if table in ("jute_mr_li", "jute_mr"):
            sets += ", updated_date_time = NOW()"
        res = conn.execute(text(f"UPDATE {table} SET {sets} WHERE {pk} = :id"),
                           {**it["new"][key], "id": int(pk_value)})
        if res.rowcount != 1:
            raise RuntimeError(f"{table} {pk_value}: expected 1 row, changed {res.rowcount}")


def _repair_invoice_links(conn, it) -> None:
    """One invoice: its header locked, every planned line locked and checked
    (still unlinked; its hop line still an active line of the seller hop),
    then made to name the hop line. Nothing else on the rows changes."""
    inv = conn.execute(text(
        "SELECT invoice_type FROM sales_invoice WHERE invoice_id = :id FOR UPDATE"
    ), {"id": it["invoice_id"]}).fetchone()
    if not inv or int(inv[0] or 0) != 5:
        raise RuntimeError("no longer a raw-jute invoice")
    for line in it["lines"]:
        dtl_id, li_id = int(line["invoice_line_item_id"]), int(line["jute_mr_li_id"])
        cur = conn.execute(text(
            "SELECT jute_mr_li_id FROM sales_invoice_dtl WHERE invoice_line_item_id = :id FOR UPDATE"
        ), {"id": dtl_id}).fetchone()
        if not cur:
            raise RuntimeError(f"invoice line {dtl_id} is gone")
        if cur[0] is not None:
            raise RuntimeError(f"invoice line {dtl_id} already names MR line {cur[0]}")
        hop_line = conn.execute(text(
            "SELECT jute_mr_id, active FROM jute_mr_li WHERE jute_mr_li_id = :id"
        ), {"id": li_id}).fetchone()
        if (not hop_line or int(hop_line[0]) != int(it["hop_mr_id"])
                or (hop_line[1] is not None and int(hop_line[1]) != 1)):
            raise RuntimeError(f"MR line {li_id} is no longer an active line of hop MR {it['hop_mr_id']}")
        res = conn.execute(text("""
            UPDATE sales_invoice_dtl SET jute_mr_li_id = :li
            WHERE invoice_line_item_id = :id AND jute_mr_li_id IS NULL
        """), {"li": li_id, "id": dtl_id})
        if res.rowcount != 1:
            raise RuntimeError(f"invoice line {dtl_id}: expected 1 row, changed {res.rowcount}")


REPAIR_FNS = {
    "finalized-net": _repair_finalized_net,
    "unfinalized-party": _repair_unfinalized_party,
    "godowns": _repair_godown,
    "marked-stock": _repair_marked_stock,
    "invoice-links": _repair_invoice_links,
    "hop-tds": _repair_hop_tds,
}


def apply(plans: dict, log_path: Path) -> int:
    """One transaction per row; the whole plan is in the log before the first
    write. A row that no longer matches the plan is rolled back and reported.
    finalized-net stops at its first failure (a later lorry's TDS counts the
    earlier ones as repaired); the other repairs go on to the next row."""
    log = {"planned": plans, "repaired": [], "failed": [], "not_attempted": []}
    _write_json(log_path, log)
    for name, items in plans.items():
        for position, it in enumerate(items):
            key = it.get("jute_mr_id") or it.get("warehouse_id") or it.get("invoice_id")
            try:
                with DatabaseConnection.get_transaction() as conn:
                    REPAIR_FNS[name](conn, it)
            except Exception as exc:
                log["failed"].append({"repair": name, **it, "error": str(exc)})
                print(f"  {name}: FAILED {key}: {exc}")
                if name in ("finalized-net", "hop-tds"):
                    rest = items[position + 1:]
                    log["not_attempted"] += [{"repair": name, **r} for r in rest]
                    print(f"  {name}: STOPPED, {len(rest)} later row(s) not attempted "
                          "(their TDS counts this lorry). Take a new dry run.")
                    _write_json(log_path, log)
                    break
            else:
                log["repaired"].append({"repair": name, **it})
                print(f"  {name}: repaired {key}")
            _write_json(log_path, log)
    print(f"\nRepaired {len(log['repaired'])}, failed {len(log['failed'])}, "
          f"not attempted {len(log['not_attempted'])}. Old and new values: {log_path}")
    return 1 if log["failed"] else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--only", choices=REPAIRS, action="append",
                    help="run only this repair (repeatable; required with --apply)")
    ap.add_argument("--expect", type=int,
                    help="with --apply: the row count the dry-run showed for these repairs")
    ap.add_argument("--open-chains", action="store_true",
                    help="invoice-links: also the forward invoices of chains still awaiting return")
    ap.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    args = ap.parse_args(argv)
    if args.apply and (not args.only or args.expect is None):
        ap.error("--apply needs --only <repair> and --expect N (the count the dry-run showed)")
    chosen = args.only or list(REPAIRS)
    global INVOICE_LINKS_OPEN_CHAINS
    INVOICE_LINKS_OPEN_CHAINS = bool(args.open_chains)

    with DatabaseConnection.get_engine().connect() as conn:
        # Read-only planning: a READ ONLY transaction, not SET SESSION (that
        # would stay on the pooled connection and fail every --apply write).
        conn.execute(text("START TRANSACTION READ ONLY"))
        try:
            plans = {name: PLANNERS[name](conn) for name in chosen}
        finally:
            conn.rollback()

    if not args.apply:
        report(plans)
        return 0
    found = sum(len(v) for v in plans.values())
    if found != args.expect:
        print(f"REFUSED: --expect {args.expect} but {found} row(s) would be repaired now; "
              "re-run the dry run and check")
        return 2
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"jt_repair_{datetime.now():%Y%m%d_%H%M%S}.json"
    report(plans, applying=True)
    print(f"\nOld and new values of every row above: {log_path}\n")
    return apply(plans, log_path)


if __name__ == "__main__":
    sys.exit(main())
