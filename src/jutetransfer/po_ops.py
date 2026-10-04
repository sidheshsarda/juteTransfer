"""Transfer PO writes for the vertical transfer chain.

Every chain transfer is backed by purchase orders in the ERP's own tables
(jute_po / jute_po_li / jute_po_status_log):

* Forwarding PO (role FORWARD) -- one per hop MR, in the forwarding company,
  on that hop's party, linked from the hop (jute_mr.po_id,
  jute_mr_li.jute_po_li_id).
* Final PO (role FINAL) -- one per finalized chain, in the origin company, on
  the last forwarding company. The root MR keeps its original ERP PO (owner
  decision 2026-10-01), so the Final PO is found through its marker, not
  through an MR.

Both are built from the MR's own lines as stored (one PO per lorry), with the
rate rounded to the closest 50 (po_helpers). They are inserted CLOSED so the
ERP never offers them for a real gate entry or lists them as outstanding.

Two phases: plan_* only READS and returns everything the PO would contain
(the backfill dry-run and the tests use it as is); create_* = plan + insert.

Every function runs on the CALLER's connection, inside the caller's
transaction -- nothing here commits. Kept apart from transfer.py (MR and
invoice writes) and never imported by the marked-stock / lot code.
"""

import os
from datetime import date, datetime
from typing import Optional

from sqlalchemy import text

from .database import DatabaseConnection, delete_by_ids, select_ids
from .po_helpers import (
    CLOSE_TYPE_TRANSFER,
    LOG_SOURCE,
    MAX_PO_VALUE,
    PO_LINE_STATUS,
    PO_STATUS_APPROVED,
    PO_STATUS_CLOSED,
    ROLE_FINAL,
    ROLE_FORWARD,
    build_marker,
    build_po_lines,
    final_close_remark,
    final_marker_like,
    format_po_no,
    forward_close_remark,
    normalise_uom,
    parse_marker,
    po_remarks,
    po_totals,
)
from .queries import _get_financial_year_bounds

DEFAULT_CHANNEL = "DOMESTIC"

_ACTIVE_LINES = "(active = 1 OR active IS NULL)"

# Default ON since 2026-10-04: the pilot was verified and the existing chains
# are backfilled by scripts/backfill_transfer_pos.py. JT_TRANSFER_PO=0 in the
# environment switches PO creation off again (deletes still remove POs).
_ENABLED_DEFAULT = "1"


_ON_VALUES = {"1", "true", "yes", "on"}


def transfer_po_enabled() -> bool:
    """Kill switch: PO creation runs only when JT_TRANSFER_PO is 1 / true /
    yes / on (any case); anything else -- 0, off, a typo such as 'fasle' --
    keeps it OFF, so a misspelling can never switch it on. Unset or empty:
    the default above, read by the same rule. Deletes always run."""
    value = (os.getenv("JT_TRANSFER_PO") or "").strip().lower() or _ENABLED_DEFAULT
    return value.strip().lower() in _ON_VALUES


# -- the ERP's PO numbering lock (vowerp3be po_numbering.py, LOCK PROTOCOL) --
# MAX(po_no)+1 per branch is read by the ERP and by this app without a unique
# key; both now take the same server-wide user lock around "read MAX +
# insert + commit". Name: jute_po_no:<schema>:<branch_id> -- '{db}' is filled
# in by database.named_locks (SELECT DATABASE()). The lock must be held from
# BEFORE the writing transaction's first read until AFTER its commit, which
# is why the callers of create_*_po (save_transfer_step, the backfill) take
# it around their transaction rather than _next_po_no itself.
PO_NO_LOCK_PREFIX = "jute_po_no"
PO_NO_LOCK_TIMEOUT = 5  # seconds, as the ERP: 0 after that = "save again"


def po_no_lock(branch_id) -> str:
    """The named lock guarding the PO number series of one branch."""
    return f"{PO_NO_LOCK_PREFIX}:{{db}}:{int(branch_id)}"


