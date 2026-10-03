"""Read queries for transfer POs (chain step cards and the PO tracker).

Read-only. Writes live in po_ops.py."""

from datetime import date
from typing import Optional

import pandas as pd
import streamlit as st
from sqlalchemy import text

from .database import DatabaseConnection
from .po_helpers import final_marker_like, format_po_no, parse_marker


def get_final_transfer_po(root_mr_id: int) -> Optional[dict]:
    """The FINAL PO written when a chain was finalized, or None.

    The root MR keeps its original ERP PO, so the final PO is found through
    its marker in jute_po.internal_note (see po_helpers.build_marker)."""
    df = DatabaseConnection.execute_query(
        """
        SELECT p.jute_po_id, p.po_no, p.po_date, p.weight, p.jute_po_value,
               cm.co_prefix, bm.branch_prefix
        FROM jute_mr r
        JOIN jute_po p ON p.branch_id = r.branch_id
                      AND p.internal_note LIKE :pat
        JOIN branch_mst bm ON bm.branch_id = p.branch_id
        JOIN co_mst cm ON cm.co_id = bm.co_id
        WHERE r.jute_mr_id = :root_id
        ORDER BY p.jute_po_id
        LIMIT 1
        """,
        {"root_id": int(root_mr_id), "pat": final_marker_like(root_mr_id)},
    )
    if df is None or df.empty:
        return None
    row = df.iloc[0].to_dict()
    row["po_no_formatted"] = format_po_no(
        row.get("po_no"), row.get("co_prefix"), row.get("branch_prefix"),
        None if pd.isna(row.get("po_date")) else row.get("po_date"),
    )
    return row


# ---------------------------------------------------------------------------
# PO Tracker (pages/po_tracker.py) -- SELECT only, cached for a minute
# ---------------------------------------------------------------------------

# jute_mr.src_jute_mr_id has no index. Every chain query therefore starts
# from the hop rows (one scan of jute_mr, ~0.2 s) and joins the root by
# primary key; the other way round -- root first, children through
# src_jute_mr_id -- ran past 120 s on the live server.
_HOP_ROWS = "h.transfer_mode = 0 AND h.src_jute_mr_id IS NOT NULL"

# The period a lorry belongs to is its gate-entry date at the mill, so a
# month shows the same lorries here and on the Transfer Chain page.
_LORRY_DATE = "COALESCE(r.jute_gate_entry_date, r.jute_mr_date, h.jute_mr_date)"

TRACKER_CACHE_TTL = 60  # seconds


def _read(conn, sql: str, params: Optional[dict] = None) -> pd.DataFrame:
    return pd.read_sql_query(text(sql), conn, params=params or {})


def _id_list(ids) -> str:
    """Integers for an IN (...) list ('' when there are none). jute_po_li has
    no index on jute_po_id, so lines are always fetched for all POs at once."""
    return ",".join(str(i) for i in sorted({int(i) for i in ids}))


@st.cache_data(ttl=TRACKER_CACHE_TTL, show_spinner=False)
def get_tracker_index() -> dict:
    """Every chain hop of every financial year, lightly: what the tracker's
    Mill and Period controls are built from, and what tells an orphan
    transfer PO from one whose lorry is in another year.

    Returns {'chains': DataFrame(hop_mr_id, root_mr_id, fwd_po_id,
    lorry_date, mill_co_id, mill_prefix, mill_name), 'today': date}.
    'today' is the DATABASE's date: the server clock is IST, this VM is UTC,
    so between 00:00 and 05:30 IST the VM is still on yesterday."""
    with DatabaseConnection.get_connection() as conn:
        chains = _read(conn, f"""
            SELECT h.jute_mr_id AS hop_mr_id,
                   h.src_jute_mr_id AS root_mr_id,
                   h.po_id AS fwd_po_id,
                   {_LORRY_DATE} AS lorry_date,
                   rc.co_id AS mill_co_id,
                   rc.co_prefix AS mill_prefix,
                   rc.co_name AS mill_name,
                   CURDATE() AS db_today
            FROM jute_mr h
            JOIN jute_mr r ON r.jute_mr_id = h.src_jute_mr_id
            LEFT JOIN branch_mst rb ON rb.branch_id = r.branch_id
            LEFT JOIN co_mst rc ON rc.co_id = rb.co_id
            WHERE {_HOP_ROWS}
        """)
        if chains.empty:
            today = _read(conn, "SELECT CURDATE() AS db_today")["db_today"].iloc[0]
        else:
            today = chains["db_today"].iloc[0]
    return {"chains": chains.drop(columns=["db_today"]), "today": today}


