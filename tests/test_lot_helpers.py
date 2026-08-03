"""Tests for pure lot math (no DB, no Streamlit)."""
import pytest
from src.jutetransfer.lot_helpers import (
    validate_takes, apply_pct, line_price, primary_source_mr,
)


def test_validate_takes_ok_and_rounding():
    out = validate_takes([(11, 4000.0), (12, 1999.9995)], {11: 6000.0, 12: 2000.0})
    assert out == [(11, 4000.0), (12, 1999.999)]  # 3dp rounding; banker's rounding


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
        validate_takes([(11, 6000.001)], {11: 6000.0})


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
