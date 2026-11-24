"""Jute Supplier Dues shaping (pure): name matching, supplier resolution,
notional FIFO, summaries, period columns and the others -> supplier list."""

from datetime import date

import pandas as pd
import pytest

from src.jutetransfer import payment_summary_helpers as S
from src.jutetransfer import payment_tracker_helpers as H

D = date
NAN = float("nan")
TODAY = D(2026, 11, 24)

NAMES = [
    "others", "Kishan Kothari", "Sushil Kumar Pugalia", "Binod Pugalia", "Ramobatar  Poddar",
    "Bishnu Kumar Bajaj", "sukumar saha", "Lalit Agrawal", "Gopal Chand Bajaj & Sons",
    "agarparaaa", "AKASH KOTHARI", "KRISHNA TRADING", "khetulalji", "rungta", "ss rungta adpl",
    "ss rungta sspl", "Raju babu", "uttam jaju", "SAgarwal", "tirupati", "jugalji", "LK DAGA",
    "SK Mohata", "others ", ".", "Na", "u Mundra", "P mundra", "J JAJU", "Bimli Jute",
    "Shema Jute Agency", "shyamji", "sk lahoti", "Saroj Bai", "Bansilalji", "chajjer", "jain",
    "MR Rathi", "IK MAL", "narayan t", "rajeshwar k", "dave",
]
MASTER = {i + 1: n for i, n in enumerate(NAMES)}
MASTER[2397] = "others "          # the real trailing-space duplicate
ID = {n: i for i, n in MASTER.items()}


@pytest.mark.parametrize("sheet,master_name", [
    ("kishan k", "Kishan Kothari"), ("sk pugalia", "Sushil Kumar Pugalia"),
    ("SK pugalia", "Sushil Kumar Pugalia"), ("binod p", "Binod Pugalia"),
    ("Binod P", "Binod Pugalia"), ("r poddar", "Ramobatar  Poddar"),
    ("b bajaj", "Bishnu Kumar Bajaj"), ("sukumar", "sukumar saha"),
    ("L agrawal", "Lalit Agrawal"), ("GCB", "Gopal Chand Bajaj & Sons"),
    ("agarpara", "agarparaaa"), ("A kothari", "AKASH KOTHARI"),
    ("krishna T", "KRISHNA TRADING"),
    ("khetulalji", "khetulalji"), ("rungta", "rungta"), ("ss rungta adpl", "ss rungta adpl"),
    ("ss rungta sspl", "ss rungta sspl"), ("Raju babu", "Raju babu"),
    ("uttam jaju", "uttam jaju"), ("SAgarwal", "SAgarwal"), ("tirupati", "tirupati"),
    ("jugalji", "jugalji"), ("LK DAGA", "LK DAGA"), ("SK Mohata", "SK Mohata"),
    ("p mundra", "P mundra"), ("u mundra", "u Mundra"), ("bimli", "Bimli Jute"),
])
def test_match_master_real_names(sheet, master_name):
    assert S.match_master(sheet, MASTER) == ID[master_name]


@pytest.mark.parametrize("sheet", ["cheena", "cuttuck", "others", "", None, "na", "."])
def test_match_master_no_match(sheet):
    assert S.match_master(sheet, MASTER) is None


def test_match_master_ambiguous_is_none():
    m = {1: "Ram Kumar", 2: "Ramesh Kumar"}
    assert S.match_master("ra kumar", m) is None
    assert S.match_master("ramesh k", m) == 2


def test_norm_name_and_others():
    assert S.norm_name("  Gopal Chand-Bajaj & Sons. ") == "gopal chand bajaj sons"
    assert S.norm_name(None) == "" and S.norm_name(NAN) == ""
    o = S.others_ids(MASTER)
    assert {ID["others"], 2397, ID["."], ID["Na"]} <= o
    assert ID["rungta"] not in o


