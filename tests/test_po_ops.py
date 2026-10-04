"""Transfer PO writes (po_ops.py) through the fake sls database.

Row by row what a Forwarding / Final PO holds (DESIGN §3), when none is made
(§4) and what deleting one removes or refuses (§7). Chain states are
hand-built with the Scenario helpers of tests/fake_mysql.py;
tests/test_transfer_po_flow.py drives the same code through transfer.py.
PO creation is switched on explicitly (JT_TRANSFER_PO=1) wherever a PO is
expected: the shipped default must not matter here."""
from datetime import date, datetime
from decimal import Decimal

import pytest

from src.jutetransfer import po_ops
from src.jutetransfer.database import DatabaseConnection
from src.jutetransfer.po_helpers import (
    CLOSE_TYPE_TRANSFER,
    ROLE_FINAL,
    ROLE_FORWARD,
    build_marker,
    final_close_remark,
    forward_close_remark,
    po_remarks,
)

from .fake_mysql import build_scenario, fake_db, scenario  # noqa: F401  (fixtures)

ORIGINAL_PO = "EJM/F/JPO/26-27/00011"                 # PO X as the ERP prints it
FORWARDER_IN_A = "JAGRATI TRADE SERVICES PVT. LTD."   # B as a party of A (Scenario.finalize_root)


@pytest.fixture
def po_on(monkeypatch):
    monkeypatch.setenv("JT_TRANSFER_PO", "1")


def in_txn(fn, *args, **kwargs):
    """A po_ops call the way transfer.py makes it: on the caller's
    connection, inside one transaction (rolled back when it raises)."""
    with DatabaseConnection.get_transaction() as conn:
        return fn(conn, *args, **kwargs)


def header(db, **cols):
    """An expected jute_po row: every column NULL unless given."""
    row = dict.fromkeys(db.schema.columns["jute_po"])
    row.update(cols)
    return row


def po_line(db, **cols):
    """An expected jute_po_li row: §3.4's fixed values, NULL elsewhere."""
    row = dict.fromkeys(db.schema.columns["jute_po_li"])
    row.update(active=1, status_id=21, updated_date_time=db.now)
    row.update(cols)
    return row


def lines_of(db, po_id):
    return db.rows("jute_po_li", jute_po_id=po_id)


def summary(out):
    return {k: out[k] for k in ("role", "po_no", "po_no_formatted", "skipped")}


# --- Forwarding PO ---------------------------------------------------------------

def test_forwarding_po_header(scenario, po_on):
    sc, db = scenario, scenario.db
    hop = sc.add_hop(mr_date=date(2026, 9, 1))           # the hop's own GE: 1 of 01-09-2026
    party = int(db.row("jute_mr", jute_mr_id=hop)["party_id"])
    start = len(db.statements)

    out = in_txn(po_ops.create_forward_po, hop, 7)

    assert summary(out) == {"role": ROLE_FORWARD, "po_no": 1,
                            "po_no_formatted": "JTSPL/JPO/26-27/00001", "skipped": None}
    po = db.row("jute_po", jute_po_id=out["po_id"])
    assert po == header(
        db, jute_po_id=out["po_id"], branch_id=sc.b_branch, po_no=1, po_date=date(2026, 9, 1),
        supplier_id=sc.supplier, party_id=party, jute_mukam_id=sc.mukam, jute_uom="BALE",
        vehicle_type_id=None, vehicle_quantity=1, weight=10800.0,
        jute_po_value=Decimal("1384800.00"), channel_code="DOMESTIC",
        credit_term=sc.po_credit_term, delivery_days=sc.po_delivery_days,
        remarks=po_remarks(ROLE_FORWARD),
        # closed_by stays NULL like the ERP's own CLI closes: the app's user id
        # is not an sls user, and the ERP would show "User #7"
        status_id=5, close_type=CLOSE_TYPE_TRANSFER, closed_date=db.now, closed_by=None,
        close_remark=forward_close_remark(sc.ge_no, sc.ge_date, sc.a_name, ORIGINAL_PO),
        internal_note=build_marker(ROLE_FORWARD, sc.root, hop, sc.po),
        updated_by=7, updated_date_time=db.now)
    # the remark names the ROOT's lorry, the mill and the original PO
    assert ("for lorry GE 21 dt 31-08-2026 received at THE EMPIRE JUTE COMPANY LTD. "
            "Original PO EJM/F/JPO/26-27/00011. Do not reopen.") in po["close_remark"]
    # vehicle_type_id stays NULL: the lorry master is neither read nor written
    assert not [s for s in db.statements[start:] if "jute_lorry_mst" in s]


