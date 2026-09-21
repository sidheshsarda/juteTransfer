"""Pure lot math for warehouse lot management.

No Streamlit/DB imports (same rule as jute_mr_chain_helpers.py).
"""

from decimal import Decimal, ROUND_HALF_UP

_EPS = 1e-9


def round_kg(value) -> int:
    """Jute stock is kept in WHOLE KG (owner rule 2026-09-05; same helper as
    vowerp3be src/juteProcurement/totals.py::round_kg). Half-up, int."""
    if value is None:
        return 0
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def production_rate(line) -> float:
    """jute_mr_li carries two rates: `rate` (approved -> accounting) and
    `actual_rate` (production -> jute stock / batch-cost reports). App-created
    lines must carry the SOURCE line's production rate unchanged; the marked-up
    transfer rate only ever goes into `rate`. Falls back to `rate` on legacy
    lines that never had actual_rate set."""
    ar = line.get("actual_rate") if hasattr(line, "get") else line["actual_rate"]
    if ar is None:
        ar = line.get("rate") if hasattr(line, "get") else line["rate"]
    return float(ar or 0)


def net_rate(line) -> float:
    """Post-claim stock rate per quintal: rate - claim_rate. The ERP keeps
    `rate` gross and books the claim separately (claim_amount =
    accepted_weight/100 * claim_rate), so an approved MR's stock is really
    worth rate - claim_rate. App-created marked lines are claim-free, so for
    them this is just `rate`. No post-claim rate is stored anywhere."""
    return float(line["rate"] or 0) - float(line["claim_rate"] or 0)


def validate_takes(takes, available):
    """Validate (jute_mr_li_id, qty_kg) takes against available kg per line.

    Returns normalised [(int_id, qty_whole_kg)]. Raises ValueError on empty
    input, duplicate line, qty <= 0, or qty > available.
    """
    if not takes:
        raise ValueError("no lots selected")
    seen, out = set(), []
    for li_id, qty in takes:
        li_id = int(li_id)
        qty = float(round_kg(qty))
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
    """Common % rate change (positive or negative), rounded to a WHOLE rupee
    per quintal, half-up. Done in Decimal: float 13300 * 1.005 is
    13366.4999..., which would wrongly round down."""
    new = Decimal(str(rate)) * (1 + Decimal(str(pct)) / 100)
    return float(new.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


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
    total_kg = float(round_kg(sum(float(q) for q, _ in parts)))
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

    Returns (new_accepted, new_actual_w, new_actual_q, aq_delta, aw_delta);
    kg fields are whole kg, actual_q (bales) keeps 3dp. Raises ValueError if actual_w is missing/zero or
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
    aw_delta = float(round_kg(min(qty, actual_w)))
    aq_delta = round(actual_q * frac, 3)
    new_accepted = float(round_kg(accepted - qty))
    new_actual_w = float(round_kg(max(0.0, actual_w - aw_delta)))
    new_actual_q = round(max(0.0, actual_q - aq_delta), 3)
    return new_accepted, new_actual_w, new_actual_q, aq_delta, aw_delta


def restore_amounts(accepted, actual_w, actual_q, qty, aq_delta, aw_delta):
    """Undo reduce_amounts: add qty/aq_delta/aw_delta back (kg whole, bales 3dp)."""
    new_accepted = float(round_kg(float(accepted or 0) + float(qty)))
    new_actual_w = float(round_kg(float(actual_w or 0) + float(aw_delta)))
    new_actual_q = round(float(actual_q or 0) + float(aq_delta), 3)
    return new_accepted, new_actual_w, new_actual_q