def test_resolve_order():
    pm = {9: ID["jain"], 8: None}
    r = lambda **k: dict(dict(jute_supplier_id=None, sheet_party="", party_id=9), **k)
    assert S.resolve_supplier(r(jute_supplier_id=ID["rungta"], sheet_party="jain"), MASTER, pm) \
        == (ID["rungta"], "mr")
    assert S.resolve_supplier(r(jute_supplier_id=NAN, sheet_party="Jain"), MASTER, {}) \
        == (ID["jain"], "sheet")
    # the MR names a supplier, but it is others: the sheet name is not consulted
    assert S.resolve_supplier(r(jute_supplier_id=ID["others"], sheet_party="jain"), MASTER, {}) \
        == (None, "others")
    assert S.resolve_supplier(r(), MASTER, pm) == (ID["jain"], "confirmed")
    assert S.resolve_supplier(r(party_id=8), MASTER, pm) == (None, "others")
    assert S.resolve_supplier(r(party_id=NAN, sheet_party="kishan k"), MASTER, pm) \
        == (None, "others")        # short names are not auto-matched


# ---- rows -----------------------------------------------------------------------
def row(party_id=1, sid=None, sheet="", co="ACPL", mr_date=D(2026, 7, 1), amount=100.0,
        pending=100.0, hold=False, supplier="Kishan Kothari", **kw):
    r = {c: None for c in H.HIDDEN + H.COLUMNS}
    r.update(party_id=party_id, jute_supplier_id=sid, sheet_party=sheet, step1_co_id=65,
             **{"Step 1 Co": co, "MR Date": mr_date, "Bill Amount": amount, "Pending": pending,
                "Paid": amount - pending, "on_hold": hold, "Supplier": supplier,
                "Status": "Pending", "Bill": "Current", "Last Payment": None})
    r.update(kw)
    return r


def frame(*rows):
    return pd.DataFrame(list(rows))


def test_add_supplier_key_and_split():
    rows = frame(row(1, sid=ID["rungta"]), row(2, sheet="jain"), row(3, supplier="Cheena"),
                 row(NAN, supplier="No party"))
    k = S.add_supplier_key(rows, MASTER, {})
    assert list(k["skey"]) == [f"S{ID['rungta']}", f"S{ID['jain']}", "O", "O"]
    assert list(k["Jute Supplier"])[:2] == ["rungta", "jain"] and k["Jute Supplier"][2] == "others"
    assert list(k["ssource"]) == ["mr", "sheet", "others", "others"]
    sp = S.add_supplier_key(rows, MASTER, {}, split_others=True)
    assert list(sp["skey"])[2:] == ["O3", "O"]
    assert sp["Jute Supplier"][2] == "Cheena (others)"
    # a confirmed party map moves the party out of others
    assert S.add_supplier_key(rows, MASTER, {3: ID["dave"]})["skey"][2] == f"S{ID['dave']}"
    assert len(S.add_supplier_key(pd.DataFrame(), MASTER, {})) == 0


# ---- FIFO -------------------------------------------------------------------------
JUL = [(D(2026, 7, 1), 100, 100), (D(2026, 7, 8), 100, 100), (D(2026, 7, 15), 100, 100)]


def test_fifo_nothing_paid():
    f = S.fifo_position(JUL, today=TODAY)
    assert f["pending_from"] == D(2026, 7, 1) and f["cleared_till"] is None
    assert f["days_pending"] == (TODAY - D(2026, 7, 1)).days
    assert f["pending"] == 300 and f["fifo_pending"] == [100, 100, 100]


def test_fifo_paid_out_of_turn():
    bills = JUL[:2] + [(D(2026, 7, 15), 100, 0)]
    f = S.fifo_position(bills, today=TODAY)
    assert f["actual_oldest_unpaid"] == D(2026, 7, 1)
    assert f["pending_from"] == D(2026, 7, 8) and f["cleared_till"] == D(2026, 7, 1)
    assert f["fifo_pending"] == [0, 100, 100]
    assert f["days_pending"] == (TODAY - D(2026, 7, 8)).days