def _result(role: str, po_id=None, po_no=None, formatted: str = "",
            skipped: Optional[str] = None) -> dict:
    return {"role": role, "po_id": po_id, "po_no": po_no,
            "po_no_formatted": formatted, "skipped": skipped}


def _row(conn, sql: str, params: dict) -> Optional[dict]:
    r = conn.execute(text(sql), params).fetchone()
    return dict(r._mapping) if r else None


def _as_int(value) -> Optional[int]:
    """int for ids that may arrive as VARCHAR ('8646'), Decimal or float."""
    if value is None:
        return None
    s = str(value).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return int(s) if s.isdigit() else None


def _as_date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _load_mr(conn, jute_mr_id: int) -> Optional[dict]:
    return _row(conn, """
        SELECT jute_mr_id, branch_id, po_id, party_id, jute_supplier_id,
               mukam_id, unit_conversion, jute_mr_date, jute_gate_entry_date,
               jute_gate_entry_no, src_jute_mr_id, src_com_id, transfer_mode,
               status_id, branch_mr_no
        FROM jute_mr WHERE jute_mr_id = :id
    """, {"id": int(jute_mr_id)})


def _load_mr_lines(conn, jute_mr_id: int) -> list:
    rows = conn.execute(text(f"""
        SELECT jute_mr_li_id, actual_item_id, challan_item_id, accepted_weight,
               rate, marka, crop_year, allowable_moisture, active
        FROM jute_mr_li WHERE jute_mr_id = :id AND {_ACTIVE_LINES}
        ORDER BY jute_mr_li_id
    """), {"id": int(jute_mr_id)}).fetchall()
    return [dict(r._mapping) for r in rows]


def _load_branch(conn, branch_id) -> dict:
    row = _row(conn, """
        SELECT bm.branch_id, bm.co_id, cm.co_name, cm.co_prefix, bm.branch_prefix
        FROM branch_mst bm JOIN co_mst cm ON cm.co_id = bm.co_id
        WHERE bm.branch_id = :bid
    """, {"bid": int(branch_id)})
    if not row:
        raise ValueError(f"Branch {branch_id} not found")
    return row


def _load_source_po(conn, po_id) -> dict:
    """The origin's ERP PO a lorry was received on: the terms a transfer PO
    carries over, and its printed number for the remark. {} when the root
    has none."""
    po_id = _as_int(po_id)
    if not po_id:
        return {}
    po = _row(conn, """
        SELECT p.jute_po_id, p.po_no, p.po_date,
               p.channel_code, p.credit_term, p.delivery_days, p.jute_uom,
               cm.co_prefix, bm.branch_prefix
        FROM jute_po p
        LEFT JOIN branch_mst bm ON bm.branch_id = p.branch_id
        LEFT JOIN co_mst cm ON cm.co_id = bm.co_id
        WHERE p.jute_po_id = :id
    """, {"id": po_id})
    if not po:
        return {}
    crop = conn.execute(text(f"""
        SELECT crop_year FROM jute_po_li
        WHERE jute_po_id = :id AND {_ACTIVE_LINES} AND crop_year IS NOT NULL
        ORDER BY jute_po_li_id LIMIT 1
    """), {"id": po_id}).fetchone()
    po["crop_year"] = crop[0] if crop else None
    po["po_no_formatted"] = format_po_no(
        po.get("po_no"), po.get("co_prefix"), po.get("branch_prefix"),
        _as_date(po.get("po_date")))
    return po


def _party_name(conn, party_id) -> Optional[str]:
    party_id = _as_int(party_id)
    if not party_id:
        return None
    row = conn.execute(text(
        "SELECT supp_name FROM party_mst WHERE party_id = :pid"
    ), {"pid": party_id}).fetchone()
    return row[0] if row else None


def _db_today(conn) -> date:
    """Today by the DATABASE clock (IST, like every ERP date); this VM runs on
    UTC, so between 00:00 and 05:30 IST its date is still yesterday."""
    return _as_date(conn.execute(text("SELECT CURDATE()")).scalar()) or date.today()


