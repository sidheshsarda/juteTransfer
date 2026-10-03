"""Vertical transfer chains with their transfer POs, end to end through
transfer.py -- saved and deleted exactly the way pages/new_transfer_chain.py
does it -- on the fake sls database (tests/fake_mysql.py).

Scenario: lorry GE 21 of 31-08-2026 received at the mill A (Empire, branch
29) on ERP PO X (13314), root MR 28137 at Pending (13); B (Jagrati) and C
(Greeting) are the forwarding companies. PO creation is switched on or off
explicitly in every test (JT_TRANSFER_PO): the shipped default must not
matter here."""
from datetime import date
from decimal import Decimal

import pytest

from src.jutetransfer import po_ops, transfer
from src.jutetransfer.database import DatabaseConnection
from src.jutetransfer.jute_mr_chain_helpers import _recalculate_chain
from src.jutetransfer.po_helpers import ROLE_FINAL, ROLE_FORWARD, build_marker
from src.jutetransfer.queries import get_source_mr_full

from .fake_mysql import fake_db, scenario  # noqa: F401  (fixtures)

PO_TABLES = ("jute_po", "jute_po_li", "jute_po_status_log")
INVOICE_TABLES = ("sales_invoice", "sales_invoice_dtl", "sales_invoice_jute",
                  "sales_invoice_jute_dtl")
MASTER_TABLES = ("party_mst", "party_branch_mst", "item_grp_mst", "item_mst",
                 "jute_supp_party_map")
AUDIT = {"jute_mr": ["updated_by", "updated_date_time"], "jute_mr_li": ["updated_date_time"]}

ROOT_MR_DATE = date(2026, 8, 30)         # the root's MR date before finalize
ORIGINAL = {"party_id": "207", "party_branch_id": 3811, "jute_mr_date": ROOT_MR_DATE}
FORWARD_B = "JTSPL/JPO/26-27/00001"      # first PO of Jagrati's branch
FORWARD_C = "GMPL/KOL/JPO/26-27/00001"   # first PO of Greeting's branch
FINAL_A = "EJM/F/JPO/26-27/00012"        # the mill's next PO after X (00011)


@pytest.fixture
def po_on(monkeypatch):
    monkeypatch.setenv("JT_TRANSFER_PO", "1")


@pytest.fixture
def po_off(monkeypatch):
    monkeypatch.setenv("JT_TRANSFER_PO", "0")


def line_claim(line) -> float:
    """A line's claim as the overview query shows it."""
    return (float(line["accepted_weight"] or 0) * float(line["claim_rate"] or 0) / 100
            + float(line["water_damage_amount"] or 0) - float(line["premium_amount"] or 0))


class Chain:
    """One lorry's transfer chain, saved and deleted the way the chain page
    does it (pages/new_transfer_chain.py: _save_step and the 'Delete from
    Step N onward' button)."""

    def __init__(self, sc, root=None):
        self.sc, self.db = sc, sc.db
        self.root = root or sc.root
        self.steps = []                 # saved steps: {"to", "pct", "mr_id", "final"}

    def save(self, to: str, pct: float = 0.0, mr_date: date = date(2026, 9, 1)) -> dict:
        sc = self.sc
        index = len(self.steps)
        co_id, branch_id = sc.co(to), sc.branch(to)
        prev = self.steps[-1]["to"] if self.steps else "A"
        # the step totals the screen shows: root lines as the overview lists them
        root_lines = [l for l in self.db.rows("jute_mr_li", jute_mr_id=self.root)
                      if l["active"] in (1, None)]
        line_items = [{"weight": float(l["accepted_weight"] or 0),
                       "original_rate": float(l["rate"] or 0),
                       "original_claim": line_claim(l)} for l in root_lines]
        screen = [{"company": s["to"], "pct_rate_increase": s["pct"]} for s in self.steps]
        screen.append({"company": to, "pct_rate_increase": pct})
        _recalculate_chain(screen, line_items, 0.0, use_new_rounding=True)
        shown = screen[-1]
        step = transfer.TransferStep(
            co_id=co_id, branch_id=branch_id, mr_date=mr_date, mr_rate=shown["weighted_avg_rate"],
            pct_rate_increase=pct, total_amount=shown["total_amount"],
            claim_amount=shown["claim_amount"], net_amount=shown["net_amount"],
            warehouse_id=sc.godown(to), mr_no=0, lc_reference_no="", lc_date=None,
            po_no_for_lc="", order_date_for_lc=None, transfer_transport=True)
        is_final = co_id == sc.a_co and branch_id == sc.a_branch
        source = self.steps[-1]["mr_id"] if self.steps and self.steps[-1]["mr_id"] else self.root
        result = transfer.save_transfer_step(
            source_mr_id=source, step=step, prev_co_id=sc.co(prev),
            prev_branch_id=sc.branch(prev), source_co_id=sc.a_co, source_branch_id=sc.a_branch,
            root_mr_id=self.root, updated_by=sc.user, rate_multiplier=1.0 + pct / 100.0,
            is_first_step=index == 0, is_final=is_final, original_source_mr_id=self.root,
            use_new_rounding=True)
        self.steps.append({"to": to, "pct": pct, "mr_id": result["mr_id"], "final": is_final})
        return result

    def unfinalize(self) -> dict:
        out = transfer.delete_chain_from_step(self.root, self.root, self.sc.user)
        if self.steps and self.steps[-1]["final"]:
            self.steps.pop()
        return out

    def delete_from(self, step_no: int) -> dict:
        out = transfer.delete_chain_from_step(self.root, self.steps[step_no - 1]["mr_id"],
                                              self.sc.user)
        del self.steps[step_no - 1:]
        return out


def party_named(db, co_id, name):
    found = [p["party_id"] for p in db.rows("party_mst", co_id=co_id)
             if p["supp_name"].strip().lower() == name.strip().lower()]
    assert len(found) == 1, (co_id, name, found)
    return found[0]


