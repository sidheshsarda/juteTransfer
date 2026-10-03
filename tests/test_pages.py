"""The Streamlit pages, headless (streamlit.testing.v1.AppTest) on fixed
in-memory data: no database, and every write function the pages call is
replaced by a recorder, so these tests only ever see what a page WOULD have
posted.

The chain page's bug these tests pin down (round-2 review P1): the '% Rate
Increase' box showed one figure while Save posted another -- a plain
session-state key the box was seeded from outlived the step dict after a
save or delete on ANOTHER lorry of the same month. The box is now seeded
from, and compared with, its step dict only.
"""
from datetime import date

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from src.jutetransfer import po_queries
from src.jutetransfer.pages import new_transfer_chain as chain_page
from src.jutetransfer.pages import po_tracker as tracker_page
from src.jutetransfer.pages import warehouse_stock as wh_page

# --- the fixed world: the mill EJM (co 2 / branch 29), forwarders JTSPL and GMPL ---------

EJM, JTSPL, GMPL = (2, 29), (74, 20), (27, 33)
COMPANIES = {"THE EMPIRE JUTE COMPANY LTD.": 2, "Jagrati Trade Services Pvt. Ltd.": 74,
             "Greeting Marketing Pvt. Ltd.": 27}
BRANCHES = {2: {"FACTORY": 29}, 74: {"FACTORY": 20}, 27: {"FACTORY": 33}}
CB_OPTIONS = ["", "EJM-FACTORY", "GMPL-FACTORY", "JTSPL-FACTORY"]
CB_MAP = {"EJM-FACTORY": EJM, "GMPL-FACTORY": GMPL, "JTSPL-FACTORY": JTSPL}
GODOWNS = {29: {291: "EJM_JUTE", 292: "EJM_JUTE-2"}, 20: {201: "JTSPL_JUTE"},
           33: {112: "1", 113: "GMPL_JUTE"}}
LINES = [(4729.0, 13000.0), (4729.0, 12800.0)]        # kg, rate per quintal; no claim
ROOT_TOTAL = 1220082.0                                 # 4729 @ 13000 + 4729 @ 12800
TOTAL_AT_HALF_PCT = 1226182.0                          # the pilot's return leg at +0.50 %

A_ROOT, A_HOP = 101, 201        # GE 68, one hop saved at JTSPL, step 2 open
B_ROOT, B_HOP = 102, 202        # GE 66, same shape
C_ROOT, C_HOP = 103, 203        # GE 21, finalized: back at the mill as MR 11
FINAL_MONEY = dict(total=1385746.0, claim=13757.0, tds=1385.75, roundoff=0.04,
                   net=1370603.0, invoice=1385746.0)


def _month_rows():
    rows = []
    lorries = [(A_ROOT, 68, None, 13, "Pending (Transfer)", None),
               (B_ROOT, 66, None, 13, "Pending (Transfer)", None),
               (C_ROOT, 21, 11, 3, "Approved", FINAL_MONEY)]
    for root, ge_no, mill_mr_no, status, status_name, money in lorries:
        for n, (kg, rate) in enumerate(LINES):
            rows.append({
                "jute_mr_id": root, "jute_mr_li_id": root * 10 + n,
                "Jute Gate Entry No": ge_no, "Jute Gate Entry Date": date(2026, 9, 20),
                "EJM MR No.": mill_mr_no, "MR DATE": date(2026, 9, 20),
                "Jute Supplier": "sukumar saha", "Party Name": "HONEYWELL",
                "Item Quality": f"TD-{5 + n}", "Weight (KG)": kg,
                "status_id_raw": status, "Status": status_name,
                "MR Rate": rate, "Total Amount": kg * rate / 100,
                "Claim Rate": 0.0, "Claim Amount": 0.0, "Net Total": kg * rate / 100,
                "Challan Date": date(2026, 9, 19), "Warehouse": "EJM_JUTE",
                "mr_total_amount": money["total"] if money else ROOT_TOTAL,
                "mr_claim_amount": money["claim"] if money else 0.0,
                "mr_tds_amount": money["tds"] if money else 0.0,
                "mr_roundoff": money["roundoff"] if money else 0.0,
                "mr_net_total": money["net"] if money else ROOT_TOTAL,
                "mr_invoice_amount": money["invoice"] if money else None,
            })
    return pd.DataFrame(rows)


