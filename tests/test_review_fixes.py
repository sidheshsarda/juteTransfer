"""Fixes from the 2026-10-02 code review of the transfer-PO feature, end to
end on the in-memory MySQL stand-in (tests/fake_mysql.py): stale-tab guards,
a Final PO only for a finalized root, later-hop suppliers without new map
rows, deletes by primary key, money exactly as the ERP writes it (194Q TDS),
targeted transfer-PO deletes, the kill-switch spellings and crop years."""
import re
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from src.jutetransfer import po_ops, transfer
from src.jutetransfer.database import DatabaseConnection, lock_sort_key, named_locks
from src.jutetransfer.po_helpers import ROLE_FINAL, ROLE_FORWARD, build_po_lines, po_remarks

from .fake_mysql import fake_db, scenario  # noqa: F401  (fixtures)
from .test_transfer_po_flow import (  # noqa: F401  (po_on is a fixture)
    INVOICE_TABLES, LOCK_SQL, Chain, party_named, po_on, root_rates,
)


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
    # finalize itself now counts only the MRs before this one (ERP rule, 2026-10-03)
    assert db.row("jute_mr", jute_mr_id=sc.root)["tds_amount"] == 385.75
    # what the old finalize left behind
    db.update("jute_mr", {"jute_mr_id": sc.root}, claim_amount=None, tds_amount=None,
              roundoff=None, net_total=None)

    plan = in_txn(repair.plan_finalized_net)
    assert [p["jute_mr_id"] for p in plan] == [sc.root]
    assert plan[0]["approved_before"] == 4_000_000.0
    # the same figure the live finalize writes: one rule, two code paths
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

@pytest.mark.parametrize("value", ["0", "false", "OFF", " no ", "Off",
                                   # the switch fails CLOSED: a typo never turns it on
                                   "fasle", "disabled", "none", "n", "2", "O", "Yes please"])
def test_kill_switch_off_spellings(monkeypatch, value):
    monkeypatch.setenv("JT_TRANSFER_PO", value)
    assert po_ops.transfer_po_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "on", "yes", " TRUE ", "On", "YES"])
def test_kill_switch_on_spellings(monkeypatch, value):
    monkeypatch.setenv("JT_TRANSFER_PO", value)
    assert po_ops.transfer_po_enabled() is True


def test_kill_switch_unset_or_empty_means_the_default(monkeypatch):
    assert po_ops._ENABLED_DEFAULT in ("0", "1")                  # a plain, unambiguous default
    expected = po_ops._ENABLED_DEFAULT == "1"
    monkeypatch.setenv("JT_TRANSFER_PO", "  ")
    assert po_ops.transfer_po_enabled() is expected
    monkeypatch.delenv("JT_TRANSFER_PO")
    assert po_ops.transfer_po_enabled() is expected
    monkeypatch.setattr(po_ops, "_ENABLED_DEFAULT", "1")          # both defaults read by the same rule
    assert po_ops.transfer_po_enabled() is True
    monkeypatch.setattr(po_ops, "_ENABLED_DEFAULT", "0")
    assert po_ops.transfer_po_enabled() is False


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


# ===========================================================================================
# Round 2 (review of 2026-10-03): numbering under named locks (S1), one party branch per
# forwarder (S2), money and endpoints from the rows (S3), invoice lines naming the seller's
# MR lines (stock view option A), the ERP's 194Q sequence rule, and the small items
# (N6 supplier of a later hop, N7 marker before lock, N9 switch fails closed, N13 line order).
# ===========================================================================================

CHAIN_LOCK = "jt_chain_save:sls"


def po_lock(branch_id):
    return f"jute_po_no:sls:{branch_id}"


def lock_calls(db, start, kind="SELECT GET_LOCK(:name, :timeout)"):
    """Names of the GET_LOCK (or RELEASE_LOCK) statements since `start`, in order."""
    return [p["name"] for s, p in zip(db.statements[start:], db.parameters[start:]) if s == kind]


def erp_header(db, mr_id, tds=0.0):
    """The five money columns the ERP's rule gives an MR from its active lines."""
    lines = [l for l in db.rows("jute_mr_li", jute_mr_id=mr_id) if l["active"] in (1, None)]
    total = round(sum(float(l["accepted_weight"]) / 100 * float(l["rate"] or 0) for l in lines), 2)
    claim = round(sum(float(l["accepted_weight"]) / 100 * float(l["claim_rate"] or 0) for l in lines), 2)
    roundoff, net = transfer._erp_jute_totals(total, claim, tds)
    return {"total_amount": total, "claim_amount": claim, "tds_amount": tds,
            "roundoff": roundoff, "net_total": net}


def money(db, mr_id):
    row = db.row("jute_mr", jute_mr_id=mr_id)
    return {k: row[k] for k in ("total_amount", "claim_amount", "tds_amount", "roundoff", "net_total")}