def branch_of(db, party_id):
    return db.rows("party_branch_mst", party_id=party_id)[0]["party_mst_branch_id"]


def po_state(db, po_id):
    return tuple(db.rows(t, jute_po_id=po_id) for t in PO_TABLES)


def mr_state(db, mr_id):
    return db.rows("jute_mr", jute_mr_id=mr_id), db.rows("jute_mr_li", jute_mr_id=mr_id)


def date_the_root(sc):
    sc.db.update("jute_mr", {"jute_mr_id": sc.root}, jute_mr_date=ROOT_MR_DATE)


def root_rates(sc):
    return [l["rate"] for l in sc.db.rows("jute_mr_li", jute_mr_id=sc.root, active=1)]


REVERTED_KEYS = ("status_id", "branch_mr_no", "bill_pass_no", "bill_pass_date", "invoice_no",
                 "invoice_date", "invoice_amount", "party_id", "party_branch_id", "jute_mr_date",
                 "challan_no", "challan_date", "po_id")


def reverted_root(sc, party="207", party_branch=3811, mr_date=ROOT_MR_DATE):
    """The root header after un-finalize: Pending again, nothing of finalize
    left, the original party / party branch / MR date and gate-entry challan."""
    return {"status_id": 13, "branch_mr_no": None, "bill_pass_no": None, "bill_pass_date": None,
            "invoice_no": None, "invoice_date": None, "invoice_amount": None, "party_id": party,
            "party_branch_id": party_branch, "jute_mr_date": mr_date, "challan_no": "26",
            "challan_date": date(2026, 8, 29), "po_id": sc.po}


def root_header(sc, keys=REVERTED_KEYS):
    row = sc.db.row("jute_mr", jute_mr_id=sc.root)
    return {k: row[k] for k in keys}


def grown_masters(db, before):
    """Master rows added since `before` (a snapshot), by natural key. Fails
    when a master row that existed was changed or removed."""
    after = db.snapshot(MASTER_TABLES)
    new = {}
    for table in MASTER_TABLES:
        key = db.schema.primary_key[table]
        old_ids = {r[key] for r in before[table]}
        assert [r for r in after[table] if r[key] in old_ids] == before[table], table
        new[table] = [r for r in after[table] if r[key] not in old_ids]
    party = {p["party_id"]: (p["co_id"], p["supp_name"]) for p in after["party_mst"]}
    group_co = {g["item_grp_id"]: g["co_id"] for g in after["item_grp_mst"]}
    return {
        "party_mst": sorted((r["co_id"], r["supp_name"]) for r in new["party_mst"]),
        "party_branch_mst": sorted(party[r["party_id"]] for r in new["party_branch_mst"]),
        "item_grp_mst": sorted((r["co_id"], r["item_grp_name"]) for r in new["item_grp_mst"]),
        "item_mst": sorted((group_co[r["item_grp_id"]], r["item_name"]) for r in new["item_mst"]),
        "jute_supp_party_map": sorted((r["co_id"], r["jute_supplier_id"], party[r["party_id"]][1])
                                      for r in new["jute_supp_party_map"]),
    }


# --- 1. step 1 ------------------------------------------------------------------------------

def test_step_1_saves_the_hop_with_a_forwarding_po_on_the_original_supplier(scenario, po_on):
    sc, db = scenario, scenario.db
    root_before, x_before = mr_state(db, sc.root), po_state(db, sc.po)

    result = Chain(sc).save("B", mr_date=date(2026, 9, 1))

    hop, po = result["mr_id"], result["po"]
    assert result["invoice_id"] is None                     # a supplier delivery: no sale yet
    assert {k: po[k] for k in ("role", "po_no", "po_no_formatted", "skipped")} == {
        "role": ROLE_FORWARD, "po_no": 1, "po_no_formatted": FORWARD_B, "skipped": None}
    supplier_in_b = party_named(db, sc.b_co, sc.supplier_party_name)
    hop_row = db.row("jute_mr", jute_mr_id=hop)
    assert {k: hop_row[k] for k in ("branch_id", "src_jute_mr_id", "transfer_mode", "src_com_id",
                                     "party_id", "status_id", "po_id")} == {
        "branch_id": sc.b_branch, "src_jute_mr_id": sc.root, "transfer_mode": 0,
        "src_com_id": sc.a_co, "party_id": str(supplier_in_b), "status_id": 3,
        "po_id": po["po_id"]}
    header = db.row("jute_po", jute_po_id=po["po_id"])
    assert {k: header[k] for k in ("branch_id", "po_date", "party_id", "supplier_id",
                                    "credit_term", "delivery_days", "status_id",
                                    "internal_note")} == {
        "branch_id": sc.b_branch, "po_date": date(2026, 9, 1), "party_id": supplier_in_b,
        "supplier_id": sc.supplier, "credit_term": 45, "delivery_days": 7, "status_id": 5,
        "internal_note": build_marker(ROLE_FORWARD, sc.root, hop, sc.po)}
    po_lines = db.rows("jute_po_li", jute_po_id=po["po_id"])
    assert [(l["quantity"], l["rate"]) for l in po_lines] == [(62.0, 12850.0), (10.0, 12650.0)]
    assert [l["jute_po_li_id"] for l in db.rows("jute_mr_li", jute_mr_id=hop)] == [
        l["jute_po_li_id"] for l in po_lines]
    assert db.count("jute_po_status_log", jute_po_id=po["po_id"]) == 1
    # the mill's MR and its PO are not touched by a step 1
    assert (mr_state(db, sc.root), po_state(db, sc.po)) == (root_before, x_before)


# --- 2. finalize ------------------------------------------------------------------------------

MONEY_COLUMNS = ("total_amount", "claim_amount", "tds_amount", "roundoff", "net_total")