def test_forwarding_po_lines_and_hop_links(scenario, po_on):
    sc, db = scenario, scenario.db
    # X's crop year = the first non-NULL crop year of its ACTIVE lines (31267: 26)
    db.update("jute_po_li", {"jute_po_li_id": sc.po_lines[0]}, crop_year=None)
    db.insert("jute_po_li", jute_po_li_id=31265, jute_po_id=sc.po, item_id=sc.items[0],
              crop_year=24, active=0)
    hop = sc.add_hop()
    b_item = sc.items_in(sc.b_co)
    first, second = sc.active_lines(hop)
    template = db.row("jute_mr_li", jute_mr_li_id=first)
    template.pop("jute_mr_li_id")
    zero = db.insert("jute_mr_li", **{**template, "accepted_weight": 0, "total_price": 0})
    dead = db.insert("jute_mr_li", **{**template, "active": 0})
    marka = "LOT-7/" + "M" * 54                           # 60 chars; jute_po_li.marka is 50
    odd = db.insert("jute_mr_li", **{
        **template, "active": None, "actual_item_id": None,
        "challan_item_id": b_item[sc.items[1]], "accepted_weight": 700, "rate": 12925,
        "marka": marka, "crop_year": 25, "allowable_moisture": 18.5})

    out = in_txn(po_ops.create_forward_po, hop, sc.user)

    lines = lines_of(db, out["po_id"])
    ids = [l["jute_po_li_id"] for l in lines]
    common = dict(jute_po_id=out["po_id"], jute_uom="BALE")
    assert lines == [
        # 9312 kg -> 62 bales = 9300 kg; 93 q x 12,850
        po_line(db, jute_po_li_id=ids[0], item_id=b_item[sc.items[0]], quantity=62.0,
                rate=12850.0, value=Decimal("1195050.00"), crop_year=26,
                allowable_moisture=20.0, **common),
        # 1441 kg -> 10 bales = 1500 kg; 15 q x 12,650
        po_line(db, jute_po_li_id=ids[1], item_id=b_item[sc.items[1]], quantity=10.0,
                rate=12650.0, value=Decimal("189750.00"), crop_year=26,
                allowable_moisture=20.0, **common),
        # active NULL counts as active; challan item when no actual item; own crop
        # year; 700 kg -> 5 bales = 750 kg; 12,925 -> 12,950 (tie goes up)
        po_line(db, jute_po_li_id=ids[2], item_id=b_item[sc.items[1]], quantity=5.0,
                rate=12950.0, value=Decimal("97125.00"), marka=marka[:50], crop_year=25,
                allowable_moisture=18.5, **common),
    ]
    po = db.row("jute_po", jute_po_id=out["po_id"])
    assert (po["weight"], po["jute_po_value"]) == (11550.0, Decimal("1481925.00"))
    # the hop and every USED line point at the PO; zero-weight / inactive lines do not
    assert db.row("jute_mr", jute_mr_id=hop)["po_id"] == out["po_id"]
    assert {l["jute_mr_li_id"]: l["jute_po_li_id"] for l in db.rows("jute_mr_li", jute_mr_id=hop)
            } == {first: ids[0], second: ids[1], zero: None, dead: None, odd: ids[2]}


@pytest.mark.parametrize("role", [ROLE_FORWARD, ROLE_FINAL])
def test_status_log_row(scenario, po_on, role):
    sc, db = scenario, scenario.db
    hop = sc.add_hop()
    if role == ROLE_FINAL:
        sc.finalize_root()
    mr = hop if role == ROLE_FORWARD else sc.root
    create = po_ops.create_forward_po if role == ROLE_FORWARD else po_ops.create_final_po

    out = in_txn(create, mr, 7)

    po = db.row("jute_po", jute_po_id=out["po_id"])
    log = db.row("jute_po_status_log")
    assert log == {"log_id": log["log_id"], "jute_po_id": out["po_id"], "old_status_id": 3,
                   "new_status_id": 5, "close_type": CLOSE_TYPE_TRANSFER, "source": "BACKEND",
                   "trigger_ref": f"juteTransfer {role} MR:{mr}", "remark": po["close_remark"],
                   "user_id": None, "created_at": db.now}


@pytest.mark.parametrize("mr_uom, x_uom, uom, quantities, weight, value", [
    ("BALE", "BALE", "BALE", [62.0, 10.0], 10800.0, "1384800.00"),
    ("LOOSE", "LOOSE", "LOOSE", [194.0, 30.0], 10752.0, "1378752.00"),   # 48 kg units
    (None, "BALE", "BALE", [62.0, 10.0], 10800.0, "1384800.00"),         # MR has none: X's
    ("KG", "LOOSE", "LOOSE", [194.0, 30.0], 10752.0, "1378752.00"),      # not BALE/LOOSE: X's
    (None, None, "LOOSE", [194.0, 30.0], 10752.0, "1378752.00"),         # neither: LOOSE
])
def test_unit_quantity_weight_and_value(fake_db, po_on, mr_uom, x_uom, uom, quantities,
                                        weight, value):
    sc = build_scenario(fake_db, uom=x_uom or "BALE")
    if x_uom is None:
        fake_db.update("jute_po", {"jute_po_id": sc.po}, jute_uom=None)
    hop = sc.add_hop(unit_conversion=mr_uom)

    out = in_txn(po_ops.create_forward_po, hop, sc.user)

    po = fake_db.row("jute_po", jute_po_id=out["po_id"])
    lines = lines_of(fake_db, out["po_id"])
    assert (po["jute_uom"], po["weight"], po["jute_po_value"]) == (uom, weight, Decimal(value))
    assert [(l["quantity"], l["jute_uom"]) for l in lines] == [(q, uom) for q in quantities]
    assert sum(l["value"] for l in lines) == Decimal(value)


