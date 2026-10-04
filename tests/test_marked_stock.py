"""Marked warehouse stock (Type 2) end to end through warehouse_stock_ops.py
and lot_ops.py -- saved and undone the way pages/warehouse_stock.py does it --
on the fake sls database with the ERP's stock ledger vw_jute_stock_outstanding
in place (tests/fake_mysql.py).

Scenario: lorry GE 21 at the mill A (Empire, branch 29) approved as MR 1 of
30-08-2026 with two lines; B (Jagrati) and C (Greeting) are the companies the
stock is marked to. The defect these tests pin down (audit-02 V1, live on MR
28253): a marked move used to drain the source line AND book the seller
invoice, so the ERP saw the jute leave the seller twice and the purchase MR
was left at total 0 / net -claim. The move must represent the outflow once --
as the invoice line that names the source MR line -- and never touch the
purchase."""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest

from src.jutetransfer import lot_ops
from src.jutetransfer import warehouse_stock_ops as ops
from src.jutetransfer.database import DatabaseConnection
from src.jutetransfer.queries import get_available_lots, get_marked_stock_with_balance
from src.jutetransfer.transfer import _erp_jute_totals

from .fake_mysql import fake_db, scenario  # noqa: F401  (fixtures)

MR_DATE = date(2026, 8, 30)
MOVE_DATE = date(2026, 9, 5)
AUDIT = {"jute_mr": ["updated_by", "updated_date_time"], "jute_mr_li": ["updated_date_time"]}
INVOICE_TABLES = ("sales_invoice", "sales_invoice_dtl", "sales_invoice_jute",
                  "sales_invoice_jute_dtl")
MASTER_TABLES = ("party_mst", "party_branch_mst", "item_grp_mst", "item_mst",
                 "jute_supp_party_map")
# what the ERP's stock reports leave out (reportQueries._MR_STOCK_ELIGIBLE / the view)
NOT_STOCK = "(4, 6, 21, 48)"


# --- the scenario as the ERP leaves an approved purchase -------------------------------------------

def approve(sc, mr_date: date = MR_DATE) -> dict:
    """The root as the ERP's approve leaves it: Approved (3), numbered and
    dated, header money by the ERP's own rule (calculate_mr_amounts +
    compute_jute_totals, TDS 0), MR weight = the lines' accepted kg."""
    db = sc.db
    lines = [l for l in db.rows("jute_mr_li", jute_mr_id=sc.root) if l["active"] in (1, None)]
    total = round(sum(float(l["accepted_weight"]) * float(l["rate"]) / 100 for l in lines), 2)
    claim = round(sum(float(l["accepted_weight"]) * float(l["claim_rate"] or 0) / 100
                      for l in lines), 2)
    roundoff, net = _erp_jute_totals(total, claim, 0.0)
    money = dict(total_amount=total, claim_amount=claim, tds_amount=0.0, roundoff=roundoff,
                 net_total=net, mr_weight=sum(float(l["accepted_weight"]) for l in lines))
    db.update("jute_mr", {"jute_mr_id": sc.root}, status_id=3, branch_mr_no=1,
              jute_mr_date=mr_date, bill_pass_no=1, bill_pass_date=mr_date, **money)
    return money


def without_shortage(sc) -> None:
    """Both lines weighed exactly what was accepted (no moisture deduction),
    so receipt, balance and sale are one figure."""
    db = sc.db
    for line_id in sc.root_lines:
        line = db.row("jute_mr_li", jute_mr_li_id=line_id)
        db.update("jute_mr_li", {"jute_mr_li_id": line_id}, actual_weight=line["accepted_weight"],
                  shortage_kgs=0)
    db.update("jute_mr", {"jute_mr_id": sc.root}, actual_weight=10753, net_weight=10753)


def move(sc, line_ids, to: str = "B", pct: float = 0.0, mr_date: date = MOVE_DATE) -> list:
    """The Transfer tab's save: whole lots to a marked godown of company `to`."""
    return ops.save_marked_batch(list(line_ids), pct, sc.co(to), sc.branch(to), sc.godown(to),
                                 mr_date, sc.user)


def child_lines(db, result: dict) -> list:
    return db.rows("jute_mr_li", jute_mr_id=result["child_mr_id"])


def invoice_of(db, result: dict) -> tuple:
    """(sales_invoice row, its sales_invoice_dtl rows) of one marked child."""
    (link,) = db.rows("sales_invoice_jute", mr_id=result["child_mr_id"])
    return (db.row("sales_invoice", invoice_id=link["invoice_id"]),
            db.rows("sales_invoice_dtl", invoice_id=link["invoice_id"]))


def balance(db, li_id: int):
    """bal_weight of one line in the ERP stock ledger (None: not stock)."""
    rows = db.execute("SELECT bal_weight FROM vw_jute_stock_outstanding WHERE jute_mr_li_id = :id",
                      id=li_id)
    return rows[0]["bal_weight"] if rows else None


def bales(db, li_id: int):
    rows = db.execute("SELECT bal_qty FROM vw_jute_stock_outstanding WHERE jute_mr_li_id = :id",
                      id=li_id)
    return round(rows[0]["bal_qty"], 3) if rows else None


