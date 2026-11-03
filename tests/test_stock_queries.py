"""The read queries behind the warehouse page and the P&L dashboard, on the
fake sls database (tests/fake_mysql.py).

Pinned here:
  * get_available_lots / get_quality_availability_summary never offer a
    soft-deleted or removed line (the backend refuses it);
  * get_company_wise_stock_as_of -- the P&L's opening / closing stock -- is
    the ERP's per-line balance on a date (receipt - issues - line-linked
    raw-jute sales dated on or before it), capped at accepted kg and valued
    at rate - claim_rate, per company; marked children (mode 1) and chain /
    purchase MRs (mode 0) split by transfer_mode.
"""
from datetime import date, timedelta

import pandas as pd
import pytest

from src.jutetransfer import queries
from src.jutetransfer import warehouse_stock_ops as ops

from .fake_mysql import fake_db, scenario  # noqa: F401  (fixtures)
from .test_marked_stock import approve, without_shortage

DAY = timedelta(days=1)
MR_DATE, MOVE_DATE = date(2026, 8, 30), date(2026, 9, 5)
EVENT_DATE = date(2026, 9, 10)            # an ERP issue / sale after the MR date
NEXT_FY = date(2027, 6, 30)               # stock bought last FY and still held counts

# the root's two active lines at the post-claim rate (rate - claim_rate)
LINE0 = 9312 * (12850 - 140) / 100
LINE1 = 1441 * (12650 - 50) / 100
ROOT_VALUE = round(LINE0 + LINE1, 2)
# the root after finalize_root: final rates, same claims
FINAL_VALUE = round(9312 * (12914 - 140) / 100 + 1441 * (12713 - 50) / 100, 2)


def _by_company(frame: pd.DataFrame) -> dict:
    return {int(r.co_id): round(float(r.stock_value), 2) for r in frame.itertuples()}


def _stock(as_of: date, transfer_mode=None) -> dict:
    return _by_company(queries.get_company_wise_stock_as_of(as_of, transfer_mode=transfer_mode))


def _lots(sc, which, **kw) -> dict:
    df = queries.get_available_lots(sc.co(which), sc.branch(which), MR_DATE.year, MR_DATE.month, **kw)
    return {int(r.jute_mr_li_id): float(r.remaining_kg) for r in df.itertuples()}


def _issue(sc, line_id: int, weight: float, on: date, status_id: int = 3) -> None:
    """An ERP jute issue entry against one MR line."""
    sc.db.insert("jute_issue", jute_mr_li_id=line_id, issue_date=on, status_id=status_id,
                 weight=weight, branch_id=sc.a_branch)


def _invoice(sc, on: date, branch_id: int) -> int:
    """An approved, active raw-jute sales invoice header (invoice_type 5)."""
    return sc.db.insert("sales_invoice", invoice_no=7, invoice_date=on, invoice_type=5,
                        branch_id=branch_id, status_id=3, active=1, invoice_amount=150000,
                        round_off=0, updated_by=24)


def _line_sale(sc, line_id: int, weight: float, on: date) -> None:
    """An ERP raw-jute sale whose invoice line names the MR line."""
    inv = _invoice(sc, on, sc.a_branch)
    sc.db.insert("sales_invoice_dtl", invoice_id=inv, item_id=sc.items[0], quantity=weight,
                 sales_weight=weight, rate=150, amount_without_tax=weight * 150,
                 jute_mr_li_id=line_id)