def _hop_rows(hop_id):
    return pd.DataFrame([{
        "jute_mr_id": hop_id, "src_com_id": 2, "branch_id": 20,
        "jute_mr_date": date(2026, 9, 22), "challan_date": date(2026, 9, 19),
        "branch_mr_no": 50, "total_amount": ROOT_TOTAL, "claim_amount": 0.0,
        "tds_amount": 0.0, "roundoff": 0.0, "net_total": ROOT_TOTAL,
        "owner_co_id": 74, "branch_name": "FACTORY", "co_name": "Jagrati Trade Services Pvt. Ltd.",
        "co_prefix": "JTSPL", "branch_prefix": None,
        "transfer_po_id": None, "transfer_po_no": None, "transfer_po_date": None,
        "transfer_po_weight": None, "transfer_po_value": None, "transfer_po_note": None,
    }])


CHAINS = {A_ROOT: _hop_rows(A_HOP), B_ROOT: _hop_rows(B_HOP), C_ROOT: _hop_rows(C_HOP)}


def _hop_line_items(mr_id):
    return [{"weight": kg, "original_rate": rate, "original_claim": 0.0,
             "item_quality": f"TD-{5 + n}", "warehouse_id": 201}
            for n, (kg, rate) in enumerate(LINES)]


class Recorder:
    """What the page handed to the (stubbed) write functions."""

    def __init__(self):
        self.saves, self.deletes = [], []
        self.save_result = {"mr_id": 999, "invoice_id": None, "po": None}
        self.delete_result = {"deleted_mr_ids": [], "deleted_pos": [], "reverted": False}
        self.save_raises = None
        self.month_raises = None

    def save(self, **kw):
        step = kw.pop("step")
        kw["step"] = {k: getattr(step, k) for k in
                      ("co_id", "branch_id", "mr_date", "pct_rate_increase", "total_amount",
                       "claim_amount", "net_amount", "warehouse_id", "lc_reference_no")}
        self.saves.append(kw)
        if self.save_raises:
            raise self.save_raises
        return self.save_result

    def delete(self, **kw):
        self.deletes.append(kw)
        return self.delete_result

    @property
    def last_posted(self):
        last = self.saves[-1]
        return (last["step"]["pct_rate_increase"], last["rate_multiplier"],
                last["step"]["total_amount"])


@pytest.fixture
def chain(monkeypatch):
    rec = Recorder()
    month_frame = _month_rows()

    def load_month(year, month, company_id=None, branch_id=None):
        if rec.month_raises:
            raise rec.month_raises
        return month_frame.copy()

    monkeypatch.setattr(chain_page, "get_companies", lambda: dict(COMPANIES))
    monkeypatch.setattr(chain_page, "get_branches_by_company",
                        lambda co: dict(BRANCHES.get(int(co), {})))
    monkeypatch.setattr(chain_page, "get_company_branch_options",
                        lambda: (list(CB_OPTIONS), dict(CB_MAP)))
    monkeypatch.setattr(chain_page, "get_jute_mr_with_line_items", load_month)
    monkeypatch.setattr(chain_page, "get_transfer_chain", lambda root: CHAINS.get(int(root)))
    monkeypatch.setattr(chain_page, "get_original_po", lambda root: None)
    rec.godowns = {b: dict(g) for b, g in GODOWNS.items()}
    monkeypatch.setattr(chain_page, "get_warehouses_by_branch",
                        lambda b: dict(rec.godowns.get(int(b), {})))
    monkeypatch.setattr(chain_page, "get_invoice_details_by_mr_id", lambda mr: None)
    monkeypatch.setattr(chain_page, "_fetch_step_line_items", _hop_line_items)
    monkeypatch.setattr(chain_page, "transfer_po_enabled", lambda: True)
    monkeypatch.setattr(po_queries, "get_final_transfer_po", lambda root: None)
    monkeypatch.setattr(po_queries, "clear_tracker_cache", lambda: None)
    monkeypatch.setattr(chain_page, "save_transfer_step", rec.save)
    monkeypatch.setattr(chain_page, "delete_chain_from_step", rec.delete)
    chain_page._company_branch_map.clear()
    return rec


