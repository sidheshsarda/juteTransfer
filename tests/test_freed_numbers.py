"""Freed-number slots (owner ruling 2026-11-17): a number freed in the middle
of a series is taken back by the next save whose date fits."""

from datetime import date

import pytest

from src.jutetransfer import transfer

from .fake_mysql import fake_db, scenario  # noqa: F401  (fixtures)
from .test_marked_stock import approve, move
from .test_transfer_po_flow import Chain, po_on  # noqa: F401  (po_on is a fixture)

D = date


def hop(db, mr_id):
    mr = db.row("jute_mr", jute_mr_id=mr_id)
    return mr["branch_mr_no"], mr["jute_gate_entry_no"], mr["bill_pass_no"]


def three_lorries_at_b(sc):
    """MR nos 1, 2, 3 at B on 5, 6 and 7 September."""
    chains = [Chain(sc), Chain(sc, root=sc.add_root(ge_no=8)), Chain(sc, root=sc.add_root(ge_no=9))]
    ids = [c.save("B", mr_date=D(2026, 9, 5 + i))["mr_id"] for i, c in enumerate(chains)]
    return chains, ids


def test_the_corrected_lorry_gets_its_old_numbers_back(scenario, po_on):
    sc, db = scenario, scenario.db
    chains, ids = three_lorries_at_b(sc)
    assert [hop(db, i)[0] for i in ids] == [1, 2, 3]
    chains[0].delete_from(1)                                  # MR 1 was wrong
    (slot_series) = sorted(r["series"] for r in db.rows("jt_freed_no"))
    assert slot_series == ["billpass", "gate", "mr"]
    again = chains[0].save("B", mr_date=D(2026, 9, 5))["mr_id"]
    assert hop(db, again) == (1, 1, 1)                        # not 4
    assert all(r["closed_note"] == "reused" for r in db.rows("jt_freed_no"))


def test_a_date_that_does_not_fit_takes_the_next_number(scenario, po_on):
    sc, db = scenario, scenario.db
    chains, _ = three_lorries_at_b(sc)
    chains[0].delete_from(1)
    later = chains[0].save("B", mr_date=D(2026, 9, 8))["mr_id"]     # after MR 3
    assert hop(db, later)[0] == 4
    assert [r["closed_note"] for r in db.rows("jt_freed_no", series="mr")] == [None]   # still open


def test_the_freed_number_lets_the_old_date_through_the_date_rule(scenario, po_on):
    sc, db = scenario, scenario.db
    chains, _ = three_lorries_at_b(sc)
    chains[1].delete_from(1)                                  # MR 2 (6 Sep) freed
    # 6 Sep is before B's latest MR (7 Sep), but number 2 is waiting between 5 and 7 Sep
    again = chains[1].save("B", mr_date=D(2026, 9, 6))["mr_id"]
    assert hop(db, again)[0] == 2
    with pytest.raises(transfer.MrDateOrderError):            # no slot left: the rule is back
        Chain(sc, root=sc.add_root(ge_no=10)).save("B", mr_date=D(2026, 9, 6))


def test_a_number_freed_at_the_top_needs_no_slot(scenario, po_on):
    sc, db = scenario, scenario.db
    chains, _ = three_lorries_at_b(sc)
    chains[2].delete_from(1)                                  # MR 3, the top
    assert db.rows("jt_freed_no") == []
    assert hop(db, chains[2].save("B", mr_date=D(2026, 9, 7))["mr_id"])[0] == 3


def test_the_invoice_number_comes_back_too(scenario, po_on):
    sc, db = scenario, scenario.db

    def invoice_no(seller_mr_id):
        (link,) = db.rows("sales_invoice_jute", mr_id=seller_mr_id)
        return db.row("sales_invoice", invoice_id=link["invoice_id"])["invoice_no"]

    chains, ids = three_lorries_at_b(sc)
    for c in chains:                                          # B sells each on to C, same days
        c.save("C", pct=0.5, mr_date=db.row("jute_mr", jute_mr_id=c.steps[0]["mr_id"])["jute_mr_date"])
    assert [invoice_no(i) for i in ids] == [1, 2, 3]
    chains[0].delete_from(2)                                  # the sale B -> C of lorry 1
    chains[0].save("C", pct=0.25, mr_date=D(2026, 9, 5))
    assert invoice_no(ids[0]) == 1


def test_un_finalize_and_finalize_again_keeps_the_mills_number(scenario, po_on):
    sc, db = scenario, scenario.db
    first, second = Chain(sc), Chain(sc, root=sc.add_root(ge_no=9))
    first.save("B", mr_date=D(2026, 9, 5))
    first.save("A", pct=0.5, mr_date=D(2026, 9, 6))
    second.save("B", mr_date=D(2026, 9, 7))
    second.save("A", pct=0.5, mr_date=D(2026, 9, 8))
    no = db.row("jute_mr", jute_mr_id=first.root)["branch_mr_no"]
    assert db.row("jute_mr", jute_mr_id=second.root)["branch_mr_no"] == no + 1
    first.unfinalize()
    first.save("A", pct=0.25, mr_date=D(2026, 9, 6))
    root = db.row("jute_mr", jute_mr_id=first.root)
    assert (root["branch_mr_no"], root["bill_pass_no"]) == (no, 1)


def test_a_marked_move_undone_and_made_again_keeps_its_number(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    (first,) = move(sc, [sc.root_lines[0]], mr_date=D(2026, 9, 5))
    (second,) = move(sc, [sc.root_lines[1]], mr_date=D(2026, 9, 6))
    no = db.row("jute_mr", jute_mr_id=first["child_mr_id"])["branch_mr_no"]
    from src.jutetransfer import warehouse_stock_ops as ops
    ops.delete_marked_move(first["child_mr_id"], sc.user)
    (again,) = move(sc, [sc.root_lines[0]], mr_date=D(2026, 9, 5))
    assert db.row("jute_mr", jute_mr_id=again["child_mr_id"])["branch_mr_no"] == no