def test_po_number_per_branch_and_financial_year(scenario, po_on):
    sc, db = scenario, scenario.db
    for branch, no, day in ((sc.b_branch, 40, date(2026, 3, 31)),      # FY 25-26, last day
                            (sc.b_branch, 7, date(2027, 3, 31)),       # FY 26-27, last day
                            (sc.b_branch, 3, date(2027, 4, 1)),        # FY 27-28, first day
                            (sc.c_branch, 500, date(2026, 9, 1))):     # another branch
        db.insert("jute_po", branch_id=branch, po_no=no, po_date=day)
    made = []
    for ge_no, day in ((31, date(2026, 4, 1)), (32, date(2027, 3, 31)),
                       (33, date(2027, 4, 1)), (34, date(2026, 3, 31))):
        hop = sc.add_hop(mr_date=day, root=sc.add_root(ge_no=ge_no, ge_date=day))
        made.append(in_txn(po_ops.create_forward_po, hop, sc.user))

    assert [(m["po_no"], m["po_no_formatted"]) for m in made] == [
        (8, "JTSPL/JPO/26-27/00008"),      # 01-04-2026: FY 26-27, after 7
        (9, "JTSPL/JPO/26-27/00009"),      # 31-03-2027: same FY, after the PO just made
        (4, "JTSPL/JPO/27-28/00004"),      # 01-04-2027: FY 27-28, after 3
        (41, "JTSPL/JPO/25-26/00041"),     # 31-03-2026: FY 25-26, after 40
    ]
    assert [db.row("jute_po", jute_po_id=m["po_id"])["po_no"] for m in made] == [8, 9, 4, 41]


def test_second_po_in_the_same_transaction_gets_the_next_number(scenario, po_on):
    sc = scenario
    one, two = sc.add_hop(), sc.add_hop(root=sc.add_root())
    with DatabaseConnection.get_transaction() as conn:
        first = po_ops.create_forward_po(conn, one, sc.user)
        second = po_ops.create_forward_po(conn, two, sc.user)
    assert (first["po_no"], second["po_no"]) == (1, 2)


def test_po_date_falls_back_to_the_gate_entry_date_then_today(scenario, po_on):
    sc, db = scenario, scenario.db
    by_gate_entry = sc.add_hop(mr_date=date(2026, 9, 5), jute_mr_date=None)
    undated = sc.add_hop(root=sc.add_root(), jute_mr_date=None, jute_gate_entry_date=None)

    first = in_txn(po_ops.create_forward_po, by_gate_entry, sc.user)
    before = date.today()
    second = in_txn(po_ops.create_forward_po, undated, sc.user)
    after = date.today()

    assert db.row("jute_po", jute_po_id=first["po_id"])["po_date"] == date(2026, 9, 5)
    assert db.row("jute_po", jute_po_id=second["po_id"])["po_date"] in (before, after)


def test_credit_terms_only_on_the_first_hop(scenario, po_on):
    sc, db = scenario, scenario.db
    first = sc.add_hop(at="B")                                 # received from the mill's company
    b_in_c, b_in_c_branch = sc.party_in(sc.c_co, sc.b_name)
    later = sc.add_hop(at="C", mr_date=date(2026, 9, 2), src_co=sc.b_co,
                       party_id=str(b_in_c), party_branch_id=b_in_c_branch)

    def terms(out):
        po = db.row("jute_po", jute_po_id=out["po_id"])
        return po["credit_term"], po["delivery_days"]

    assert terms(in_txn(po_ops.create_forward_po, first, sc.user)) == (45, 7)
    later_out = in_txn(po_ops.create_forward_po, later, sc.user)
    assert terms(later_out) == (None, None)                    # a sister company sold it
    later_po = db.row("jute_po", jute_po_id=later_out["po_id"])
    assert (later_po["branch_id"], later_po["party_id"], later_po["po_no"]) == (
        sc.c_branch, b_in_c, 1)
    assert later_out["po_no_formatted"] == "GMPL/KOL/JPO/26-27/00001"
    sc.finalize_root(forwarder="C")
    assert terms(in_txn(po_ops.create_final_po, sc.root, sc.user)) == (None, None)