def _chain_script():
    """The page, then the row the test asked for: AppTest has no dataframe
    selection events, so the lorry is picked through session state and the
    editor rendered the way the page does once a row is selected."""
    import streamlit as st
    from src.jutetransfer.pages import new_transfer_chain as p
    st.session_state.setdefault("user_id", 1)
    p.transfer_chain_page()
    pick = st.session_state.get("_pick")
    if pick is not None:
        fk = (f"{st.session_state['selected_company_id']}_{st.session_state['selected_branch_id']}_"
              f"{st.session_state['selected_year']}_{st.session_state['selected_month']}")
        df = st.session_state.get(f"source_df_{fk}")
        if df is not None:
            ids = df["jute_mr_id"].astype(int).tolist()
            if pick in ids:
                st.session_state[f"selected_row_{fk}"] = ids.index(pick)
                p._render_chain_editor(fk)


def _start(pick=None):
    at = AppTest.from_function(_chain_script, default_timeout=60)
    if pick is not None:
        at.session_state["_pick"] = pick
    _run(at)
    return at


def _run(at):
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _fk(at):
    ss = at.session_state
    return f"{ss['selected_company_id']}_{ss['selected_branch_id']}_{ss['selected_year']}_{ss['selected_month']}"


def _steps(at, root):
    return (at.session_state[f"transfers_{_fk(at)}"] or {}).get(root, [])


def _texts(at, kind):
    return [" ".join(str(e.value).split()) for e in getattr(at, kind)]


def _pick(at, root):
    at.session_state["_pick"] = root
    return _run(at)


def _open_step_2(at, root, company="EJM-FACTORY", pct=None):
    at.selectbox(key=f"company_{root}_1").set_value(company)
    _run(at)
    if pct is not None:
        at.number_input(key=f"pct_input_{root}_1").set_value(pct)
        _run(at)


# --- P1: the % box, the Total and the posted % are one figure ---------------------------

def test_a_percent_typed_on_one_lorry_survives_a_save_on_another_and_is_what_gets_posted(chain):
    """Type 0.50 on lorry A, save lorry B (same month), come back to A: the
    box, the Total under it and the Save all say +0.50 % (the box used to
    show 0.50 while the save posted 0 %)."""
    at = _start(A_ROOT)
    _open_step_2(at, A_ROOT, pct=0.5)
    assert _steps(at, A_ROOT)[1]["pct_rate_increase"] == 0.5
    assert _steps(at, A_ROOT)[1]["total_amount"] == TOTAL_AT_HALF_PCT

    _pick(at, B_ROOT)
    _open_step_2(at, B_ROOT, pct=0.25)
    at.button(key=f"save_{B_ROOT}_1").click()
    _run(at)
    # 13000 x 1.0025 = 130.325/kg -> 130.33; 12800 -> 128.32: 616,330.57 + 606,825.28
    assert chain.last_posted == (0.25, 1.0025, 1223156.0)
    assert any(t.startswith("Step 2 saved") for t in _texts(at, "success"))
    # the saved lorry's state is rebuilt from the database; A's typed step is kept
    assert B_ROOT not in at.session_state[f"transfers_{_fk(at)}"] or \
        _steps(at, B_ROOT)[1].get("pct_rate_increase", 0) == 0
    assert _steps(at, A_ROOT)[1]["pct_rate_increase"] == 0.5

    _pick(at, A_ROOT)
    assert at.selectbox(key=f"company_{A_ROOT}_1").value == "EJM-FACTORY"
    box = at.number_input(key=f"pct_input_{A_ROOT}_1").value
    step = _steps(at, A_ROOT)[1]
    assert box == step["pct_rate_increase"] == 0.5
    assert step["total_amount"] == TOTAL_AT_HALF_PCT
    assert any(f"₹{TOTAL_AT_HALF_PCT:,.0f}" in t for t in _texts(at, "markdown"))
    at.button(key=f"save_{A_ROOT}_1").click()
    _run(at)
    assert chain.last_posted == (0.5, 1.005, TOTAL_AT_HALF_PCT)
    assert chain.saves[-1]["root_mr_id"] == A_ROOT and chain.saves[-1]["is_final"] is True


