"""Fixes from the 2026-10-02 code review of the transfer-PO feature, end to
end on the in-memory MySQL stand-in (tests/fake_mysql.py): stale-tab guards,
a Final PO only for a finalized root, later-hop suppliers without new map
rows, deletes by primary key, money exactly as the ERP writes it (194Q TDS),
targeted transfer-PO deletes, the kill-switch spellings and crop years."""
import re
from datetime import date

import pytest

from src.jutetransfer import po_ops, transfer
from src.jutetransfer.database import DatabaseConnection
from src.jutetransfer.po_helpers import ROLE_FINAL, ROLE_FORWARD, build_po_lines, po_remarks

from .fake_mysql import fake_db, scenario  # noqa: F401  (fixtures)
from .test_transfer_po_flow import Chain, po_on  # noqa: F401  (po_on is a fixture)


def in_txn(fn, *args, **kwargs):
    with DatabaseConnection.get_transaction() as conn:
        return fn(conn, *args, **kwargs)


# --- a stale tab can no longer bend a chain --------------------------------------------------

def test_a_stale_tab_cannot_save_a_later_step_twice(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    stale = Chain(sc)
    stale.steps = list(chain.steps)                 # a tab loaded after step 1 only
    chain.save("C", pct=0.5, mr_date=date(2026, 9, 2))
    before = db.snapshot()
    with pytest.raises(ValueError, match="has changed since this page was loaded"):
        stale.save("C", pct=0.5, mr_date=date(2026, 9, 2))
    assert db.diff(before, db.snapshot()) == []


def test_no_step_can_follow_a_finalized_chain(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    stale = Chain(sc)
    stale.steps = list(chain.steps)
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    before = db.snapshot()
    with pytest.raises(ValueError, match="already finalized"):
        stale.save("C", pct=0.5, mr_date=date(2026, 9, 3))
    assert db.diff(before, db.snapshot()) == []


# --- a Final PO only for a finalized root ----------------------------------------------------

def test_no_final_po_for_a_root_that_is_not_finalized(scenario, po_on):
    sc, db = scenario, scenario.db
    Chain(sc).save("B", mr_date=date(2026, 9, 1))         # out, not back yet
    pos_before = db.count("jute_po")
    plan = in_txn(po_ops.plan_final_po, sc.root)
    out = in_txn(po_ops.create_final_po, sc.root, 7)
    assert plan["skipped"] == f"MR {sc.root} is not finalized"
    assert out["po_id"] is None and "not finalized" in out["skipped"]
    assert db.count("jute_po") == pos_before


# --- a later hop's PO never pairs the outside supplier with a sister company ----------------

def test_a_later_hop_po_uses_the_sister_companys_existing_mapping(scenario, po_on):
    sc, db = scenario, scenario.db
    # In C the broker already supplies through another party, and B is known
    # as a party of C under the catch-all supplier "others".
    other = sc.add_party(sc.c_co, "SOME OTHER JUTE PARTY")
    db.insert("jute_supp_party_map", co_id=sc.c_co, jute_supplier_id=sc.supplier, party_id=other)
    b_in_c = sc.add_party(sc.c_co, sc.b_name)
    db.insert("jute_supp_party_map", co_id=sc.c_co, jute_supplier_id=sc.others_supplier,
              party_id=b_in_c)
    maps_in_c = db.rows("jute_supp_party_map", co_id=sc.c_co)

    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    to_c = chain.save("C", pct=0.5, mr_date=date(2026, 9, 2))

    po = db.row("jute_po", jute_po_id=to_c["po"]["po_id"])
    assert (po["party_id"], po["supplier_id"]) == (b_in_c, sc.others_supplier)
    assert (po["credit_term"], po["delivery_days"]) == (None, None)
    assert db.rows("jute_supp_party_map", co_id=sc.c_co) == maps_in_c


# --- deletes lock only the rows they remove -------------------------------------------------

PK = {"jute_po_status_log": "log_id", "jute_po_li": "jute_po_li_id", "jute_po": "jute_po_id",
      "sales_invoice_jute_dtl": "sales_invoice_jute_dtl_id",
      "sales_invoice_jute": "sales_invoice_jute_id",
      "sales_invoice_dtl": "invoice_line_item_id", "sales_invoice": "invoice_id",
      "jute_mr_li": "jute_mr_li_id", "jute_mr": "jute_mr_id"}


def test_every_delete_and_line_update_goes_by_primary_key(scenario, po_on):
    """A DELETE / UPDATE filtered on a column without an index scans the
    table and, under REPEATABLE READ, locks every row of it until commit."""
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    start = len(db.statements)
    chain.unfinalize()
    chain.delete_from(1)
    writes = [s for s in db.statements[start:] if s.upper().startswith(("DELETE", "UPDATE"))]
    assert any(s.upper().startswith("DELETE") for s in writes)
    for stmt in writes:
        m = re.match(r"(?:DELETE FROM|UPDATE) (\w+)\b.*? WHERE (\w+) (?:IN \(|= :)", stmt)
        assert m, stmt
        table, column = m.groups()
        if table in ("jute_mr_li", "jute_po_li", "jute_po_status_log", "sales_invoice_jute_dtl",
                     "sales_invoice_jute", "sales_invoice_dtl"):
            assert column == PK[table], stmt


# --- money exactly as the ERP writes it ------------------------------------------------------

def test_erp_tds_rule():
    assert transfer._erp_tds_amount(0.0, 4_000_000.0) == 0.0               # under Rs 50 lakh
    assert transfer._erp_tds_amount(6_000_000.0, 1_385_746.01) == 1385.75  # already over: all
    # crossing the threshold with this MR: only the part above it
    assert transfer._erp_tds_amount(4_900_000.0, 1_385_746.01) == round(1_285_746.01 * 0.001, 2)
    assert transfer._erp_jute_totals(1385746.01, 13757.3, 1385.75) == (0.04, 1370603.0)
    assert transfer._erp_jute_totals(100.5, 0, 0) == (-0.5, 100.0)          # Python round, as the ERP


def test_finalize_derives_194q_tds_and_unfinalize_drops_it(scenario, po_on):
    sc, db = scenario, scenario.db
    # The mill has already bought Rs 60 lakh from B (as a party of A) this FY.
    b_in_a = sc.add_party(sc.a_co, sc.b_name)
    db.insert("jute_mr", branch_id=sc.a_branch, party_id=str(b_in_a), status_id=3,
              jute_mr_date=date(2026, 8, 1), total_amount=6_000_000.0, transfer_mode=0)
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))

    root = db.row("jute_mr", jute_mr_id=sc.root)
    assert root["party_id"] == str(b_in_a)
    assert (root["total_amount"], root["claim_amount"], root["tds_amount"]) == (
        1385746.01, 13757.3, 1385.75)
    assert (root["roundoff"], root["net_total"]) == (0.04, 1370603.0)

    chain.unfinalize()
    root = db.row("jute_mr", jute_mr_id=sc.root)
    assert root["tds_amount"] == 0.0 and root["status_id"] == 13