def _next_po_no(conn, branch_id: int, po_date: date) -> int:
    """ERP rule (vowerp3be jutePO.py): MAX(po_no)+1 per branch within the
    Apr-Mar financial year of po_date. Run inside the transaction so a second
    PO of the same save sees the first. The caller must hold
    po_no_lock(branch_id) from before the transaction's first read (see
    database.named_locks): without it two savers read the same MAX."""
    fy_start, fy_end = _get_financial_year_bounds(po_date)
    result = conn.execute(
        text("""SELECT COALESCE(MAX(po_no), 0) AS max_no
                FROM jute_po
                WHERE branch_id = :bid
                AND po_date BETWEEN :fy_start AND :fy_end"""),
        {"bid": int(branch_id), "fy_start": fy_start.strftime("%Y-%m-%d"),
         "fy_end": fy_end.strftime("%Y-%m-%d")},
    )
    return int(result.scalar() or 0) + 1


def _has_supplier_party_triple(conn, co_id: int, supplier_id, party_id) -> bool:
    return conn.execute(text("""
        SELECT map_id FROM jute_supp_party_map
        WHERE co_id = :co AND jute_supplier_id = :sid AND party_id = :pid LIMIT 1
    """), {"co": int(co_id), "sid": supplier_id, "pid": party_id}).fetchone() is not None


def mapped_supplier(conn, co_id: int, party_id, preferred_supplier_id) -> Optional[int]:
    """Supplier under which party_id is already mapped in co_id (the MR's own
    supplier if it is one of them; that supplier when the party is mapped
    under none). Lookup only: the company's supplier/party masters are never
    extended from here. Used for the Final PO, a later hop's Forwarding PO
    and (transfer.save_transfer_step) the later hop MR itself."""
    party_id = _as_int(party_id)
    preferred = _as_int(preferred_supplier_id)
    if party_id:
        row = conn.execute(text("""
            SELECT jute_supplier_id FROM jute_supp_party_map
            WHERE co_id = :co AND party_id = :pid AND jute_supplier_id IS NOT NULL
            ORDER BY (jute_supplier_id = :pref) DESC, map_id LIMIT 1
        """), {"co": int(co_id), "pid": party_id, "pref": preferred or 0}).fetchone()
        if row:
            return int(row[0])
    return preferred


def find_final_po_ids(conn, root_mr_id: int) -> list:
    """Final PO(s) of a chain root, through the marker (the root MR itself
    keeps its original ERP PO).

    Deliberately a plain read: a locking read here would next-key-lock every
    PO of the origin branch (the scan runs on the branch index) and stall
    ERP users saving POs at the mill."""
    root = _load_mr(conn, root_mr_id)
    if not root:
        return []
    rows = conn.execute(text("""
        SELECT jute_po_id FROM jute_po
        WHERE branch_id = :bid AND internal_note LIKE :pat
        ORDER BY jute_po_id
    """), {"bid": int(root["branch_id"]),
           "pat": final_marker_like(root_mr_id)}).fetchall()
    return [int(r[0]) for r in rows]


def final_po_original(conn, root_mr_id: int) -> Optional[dict]:
    """The mill MR's header from before finalize, as remembered by its Final
    PO (party_id, party_branch_id, jute_mr_date), or None when no Final PO
    remembers it. Read BEFORE the Final PO is deleted."""
    for po_id in find_final_po_ids(conn, root_mr_id):
        row = conn.execute(text(
            "SELECT internal_note FROM jute_po WHERE jute_po_id = :id"
        ), {"id": po_id}).fetchone()
        marker = parse_marker(row[0]) if row else None
        if marker and marker.get("original") is not None:
            return marker["original"]
    return None


# ---------------------------------------------------------------------------
# Plan (read-only)
# ---------------------------------------------------------------------------