@st.cache_data(ttl=TRACKER_CACHE_TTL, show_spinner="Loading purchase orders…")
def load_tracker_data(fy_start_year: int) -> dict:
    """Everything the PO Tracker shows for one financial year (April of
    `fy_start_year` to the following March, by the lorry's gate-entry date),
    read once; every filter, search and row selection is pandas on this.

    Returns frames:
      hops         one row per hop MR, with its root MR, the root's Original
                   PO header and the hop's Forwarding PO header
      transfer_pos every jute_po carrying a JT| marker (all years): gives the
                   Final PO per root, duplicates and orphans
      mr_lines     jute_mr_li of every root and hop above (all, with `active`)
      po_lines     jute_po_li of every Original, Forwarding and Final PO
      siblings     every MR with a gate-entry number on those Original POs
                   (what the ERP's PO list counts as 'Lorries Received')
    """
    fy = int(fy_start_year)
    params = {"fy_start": date(fy, 4, 1).isoformat(),
              "fy_end": date(fy + 1, 3, 31).isoformat()}
    with DatabaseConnection.get_connection() as conn:
        hops = _read(conn, f"""
            SELECT
                h.jute_mr_id AS hop_mr_id,
                h.src_jute_mr_id AS root_mr_id,
                {_LORRY_DATE} AS lorry_date,
                h.branch_mr_no AS hop_mr_no,
                h.jute_mr_date AS hop_mr_date,
                h.po_id AS fwd_po_id,
                hc.co_id AS hop_co_id,
                hc.co_prefix AS hop_co_prefix,
                hc.co_name AS hop_co_name,
                hp.supp_name AS hop_party_name,
                r.status_id AS root_status_id,
                rs.status_name AS root_status_name,
                r.po_id AS orig_po_id,
                r.jute_gate_entry_no AS ge_no,
                r.jute_gate_entry_date AS ge_date,
                r.jute_mr_date AS root_mr_date,
                r.branch_mr_no AS root_mr_no,
                r.vehicle_no,
                r.invoice_no,
                r.invoice_amount,
                rc.co_id AS mill_co_id,
                rc.co_prefix AS mill_prefix,
                rc.co_name AS mill_name,
                s.supplier_name AS broker_name,
                rp.supp_name AS root_party_name,
                op.po_no AS orig_po_no,
                op.po_date AS orig_po_date,
                op.status_id AS orig_po_status_id,
                op.close_type AS orig_po_close_type,
                op.weight AS orig_po_weight,
                op.jute_po_value AS orig_po_value,
                op.vehicle_quantity AS orig_po_lorries,
                ops.supplier_name AS orig_po_supplier_name,
                opp.supp_name AS orig_po_party_name,
                opc.co_prefix AS orig_po_co_prefix,
                opb.branch_prefix AS orig_po_branch_prefix,
                fp.jute_po_id AS fwd_po_row_id,
                fp.po_no AS fwd_po_no,
                fp.po_date AS fwd_po_date,
                fp.status_id AS fwd_po_status_id,
                fp.close_type AS fwd_po_close_type,
                fp.weight AS fwd_po_weight,
                fp.jute_po_value AS fwd_po_value,
                fp.jute_uom AS fwd_po_uom,
                fp.internal_note AS fwd_po_note,
                fpc.co_prefix AS fwd_po_co_prefix,
                fpb.branch_prefix AS fwd_po_branch_prefix
            FROM jute_mr h
            JOIN jute_mr r ON r.jute_mr_id = h.src_jute_mr_id
            LEFT JOIN branch_mst hb ON hb.branch_id = h.branch_id
            LEFT JOIN co_mst hc ON hc.co_id = hb.co_id
            LEFT JOIN party_mst hp ON hp.party_id = h.party_id
            LEFT JOIN branch_mst rb ON rb.branch_id = r.branch_id
            LEFT JOIN co_mst rc ON rc.co_id = rb.co_id
            LEFT JOIN status_mst rs ON rs.status_id = r.status_id
            LEFT JOIN jute_supplier_mst s ON s.supplier_id = r.jute_supplier_id
            LEFT JOIN party_mst rp ON rp.party_id = r.party_id
            LEFT JOIN jute_po op ON op.jute_po_id = r.po_id
            LEFT JOIN jute_supplier_mst ops ON ops.supplier_id = op.supplier_id
            LEFT JOIN party_mst opp ON opp.party_id = op.party_id
            LEFT JOIN branch_mst opb ON opb.branch_id = op.branch_id
            LEFT JOIN co_mst opc ON opc.co_id = opb.co_id
            LEFT JOIN jute_po fp ON fp.jute_po_id = h.po_id
            LEFT JOIN branch_mst fpb ON fpb.branch_id = fp.branch_id
            LEFT JOIN co_mst fpc ON fpc.co_id = fpb.co_id
            WHERE {_HOP_ROWS}
              AND {_LORRY_DATE} BETWEEN :fy_start AND :fy_end
            ORDER BY h.src_jute_mr_id, h.jute_mr_id
        """, params)

        transfer_pos = _read(conn, """
            SELECT p.jute_po_id, p.po_no, p.po_date, p.branch_id, p.status_id,
                   p.close_type, p.weight, p.jute_po_value, p.jute_uom,
                   p.internal_note,
                   cm.co_id, cm.co_prefix, cm.co_name, bm.branch_prefix,
                   pm.supp_name AS party_name
            FROM jute_po p
            LEFT JOIN branch_mst bm ON bm.branch_id = p.branch_id
            LEFT JOIN co_mst cm ON cm.co_id = bm.co_id
            LEFT JOIN party_mst pm ON pm.party_id = p.party_id
            WHERE p.internal_note LIKE :pat
            ORDER BY p.jute_po_id
        """, {"pat": "JT|%"})

        mr_ids = set(hops["hop_mr_id"]) | set(hops["root_mr_id"])
        mr_lines = pd.DataFrame()
        if mr_ids:
            mr_lines = _read(conn, f"""
                SELECT li.jute_mr_li_id, li.jute_mr_id, li.jute_po_li_id, li.active,
                       li.actual_item_id, li.challan_item_id,
                       COALESCE(im.item_name,
                                CONCAT('Item-', COALESCE(li.actual_item_id, li.challan_item_id))
                       ) AS item_name,
                       li.accepted_weight, li.actual_weight, li.rate
                FROM jute_mr_li li
                LEFT JOIN item_mst im
                       ON im.item_id = COALESCE(li.actual_item_id, li.challan_item_id)
                WHERE li.jute_mr_id IN ({_id_list(mr_ids)})
                ORDER BY li.jute_mr_id, li.jute_mr_li_id
            """)

        orig_po_ids = set(hops["orig_po_id"].dropna())
        po_ids = orig_po_ids | set(hops["fwd_po_id"].dropna())
        for po_id, note in zip(transfer_pos["jute_po_id"], transfer_pos["internal_note"]):
            marker = parse_marker(note)
            if marker and marker["root_mr_id"] in mr_ids:
                po_ids.add(po_id)
        po_lines = pd.DataFrame()
        if po_ids:
            po_lines = _read(conn, f"""
                SELECT pl.jute_po_li_id, pl.jute_po_id, pl.item_id,
                       COALESCE(im.item_name, iq.item_name,
                                CONCAT('Item-', pl.item_id)) AS item_name,
                       pl.quantity, pl.rate, pl.value, pl.percentage,
                       pl.jute_uom, pl.active
                FROM jute_po_li pl
                LEFT JOIN item_mst im ON im.item_id = pl.item_id
                LEFT JOIN item_mst iq ON iq.item_id = pl.jute_quality_id
                WHERE pl.jute_po_id IN ({_id_list(po_ids)})
                ORDER BY pl.jute_po_id, pl.jute_po_li_id
            """)

        siblings = pd.DataFrame()
        if orig_po_ids:
            siblings = _read(conn, f"""
                SELECT m.jute_mr_id, m.po_id, m.jute_gate_entry_no,
                       m.jute_gate_entry_date, m.status_id, sm.status_name,
                       m.mr_weight
                FROM jute_mr m
                LEFT JOIN status_mst sm ON sm.status_id = m.status_id
                WHERE m.po_id IN ({_id_list(orig_po_ids)})
                  AND m.jute_gate_entry_no IS NOT NULL
                ORDER BY m.po_id, m.jute_gate_entry_date, m.jute_gate_entry_no
            """)

    return {"fy_start_year": fy, "hops": hops, "transfer_pos": transfer_pos,
            "mr_lines": mr_lines, "po_lines": po_lines, "siblings": siblings}


def clear_tracker_cache() -> None:
    """Forget the tracker's cached reads: the Refresh button, and the
    Transfer Chain page after a save or delete -- so 'save a step, open the
    tracker, see the PO' works without waiting for the cache to expire."""
    get_tracker_index.clear()
    load_tracker_data.clear()