def invoice_follows_its_lines(db, invoice_id):
    """invoice_amount = the lines to the rupee, round_off the paise between,
    the jute claim = the sum of the per-line claims."""
    inv = db.row("sales_invoice", invoice_id=invoice_id)
    dtl = db.rows("sales_invoice_dtl", invoice_id=invoice_id)
    lines = sum(Decimal(str(d["amount_without_tax"])) for d in dtl)
    assert Decimal(str(inv["invoice_amount"])) == Decimal(round(lines))
    assert Decimal(str(inv["invoice_amount"])) - inv["round_off"] == lines
    assert abs(inv["round_off"]) <= Decimal("0.5")
    claims = sum(Decimal(str(db.row("sales_invoice_jute_dtl",
                                    invoice_line_item_id=d["invoice_line_item_id"])["claim_amount_dtl"]))
                 for d in dtl)
    assert db.row("sales_invoice_jute", invoice_id=invoice_id)["claim_amount"] == claims


# --- S1: every save numbers under named locks -------------------------------------------------

def test_a_save_takes_its_locks_before_the_transaction_and_releases_them_after(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    start = len(db.statements)

    chain.save("B", mr_date=date(2026, 9, 1))

    stmts = db.statements[start:]
    # the lock connection first: the schema name, then the locks in one fixed order
    assert stmts[0] == "SELECT DATABASE()"
    assert lock_calls(db, start) == [CHAIN_LOCK, po_lock(sc.b_branch)]
    waits = {p["name"]: p["timeout"] for s, p in zip(stmts, db.parameters[start:])
             if s == "SELECT GET_LOCK(:name, :timeout)"}
    assert waits == {CHAIN_LOCK: 30, po_lock(sc.b_branch): 5}
    # every GET_LOCK precedes the transaction's first statement (the root lock) ...
    first = next(i for i, s in enumerate(stmts) if s not in LOCK_SQL)
    assert stmts[first].endswith("FROM jute_mr WHERE jute_mr_id = :id FOR UPDATE")
    assert all(s != "SELECT GET_LOCK(:name, :timeout)" for s in stmts[first:])
    # ... and every RELEASE_LOCK follows its last write (the harness itself refuses a
    # lock connection that is still in a transaction when the save's begins, and one
    # that releases while the save's transaction is still open)
    last_write = max(i for i, s in enumerate(stmts)
                     if s.split(None, 1)[0].upper() in ("INSERT", "UPDATE", "DELETE"))
    releases = [i for i, s in enumerate(stmts) if s == "SELECT RELEASE_LOCK(:name)"]
    assert releases and min(releases) > last_write
    assert lock_calls(db, start, "SELECT RELEASE_LOCK(:name)") == [po_lock(sc.b_branch), CHAIN_LOCK]
    assert db.locks == set()

    # the final step guards the mill's PO series (the Final PO) -- the step's branch
    start = len(db.statements)
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    assert lock_calls(db, start) == [CHAIN_LOCK, po_lock(sc.a_branch)]
    assert db.locks == set()

    # deletes allocate no number: no lock at all
    for action in (chain.unfinalize, lambda: chain.delete_from(1)):
        start = len(db.statements)
        action()
        assert [s for s in db.statements[start:] if s in LOCK_SQL] == []


def test_a_busy_lock_refuses_the_save_before_anything_is_read_or_written(scenario, po_on):
    """An ERP PO save is numbering at B right now (holds jute_po_no:sls:20
    for longer than our 5 s): the save says so and touches nothing."""
    sc, db = scenario, scenario.db
    db.busy_locks.add(po_lock(sc.b_branch))
    before, start = db.snapshot(), len(db.statements)

    with pytest.raises(ValueError, match="numbering documents at this branch right now.*save again"):
        Chain(sc).save("B", mr_date=date(2026, 9, 1))

    assert db.diff(before, db.snapshot()) == []
    assert [s for s in db.statements[start:] if s not in LOCK_SQL] == []   # no transaction opened
    # the chain-save lock, taken first, was given back
    assert db.lock_log[-2:] == [("get", po_lock(sc.b_branch), 0), ("release", CHAIN_LOCK, 1)]
    assert db.locks == set()

    db.busy_locks.clear()
    db.busy_locks.add(CHAIN_LOCK)                     # another user's save is running
    with pytest.raises(ValueError, match="save again"):
        Chain(sc).save("B", mr_date=date(2026, 9, 1))
    assert db.diff(before, db.snapshot()) == [] and db.locks == set()


def test_a_refused_or_failed_save_releases_its_locks(scenario, po_on, monkeypatch):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    with pytest.raises(ValueError, match="already has a transfer chain"):
        Chain(sc).save("B", mr_date=date(2026, 9, 1))               # refused inside the transaction
    assert db.locks == set()
    monkeypatch.setattr(po_ops, "_apply", lambda conn, plan, by: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        chain.save("C", pct=0.5, mr_date=date(2026, 9, 2))         # failed inside the transaction
    assert db.locks == set()


def test_named_locks_order_schema_timeouts_and_release(fake_db):
    db = fake_db
    start = len(db.statements)
    with named_locks({"jute_po_no:{db}:100": 5, "jute_po_no:{db}:29": 7, "jt_chain_save:{db}": 30}) as held:
        # numeric segments sort as numbers (29 before 100), '{db}' is the schema
        assert held == ["jt_chain_save:sls", "jute_po_no:sls:29", "jute_po_no:sls:100"]
        assert db.locks == set(held)
        # the lock connection has ended its own transaction: the caller's may begin
        with DatabaseConnection.get_transaction() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM jute_po")).scalar() == 0
    assert db.locks == set()
    gets = [(p["name"], p["timeout"]) for s, p in zip(db.statements[start:], db.parameters[start:])
            if s == "SELECT GET_LOCK(:name, :timeout)"]
    assert gets == [("jt_chain_save:sls", 30), ("jute_po_no:sls:29", 7), ("jute_po_no:sls:100", 5)]
    assert lock_calls(db, start, "SELECT RELEASE_LOCK(:name)") == list(reversed(held))
    # a plain list shares one timeout
    with named_locks(["b", "a"], timeout=9) as held:
        assert held == ["a", "b"]
    assert db.parameters[-3]["timeout"] == 9 and db.locks == set()


def test_named_locks_with_nothing_to_lock_do_not_touch_the_database(fake_db):
    start = len(fake_db.statements)
    with named_locks([]) as held:
        assert held == []
    with named_locks([None, ""]) as held:
        assert held == []
    assert fake_db.statements[start:] == []


def test_named_locks_busy_or_failing_give_back_what_was_taken(fake_db):
    db = fake_db
    db.busy_locks.add("b")
    with pytest.raises(ValueError, match="lock 'b' still held after 4 s.*save again"):
        with named_locks(["c", "a", "b"], timeout=4):
            raise AssertionError("must not get here")
    assert db.locks == set()
    assert db.lock_log[-3:] == [("get", "a", 1), ("get", "b", 0), ("release", "a", 1)]
    db.null_locks.add("n")
    with pytest.raises(ValueError, match="Could not take the save lock 'n' \\(database error\\)"):
        with named_locks(["n"]):
            raise AssertionError("must not get here")
    assert db.locks == set()
    with pytest.raises(RuntimeError, match="inside"):
        with named_locks(["x", "y"]):
            assert db.locks == {"x", "y"}
            raise RuntimeError("inside")
    assert db.locks == set()


def test_lock_sort_key_is_one_total_order_for_every_process():
    names = ["jute_po_no:sls:100", "jute_po_no:sls:29", "jute_po_no:sls:9", "jt_chain_save:sls",
             "jute_po_no:dev3:29"]
    assert sorted(names, key=lock_sort_key) == [
        "jt_chain_save:sls", "jute_po_no:dev3:29", "jute_po_no:sls:9", "jute_po_no:sls:29",
        "jute_po_no:sls:100"]
    assert po_ops.po_no_lock(29) == "jute_po_no:{db}:29"
    assert transfer.CHAIN_SAVE_LOCK == "jt_chain_save:{db}"


# --- S2: one party branch for the forwarding company, however often it is finalized --------

def test_finalizing_three_times_leaves_one_party_branch_for_the_forwarder(scenario, po_on):
    sc, db = scenario, scenario.db
    db.update("branch_mst", {"branch_id": sc.b_branch}, gst_no=None)   # live: no chain branch has a GST no
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    for day in (3, 4, 5):
        chain.save("A", pct=0.5, mr_date=date(2026, 9, day))
        b_in_a = party_named(db, sc.a_co, sc.b_name)
        branches = db.rows("party_branch_mst", party_id=b_in_a)
        assert len(branches) == 1, f"finalize {day - 2}: {len(branches)} branch rows"
        assert db.row("jute_mr", jute_mr_id=sc.root)["party_branch_id"] == branches[0]["party_mst_branch_id"]
        if day < 5:
            chain.unfinalize()


@pytest.mark.parametrize("case", ["same GST", "another GST on the party's branch", "no GST anywhere"])
def test_finalize_reuses_the_partys_existing_branch(scenario, po_on, case):
    """B already exists as a party of A with one branch: finalize points the
    mill MR at that branch and never adds a second one."""
    sc, db = scenario, scenario.db
    b_in_a = sc.add_party(sc.a_co, sc.b_name)                        # one branch, GST 19AABCD....
    (branch,) = db.rows("party_branch_mst", party_id=b_in_a)
    if case == "same GST":
        db.update("branch_mst", {"branch_id": sc.b_branch}, gst_no=branch["gst_no"].lower())
    elif case == "no GST anywhere":
        db.update("branch_mst", {"branch_id": sc.b_branch}, gst_no=None)
        db.update("party_branch_mst", {"party_mst_branch_id": branch["party_mst_branch_id"]}, gst_no=None)
    (branch,) = db.rows("party_branch_mst", party_id=b_in_a)
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    assert db.rows("party_branch_mst", party_id=b_in_a) == [branch]
    assert db.row("jute_mr", jute_mr_id=sc.root)["party_branch_id"] == branch["party_mst_branch_id"]


def test_a_party_without_any_branch_gets_one_from_branch_mst(scenario, po_on):
    sc, db = scenario, scenario.db
    b_in_a = sc.add_party(sc.a_co, sc.b_name, branch_ids=())
    with DatabaseConnection.get_transaction() as conn:
        made = transfer._ensure_party_branch_from_source_branch(conn, b_in_a, sc.b_branch, sc.user)
        again = transfer._ensure_party_branch_from_source_branch(conn, b_in_a, sc.b_branch, sc.user)
    rows = db.rows("party_branch_mst", party_id=b_in_a)
    assert [r["party_mst_branch_id"] for r in rows] == [made] and again == made
    assert (rows[0]["gst_no"], rows[0]["address"]) == (f"19AAACB{sc.b_branch:04d}K1Z{sc.b_branch % 10}",
                                                       f"{sc.b_branch} MILL ROAD")


# --- S3: money from the rows, endpoints checked against them --------------------------------

def stale_step(sc, to, total, claim, mr_date):
    """A TransferStep as a tab that was loaded before the lorry changed sends it."""
    return transfer.TransferStep(
        co_id=sc.co(to), branch_id=sc.branch(to), mr_date=mr_date, mr_rate=0,
        pct_rate_increase=0.0, total_amount=total, claim_amount=claim, net_amount=total - claim,
        warehouse_id=sc.godown(to), mr_no=0)


def test_a_stale_tab_with_wrong_totals_posts_the_hop_header_from_its_lines(scenario, po_on):
    """An ERP user corrected a rate 12,850 -> 12,350 after the tab was loaded;
    the tab saves step 1 with the old totals (13,78,878 / 13,757)."""
    sc, db = scenario, scenario.db
    db.update("jute_mr_li", {"jute_mr_li_id": sc.root_lines[0]}, rate=12350)
    step = stale_step(sc, "B", 1378878.0, 13757.0, date(2026, 9, 1))

    out = transfer.save_transfer_step(
        source_mr_id=sc.root, step=step, prev_co_id=sc.a_co, prev_branch_id=sc.a_branch,
        source_co_id=sc.a_co, source_branch_id=sc.a_branch, root_mr_id=sc.root, updated_by=sc.user,
        rate_multiplier=1.0, is_first_step=True, use_new_rounding=True)

    hop = out["mr_id"]
    assert [l["rate"] for l in db.rows("jute_mr_li", jute_mr_id=hop)] == [12350.0, 12650.0]
    # 9312 x 123.50 + 1441 x 126.50 = 11,50,032 + 1,82,286.50; claim 9312 x 1.40 + 1441 x 0.50
    assert money(db, hop) == erp_header(db, hop) == {
        "total_amount": 1332318.5, "claim_amount": 13757.3, "tds_amount": 0.0,
        "roundoff": -0.2, "net_total": 1318561.0}
    # the PO was built from the same lines
    assert [l["rate"] for l in db.rows("jute_po_li", jute_po_id=out["po"]["po_id"])] == [12350.0, 12650.0]


def test_a_stale_tab_with_wrong_totals_posts_the_invoice_from_its_lines(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    hop = chain.save("B", mr_date=date(2026, 9, 1))["mr_id"]
    # the tab's figures are nonsense: Rs 1 crore total, Rs 1 claim
    step = stale_step(sc, "A", 10_000_000.0, 1.0, date(2026, 9, 3))

    out = transfer.save_transfer_step(
        source_mr_id=hop, step=step, prev_co_id=sc.b_co, prev_branch_id=sc.b_branch,
        source_co_id=sc.a_co, source_branch_id=sc.a_branch, root_mr_id=sc.root, updated_by=sc.user,
        rate_multiplier=1.0, is_final=True, original_source_mr_id=sc.root, use_new_rounding=True)

    invoice_follows_its_lines(db, out["invoice_id"])
    inv = db.row("sales_invoice", invoice_id=out["invoice_id"])
    # lines 13,78,878.50: to the rupee with Python's round (half to even), as the screen
    # and the ERP's MR net do -- not the tab's Rs 1 crore
    assert (inv["invoice_amount"], inv["round_off"]) == (1378878.0, Decimal("-0.50"))
    assert db.row("sales_invoice_jute", invoice_id=out["invoice_id"])["claim_amount"] == Decimal("13757.30")
    root = db.row("jute_mr", jute_mr_id=sc.root)
    assert (root["invoice_amount"], root["status_id"]) == (1378878.0, 3)


def test_every_header_of_a_three_step_chain_equals_its_lines(scenario, po_on):
    sc, db = scenario, scenario.db
    db.update("jute_mr_li", {"jute_mr_li_id": sc.root_lines[0]}, rate=13100, water_damage_amount=250.40)
    chain = Chain(sc)
    to_b = chain.save("B", mr_date=date(2026, 9, 1))
    to_c = chain.save("C", pct=0.5, mr_date=date(2026, 9, 2))
    back = chain.save("A", pct=1.25, mr_date=date(2026, 9, 3))
    for hop in (to_b["mr_id"], to_c["mr_id"]):
        assert money(db, hop) == erp_header(db, hop)
        assert db.row("jute_mr", jute_mr_id=hop)["roundoff"] == erp_header(db, hop)["roundoff"]
    assert money(db, sc.root) == erp_header(db, sc.root)
    for invoice_id in (to_c["invoice_id"], back["invoice_id"]):
        invoice_follows_its_lines(db, invoice_id)
    # the hop MR records the invoice that sold it the lorry, as booked
    assert db.row("jute_mr", jute_mr_id=to_c["mr_id"])["invoice_amount"] == db.row(
        "sales_invoice", invoice_id=to_c["invoice_id"])["invoice_amount"]


def test_a_hop_mr_carries_the_tds_the_erp_would_derive(scenario, po_on):
    """B has already bought Rs 60 lakh from the supplier this FY (dated before
    the lorry): the ERP's approve would put 0.1 % 194Q TDS on B's hop MR, and
    so does the app (owner ruling 2026-10-03). A lorry dated BEFORE those
    purchases counts none of them."""
    sc, db = scenario, scenario.db
    supplier_in_b, _ = sc.party_in(sc.b_co, sc.supplier_party_name)
    db.insert("jute_mr", branch_id=sc.b_branch, party_id=str(supplier_in_b), status_id=3,
              jute_mr_date=date(2026, 8, 1), total_amount=6_000_000.0, transfer_mode=0)
    hop = Chain(sc).save("B", mr_date=date(2026, 9, 1))["mr_id"]
    total = erp_header(db, hop)["total_amount"]
    assert money(db, hop) == erp_header(db, hop, tds=round(total * 0.001, 2))
    assert money(db, hop)["tds_amount"] == 1378.88

    earlier = Chain(sc, root=sc.add_root(ge_no=9, ge_date=date(2026, 7, 20)))
    hop2 = earlier.save("B", mr_date=date(2026, 7, 25))["mr_id"]      # before the Rs 60 lakh
    assert money(db, hop2)["tds_amount"] == 0.0


@pytest.mark.parametrize("wrong", ["prev branch", "prev company", "step-1 source", "final origin"])
def test_a_tab_whose_endpoints_do_not_match_the_rows_is_refused(scenario, po_on, wrong):
    sc, db = scenario, scenario.db
    other_root = sc.add_root()
    chain = Chain(sc)
    hop = chain.save("B", mr_date=date(2026, 9, 1))["mr_id"]
    kwargs = dict(source_mr_id=hop, prev_co_id=sc.b_co, prev_branch_id=sc.b_branch,
                  source_co_id=sc.a_co, source_branch_id=sc.a_branch, root_mr_id=sc.root,
                  updated_by=sc.user, rate_multiplier=1.005, is_final=True,
                  original_source_mr_id=sc.root, use_new_rounding=True)
    step = stale_step(sc, "A", 1385746.0, 13757.0, date(2026, 9, 3))
    if wrong == "prev branch":
        kwargs["prev_branch_id"] = sc.c_branch                     # the invoice would be booked at C
        error = f"receives from company {sc.b_co} / branch {sc.c_branch}, but its source MR {hop}"
    elif wrong == "prev company":
        kwargs["prev_co_id"] = sc.c_co
        error = f"receives from company {sc.c_co} / branch {sc.b_branch}"
    elif wrong == "step-1 source":
        kwargs.update(source_mr_id=other_root, prev_co_id=sc.a_co, prev_branch_id=sc.a_branch,
                      is_final=False, is_first_step=True, root_mr_id=other_root)
        kwargs["root_mr_id"] = sc.root
        step = stale_step(sc, "C", 1378878.0, 13757.0, date(2026, 9, 3))
        db.delete("jute_mr", jute_mr_id=hop)                      # a root without a chain again
        db.delete("jute_mr_li", jute_mr_id=hop)
        error = f"A first step must start from the root MR {sc.root}, not from MR {other_root}"
    else:
        kwargs.update(source_co_id=sc.b_co, source_branch_id=sc.b_branch)   # the mill's numbers at B
        error = f"names company {sc.b_co} / branch {sc.b_branch} as the origin, but root MR {sc.root}"
    before, start = db.snapshot(), len(db.statements)

    with pytest.raises(ValueError, match=re.escape(error)):
        transfer.save_transfer_step(step=step, **kwargs)

    assert db.diff(before, db.snapshot()) == []
    assert db.writes(start) == [] and db.locks == set()


# --- stock view option A: invoice lines name the seller's MR lines ----------------------------

def test_chain_invoice_lines_name_the_sellers_lines_and_the_stock_view_nets_them(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    to_b = chain.save("B", mr_date=date(2026, 9, 1))
    to_c = chain.save("C", pct=0.5, mr_date=date(2026, 9, 2))
    back = chain.save("A", pct=1.0, mr_date=date(2026, 9, 3))
    hop_b, hop_c = sc.active_lines(to_b["mr_id"]), sc.active_lines(to_c["mr_id"])

    # B -> C sells B's hop lines, C -> A sells C's; the mill's lines are never named
    assert [d["jute_mr_li_id"] for d in db.rows("sales_invoice_dtl", invoice_id=to_c["invoice_id"])] == hop_b
    assert [d["jute_mr_li_id"] for d in db.rows("sales_invoice_dtl", invoice_id=back["invoice_id"])] == hop_c
    assert not set(sc.active_lines(sc.root)) & {d["jute_mr_li_id"] for d in db.rows("sales_invoice_dtl")}
    # the ERP's stock view takes the sale off the seller: sold = the accepted kg invoiced,
    # balance = the lorry's actual weight less that (the hop keeps the lorry's full weights)
    view = {r["jute_mr_li_id"]: r for r in db.execute(
        "SELECT jute_mr_li_id, actual_weight, sold_weight, bal_weight FROM vw_jute_stock_outstanding")}
    for line_id in hop_b + hop_c:
        line = db.row("jute_mr_li", jute_mr_li_id=line_id)
        assert view[line_id]["sold_weight"] == float(line["accepted_weight"])
        assert view[line_id]["bal_weight"] == round(float(line["actual_weight"]) - float(line["accepted_weight"]), 3)
    for root_line, hop_line in zip(sc.active_lines(sc.root), hop_b):
        assert db.row("jute_mr_li", jute_mr_li_id=hop_line)["actual_weight"] == db.row(
            "jute_mr_li", jute_mr_li_id=root_line)["actual_weight"]
    # the mill's root lines stay in stock in full (the floor issues from them)
    for line_id in sc.active_lines(sc.root):
        assert view[line_id]["sold_weight"] == 0.0

    # deleting the chain removes the invoices, and with them the stock-out
    chain.delete_from(1)
    assert {t: db.count(t) for t in INVOICE_TABLES} == dict.fromkeys(INVOICE_TABLES, 0)


def test_repair_invoice_links_names_the_seller_lines_of_old_chain_invoices(scenario, po_on, tmp_path,
                                                                            capsys):
    from scripts import repair_transfer_data as repair
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    hop = chain.save("B", mr_date=date(2026, 9, 1))["mr_id"]
    invoice_id = chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))["invoice_id"]
    hop_lines = sc.active_lines(hop)
    dtl_ids = [d["invoice_line_item_id"] for d in db.rows("sales_invoice_dtl", invoice_id=invoice_id)]
    # the old code wrote no link, and left zero-kg lines of soft-deleted MR lines on the invoice
    db.execute("UPDATE sales_invoice_dtl SET jute_mr_li_id = NULL WHERE invoice_id = :id", id=invoice_id)
    zero = db.insert("sales_invoice_dtl", invoice_id=invoice_id, item_id=sc.items[0], quantity=0,
                     sales_weight=0, rate=128.5, amount_without_tax=0, total_amount=0)
    # a marked-stock (Type 2) invoice is another agent's business: never listed
    marked = sc.add_hop(transfer_mode=1)
    marked_inv = db.insert("sales_invoice", invoice_no=77, invoice_date=date(2026, 9, 5), invoice_type=5,
                           branch_id=sc.a_branch, status_id=3, active=1, invoice_amount=1000,
                           round_off=0, updated_by=sc.user)
    db.insert("sales_invoice_jute", invoice_id=marked_inv, mr_id=marked)
    db.insert("sales_invoice_dtl", invoice_id=marked_inv, item_id=sc.items[0], quantity=750, rate=128.5)

    plan = in_txn(repair.plan_invoice_links)

    assert [(p["invoice_id"], p["hop_mr_id"], p["root_mr_id"], p["root_finalized"]) for p in plan] == [
        (invoice_id, hop, sc.root, True)]
    assert [(l["invoice_line_item_id"], l["jute_mr_li_id"], l["kg"]) for l in plan[0]["lines"]] == [
        (dtl_ids[0], hop_lines[0], 9312), (dtl_ids[1], hop_lines[1], 1441)]
    assert plan[0]["new"] == {f"sales_invoice_dtl:{d}": {"jute_mr_li_id": l}
                              for d, l in zip(dtl_ids, hop_lines)}

    # the dry run writes nothing; the apply links exactly the planned lines
    since = len(db.statements)
    assert repair.main(["--only", "invoice-links"]) == 0
    assert db.writes(since) == []
    out = capsys.readouterr().out
    assert "1 chain invoice(s), 2 line(s)" in out and f"seller hop MR {hop}" in out
    assert repair.main(["--apply", "--only", "invoice-links", "--expect", "1",
                        "--log-dir", str(tmp_path)]) == 0
    assert [d["jute_mr_li_id"] for d in db.rows("sales_invoice_dtl", invoice_id=invoice_id)] == hop_lines + [None]
    assert db.row("sales_invoice_dtl", invoice_line_item_id=zero)["jute_mr_li_id"] is None
    assert db.row("sales_invoice_dtl", invoice_id=marked_inv)["jute_mr_li_id"] is None
    assert in_txn(repair.plan_invoice_links) == []                 # nothing left
    (log,) = tmp_path.glob("jt_repair_*.json")
    assert f'"invoice_id": {invoice_id}' in log.read_text()


def test_repair_invoice_links_leaves_out_an_invoice_it_cannot_match_line_by_line(scenario, po_on, capsys):
    from scripts import repair_transfer_data as repair
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    hop = chain.save("B", mr_date=date(2026, 9, 1))["mr_id"]
    invoice_id = chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))["invoice_id"]
    db.execute("UPDATE sales_invoice_dtl SET jute_mr_li_id = NULL WHERE invoice_id = :id", id=invoice_id)
    first, second = sc.active_lines(hop)
    # two hop lines of the same item and kg: the 1441-kg line matches twice
    db.update("jute_mr_li", {"jute_mr_li_id": first}, actual_item_id=db.row(
        "jute_mr_li", jute_mr_li_id=second)["actual_item_id"], accepted_weight=1441)
    assert in_txn(repair.plan_invoice_links) == []
    assert "2 hop line(s) match" in capsys.readouterr().out
    # a line whose kg the ERP changed on the hop matches nothing
    db.update("jute_mr_li", {"jute_mr_li_id": first}, accepted_weight=9000)
    assert in_txn(repair.plan_invoice_links) == []
    assert "0 hop line(s) match" in capsys.readouterr().out
    # the row that changed between the dry run and the apply is refused, nothing written
    db.update("jute_mr_li", {"jute_mr_li_id": first}, actual_item_id=sc.items_in(sc.b_co)[sc.items[0]],
              accepted_weight=9312)
    (plan,) = in_txn(repair.plan_invoice_links)
    db.update("sales_invoice_dtl", {"invoice_line_item_id": plan["lines"][0]["invoice_line_item_id"]},
              jute_mr_li_id=second)
    before = db.snapshot()
    with pytest.raises(RuntimeError, match="already names MR line"):
        in_txn(repair._repair_invoice_links, plan)
    assert db.diff(before, db.snapshot()) == []