def test_fifo_exact_part_advance_and_all_paid():
    exact = S.fifo_position([(D(2026, 7, 1), 100, 100), (D(2026, 7, 8), 100, 100)], 100, TODAY)
    assert exact["pending_from"] == D(2026, 7, 8) and exact["cleared_till"] == D(2026, 7, 1)
    part = S.fifo_position([(D(2026, 7, 1), 100, 100), (D(2026, 7, 8), 100, 100)], 150, TODAY)
    assert part["pending_from"] == D(2026, 7, 8) and part["fifo_pending"] == [0, 50]
    adv = S.fifo_position(JUL, extra_paid=500, today=TODAY)
    assert adv["pending"] == 0 and adv["pending_from"] is None and adv["days_pending"] is None
    assert adv["cleared_till"] == D(2026, 7, 15) and adv["fifo_pending"] == [0, 0, 0]
    paid = S.fifo_position([(D(2026, 7, 1), 100, 0)], today=TODAY)
    assert paid["pending_from"] is None and paid["cleared_till"] == D(2026, 7, 1)


def test_fifo_same_day_none_date_and_empty():
    same = S.fifo_position([(D(2026, 7, 1), 100, 100), (D(2026, 7, 1), 100, 100)], 100, TODAY)
    assert same["fifo_pending"] == [0, 100] and same["pending_from"] == D(2026, 7, 1)
    nodate = S.fifo_position([(D(2026, 7, 1), 100, 100), (None, 100, 100)], 100, TODAY)
    assert nodate["fifo_pending"] == [100, 0]        # undated sorts first, FIFO result aligned to input
    e = S.fifo_position([], today=TODAY)
    assert e["pending"] == 0 and e["fifo_pending"] == [] and e["pending_from"] is None


# ---- on account / summaries ---------------------------------------------------------------
def oa_frame(*recs):
    return pd.DataFrame(list(recs), columns=S.ON_ACCOUNT_COLS)


def keyed(*rows):
    return S.add_supplier_key(frame(*rows), MASTER, {})


def test_on_account_goes_to_the_suppliers_largest_pending():
    k = keyed(row(1, sid=ID["rungta"], pending=50), row(1, sid=ID["jain"], pending=500),
              row(2, sid=ID["dave"], pending=0, mr_date=D(2026, 6, 1)),
              row(2, sid=ID["tirupati"], pending=0, mr_date=D(2026, 8, 1)))
    oa = oa_frame((65, "ACPL", 1, "S1", 40.0, D(2026, 9, 1)),
                  (65, "ACPL", 2, "S2", 10.0, D(2026, 9, 1)),
                  (65, "ACPL", 7, "S7", 5.0, D(2026, 9, 1)),
                  (67, "AVPL", 8, "S8", 5.0, D(2026, 9, 1)))
    a = S.assign_on_account(oa, k, MASTER, {8: ID["dave"]}, holds={(65, 2): "x"})
    assert list(a["skey"]) == [f"S{ID['jain']}", f"S{ID['tirupati']}", "O", f"S{ID['dave']}"]
    assert list(a["on_hold"]) == [False, True, False, False]
    assert S.assign_on_account(None, k, MASTER, {}).empty


def test_supplier_summary_hold_shown_both_ways():
    rows = [row(1, sid=3, pending=100, mr_date=D(2026, 7, 1), co="ACPL"),
            row(2, sid=3, pending=100, mr_date=D(2026, 6, 1), co="AVPL", hold=True,
                supplier="Held Party"),
            row(1, sid=3, pending=0, mr_date=D(2026, 8, 1), co="ACPL", last_payment=None,
                **{"Last Payment": D(2026, 10, 5)})]
    k = S.add_supplier_key(frame(*rows), {3: "Acme"}, {})
    oa = S.assign_on_account(oa_frame((65, "ACPL", 1, "S", 30.0, D(2026, 9, 1))), k, {3: "Acme"}, {})
    s = S.supplier_summary(k, oa, TODAY).iloc[0]
    assert (s["Pending"], s["On Hold"], s["Free To Pay"], s["On Account"]) == (200, 100, 100, 30)
    assert s["Hold"] == "Part" and s["Held Parties"] == "Held Party"
    assert s["Unpaid Bills"] == 2 and s["Companies"] == "ACPL, AVPL"
    assert s["Oldest Unpaid Bill"] == D(2026, 6, 1)
    # without the held bill: Jul bill + paid Aug bill (100 paid) + 30 on account -> 130 goes oldest first
    assert s["Pending From"] == D(2026, 8, 1) and s["Cleared Till"] == D(2026, 7, 1)
    assert s["Days Pending"] == (TODAY - D(2026, 8, 1)).days
    # with the held June bill the same 130 covers June first, so Jul 1 is the oldest uncovered
    assert s["Days Incl. Hold"] == (TODAY - D(2026, 7, 1)).days
    assert s["Last Payment"] == D(2026, 10, 5)


