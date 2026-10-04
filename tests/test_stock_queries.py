"""The read queries behind the warehouse page and the P&L dashboard, on the
fake sls database (tests/fake_mysql.py) after a marked move written the
linked way (the source line is not drained; the seller invoice line names it
and the ERP stock view nets the balance).

Pinned here (round-2 follow-ups of the marked-stock change):
  * get_available_lots / get_quality_availability_summary never offer a
    soft-deleted or removed line (the backend refuses it);
  * get_company_wise_unsold_stock values a source MR sold through a marked
    move at its remaining view balance, so the moved kg count once -- as
    marked stock at the holder -- and not also as the seller's unsold stock.
"""
from datetime import date

import pandas as pd

from src.jutetransfer import queries
from src.jutetransfer import warehouse_stock_ops as ops

from .fake_mysql import fake_db, scenario  # noqa: F401  (fixtures)
from .test_marked_stock import approve, without_shortage

FY = (date(2026, 4, 1), date(2027, 3, 31))
MR_DATE, MOVE_DATE = date(2026, 8, 30), date(2026, 9, 5)


def _by_company(frame: pd.DataFrame) -> dict:
    return {int(r.co_id): round(float(r.stock_value), 2) for r in frame.itertuples()}


def _lots(sc, which, **kw) -> dict:
    df = queries.get_available_lots(sc.co(which), sc.branch(which), MR_DATE.year, MR_DATE.month, **kw)
    return {int(r.jute_mr_li_id): float(r.remaining_kg) for r in df.itertuples()}


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


def test_unsold_stock_counts_a_lorry_sold_through_a_marked_move_once(scenario):
    sc, db = scenario, scenario.db
    approve(sc)
    without_shortage(sc)
    root = db.row("jute_mr", jute_mr_id=sc.root)
    before = _by_company(queries.get_company_wise_unsold_stock(*FY))
    assert before == {sc.a_co: round(float(root["net_total"]), 2)}
    assert _by_company(queries.get_company_wise_marked_stock(*FY)) == {}

    # the first line (9,312 kg @ 12,850 less 140 claim) goes to B as marked
    # stock, at the post-claim rate
    (result,) = ops.save_marked_batch([sc.root_lines[0]], 0.0, sc.b_co, sc.b_branch,
                                      sc.b_godown, MOVE_DATE, sc.user)
    marked = _by_company(queries.get_company_wise_marked_stock(*FY))
    assert marked == {sc.b_co: round(9312 * (12850 - 140) / 100, 2)}
    unsold = _by_company(queries.get_company_wise_unsold_stock(*FY))
    # the seller keeps only the line that is still its own: the view balance
    # of the moved line is 0, the other line is 1,441 kg @ 12,650 less 50 claim
    assert unsold == {sc.a_co: round(1441 * (12650 - 50) / 100, 2)}
    # ... and the purchase MR itself was not touched by the move
    assert db.row("jute_mr", jute_mr_id=sc.root)["net_total"] == root["net_total"]

    # undo: the invoice goes, the balance is back, the valuation is as before
    ops.delete_marked_move(result["child_mr_id"], sc.user)
    assert _by_company(queries.get_company_wise_unsold_stock(*FY)) == before
    assert _by_company(queries.get_company_wise_marked_stock(*FY)) == {}


def test_unsold_stock_of_a_source_with_a_claim_is_valued_post_claim(scenario):
    sc, db = scenario, scenario.db
    db.update("jute_mr_li", {"jute_mr_li_id": sc.root_lines[1]}, claim_rate=150)
    approve(sc)
    without_shortage(sc)
    ops.save_marked_batch([sc.root_lines[0]], 0.0, sc.b_co, sc.b_branch, sc.b_godown,
                          MOVE_DATE, sc.user)
    unsold = _by_company(queries.get_company_wise_unsold_stock(*FY))
    assert unsold == {sc.a_co: round(1441 * (12650 - 150) / 100, 2)}