def test_finalize_reprices_the_root_books_the_invoice_and_writes_the_final_po(scenario, po_on):
    sc, db = scenario, scenario.db
    date_the_root(sc)
    db.update("jute_mr_li", {"jute_mr_li_id": sc.root_lines[0]}, water_damage_amount=250.40)
    db.update("jute_mr_li", {"jute_mr_li_id": sc.root_lines[1]}, premium_amount=100.25)
    x_before = po_state(db, sc.po)
    chain = Chain(sc)
    hop = chain.save("B", mr_date=date(2026, 9, 1))["mr_id"]

    result = chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))

    assert result["mr_id"] is None
    # root re-priced at the exact Decimal rate: 12,850 x 1.005 = 12,914.25 -> 129.14 / kg
    lines = db.rows("jute_mr_li", jute_mr_id=sc.root, active=1)
    assert [(l["rate"], l["total_price"], l["jute_po_li_id"]) for l in lines] == [
        (12914.0, Decimal("1202551.68"), sc.po_lines[0]),
        (12713.0, Decimal("183194.33"), sc.po_lines[1])]
    b_in_a = party_named(db, sc.a_co, sc.b_name)
    root = db.row("jute_mr", jute_mr_id=sc.root)
    # Money exactly as the ERP's approve writes it (mr.recompute_mr_money):
    # total to the paisa, claim = 9312 x 1.40 + 1441 x 0.50 = 13,757.30 (the
    # ERP's MR claim has no water damage / premium -- the invoice's has),
    # TDS 0 (B's purchases from A stay under Rs 50 lakh), net to the rupee.
    assert {k: root[k] for k in REVERTED_KEYS + MONEY_COLUMNS} == {
        "status_id": 3, "branch_mr_no": 1, "bill_pass_no": 1, "bill_pass_date": date(2026, 9, 3),
        "invoice_no": "JTSPL/SI/26-27/1", "invoice_date": date(2026, 9, 3),
        "invoice_amount": 1385746.0, "party_id": str(b_in_a),
        "party_branch_id": branch_of(db, b_in_a), "jute_mr_date": date(2026, 9, 3),
        "challan_no": "SEP/0001", "challan_date": date(2026, 9, 3), "po_id": sc.po,
        "total_amount": 1385746.01, "claim_amount": 13757.3, "tds_amount": 0.0,
        "roundoff": 0.29, "net_total": 1371989.0}

    # the invoice B -> A, linked to the last seller's MR (the hop)
    invoice = db.row("sales_invoice", invoice_id=result["invoice_id"])
    assert {k: invoice[k] for k in ("branch_id", "party_id", "invoice_type", "invoice_no",
                                     "invoice_date", "invoice_amount", "round_off")} == {
        "branch_id": sc.b_branch, "party_id": party_named(db, sc.b_co, sc.a_name),
        "invoice_type": 5, "invoice_no": 1, "invoice_date": date(2026, 9, 3),
        "invoice_amount": 1385746.0, "round_off": Decimal("-0.01")}
    dtl = db.rows("sales_invoice_dtl", invoice_id=result["invoice_id"])
    assert [(d["quantity"], d["rate"], d["amount_without_tax"]) for d in dtl] == [
        (9312.0, 129.14, 1202551.68), (1441.0, 127.13, 183194.33)]
    jute = db.row("sales_invoice_jute", invoice_id=result["invoice_id"])
    assert (jute["mr_id"], jute["claim_amount"]) == (hop, Decimal("13907.00"))
    assert [db.row("sales_invoice_jute_dtl", invoice_line_item_id=d["invoice_line_item_id"])[
        "claim_amount_dtl"] for d in dtl] == [13287.2, 620.25]

    # the Final PO at the mill, on B, rates to the closest 50, remembering the old header
    final = result["po"]
    assert {k: final[k] for k in ("role", "po_no", "po_no_formatted", "skipped")} == {
        "role": ROLE_FINAL, "po_no": 12, "po_no_formatted": FINAL_A, "skipped": None}
    header = db.row("jute_po", jute_po_id=final["po_id"])
    assert {k: header[k] for k in ("branch_id", "po_date", "party_id", "supplier_id",
                                    "credit_term", "delivery_days", "status_id",
                                    "internal_note")} == {
        "branch_id": sc.a_branch, "po_date": date(2026, 9, 3), "party_id": b_in_a,
        "supplier_id": sc.supplier, "credit_term": None, "delivery_days": None, "status_id": 5,
        "internal_note": build_marker(ROLE_FINAL, sc.root, sc.root, sc.po, ORIGINAL)}
    assert [(l["quantity"], l["rate"], l["value"])
            for l in db.rows("jute_po_li", jute_po_id=final["po_id"])] == [
        (62.0, 12900.0, Decimal("1199700.00")), (10.0, 12700.0, Decimal("190500.00"))]
    assert po_state(db, sc.po) == x_before                   # X and its lines untouched


# --- 3. un-finalize --------------------------------------------------------------------------

def test_unfinalize_removes_the_final_po_and_invoice_and_restores_the_root(scenario, po_on):
    sc, db = scenario, scenario.db
    date_the_root(sc)
    chain = Chain(sc)
    step1 = chain.save("B", mr_date=date(2026, 9, 1))
    hop_before = mr_state(db, step1["mr_id"]), po_state(db, step1["po"]["po_id"])
    finalized = chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    assert [db.count(t) for t in INVOICE_TABLES] == [1, 2, 1, 2]

    out = chain.unfinalize()

    assert out == {"deleted_mr_ids": [], "deleted_pos": [FINAL_A], "reverted": True}
    assert po_state(db, finalized["po"]["po_id"]) == ([], [], [])
    assert {t: db.count(t) for t in INVOICE_TABLES} == dict.fromkeys(INVOICE_TABLES, 0)
    assert root_header(sc) == reverted_root(sc)
    assert root_rates(sc) == [12850.0, 12650.0]
    root = db.row("jute_mr", jute_mr_id=sc.root)
    assert root["tds_amount"] == 0.0               # a Pending hand-off carries no TDS
    assert (root["roundoff"], root["net_total"]) == transfer._erp_jute_totals(
        root["total_amount"], root["claim_amount"], 0.0)
    # the hop and its Forwarding PO stay as they were
    assert (mr_state(db, step1["mr_id"]), po_state(db, step1["po"]["po_id"])) == hop_before

    # finalize again: a fresh Final PO (the number is free again: it was the latest)
    again = chain.save("A", pct=0.5, mr_date=date(2026, 9, 4))
    assert again["po"]["po_id"] not in (None, finalized["po"]["po_id"])
    assert (again["po"]["po_no"], again["po"]["po_no_formatted"]) == (12, FINAL_A)
    header = db.row("jute_po", jute_po_id=again["po"]["po_id"])
    assert (header["po_date"], header["internal_note"]) == (
        date(2026, 9, 4), build_marker(ROLE_FINAL, sc.root, sc.root, sc.po, ORIGINAL))
    assert {r["jute_po_id"] for r in db.rows("jute_po")} == {
        sc.po, step1["po"]["po_id"], again["po"]["po_id"]}
    assert root_rates(sc) == [12914.0, 12713.0] and root_header(sc)["status_id"] == 3