def test_hold_all_and_none():
    k = S.add_supplier_key(frame(row(1, sid=3, hold=True)), {3: "A"}, {})
    assert S.supplier_summary(k, S.assign_on_account(None, k, {3: "A"}, {}), TODAY)["Hold"][0] == "All"
    k = S.add_supplier_key(frame(row(1, sid=3)), {3: "A"}, {})
    s = S.supplier_summary(k, S.assign_on_account(None, k, {3: "A"}, {}), TODAY)
    assert s["Hold"][0] == "" and s["Held Parties"][0] == ""


def test_supplier_summary_sorted_and_empty():
    k = keyed(row(1, sid=ID["rungta"], pending=10), row(2, sid=ID["jain"], pending=900))
    s = S.supplier_summary(k, S.assign_on_account(None, k, MASTER, {}), TODAY)
    assert list(s["Jute Supplier"]) == ["jain", "rungta"]
    e = S.supplier_summary(S.add_supplier_key(pd.DataFrame(), MASTER, {}), None, TODAY)
    assert e.empty and list(e.columns) == S.SUMMARY_COLUMNS


# ---- periods ---------------------------------------------------------------------------------
def test_period_label_across_march_april():
    start = D(2026, 4, 1)
    assert S.period_label(D(2026, 4, 1), start) == "Apr-26"
    assert S.period_label(D(2026, 3, 31), start) == "FY 25-26"
    assert S.period_label(D(2025, 4, 1), start) == "FY 25-26"
    assert S.period_label(D(2025, 3, 31), start) == "FY 24-25"
    assert S.period_label(D(2026, 11, 3), D(2026, 4, 1)) == "Nov-26"


def test_period_matrix_order_and_values():
    k = keyed(row(1, sid=3, pending=10, mr_date=D(2026, 5, 3)),
              row(1, sid=3, pending=5, mr_date=D(2026, 5, 20)),
              row(1, sid=3, pending=7, mr_date=D(2026, 3, 5)),
              row(1, sid=3, pending=4, mr_date=D(2025, 2, 5)),
              row(1, sid=3, pending=0, mr_date=D(2026, 9, 5)),
              row(1, sid=3, pending=9, mr_date=None),
              row(2, sid=4, pending=1, mr_date=D(2026, 4, 9), co="AVPL"))
    m = S.period_matrix(k, D(2026, 4, 1))
    assert list(m.columns) == ["FY 24-25", "FY 25-26", "Apr-26", "May-26"]
    assert m.loc["S3", "May-26"] == 15 and m.loc["S3", "FY 25-26"] == 7 and m.loc["S4", "Apr-26"] == 1
    c = S.period_matrix(k, D(2026, 4, 1), by="Step 1 Co")
    assert c.loc["AVPL", "Apr-26"] == 1 and c.loc["ACPL", "May-26"] == 15
    assert S.period_matrix(pd.DataFrame(columns=["skey"]), D(2026, 4, 1)).empty


# ---- company / bills ------------------------------------------------------------------------------
def test_company_summary_total_equals_supplier_summary():
    rows = [row(1, sid=3, pending=100, co="ACPL", mr_date=D(2026, 7, 1)),
            row(1, sid=3, pending=40, amount=100, co="AVPL", mr_date=D(2026, 6, 1)),
            row(2, sid=3, pending=70, co="AVPL", mr_date=D(2026, 8, 1), hold=True),
            row(5, sid=4, pending=1000)]
    k = S.add_supplier_key(frame(*rows), {3: "A", 4: "B"}, {})
    oa = S.assign_on_account(oa_frame((67, "AVPL", 1, "S", 25.0, D(2026, 9, 1))), k,
                             {3: "A", 4: "B"}, {})
    c = S.company_summary(k, oa, "S3", TODAY)
    s = S.supplier_summary(k, oa, TODAY).set_index("skey").loc["S3"]
    assert list(c["Company"]) == ["ACPL", "AVPL", "All companies"]
    tot = c.iloc[-1]
    for col in ("Pending", "On Hold", "On Account", "Days Pending", "Days Incl. Hold",
                "Unpaid Bills", "Oldest Unpaid Bill"):
        assert tot[col] == s[col], col
    assert c["Pending"].iloc[:2].sum() == tot["Pending"] and tot["Bills"] == 3
    assert c.set_index("Company").loc["AVPL", "On Account"] == 25
    assert S.company_summary(k, oa, "S99", TODAY).empty


