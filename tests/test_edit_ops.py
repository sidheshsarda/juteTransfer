"""Edit mode: a saved step or marked move is changed in place -- same ids,
same numbers -- and every edit is logged (owner ruling 2026-11-17)."""

import json
from datetime import date

import pytest

from src.jutetransfer import access, edit_ops
from src.jutetransfer.jute_mr_chain_helpers import edit_date_problem

from .fake_mysql import fake_db, scenario  # noqa: F401  (fixtures)
from .test_marked_stock import approve, move
from .test_transfer_po_flow import Chain, po_on  # noqa: F401  (po_on is a fixture)

D = date


def edit(sc, chain, step_mr_id, **kw):
    kw.setdefault("reason", "keyed on the wrong day")
    return edit_ops.edit_chain_step(chain.root, step_mr_id, updated_by=sc.user, **kw)


def numbers(db, mr_id):
    mr = db.row("jute_mr", jute_mr_id=mr_id)
    return {k: mr[k] for k in ("branch_mr_no", "jute_gate_entry_no", "bill_pass_no", "invoice_no",
                               "challan_no", "po_id", "total_amount", "claim_amount")}


def incoming_invoice(db, seller_mr_id):
    (link,) = db.rows("sales_invoice_jute", mr_id=seller_mr_id)
    return db.row("sales_invoice", invoice_id=link["invoice_id"])


def test_the_date_window():
    old = D(2026, 9, 5)
    assert edit_date_problem(D(2026, 9, 7), old, D(2026, 9, 1), D(2026, 9, 10)) == (None, False)
    assert edit_date_problem(D(2026, 9, 1), old, D(2026, 9, 1), D(2026, 9, 10)) == (None, False)
    msg, soft = edit_date_problem(D(2026, 9, 12), old, D(2026, 9, 1), D(2026, 9, 10))
    assert soft and "10-09-2026 or earlier" in msg
    msg, soft = edit_date_problem(D(2026, 8, 31), old, None, None)
    assert not soft and "September 2026" in msg
    msg, soft = edit_date_problem(D(2026, 9, 7), old, None, None, today=D(2026, 9, 6))
    assert not soft and "after today" in msg