# --- 4. un-finalize without a Final PO --------------------------------------------------------

@pytest.mark.parametrize("rule, party, party_branch", [
    ("one party of that name", "207", 3811),
    ("same name: the original PO's party", "300", 3300),
    ("same name: mapped to the root's supplier", "207", 3811),
    ("same name: lowest id", "150", 2900),
])
def test_unfinalize_without_a_final_po_derives_the_party_by_name(scenario, monkeypatch, rule,
                                                                 party, party_branch):
    sc, db = scenario, scenario.db
    date_the_root(sc)
    if rule.startswith("same name"):
        sc.add_party(sc.a_co, " honeywell commercial pvt. ltd. ", party_id=150,
                     branch_ids=(2950, 2900))
        sc.add_party(sc.a_co, "Honeywell Commercial Pvt. Ltd.", party_id=300, branch_ids=(3300,))
        db.update("jute_po", {"jute_po_id": sc.po},
                  party_id=300 if rule.endswith("PO's party") else 98)   # 98: another name
        if rule.endswith("lowest id"):
            db.delete("jute_supp_party_map", map_id=5001)               # (A, 2430) -> 207
    monkeypatch.setenv("JT_TRANSFER_PO", "0")                # finalized with POs switched off
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    assert [r["jute_po_id"] for r in db.rows("jute_po")] == [sc.po]
    monkeypatch.setenv("JT_TRANSFER_PO", "1")                # (the switch plays no part here)

    out = chain.unfinalize()

    assert out == {"deleted_mr_ids": [], "deleted_pos": [], "reverted": True}
    assert root_header(sc) == reverted_root(sc, party, party_branch, mr_date=None)
    assert root_rates(sc) == [12850.0, 12650.0]


def test_unfinalize_with_a_final_po_that_remembers_nothing_derives_the_party(scenario,
                                                                           monkeypatch):
    """A chain finalized with POs off whose Final PO came later, from the
    backfill (no orig= in its marker): the PO goes, the party is derived."""
    sc, db = scenario, scenario.db
    date_the_root(sc)
    monkeypatch.setenv("JT_TRANSFER_PO", "0")
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    monkeypatch.setenv("JT_TRANSFER_PO", "1")
    with DatabaseConnection.get_transaction() as conn:
        backfilled = po_ops.create_final_po(conn, sc.root, sc.user)       # original unknown

    out = chain.unfinalize()

    assert out == {"deleted_mr_ids": [], "deleted_pos": [FINAL_A], "reverted": True}
    assert po_state(db, backfilled["po_id"]) == ([], [], [])
    assert root_header(sc) == reverted_root(sc, mr_date=None)


def test_derived_party_keeps_the_roots_party_branch_only_while_the_party_is_unchanged(
        scenario, po_on):
    sc, db = scenario, scenario.db
    db.insert("party_branch_mst", party_mst_branch_id=3000, party_id=sc.supplier_party, active=1,
              created_by=24, updated_by=24)                 # a lower branch id of party 207
    chain = Chain(sc)
    hop = chain.save("B")["mr_id"]

    def derived():
        with DatabaseConnection.get_transaction() as conn:
            return transfer._derive_original_party(conn, sc.root, get_source_mr_full(hop, conn=conn))

    assert derived() == ("207", 3811)                        # party unchanged: its own branch
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    assert derived() == ("207", 3000)                        # party changed: lowest branch id


# --- 5. delete from step 1 of a finalized chain -------------------------------------------------

def test_delete_from_step_1_of_a_finalized_chain_removes_all_and_reverts_the_root(scenario, po_on):
    sc, db = scenario, scenario.db
    date_the_root(sc)
    root_line_ids = [l["jute_mr_li_id"] for l in db.rows("jute_mr_li")]
    chain = Chain(sc)
    hop = chain.save("B", mr_date=date(2026, 9, 1))["mr_id"]
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))

    out = chain.delete_from(1)

    assert out == {"deleted_mr_ids": [hop], "deleted_pos": [FORWARD_B, FINAL_A], "reverted": True}
    assert [r["jute_po_id"] for r in db.rows("jute_po")] == [sc.po]
    assert [r["jute_po_li_id"] for r in db.rows("jute_po_li")] == list(sc.po_lines)
    assert db.count("jute_po_status_log") == 0
    assert {t: db.count(t) for t in INVOICE_TABLES} == dict.fromkeys(INVOICE_TABLES, 0)
    assert [r["jute_mr_id"] for r in db.rows("jute_mr")] == [sc.root]
    assert [l["jute_mr_li_id"] for l in db.rows("jute_mr_li")] == root_line_ids
    assert root_header(sc) == reverted_root(sc)
    assert root_rates(sc) == [12850.0, 12650.0]


# --- 6. A -> B -> C -> A -----------------------------------------------------------------------