@pytest.mark.parametrize("channel, expected", [("IMPORT", "IMPORT"), (None, "DOMESTIC")])
def test_channel_code_is_the_original_pos(scenario, po_on, channel, expected):
    sc, db = scenario, scenario.db
    db.update("jute_po", {"jute_po_id": sc.po}, channel_code=channel)
    hop = sc.add_hop()
    forward = in_txn(po_ops.create_forward_po, hop, sc.user)
    sc.finalize_root()
    final = in_txn(po_ops.create_final_po, sc.root, sc.user)
    assert [db.row("jute_po", jute_po_id=o["po_id"])["channel_code"]
            for o in (forward, final)] == [expected, expected]


@pytest.mark.parametrize("root_po", [None, 999999], ids=["no po_id", "po row missing"])
def test_root_without_an_erp_po_still_gets_transfer_pos(scenario, po_on, root_po):
    sc, db = scenario, scenario.db
    db.update("jute_mr", {"jute_mr_id": sc.root}, po_id=root_po)
    hop = sc.add_hop()
    db.update("jute_mr_li", {"jute_mr_li_id": sc.active_lines(hop)[0]}, crop_year=25)

    out = in_txn(po_ops.create_forward_po, hop, sc.user)

    po = db.row("jute_po", jute_po_id=out["po_id"])
    assert (po["channel_code"], po["credit_term"], po["delivery_days"]) == ("DOMESTIC", None, None)
    assert po["internal_note"] == build_marker(ROLE_FORWARD, sc.root, hop, None)
    assert po["close_remark"].endswith("Original PO none. Do not reopen.")
    assert [l["crop_year"] for l in lines_of(db, out["po_id"])] == [25, None]

    sc.finalize_root()
    final = db.row("jute_po", jute_po_id=in_txn(po_ops.create_final_po, sc.root,
                                                 sc.user)["po_id"])
    assert (final["channel_code"], final["credit_term"]) == ("DOMESTIC", None)
    assert final["internal_note"] == build_marker(ROLE_FINAL, sc.root, sc.root, None)
    assert "received on original PO none, so this PO shows no receipt" in final["close_remark"]


# --- supplier / party ---------------------------------------------------------------

@pytest.mark.parametrize("existing", ["none", "supplier mapped to another party", "triple"])
def test_forwarding_company_gets_the_exact_supplier_party_map_row(scenario, po_on, existing):
    sc, db = scenario, scenario.db
    party, _ = sc.party_in(sc.b_co, sc.supplier_party_name)
    other = sc.add_party(sc.b_co, "SOME OTHER JUTE TRADER")
    if existing != "none":
        db.insert("jute_supp_party_map", co_id=sc.b_co, jute_supplier_id=sc.supplier,
                  party_id=other if existing.startswith("supplier") else party, updated_by=24)
    hop = sc.add_hop(mapped=False)
    before = db.rows("jute_supp_party_map")

    in_txn(po_ops.create_forward_po, hop, 7)

    after = db.rows("jute_supp_party_map")
    assert after[:len(before)] == before                       # nothing changed or removed
    added = [(r["co_id"], r["jute_supplier_id"], r["party_id"], r["updated_by"],
              r["updated_date_time"]) for r in after[len(before):]]
    assert added == ([] if existing == "triple" else [(sc.b_co, sc.supplier, party, 7, db.now)])


@pytest.mark.parametrize("party", [None, "", "ABC", "207A"])
def test_forwarding_po_without_a_numeric_party(scenario, po_on, party):
    sc, db = scenario, scenario.db
    hop = sc.add_hop(party_id=party)
    maps = db.rows("jute_supp_party_map")
    out = in_txn(po_ops.create_forward_po, hop, sc.user)
    po = db.row("jute_po", jute_po_id=out["po_id"])
    assert (po["party_id"], po["supplier_id"]) == (None, sc.supplier)
    assert db.rows("jute_supp_party_map") == maps


def test_forwarding_po_without_a_supplier_adds_no_map_row(scenario, po_on):
    sc, db = scenario, scenario.db
    hop = sc.add_hop(mapped=False, jute_supplier_id=None)
    party = int(db.row("jute_mr", jute_mr_id=hop)["party_id"])
    maps = db.rows("jute_supp_party_map")
    out = in_txn(po_ops.create_forward_po, hop, sc.user)
    po = db.row("jute_po", jute_po_id=out["po_id"])
    assert (po["party_id"], po["supplier_id"]) == (party, None)
    assert db.rows("jute_supp_party_map") == maps


