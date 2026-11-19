"""Jute Payment Tracker shaping: which MRs are supplier bills, the row the
grid shows, and when ticked bills can be paid together."""

from datetime import date

import pandas as pd

from src.jutetransfer import payment_tracker_helpers as H

D = date
NAN = float("nan")


def mr(mr_id, src=None, **kw):
    row = dict(jute_mr_id=mr_id, src_jute_mr_id=src if src is not None else NAN, branch_mr_no=24,
               jute_mr_date=D(2026, 11, 6), net_total=1799511.0, payment_terms_days=NAN,
               payment_due_date=None, party_id=501, step1_co_id=65, step1_co="ACPL",
               step1_branch_prefix=None, supplier="JAY TULSI JUTE ", party="Kishan Kothari",
               gate_co="EJM", ge_no=180, ge_date=D(2026, 11, 5), root_terms_days=NAN,
               acc_bill_ref_id=NAN, acc_pending=NAN, acc_due_date=None, last_payment_date=None,
               acc_proposed=NAN)
    row.update(kw)
    return row


def test_mr_number_as_the_erp_prints_it():
    assert H.format_mr_no("ACPL", None, 24, D(2026, 11, 6)) == "ACPL/JMR/26-27/00024"
    assert H.format_mr_no("EJM", "F", 7, D(2026, 3, 31)) == "EJM/F/JMR/25-26/00007"
    assert H.format_mr_no("ACPL", NAN, 24.0, D(2026, 4, 1)) == "ACPL/JMR/26-27/00024"
    assert H.format_mr_no("ACPL", "", None, D(2026, 4, 1)) == ""


def test_only_the_step_1_mr_and_untransferred_mill_mrs_are_bills():
    first_hop_of = {100: 201, 110: 211}                 # two chains
    mrs = pd.DataFrame([
        mr(100),            # a chain root at the mill: inter-company after finalize
        mr(201, src=100),   # its step-1 MR: the supplier's bill
        mr(202, src=100),   # a later hop
        mr(120),            # a mill MR never transferred: the supplier's bill
        mr(211, src=110),
    ])
    assert list(H.pick_bills(mrs, first_hop_of)["jute_mr_id"]) == [201, 120, 211]


def test_a_bill_accounts_has_not_posted_is_fully_pending():
    (row,) = H.build_rows(pd.DataFrame([mr(201, src=100)]), today=D(2026, 11, 19)).to_dict("records")
    assert (row["Gate Entry Co"], row["Step 1 Co"], row["MR No"]) == (
        "EJM", "ACPL", "ACPL/JMR/26-27/00024")
    assert (row["Supplier"], row["Bill Amount"], row["Paid"], row["Pending"], row["Status"]) == (
        "JAY TULSI JUTE", 1799511.0, 0.0, 1799511.0, H.STATUS_NOT_POSTED)
    assert all(pd.isna(row[k]) for k in ("Terms (days)", "Due Date", "Overdue (days)"))


def test_paid_and_pending_come_from_accounts_and_terms_from_the_mr():
    frame = pd.DataFrame([
        mr(1, src=100, acc_bill_ref_id=9.0, acc_pending=0.0, last_payment_date=D(2026, 11, 16)),
        mr(2, src=101, acc_bill_ref_id=10.0, acc_pending=129544.0, payment_terms_days=7.0),
        mr(3, src=102, acc_bill_ref_id=11.0, acc_pending=1799511.0, root_terms_days=60.0),
        mr(4, src=103, payment_due_date=D(2026, 11, 10), payment_terms_days=60.0),
    ])
    rows = H.build_rows(frame, today=D(2026, 11, 19)).to_dict("records")
    assert [r["Status"] for r in rows] == [H.STATUS_PAID, H.STATUS_PART, H.STATUS_PENDING,
                                           H.STATUS_NOT_POSTED]
    assert rows[0]["Paid"] == 1799511.0 and rows[0]["Last Payment"] == D(2026, 11, 16)
    assert pd.isna(rows[0]["Overdue (days)"])                             # nothing left to pay
    assert (rows[1]["Due Date"], rows[1]["Overdue (days)"]) == (D(2026, 11, 13), 6)
    assert (rows[2]["Terms (days)"], rows[2]["Due Date"]) == (60, D(2027, 1, 5))   # the root's terms
    assert (rows[3]["Due Date"], rows[3]["Overdue (days)"]) == (D(2026, 11, 10), 9)  # stated due date
    t = H.totals(H.build_rows(frame, today=D(2026, 11, 19)))
    assert t["bills"] == 4 and t["pending"] == 129544.0 + 2 * 1799511.0
    assert t["overdue"] == 129544.0 + 1799511.0


def test_a_payment_waiting_for_approval_shows_as_proposed():
    rows = H.build_rows(pd.DataFrame([
        mr(1, src=100, acc_bill_ref_id=9.0, acc_pending=1799511.0, acc_proposed=1000000.0),
        mr(2, src=101, acc_bill_ref_id=10.0, acc_pending=1799511.0, acc_proposed=1799511.0),
    ])).to_dict("records")
    assert [(r["Status"], r["Pending"], r["Proposed"], r["can_pay"]) for r in rows] == [
        (H.STATUS_PROPOSED, 1799511.0, 1000000.0, 799511.0),
        (H.STATUS_PROPOSED, 1799511.0, 1799511.0, 0.0)]