def _plan(conn, *, role: str, mr: dict, root: dict, source_po: dict,
          original: Optional[dict] = None) -> dict:
    """Common part of a plan: lines, totals, date, branch. 'skipped' is set
    when there is nothing to order (no line with accepted weight)."""
    mr_id = int(mr["jute_mr_id"])
    root_mr_id = int(root["jute_mr_id"])
    uom = normalise_uom(mr.get("unit_conversion"), source_po.get("jute_uom"))
    lines = build_po_lines(_load_mr_lines(conn, mr_id), uom,
                           default_crop_year=source_po.get("crop_year"))
    plan = {"role": role, "mr_id": mr_id, "root_mr_id": root_mr_id,
            "skipped": None, "lines": lines, "uom": uom}
    if not lines:
        plan["skipped"] = f"MR {mr_id} has no line with accepted weight"
        return plan
    weight, value = po_totals(lines)
    if value > float(MAX_PO_VALUE):
        raise ValueError(
            f"Transfer PO value {value:,.2f} for MR {mr_id} exceeds what "
            "jute_po.jute_po_value can hold"
        )
    branch = _load_branch(conn, mr["branch_id"])
    plan.update({
        "branch_id": int(mr["branch_id"]),
        "co_id": int(branch["co_id"]),
        "co_name": branch.get("co_name"),
        "co_prefix": branch.get("co_prefix"),
        "branch_prefix": branch.get("branch_prefix"),
        "po_date": (_as_date(mr.get("jute_mr_date"))
                    or _as_date(mr.get("jute_gate_entry_date")) or _db_today(conn)),
        "mukam_id": _as_int(mr.get("mukam_id")),
        "weight": weight,
        "value": value,
        "channel_code": source_po.get("channel_code") or DEFAULT_CHANNEL,
        "credit_term": None,
        "delivery_days": None,
        "source_po_id": source_po.get("jute_po_id"),
        "source_po_no": source_po.get("po_no_formatted") or None,
        "internal_note": build_marker(role, root_mr_id, mr_id,
                                      source_po.get("jute_po_id"), original),
        "map_row": None,
        # No lorry type on a transfer PO: it is one lorry's actual receipt in
        # whole bales / loose units, not "N lorries of a master capacity".
        # With a type set the ERP PO page checks the weight against that
        # capacity and paints it red when it is more than 5 % off -- which a
        # real lorry load usually is. (Lorry types are per company anyway and
        # the forwarding companies have almost none.)
        "vehicle_type_id": None,
    })
    return plan


def plan_forward_po(conn, hop_mr_id: int) -> dict:
    """Everything the Forwarding PO of a hop would contain, without writing.
    plan['skipped'] says why none would be created."""
    hop = _load_mr(conn, hop_mr_id)
    if not hop:
        raise ValueError(f"Hop MR {hop_mr_id} not found")
    if hop["src_jute_mr_id"] is None or int(hop["transfer_mode"] or 0) != 0:
        raise ValueError(f"MR {hop_mr_id} is not a vertical-chain hop")
    root_mr_id = int(hop["src_jute_mr_id"])
    if hop["po_id"] is not None:
        return {"role": ROLE_FORWARD, "mr_id": int(hop_mr_id),
                "root_mr_id": root_mr_id, "lines": [],
                "skipped": f"MR {hop_mr_id} already has PO {hop['po_id']}"}
    root = _load_mr(conn, root_mr_id)
    if not root:
        raise ValueError(f"Root MR {root_mr_id} of hop {hop_mr_id} not found")

    source_po = _load_source_po(conn, root["po_id"])
    plan = _plan(conn, role=ROLE_FORWARD, mr=hop, root=root, source_po=source_po)
    if plan["skipped"]:
        return plan

    party_id = _as_int(hop.get("party_id"))
    plan["party_id"] = party_id
    mill = _load_branch(conn, root["branch_id"])
    first_hop = _as_int(hop.get("src_com_id")) == int(mill["co_id"])
    if first_hop:
        # The PO placed ON the outside supplier: its own terms travel with it
        # (the ERP reads credit_term through jute_mr.po_id for the bill's due
        # date), and the ERP PO form resolves the party only through the
        # exact (company, supplier, party) row of jute_supp_party_map.
        supplier_id = _as_int(hop.get("jute_supplier_id"))
        plan["credit_term"] = source_po.get("credit_term")
        plan["delivery_days"] = source_po.get("delivery_days")
        if supplier_id and party_id and not _has_supplier_party_triple(
                conn, plan["co_id"], supplier_id, party_id):
            plan["map_row"] = {"co_id": plan["co_id"], "supplier_id": supplier_id,
                               "party_id": party_id}
    else:
        # A later hop buys from a sister company: no terms, and the supplier
        # is the one that sister company is already mapped under here (as on
        # the Final PO) -- never a new map row pairing the outside jute
        # supplier with a sister company.
        supplier_id = mapped_supplier(conn, plan["co_id"], party_id,
                                       hop.get("jute_supplier_id"))
    plan["supplier_id"] = supplier_id
    plan["close_remark"] = forward_close_remark(
        root.get("jute_gate_entry_no"), _as_date(root.get("jute_gate_entry_date")),
        mill.get("co_name"), plan["source_po_no"])
    return plan