def test_multi_hop_chain_and_delete_from_the_middle(scenario, po_on):
    sc, db = scenario, scenario.db
    date_the_root(sc)
    chain = Chain(sc)
    to_b = chain.save("B", mr_date=date(2026, 9, 1))
    hop_b_before = mr_state(db, to_b["mr_id"]), po_state(db, to_b["po"]["po_id"])
    to_c = chain.save("C", pct=0.5, mr_date=date(2026, 9, 2))
    back = chain.save("A", pct=1.0, mr_date=date(2026, 9, 3))

    def head(po):
        h = db.row("jute_po", jute_po_id=po["po_id"])
        return (h["branch_id"], h["party_id"], h["supplier_id"], h["credit_term"],
                h["delivery_days"])

    def rates(po):
        return [l["rate"] for l in db.rows("jute_po_li", jute_po_id=po["po_id"])]

    # B: on the outside supplier (as a party of B) with X's terms
    assert head(to_b["po"]) == (sc.b_branch, party_named(db, sc.b_co, sc.supplier_party_name),
                                sc.supplier, 45, 7)
    # C: on B as a party of C, no terms (a sister company sold it); 12,914 / 12,713 -> 50s
    assert head(to_c["po"]) == (sc.c_branch, party_named(db, sc.c_co, sc.b_name), sc.supplier,
                                None, None)
    assert to_c["po"]["po_no_formatted"] == FORWARD_C
    assert rates(to_c["po"]) == [12900.0, 12700.0]
    # Final at A on C: 12,914 x 1.01 = 13,043.14 -> 13,043 -> PO 13,050; 12,840.13 -> 12,840 -> 12,850
    assert root_rates(sc) == [13043.0, 12840.0]
    assert head(back["po"]) == (sc.a_branch, party_named(db, sc.a_co, sc.c_name), sc.supplier,
                                None, None)
    assert rates(back["po"]) == [13050.0, 12850.0]
    assert [db.count(t) for t in INVOICE_TABLES] == [2, 4, 2, 4]   # B -> C and C -> A

    out = chain.delete_from(2)

    assert out == {"deleted_mr_ids": [to_c["mr_id"]], "deleted_pos": [FORWARD_C, FINAL_A],
                   "reverted": True}
    assert {r["jute_po_id"] for r in db.rows("jute_po")} == {sc.po, to_b["po"]["po_id"]}
    assert db.count("jute_mr", jute_mr_id=to_c["mr_id"]) == 0
    assert {t: db.count(t) for t in INVOICE_TABLES} == dict.fromkeys(INVOICE_TABLES, 0)
    assert (mr_state(db, to_b["mr_id"]), po_state(db, to_b["po"]["po_id"])) == hop_b_before
    assert root_header(sc) == reverted_root(sc)
    assert root_rates(sc) == [12850.0, 12650.0]             # step 1's rates


def test_deleting_a_step_takes_the_invoice_of_the_highest_earlier_hop(scenario, po_on):
    """A -> B -> C -> B visits B twice. Deleting the C step removes the sale
    onward (C -> B, linked to it) and the sale that created it (B -> C,
    linked to step 1 = the highest chain id below it) -- not the later B hop."""
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    to_b = chain.save("B", mr_date=date(2026, 9, 1))
    to_c = chain.save("C", pct=0.5, mr_date=date(2026, 9, 2))
    again_b = chain.save("B", pct=0.5, mr_date=date(2026, 9, 3))
    assert [db.row("sales_invoice_jute", invoice_id=r["invoice_id"])["mr_id"]
            for r in (to_c, again_b)] == [to_b["mr_id"], to_c["mr_id"]]
    assert again_b["po"]["po_no_formatted"] == "JTSPL/JPO/26-27/00002"
    keep = (mr_state(db, to_b["mr_id"]), po_state(db, to_b["po"]["po_id"]),
            mr_state(db, again_b["mr_id"]), po_state(db, again_b["po"]["po_id"]))

    assert transfer.delete_transfer_step(to_c["mr_id"], sc.user) == {
        "po_id": to_c["po"]["po_id"], "po_no_formatted": FORWARD_C}

    assert {t: db.count(t) for t in INVOICE_TABLES} == dict.fromkeys(INVOICE_TABLES, 0)
    assert mr_state(db, to_c["mr_id"]) == ([], [])
    assert (mr_state(db, to_b["mr_id"]), po_state(db, to_b["po"]["po_id"]),
            mr_state(db, again_b["mr_id"]), po_state(db, again_b["po"]["po_id"])) == keep


def test_delete_chain_from_step_without_anything_to_do_changes_nothing(scenario, po_on):
    sc, db = scenario, scenario.db
    nothing = {"deleted_mr_ids": [], "deleted_pos": [], "reverted": False}
    other_root = sc.add_root()
    other_hop = Chain(sc, root=other_root).save("B")["mr_id"]
    before = db.snapshot()
    assert transfer.delete_chain_from_step(sc.root, sc.root, sc.user) == nothing   # no chain
    assert db.diff(before, db.snapshot()) == []
    Chain(sc).save("B", mr_date=date(2026, 9, 1))
    before = db.snapshot()
    for root, step in ((sc.root, sc.root),         # not finalized: nothing to un-finalize
                       (sc.root, 999999),          # not a step of this chain
                       (sc.root, other_hop),       # a step of another lorry's chain
                       (999999, 999999)):          # no such root
        assert transfer.delete_chain_from_step(root, step, sc.user) == nothing
    assert db.diff(before, db.snapshot()) == []


# --- 7. symmetry: save, then delete from step 1 ----------------------------------------------------

