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


def primary_source_mr(mr_take_totals):
    """MR contributing the largest take qty; ties -> lowest jute_mr_id."""
    if not mr_take_totals:
        raise ValueError("no source MRs")
    return min(mr_take_totals.items(), key=lambda kv: (-kv[1], kv[0]))[0]