def test_a_percent_typed_on_one_lorry_survives_a_delete_on_another(chain):
    at = _start(A_ROOT)
    _open_step_2(at, A_ROOT, pct=0.5)
    _pick(at, B_ROOT)
    chain.delete_result = {"deleted_mr_ids": [B_HOP], "deleted_pos": [], "reverted": False}
    at.checkbox(key=f"confirm_delete_{B_ROOT}_0").check()
    _run(at)
    at.button(key=f"delete_saved_{B_ROOT}_0").click()
    _run(at)
    assert chain.deletes[-1] == {"root_mr_id": B_ROOT, "from_mr_id": B_HOP, "updated_by": 1}
    assert "Deleted from step 1 — MR removed." in _texts(at, "success")

    _pick(at, A_ROOT)
    box = at.number_input(key=f"pct_input_{A_ROOT}_1").value
    assert box == _steps(at, A_ROOT)[1]["pct_rate_increase"] == 0.5
    at.button(key=f"save_{A_ROOT}_1").click()
    _run(at)
    assert chain.last_posted == (0.5, 1.005, TOTAL_AT_HALF_PCT)


def test_no_shadow_percent_key_is_kept_anywhere(chain):
    at = _start(A_ROOT)
    _open_step_2(at, A_ROOT, pct=0.5)
    pct_keys = [k for k in at.session_state if str(k).startswith("pct_")]
    assert pct_keys == [f"pct_input_{A_ROOT}_1"]


def test_a_stale_box_follows_the_step_dict_never_the_other_way_round(chain):
    """Whatever is left in a box key when a chain is rebuilt (here: the
    rebuilt dict says 0 while the box still says 0.5), the dict follows the
    box in the same run and the chain is recomputed -- there is no run in
    which the two differ."""
    at = _start(A_ROOT)
    _open_step_2(at, A_ROOT, pct=0.5)
    at.session_state[f"transfers_{_fk(at)}"][A_ROOT][1]["pct_rate_increase"] = 0.0
    _run(at)
    step = _steps(at, A_ROOT)[1]
    assert at.number_input(key=f"pct_input_{A_ROOT}_1").value == step["pct_rate_increase"] == 0.5
    assert step["total_amount"] == TOTAL_AT_HALF_PCT


def test_percent_and_save_in_the_same_run_post_the_typed_percent(chain):
    """A % typed and Save tapped together (one rerun on a phone) used to lose
    the tap to an st.rerun() in the % handler."""
    at = _start(A_ROOT)
    _open_step_2(at, A_ROOT)
    at.number_input(key=f"pct_input_{A_ROOT}_1").set_value(0.5)
    at.button(key=f"save_{A_ROOT}_1").click()
    _run(at)
    assert chain.last_posted == (0.5, 1.005, TOTAL_AT_HALF_PCT)


def test_the_total_is_right_in_the_run_the_company_is_picked(chain):
    at = _start(A_ROOT)
    at.selectbox(key=f"company_{A_ROOT}_1").set_value("EJM-FACTORY")
    _run(at)
    assert _steps(at, A_ROOT)[1]["total_amount"] == ROOT_TOTAL
    assert any(f"₹{ROOT_TOTAL:,.0f}" in t and "Total:" in t for t in _texts(at, "markdown"))


# --- P8: Reload keeps what was typed, consistently -----------------------------------------

def test_reload_keeps_the_typed_company_date_lc_and_percent(chain):
    at = _start(A_ROOT)
    _open_step_2(at, A_ROOT, pct=0.5)
    at.text_input(key=f"lc_ref_{A_ROOT}_1").input("LC-123")
    _run(at)
    at.button(key=f"reload_{_fk(at)}").click()
    _run(at)
    step = _steps(at, A_ROOT)[1]
    assert at.selectbox(key=f"company_{A_ROOT}_1").value == "EJM-FACTORY"
    assert at.text_input(key=f"lc_ref_{A_ROOT}_1").value == "LC-123"
    assert at.number_input(key=f"pct_input_{A_ROOT}_1").value == step["pct_rate_increase"] == 0.5
    assert step["total_amount"] == TOTAL_AT_HALF_PCT
    at.button(key=f"save_{A_ROOT}_1").click()
    _run(at)
    assert chain.last_posted == (0.5, 1.005, TOTAL_AT_HALF_PCT)
    assert chain.saves[-1]["step"]["lc_reference_no"] == "LC-123"