def test_final_po_without_a_numeric_party(scenario, po_on):
    sc, db = scenario, scenario.db
    sc.add_hop()
    sc.finalize_root()
    db.update("jute_mr", {"jute_mr_id": sc.root}, party_id="ABC")
    out = in_txn(po_ops.create_final_po, sc.root, sc.user)
    po = db.row("jute_po", jute_po_id=out["po_id"])
    assert (po["party_id"], po["supplier_id"]) == (None, sc.supplier)
    assert "came back from the forwarding company." in po["close_remark"]


@pytest.mark.parametrize("maps, root_supplier, expected", [
    ([], 2430, 2430),                              # party unmapped at A: the MR's own supplier
    ([(9001, 2397)], 2430, 2397),                  # mapped under 'others' only (live forwarders)
    ([(9001, 2397), (9002, 2430)], 2430, 2430),    # the MR's own supplier is preferred
    ([(9002, 2500), (9001, 2397)], 2430, 2397),    # else the lowest map_id
    ([(9000, None), (9003, 2500)], 2430, 2500),    # a map row without a supplier is ignored
    ([], None, None),
])
def test_final_po_supplier_is_looked_up_never_mapped_at_the_origin(scenario, po_on, maps,
                                                                  root_supplier, expected):
    sc, db = scenario, scenario.db
    sc.add_hop()
    forwarder = sc.finalize_root()
    db.update("jute_mr", {"jute_mr_id": sc.root}, jute_supplier_id=root_supplier)
    # a row of ANOTHER company for the same party id must not count
    db.insert("jute_supp_party_map", map_id=8000, co_id=sc.b_co, jute_supplier_id=2600,
              party_id=forwarder, updated_by=24)
    for map_id, supplier in maps:
        db.insert("jute_supp_party_map", map_id=map_id, co_id=sc.a_co, jute_supplier_id=supplier,
                  party_id=forwarder, updated_by=24)
    before = db.rows("jute_supp_party_map")

    out = in_txn(po_ops.create_final_po, sc.root, sc.user)

    assert db.row("jute_po", jute_po_id=out["po_id"])["supplier_id"] == expected
    assert db.rows("jute_supp_party_map") == before


# --- Final PO ----------------------------------------------------------------------

def test_final_po_header_and_lines(scenario, po_on):
    sc, db = scenario, scenario.db
    sc.add_hop()
    forwarder = sc.finalize_root()          # B as a party of A, rates 12914 / 12713, MR date 03-09
    x_before = (db.row("jute_po", jute_po_id=sc.po), lines_of(db, sc.po))
    root_lines = db.rows("jute_mr_li", jute_mr_id=sc.root)
    maps = db.rows("jute_supp_party_map")
    original = {"party_id": "207", "party_branch_id": 3811, "jute_mr_date": date(2026, 8, 30)}

    out = in_txn(po_ops.create_final_po, sc.root, 7, original=original)

    assert summary(out) == {"role": ROLE_FINAL, "po_no": 12,
                            "po_no_formatted": "EJM/F/JPO/26-27/00012", "skipped": None}
    po = db.row("jute_po", jute_po_id=out["po_id"])
    assert po == header(            # vehicle_type_id, credit_term, delivery_days stay NULL
        db, jute_po_id=out["po_id"], branch_id=sc.a_branch, po_no=12, po_date=date(2026, 9, 3),
        supplier_id=sc.supplier, party_id=forwarder, jute_mukam_id=sc.mukam, jute_uom="BALE",
        vehicle_quantity=1, weight=10800.0, jute_po_value=Decimal("1390200.00"),
        channel_code="DOMESTIC", status_id=5, close_type=CLOSE_TYPE_TRANSFER,
        remarks=po_remarks(ROLE_FINAL), closed_date=db.now, closed_by=None,
        close_remark=final_close_remark(sc.ge_no, sc.ge_date, FORWARDER_IN_A, ORIGINAL_PO),
        internal_note=build_marker(ROLE_FINAL, sc.root, sc.root, sc.po, original),
        updated_by=7, updated_date_time=db.now)
    assert ("when lorry GE 21 dt 31-08-2026 came back from JAGRATI TRADE SERVICES PVT. LTD. "
            "The lorry was received on original PO EJM/F/JPO/26-27/00011, so this PO shows "
            "no receipt. Do not reopen.") in po["close_remark"]
    common = dict(jute_po_id=out["po_id"], jute_uom="BALE", crop_year=26, allowable_moisture=20.0)
    ids = [l["jute_po_li_id"] for l in lines_of(db, out["po_id"])]
    assert lines_of(db, out["po_id"]) == [     # final rates 12,914 / 12,713 -> 12,900 / 12,700
        po_line(db, jute_po_li_id=ids[0], item_id=sc.items[0], quantity=62.0, rate=12900.0,
                value=Decimal("1199700.00"), **common),
        po_line(db, jute_po_li_id=ids[1], item_id=sc.items[1], quantity=10.0, rate=12700.0,
                value=Decimal("190500.00"), **common),
    ]
    # nothing links to it: the root keeps X, its lines keep X's lines
    assert db.row("jute_mr", jute_mr_id=sc.root)["po_id"] == sc.po
    assert db.rows("jute_mr_li", jute_mr_id=sc.root) == root_lines
    assert db.count("jute_mr", po_id=out["po_id"]) == 0
    assert (db.row("jute_po", jute_po_id=sc.po), lines_of(db, sc.po)) == x_before
    assert db.rows("jute_supp_party_map") == maps          # lookup only at the origin


