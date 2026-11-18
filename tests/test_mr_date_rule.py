"""MR date order (owner rule 2026-11-17): a new MR number may not be dated
before the latest MR already numbered at that branch in the financial year,
nor after today; an admin may override with a reason, which is logged."""

import json
from datetime import date, timedelta

import pytest

from src.jutetransfer import access, transfer
from src.jutetransfer.jute_mr_chain_helpers import mr_date_problem

from .fake_mysql import fake_db, scenario  # noqa: F401  (fixtures)
from .test_marked_stock import approve, move
from .test_transfer_po_flow import Chain, po_on  # noqa: F401  (po_on is a fixture)


def test_the_rule_itself():
    d = date(2026, 9, 5)
    assert mr_date_problem(d) is None                                  # first MR of the series
    assert mr_date_problem(d, date(2026, 9, 4), 7) is None
    assert mr_date_problem(d, date(2026, 9, 5), 7) is None             # same day is fine
    assert "MR no. 7" in mr_date_problem(d, date(2026, 9, 6), 7)
    assert "06-09-2026" in mr_date_problem(d, date(2026, 9, 6), 7)
    assert "after today" in mr_date_problem(d, None, None, today=date(2026, 9, 4))
    assert mr_date_problem(d, None, None, today=d) is None


def test_a_hop_dated_before_the_branchs_latest_mr_is_refused(scenario, po_on):
    sc, db = scenario, scenario.db
    Chain(sc).save("B", mr_date=date(2026, 9, 5))
    later = Chain(sc, root=sc.add_root(ge_no=9))
    before = db.snapshot()
    with pytest.raises(transfer.MrDateOrderError, match="earliest date allowed is 05-09-2026"):
        later.save("B", mr_date=date(2026, 9, 4))
    assert db.snapshot() == before                                       # nothing written
    assert later.save("B", mr_date=date(2026, 9, 5))["mr_id"]          # the same day is fine


def test_another_company_is_not_affected(scenario, po_on):
    sc = scenario
    Chain(sc).save("B", mr_date=date(2026, 9, 5))
    other = Chain(sc, root=sc.add_root(ge_no=9))
    assert other.save("C", mr_date=date(2026, 9, 1))["mr_id"]


def test_erp_mrs_count_and_rejected_ones_do_not(scenario, po_on):
    sc, db = scenario, scenario.db
    db.insert("jute_mr", branch_id=sc.b_branch, status_id=3, branch_mr_no=40,
              jute_mr_date=date(2026, 9, 10), transfer_mode=0)         # approved in the ERP
    db.insert("jute_mr", branch_id=sc.b_branch, status_id=4, branch_mr_no=41,
              jute_mr_date=date(2026, 9, 20), transfer_mode=0)         # rejected: holds no place
    db.insert("jute_mr", branch_id=sc.b_branch, status_id=13,
              jute_mr_date=date(2026, 9, 25), transfer_mode=0)         # no number yet
    with pytest.raises(transfer.MrDateOrderError, match=r"10-09-2026 \(MR no. 40\)"):
        Chain(sc).save("B", mr_date=date(2026, 9, 9))
    assert Chain(sc).save("B", mr_date=date(2026, 9, 10))["mr_id"]


def test_finalize_is_checked_at_the_mill(scenario, po_on):
    sc, db = scenario, scenario.db
    db.insert("jute_mr", branch_id=sc.a_branch, status_id=3, branch_mr_no=40,
              jute_mr_date=date(2026, 9, 10), transfer_mode=0)
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    with pytest.raises(transfer.MrDateOrderError):
        chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    assert chain.save("A", pct=0.5, mr_date=date(2026, 9, 10))


def test_a_new_financial_year_starts_its_own_series(scenario, po_on):
    sc, db = scenario, scenario.db
    db.insert("jute_mr", branch_id=sc.b_branch, status_id=3, branch_mr_no=900,
              jute_mr_date=date(2026, 3, 30), transfer_mode=0)         # last year's series
    early = Chain(sc, root=sc.add_root(ge_no=9, ge_date=date(2026, 4, 1)))
    assert early.save("B", mr_date=date(2026, 4, 2))["mr_id"]


def test_a_future_date_is_refused(scenario, po_on):
    with pytest.raises(transfer.MrDateOrderError, match="after today"):
        Chain(scenario).save("B", mr_date=date.today() + timedelta(days=1))


def test_an_admin_override_saves_and_is_logged(scenario, po_on):
    sc, db = scenario, scenario.db
    Chain(sc).save("B", mr_date=date(2026, 9, 5))
    later = Chain(sc, root=sc.add_root(ge_no=9))
    assert later.save("B", mr_date=date(2026, 9, 4), date_override="lorry keyed late")["mr_id"]
    (row,) = db.rows("jt_change_log")
    assert (row["action"], row["reason"], row["user_id"], row["branch_id"], row["jute_mr_id"]) == (
        "mr-date-override", "lorry keyed late", sc.user, sc.b_branch, later.root)
    assert json.loads(row["detail"]) == {"mr_date": "2026-09-04", "latest_mr_date": "2026-09-05",
                                         "latest_mr_no": 1}


def test_an_override_within_the_rule_logs_nothing(scenario, po_on):
    sc, db = scenario, scenario.db
    assert Chain(sc).save("B", mr_date=date(2026, 9, 5), date_override="not needed")["mr_id"]
    assert db.rows("jt_change_log") == []


def test_only_an_admin_may_override(scenario, po_on, monkeypatch):
    sc, db = scenario, scenario.db
    monkeypatch.delenv("JT_LEGACY_LOGIN", raising=False)
    monkeypatch.setattr(access, "get_role", lambda uid: "user")
    before = db.snapshot()
    with pytest.raises(access.NotAllowed):
        Chain(sc).save("B", mr_date=date(2026, 9, 5), date_override="because")
    assert db.snapshot() == before


def test_marked_moves_follow_the_rule_at_the_target_branch(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    db.insert("jute_mr", branch_id=sc.b_branch, status_id=3, branch_mr_no=40,
              jute_mr_date=date(2026, 9, 10), transfer_mode=0)
    before = db.snapshot()
    with pytest.raises(transfer.MrDateOrderError):
        move(sc, [sc.root_lines[0]], mr_date=date(2026, 9, 9))
    assert db.snapshot() == before
    assert move(sc, [sc.root_lines[0]], mr_date=date(2026, 9, 10))