def test_repair_counts_tds_in_the_order_the_lorries_were_finalized(scenario, po_on):
    """Repairing an old finalize today must not count what the party was paid
    AFTER that lorry: the first Rs 50 lakh of the year carry no TDS."""
    from scripts import repair_transfer_data as repair
    sc, db = scenario, scenario.db
    b_in_a = sc.add_party(sc.a_co, sc.b_name)
    # Rs 40 lakh bought from B before this lorry, Rs 20 lakh after it
    db.insert("jute_mr", branch_id=sc.a_branch, party_id=str(b_in_a), status_id=3,
              jute_mr_date=date(2026, 8, 1), branch_mr_no=1, total_amount=4_000_000.0,
              transfer_mode=0)
    db.insert("jute_mr", branch_id=sc.a_branch, party_id=str(b_in_a), status_id=3,
              jute_mr_date=date(2026, 9, 20), branch_mr_no=999, total_amount=2_000_000.0,
              transfer_mode=0)
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    assert db.row("jute_mr", jute_mr_id=sc.root)["tds_amount"] == 1385.75   # today: all 60 lakh count
    # what the old finalize left behind
    db.update("jute_mr", {"jute_mr_id": sc.root}, claim_amount=None, tds_amount=None,
              roundoff=None, net_total=None)

    plan = in_txn(repair.plan_finalized_net)
    assert [p["jute_mr_id"] for p in plan] == [sc.root]
    assert plan[0]["approved_before"] == 4_000_000.0
    assert plan[0]["new"] == {"total_amount": 1385746.01, "claim_amount": 13757.3,
                              "tds_amount": 385.75, "roundoff": 0.04, "net_total": 1371603.0}

    in_txn(repair._repair_finalized_net, plan[0])
    root = db.row("jute_mr", jute_mr_id=sc.root)
    assert {k: root[k] for k in plan[0]["new"]} == plan[0]["new"]
    assert in_txn(repair.plan_finalized_net) == []