def test_soft_deleted_and_removed_lines_are_not_offered_as_lots(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    db.update("jute_mr_li", {"jute_mr_li_id": sc.root_dead_line}, accepted_weight=9900,
              actual_weight=9900, rate=12850, total_price=1272150)
    assert set(_lots(sc, "A")) == set(sc.root_lines)
    summary = queries.get_quality_availability_summary(sc.a_co, sc.a_branch, MR_DATE.year, MR_DATE.month)
    assert float(summary["total_kg"].sum()) == 9312.0 + 1441.0
    # a line removed at QC (status 4) is not stock either
    db.update("jute_mr_li", {"jute_mr_li_id": sc.root_lines[1]}, status="4")
    assert set(_lots(sc, "A")) == {sc.root_lines[0]}
    summary = queries.get_quality_availability_summary(sc.a_co, sc.a_branch, MR_DATE.year, MR_DATE.month)
    assert float(summary["total_kg"].sum()) == 9312.0


# --- get_company_wise_stock_as_of -------------------------------------------------------------

def test_an_approved_mr_is_stock_from_its_mr_date_at_the_post_claim_rate(scenario):
    """Not stock the day before jute_mr_date; from that day on, accepted x (rate - claim)."""
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    # the value comes from the lines, not from the header's net_total
    db.update("jute_mr", {"jute_mr_id": sc.root}, net_total=1)
    assert _stock(MR_DATE - DAY) == {}
    assert _stock(MR_DATE) == {sc.a_co: ROOT_VALUE}
    assert _stock(NEXT_FY) == {sc.a_co: ROOT_VALUE}


def test_a_shortage_line_is_capped_at_its_accepted_weight(scenario):
    """actual > accepted: the balance counts only up to accepted_weight."""
    sc = scenario
    approve(sc)                            # actual 9700 / 1470 kg, accepted 9312 / 1441 kg
    assert _stock(MR_DATE) == {sc.a_co: ROOT_VALUE}
    # 300 kg issued leaves 9,400 kg -- still above accepted, value unchanged
    _issue(sc, sc.root_lines[0], 300, MR_DATE)
    assert _stock(MR_DATE) == {sc.a_co: ROOT_VALUE}
    # 100 kg more leaves 9,300 kg -- now below accepted
    _issue(sc, sc.root_lines[0], 100, MR_DATE)
    assert _stock(MR_DATE) == {sc.a_co: round(9300 * (12850 - 140) / 100 + LINE1, 2)}


def test_an_erp_issue_reduces_the_balance_from_its_issue_date(scenario):
    """An approved issue dated after as_of does not count; on or before it, it does."""
    sc = scenario
    approve(sc)
    without_shortage(sc)
    _issue(sc, sc.root_lines[0], 1000, EVENT_DATE)
    assert _stock(EVENT_DATE - DAY) == {sc.a_co: ROOT_VALUE}
    after = {sc.a_co: round((9312 - 1000) * (12850 - 140) / 100 + LINE1, 2)}
    assert _stock(EVENT_DATE) == after
    assert _stock(NEXT_FY) == after


def test_a_cancelled_erp_issue_never_reduces_the_balance(scenario):
    """An issue at status 4 is ignored on every date."""
    sc = scenario
    approve(sc)
    without_shortage(sc)
    _issue(sc, sc.root_lines[0], 1000, MR_DATE, status_id=4)
    assert _stock(MR_DATE) == {sc.a_co: ROOT_VALUE}
    assert _stock(NEXT_FY) == {sc.a_co: ROOT_VALUE}


def test_an_erp_line_sale_reduces_the_balance_from_its_invoice_date(scenario):
    """An approved raw-jute invoice line naming the MR line counts from its invoice date."""
    sc = scenario
    approve(sc)
    without_shortage(sc)
    _line_sale(sc, sc.root_lines[1], 441, EVENT_DATE)
    assert _stock(EVENT_DATE - DAY) == {sc.a_co: ROOT_VALUE}
    assert _stock(EVENT_DATE) == {sc.a_co: round(LINE0 + 1000 * (12650 - 50) / 100, 2)}


def test_a_marked_move_shifts_the_line_to_the_holder_on_the_move_date(scenario):
    """Seller keeps both lines until MOVE_DATE; then the holder has the moved line; undo restores."""
    sc = scenario
    approve(sc)
    without_shortage(sc)
    before = {sc.a_co: ROOT_VALUE}
    (result,) = ops.save_marked_batch([sc.root_lines[0]], 0.0, sc.b_co, sc.b_branch,
                                      sc.b_godown, MOVE_DATE, sc.user)

    assert _stock(MOVE_DATE - DAY) == before
    # the child's own sales_invoice_jute row (the invoice that created it,
    # dated MOVE_DATE) must not take it out: that rule is mode-0 only
    moved, kept = {sc.b_co: round(LINE0, 2)}, {sc.a_co: round(LINE1, 2)}
    assert _stock(MOVE_DATE) == {**kept, **moved}
    assert _stock(MOVE_DATE, transfer_mode=1) == moved
    assert _stock(MOVE_DATE, transfer_mode=0) == kept

    ops.delete_marked_move(result["child_mr_id"], sc.user)
    assert _stock(MOVE_DATE) == before
    assert _stock(MOVE_DATE, transfer_mode=1) == {}


def test_a_chain_hop_is_not_stock_while_its_root_is_pending(scenario):
    """A mode-0 hop whose root is still at 13 is not stock (nor is the root)."""
    sc = scenario
    sc.add_hop("B", mr_date=date(2026, 9, 1))
    assert sc.db.row("jute_mr", jute_mr_id=sc.root)["status_id"] == 13
    assert _stock(NEXT_FY) == {}


def test_a_finalized_chain_is_stock_at_the_hop_and_at_the_root(scenario):
    """After finalize: the hop at B (its own rates) and the root at A at the final rates."""
    sc = scenario
    sc.add_hop("B", mr_date=date(2026, 9, 1))
    sc.finalize_root("B", mr_date=date(2026, 9, 3))
    hop = {sc.b_co: ROOT_VALUE}            # the hop copies the root's lines and claims
    assert _stock(date(2026, 8, 31)) == {}
    assert _stock(date(2026, 9, 2)) == hop
    assert _stock(date(2026, 9, 3)) == {**hop, sc.a_co: FINAL_VALUE}
    assert _stock(date(2026, 9, 3), transfer_mode=1) == {}


def test_a_chain_hop_invoiced_forward_leaves_stock_on_the_invoice_date(scenario):
    """A sales_invoice_jute row naming the hop removes it from its invoice date on."""
    sc, db = scenario, scenario.db
    hop_id = sc.add_hop("B", mr_date=date(2026, 9, 1))
    sc.finalize_root("B", mr_date=date(2026, 9, 3))
    fwd = date(2026, 9, 4)
    db.insert("sales_invoice_jute", invoice_id=_invoice(sc, fwd, sc.b_branch), mr_id=hop_id)
    assert _stock(fwd - DAY) == {sc.b_co: ROOT_VALUE, sc.a_co: FINAL_VALUE}
    assert _stock(fwd) == {sc.a_co: FINAL_VALUE}


def test_a_pending_mr_with_an_mr_date_is_stock(scenario):
    """Status 13 is stock-view eligible: a Pending MR dated <= as_of counts."""
    sc = scenario
    without_shortage(sc)
    sc.db.update("jute_mr", {"jute_mr_id": sc.root}, jute_mr_date=MR_DATE)
    assert sc.db.row("jute_mr", jute_mr_id=sc.root)["status_id"] == 13
    assert _stock(MR_DATE - DAY) == {}
    assert _stock(MR_DATE) == {sc.a_co: ROOT_VALUE}


def test_an_invoice_both_header_and_line_linked_nets_per_line(scenario):
    """sales_invoice_jute names the MR but a dtl line names line 1: only line 1 leaves."""
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    inv = _invoice(sc, EVENT_DATE, sc.a_branch)
    db.insert("sales_invoice_jute", invoice_id=inv, mr_id=sc.root)
    db.insert("sales_invoice_dtl", invoice_id=inv, item_id=sc.items[0], quantity=9312,
              sales_weight=9312, rate=150, amount_without_tax=9312 * 150,
              jute_mr_li_id=sc.root_lines[0])
    assert _stock(EVENT_DATE - DAY) == {sc.a_co: ROOT_VALUE}
    assert _stock(EVENT_DATE) == {sc.a_co: round(LINE1, 2)}


def test_a_legacy_line_without_accepted_weight_is_valued_on_its_balance(scenario):
    """accepted_weight NULL: no cap, the line counts at actual_weight x (rate - claim)."""
    sc = scenario
    approve(sc)                            # line 0: actual 9700 capped at 9312
    sc.db.update("jute_mr_li", {"jute_mr_li_id": sc.root_lines[1]}, accepted_weight=None)
    assert _stock(MR_DATE) == {sc.a_co: round(LINE0 + 1470 * (12650 - 50) / 100, 2)}


@pytest.mark.parametrize("status_id", [4, 6, 21, 48])
def test_a_rejected_cancelled_draft_or_returned_mr_is_never_stock(scenario, status_id):
    """An MR at 4 / 6 / 21 / 48 is not stock on any date."""
    sc = scenario
    approve(sc)
    without_shortage(sc)
    sc.db.update("jute_mr", {"jute_mr_id": sc.root}, status_id=status_id)
    assert _stock(MR_DATE) == {}
    assert _stock(NEXT_FY) == {}


def test_a_line_removed_at_qc_is_never_stock(scenario):
    """A line at status '4' does not count; the MR's other line still does."""
    sc = scenario
    approve(sc)
    without_shortage(sc)
    sc.db.update("jute_mr_li", {"jute_mr_li_id": sc.root_lines[0]}, status="4")
    assert _stock(MR_DATE) == {sc.a_co: round(LINE1, 2)}
    assert _stock(NEXT_FY) == {sc.a_co: round(LINE1, 2)}