def test_supplier_bills_fifo_column():
    rows = [row(1, sid=3, pending=100, mr_date=D(2026, 7, 15), **{"MR No": "c"}),
            row(1, sid=3, pending=100, mr_date=D(2026, 7, 1), **{"MR No": "a"}),
            row(2, sid=3, pending=100, mr_date=D(2026, 7, 8), hold=True, **{"MR No": "b"}),
            row(1, sid=3, pending=100, mr_date=D(2026, 7, 8), amount=100, **{"MR No": "d"})]
    k = S.add_supplier_key(frame(*rows), {3: "A"}, {})
    oa = S.assign_on_account(oa_frame((65, "ACPL", 1, "S", 100.0, D(2026, 9, 1))), k, {3: "A"}, {})
    b = S.supplier_bills(k, oa, "S3")
    assert list(b["MR No"]) == ["a", "b", "d", "c"]            # oldest first
    assert b["FIFO Pending"].tolist()[0] == 0 and pd.isna(b["FIFO Pending"][1])
    assert b["FIFO Pending"].tolist()[2:] == [100, 100]
    assert list(b.columns) == S.BILL_COLUMNS
    assert S.supplier_bills(k, oa, "nope").empty


# ---- suggestions ----------------------------------------------------------------------------------------
def test_suggest_party_suppliers():
    rows = ([row(10, sheet="kishan k", pending=500, supplier="Kishan Co")] * 3
            + [row(10, sheet="others", pending=0, supplier="Kishan Co", co="AVPL")]
            + [row(11, sheet="cheena", pending=50, supplier="Cheena Co")] * 2
            + [row(12, pending=900, supplier="History Co")]
            + [row(13, sid=ID["jain"], sheet="kishan k", supplier="Resolved Co")]   # via the MR: not listed
            + [row(14, sheet="jain", supplier="Sheet exact")]                       # rule 2: not listed
            + [row(NAN, sheet="x")])
    hist = pd.DataFrame([dict(party_id=12, jute_supplier_id=ID["dave"], n=12),
                         dict(party_id=12, jute_supplier_id=ID["others"], n=2),
                         dict(party_id=11, jute_supplier_id=ID["others"], n=4)])
    out = S.suggest_party_suppliers(frame(*rows), MASTER, hist, {10: ID["Kishan Kothari"], 11: None})
    assert list(out["party_id"]) == [10, 12, 11]            # biggest pending first
    p10 = out.set_index("party_id").loc[10]
    assert p10["Suggested"] == "Kishan Kothari" and p10["suggested_id"] == ID["Kishan Kothari"]
    assert p10["Basis"] == "Sheet name 'kishan k' on 3 of 4 bills"
    assert p10["Sheet Names"] == "kishan k (3), others (1)"
    assert p10["Confirmed"] == "Kishan Kothari" and p10["Bills"] == 4 and p10["Unpaid Bills"] == 3
    assert p10["Pending"] == 1500 and p10["Companies"] == "ACPL, AVPL"
    p11 = out.set_index("party_id").loc[11]
    assert p11["Suggested"] == "" and p11["Basis"] == "Sheet name 'cheena' has no match in the master"
    assert p11["Confirmed"] == "others"
    p12 = out.set_index("party_id").loc[12]
    assert p12["Suggested"] == "dave" and p12["Basis"] == "Jute supplier on 12 of 14 MRs of this party"
    assert p12["Confirmed"] == ""
    e = S.suggest_party_suppliers(pd.DataFrame(), MASTER, None, {})
    assert e.empty and list(e.columns) == S.SUGGEST_COLUMNS


# ---- review additions (2026-11-24) ---------------------------------------------
def _sugg_row(party_id, party, co, sheet, pending=100.0, jsid=None):
    return {"party_id": party_id, "Supplier": party, "Step 1 Co": co, "sheet_party": sheet,
            "jute_supplier_id": jsid, "Pending": pending, "Bill Amount": pending,
            "MR Date": date(2026, 7, 1)}