GROWN_A_B_A = {
    "party_mst": sorted([(74, "HONEYWELL COMMERCIAL PVT. LTD."),     # supplier as party of B
                         (74, "THE EMPIRE JUTE COMPANY LTD."),       # A, buyer of B's invoice
                         (2, "Jagrati Trade Services Pvt. Ltd.")]),  # B, the root's final party
    "party_branch_mst": sorted([(74, "HONEYWELL COMMERCIAL PVT. LTD."),
                                (74, "THE EMPIRE JUTE COMPANY LTD."),
                                (2, "Jagrati Trade Services Pvt. Ltd.")]),
    "item_grp_mst": [(74, "JUTE")],
    "item_mst": [(74, "D TD-5"), (74, "D TD-6")],
    "jute_supp_party_map": [(74, 2430, "HONEYWELL COMMERCIAL PVT. LTD.")],
}


def finalized_round_trip(sc):
    """A -> B -> A saved, then 'Delete from Step 1 onward'. The snapshot
    before the first save."""
    date_the_root(sc)
    before = sc.db.snapshot()
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    chain.delete_from(1)
    return before


def test_open_chain_saved_then_deleted_from_step_1_leaves_no_trace(scenario, po_on):
    sc, db = scenario, scenario.db
    date_the_root(sc)
    others = [t for t in db.schema.columns if t not in MASTER_TABLES]
    before = db.snapshot(others, ignore=AUDIT)
    masters = db.snapshot(MASTER_TABLES)
    chain = Chain(sc)
    hop_b = chain.save("B", mr_date=date(2026, 9, 1))["mr_id"]
    hop_c = chain.save("C", pct=0.5, mr_date=date(2026, 9, 2))["mr_id"]
    assert (db.count("jute_po"), db.count("sales_invoice")) == (3, 1)

    out = chain.delete_from(1)

    assert out == {"deleted_mr_ids": [hop_c, hop_b], "deleted_pos": [FORWARD_C, FORWARD_B],
                   "reverted": False}
    assert db.diff(before, db.snapshot(others, ignore=AUDIT)) == []
    assert grown_masters(db, masters) == {
        "party_mst": sorted([(74, "HONEYWELL COMMERCIAL PVT. LTD."),
                             (74, "Greeting Marketing Pvt. Ltd."),       # C, buyer of B's invoice
                             (27, "Jagrati Trade Services Pvt. Ltd.")]), # B, hop C's party
        "party_branch_mst": sorted([(74, "HONEYWELL COMMERCIAL PVT. LTD."),
                                    (74, "Greeting Marketing Pvt. Ltd."),
                                    (27, "Jagrati Trade Services Pvt. Ltd.")]),
        "item_grp_mst": [(27, "JUTE"), (74, "JUTE")],
        "item_mst": [(27, "D TD-5"), (27, "D TD-6"), (74, "D TD-5"), (74, "D TD-6")],
        "jute_supp_party_map": [(27, 2430, "Jagrati Trade Services Pvt. Ltd."),
                                (74, 2430, "HONEYWELL COMMERCIAL PVT. LTD.")],
    }


def test_finalized_chain_deleted_from_step_1_removes_everything_it_created(scenario, po_on):
    sc, db = scenario, scenario.db
    before = finalized_round_trip(sc)
    after = db.snapshot()
    for table in PO_TABLES + INVOICE_TABLES:
        assert after[table] == before[table], table
    for table in ("jute_mr", "jute_mr_li"):
        key = db.schema.primary_key[table]
        assert [r[key] for r in after[table]] == [r[key] for r in before[table]], table
    untouched = [t for t in db.schema.columns
                 if t not in PO_TABLES + INVOICE_TABLES + MASTER_TABLES + ("jute_mr", "jute_mr_li")]
    assert db.diff({t: before[t] for t in untouched}, {t: after[t] for t in untouched}) == []
    assert grown_masters(db, {t: before[t] for t in MASTER_TABLES}) == GROWN_A_B_A


@pytest.mark.parametrize("undo", ["un-finalize", "delete from step 1"])
def test_round_trip_restores_the_root_lines_exactly(scenario, po_on, undo):
    sc, db = scenario, scenario.db
    date_the_root(sc)
    before = db.snapshot(["jute_mr_li"], ignore=AUDIT)
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    if undo == "un-finalize":
        chain.unfinalize()
        chain.delete_from(1)
    else:
        chain.delete_from(1)
    assert db.diff(before, db.snapshot(["jute_mr_li"], ignore=AUDIT)) == []


# Decided 2026-10-02: un-finalize does not try to bring back the ERP
# hand-off's money columns (a NULL claim / net total is what made the P&L read
# finalized purchases as 0). It recomputes them from the restored lines the
# way the ERP's own Pending hand-off does (mr.recompute_mr_money, TDS 0).
# Every other header column comes back exactly.


def test_round_trip_restores_the_root_header_and_recomputes_its_money(scenario, po_on):
    sc, db = scenario, scenario.db
    before = finalized_round_trip(sc)
    drop = set(AUDIT["jute_mr"]) | set(MONEY_COLUMNS)
    expected = {"jute_mr": [{k: v for k, v in r.items() if k not in drop}
                            for r in before["jute_mr"]]}
    after = db.snapshot(["jute_mr"], ignore=AUDIT)
    actual = {"jute_mr": [{k: v for k, v in r.items() if k not in drop}
                          for r in after["jute_mr"]]}
    assert db.diff(expected, actual) == []

    root = db.rows("jute_mr", jute_mr_id=sc.root)[0]
    lines = [li for li in db.rows("jute_mr_li", jute_mr_id=sc.root)
             if li["active"] in (1, None)]
    total = round(sum(float(li["accepted_weight"]) / 100 * float(li["rate"]) for li in lines), 2)
    claim = round(sum(float(li["accepted_weight"]) / 100 * float(li["claim_rate"]) for li in lines), 2)
    assert (root["total_amount"], root["claim_amount"], root["tds_amount"]) == (total, claim, 0.0)
    assert (root["roundoff"], root["net_total"]) == transfer._erp_jute_totals(total, claim, 0.0)


# --- 8. atomicity ------------------------------------------------------------------------------

@pytest.mark.parametrize("blocker", [
    "issue entry on step 1", "issue entry on step 2", "ERP MR on step 2's Forwarding PO",
    "ERP MR on the Final PO"])