def plan_final_po(conn, root_mr_id: int, original: Optional[dict] = None) -> dict:
    """Everything the Final PO of a finalized chain root would contain,
    without writing (root lines as they stand: final rates, party = the last
    forwarding company). `original` = the root's party_id / party_branch_id /
    jute_mr_date from BEFORE finalize, remembered in the marker so
    un-finalize can restore them (None when not known, e.g. a backfill)."""
    root = _load_mr(conn, root_mr_id)
    if not root:
        raise ValueError(f"Root MR {root_mr_id} not found")
    if root["src_jute_mr_id"] is not None or int(root["transfer_mode"] or 0) != 0:
        raise ValueError(f"MR {root_mr_id} is not a chain root")
    if int(root["status_id"] or 0) != 3 or root["branch_mr_no"] is None:
        # Un-finalized in the meantime (e.g. during a backfill run): a Final
        # PO now would block the real one at the next finalize.
        return {"role": ROLE_FINAL, "mr_id": int(root_mr_id),
                "root_mr_id": int(root_mr_id), "lines": [],
                "skipped": f"MR {root_mr_id} is not finalized"}
    existing = find_final_po_ids(conn, root_mr_id)
    if existing:
        return {"role": ROLE_FINAL, "mr_id": int(root_mr_id),
                "root_mr_id": int(root_mr_id), "lines": [],
                "skipped": f"MR {root_mr_id} already has final PO {existing[0]}"}

    source_po = _load_source_po(conn, root["po_id"])
    plan = _plan(conn, role=ROLE_FINAL, mr=root, root=root, source_po=source_po,
                 original=original)
    if plan["skipped"]:
        return plan
    plan["party_id"] = _as_int(root.get("party_id"))
    plan["supplier_id"] = mapped_supplier(
        conn, plan["co_id"], root.get("party_id"), root.get("jute_supplier_id"))
    plan["close_remark"] = final_close_remark(
        root.get("jute_gate_entry_no"), _as_date(root.get("jute_gate_entry_date")),
        _party_name(conn, root.get("party_id")), plan["source_po_no"])
    return plan


# ---------------------------------------------------------------------------
# Apply (writes)
# ---------------------------------------------------------------------------

