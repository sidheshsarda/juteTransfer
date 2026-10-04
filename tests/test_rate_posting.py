"""The rate posted on MR / invoice lines must be the rate the screen showed.

Screen: jute_mr_chain_helpers._cascade_rate (Decimal). Posting: transfer.py
via hop_rate. They used to differ: in float, 13100 x 1.005 is
13165.499999999998, so posting stored 13165 while the screen (and the header
total built from it) said 13166 -- the gap went into the invoice round-off."""
from decimal import Decimal

import pytest

from src.jutetransfer.jute_mr_chain_helpers import (
    _calculate_line_item_amount, _cascade_rate, hop_rate, step_multiplier,
)


def _screen_rate(rate, pct):
    return _cascade_rate(rate, [{}, {"pct_rate_increase": pct}], 1)


def test_the_live_case_13100_at_half_a_percent():
    assert 13100 * 1.005 != 13165.5                     # the float trap
    assert _screen_rate(13100, 0.5) == 13166.0
    assert hop_rate(13100, step_multiplier(0.5, 1.0 + 0.5 / 100.0)) == (13166.0, 131.66)
    # even when only the float multiplier is known
    assert hop_rate(13100, 1.005) == (13166.0, 131.66)


@pytest.mark.parametrize("pct", [0, 0.25, 0.33, 0.5, 0.75, 1, 1.5, 2, 2.5, -0.5, -1.25, 10])
def test_posted_rate_equals_screen_rate_for_every_whole_rate(pct):
    multiplier = step_multiplier(pct, 1.0 + pct / 100.0)
    for rate in range(3000, 20001, 50):
        assert hop_rate(rate, multiplier)[0] == _screen_rate(rate, pct), (rate, pct)


def test_posted_rate_equals_screen_rate_for_odd_rates():
    for rate in (17075, 14657, 14526, 14465, 11861, 11851, 11551, 3446, 15000.1, 12913.75):
        for pct in (0.5, 0.33, 1.0):
            multiplier = step_multiplier(pct, 1.0 + pct / 100.0)
            assert hop_rate(rate, multiplier)[0] == _screen_rate(rate, pct), (rate, pct)


def test_kg_rate_and_quintal_rate_are_exact_floats():
    q, kg = hop_rate(12850, Decimal("1.005"))
    assert (q, kg) == (12914.0, 129.14)                 # live finalized root 28137
    assert q == kg * 100 or abs(q - kg * 100) < 1e-9
    assert _calculate_line_item_amount(9312, q) == 1202551.68   # the stored line total


def test_missing_rate_is_zero():
    assert hop_rate(None, 1.005) == (0.0, 0.0)
    assert hop_rate(float("nan"), 1.005) == (0.0, 0.0)


def test_step_multiplier_prefers_the_typed_percentage():
    assert step_multiplier(0.5, 1.005) == Decimal("1.005")
    assert step_multiplier(0.33, 1.0 + 0.33 / 100.0) == Decimal("1.0033")
    assert step_multiplier(0.0, 1.0) == Decimal("1")
    assert step_multiplier(None, 1.005) == Decimal("1.005")       # no pct: use the float
    assert step_multiplier(float("nan"), 1.005) == Decimal("1.005")
    assert step_multiplier(0.5) == Decimal("1.005")               # pct only
    assert step_multiplier(None) == Decimal("1")
    # pct that does not describe the multiplier (e.g. a wrapper passing 1.0
    # for the first step of a chain whose step carries a pct): trust the multiplier
    assert step_multiplier(0.5, 1.0) == Decimal("1.0")
