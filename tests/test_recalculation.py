"""Tests for _recalculate_chain with rounding rules."""
from src.jutetransfer.jute_mr_chain_helpers import _recalculate_chain


def _make_line_items(items):
    """Helper: create line_items list from (weight, rate, claim) tuples."""
    return [{"weight": w, "original_rate": r, "original_claim": c} for w, r, c in items]


def _make_step(company="CoB", pct=0.0):
    """Helper: create a minimal step dict."""
    return {
        "company": company,
        "mr_date": None,
        "pct_rate_increase": pct,
        "mr_no": None,
        "warehouse_id": None,
    }


def test_single_item_no_increase():
    """Single line item, one step, no % increase."""
    line_items = _make_line_items([(1000, 2500, 50)])  # 1000kg, 2500 Rs/q, 50 claim
    steps = [_make_step()]
    steps = _recalculate_chain(steps, line_items)

    assert steps[0]["total_amount"] == 25000  # round(1000 * 2500/100, 0) = 25000
    assert steps[0]["claim_amount"] == 50     # round(50, 0)
    assert steps[0]["net_amount"] == 24950
    assert steps[0]["roundoff"] == 0.0


def test_single_item_with_increase():
    """Single line item, two steps, 10% increase on step 2."""
    line_items = _make_line_items([(1000, 2500, 50)])
    steps = [_make_step("CoB"), _make_step("CoC", pct=10.0)]
    steps = _recalculate_chain(steps, line_items)

    # Step 0: original rate
    assert steps[0]["total_amount"] == 25000

    # Step 1: 2500 * 1.10 = 2750 → 1000 * 2750/100 = 27500
    assert steps[1]["total_amount"] == 27500
    assert steps[1]["claim_amount"] == 50  # claim unaffected by %
    assert steps[1]["net_amount"] == 27450


def test_multiple_items_rounding():
    """Multiple line items with different rates; verify rounding rules."""
    line_items = _make_line_items([
        (500, 2450, 30),   # li1: 500 * 2450/100 = 12250.00
        (750, 2800, 45),   # li2: 750 * 2800/100 = 21000.00
    ])
    steps = [_make_step()]
    steps = _recalculate_chain(steps, line_items)

    # Line items: round to 2 → 12250.00 + 21000.00 = 33250.00
    # Header: round to 0 → 33250
    assert steps[0]["total_amount"] == 33250
    assert steps[0]["claim_amount"] == 75  # round(30 + 45, 0)
    assert steps[0]["roundoff"] == 0.0     # no rounding difference here


def test_rounding_creates_roundoff():
    """Verify roundoff captures the rounding difference."""
    # Construct values where line item sum has fractional part
    line_items = _make_line_items([
        (333, 2501, 10),   # li1: round(333 * 2501/100, 2) = round(8328.33, 2) = 8328.33
        (667, 2499, 20),   # li2: round(667 * 2499/100, 2) = round(16668.33, 2) = 16668.33
    ])
    steps = [_make_step()]
    steps = _recalculate_chain(steps, line_items)

    raw_total = 8328.33 + 16668.33  # 24996.66
    assert steps[0]["total_amount"] == 24997  # round(24996.66, 0)
    assert abs(steps[0]["roundoff"] - (24997 - 24996.66)) < 0.01


def test_claim_unaffected_by_pct():
    """Claim amount must NOT change when % rate increase is applied."""
    line_items = _make_line_items([(1000, 2500, 100)])
    steps = [_make_step("CoB"), _make_step("CoC", pct=20.0), _make_step("CoD", pct=15.0)]
    steps = _recalculate_chain(steps, line_items)

    # Claim stays 100 at every step
    for s in steps:
        assert s["claim_amount"] == 100


def test_cumulative_cascading():
    """Step 2's rates are based on Step 1's output, not originals."""
    line_items = _make_line_items([(1000, 2000, 0)])
    steps = [
        _make_step("CoB"),
        _make_step("CoC", pct=10.0),  # 2000 * 1.10 = 2200
        _make_step("CoD", pct=5.0),   # 2200 * 1.05 = 2310
    ]
    steps = _recalculate_chain(steps, line_items)

    assert steps[0]["total_amount"] == 20000  # 1000 * 2000/100
    assert steps[1]["total_amount"] == 22000  # 1000 * 2200/100
    assert steps[2]["total_amount"] == 23100  # 1000 * 2310/100


def test_zero_weight():
    """Zero weight should not cause division errors."""
    line_items = _make_line_items([(0, 2500, 0)])
    steps = [_make_step()]
    steps = _recalculate_chain(steps, line_items)

    assert steps[0]["total_amount"] == 0
    assert steps[0]["weighted_avg_rate"] == 0.0


def test_empty_company_clears_values():
    """Step with no company selected should have zero values."""
    line_items = _make_line_items([(1000, 2500, 50)])
    steps = [_make_step("CoB"), {"company": "", "pct_rate_increase": 0}]
    steps = _recalculate_chain(steps, line_items)

    assert steps[1]["total_amount"] == 0
    assert steps[1]["claim_amount"] == 0
    assert steps[1]["net_amount"] == 0
