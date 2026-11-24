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
    payload = H.payment_payload(67, 501, form, {9001: 1000000, 9002: 0, 9003: 5.5}, acting_user_id=24)
    assert payload == {
        "co_id": 67, "branch_id": 35, "voucher_date": "2026-11-19", "party_id": 501,
        "bank_ledger_id": 118, "pay_mode": "RTGS", "instrument_no": "UTR123",
        "instrument_date": None, "narration": None, "acting_user_id": 24,
        "bills": [{"acc_bill_ref_id": 9001, "amount": 1000000.0},
                  {"acc_bill_ref_id": 9003, "amount": 5.5}]}
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
    assert H.selection_problem(both.iloc[[1]]) is None          # opening bills can be paid
    assert H.selection_problem(both) is None                    # alone or with each other


def test_a_supplier_on_hold_is_marked_and_cannot_be_ticked():
    assert H.COLUMNS.index("On hold") == H.COLUMNS.index("Status") + 1
    posted = dict(acc_bill_ref_id=9.0, acc_pending=1799511.0)
    bills = pd.DataFrame([mr(1, src=100, **posted), mr(2, src=101, party_id=777, **posted),
                          mr(3, src=102, party_id=888, **posted)])
    holds = pd.DataFrame([dict(co_id=65, party_id=501, note="cheque bounced"),
                          dict(co_id=65, party_id=888, note=None),
                          dict(co_id=67, party_id=777, note="other company")])
    rows = H.build_rows(bills, holds=holds)
    assert list(rows["On hold"]) == ["cheque bounced", "", "Yes"]
    assert list(rows["on_hold"]) == [True, False, True]
    problem = H.selection_problem(rows.iloc[[0]])
    assert problem == ("JAY TULSI JUTE is on hold for payment. Release the hold in VoW "
                       "(Accounting > Pay Bills) first.")
    assert H.selection_problem(rows.iloc[[1]]) is None
    # no holds at all (frame empty, None or dict): nothing marked
    for none in (None, pd.DataFrame(columns=["co_id", "party_id", "note"]), {}):
        again = H.build_rows(bills, holds=none)
        assert (again["On hold"] == "").all() and not again["on_hold"].any()
    assert H.holds_map({(65, 501): "x"}) == {(65, 501): "x"}
    assert H.build_rows(bills.iloc[0:0]).empty


def test_opening_rows_carry_the_hold_too():
    opening = pd.DataFrame([dict(
        acc_bill_ref_id=502, step1_co_id=67, step1_co="AVPL", party_id=9, supplier="SHIVAM",
        bill_no="OLD-7", bill_date=D(2025, 12, 1), acc_due_date=None, acc_amount=700000.0,
        acc_pending=700000.0, src_doc_type=None, src_doc_id=NAN, origin_note=None,
        last_payment_date=None, acc_proposed=0.0)])
    held = H.build_opening_rows(opening, pd.DataFrame(), holds={(67, 9): ""})
    assert held.iloc[0]["On hold"] == "Yes" and bool(held.iloc[0]["on_hold"])
    assert "on hold for payment" in H.selection_problem(held)
    free = H.build_opening_rows(opening, pd.DataFrame())
    assert free.iloc[0]["On hold"] == "" and not free.iloc[0]["on_hold"]


def _opening_row(ref_id, mr_id, bill_date):
    return pd.DataFrame([dict(
        acc_bill_ref_id=ref_id, step1_co_id=65, step1_co="ACPL", party_id=9, supplier="S",
        bill_no="B-%d" % ref_id, bill_date=bill_date, acc_due_date=None, acc_amount=1000.0,
        acc_pending=1000.0, src_doc_type="JUTE_MR" if mr_id else None,
        src_doc_id=mr_id if mr_id else NAN, origin_note="n", last_payment_date=None,
        acc_proposed=0.0)])


def _mr_rows(*specs):
    bills = pd.DataFrame([mr(i, acc_bill_ref_id=ref) for i, ref in specs])
    return H.build_rows(bills, D(2026, 11, 19))


def test_imported_ref_replaces_the_unposted_mr_row():
    src = pd.DataFrame([mr(88, step1_co_id=65, jute_mr_date=D(2026, 6, 1))])
    opening = H.build_opening_rows(_opening_row(700, 88, D(2026, 6, 1)), src, D(2026, 11, 19))
    merged = H.merge_rows(opening, _mr_rows((88, NAN), (89, NAN)))
    assert list(merged["jute_mr_id"]) == [88, 89]
    assert merged.iloc[0]["acc_bill_ref_id"] == 700
    assert list(merged.columns) == list(opening.columns)


def test_posted_mr_keeps_both_rows():
    src = pd.DataFrame([mr(88)])
    opening = H.build_opening_rows(_opening_row(700, 88, D(2026, 6, 1)), src, D(2026, 11, 19))
    merged = H.merge_rows(opening, _mr_rows((88, 55.0)))
    assert list(merged["acc_bill_ref_id"]) == [700, 55]


def test_opening_without_an_mr_is_untouched():
    opening = H.build_opening_rows(_opening_row(701, None, D(2025, 12, 1)), pd.DataFrame())
    merged = H.merge_rows(opening, _mr_rows((89, NAN)))
    assert len(merged) == 2 and merged.iloc[0]["acc_bill_ref_id"] == 701