def erp_closing(db, branch_id: int, basis: str = "actual") -> float:
    """The ERP jute stock report's all-time closing for a branch
    (reportQueries.get_jute_stock_report_query): receipts on stock-eligible MR
    lines, minus issues, minus every approved raw-jute invoice line of the
    branch -- linked to an MR line or not."""
    weight = ("COALESCE(li.actual_weight, 0)" if basis == "actual"
              else "COALESCE(li.accepted_weight, li.actual_weight, 0)")
    eligible = (f"jm.status_id NOT IN {NOT_STOCK} AND li.active = 1 "
                "AND (li.status IS NULL OR li.status NOT IN ('4', '6'))")
    receipt = db.execute(f"""
        SELECT COALESCE(SUM({weight}), 0) AS wt FROM jute_mr jm
        JOIN jute_mr_li li ON li.jute_mr_id = jm.jute_mr_id
        WHERE jm.branch_id = :b AND {eligible}""", b=branch_id)[0]["wt"]
    issued = db.execute(f"""
        SELECT COALESCE(SUM(ji.weight), 0) AS wt FROM jute_issue ji
        JOIN jute_mr_li li ON li.jute_mr_li_id = ji.jute_mr_li_id
        JOIN jute_mr jm ON jm.jute_mr_id = li.jute_mr_id
        WHERE ji.branch_id = :b AND ji.status_id <> 4 AND {eligible}""", b=branch_id)[0]["wt"]
    sold = db.execute("""
        SELECT COALESCE(SUM(COALESCE(NULLIF(sid.sales_weight, 0), sid.quantity)), 0) AS wt
        FROM sales_invoice sinv JOIN sales_invoice_dtl sid ON sid.invoice_id = sinv.invoice_id
        WHERE sinv.branch_id = :b AND sinv.invoice_type = 5 AND sinv.status_id = 3
          AND COALESCE(sinv.active, 1) = 1""", b=branch_id)[0]["wt"]
    return round(float(receipt) - float(issued) - float(sold), 3)


def money(row: dict) -> dict:
    return {k: row[k] for k in ("total_amount", "claim_amount", "tds_amount", "roundoff",
                                "net_total", "mr_weight")}


def mr_state(db, mr_id: int) -> tuple:
    return (db.rows("jute_mr", jute_mr_id=mr_id), db.rows("jute_mr_li", jute_mr_id=mr_id))


def lots_at(sc, which: str, include_marked: bool = False, month: date = MR_DATE) -> dict:
    """{line id: remaining kg} as the Transfer tab lists it for a company, for
    the gate-entry month (a marked child's gate entry is its move date)."""
    df = get_available_lots(sc.co(which), sc.branch(which), month.year, month.month,
                            include_marked=include_marked)
    return {int(r.jute_mr_li_id): float(r.remaining_kg) for r in df.itertuples()}


# --- 1. the defect: stock must leave the seller once ----------------------------------------------

