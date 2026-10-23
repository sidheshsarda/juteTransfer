"""Bill To / Ship To on the transfer invoices: the buyer company's own
branches, mapped onto its party branches in the seller's books by address
(transfer._invoice_party_branch). The Tally export reads the buyer address
from billing_to_id and the Destination from shipping_to_id."""
from datetime import date

import pytest

from src.jutetransfer import transfer

from .fake_mysql import fake_db, scenario  # noqa: F401  (fixtures)
from .test_marked_stock import approve, invoice_of, move
from .test_transfer_po_flow import Chain, po_on  # noqa: F401  (po_on is a fixture)

C_HO = 34


def add_ho(sc, branch_id=C_HO, co="C"):
    """A head office for a company: another branch, another address, same GSTIN."""
    factory = sc.db.row("branch_mst", branch_id=sc.branch(co))
    sc.db.insert("branch_mst", branch_id=branch_id, branch_name="HO", co_id=sc.co(co),
                 branch_address1="3A SHAKESPEARE SARANI", branch_zipcode=700071,
                 country_id=101, city_id=5583, state_id=19, gst_no=factory["gst_no"],
                 active=1, updated_by=sc.user)
    return branch_id


def address(db, party_branch_id):
    return db.row("party_branch_mst", party_mst_branch_id=party_branch_id)["address"]


def invoice_to(db, buyer_mr_id_before):
    """The invoice that sold the MR buyer_mr_id_before (sales_invoice_jute.mr_id)."""
    (link,) = db.rows("sales_invoice_jute", mr_id=buyer_mr_id_before)
    return db.row("sales_invoice", invoice_id=link["invoice_id"])


def test_bill_to_ho_and_ship_to_the_receiving_branch(scenario, po_on):
    sc, db = scenario, scenario.db
    ho = add_ho(sc)
    chain = Chain(sc)
    b = chain.save("B")
    chain.save("C", bill_to_branch_id=ho, ship_to_branch_id=sc.c_branch)
    inv = invoice_to(db, b["mr_id"])
    assert address(db, inv["billing_to_id"]) == "3A SHAKESPEARE SARANI"
    assert address(db, inv["shipping_to_id"]) == f"{sc.c_branch} MILL ROAD"


def test_the_same_picks_on_the_next_lorry_add_no_party_branch(scenario, po_on):
    sc, db = scenario, scenario.db
    ho = add_ho(sc)
    first = Chain(sc)
    b1 = first.save("B")
    first.save("C", bill_to_branch_id=ho, ship_to_branch_id=sc.c_branch)
    inv1 = invoice_to(db, b1["mr_id"])
    branches = db.rows("party_branch_mst", party_id=inv1["party_id"])

    second = Chain(sc, root=sc.add_root())
    b2 = second.save("B")
    second.save("C", bill_to_branch_id=ho, ship_to_branch_id=sc.c_branch)
    inv2 = invoice_to(db, b2["mr_id"])
    assert db.rows("party_branch_mst", party_id=inv1["party_id"]) == branches
    assert (inv2["billing_to_id"], inv2["shipping_to_id"]) == (inv1["billing_to_id"],
                                                               inv1["shipping_to_id"])


def test_no_picks_keeps_the_partys_first_branch(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    b = chain.save("B")
    chain.save("C")
    inv = invoice_to(db, b["mr_id"])
    (first,) = db.rows("party_branch_mst", party_id=inv["party_id"])
    assert inv["billing_to_id"] == inv["shipping_to_id"] == first["party_mst_branch_id"]


def test_a_branch_of_another_company_is_refused_and_nothing_is_written(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    chain.save("B")
    before = len(db.rows("sales_invoice"))
    with pytest.raises(ValueError, match="not a branch of company"):
        chain.save("C", bill_to_branch_id=sc.a_branch, ship_to_branch_id=sc.c_branch)
    assert len(db.rows("sales_invoice")) == before


def test_a_marked_move_ships_to_the_target_branch(scenario):
    """B is already a party of A at another address: the bill stays on that
    branch, the shipment names the branch the jute goes to."""
    sc, db = scenario, scenario.db
    b_in_a = sc.add_party(sc.a_co, sc.b_name)
    (old,) = db.rows("party_branch_mst", party_id=b_in_a)
    approve(sc)
    (result,) = move(sc, sc.root_lines[:1])
    inv, _ = invoice_of(db, result)
    assert inv["billing_to_id"] == old["party_mst_branch_id"]
    assert address(db, inv["shipping_to_id"]) == f"{sc.b_branch} MILL ROAD"