@pytest.mark.parametrize("original, remembered", [
    ({"party_id": "207", "party_branch_id": 3811, "jute_mr_date": date(2026, 8, 30)}, "same"),
    ({"party_id": "207", "party_branch_id": 3811, "jute_mr_date": None}, "same"),
    ({"party_id": None, "party_branch_id": None, "jute_mr_date": None}, "same"),
    ({"party_id": 207.0, "party_branch_id": 3811.0, "jute_mr_date": datetime(2026, 8, 30)},
     {"party_id": "207", "party_branch_id": 3811, "jute_mr_date": date(2026, 8, 30)}),
    ({"party_id": "LCPL-9", "party_branch_id": 5, "jute_mr_date": date(2026, 8, 30)}, None),
    (None, None),
], ids=["all", "no date", "all NULL", "pandas types", "party not a number", "not given"])
def test_final_po_original_reads_back_what_finalize_remembered(scenario, po_on, original,
                                                               remembered):
    sc = scenario
    sc.add_hop()
    sc.finalize_root()
    assert in_txn(po_ops.final_po_original, sc.root) is None            # no Final PO yet
    in_txn(po_ops.create_final_po, sc.root, sc.user, original=original)
    expected = original if remembered == "same" else remembered
    assert in_txn(po_ops.final_po_original, sc.root) == expected


# --- skips, idempotency, refusals ---------------------------------------------------------

def test_no_po_for_an_mr_without_weighted_lines(scenario, po_on):
    sc, db = scenario, scenario.db
    hop = sc.add_hop()
    first, second = sc.active_lines(hop)
    db.update("jute_mr_li", {"jute_mr_li_id": first}, accepted_weight=0)
    db.update("jute_mr_li", {"jute_mr_li_id": second}, active=0)
    sc.finalize_root()
    for line in sc.active_lines(sc.root):
        db.update("jute_mr_li", {"jute_mr_li_id": line}, accepted_weight=0)
    before = db.snapshot()

    forward = in_txn(po_ops.create_forward_po, hop, sc.user)
    final = in_txn(po_ops.create_final_po, sc.root, sc.user)

    for out in (forward, final):
        assert (out["po_id"], out["po_no"]) == (None, None)
        assert "has no line with accepted weight" in out["skipped"]
    assert db.diff(before, db.snapshot()) == []


def test_creating_twice_is_a_no_op(scenario, po_on):
    sc, db = scenario, scenario.db
    hop = sc.add_hop()
    forward = in_txn(po_ops.create_forward_po, hop, sc.user)
    sc.finalize_root()
    final = in_txn(po_ops.create_final_po, sc.root, sc.user)
    before = db.snapshot()

    again_forward = in_txn(po_ops.create_forward_po, hop, sc.user)
    again_final = in_txn(po_ops.create_final_po, sc.root, sc.user)

    assert again_forward["po_id"] is None
    assert f"already has PO {forward['po_id']}" in again_forward["skipped"]
    assert again_final["po_id"] is None
    assert f"already has final PO {final['po_id']}" in again_final["skipped"]
    assert db.diff(before, db.snapshot()) == []


def test_a_hop_linked_to_an_erp_po_by_hand_gets_no_forwarding_po(scenario, po_on):
    sc, db = scenario, scenario.db
    hop = sc.add_hop(po_id=sc.po)
    before = db.snapshot()
    out = in_txn(po_ops.create_forward_po, hop, sc.user)
    assert out["po_id"] is None and f"already has PO {sc.po}" in out["skipped"]
    assert db.diff(before, db.snapshot()) == []


def test_plans_refuse_an_mr_of_the_wrong_kind(scenario, po_on):
    sc = scenario
    hop = sc.add_hop()
    marked = sc.add_hop(transfer_mode=1)                   # a marked-godown child
    for mr in (sc.root, marked):
        with pytest.raises(ValueError, match="is not a vertical-chain hop"):
            in_txn(po_ops.plan_forward_po, mr)
    with pytest.raises(ValueError, match="is not a chain root"):
        in_txn(po_ops.plan_final_po, hop)
    with pytest.raises(ValueError, match="not found"):
        in_txn(po_ops.plan_forward_po, 999999)