def test_a_marked_move_takes_the_stock_out_of_the_seller_once(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    purchase = mr_state(db, sc.root)
    assert erp_closing(db, sc.a_branch) == 10753.0

    (result,) = move(sc, [sc.root_lines[0]])

    # the purchase MR -- a real supplier delivery -- is not touched: its lines,
    # money (bill pass, TDS, purchase register) and MR weight stay
    assert mr_state(db, sc.root) == purchase
    # the ERP stock ledger: the moved kg left the source LINE and sit on the child
    (line,) = child_lines(db, result)
    assert (line["accepted_weight"], line["actual_weight"], line["actual_qty"]) == (9312.0, 9312.0, 66.0)
    assert balance(db, sc.root_lines[0]) == 0.0 and bales(db, sc.root_lines[0]) == 0.0
    assert balance(db, line["jute_mr_li_id"]) == 9312.0
    assert balance(db, sc.root_lines[1]) == 1441.0            # the other line is still A's
    # ... because the seller's invoice line names the source MR line, which is
    # how the ERP books a raw-jute sale out of MR stock
    invoice, dtl = invoice_of(db, result)
    assert (invoice["branch_id"], invoice["invoice_type"], invoice["status_id"]) == (sc.a_branch, 5, 3)
    assert [(d["jute_mr_li_id"], d["quantity"], d["sales_weight"]) for d in dtl] == [
        (sc.root_lines[0], 9312.0, 9312.0)]
    # the ERP's stock report: A closes at the unsold line, B at the moved kg;
    # nothing left the group twice (the old code closed A at -7,871)
    assert (erp_closing(db, sc.a_branch), erp_closing(db, sc.a_branch, "accounts")) == (1441.0, 1441.0)
    assert erp_closing(db, sc.b_branch) == 9312.0
    assert erp_closing(db, sc.a_branch) + erp_closing(db, sc.b_branch) == 10753.0


def test_the_moved_line_is_no_longer_offered_and_the_other_still_is(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    assert lots_at(sc, "A") == {sc.root_lines[0]: 9312.0, sc.root_lines[1]: 1441.0}

    (result,) = move(sc, [sc.root_lines[0]])

    assert lots_at(sc, "A") == {sc.root_lines[1]: 1441.0}
    (line,) = child_lines(db, result)
    assert lots_at(sc, "B", include_marked=True, month=MOVE_DATE) == {line["jute_mr_li_id"]: 9312.0}
    marked = get_marked_stock_with_balance(sc.b_co, sc.b_branch, MOVE_DATE.year, MOVE_DATE.month)
    assert [(int(r.jute_mr_li_id), float(r.balance_kg), bool(r.consumed)) for r in marked.itertuples()] == [
        (line["jute_mr_li_id"], 9312.0, False)]
    # a second save of the same selection (stale tab) finds nothing to move
    with pytest.raises(ValueError, match="no available weight"):
        move(sc, [sc.root_lines[0]])


def test_two_lines_of_one_mr_make_one_child_and_one_invoice_naming_both_lines(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    purchase = mr_state(db, sc.root)

    (result,) = move(sc, sc.root_lines, pct=0.5)

    assert mr_state(db, sc.root) == purchase
    lines = child_lines(db, result)
    # post-claim rate x 1.005, whole rupee: (12850 - 140) x 1.005 = 12,773.55 -> 12,774;
    # (12650 - 50) x 1.005 = 12,663 exactly
    assert [(l["accepted_weight"], l["rate"], l["claim_rate"], l["actual_rate"], l["total_price"])
            for l in lines] == [(9312.0, 12774.0, 0.0, 12850.0, Decimal("1189514.88")),
                                (1441.0, 12663.0, 0.0, 12650.0, Decimal("182473.83"))]
    invoice, dtl = invoice_of(db, result)
    assert [(d["jute_mr_li_id"], d["quantity"], d["rate"], d["amount_without_tax"]) for d in dtl] == [
        (sc.root_lines[0], 9312.0, 127.74, 1189514.88), (sc.root_lines[1], 1441.0, 126.63, 182473.83)]
    assert [balance(db, li) for li in sc.root_lines] == [0.0, 0.0]
    assert (erp_closing(db, sc.a_branch), erp_closing(db, sc.b_branch)) == (0.0, 10753.0)
    # the child's header is what the ERP's own approve would write for its
    # lines: total to the paisa, claim 0, TDS 0, net a whole rupee
    child = db.row("jute_mr", jute_mr_id=result["child_mr_id"])
    assert money(child) == {"total_amount": 1371988.71, "claim_amount": 0.0, "tds_amount": 0.0,
                            "roundoff": 0.29, "net_total": 1371989.0, "mr_weight": 10753.0}
    assert (child["invoice_no"], child["invoice_amount"], invoice["invoice_amount"]) == (
        "EJM/F/SI/26-27/1", 1371989.0, 1371989.0)
    assert (child["status_id"], child["transfer_mode"], child["src_jute_mr_id"], child["branch_id"]) == (
        3, 1, sc.root, sc.b_branch)
    # provenance: one row per line, nothing taken off the source rows
    prov = db.rows("jute_lot_src")
    assert [(p["new_jute_mr_li_id"], p["src_jute_mr_li_id"], p["qty_kg"], p["actual_qty_delta"],
             p["actual_weight_delta"]) for p in prov] == [
        (lines[0]["jute_mr_li_id"], sc.root_lines[0], Decimal("9312.000"), None, None),
        (lines[1]["jute_mr_li_id"], sc.root_lines[1], Decimal("1441.000"), None, None)]


def test_a_line_partly_issued_in_the_erp_moves_only_its_balance(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    db.insert("jute_issue", jute_mr_li_id=sc.root_lines[0], issue_date=date(2026, 9, 1),
              status_id=3, weight=500, quantity=4, branch_id=sc.a_branch)
    assert lots_at(sc, "A") == {sc.root_lines[0]: 8812.0, sc.root_lines[1]: 1441.0}

    (result,) = move(sc, [sc.root_lines[0]])

    (line,) = child_lines(db, result)
    assert (line["accepted_weight"], line["actual_weight"]) == (8812.0, 8812.0)
    assert balance(db, sc.root_lines[0]) == 0.0 and balance(db, line["jute_mr_li_id"]) == 8812.0
    assert (erp_closing(db, sc.a_branch), erp_closing(db, sc.b_branch)) == (1441.0, 8812.0)


def test_a_moisture_shortage_stays_on_the_sellers_line(scenario):
    """The scenario's lines weighed 9,700 / 1,470 kg but were accepted at
    9,312 / 1,441: a whole-lot move sells the ACCEPTED kg (what the mill paid
    for), and the view keeps the deducted kg on the seller's line, as the
    ERP keeps them on any gate entry with a moisture claim. Nothing is lost
    or doubled across the group (the old code closed A at -8,924)."""
    sc, db = scenario, scenario.db
    approve(sc)
    purchase = mr_state(db, sc.root)

    (result,) = move(sc, [sc.root_lines[0]])

    assert mr_state(db, sc.root) == purchase
    (line,) = child_lines(db, result)
    assert (line["accepted_weight"], line["actual_weight"], line["actual_qty"]) == (9312.0, 9312.0, 63.36)
    assert balance(db, sc.root_lines[0]) == 388.0 and bales(db, sc.root_lines[0]) == 2.64
    assert (erp_closing(db, sc.a_branch), erp_closing(db, sc.b_branch)) == (1858.0, 9312.0)
    assert erp_closing(db, sc.a_branch, "accounts") == 1441.0
    assert erp_closing(db, sc.a_branch) + erp_closing(db, sc.b_branch) == 9700.0 + 1470.0


def test_sold_share_follows_the_stock_views_pro_rata():
    from src.jutetransfer.lot_helpers import sold_share
    assert sold_share(66, 9700, 9700) == 66.0
    assert sold_share(66, 9700, 9312) == 63.36
    assert sold_share(None, 9700, 9312) == 0.0 and sold_share(66, 0, 100) == 0.0
    assert sold_share(Decimal("43.251"), 10000, 10000) == 43.251


# --- 2. resale ------------------------------------------------------------------------------

def resold(sc) -> tuple:
    """A -> B, then B -> C: (child at B, grandchild at C)."""
    (child,) = move(sc, [sc.root_lines[0]])
    (b_line,) = child_lines(sc.db, child)
    (grandchild,) = move(sc, [b_line["jute_mr_li_id"]], to="C", pct=1.0,
                         mr_date=date(2026, 9, 10))
    return child, grandchild


def test_resale_books_the_holders_sale_against_its_own_line_and_drains_nothing(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)

    child, grandchild = resold(sc)

    (b_line,) = child_lines(db, child)
    (c_line,) = child_lines(db, grandchild)
    # the holder's child line and header keep what B bought
    assert (b_line["accepted_weight"], b_line["actual_weight"], b_line["actual_qty"]) == (9312.0, 9312.0, 66.0)
    assert money(db.row("jute_mr", jute_mr_id=child["child_mr_id"]))["net_total"] == 1183555.0
    # the resale invoice is B's, names B's line, and the stock sits at C now
    invoice, dtl = invoice_of(db, grandchild)
    assert (invoice["branch_id"], [(d["jute_mr_li_id"], d["quantity"]) for d in dtl]) == (
        sc.b_branch, [(b_line["jute_mr_li_id"], 9312.0)])
    assert (balance(db, sc.root_lines[0]), balance(db, b_line["jute_mr_li_id"]),
            balance(db, c_line["jute_mr_li_id"])) == (0.0, 0.0, 9312.0)
    assert [erp_closing(db, b) for b in (sc.a_branch, sc.b_branch, sc.c_branch)] == [1441.0, 0.0, 9312.0]
    assert [erp_closing(db, b, "accounts") for b in (sc.a_branch, sc.b_branch, sc.c_branch)] == [
        1441.0, 0.0, 9312.0]
    # B's marked stock reads consumed and resold; C's is live
    at_b = get_marked_stock_with_balance(sc.b_co, sc.b_branch, MOVE_DATE.year, MOVE_DATE.month)
    assert [(float(r.balance_kg), bool(r.consumed), bool(r.resold)) for r in at_b.itertuples()] == [
        (0.0, True, True)]
    assert lots_at(sc, "B", include_marked=True, month=MOVE_DATE) == {}
    assert lots_at(sc, "C", include_marked=True, month=date(2026, 9, 10)) == {c_line["jute_mr_li_id"]: 9312.0}
    grand = db.row("jute_mr", jute_mr_id=grandchild["child_mr_id"])
    assert (grand["src_jute_mr_id"], grand["src_com_id"], grand["transfer_mode"]) == (
        child["child_mr_id"], sc.b_co, 1)


# --- 3. undo ------------------------------------------------------------------------------

def test_undo_deletes_the_invoice_and_the_child_and_writes_nothing_else(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    others = [t for t in db.schema.columns if t not in MASTER_TABLES]
    before = db.snapshot(others, ignore=AUDIT)
    (result,) = move(sc, sc.root_lines)
    assert (db.count("sales_invoice"), db.count("jute_lot_src"), db.count("jute_mr")) == (1, 2, 2)
    since = len(db.statements)

    ops.delete_marked_move(result["child_mr_id"], sc.user)

    assert db.diff(before, db.snapshot(others, ignore=AUDIT)) == []
    assert erp_closing(db, sc.a_branch) == 10753.0
    assert lots_at(sc, "A") == {sc.root_lines[0]: 9312.0, sc.root_lines[1]: 1441.0}
    # the purchase MR and its lines were not even written
    writes = [s for s in db.writes(since) if "jute_mr " in s + " " or s.startswith("UPDATE jute_mr_li")]
    assert not [w for w in writes if w.startswith("UPDATE")]


def test_resale_is_undone_leaf_first(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    before = db.snapshot(["jute_mr", "jute_mr_li"], ignore=AUDIT)
    child, grandchild = resold(sc)
    (b_line,) = child_lines(db, child)
    middle = db.snapshot(["jute_mr", "jute_mr_li"], ignore=AUDIT)

    with pytest.raises(ValueError, match="Delete dependent marked moves first"):
        ops.delete_marked_move(child["child_mr_id"], sc.user)
    assert db.diff(middle, db.snapshot(["jute_mr", "jute_mr_li"], ignore=AUDIT)) == []

    ops.delete_marked_move(grandchild["child_mr_id"], sc.user)
    assert balance(db, b_line["jute_mr_li_id"]) == 9312.0          # B holds the stock again
    assert db.count("sales_invoice") == 1
    ops.delete_marked_move(child["child_mr_id"], sc.user)
    assert db.diff(before, db.snapshot(["jute_mr", "jute_mr_li"], ignore=AUDIT)) == []
    assert {t: db.count(t) for t in INVOICE_TABLES + ("jute_lot_src",)} == dict.fromkeys(
        INVOICE_TABLES + ("jute_lot_src",), 0)


@pytest.mark.parametrize("blocker", ["an ERP issue entry", "an ERP sale", "no provenance"])
def test_an_undeletable_child_leaves_every_table_unchanged(scenario, blocker):
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    (result,) = move(sc, [sc.root_lines[0]])
    (line,) = child_lines(db, result)
    if blocker == "an ERP issue entry":
        db.insert("jute_issue", jute_mr_li_id=line["jute_mr_li_id"], issue_date=date(2026, 9, 8),
                  status_id=None, weight=150, branch_id=sc.b_branch)
        error = "has ERP issue entries"
    elif blocker == "an ERP sale":
        inv = db.insert("sales_invoice", invoice_no=7, invoice_date=date(2026, 9, 8), invoice_type=5,
                        branch_id=sc.b_branch, status_id=3, active=1, invoice_amount=150000,
                        round_off=0, updated_by=24)
        db.insert("sales_invoice_dtl", invoice_id=inv, item_id=sc.items[0], quantity=1000,
                  sales_weight=1000, rate=150, amount_without_tax=150000,
                  jute_mr_li_id=line["jute_mr_li_id"])
        error = "sold in the ERP"
    else:
        db.delete("jute_lot_src", new_jute_mr_li_id=line["jute_mr_li_id"])
        error = "no line provenance"
    before = db.snapshot()

    with pytest.raises(ValueError, match=error):
        ops.delete_marked_move(result["child_mr_id"], sc.user)

    assert db.diff(before, db.snapshot()) == []


def test_undo_after_the_erp_cancelled_the_invoice_restores_nothing_twice(scenario):
    """The ERP user cancels the seller invoice (status 4): the view already
    gives the balance back to A. The undo must only remove the cancelled
    invoice and the child -- the provenance says nothing was taken off the
    source row, so nothing is added back."""
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    purchase = mr_state(db, sc.root)
    (result,) = move(sc, [sc.root_lines[0]])
    invoice, _ = invoice_of(db, result)
    db.update("sales_invoice", {"invoice_id": invoice["invoice_id"]}, status_id=4)
    assert balance(db, sc.root_lines[0]) == 9312.0

    ops.delete_marked_move(result["child_mr_id"], sc.user)

    assert mr_state(db, sc.root) == purchase
    assert db.count("sales_invoice") == 0 and db.count("jute_mr") == 1


# --- 4. moves written by the old code (drained source) ------------------------------------------

def drained_the_old_way(sc, result: dict) -> None:
    """Turn a move into what the code before 2026-10-03 wrote: the source
    line emptied (accepted / actual weight, bales, price), its header
    re-written at total 0 / net -claim / weight 0 -- live sls MR 28253 -- the
    provenance carrying the deltas and the invoice line naming no MR line."""
    db = sc.db
    (line,) = child_lines(db, result)
    (prov,) = db.rows("jute_lot_src", new_jute_mr_li_id=line["jute_mr_li_id"])
    src = db.row("jute_mr_li", jute_mr_li_id=prov["src_jute_mr_li_id"])
    db.update("jute_lot_src", {"lot_src_id": prov["lot_src_id"]},
              actual_qty_delta=src["actual_qty"], actual_weight_delta=src["actual_weight"])
    db.update("jute_mr_li", {"jute_mr_li_id": src["jute_mr_li_id"]}, accepted_weight=0,
              actual_weight=0, actual_qty=0, total_price=0)
    invoice, dtl = invoice_of(db, result)
    for d in dtl:
        db.update("sales_invoice_dtl", {"invoice_line_item_id": d["invoice_line_item_id"]},
                  jute_mr_li_id=None)
    left = [l for l in db.rows("jute_mr_li", jute_mr_id=src["jute_mr_id"])
            if l["active"] in (1, None) and l["jute_mr_li_id"] != src["jute_mr_li_id"]]
    total = float(sum(Decimal(str(l["total_price"])) for l in left)
                  .quantize(Decimal("1"), rounding=ROUND_HALF_UP))      # SQL ROUND(SUM(..), 0)
    header = db.row("jute_mr", jute_mr_id=src["jute_mr_id"])
    db.update("jute_mr", {"jute_mr_id": src["jute_mr_id"]}, total_amount=total, roundoff=0,
              net_total=total - float(header["claim_amount"]),
              mr_weight=sum(float(l["accepted_weight"]) for l in left))


def test_undo_of_a_move_written_by_the_old_code_restores_the_source_the_erp_way(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    purchase = mr_state(db, sc.root)
    (result,) = move(sc, [sc.root_lines[0]])
    drained_the_old_way(sc, result)
    root = db.row("jute_mr", jute_mr_id=sc.root)
    assert (root["total_amount"], root["net_total"], root["mr_weight"]) == (182287.0, 168529.7, 1441.0)
    assert erp_closing(db, sc.a_branch) == -7871.0                     # the live defect

    ops.delete_marked_move(result["child_mr_id"], sc.user)

    purchase_header, purchase_lines = purchase
    restored = {k: v for k, v in db.row("jute_mr_li", jute_mr_li_id=sc.root_lines[0]).items()
                if k != "updated_date_time"}
    original = next(l for l in purchase_lines if l["jute_mr_li_id"] == sc.root_lines[0])
    assert restored == {k: v for k, v in original.items() if k != "updated_date_time"}
    assert money(db.row("jute_mr", jute_mr_id=sc.root)) == money(purchase_header[0])
    assert erp_closing(db, sc.a_branch) == 10753.0 and db.count("jute_mr") == 1


def test_undo_of_an_old_resale_restores_only_the_holders_actual_fields(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    child, grandchild = resold(sc)
    (b_line,) = child_lines(db, child)
    held = {k: v for k, v in b_line.items() if k != "updated_date_time"}
    # the old resale drained B's actual fields only (keep_accepted)
    (prov,) = db.rows("jute_lot_src", src_jute_mr_li_id=b_line["jute_mr_li_id"])
    db.update("jute_lot_src", {"lot_src_id": prov["lot_src_id"]},
              actual_qty_delta=66, actual_weight_delta=9312)
    db.update("jute_mr_li", {"jute_mr_li_id": b_line["jute_mr_li_id"]}, actual_weight=0, actual_qty=0)
    _, dtl = invoice_of(db, grandchild)
    db.update("sales_invoice_dtl", {"invoice_line_item_id": dtl[0]["invoice_line_item_id"]},
              jute_mr_li_id=None)
    assert erp_closing(db, sc.b_branch) == -9312.0                     # live branch 99

    ops.delete_marked_move(grandchild["child_mr_id"], sc.user)

    assert {k: v for k, v in db.row("jute_mr_li", jute_mr_li_id=b_line["jute_mr_li_id"]).items()
            if k != "updated_date_time"} == held
    assert balance(db, b_line["jute_mr_li_id"]) == 9312.0 and erp_closing(db, sc.b_branch) == 9312.0


# --- 5. lines that are not stock ---------------------------------------------------------------

def test_a_soft_deleted_line_cannot_be_moved_or_re_lotted(scenario):
    """The ERP QC edit flips active to 0 and leaves the weights on the row
    (sls MR 28304: 16,520 kg on an inactive line). The stock view does not
    list it, so neither may the app move it or split it."""
    sc, db = scenario, scenario.db
    approve(sc)
    db.update("jute_mr_li", {"jute_mr_li_id": sc.root_dead_line}, accepted_weight=9900,
              actual_weight=9900, rate=12850, total_price=1272150)
    assert balance(db, sc.root_dead_line) is None
    # queries.get_available_lots does not offer it either (it used to, through
    # COALESCE(v.bal_weight, li.accepted_weight) with no active filter --
    # audit-02 V3); the backend stays the guard.
    assert sc.root_dead_line not in lots_at(sc, "A")
    assert set(lots_at(sc, "A")) == set(sc.root_lines)
    before = db.snapshot()
    with pytest.raises(ValueError, match="soft-deleted"):
        move(sc, [sc.root_dead_line])
    with pytest.raises(ValueError, match="soft-deleted"):
        lot_ops.create_lot([(sc.root_dead_line, 100)], sc.user)
    assert db.diff(before, db.snapshot()) == []


def test_available_kg_is_zero_for_a_line_the_stock_view_does_not_list(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    with DatabaseConnection.get_transaction() as conn:
        assert ops._available_kg(conn, sc.root_lines[0], 9312.0) == 9312.0
        assert ops._available_kg(conn, sc.root_dead_line, 9900.0) == 0.0
        assert ops._available_kg(conn, 999999, 9900.0) == 0.0
    db.update("jute_mr", {"jute_mr_id": sc.root}, status_id=48)        # Returned: not stock
    with DatabaseConnection.get_transaction() as conn:
        assert ops._available_kg(conn, sc.root_lines[0], 9312.0) == 0.0


# --- 6. lots and moves together ------------------------------------------------------------------

def test_a_split_lot_moved_whole_and_undone_in_order_leaves_the_purchase_as_it_was(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    before = db.snapshot(["jute_mr", "jute_mr_li"], ignore=AUDIT)

    (lot,) = lot_ops.create_lot([(sc.root_lines[0], 3000)], sc.user)

    src = db.row("jute_mr_li", jute_mr_li_id=sc.root_lines[0])
    new = db.row("jute_mr_li", jute_mr_li_id=lot)
    assert (src["accepted_weight"], src["actual_weight"], new["accepted_weight"], new["jute_mr_id"]) == (
        6312.0, 6312.0, 3000.0, sc.root)
    assert erp_closing(db, sc.a_branch) == 10753.0
    # the in-place split leaves the ERP's money and weight where they were
    assert money(db.row("jute_mr", jute_mr_id=sc.root)) == money(before["jute_mr"][0])
    assert lots_at(sc, "A") == {sc.root_lines[0]: 6312.0, sc.root_lines[1]: 1441.0, lot: 3000.0}

    (result,) = move(sc, [lot])

    assert money(db.row("jute_mr", jute_mr_id=sc.root)) == money(before["jute_mr"][0])
    assert balance(db, lot) == 0.0 and erp_closing(db, sc.a_branch) == 7753.0
    with pytest.raises(ValueError, match="feeds a newer lot or marked move"):
        lot_ops.delete_lot_line(lot, sc.user)

    ops.delete_marked_move(result["child_mr_id"], sc.user)
    lot_ops.delete_lot_line(lot, sc.user)

    assert db.diff(before, db.snapshot(["jute_mr", "jute_mr_li"], ignore=AUDIT)) == []
    assert db.count("jute_lot_src") == 0


def test_a_lot_line_sold_in_the_erp_cannot_be_undone(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    (lot,) = lot_ops.create_lot([(sc.root_lines[0], 3000)], sc.user)
    inv = db.insert("sales_invoice", invoice_no=7, invoice_date=date(2026, 9, 8), invoice_type=5,
                    branch_id=sc.a_branch, status_id=3, active=1, invoice_amount=380000,
                    round_off=0, updated_by=24)
    db.insert("sales_invoice_dtl", invoice_id=inv, item_id=sc.items[0], quantity=3000,
              sales_weight=3000, rate=127, amount_without_tax=381000, jute_mr_li_id=lot)
    before = db.snapshot()
    with pytest.raises(ValueError, match="sales invoice lines against it"):
        lot_ops.delete_lot_line(lot, sc.user)
    assert db.diff(before, db.snapshot()) == []


def test_header_recompute_follows_the_erp_and_ignores_soft_deleted_lines(scenario):
    """mr.recompute_mr_money: active lines, 2-dp total, claim from the lines,
    stored TDS kept, whole-rupee net; sync_mr_weight_from_lines for the weight."""
    sc, db = scenario, scenario.db
    approve(sc)
    db.update("jute_mr_li", {"jute_mr_li_id": sc.root_dead_line}, accepted_weight=16520,
              actual_weight=16520, rate=12850, total_price=2122820)      # ERP QC left-over
    db.update("jute_mr", {"jute_mr_id": sc.root}, tds_amount=1800.0, claim_amount=0,
              net_total=0, total_amount=0, mr_weight=0)
    with DatabaseConnection.get_transaction() as conn:
        got = ops._recompute_mr_header(conn, sc.root, sc.user)
    root = db.row("jute_mr", jute_mr_id=sc.root)
    assert got == {"total_amount": 1378878.5, "claim_amount": 13757.3, "tds_amount": 1800.0,
                   "roundoff": -0.2, "net_total": 1363321.0}
    assert money(root) == {**got, "mr_weight": 10753.0}
    assert (root["updated_by"], root["updated_date_time"]) == (sc.user, db.now)


# --- 7. the repair of moves written by the old code (scripts/repair_transfer_data.py) ----------------

def test_marked_stock_repair_plans_the_source_the_links_and_the_headers(scenario, capsys):
    from scripts import repair_transfer_data as repair
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    purchase_line = db.row("jute_mr_li", jute_mr_li_id=sc.root_lines[0])
    purchase_money = money(db.row("jute_mr", jute_mr_id=sc.root))
    (result,) = move(sc, [sc.root_lines[0]])
    drained_the_old_way(sc, result)
    (line,) = child_lines(db, result)
    (prov,) = db.rows("jute_lot_src")
    _, dtl = invoice_of(db, result)
    since = len(db.statements)

    assert repair.main(["--only", "marked-stock"]) == 0

    assert db.writes(since) == []
    out = capsys.readouterr().out
    assert "1 marked child MR(s)" in out and f"child MR {result['child_mr_id']}" in out
    assert f"jute_mr_li {sc.root_lines[0]}: accepted_weight 0.00 -> 9,312.00" in out
    assert f"jute_mr {sc.root}: total_amount 182,287.00 -> 1,378,878.50" in out
    assert f"sales_invoice_dtl {dtl[0]['invoice_line_item_id']}: jute_mr_li_id NULL -> {sc.root_lines[0]}" in out
    assert "--apply --only marked-stock --expect 1" in out

    with DatabaseConnection.get_engine().connect() as conn:
        (plan,) = repair.plan_marked_stock(conn)
    assert plan["jute_mr_id"] == result["child_mr_id"]
    assert plan["sources"] == {str(sc.root): [sc.root_lines[0]]}
    assert plan["new"][f"jute_mr_li:{sc.root_lines[0]}"] == {
        "accepted_weight": 9312.0, "actual_weight": 9312.0, "actual_qty": 66.0,
        "total_price": float(purchase_line["total_price"])}
    assert plan["new"][f"jute_mr:{sc.root}"] == purchase_money
    assert plan["new"][f"sales_invoice_dtl:{dtl[0]['invoice_line_item_id']}"] == {
        "jute_mr_li_id": sc.root_lines[0]}
    assert plan["new"][f"jute_lot_src:{prov['lot_src_id']}"] == {
        "actual_qty_delta": None, "actual_weight_delta": None}
    assert f"jute_mr:{result['child_mr_id']}" not in plan["new"]        # the child's header is right


def test_marked_stock_repair_through_main_then_the_undo_works_the_linked_way(scenario, tmp_path):
    from scripts import repair_transfer_data as repair
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    others = [t for t in db.schema.columns if t not in MASTER_TABLES]
    pristine = db.snapshot(others, ignore=AUDIT)
    (result,) = move(sc, [sc.root_lines[0]])
    linked = db.snapshot(others, ignore=AUDIT)                         # what the code writes today
    drained_the_old_way(sc, result)
    assert erp_closing(db, sc.a_branch) == -7871.0
    args = ["--apply", "--only", "marked-stock", "--log-dir", str(tmp_path)]
    assert repair.main(args + ["--expect", "2"]) == 2                   # not what the dry run shows

    assert repair.main(args + ["--expect", "1"]) == 0

    # the converted rows are exactly what save_marked_batch writes today
    assert db.diff(linked, db.snapshot(others, ignore=AUDIT)) == []
    assert erp_closing(db, sc.a_branch) == 1441.0 and balance(db, sc.root_lines[0]) == 0.0
    (log,) = tmp_path.glob("jt_repair_*.json")
    import json
    logged = json.loads(log.read_text())
    assert [r["jute_mr_id"] for r in logged["repaired"]] == [result["child_mr_id"]]
    assert logged["repaired"][0]["old"][f"jute_mr_li:{sc.root_lines[0]}"]["accepted_weight"] == 0.0
    assert repair.main(args + ["--expect", "1"]) == 2                   # nothing left to repair

    ops.delete_marked_move(result["child_mr_id"], sc.user)              # no restore arithmetic now
    assert db.diff(pristine, db.snapshot(others, ignore=AUDIT)) == []


def test_marked_stock_repair_refuses_a_row_that_changed_since_the_dry_run(scenario, tmp_path, monkeypatch):
    from scripts import repair_transfer_data as repair
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    (result,) = move(sc, [sc.root_lines[0]])
    drained_the_old_way(sc, result)
    real = repair.REPAIR_FNS["marked-stock"]

    def erp_edits_the_line_first(conn, it):
        db.update("jute_mr_li", {"jute_mr_li_id": sc.root_lines[0]}, accepted_weight=5)
        return real(conn, it)

    monkeypatch.setitem(repair.REPAIR_FNS, "marked-stock", erp_edits_the_line_first)
    before = db.snapshot(["jute_mr", "jute_mr_li", "sales_invoice_dtl", "jute_lot_src"])

    assert repair.main(["--apply", "--only", "marked-stock", "--expect", "1",
                        "--log-dir", str(tmp_path)]) == 1

    # nothing of the plan was written (the stand-in's single connection rolls
    # the ERP edit back with the refused transaction, so the line reads 0 again)
    assert db.diff(before, db.snapshot(["jute_mr", "jute_mr_li", "sales_invoice_dtl",
                                        "jute_lot_src"])) == []
    import json
    (log,) = tmp_path.glob("jt_repair_*.json")
    (failed,) = json.loads(log.read_text())["failed"]
    assert failed["jute_mr_id"] == result["child_mr_id"] and "changed since the dry run" in failed["error"]


def test_marked_stock_repair_also_fixes_a_resale_and_the_childrens_paise(scenario, capsys):
    """Live shape: 28253 -> 28259 -> 28260. The resale drained the holder's
    actual fields; the old header recompute wrote the children's total to the
    rupee where the ERP keeps paise."""
    from scripts import repair_transfer_data as repair
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    child, grandchild = resold(sc)
    (b_line,) = child_lines(db, child)
    held = {k: v for k, v in b_line.items() if k != "updated_date_time"}
    drained_the_old_way(sc, child)
    (prov,) = db.rows("jute_lot_src", src_jute_mr_li_id=b_line["jute_mr_li_id"])
    db.update("jute_lot_src", {"lot_src_id": prov["lot_src_id"]}, actual_qty_delta=66,
              actual_weight_delta=9312)
    db.update("jute_mr_li", {"jute_mr_li_id": b_line["jute_mr_li_id"]}, actual_weight=0, actual_qty=0)
    _, dtl = invoice_of(db, grandchild)
    db.update("sales_invoice_dtl", {"invoice_line_item_id": dtl[0]["invoice_line_item_id"]},
              jute_mr_li_id=None)
    # (12850 - 140) x 1.01 = 12,837.10 -> 12,837; 9,312 kg -> 1,195,381.44, written to the rupee
    db.update("jute_mr", {"jute_mr_id": grandchild["child_mr_id"]}, total_amount=1195381.0)

    with DatabaseConnection.get_engine().connect() as conn:
        plans = repair.plan_marked_stock(conn)

    assert [p["jute_mr_id"] for p in plans] == [child["child_mr_id"], grandchild["child_mr_id"]]
    resale = plans[1]
    assert resale["new"][f"jute_mr_li:{b_line['jute_mr_li_id']}"] == {
        "accepted_weight": 9312.0, "actual_weight": 9312.0, "actual_qty": 66.0,
        "total_price": float(held["total_price"])}
    assert f"jute_mr:{child['child_mr_id']}" not in resale["new"]       # the holder's header was right
    assert resale["new"][f"jute_mr:{grandchild['child_mr_id']}"]["total_amount"] == 1195381.44
    assert resale["new"][f"sales_invoice_dtl:{dtl[0]['invoice_line_item_id']}"] == {
        "jute_mr_li_id": b_line["jute_mr_li_id"]}


def test_marked_stock_repair_plans_a_header_two_children_share_once(scenario, tmp_path):
    """Live shape 28253 -> 28259 -> 28260: the holder 28259 is the first
    move's child and the resale's source. Its header must be planned once,
    from both restores, or the second row fails its changed-since check."""
    from scripts import repair_transfer_data as repair
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    child, grandchild = resold(sc)
    (b_line,) = child_lines(db, child)
    drained_the_old_way(sc, child)
    (prov,) = db.rows("jute_lot_src", src_jute_mr_li_id=b_line["jute_mr_li_id"])
    db.update("jute_lot_src", {"lot_src_id": prov["lot_src_id"]}, actual_qty_delta=66,
              actual_weight_delta=9312)
    db.update("jute_mr_li", {"jute_mr_li_id": b_line["jute_mr_li_id"]}, actual_weight=0, actual_qty=0)
    _, dtl = invoice_of(db, grandchild)
    db.update("sales_invoice_dtl", {"invoice_line_item_id": dtl[0]["invoice_line_item_id"]},
              jute_mr_li_id=None)
    db.update("jute_mr", {"jute_mr_id": child["child_mr_id"]}, total_amount=1183555.0)     # was .20

    with DatabaseConnection.get_engine().connect() as conn:
        first, second = repair.plan_marked_stock(conn)
    assert first["new"][f"jute_mr:{child['child_mr_id']}"]["total_amount"] == 1183555.2
    assert f"jute_mr:{child['child_mr_id']}" not in second["new"]

    assert repair.main(["--apply", "--only", "marked-stock", "--expect", "2",
                        "--log-dir", str(tmp_path)]) == 0
    assert db.row("jute_mr", jute_mr_id=child["child_mr_id"])["total_amount"] == 1183555.2
    assert [balance(db, li) for li in (sc.root_lines[0], b_line["jute_mr_li_id"])] == [0.0, 0.0]
    assert [erp_closing(db, b) for b in (sc.a_branch, sc.b_branch, sc.c_branch)] == [1441.0, 0.0, 9312.0]