def test_repair_invoice_links_plans_open_chains_only_when_asked(scenario, po_on, tmp_path, capsys,
                                                                monkeypatch):
    """A second hop's seller invoice (B -> C of a chain still out) is counted
    and left out by default -- the owner's ruling covers closed chains -- and
    planned with --open-chains."""
    from scripts import repair_transfer_data as repair
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    hop_b = chain.save("B", mr_date=date(2026, 9, 1))["mr_id"]
    invoice_id = chain.save("C", pct=0.5, mr_date=date(2026, 9, 2))["invoice_id"]
    db.execute("UPDATE sales_invoice_dtl SET jute_mr_li_id = NULL WHERE invoice_id = :id", id=invoice_id)

    assert in_txn(repair.plan_invoice_links) == []
    out = capsys.readouterr().out
    assert "1 invoice(s) of chains still awaiting return (2 unlinked line(s)" in out
    assert f"seller branch(es) [{sc.b_branch}]" in out and "--open-chains" in out
    (plan,) = in_txn(repair.plan_invoice_links, include_open=True)
    assert (plan["invoice_id"], plan["hop_mr_id"], plan["root_finalized"]) == (invoice_id, hop_b, False)
    assert [l["jute_mr_li_id"] for l in plan["lines"]] == sc.active_lines(hop_b)

    assert repair.main(["--apply", "--only", "invoice-links", "--expect", "0",
                        "--log-dir", str(tmp_path / "closed")]) == 0            # nothing in scope
    assert [d["jute_mr_li_id"] for d in db.rows("sales_invoice_dtl", invoice_id=invoice_id)] == [None, None]
    assert repair.main(["--apply", "--only", "invoice-links", "--open-chains", "--expect", "1",
                        "--log-dir", str(tmp_path / "open")]) == 0
    assert [d["jute_mr_li_id"] for d in db.rows("sales_invoice_dtl", invoice_id=invoice_id)] == sc.active_lines(hop_b)
    assert repair.INVOICE_LINKS_OPEN_CHAINS is True                 # main() set it; back to the default
    monkeypatch.setattr(repair, "INVOICE_LINKS_OPEN_CHAINS", False)