def _apply(conn, plan: dict, updated_by: int) -> dict:
    """Insert what a plan describes: the map row it needs, header, lines,
    status log. Returns the result dict; result['line_ids'] holds each line's
    new jute_po_li_id in plan['lines'] order."""
    role, mr_id = plan["role"], plan["mr_id"]

    if plan.get("map_row"):
        m = plan["map_row"]
        conn.execute(text("""
            INSERT INTO jute_supp_party_map
                (co_id, jute_supplier_id, party_id, updated_by, updated_date_time)
            VALUES (:co, :sid, :pid, :updated_by, NOW())
        """), {"co": m["co_id"], "sid": m["supplier_id"], "pid": m["party_id"],
               "updated_by": updated_by})

    po_no = _next_po_no(conn, plan["branch_id"], plan["po_date"])
    formatted = format_po_no(po_no, plan.get("co_prefix"),
                             plan.get("branch_prefix"), plan["po_date"])

    po_id = int(DatabaseConnection.execute_insert_returning_id(conn, """
        INSERT INTO jute_po (
            branch_id, po_no, po_date, supplier_id, party_id, jute_mukam_id,
            jute_uom, vehicle_type_id, vehicle_quantity, weight, jute_po_value,
            channel_code, credit_term, delivery_days, remarks,
            status_id, close_type, closed_date, closed_by, close_remark,
            internal_note, updated_by, updated_date_time
        ) VALUES (
            :branch_id, :po_no, :po_date, :supplier_id, :party_id, :mukam_id,
            :uom, :vehicle_type_id, 1, :weight, :value,
            :channel_code, :credit_term, :delivery_days, :remarks,
            :status_id, :close_type, NOW(), NULL, :close_remark,
            :internal_note, :updated_by, NOW()
        )
    """, {
        "branch_id": plan["branch_id"],
        "po_no": po_no,
        "po_date": plan["po_date"],
        "supplier_id": plan.get("supplier_id"),
        "party_id": plan.get("party_id"),
        "mukam_id": plan.get("mukam_id"),
        "uom": plan["uom"],
        "vehicle_type_id": plan.get("vehicle_type_id"),
        "weight": plan["weight"],
        "value": plan["value"],
        "channel_code": plan["channel_code"],
        "credit_term": plan.get("credit_term"),
        "delivery_days": plan.get("delivery_days"),
        "remarks": po_remarks(role),
        "status_id": PO_STATUS_CLOSED,
        "close_type": CLOSE_TYPE_TRANSFER,
        "close_remark": plan["close_remark"],
        "internal_note": plan["internal_note"],
        "updated_by": updated_by,
    }))

    line_ids = []
    for l in plan["lines"]:
        line_ids.append(int(DatabaseConnection.execute_insert_returning_id(conn, """
            INSERT INTO jute_po_li (
                jute_po_id, item_id, quantity, rate, value, marka, crop_year,
                allowable_moisture, active, status_id, updated_date_time, jute_uom
            ) VALUES (
                :po_id, :item_id, :quantity, :rate, :value, :marka, :crop_year,
                :allowable_moisture, 1, :status_id, NOW(), :uom
            )
        """, {
            "po_id": po_id,
            "item_id": l["item_id"],
            "quantity": l["quantity"],
            "rate": l["rate"],
            "value": l["value"],
            "marka": l["marka"],
            "crop_year": l["crop_year"],
            "allowable_moisture": l["allowable_moisture"],
            "status_id": PO_LINE_STATUS,
            "uom": plan["uom"],
        })))

    # old_status_id = Approved: an ERP user reopening the PO lands on
    # Approved (read-only), never on Open (editable).
    conn.execute(text("""
        INSERT INTO jute_po_status_log (
            jute_po_id, old_status_id, new_status_id, close_type, source,
            trigger_ref, remark, user_id, created_at
        ) VALUES (
            :po_id, :old_status, :new_status, :close_type, :source,
            :trigger_ref, :remark, :user_id, NOW()
        )
    """), {
        "po_id": po_id,
        "old_status": PO_STATUS_APPROVED,
        "new_status": PO_STATUS_CLOSED,
        "close_type": CLOSE_TYPE_TRANSFER,
        "source": LOG_SOURCE,
        "trigger_ref": f"juteTransfer {role} MR:{mr_id}",
        "remark": plan["close_remark"],
        # closed_by / the log's user stay NULL (as on the ERP's own CLI
        # closes): the app's demo user id is not an sls user, and the ERP
        # would show "User #1". The remark says who closed it.
        "user_id": None,
    })

    out = _result(role, po_id, po_no, formatted)
    out["line_ids"] = line_ids
    return out


