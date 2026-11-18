"""Owner rules 2026-11-17: an MR in a chain is never dated before where the
lorry came from, and a new step starts on that date, not on today."""

from datetime import date, datetime

import pandas as pd
import pytest

from src.jutetransfer import edit_ops, transfer
from src.jutetransfer.jute_mr_chain_helpers import default_step_date, received_date_problem

from .fake_mysql import fake_db, scenario  # noqa: F401  (fixtures)
from .test_marked_stock import MR_DATE, approve, move
from .test_transfer_po_flow import Chain, po_on  # noqa: F401  (po_on is a fixture)

D = date


def test_the_rule_and_the_default():
    assert received_date_problem(D(2026, 9, 5), D(2026, 9, 5)) is None
    assert received_date_problem(D(2026, 9, 5), None) is None
    assert "Use 05-09-2026 or later" in received_date_problem(D(2026, 9, 4), datetime(2026, 9, 5, 10))
    today = D(2026, 11, 17)
    gate = pd.Timestamp("2026-08-31")
    assert default_step_date([], gate, today) == D(2026, 8, 31)                 # first transfer
    assert default_step_date([{"mr_date": D(2026, 9, 2)}], gate, today) == D(2026, 9, 2)
    assert default_step_date([{"mr_date": "2026-09-02"}, {"mr_date": None}], gate, today) == D(2026, 9, 2)
    assert default_step_date([], pd.NaT, today) == today                        # nothing known


def test_the_first_transfer_cannot_be_before_the_gate_entry(scenario, po_on):
    sc, db = scenario, scenario.db
    gate = db.row("jute_mr", jute_mr_id=sc.root)["jute_gate_entry_date"]
    before = db.snapshot()
    with pytest.raises(transfer.ChainDateError, match="before the gate entry"):
        Chain(sc).save("B", mr_date=gate.replace(day=gate.day - 1))
    assert db.snapshot() == before
    assert Chain(sc).save("B", mr_date=gate)["mr_id"]                           # the same day is fine


def test_a_later_transfer_cannot_be_before_the_previous_one(scenario, po_on):
    sc, db = scenario, scenario.db
    chain = Chain(sc)
    chain.save("B", mr_date=D(2026, 9, 5))
    before = db.snapshot()
    with pytest.raises(transfer.ChainDateError, match="before the previous transfer"):
        chain.save("C", pct=0.5, mr_date=D(2026, 9, 4))
    with pytest.raises(transfer.ChainDateError):                                # the return to the mill too
        chain.save("A", pct=0.5, mr_date=D(2026, 9, 4))
    assert db.snapshot() == before
    assert chain.save("C", pct=0.5, mr_date=D(2026, 9, 5))["mr_id"]


def test_edit_mode_keeps_a_step_between_its_neighbours_in_the_chain(scenario, po_on):
    sc = scenario
    chain = Chain(sc)
    b = chain.save("B", mr_date=D(2026, 9, 5))["mr_id"]
    c = chain.save("C", pct=0.5, mr_date=D(2026, 9, 8))["mr_id"]
    chain.save("A", pct=0.5, mr_date=D(2026, 9, 10))

    def edit(step, new_date):
        return edit_ops.edit_chain_step(chain.root, step, new_date=new_date, reason="fix",
                                        updated_by=sc.user, allow_out_of_order=True)
    with pytest.raises(ValueError, match="after the next transfer"):
        edit(b, D(2026, 9, 9))
    with pytest.raises(ValueError, match="before where the lorry came from"):
        edit(c, D(2026, 9, 4))
    with pytest.raises(ValueError, match="after the next transfer"):
        edit(c, D(2026, 9, 11))
    with pytest.raises(ValueError, match="before where the lorry came from"):
        edit(chain.root, D(2026, 9, 7))
    assert edit(b, D(2026, 9, 8)) and edit(c, D(2026, 9, 10)) and edit(chain.root, D(2026, 9, 12))


def test_a_marked_move_cannot_be_before_its_source_mr(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    before = db.snapshot()
    with pytest.raises(transfer.ChainDateError, match="source MR"):
        move(sc, [sc.root_lines[0]], mr_date=MR_DATE.replace(day=MR_DATE.day - 1))
    assert db.snapshot() == before
    assert move(sc, [sc.root_lines[0]], mr_date=MR_DATE)
