"""Pure lot math for warehouse lot management.

No Streamlit/DB imports (same rule as jute_mr_chain_helpers.py).
"""

_EPS = 1e-9


def validate_takes(takes, available):
    """Validate (jute_mr_li_id, qty_kg) takes against available kg per line.

    Returns normalised [(int_id, qty_rounded_3dp)]. Raises ValueError on empty
    input, duplicate line, qty <= 0, or qty > available.
    """
    if not takes:
        raise ValueError("no lots selected")
    seen, out = set(), []
    for li_id, qty in takes:
        li_id = int(li_id)
        qty = round(float(qty), 3)
        if li_id in seen:
            raise ValueError(f"duplicate source line {li_id}")
        seen.add(li_id)
        avail = float(available.get(li_id, 0.0))
        if qty <= 0:
            raise ValueError(f"take qty must be > 0 for line {li_id}, got {qty}")
        if qty > avail + _EPS:
            raise ValueError(f"take {qty} exceeds available {avail} for line {li_id}")
        out.append((li_id, qty))
    return out


def apply_pct(rate, pct):
    """Common % rate change (positive or negative), money-rounded."""
    return round(float(rate) * (1.0 + float(pct) / 100.0), 2)


def line_price(weight_kg, rate):
    """Line value: rate is per quintal."""
    return round(float(weight_kg) * float(rate) / 100.0, 2)


def combine_takes(parts):
    """Combine (qty_kg, rate) parts into one merged line.

    Returns (total_kg, total_price, avg_rate). Value is conserved exactly
    (price = sum of part prices); the weighted-average rate is derived back
    from it, money-rounded — so kg * rate / 100 may differ from price by
    rounding pennies. Raises ValueError on empty/zero total.
    """
    total_kg = round(sum(float(q) for q, _ in parts), 3)
    if total_kg <= 0:
        raise ValueError("nothing to merge")
    total_price = round(sum(line_price(q, r) for q, r in parts), 2)
    avg_rate = round(total_price * 100.0 / total_kg, 2)
    return total_kg, total_price, avg_rate


def primary_source_mr(mr_take_totals):
    """MR contributing the largest take qty; ties -> lowest jute_mr_id."""
    if not mr_take_totals:
        raise ValueError("no source MRs")
    return min(mr_take_totals.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def reduce_amounts(accepted, actual_w, actual_q, qty, available):
    """Reduce a source line by qty kg across accepted AND actual fields.

    Mirrors the ERP-stock-view-aware split used by mark moves / lots: the
    take fraction (qty/available) scales actual_qty, while actual_weight is
    reduced by min(qty, actual_w) directly.

    Returns (new_accepted, new_actual_w, new_actual_q, aq_delta, aw_delta),
    all rounded to 3dp. Raises ValueError if actual_w is missing/zero or
    less than qty (moving more than the line's actual on-hand weight would
    mint balance the ERP stock view doesn't have).
    """
    accepted = float(accepted or 0)
    actual_w = float(actual_w or 0)
    actual_q = float(actual_q or 0)
    qty = float(qty)
    available = float(available)
    if actual_w <= 0 or actual_w + _EPS < qty:
        raise ValueError(
            f"Line: actual_weight ({actual_w}) is missing or less than the "
            f"quantity to move ({qty}); ERP stock view would go inconsistent "
            "- fix the line data first"
        )
    frac = qty / available if available > 0 else 1.0
    aw_delta = round(min(qty, actual_w), 3)
    aq_delta = round(actual_q * frac, 3)
    new_accepted = round(accepted - qty, 3)
    new_actual_w = round(max(0.0, actual_w - aw_delta), 3)
    new_actual_q = round(max(0.0, actual_q - aq_delta), 3)
    return new_accepted, new_actual_w, new_actual_q, aq_delta, aw_delta


def restore_amounts(accepted, actual_w, actual_q, qty, aq_delta, aw_delta):
    """Undo reduce_amounts: add qty/aq_delta/aw_delta back, 3dp rounded."""
    new_accepted = round(float(accepted or 0) + float(qty), 3)
    new_actual_w = round(float(actual_w or 0) + float(aw_delta), 3)
    new_actual_q = round(float(actual_q or 0) + float(aq_delta), 3)
    return new_accepted, new_actual_w, new_actual_q