def create_forward_po(conn, hop_mr_id: int, updated_by: int) -> dict:
    """Forwarding PO for one chain hop MR, built from the hop's stored lines
    and linked back onto it. No-op when the hop already carries a po_id."""
    if not transfer_po_enabled():
        return _result(ROLE_FORWARD, skipped="transfer POs are switched off (JT_TRANSFER_PO=0)")
    plan = plan_forward_po(conn, hop_mr_id)
    if plan["skipped"]:
        return _result(ROLE_FORWARD, skipped=plan["skipped"])
    out = _apply(conn, plan, updated_by)
    conn.execute(text("UPDATE jute_mr SET po_id = :po WHERE jute_mr_id = :id"),
                 {"po": out["po_id"], "id": int(hop_mr_id)})
    for l, po_li_id in zip(plan["lines"], out["line_ids"]):
        conn.execute(text(
            "UPDATE jute_mr_li SET jute_po_li_id = :pl WHERE jute_mr_li_id = :id"
        ), {"pl": po_li_id, "id": l["jute_mr_li_id"]})
    return out


def create_final_po(conn, root_mr_id: int, updated_by: int,
                    original: Optional[dict] = None) -> dict:
    """Final PO at the origin company for a finalized chain. Never linked
    from the root MR. No-op when one already exists. See plan_final_po for
    `original`."""
    if not transfer_po_enabled():
        return _result(ROLE_FINAL, skipped="transfer POs are switched off (JT_TRANSFER_PO=0)")
    plan = plan_final_po(conn, root_mr_id, original)
    if plan["skipped"]:
        return _result(ROLE_FINAL, skipped=plan["skipped"])
    return _apply(conn, plan, updated_by)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def _delete_po_rows(conn, po_id: int) -> None:
    delete_by_ids(conn, "jute_po_status_log", "log_id", select_ids(
        conn, "SELECT log_id FROM jute_po_status_log WHERE jute_po_id = :id", {"id": po_id}))
    delete_by_ids(conn, "jute_po_li", "jute_po_li_id", select_ids(
        conn, "SELECT jute_po_li_id FROM jute_po_li WHERE jute_po_id = :id", {"id": po_id}))
    conn.execute(text("DELETE FROM jute_po WHERE jute_po_id = :id"), {"id": po_id})


def _refuse_if_other_mrs(conn, po_id: int, own_mr_id: Optional[int]) -> None:
    """A transfer PO that an ERP user has since booked another MR against
    cannot be hard-deleted: that MR's po_id would dangle."""
    rows = conn.execute(text("""
        SELECT jute_mr_id FROM jute_mr
        WHERE po_id = :po AND jute_mr_id <> :own LIMIT 5
    """), {"po": po_id, "own": own_mr_id or 0}).fetchall()
    if rows:
        others = ", ".join(str(int(r[0])) for r in rows)
        raise ValueError(
            f"Transfer PO {po_id} has other MR(s) linked to it in the ERP "
            f"({others}); unlink them there first"
        )


def _po_label(conn, po: dict) -> str:
    """Printed number of a PO row (for messages), falling back to its id."""
    try:
        branch = _load_branch(conn, po["branch_id"])
    except (ValueError, TypeError):
        branch = {}
    return format_po_no(po.get("po_no"), branch.get("co_prefix"),
                        branch.get("branch_prefix"),
                        _as_date(po.get("po_date"))) or f"#{po['jute_po_id']}"


def _unlink_hop(conn, hop_mr_id: int, po_id: int) -> None:
    """Clear the hop's links to po_id -- the header if it points there, and
    only the lines pointing at po_id's own lines (an ERP user may have
    re-pointed some) -- with plain reads and primary-key updates."""
    po_line_ids = set(select_ids(conn, """
        SELECT jute_po_li_id FROM jute_po_li WHERE jute_po_id = :po
    """, {"po": int(po_id)}))
    rows = conn.execute(text("""
        SELECT jute_mr_li_id, jute_po_li_id FROM jute_mr_li
        WHERE jute_mr_id = :id AND jute_po_li_id IS NOT NULL
    """), {"id": int(hop_mr_id)}).fetchall()
    line_ids = [int(r[0]) for r in rows if r[1] is not None and int(r[1]) in po_line_ids]
    if line_ids:
        conn.execute(text(
            "UPDATE jute_mr_li SET jute_po_li_id = NULL "
            f"WHERE jute_mr_li_id IN ({','.join(str(i) for i in line_ids)})"
        ))
    conn.execute(text(
        "UPDATE jute_mr SET po_id = NULL WHERE jute_mr_id = :id AND po_id = :po"
    ), {"id": int(hop_mr_id), "po": int(po_id)})


