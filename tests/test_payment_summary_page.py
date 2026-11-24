"""Payment Summary page, headless (AppTest) on fixed in-memory data: the
loaders are replaced, supplier_map.save_map is a recorder, so nothing touches
a database and the test only sees what the page WOULD have saved."""
from datetime import date

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

D = date
NAN = float("nan")
MASTER = {1: "others", 2: "Kishan Kothari", 3: "Sukumar Saha"}


EXTRA = {"on": False}     # the same party under a second company's party record


def extra_mrs(mr):
    if EXTRA["on"]:
        yield mr(4, 504, "P Three", 1, D(2026, 7, 12), 30000.0, 30000.0)
        yield mr(5, 505, "P Five", 1, D(2026, 7, 13), 10000.0, 10000.0)


def make_data():
    def mr(i, party, sup_name, jsup, on, amt, pending):
        return dict(jute_mr_id=i, src_jute_mr_id=NAN, branch_mr_no=i, jute_mr_date=on,
                    net_total=amt, payment_terms_days=NAN, payment_due_date=None,
                    party_id=party, jute_supplier_id=jsup, step1_co_id=65, step1_co="ACPL",
                    step1_branch_prefix=None, supplier=sup_name, party="x", gate_co="EJM",
                    ge_no=i, ge_date=on, root_terms_days=NAN, _pending=pending)
    mrs = pd.DataFrame([
        mr(1, 501, "P One", 2, D(2026, 5, 10), 100000.0, 100000.0),
        mr(2, 502, "P Two", 3, D(2026, 6, 10), 200000.0, 200000.0),
        mr(3, 503, "P Three", 1, D(2026, 7, 10), 50000.0, 50000.0),
    ] + list(extra_mrs(mr))).drop(columns=["_pending"])
    acc = pd.DataFrame(columns=["jute_mr_id", "acc_bill_ref_id", "acc_amount", "acc_pending",
                                "acc_due_date", "last_payment_date", "acc_proposed"])
    holds = pd.DataFrame([{"co_id": 65, "party_id": 502, "note": "hold"}])
    return {"mrs": mrs, "first_hop_of": {}, "acc": acc, "opening": pd.DataFrame(),
            "opening_mrs": pd.DataFrame(), "holds": holds}


def make_extras(map_ready=True):
    return {
        "master": pd.DataFrame({"supplier_id": list(MASTER), "supplier_name": list(MASTER.values())}),
        "on_account": pd.DataFrame(columns=["co_id", "co_prefix", "party_id", "supplier",
                                            "amount", "bill_date"]),
        "party_history": pd.DataFrame(columns=["party_id", "jute_supplier_id", "n"]),
        "party_map": {}, "map_ready": map_ready,
    }


@pytest.fixture(autouse=True)
def _restore_patched_names():
    """The page script replaces loaders on the module; put them back afterwards."""
    from src.jutetransfer import supplier_map
    from src.jutetransfer.pages import payment_summary as P
    saved = {n: getattr(P, n) for n in ("load_bills", "load_summary_extras", "clear_cache")}
    save_map = supplier_map.save_map
    yield
    for n, v in saved.items():
        setattr(P, n, v)
    supplier_map.save_map = save_map


def _script():
    import streamlit as st
    from src.jutetransfer.pages import payment_summary as P
    from tests.test_payment_summary_page import make_data, make_extras

    P.load_bills = lambda a, b: make_data()
    P.load_summary_extras = lambda: make_extras(st.session_state.get("_map_ready", True))
    P.clear_cache = lambda: None

    def record(changes, user_id):
        st.session_state["_saved"] = (dict(changes), user_id)
        return len(changes)
    P.supplier_map.save_map = record
    st.session_state.setdefault("user_id", 7)
    P.payment_summary_page()


def _run(role="admin", map_ready=True):
    at = AppTest.from_function(_script, default_timeout=60)
    at.session_state["role"] = role
    at.session_state["_map_ready"] = map_ready
    at.run()
    assert not at.exception, at.exception
    return at


def _metric(at, label):
    return next(m.value for m in at.metric if m.label == label)


def test_page_renders_with_totals():
    at = _run()
    assert not at.error
    assert _metric(at, "Pending") == "₹350,000"
    assert _metric(at, "On hold") == "₹200,000"
    assert _metric(at, "Free to pay") == "₹150,000"
    assert _metric(at, "Suppliers with dues") == "3"


def test_tab_three_is_read_only_without_the_table():
    at = _run(map_ready=False)
    assert any("not switched on" in i.value for i in at.info)
    assert not [b for b in at.button if b.key == "ps_save"]


def test_non_admin_cannot_save():
    at = _run(role="user")
    assert not [b for b in at.button if b.key == "ps_save"]


def test_save_sends_only_the_changed_rows():
    at = _run()
    # the party list: only the 'others' bill (party 503) needs a decision
    at.session_state["ps_map_editor"] = {"edited_rows": {0: {"Confirm as": "Kishan Kothari"}},
                                         "added_rows": [], "deleted_rows": []}
    at.run()
    assert not at.exception, at.exception
    save = next(b for b in at.button if b.key == "ps_save")
    assert not save.disabled
    save.click().run()
    assert not at.exception, at.exception
    assert at.session_state["_saved"] == ({503: 2}, 7)


def test_one_row_per_party_name_saves_every_party_record_of_it():
    """party_mst has one record per company for the same party: 'P Three' is
    503 and 504. It is one row in the list and one confirmation covers both;
    'P Five' (505), left alone, is not sent."""
    EXTRA["on"] = True
    try:
        at = _run()
        at.session_state["ps_map_editor"] = {"edited_rows": {0: {"Confirm as": "Sukumar Saha"}},
                                             "added_rows": [], "deleted_rows": []}
        at.run()
        assert not at.exception, at.exception
        save = next(b for b in at.button if b.key == "ps_save")
        assert "1 changed" in save.label
        save.click().run()
        assert not at.exception, at.exception
        assert at.session_state["_saved"] == ({503: 3, 504: 3}, 7)
    finally:
        EXTRA["on"] = False
