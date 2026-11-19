"""Jute Payment Tracker reads (SELECT only). See payment_tracker_helpers for
what a bill is; shaping happens there, on these frames."""

from datetime import date

import pandas as pd
import streamlit as st
from sqlalchemy import text

from .database import DatabaseConnection

CACHE_TTL = 120  # seconds

_MRS_SQL = """
    SELECT h.jute_mr_id, h.src_jute_mr_id, h.branch_mr_no, h.jute_mr_date, h.net_total,
           h.payment_terms_days, h.payment_due_date, h.party_id,
           hc.co_id AS step1_co_id, hc.co_prefix AS step1_co, hb.branch_prefix AS step1_branch_prefix,
           pm.supp_name AS supplier, js.supplier_name AS party,
           rc.co_prefix AS gate_co, r.jute_gate_entry_no AS ge_no,
           r.jute_gate_entry_date AS ge_date, r.payment_terms_days AS root_terms_days
    FROM jute_mr h
    JOIN branch_mst hb ON hb.branch_id = h.branch_id
    JOIN co_mst hc ON hc.co_id = hb.co_id
    LEFT JOIN party_mst pm ON pm.party_id = h.party_id
    LEFT JOIN jute_supplier_mst js ON js.supplier_id = h.jute_supplier_id
    LEFT JOIN jute_mr r ON r.jute_mr_id = COALESCE(h.src_jute_mr_id, h.jute_mr_id)
    LEFT JOIN branch_mst rb ON rb.branch_id = r.branch_id
    LEFT JOIN co_mst rc ON rc.co_id = rb.co_id
"""
_MRS_IN_RANGE = """
    WHERE h.status_id = 3
      AND COALESCE(h.transfer_mode, 0) = 0
      AND h.jute_mr_date BETWEEN :date_from AND :date_to
    ORDER BY h.jute_mr_date, h.jute_mr_id
"""

# Opening bills: what a supplier was still owed when the company started in
# accounts, entered bill-wise in VoW (Accounting > Opening Bills). Each is a
# NEW bill reference on an approved OPENING_RECON journal; one entered against
# an old jute MR carries src_doc_type 'JUTE_MR' + the MR id.
_OPENING_SQL = """
    SELECT br.acc_bill_ref_id, br.co_id AS step1_co_id, c.co_prefix AS step1_co, br.party_id,
           pm.supp_name AS supplier, br.bill_no, br.bill_date, br.due_date AS acc_due_date,
           br.amount AS acc_amount, br.pending_amount AS acc_pending,
           br.src_doc_type, br.src_doc_id, br.origin_note,
           (SELECT MAX(pv.voucher_date) {settled} AND pv.status_id = 3) AS last_payment_date,
           (SELECT COALESCE(SUM(s.settled_amount), 0) {settled}
               AND pv.status_id IN (21, 1, 20)) AS acc_proposed
    FROM acc_voucher v
    JOIN acc_bill_ref br ON br.acc_voucher_id = v.acc_voucher_id
                        AND br.ref_type = 'NEW' AND COALESCE(br.active, 1) = 1
    JOIN co_mst c ON c.co_id = br.co_id
    LEFT JOIN party_mst pm ON pm.party_id = br.party_id
    WHERE v.source_doc_type = 'OPENING_RECON'
      AND v.status_id = 3 AND COALESCE(v.is_reversed, 0) = 0 AND COALESCE(v.active, 1) = 1
    ORDER BY br.bill_date, br.acc_bill_ref_id
"""

# The first hop of every chain: one pass over the hop rows (src_jute_mr_id
# has no index, so no per-row subquery).
_FIRST_HOPS_SQL = """
    SELECT src_jute_mr_id AS root_id, MIN(jute_mr_id) AS first_hop_id
    FROM jute_mr
    WHERE src_jute_mr_id IS NOT NULL AND COALESCE(transfer_mode, 0) = 0
    GROUP BY src_jute_mr_id
"""

# What accounts holds for a bill: its live JUTE_BILLPASS voucher's NEW bill
# reference (amount, stored pending -- reduced only when a payment is
# APPROVED -- and due date), the date of the last approved payment against
# it, and what unapproved payment vouchers (Draft 21, Open 1, Pending
# approval 20) propose to settle.
_SETTLED = """FROM acc_bill_settlement s
               JOIN acc_bill_ref sr ON sr.acc_bill_ref_id = s.acc_bill_ref_id
               JOIN acc_voucher pv ON pv.acc_voucher_id = sr.acc_voucher_id
              WHERE s.settled_against_bill_ref_id = br.acc_bill_ref_id
                AND COALESCE(s.active, 1) = 1 AND COALESCE(sr.active, 1) = 1
                AND COALESCE(pv.active, 1) = 1 AND COALESCE(pv.is_reversed, 0) = 0"""

_ACC_SQL = f"""
    SELECT v.source_doc_id AS jute_mr_id, br.acc_bill_ref_id, br.amount AS acc_amount,
           br.pending_amount AS acc_pending, br.due_date AS acc_due_date,
           (SELECT MAX(pv.voucher_date) {_SETTLED} AND pv.status_id = 3) AS last_payment_date,
           (SELECT COALESCE(SUM(s.settled_amount), 0) {_SETTLED}
               AND pv.status_id IN (21, 1, 20)) AS acc_proposed
    FROM acc_voucher v
    JOIN acc_bill_ref br ON br.acc_voucher_id = v.acc_voucher_id
                        AND br.ref_type = 'NEW' AND COALESCE(br.active, 1) = 1
    WHERE v.source_doc_type = 'JUTE_BILLPASS'
      AND v.status_id = 3 AND COALESCE(v.is_reversed, 0) = 0 AND COALESCE(v.active, 1) = 1
"""


@st.cache_data(ttl=CACHE_TTL, show_spinner="Loading jute bills…")
def load_bills(date_from: date, date_to: date) -> dict:
    """{'mrs': approved mode-0 MRs dated in the range, 'first_hop_of': {root
    id: first hop id}, 'acc': one row per bill accounts has posted, 'opening':
    the opening bills entered in VoW, 'opening_mrs': the old MRs they name}."""
    with DatabaseConnection.get_engine().connect() as conn:
        mrs = pd.read_sql_query(text(_MRS_SQL + _MRS_IN_RANGE), conn,
                                params={"date_from": date_from, "date_to": date_to})
        hops = conn.execute(text(_FIRST_HOPS_SQL)).fetchall()
        try:
            acc = pd.read_sql_query(text(_ACC_SQL), conn)
        except Exception:        # accounting tables absent: every bill is 'Not in accounts'
            acc = pd.DataFrame(columns=["jute_mr_id", "acc_bill_ref_id", "acc_amount",
                                        "acc_pending", "acc_due_date", "last_payment_date",
                                        "acc_proposed"])
        try:
            opening = pd.read_sql_query(text(_OPENING_SQL.format(settled=_SETTLED)), conn)
        except Exception:        # the origin columns are not on this database yet
            opening = pd.DataFrame()
        opening_mrs = pd.DataFrame()
        if not opening.empty:
            ids = sorted({int(i) for i in opening.loc[opening["src_doc_type"] == "JUTE_MR",
                                                      "src_doc_id"].dropna()})
            if ids:
                opening_mrs = pd.read_sql_query(
                    text(_MRS_SQL + f" WHERE h.jute_mr_id IN ({','.join(map(str, ids))})"), conn)
    return {"mrs": mrs, "first_hop_of": {int(r[0]): int(r[1]) for r in hops}, "acc": acc,
            "opening": opening, "opening_mrs": opening_mrs}


def clear_cache() -> None:
    load_bills.clear()