def test_a_chain_changed_in_the_database_is_rebuilt_not_edited_over(chain, monkeypatch):
    """Another session saved the return leg of A: after a Reload the kept
    step 2 (typed 0.5) is gone and the chain shows what the database holds."""
    at = _start(A_ROOT)
    _open_step_2(at, A_ROOT, pct=0.5)
    finalized = _month_rows()
    mask = finalized["jute_mr_id"] == A_ROOT
    finalized.loc[mask, "EJM MR No."] = 12
    finalized.loc[mask, "status_id_raw"] = 3
    monkeypatch.setattr(chain_page, "get_jute_mr_with_line_items",
                        lambda *a, **k: finalized.copy())
    at.button(key=f"reload_{_fk(at)}").click()
    _run(at)
    steps = _steps(at, A_ROOT)
    assert [s.get("saved_mr_id") for s in steps] == [A_HOP, A_ROOT]
    assert steps[1]["is_final_return"] and not any(not s.get("saved_mr_id") for s in steps)
    assert f"pct_input_{A_ROOT}_1" not in at.session_state


# --- P2: the godown follows the company ------------------------------------------------------

def test_changing_the_company_drops_the_godown_picked_for_the_previous_one(chain):
    at = _start(A_ROOT)
    _open_step_2(at, A_ROOT, company="GMPL-FACTORY")
    at.selectbox(key=f"wh_{A_ROOT}_1_33").set_value(112)
    _run(at)
    assert _steps(at, A_ROOT)[1]["warehouse_id"] == 112
    at.selectbox(key=f"company_{A_ROOT}_1").set_value("EJM-FACTORY")
    _run(at)
    assert at.selectbox(key=f"wh_{A_ROOT}_1_29").value is None
    assert _steps(at, A_ROOT)[1]["warehouse_id"] is None
    at.button(key=f"save_{A_ROOT}_1").click()
    _run(at)
    assert chain.saves[-1]["step"]["warehouse_id"] is None
    assert (chain.saves[-1]["step"]["co_id"], chain.saves[-1]["step"]["branch_id"]) == EJM


def test_a_godown_that_is_not_at_the_chosen_branch_is_refused_at_save(chain):
    """The box writes the step dict on every run, so the save-time check is
    the belt under it: a step dict carrying another branch's godown (112 is
    GMPL's) is refused by _save_step itself, before anything is posted."""
    def script():
        # (AppTest runs the function's source as a script: no closure, so the
        # fixtures are imported here)
        from datetime import date
        import streamlit as st
        from src.jutetransfer.jute_mr_chain_helpers import _empty_transfer_step
        from src.jutetransfer.pages import new_transfer_chain as p
        from tests.test_pages import A_HOP, A_ROOT, ROOT_TOTAL, _month_rows
        fk = "2_29_2026_9"
        st.session_state.update({
            "selected_company_id": 2, "selected_branch_id": 29,
            f"source_df_{fk}": _month_rows().iloc[:1], f"selected_row_{fk}": 0,
        })
        saved = dict(_empty_transfer_step(), company="JTSPL-FACTORY", saved_mr_id=A_HOP,
                     total_amount=ROOT_TOTAL)
        step = dict(_empty_transfer_step(), company="EJM-FACTORY", mr_date=date(2026, 9, 25),
                    warehouse_id=112, total_amount=ROOT_TOTAL, net_amount=ROOT_TOTAL)
        p._save_step(1, step, [saved, step], [], ROOT_TOTAL, A_ROOT, fk)
        st.session_state["_step_after"] = dict(step)

    at = AppTest.from_function(script, default_timeout=60)
    _run(at)
    assert chain.saves == []
    assert _texts(at, "error") == ["The godown picked is not at EJM-FACTORY. Pick the godown "
                                   "again (the box was reset when the company changed)."]
    assert at.session_state["_step_after"]["warehouse_id"] is None


def test_the_chosen_godown_is_posted(chain):
    at = _start(A_ROOT)
    _open_step_2(at, A_ROOT)
    at.selectbox(key=f"wh_{A_ROOT}_1_29").set_value(292)
    _run(at)
    at.button(key=f"save_{A_ROOT}_1").click()
    _run(at)
    assert chain.saves[-1]["step"]["warehouse_id"] == 292