def delete_transfer_po(conn, po_id: int, role: str, mr_id: int) -> Optional[dict]:
    """Delete ONE transfer PO, and only if its marker says it is the `role`
    PO of MR `mr_id` (the hop for a Forwarding PO, the root for a Final PO)
    and no other MR is linked to it. Unlinks the hop of a Forwarding PO.
    Returns {'po_id', 'po_no_formatted'}, or None when it is not that PO
    (nothing is touched then -- not even a row lock: an ERP PO a hop was
    re-pointed to, or a dangling po_id, is only read; a locking read on it
    would hold the ERP's PO row, or a gap at the end of jute_po, for the rest
    of the delete transaction)."""
    columns = "SELECT jute_po_id, internal_note, po_no, po_date, branch_id FROM jute_po WHERE jute_po_id = :id"

    def is_ours(row) -> bool:
        marker = parse_marker(row["internal_note"]) if row else None
        return bool(marker and marker["role"] == role and marker["mr_id"] == int(mr_id))

    if not is_ours(_row(conn, columns, {"id": int(po_id)})):
        return None
    # Ours by the plain read: lock it and check again on the locked row.
    po = _row(conn, columns + " FOR UPDATE", {"id": int(po_id)})
    if not is_ours(po):
        return None
    own = int(mr_id) if role == ROLE_FORWARD else None
    _refuse_if_other_mrs(conn, int(po_id), own)
    label = _po_label(conn, po)
    if role == ROLE_FORWARD:
        _unlink_hop(conn, int(mr_id), int(po_id))
    _delete_po_rows(conn, int(po_id))
    return {"po_id": int(po_id), "po_no_formatted": label}


def find_forward_po_ids(conn, hop_mr_id: int) -> list:
    """Forwarding PO(s) of a hop: the one it links to, plus any PO in the
    hop's branch whose marker names it (left unlinked if the ERP re-pointed
    the hop) -- plain reads."""
    hop = _load_mr(conn, hop_mr_id)
    if not hop:
        return []
    ids = []
    linked = _as_int(hop["po_id"])
    if linked:
        ids.append(linked)
    if hop["src_jute_mr_id"] is not None:
        pattern = (f"JT|{ROLE_FORWARD}|root={int(hop['src_jute_mr_id'])}"
                   f"|mr={int(hop_mr_id)}|%")
        ids += select_ids(conn, """
            SELECT jute_po_id FROM jute_po
            WHERE branch_id = :bid AND internal_note LIKE :pat
        """, {"bid": int(hop["branch_id"]), "pat": pattern})
    return sorted(set(ids))


def delete_forward_po(conn, hop_mr_id: int) -> Optional[dict]:
    """Remove the Forwarding PO of a hop that is about to be deleted. Touches
    only a PO whose marker says it was created for this very hop (an ERP PO
    linked by hand is left alone). Returns {'po_id', 'po_no_formatted'} of
    the deleted PO (the last one if there were several), or None."""
    deleted = None
    for po_id in find_forward_po_ids(conn, hop_mr_id):
        deleted = delete_transfer_po(conn, po_id, ROLE_FORWARD, hop_mr_id) or deleted
    return deleted


def delete_final_po(conn, root_mr_id: int) -> list:
    """Remove the Final PO(s) of a chain root (un-finalize). Returns a
    {'po_id', 'po_no_formatted'} dict per deleted PO."""
    deleted = []
    for po_id in find_final_po_ids(conn, root_mr_id):
        gone = delete_transfer_po(conn, po_id, ROLE_FINAL, root_mr_id)
        if gone:
            deleted.append(gone)
    return deleted