# --- the ERP's 194Q sequence: only the party's MRs BEFORE this one count -----------------------

def test_finalize_counts_only_the_partys_approved_mrs_before_this_one(scenario, po_on):
    """Order: (jute_mr_date, COALESCE(branch_mr_no, 0), jute_mr_id), placed by
    the mill MR's own stored date / number (written by finalize before the
    money is computed) / id."""
    sc, db = scenario, scenario.db
    b_in_a = sc.add_party(sc.a_co, sc.b_name)

    def approved(day, no, total, branch=sc.a_branch, **extra):
        return db.insert("jute_mr", branch_id=branch, party_id=str(b_in_a), status_id=3,
                         jute_mr_date=date(2026, 9, day), branch_mr_no=no, total_amount=total,
                         transfer_mode=0, **extra)

    # at the mill's branch: numbers 1 and 2, so the finalize takes number 3 on 3 Sep;
    # branch 30 is another branch of the mill's company buying from the same party
    before = [approved(1, 1, 3_000_000.0),                       # earlier date
              approved(3, 2, 1_000_000.0),                       # same day, lower number
              approved(3, 3, 500_000.0, branch=30, jute_mr_id=100)]   # same day, same number, lower id
    after = [approved(3, 5000, 2_000_000.0, branch=30),          # same day, higher number
             approved(3, 3, 2_000_000.0, branch=30),             # same day, same number, higher id
             approved(20, 4, 2_000_000.0, branch=30)]            # later date
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))

    root = db.row("jute_mr", jute_mr_id=sc.root)
    assert root["branch_mr_no"] == 3
    # 45 lakh before + 13,85,746.01 crosses 50 lakh by 8,85,746.01 -> 885.75 (every other
    # approved MR counting, as before the ruling, gave 1,385.75)
    assert (root["total_amount"], root["tds_amount"], root["net_total"]) == (1385746.01, 885.75, 1371103.0)
    # the Python key says the same as the SQL predicate
    mine = transfer.mr_sequence_key(root["jute_mr_date"], root["branch_mr_no"], sc.root)
    peers = db.rows("jute_mr", party_id=str(b_in_a), status_id=3)
    assert {p["jute_mr_id"] for p in peers if p["jute_mr_id"] != sc.root
            and transfer.mr_sequence_key(p["jute_mr_date"], p["branch_mr_no"], p["jute_mr_id"]) < mine
            } == set(before)
    assert all(transfer.mr_sequence_key(db.row("jute_mr", jute_mr_id=p)["jute_mr_date"],
                                        db.row("jute_mr", jute_mr_id=p)["branch_mr_no"], p) > mine
               for p in after)
    # un-finalize drops it; a Pending MR with no date or number sorts first
    chain.unfinalize()
    assert db.row("jute_mr", jute_mr_id=sc.root)["tds_amount"] == 0.0
    assert transfer.mr_sequence_key(None, None, 5) == (date.min, 0, 5)
    assert transfer.mr_sequence_key("2026-09-03 00:00:00", 7.0, 5) == (date(2026, 9, 3), 7, 5)