def test_suggestion_from_the_partys_own_name_when_the_sheet_name_has_no_match():
    master = {1: "others ", 2: "Shema Jute Agency", 3: "sukumar saha"}
    rows = pd.DataFrame([_sugg_row(10, "SHEMA JUTE AGENCY", "AVPL", "cheena", jsid=1)])
    out = S.suggest_party_suppliers(rows, master, None, {})
    assert out.loc[0, "suggested_id"] == 2
    assert "own name" in out.loc[0, "Basis"] and "cheena" in out.loc[0, "Basis"]


def test_sheet_spellings_of_one_name_count_together():
    master = {1: "others", 2: "Binod Pugalia"}
    rows = pd.DataFrame([_sugg_row(10, "BINOD KUMAR HUF", "AVPL", s, jsid=1)
                         for s in ("Binod P", "binod p", "binod p")])
    out = S.suggest_party_suppliers(rows, master, None, {})
    assert out.loc[0, "Sheet Names"] == "binod p (3)"
    assert out.loc[0, "suggested_id"] == 2


def test_group_suggestions_one_row_per_party_name_and_sheet_name():
    master = {1: "others", 2: "sukumar saha", 3: "rungta", 4: "ss rungta adpl"}
    rows = pd.DataFrame([
        _sugg_row(10, "SHIVAM ENTERPRISES", "AVPL", "sukumar", 300.0, jsid=1),
        _sugg_row(11, "SHIVAM ENTERPRISES", "SCPL", "Sukumar", 200.0, jsid=1),
        _sugg_row(20, "S.S.RUNGTA & COMPANY", "AVPL", "rungta", 50.0, jsid=1),
        _sugg_row(21, "S.S.RUNGTA & COMPANY", "ADSL", "ss rungta adpl", 70.0, jsid=1),
    ])
    grouped = S.group_suggestions(S.suggest_party_suppliers(rows, master, None, {11: 2}))
    # party 11 is already confirmed, so it stays apart from the unconfirmed party 10
    assert sorted(map(tuple, grouped["party_ids"])) == [(10,), (11,), (20,), (21,)]
    grouped = S.group_suggestions(S.suggest_party_suppliers(rows, master, None, {}))
    by_ids = {tuple(r["party_ids"]): r for r in grouped.to_dict("records")}
    assert set(by_ids) == {(10, 11), (20,), (21,)}
    shivam = by_ids[(10, 11)]
    assert shivam["Pending"] == 500.0 and shivam["Companies"] == "AVPL, SCPL"
    assert shivam["Sheet Names"] == "sukumar (2)" and shivam["Suggested"] == "sukumar saha"
    assert by_ids[(21,)]["Suggested"] == "ss rungta adpl"
    assert list(grouped["Pending"]) == sorted(grouped["Pending"], reverse=True)


def test_day_columns_are_whole_numbers_with_na_when_everything_is_held():
    rows = pd.DataFrame([
        {"skey": "S1", "Jute Supplier": "a", "MR Date": date(2026, 7, 1), "Bill Amount": 100.0,
         "Pending": 100.0, "Paid": 0.0, "on_hold": True, "Supplier": "P", "Step 1 Co": "AVPL",
         "Last Payment": None},
    ])
    out = S.supplier_summary(rows, None, date(2026, 7, 11))
    assert str(out["Days Pending"].dtype) == "Int64" and pd.isna(out.loc[0, "Days Pending"])
    assert out.loc[0, "Days Incl. Hold"] == 10


def test_cleared_till_is_a_date_before_the_first_uncovered_bill():
    # two bills on 25-Sep, only one covered: 25-Sep is not cleared yet
    bills = [(date(2026, 9, 20), 100.0, 0.0), (date(2026, 9, 25), 100.0, 0.0),
             (date(2026, 9, 25), 100.0, 100.0)]
    pos = S.fifo_position(bills, today=date(2026, 10, 5))
    assert pos["pending_from"] == date(2026, 9, 25) and pos["cleared_till"] == date(2026, 9, 20)
    assert pos["days_pending"] == 10