# --- a transfer PO is deleted only when it is the one asked for ------------------------------

def test_delete_transfer_po_deletes_exactly_the_given_po(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    to_b = chain.save("B", mr_date=date(2026, 9, 1))
    back = chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    fwd_id, final_id = to_b["po"]["po_id"], back["po"]["po_id"]

    # wrong role / wrong MR: nothing is touched
    assert in_txn(po_ops.delete_transfer_po, final_id, ROLE_FORWARD, to_b["mr_id"]) is None
    assert in_txn(po_ops.delete_transfer_po, fwd_id, ROLE_FORWARD, sc.root) is None
    assert in_txn(po_ops.delete_transfer_po, sc.po, ROLE_FINAL, sc.root) is None   # an ERP PO
    assert db.count("jute_po", jute_po_id=final_id) == 1

    gone = in_txn(po_ops.delete_transfer_po, final_id, ROLE_FINAL, sc.root)
    assert gone["po_id"] == final_id and db.count("jute_po", jute_po_id=final_id) == 0
    assert db.count("jute_po", jute_po_id=fwd_id) == 1


def test_a_forwarding_po_unlinked_in_the_erp_is_still_found_and_removed(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    to_b = chain.save("B", mr_date=date(2026, 9, 1))
    hop, fwd_id = to_b["mr_id"], to_b["po"]["po_id"]
    # an ERP user re-points the hop at the mill's original PO
    db.update("jute_mr", {"jute_mr_id": hop}, po_id=sc.po)

    chain.delete_from(1)

    assert db.count("jute_po", jute_po_id=fwd_id) == 0        # ours, found by its marker
    assert db.count("jute_po", jute_po_id=sc.po) == 1         # the ERP PO is left alone
    assert db.count("jute_po_li", jute_po_id=sc.po) > 0


# --- small things ----------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["0", "false", "OFF", " no ", "Off"])
def test_kill_switch_off_spellings(monkeypatch, value):
    monkeypatch.setenv("JT_TRANSFER_PO", value)
    assert po_ops.transfer_po_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "on", "yes"])
def test_kill_switch_on_spellings(monkeypatch, value):
    monkeypatch.setenv("JT_TRANSFER_PO", value)
    assert po_ops.transfer_po_enabled() is True


def test_kill_switch_unset_or_empty_means_the_default(monkeypatch):
    expected = po_ops._ENABLED_DEFAULT.strip().lower() not in po_ops._OFF_VALUES
    monkeypatch.setenv("JT_TRANSFER_PO", "  ")
    assert po_ops.transfer_po_enabled() is expected
    monkeypatch.delenv("JT_TRANSFER_PO")
    assert po_ops.transfer_po_enabled() is expected


def test_crop_years_are_stored_as_the_erp_does():
    lines = build_po_lines([
        {"jute_mr_li_id": 1, "actual_item_id": 5, "accepted_weight": 1500, "rate": 13000,
         "active": 1, "crop_year": 2026},
        {"jute_mr_li_id": 2, "actual_item_id": 6, "accepted_weight": 1500, "rate": 13000,
         "active": 1, "crop_year": None},
    ], "BALE", default_crop_year=2025)
    assert [l["crop_year"] for l in lines] == [26, 25]


def test_po_remarks_say_what_the_po_is():
    assert po_remarks(ROLE_FORWARD).startswith("Transfer PO (Forwarding) created by Jute Transfer")
    assert po_remarks(ROLE_FINAL).startswith("Transfer PO (Final) created by Jute Transfer")
