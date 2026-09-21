"""Tests for pure lot math (no DB, no Streamlit)."""
import pytest
from src.jutetransfer.lot_helpers import (
    validate_takes, apply_pct, line_price, primary_source_mr,
    reduce_amounts, restore_amounts, combine_takes, round_kg, production_rate, net_rate,
)


def test_validate_takes_ok_and_rounding():
    out = validate_takes([(11, 4000.0), (12, 1999.9995)], {11: 6000.0, 12: 2000.0})
    assert out == [(11, 4000.0), (12, 2000.0)]  # whole kg, half-up


def test_validate_takes_full_line_allowed():
    assert validate_takes([(11, 6000.0)], {11: 6000.0}) == [(11, 6000.0)]


def test_validate_takes_empty():
    with pytest.raises(ValueError):
        validate_takes([], {})


def test_validate_takes_duplicate_line():
    with pytest.raises(ValueError):
        validate_takes([(11, 100.0), (11, 200.0)], {11: 6000.0})


def test_validate_takes_zero_and_negative():
    for bad in (0.0, -5.0):
        with pytest.raises(ValueError):
            validate_takes([(11, bad)], {11: 6000.0})


def test_validate_takes_over_available():
    with pytest.raises(ValueError):
        validate_takes([(11, 6001)], {11: 6000.0})


def test_apply_pct_up_down_zero():
    assert apply_pct(2500.0, 10.0) == 2750.0
    assert apply_pct(2500.0, -5.0) == 2375.0
    assert apply_pct(2500.0, 0.0) == 2500.0
    assert apply_pct(2551.33, 2.5) == 2615.11  # verify against REPL; round half-even


def test_line_price():
    assert line_price(4000.0, 2750.0) == 110000.0
    assert line_price(123.456, 2500.0) == 3086.4


def test_primary_source_mr_largest_then_lowest_id():
    assert primary_source_mr({5: 100.0, 9: 300.0}) == 9
    assert primary_source_mr({9: 300.0, 5: 300.0}) == 5
    with pytest.raises(ValueError):
        primary_source_mr({})


def test_combine_takes_weighted_average():
    # 5000 kg @ 2500 + 3000 kg @ 2600 -> one 8000 kg line, value conserved
    kg, price, rate = combine_takes([(5000.0, 2500.0), (3000.0, 2600.0)])
    assert kg == 8000.0
    assert price == 125000.0 + 78000.0
    assert rate == 2537.5  # 203000 * 100 / 8000


def test_combine_takes_same_rate_keeps_rate():
    kg, price, rate = combine_takes([(5000.0, 2500.0), (3000.0, 2500.0)])
    assert (kg, price, rate) == (8000.0, 200000.0, 2500.0)


def test_combine_takes_empty_or_zero_raises():
    with pytest.raises(ValueError):
        combine_takes([])
    with pytest.raises(ValueError):
        combine_takes([(0.0, 2500.0)])


# ---------------------------------------------------------------------------
# reduce_amounts / restore_amounts (conservation math)
# ---------------------------------------------------------------------------

def test_reduce_then_restore_round_trip_full_take():
    accepted, actual_w, actual_q = 6000.0, 6000.0, 60.0
    qty, available = 6000.0, 6000.0
    new_a, new_aw, new_aq, aq_delta, aw_delta = reduce_amounts(
        accepted, actual_w, actual_q, qty, available
    )
    assert (new_a, new_aw, new_aq) == (0.0, 0.0, 0.0)
    r_a, r_aw, r_aq = restore_amounts(new_a, new_aw, new_aq, qty, aq_delta, aw_delta)
    assert (r_a, r_aw, r_aq) == (accepted, actual_w, actual_q)


def test_reduce_then_restore_round_trip_partial_take():
    accepted, actual_w, actual_q = 6000.0, 6000.0, 60.0
    qty, available = 2000.0, 6000.0
    new_a, new_aw, new_aq, aq_delta, aw_delta = reduce_amounts(
        accepted, actual_w, actual_q, qty, available
    )
    assert new_a == 4000.0
    assert new_aw == 4000.0
    assert new_aq == 40.0
    r_a, r_aw, r_aq = restore_amounts(new_a, new_aw, new_aq, qty, aq_delta, aw_delta)
    assert (r_a, r_aw, r_aq) == (accepted, actual_w, actual_q)


def test_reduce_then_restore_round_trip_partial_issued():
    # Some weight already issued to production: available (balance) < accepted.
    # Taking the whole available balance (qty == available).
    accepted, actual_w, actual_q = 6000.0, 6000.0, 60.0
    available = 4000.0  # 2000 kg already issued
    qty = available
    new_a, new_aw, new_aq, aq_delta, aw_delta = reduce_amounts(
        accepted, actual_w, actual_q, qty, available
    )
    # accepted drops by the full qty; actual_w/actual_q track the take fraction
    assert new_a == 2000.0
    assert aw_delta == round(min(qty, actual_w), 3) == 4000.0
    assert aq_delta == round(actual_q * (qty / available), 3) == 60.0
    r_a, r_aw, r_aq = restore_amounts(new_a, new_aw, new_aq, qty, aq_delta, aw_delta)
    assert (r_a, r_aw, r_aq) == (accepted, actual_w, actual_q)


def test_reduce_amounts_actual_weight_zero_raises():
    with pytest.raises(ValueError):
        reduce_amounts(6000.0, 0.0, 60.0, 100.0, 6000.0)


def test_reduce_amounts_actual_weight_none_treated_as_zero_raises():
    with pytest.raises(ValueError):
        reduce_amounts(6000.0, None, 60.0, 100.0, 6000.0)


def test_reduce_amounts_actual_weight_less_than_qty_raises():
    with pytest.raises(ValueError):
        reduce_amounts(6000.0, 50.0, 60.0, 100.0, 6000.0)


# ---------------------------------------------------------------------------
# whole-kg rule + production rate (owner rulings 2026-09-05)
# ---------------------------------------------------------------------------

def test_round_kg_half_up_int():
    assert [round_kg(x) for x in (2.5, 1999.9995, 40.5, None, "106.5")] == [3, 2000, 41, 0, 107]
    assert isinstance(round_kg(2.5), int)


def test_combine_takes_and_reduce_stay_whole_kg():
    kg, price, rate = combine_takes([(1000.5, 2500.0), (999.5, 2500.0)])
    assert kg == 2000.0
    new_a, new_aw, new_aq, aq_delta, aw_delta = reduce_amounts(4811.65, 4811.65, 60.0, 4811.0, 4811.0)
    assert (new_a, new_aw, aw_delta) == (1.0, 1.0, 4811.0)   # legacy fraction rounds, never 0.65


def test_production_rate_prefers_actual_rate_then_rate():
    assert production_rate({"rate": 13065.0, "actual_rate": 13000.0}) == 13000.0
    assert production_rate({"rate": 13065.0, "actual_rate": None}) == 13065.0
    assert production_rate({"rate": None, "actual_rate": None}) == 0.0


def test_net_rate_deducts_claim_rate():
    # ALMA MR 28253 line 45722: 13306/qtl with an 80/qtl claim -> stock at 13226
    assert net_rate({"rate": 13306.0, "claim_rate": 80.0}) == 13226.0
    assert net_rate({"rate": 13306.0, "claim_rate": None}) == 13306.0
    assert net_rate({"rate": None, "claim_rate": None}) == 0.0
    # the transfer % applies to the post-claim rate
    assert apply_pct(net_rate({"rate": 13306.0, "claim_rate": 80.0}), 2) == 13490.52