def test_po_value_beyond_decimal_10_2_raises_and_writes_nothing(scenario, po_on):
    sc, db = scenario, scenario.db
    hop = sc.add_hop()
    first, second = sc.active_lines(hop)
    db.update("jute_mr_li", {"jute_mr_li_id": second}, active=0)
    db.update("jute_mr_li", {"jute_mr_li_id": first}, rate=1075300)    # 93 q -> 100,002,900.00
    before = db.snapshot()
    with pytest.raises(ValueError, match="exceeds what jute_po.jute_po_value can hold"):
        in_txn(po_ops.create_forward_po, hop, sc.user)
    assert db.diff(before, db.snapshot()) == []

    db.update("jute_mr_li", {"jute_mr_li_id": first}, rate=1075250)    # 99,998,250.00 fits
    out = in_txn(po_ops.create_forward_po, hop, sc.user)
    assert db.row("jute_po", jute_po_id=out["po_id"])["jute_po_value"] == Decimal("99998250.00")
    assert [l["value"] for l in lines_of(db, out["po_id"])] == [Decimal("99998250.00")]


def test_plans_write_nothing(scenario, po_on):
    sc, db = scenario, scenario.db
    hop = sc.add_hop(mapped=False)                         # its plan needs a map row
    party = int(db.row("jute_mr", jute_mr_id=hop)["party_id"])
    sc.finalize_root()
    before, start = db.snapshot(), len(db.statements)

    forward = in_txn(po_ops.plan_forward_po, hop)
    final = in_txn(po_ops.plan_final_po, sc.root)

    assert db.writes(start) == []
    assert db.diff(before, db.snapshot()) == []
    assert forward["skipped"] is None and final["skipped"] is None
    assert forward["map_row"] == {"co_id": sc.b_co, "supplier_id": sc.supplier, "party_id": party}
    assert final["map_row"] is None
    assert [(l["rate"], l["quantity"]) for l in forward["lines"]] == [(12850.0, 62.0),
                                                                       (12650.0, 10.0)]
    assert (forward["weight"], forward["value"]) == (10800.0, 1384800.0)
    assert [l["rate"] for l in final["lines"]] == [12900.0, 12700.0]
    assert (final["branch_id"], final["po_date"]) == (sc.a_branch, date(2026, 9, 3))


def test_switch_off_creates_nothing_but_deletes_still_work(scenario, monkeypatch):
    sc, db = scenario, scenario.db
    monkeypatch.setenv("JT_TRANSFER_PO", "1")
    hop = sc.add_hop()
    made = in_txn(po_ops.create_forward_po, hop, sc.user)
    monkeypatch.setenv("JT_TRANSFER_PO", "0")
    assert po_ops.transfer_po_enabled() is False
    later = sc.add_hop(root=sc.add_root())
    sc.finalize_root()
    before = db.snapshot()

    for out in (in_txn(po_ops.create_forward_po, later, sc.user),
                in_txn(po_ops.create_final_po, sc.root, sc.user)):
        assert out["po_id"] is None and "switched off" in out["skipped"]
    assert db.diff(before, db.snapshot()) == []

    assert in_txn(po_ops.delete_forward_po, hop) == {
        "po_id": made["po_id"], "po_no_formatted": made["po_no_formatted"]}
    assert db.count("jute_po", jute_po_id=made["po_id"]) == 0


# --- deletes ----------------------------------------------------------------------------

def test_delete_forward_po_removes_it_and_unlinks_the_hop(scenario, po_on):
    sc, db = scenario, scenario.db
    hop = sc.add_hop()
    other = sc.add_hop(root=sc.add_root())
    made = in_txn(po_ops.create_forward_po, hop, sc.user)
    kept = in_txn(po_ops.create_forward_po, other, sc.user)
    untouched = {(t, po): db.rows(t, jute_po_id=po)
                 for t in ("jute_po", "jute_po_li", "jute_po_status_log")
                 for po in (kept["po_id"], sc.po)}

    out = in_txn(po_ops.delete_forward_po, hop)

    assert out == {"po_id": made["po_id"], "po_no_formatted": "JTSPL/JPO/26-27/00001"}
    for table in ("jute_po", "jute_po_li", "jute_po_status_log"):
        assert db.count(table, jute_po_id=made["po_id"]) == 0
    assert db.row("jute_mr", jute_mr_id=hop)["po_id"] is None
    assert [l["jute_po_li_id"] for l in db.rows("jute_mr_li", jute_mr_id=hop)] == [None, None]
    assert {key: db.rows(key[0], jute_po_id=key[1]) for key in untouched} == untouched
    assert db.row("jute_mr", jute_mr_id=other)["po_id"] == kept["po_id"]