def test_label_is_opening_before_the_start_and_imported_after():
    both = pd.concat([_opening_row(1, None, D(2026, 3, 31)), _opening_row(2, None, D(2026, 4, 1))])
    r = H.build_opening_rows(both, pd.DataFrame(), accounts_start=D(2026, 4, 1))
    assert list(r["Bill"]) == [H.BILL_OPENING, H.BILL_IMPORTED]
    assert set(H.build_opening_rows(both, pd.DataFrame())["Bill"]) == {H.BILL_OPENING}


def test_merge_with_empty_inputs():
    mrs = _mr_rows((89, NAN))
    assert len(H.merge_rows(pd.DataFrame(), mrs)) == 1
    opening = H.build_opening_rows(_opening_row(701, None, D(2025, 12, 1)), pd.DataFrame())
    assert len(H.merge_rows(opening, mrs.iloc[0:0])) == 1
    assert H.merge_rows(pd.DataFrame(), pd.DataFrame()).empty


def test_hidden_supplier_columns_on_current_rows():
    bills = pd.DataFrame([mr(1, acc_bill_ref_id=5, acc_pending=10.0, jute_supplier_id=7.0),
                          mr(2, jute_supplier_id=NAN)])
    r = H.build_rows(bills, D(2026, 11, 19))
    assert list(r.columns) == H.HIDDEN + H.COLUMNS
    assert r["jute_supplier_id"].tolist()[0] == 7 and r["jute_supplier_id"].isna().tolist()[1]
    assert list(r["sheet_party"]) == ["", ""]
    # a frame without the column is tolerated
    assert H.build_rows(pd.DataFrame([mr(3)]), D(2026, 11, 19))["jute_supplier_id"].isna().all()


def test_hidden_supplier_columns_on_opening_rows():
    def op(ref, note, src=NAN):
        return dict(acc_bill_ref_id=ref, step1_co_id=67, step1_co="AVPL", party_id=9, supplier="S",
                    bill_no="B", bill_date=D(2025, 12, 1), acc_due_date=None, acc_amount=10.0,
                    acc_pending=10.0, src_doc_type="JUTE_MR" if src == src else None,
                    src_doc_id=src, origin_note=note, last_payment_date=None, acc_proposed=0.0)
    opening = pd.DataFrame([op(1, "LCPL | kishan k"), op(2, "EJM old | others | no MR in system"),
                            op(3, "EJM old"), op(4, None), op(5, "x | y", src=88.0)])
    old_mr = pd.DataFrame([mr(88, jute_supplier_id=12, step1_co_id=67, step1_co="AVPL")])
    r = H.build_opening_rows(opening, old_mr, D(2026, 11, 19))
    assert list(r.columns) == H.HIDDEN + H.COLUMNS
    assert list(r["sheet_party"]) == ["kishan k", "others", "", "", "y"]
    assert r["jute_supplier_id"].isna().tolist() == [True, True, True, True, False]
    assert r["jute_supplier_id"].iloc[4] == 12


def test_tracker_rows_matches_the_pages_pipeline():
    drop = ["acc_bill_ref_id", "acc_pending", "acc_due_date", "last_payment_date", "acc_proposed"]
    mrs = pd.DataFrame([mr(10), mr(11)]).drop(columns=drop)
    acc = pd.DataFrame([dict(jute_mr_id=11, acc_bill_ref_id=5, acc_pending=0.0, acc_due_date=None,
                             last_payment_date=D(2026, 11, 7), acc_proposed=0.0)])
    opening = pd.DataFrame([dict(acc_bill_ref_id=1, step1_co_id=67, step1_co="AVPL", party_id=9,
                                 supplier="S", bill_no="OLD", bill_date=D(2026, 4, 2),
                                 acc_due_date=None, acc_amount=10.0, acc_pending=0.0,
                                 src_doc_type=None, src_doc_id=NAN, origin_note="a | b",
                                 last_payment_date=None, acc_proposed=0.0),
                            dict(acc_bill_ref_id=2, step1_co_id=67, step1_co="AVPL", party_id=9,
                                 supplier="S", bill_no="OLD2", bill_date=D(2025, 12, 2),
                                 acc_due_date=None, acc_amount=10.0, acc_pending=4.0,
                                 src_doc_type=None, src_doc_id=NAN, origin_note="a | c",
                                 last_payment_date=None, acc_proposed=0.0)])
    data = dict(mrs=mrs, first_hop_of={}, acc=acc, opening=opening, opening_mrs=pd.DataFrame(),
                holds=pd.DataFrame(columns=["co_id", "party_id", "note"]))
    rows = H.tracker_rows(data, D(2026, 11, 19), H.ACCOUNTS_START)
    assert list(rows["Bill"]) == ["Imported", "Opening", "Current", "Current"]
    assert list(rows["Status"])[2:] == ["Not in accounts", "Paid"]
    kept = H.tracker_rows(data, D(2026, 11, 19), H.ACCOUNTS_START, drop_paid_opening=True)
    assert list(kept["Bill"]) == ["Opening", "Current", "Current"]
    assert H.ACCOUNTS_START == D(2026, 4, 1)
    empty = dict(data, mrs=mrs.iloc[0:0], opening=None, opening_mrs=None)
    assert H.tracker_rows(empty, D(2026, 11, 19), H.ACCOUNTS_START).empty