# --- P3: the finalized card shows the ERP's net ---------------------------------------------

def test_the_finalized_card_shows_the_stored_net_and_the_tds(chain):
    at = _start(C_ROOT)
    steps = _steps(at, C_ROOT)
    assert [s.get("saved_mr_id") for s in steps] == [C_HOP, C_ROOT]
    final = steps[1]
    assert (final["total_amount"], final["claim_amount"], final["net_amount"]) == (
        FINAL_MONEY["total"], FINAL_MONEY["claim"], FINAL_MONEY["net"])
    cards = [t for t in _texts(at, "markdown") if t.startswith("**Total:**")]
    assert cards[-1] == ("**Total:** ₹1,385,746 | **Claim:** ₹13,757 | **TDS:** ₹1,386 | "
                         "**Net:** ₹1,370,603")
    assert cards[0] == "**Total:** ₹1,220,082 | **Claim:** ₹0 | **Net:** ₹1,220,082"
    # the final step's % is back-calculated from the stored totals
    assert round(final["pct_rate_increase"], 2) == round((1385746 - 1220082) / 1220082 * 100, 2)


# --- P4: a failing reload ends in a message, not a traceback page -----------------------------

def test_a_database_error_during_reload_shows_a_message_and_nothing_else(chain):
    at = _start(A_ROOT)
    _open_step_2(at, A_ROOT, pct=0.5)
    chain.month_raises = RuntimeError("Lost connection to MySQL server")
    at.button(key=f"reload_{_fk(at)}").click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("Lost connection" in t and "Reload" in t for t in _texts(at, "error"))
    assert f"source_df_{_fk(at)}" not in at.session_state       # nothing half-loaded
    # the next Reload works again (the typed step is gone with the failed run,
    # as every widget of a card that could not be shown is)
    chain.month_raises = None
    at.button(key=f"reload_{_fk(at)}").click()
    _run(at)
    assert [s.get("saved_mr_id") for s in _steps(at, A_ROOT)] == [A_HOP, None]
    assert not _texts(at, "error")


# --- P9: deleting saved steps takes a confirming tick --------------------------------------------

def test_delete_from_step_needs_the_tick_and_names_what_goes(chain):
    at = _start(A_ROOT)
    button = at.button(key=f"delete_saved_{A_ROOT}_0")
    assert button.disabled                 # AppTest, like a browser, cannot tap it
    label = at.checkbox(key=f"confirm_delete_{A_ROOT}_0").label
    assert label == "Yes, delete step 1: MR 50 (JTSPL-FACTORY) and its transfer PO"
    _run(at)
    assert chain.deletes == []
    at.checkbox(key=f"confirm_delete_{A_ROOT}_0").check()
    _run(at)
    assert not at.button(key=f"delete_saved_{A_ROOT}_0").disabled
    chain.delete_result = {"deleted_mr_ids": [A_HOP], "deleted_pos": ["JTSPL/JPO/26-27/00065"],
                           "reverted": False}
    at.button(key=f"delete_saved_{A_ROOT}_0").click()
    _run(at)
    assert chain.deletes == [{"root_mr_id": A_ROOT, "from_mr_id": A_HOP, "updated_by": 1}]
    assert ("Deleted from step 1 — MR removed; transfer PO JTSPL/JPO/26-27/00065 removed."
            in _texts(at, "success"))


def test_un_finalize_tick_names_the_mill_mr(chain):
    at = _start(C_ROOT)
    labels = [at.checkbox(key=f"confirm_delete_{C_ROOT}_{i}").label for i in range(2)]
    # the first hop has no invoice of its own; the return leg's goes with the un-finalize
    assert labels[0] == ("Yes, delete steps 1–2: MR 50 (JTSPL-FACTORY) and its transfer PO; "
                         "the mill's MR goes back to Pending (invoice and Final PO removed)")
    assert labels[1] == ("Yes, un-finalize: remove the mill's MR 11 (EJM-FACTORY), its invoice "
                         "and the Final PO — the mill's MR goes back to Pending")
    chain.delete_result = {"deleted_mr_ids": [], "deleted_pos": ["EJM/F/JPO/26-27/00063"],
                           "reverted": True}
    at.checkbox(key=f"confirm_delete_{C_ROOT}_1").check()
    _run(at)
    at.button(key=f"delete_saved_{C_ROOT}_1").click()
    _run(at)
    assert chain.deletes[-1]["from_mr_id"] == C_ROOT
    assert ("Un-finalized — the mill's MR is back to Pending and the invoice is removed; "
            "transfer PO EJM/F/JPO/26-27/00063 removed." in _texts(at, "success"))