def test_an_undeletable_step_leaves_every_table_unchanged(scenario, po_on, blocker):
    sc, db = scenario, scenario.db
    date_the_root(sc)
    chain = Chain(sc)
    to_b = chain.save("B", mr_date=date(2026, 9, 1))
    to_c = chain.save("C", pct=0.5, mr_date=date(2026, 9, 2))
    back = chain.save("A", pct=1.0, mr_date=date(2026, 9, 3))
    if blocker.startswith("issue entry"):
        hop = to_b["mr_id"] if blocker.endswith("1") else to_c["mr_id"]
        db.insert("jute_issue", jute_mr_li_id=sc.active_lines(hop)[0], issue_date=date(2026, 9, 10),
                  status_id=None if blocker.endswith("2") else 3, weight=150, branch_id=sc.b_branch)
        error = "has ERP issue entries"
    else:
        po = to_c["po"] if "Forwarding" in blocker else back["po"]
        branch = sc.c_branch if "Forwarding" in blocker else sc.a_branch
        db.insert("jute_mr", branch_id=branch, po_id=po["po_id"], status_id=3,
                  jute_gate_entry_no=77, transfer_mode=0)
        error = rf"Transfer PO {po['po_id']} has other MR\(s\) linked to it in the ERP"
    before, start = db.snapshot(), len(db.statements)

    with pytest.raises(ValueError, match=error):
        chain.delete_from(1)

    assert db.diff(before, db.snapshot()) == []
    if blocker.startswith("issue entry"):            # every guard ran before the first DELETE
        assert not [s for s in db.writes(start) if s.upper().startswith("DELETE")]