def test_the_request_sent_to_vow():
    form = dict(branch_id=35, bank_ledger_id=118, voucher_date=D(2026, 11, 19), pay_mode="RTGS",
                instrument_no=" UTR123 ", instrument_date=None, narration="")
    payload = H.payment_payload(67, 501, form, {201: 1000000, 202: 0, 203: 5.5}, acting_user_id=24)
    assert payload == {
        "co_id": 67, "branch_id": 35, "voucher_date": "2026-11-19", "party_id": 501,
        "bank_ledger_id": 118, "pay_mode": "RTGS", "instrument_no": "UTR123",
        "instrument_date": None, "narration": None, "acting_user_id": 24,
        "bills": [{"jute_mr_id": 201, "amount": 1000000.0}, {"jute_mr_id": 203, "amount": 5.5}]}
    import pytest
    with pytest.raises(ValueError, match="at least one bill"):
        H.payment_payload(67, 501, form, {201: 0}, 24)
    with pytest.raises(ValueError, match="bank or cash account"):
        H.payment_payload(67, 501, dict(form, bank_ledger_id=None), {201: 5}, 24)
    assert H.default_narration("JAY TULSI", ["A/1", "A/2"]) == "Payment to JAY TULSI against A/1, A/2"
    assert H.default_narration("X", [str(i) for i in range(9)]).endswith("5 and 3 more")


def test_a_payment_is_one_company_and_one_supplier():
    posted = dict(acc_bill_ref_id=9.0, acc_pending=1799511.0)
    rows = H.build_rows(pd.DataFrame([
        mr(1, src=100, **posted), mr(2, src=101, **posted),
        mr(3, src=102, step1_co_id=67, step1_co="AVPL", **posted),
        mr(4, src=103, party_id=777, supplier="OTHER", **posted),
        mr(5, src=104, acc_bill_ref_id=9.0, acc_pending=0.0),
        mr(6, src=105),
        mr(7, src=106, acc_proposed=1799511.0, **posted),
    ]))
    assert "not in the accounts module" in H.selection_problem(rows.iloc[[0, 5]])
    assert "waiting for" in H.selection_problem(rows.iloc[[0, 6]])
    assert H.selection_problem(rows.iloc[[0, 1]]) is None
    assert "more than one company (ACPL, AVPL)" in H.selection_problem(rows.iloc[[0, 2]])
    assert "more than one supplier" in H.selection_problem(rows.iloc[[0, 3]])
    assert "already paid" in H.selection_problem(rows.iloc[[0, 4]])
    assert H.selection_problem(rows.iloc[[]]) == "Tick the bills to pay."


def test_opening_bills_sit_next_to_this_years_bills():
    opening = pd.DataFrame([
        dict(acc_bill_ref_id=501, step1_co_id=67, step1_co="AVPL", party_id=9, supplier="SHIVAM",
             bill_no="AVPL/JMR/25-26/00190", bill_date=D(2026, 3, 20), acc_due_date=D(2026, 5, 19),
             acc_amount=1500000.0, acc_pending=500000.0, src_doc_type="JUTE_MR", src_doc_id=88.0,
             origin_note=None, last_payment_date=D(2026, 6, 1), acc_proposed=0.0),
        dict(acc_bill_ref_id=502, step1_co_id=67, step1_co="AVPL", party_id=9, supplier="SHIVAM",
             bill_no="OLD-7", bill_date=D(2025, 12, 1), acc_due_date=None, acc_amount=700000.0,
             acc_pending=700000.0, src_doc_type=None, src_doc_id=NAN, origin_note="EJM old",
             last_payment_date=None, acc_proposed=0.0),
    ])
    old_mr = pd.DataFrame([mr(88, src=40, branch_mr_no=190, jute_mr_date=D(2026, 3, 20),
                              step1_co_id=67, step1_co="AVPL", party="sukumar saha", ge_no=77)])
    a, b = H.build_opening_rows(opening, old_mr, today=D(2026, 11, 19)).to_dict("records")
    assert (a["Bill"], a["Gate Entry Co"], a["GE No"], a["MR No"], a["Party"]) == (
        "Opening", "EJM", 77, "AVPL/JMR/25-26/00190", "sukumar saha")
    assert (a["Bill Amount"], a["Paid"], a["Pending"], a["Status"]) == (
        1500000.0, 1000000.0, 500000.0, H.STATUS_PART)
    assert a["Overdue (days)"] == 184 and a["jute_mr_id"] == 88
    assert (b["MR No"], b["Gate Entry Co"], b["Party"], b["Status"]) == (
        "OLD-7", "", "EJM old", H.STATUS_PENDING)
    assert H.build_opening_rows(pd.DataFrame(), pd.DataFrame()).empty
    both = H.build_opening_rows(opening, old_mr)
    assert "paid from VoW" in H.selection_problem(both.iloc[[1]])