# --- the NICE items ------------------------------------------------------------------------------

def test_no_debug_captions_and_a_plain_total_row(chain):
    at = _start(A_ROOT)
    assert not [t for t in _texts(at, "caption") if "DEBUG" in t]
    cells = pd.concat([df.value for df in at.dataframe if "Quality" in df.value.columns])
    assert "TOTAL" in set(cells["Quality"]) and "**TOTAL**" not in set(cells["Quality"])
    assert not any(str(v).startswith("**") for v in cells["Amount"])


def test_a_save_error_is_a_plain_message_and_the_step_is_kept(chain):
    at = _start(A_ROOT)
    _open_step_2(at, A_ROOT, pct=0.5)
    chain.save_raises = ValueError("Deadlock found when trying to get lock")
    at.button(key=f"save_{A_ROOT}_1").click()
    _run(at)
    errors = _texts(at, "error")
    assert errors == ["Step 2 was not saved: Deadlock found when trying to get lock. Nothing "
                      "was written. Tap Reload above to read the chain again, then save once more."]
    assert not [t for t in _texts(at, "markdown") if "Traceback" in t]
    assert _steps(at, A_ROOT)[1]["pct_rate_increase"] == 0.5


def test_clear_empties_the_whole_step_and_delete_leaves_a_step_to_type_into(chain):
    at = _start(A_ROOT)
    _open_step_2(at, A_ROOT, pct=0.5)
    at.text_input(key=f"lc_ref_{A_ROOT}_1").input("LC-9")
    _run(at)
    at.button(key=f"clear_{A_ROOT}_1").click()
    _run(at)
    assert at.selectbox(key=f"company_{A_ROOT}_1").value == ""
    assert _steps(at, A_ROOT)[1]["pct_rate_increase"] == 0.0
    assert f"lc_ref_{A_ROOT}_1" not in at.session_state
    _open_step_2(at, A_ROOT, pct=0.25)
    at.button(key=f"delete_{A_ROOT}_1").click()
    _run(at)
    steps = _steps(at, A_ROOT)
    assert len(steps) == 2 and not steps[1].get("company") and steps[1]["pct_rate_increase"] == 0.0
    assert at.selectbox(key=f"company_{A_ROOT}_1").value == ""


def test_flash_after_a_save_is_a_banner_and_a_long_toast(chain):
    at = _start(A_ROOT)
    _open_step_2(at, A_ROOT, pct=0.5)
    chain.save_result = {"mr_id": None, "invoice_id": 1, "po": {
        "po_id": 1, "po_no": 999, "po_no_formatted": "EJM/F/JPO/26-27/00999", "skipped": None}}
    at.button(key=f"save_{A_ROOT}_1").click()
    _run(at)
    text = "Step 2 saved — returned to EJM-FACTORY, Final PO EJM/F/JPO/26-27/00999 created."
    assert text in _texts(at, "success") and text in _texts(at, "toast")
    _run(at)
    assert text not in _texts(at, "success")                    # shown once


def test_the_filters_survive_a_visit_to_another_page(chain):
    def script():
        import streamlit as st
        from src.jutetransfer.pages import new_transfer_chain as p
        if st.session_state.get("_page", "chain") == "chain":
            p.transfer_chain_page()
        else:
            st.write("another page")

    at = AppTest.from_function(script, default_timeout=60)
    _run(at)
    at.selectbox(key="company_select").set_value("Jagrati Trade Services Pvt. Ltd.")
    _run(at)
    at.selectbox(key="month_select").set_value(8)
    _run(at)
    at.session_state["_page"] = "other"
    _run(at)
    at.session_state["_page"] = "chain"
    _run(at)
    assert at.selectbox(key="company_select").value == "Jagrati Trade Services Pvt. Ltd."
    assert at.selectbox(key="month_select").value == 8