def test_erp_cumulative_previous_matches_the_repairs_python_order(scenario, po_on):
    from scripts import repair_transfer_data as repair
    sc, db = scenario, scenario.db
    b_in_a = sc.add_party(sc.a_co, sc.b_name)
    rows = [db.insert("jute_mr", branch_id=sc.a_branch, party_id=str(b_in_a), status_id=3,
                      jute_mr_date=date(2026, 9, d), branch_mr_no=n, total_amount=t, transfer_mode=0)
            for d, n, t in ((5, 1, 100.0), (5, 2, 200.0), (6, 1, 400.0), (5, 2, 800.0), (4, 9, 1600.0))]
    with DatabaseConnection.get_transaction() as conn:
        for mr_id in rows:
            mr = db.row("jute_mr", jute_mr_id=mr_id)
            by_sql = transfer._erp_cumulative_previous(conn, b_in_a, mr["jute_mr_date"],
                                                       mr["branch_mr_no"], mr_id)
            by_python = repair._approved_before(conn, {**mr, "party_id": str(b_in_a)}, {}, {})
            assert by_sql == by_python, mr_id
    # 4 Sep first (1600), then 5 Sep no 1 (100), no 2 by id (200, then 800), then 6 Sep
    mr = db.row("jute_mr", jute_mr_id=rows[2])
    assert in_txn(transfer._erp_cumulative_previous, b_in_a, mr["jute_mr_date"], 1, rows[2]) == 2700.0