def test_re_dating_a_hop_keeps_every_number_and_moves_its_invoice_and_po(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    b = chain.save("B", mr_date=D(2026, 9, 5))["mr_id"]
    c = chain.save("C", pct=0.5, mr_date=D(2026, 9, 5))["mr_id"]
    before_b, before_c = numbers(db, b), numbers(db, c)
    invoice_no = incoming_invoice(db, b)["invoice_no"]

    changed = edit(sc, chain, c, new_date=D(2026, 9, 8))
    assert changed["mr_date"] == [D(2026, 9, 5), D(2026, 9, 8)]

    mr = db.row("jute_mr", jute_mr_id=c)
    assert (mr["jute_mr_date"], mr["jute_gate_entry_date"], mr["bill_pass_date"],
            mr["invoice_date"], mr["challan_date"]) == (D(2026, 9, 8),) * 5
    assert numbers(db, c) == before_c and numbers(db, b) == before_b     # nothing renumbered
    inv = incoming_invoice(db, b)                                        # the sale B -> C
    assert (inv["invoice_date"], inv["challan_date"], inv["invoice_no"]) == (
        D(2026, 9, 8), D(2026, 9, 8), invoice_no)
    assert db.row("jute_po", jute_po_id=mr["po_id"])["po_date"] == D(2026, 9, 8)
    assert db.row("jute_mr", jute_mr_id=b)["jute_mr_date"] == D(2026, 9, 5)   # B untouched

    (log,) = db.rows("jt_change_log")
    assert (log["action"], log["jute_mr_id"], log["reason"], log["user_id"]) == (
        "edit-step", c, "keyed on the wrong day", sc.user)
    detail = json.loads(log["detail"])
    assert detail["mr_date"] == ["2026-09-05", "2026-09-08"] and detail["root_mr_id"] == chain.root


def test_the_first_hop_keeps_the_lorrys_challan_date(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    b = chain.save("B", mr_date=D(2026, 9, 5))["mr_id"]
    challan = db.row("jute_mr", jute_mr_id=b)["challan_date"]
    edit(sc, chain, b, new_date=D(2026, 9, 6))
    mr = db.row("jute_mr", jute_mr_id=b)
    assert mr["jute_mr_date"] == D(2026, 9, 6) and mr["challan_date"] == challan


def test_re_dating_the_finalized_mill_mr(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    b = chain.save("B", mr_date=D(2026, 9, 5))["mr_id"]
    chain.save("A", pct=0.5, mr_date=D(2026, 9, 6))
    root_before = db.row("jute_mr", jute_mr_id=chain.root)
    edit(sc, chain, chain.root, new_date=D(2026, 9, 9))
    root = db.row("jute_mr", jute_mr_id=chain.root)
    assert (root["jute_mr_date"], root["bill_pass_date"], root["invoice_date"]) == (D(2026, 9, 9),) * 3
    assert root["jute_gate_entry_date"] == root_before["jute_gate_entry_date"]   # the real gate entry
    for k in ("branch_mr_no", "bill_pass_no", "invoice_no", "total_amount", "net_total", "status_id"):
        assert root[k] == root_before[k]
    assert incoming_invoice(db, b)["invoice_date"] == D(2026, 9, 9)              # the sale B -> A
    (final_po,) = [p for p in db.rows("jute_po") if "JT|FINAL|" in (p["internal_note"] or "")]
    assert final_po["po_date"] == D(2026, 9, 9)


def test_a_date_out_of_the_branchs_order_needs_the_tick(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    b = chain.save("B", mr_date=D(2026, 9, 5))["mr_id"]
    Chain(sc, root=sc.add_root(ge_no=9)).save("B", mr_date=D(2026, 9, 10))      # B's MR no. 2
    before = db.snapshot()
    with pytest.raises(ValueError, match="10-09-2026 or earlier"):
        edit(sc, chain, b, new_date=D(2026, 9, 12))
    assert db.snapshot() == before
    assert edit(sc, chain, b, new_date=D(2026, 9, 10))                           # the same day is fine
    assert edit(sc, chain, b, new_date=D(2026, 9, 12), allow_out_of_order=True)


@pytest.mark.parametrize("new_date, message", [
    (D(2026, 10, 1), "only move within September 2026"),
    (D(2026, 8, 31), "only move within September 2026"),
])
def test_another_month_is_refused_even_with_the_tick(scenario, po_on, new_date, message):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    b = chain.save("B", mr_date=D(2026, 9, 5))["mr_id"]
    before = db.snapshot()
    with pytest.raises(ValueError, match=message):
        edit(sc, chain, b, new_date=new_date, allow_out_of_order=True)
    assert db.snapshot() == before


def test_changing_a_hops_godown(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    b = chain.save("B", mr_date=D(2026, 9, 5))["mr_id"]
    other = db.insert("warehouse_mst", warehouse_name="B second godown", branch_id=sc.b_branch,
                      warehouse_type="J")
    changed = edit(sc, chain, b, new_warehouse_id=other)
    assert changed == {"warehouse_id": [[sc.godown("B")], other]}
    assert {l["warehouse_id"] for l in db.rows("jute_mr_li", jute_mr_id=b)} == {other}
    assert edit(sc, chain, b, new_warehouse_id=other) == {}                      # already there
    assert len(db.rows("jt_change_log")) == 1
    with pytest.raises(ValueError, match="does not belong to this MR's branch"):
        edit(sc, chain, b, new_warehouse_id=sc.godown("C"))


def test_the_mills_godown_is_not_edited_here(scenario, po_on):
    sc = scenario
    chain = Chain(sc)
    chain.save("B", mr_date=D(2026, 9, 5))
    chain.save("A", pct=0.5, mr_date=D(2026, 9, 6))
    with pytest.raises(ValueError, match="set in the ERP"):
        edit(sc, chain, chain.root, new_warehouse_id=sc.godown("A"))


def test_a_reason_and_an_admin_are_required(scenario, po_on, monkeypatch):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    b = chain.save("B", mr_date=D(2026, 9, 5))["mr_id"]
    before = db.snapshot()
    with pytest.raises(ValueError, match="reason is required"):
        edit(sc, chain, b, new_date=D(2026, 9, 6), reason="  ")
    monkeypatch.delenv("JT_LEGACY_LOGIN", raising=False)
    monkeypatch.setattr(access, "get_role", lambda uid: "user")
    with pytest.raises(access.NotAllowed):
        edit(sc, chain, b, new_date=D(2026, 9, 6))
    assert db.snapshot() == before


def test_re_dating_a_marked_move(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    (res,) = move(sc, [sc.root_lines[0]], mr_date=D(2026, 9, 5))
    child = res["child_mr_id"]
    before = db.row("jute_mr", jute_mr_id=child)
    changed = edit_ops.edit_marked_move(child, new_date=D(2026, 9, 7), reason="wrong day",
                                        updated_by=sc.user)
    assert changed["mr_date"] == [D(2026, 9, 5), D(2026, 9, 7)]
    mr = db.row("jute_mr", jute_mr_id=child)
    assert (mr["jute_mr_date"], mr["jute_gate_entry_date"], mr["invoice_date"]) == (D(2026, 9, 7),) * 3
    for k in ("branch_mr_no", "jute_gate_entry_no", "bill_pass_no", "invoice_no", "net_total",
              "challan_date"):
        assert mr[k] == before[k]
    assert incoming_invoice(db, child)["invoice_date"] == D(2026, 9, 7)
    with pytest.raises(ValueError, match="not a marked move"):
        edit_ops.edit_marked_move(sc.root, new_date=D(2026, 9, 7), reason="x", updated_by=sc.user)