def test_unfinalize_blocked_by_an_erp_mr_on_the_final_po_changes_nothing(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    back = chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    db.insert("jute_mr", branch_id=sc.a_branch, po_id=back["po"]["po_id"], status_id=3,
              jute_gate_entry_no=77, transfer_mode=0)
    before = db.snapshot()
    with pytest.raises(ValueError, match="has other MR\\(s\\) linked to it in the ERP"):
        chain.unfinalize()
    assert db.diff(before, db.snapshot()) == []


def test_a_cancelled_issue_entry_does_not_block_the_delete(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    hop = chain.save("B", mr_date=date(2026, 9, 1))["mr_id"]
    db.insert("jute_issue", jute_mr_li_id=sc.active_lines(hop)[0], status_id=4, weight=150)
    assert chain.delete_from(1)["deleted_mr_ids"] == [hop]


# --- 9. guards ---------------------------------------------------------------------------------

def test_step_1_cannot_be_saved_twice(scenario, po_on):
    sc, db = scenario, scenario.db
    Chain(sc).save("B", mr_date=date(2026, 9, 1))
    before = db.snapshot()
    with pytest.raises(ValueError, match="already has a transfer chain"):
        Chain(sc).save("B", mr_date=date(2026, 9, 1))      # a second tab still showing no chain
    assert db.diff(before, db.snapshot()) == []


def test_a_chain_cannot_be_finalized_twice(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    stale = Chain(sc)
    stale.steps = list(chain.steps)                         # a tab loaded before the finalize
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    before = db.snapshot()
    with pytest.raises(ValueError, match="already finalized"):
        stale.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    assert db.diff(before, db.snapshot()) == []


def test_every_save_and_delete_locks_the_root_row_first(scenario, po_on):
    """Row locks are not emulated; what is checked is that the root row's
    SELECT ... FOR UPDATE is the first statement of each transaction."""
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    for action in (lambda: chain.save("B", mr_date=date(2026, 9, 1)),
                   lambda: chain.save("A", pct=0.5, mr_date=date(2026, 9, 3)),
                   chain.unfinalize, lambda: chain.delete_from(1)):
        start = len(db.statements)
        action()
        assert db.statements[start].endswith("FROM jute_mr WHERE jute_mr_id = :id FOR UPDATE")
        assert db.parameters[start] == {"id": sc.root}


def test_save_returns_the_mr_the_invoice_and_the_po(scenario, po_on):
    sc = scenario
    chain = Chain(sc)
    for result in (chain.save("B", mr_date=date(2026, 9, 1)),
                   chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))):
        assert set(result) == {"mr_id", "invoice_id", "po"}


def test_a_chain_only_starts_from_a_pending_root(scenario, po_on):
    sc, db = scenario, scenario.db
    db.update("jute_mr", {"jute_mr_id": sc.root}, status_id=3)
    before = db.snapshot()
    with pytest.raises(ValueError, match=r"is not Pending \(13\)"):
        Chain(sc).save("B")
    assert db.diff(before, db.snapshot()) == []


@pytest.mark.parametrize("which", ["the root", "an ERP MR", "a marked-godown child"])
def test_delete_transfer_step_refuses_an_mr_that_is_not_a_chain_step(scenario, po_on, which):
    sc, db = scenario, scenario.db
    Chain(sc).save("B", mr_date=date(2026, 9, 1))
    mr = {"the root": lambda: sc.root, "an ERP MR": sc.add_root,
          "a marked-godown child": lambda: sc.add_hop(transfer_mode=1)}[which]()
    before = db.snapshot()
    with pytest.raises(ValueError, match="is not a vertical-chain step; cannot delete"):
        transfer.delete_transfer_step(mr, sc.user)
    assert db.diff(before, db.snapshot()) == []
    before = db.snapshot()
    assert transfer.delete_transfer_step(999999, sc.user) is None      # no such MR: no-op
    assert db.diff(before, db.snapshot()) == []


def test_delete_transfer_step_removes_one_hop_with_its_po_and_invoices(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    to_b = chain.save("B", mr_date=date(2026, 9, 1))
    hop_b_before = mr_state(db, to_b["mr_id"]), po_state(db, to_b["po"]["po_id"])
    to_c = chain.save("C", pct=0.5, mr_date=date(2026, 9, 2))

    out = transfer.delete_transfer_step(to_c["mr_id"], sc.user)

    assert out == {"po_id": to_c["po"]["po_id"], "po_no_formatted": FORWARD_C}
    assert mr_state(db, to_c["mr_id"]) == ([], [])
    assert po_state(db, to_c["po"]["po_id"]) == ([], [], [])
    assert {t: db.count(t) for t in INVOICE_TABLES} == dict.fromkeys(INVOICE_TABLES, 0)
    assert (mr_state(db, to_b["mr_id"]), po_state(db, to_b["po"]["po_id"])) == hop_b_before


# --- 10. switched off ------------------------------------------------------------------------------

def test_switched_off_every_flow_works_and_no_po_is_ever_written(scenario, po_off):
    sc, db = scenario, scenario.db
    date_the_root(sc)

    def no_transfer_po():
        assert [r["jute_po_id"] for r in db.rows("jute_po")] == [sc.po]
        assert [r["jute_po_li_id"] for r in db.rows("jute_po_li")] == list(sc.po_lines)
        assert db.count("jute_po_status_log") == 0
        assert {r["jute_mr_id"]: r["po_id"] for r in db.rows("jute_mr")
                if r["src_jute_mr_id"] is not None} == {s["mr_id"]: None for s in chain.steps
                                                        if s["mr_id"]}

    chain = Chain(sc)
    saved = [chain.save("B", mr_date=date(2026, 9, 1)),
             chain.save("C", pct=0.5, mr_date=date(2026, 9, 2)),
             chain.save("A", pct=1.0, mr_date=date(2026, 9, 3))]
    for result in saved:
        assert result["po"]["po_id"] is None and "switched off" in result["po"]["skipped"]
    no_transfer_po()
    assert root_header(sc)["status_id"] == 3

    assert chain.unfinalize() == {"deleted_mr_ids": [], "deleted_pos": [], "reverted": True}
    assert root_header(sc) == reverted_root(sc, mr_date=None)     # derived: no Final PO
    no_transfer_po()

    chain.save("A", pct=1.0, mr_date=date(2026, 9, 4))
    hop_c = chain.steps[1]["mr_id"]
    assert chain.delete_from(2) == {"deleted_mr_ids": [hop_c], "deleted_pos": [],
                                    "reverted": True}
    hop_b = chain.steps[0]["mr_id"]
    assert chain.delete_from(1) == {"deleted_mr_ids": [hop_b], "deleted_pos": [],
                                    "reverted": False}
    no_transfer_po()
    assert [r["jute_mr_id"] for r in db.rows("jute_mr")] == [sc.root]
    assert {t: db.count(t) for t in INVOICE_TABLES} == dict.fromkeys(INVOICE_TABLES, 0)
    assert root_header(sc)["status_id"] == 13 and root_rates(sc) == [12850.0, 12650.0]


def test_switched_off_existing_transfer_pos_are_still_deleted(scenario, monkeypatch):
    sc, db = scenario, scenario.db
    monkeypatch.setenv("JT_TRANSFER_PO", "1")
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    monkeypatch.setenv("JT_TRANSFER_PO", "0")

    out = chain.delete_from(1)

    assert out["deleted_pos"] == [FORWARD_B, FINAL_A]
    assert [r["jute_po_id"] for r in db.rows("jute_po")] == [sc.po]
    assert db.count("jute_po_status_log") == 0


# --- 11. a failing PO write rolls the whole step back -------------------------------------------------

@pytest.mark.parametrize("step", ["first", "intermediate", "final"])
def test_a_failure_inside_po_creation_rolls_the_whole_step_back(scenario, po_on, monkeypatch, step):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    if step != "first":
        chain.save("B", mr_date=date(2026, 9, 1))
    before = db.snapshot()

    def boom(conn, plan, updated_by):
        raise RuntimeError("PO insert failed")

    monkeypatch.setattr(po_ops, "_apply", boom)
    with pytest.raises(RuntimeError, match="PO insert failed"):
        {"first": lambda: chain.save("B", mr_date=date(2026, 9, 1)),
         "intermediate": lambda: chain.save("C", pct=0.5, mr_date=date(2026, 9, 2)),
         "final": lambda: chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))}[step]()

    assert db.diff(before, db.snapshot()) == []               # no hop, no invoice, no masters


# --- 12. posted rate = screen rate ---------------------------------------------------------------------

@pytest.mark.parametrize("to", ["C", "A"], ids=["intermediate step", "final step"])
def test_half_a_percent_on_13100_posts_13166(scenario, po_on, to):
    sc, db = scenario, scenario.db
    db.update("jute_mr_li", {"jute_mr_li_id": sc.root_lines[0]}, rate=13100,
              total_price=Decimal("1219872.00"))
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))

    result = chain.save(to, pct=0.5, mr_date=date(2026, 9, 3))

    # 13,100 x 1.005 = 13,165.5 -> 131.655 / kg -> 131.66 (float arithmetic gave 13,165)
    mr_line = db.rows("jute_mr_li", jute_mr_id=result["mr_id"] or sc.root, active=1)[0]
    assert (mr_line["rate"], mr_line["total_price"]) == (13166.0, Decimal("1226017.92"))
    invoice = db.row("sales_invoice", invoice_id=result["invoice_id"])
    dtl = db.rows("sales_invoice_dtl", invoice_id=result["invoice_id"])
    assert (dtl[0]["rate"], dtl[0]["amount_without_tax"]) == (131.66, 1226017.92)
    assert invoice["invoice_amount"] == 1409212.0           # what the screen showed
    lines_total = sum(Decimal(str(d["amount_without_tax"])) for d in dtl)
    assert lines_total == Decimal(str(invoice["invoice_amount"])) - invoice["round_off"]
    assert abs(invoice["round_off"]) < Decimal("0.5")
    assert db.rows("jute_po_li", jute_po_id=result["po"]["po_id"])[0]["rate"] == 13150.0