# --- N6: a later hop's supplier, without a new map row ------------------------------------------

@pytest.mark.parametrize("mapped", [True, False], ids=["B mapped under 'others' at C", "B unmapped at C"])
def test_a_later_hop_carries_the_sister_companys_mapped_supplier_and_adds_no_map_row(scenario, po_on,
                                                                                      mapped):
    sc, db = scenario, scenario.db
    b_in_c = sc.add_party(sc.c_co, sc.b_name)
    if mapped:
        db.insert("jute_supp_party_map", co_id=sc.c_co, jute_supplier_id=sc.others_supplier, party_id=b_in_c)
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    maps = db.rows("jute_supp_party_map")

    to_c = chain.save("C", pct=0.5, mr_date=date(2026, 9, 2))

    hop = db.row("jute_mr", jute_mr_id=to_c["mr_id"])
    expected = sc.others_supplier if mapped else sc.supplier       # else: the lorry's own supplier
    assert (hop["party_id"], hop["jute_supplier_id"]) == (str(b_in_c), expected)
    assert db.row("jute_po", jute_po_id=to_c["po"]["po_id"])["supplier_id"] == expected   # MR and PO agree
    assert db.rows("jute_supp_party_map") == maps                  # nothing added at C
    assert db.row("jute_mr", jute_mr_id=chain.steps[0]["mr_id"])["jute_supplier_id"] == sc.supplier