@pytest.mark.parametrize("link", ["none", "dangling", "ERP PO", "another hop's PO", "Final PO"])
def test_delete_forward_po_leaves_a_po_that_is_not_this_hops(scenario, po_on, link):
    sc, db = scenario, scenario.db
    hop = sc.add_hop()
    other = sc.add_hop(root=sc.add_root())
    other_po = in_txn(po_ops.create_forward_po, other, sc.user)["po_id"]
    sc.finalize_root()
    final_po = in_txn(po_ops.create_final_po, sc.root, sc.user)["po_id"]
    target = {"none": None, "dangling": 999999, "ERP PO": sc.po,
              "another hop's PO": other_po, "Final PO": final_po}[link]
    db.update("jute_mr", {"jute_mr_id": hop}, po_id=target)
    before, start = db.snapshot(), len(db.statements)

    assert in_txn(po_ops.delete_forward_po, hop) is None

    assert db.writes(start) == []
    assert db.diff(before, db.snapshot()) == []


def test_delete_forward_po_refuses_while_an_erp_mr_uses_it(scenario, po_on):
    sc, db = scenario, scenario.db
    hop = sc.add_hop()
    po_id = in_txn(po_ops.create_forward_po, hop, sc.user)["po_id"]
    foreign = db.insert("jute_mr", branch_id=sc.b_branch, po_id=po_id, status_id=3,
                        jute_gate_entry_no=90, transfer_mode=0)
    before, start = db.snapshot(), len(db.statements)

    with pytest.raises(ValueError, match=rf"Transfer PO {po_id} has other MR\(s\) linked to it "
                                         rf"in the ERP \({foreign}\); unlink them there first"):
        in_txn(po_ops.delete_forward_po, hop)

    assert db.writes(start) == []
    assert db.diff(before, db.snapshot()) == []


def test_delete_final_po_removes_every_final_po_of_the_root_and_nothing_else(scenario, po_on):
    sc, db = scenario, scenario.db
    hop = sc.add_hop()
    forward = in_txn(po_ops.create_forward_po, hop, sc.user)["po_id"]
    sc.finalize_root()
    final = in_txn(po_ops.create_final_po, sc.root, sc.user)["po_id"]
    day = date(2026, 9, 4)
    stray = db.insert("jute_po", branch_id=sc.a_branch, po_no=50, po_date=day,
                      internal_note=build_marker(ROLE_FINAL, sc.root, sc.root, sc.po))
    db.insert("jute_po_li", jute_po_id=stray, item_id=sc.items[0], quantity=1)
    lookalikes = [db.insert("jute_po", branch_id=branch, po_no=60 + i, po_date=day,
                            internal_note=note) for i, (branch, note) in enumerate((
                    (sc.a_branch, build_marker(ROLE_FINAL, 2813, 2813, sc.po)),       # id prefix
                    (sc.a_branch, build_marker(ROLE_FINAL, 281370, 281370, sc.po)),   # id extends
                    (sc.a_branch, build_marker(ROLE_FORWARD, sc.root, hop, sc.po)),   # FORWARD
                    (sc.b_branch, build_marker(ROLE_FINAL, sc.root, sc.root, sc.po))))]  # branch
    forward_rows = (db.rows("jute_po_li", jute_po_id=forward),
                    db.rows("jute_po_status_log", jute_po_id=forward))

    out = in_txn(po_ops.delete_final_po, sc.root)

    assert out == [{"po_id": final, "po_no_formatted": "EJM/F/JPO/26-27/00012"},
                   {"po_id": stray, "po_no_formatted": "EJM/F/JPO/26-27/00050"}]
    for table in ("jute_po", "jute_po_li", "jute_po_status_log"):
        assert db.count(table, jute_po_id=final) == db.count(table, jute_po_id=stray) == 0
    assert {r["jute_po_id"] for r in db.rows("jute_po")} == {sc.po, forward, *lookalikes}
    assert (db.rows("jute_po_li", jute_po_id=forward),
            db.rows("jute_po_status_log", jute_po_id=forward)) == forward_rows
    assert in_txn(po_ops.delete_final_po, sc.root) == []        # nothing left to remove


def test_delete_final_po_refuses_while_an_erp_mr_uses_it(scenario, po_on):
    sc, db = scenario, scenario.db
    sc.add_hop()
    sc.finalize_root()
    final = in_txn(po_ops.create_final_po, sc.root, sc.user)["po_id"]
    foreign = db.insert("jute_mr", branch_id=sc.a_branch, po_id=final, status_id=1,
                        jute_gate_entry_no=95, transfer_mode=0)
    before, start = db.snapshot(), len(db.statements)

    with pytest.raises(ValueError, match=rf"Transfer PO {final} has other MR\(s\) linked to it "
                                         rf"in the ERP \({foreign}\); unlink them there first"):
        in_txn(po_ops.delete_final_po, sc.root)

    assert db.writes(start) == []
    assert db.diff(before, db.snapshot()) == []