# --- N7: a PO is locked only once the marker says it is ours ---------------------------------------

def test_delete_transfer_po_locks_only_a_po_that_is_its_own(scenario, po_on):
    sc, db = scenario, scenario.db
    to_b = Chain(sc).save("B", mr_date=date(2026, 9, 1))
    fwd = to_b["po"]["po_id"]
    start = len(db.statements)
    assert in_txn(po_ops.delete_transfer_po, sc.po, ROLE_FORWARD, to_b["mr_id"]) is None     # an ERP PO
    assert in_txn(po_ops.delete_transfer_po, 999999, ROLE_FORWARD, to_b["mr_id"]) is None    # dangling
    assert in_txn(po_ops.delete_transfer_po, fwd, ROLE_FINAL, sc.root) is None               # wrong role
    assert not [s for s in db.statements[start:] if "FOR UPDATE" in s]
    start = len(db.statements)
    assert in_txn(po_ops.delete_transfer_po, fwd, ROLE_FORWARD, to_b["mr_id"])["po_id"] == fwd
    assert [s for s in db.statements[start:] if "FOR UPDATE" in s] == [
        "SELECT jute_po_id, internal_note, po_no, po_date, branch_id FROM jute_po "
        "WHERE jute_po_id = :id FOR UPDATE"]
    assert db.count("jute_po", jute_po_id=fwd) == 0


# --- N13: lines pair by id order, whatever order the query returns them in ------------------------

def test_lines_pair_by_id_order_even_when_the_query_returns_them_shuffled(scenario, po_on, monkeypatch):
    real = transfer.get_source_mr_full

    def shuffled(mr_id, conn=None):
        mr = real(mr_id, conn=conn)
        if mr:
            mr["line_items"] = list(reversed(mr["line_items"]))
        return mr

    monkeypatch.setattr(transfer, "get_source_mr_full", shuffled)
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    hop = chain.save("B", mr_date=date(2026, 9, 1))["mr_id"]
    # hop lines in the root's id order: 9312 kg @ 12850 first
    assert [(l["accepted_weight"], l["rate"]) for l in db.rows("jute_mr_li", jute_mr_id=hop)] == [
        (9312.0, 12850.0), (1441.0, 12650.0)]
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    assert root_rates(sc) == [12914.0, 12713.0]
    chain.unfinalize()
    assert root_rates(sc) == [12850.0, 12650.0]
