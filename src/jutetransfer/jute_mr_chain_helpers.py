"""Pure chain helper functions for JuteTransfer (no Streamlit dependency)."""

import pandas as pd
from datetime import date
from decimal import Decimal, ROUND_HALF_UP


# ---------------------------------------------------------------------------
# Shared rounding helpers (used by live display, chain recalc, and posting)
# ---------------------------------------------------------------------------

def _cascade_rate(original_rate: float, steps: list, up_to_index: int) -> float:
    """Round-then-cascade: round at kg level (2 decimals) at each step boundary.

    Starting from original_rate (per quintal), for each step from 1..up_to_index:
      apply pct increase -> convert to kg -> round to 2 -> back to quintal.
    Returns the final quintal rate.
    """
    # Treat missing rate as 0 for math (display layer is responsible for showing "—")
    if original_rate is None or original_rate != original_rate:  # None or NaN
        return 0.0
    rate_quintal = Decimal(str(original_rate))
    for i in range(1, up_to_index + 1):
        pct = Decimal(str(float(steps[i].get("pct_rate_increase", 0) or 0)))
        new_quintal = rate_quintal * (1 + pct / 100)
        # Round at kg level (2 decimals), then convert back to quintal
        rate_kg = (new_quintal / 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        rate_quintal = rate_kg * 100
    return float(rate_quintal)


def _calculate_line_item_amount(weight: float, rate_per_quintal: float) -> float:
    """Amount = weight * rate / 100, rounded to 2 decimals (ROUND_HALF_UP).

    Single source of truth for per-item amount calculation.
    """
    return float(
        (Decimal(str(weight)) * Decimal(str(rate_per_quintal)) / Decimal('100'))
        .quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    )


def _calculate_step_totals(line_items: list, steps: list,
                           step_index: int) -> tuple:
    """Calculate totals for a step using round-then-cascade rounding.

    None/NaN weight or rate values are treated as 0 for math (the display layer
    is responsible for surfacing missing data clearly).

    Returns: (raw_sum_2decimal, rounded_total_0decimal, roundoff)
    """
    raw_sum = 0.0
    for li in line_items:
        w = li.get("weight")
        r = li.get("original_rate")
        weight = round(float(w), 0) if (w is not None and w == w) else 0.0
        orig_rate = float(r) if (r is not None and r == r) else 0.0
        if step_index > 0:
            effective_rate = _cascade_rate(orig_rate, steps, step_index)
        else:
            effective_rate = orig_rate
        raw_sum += _calculate_line_item_amount(weight, effective_rate)
    rounded_total = round(raw_sum, 0)
    roundoff = round(rounded_total - raw_sum, 2)
    return (raw_sum, rounded_total, roundoff)


# Columns shown in the compact monthly overview table
COMPACT_COLUMNS = [
    "Jute Gate Entry No", "MR DATE", "Jute Supplier", "Party Name",
    "Party Branch Address", "Item Quality",
    "Weight (KG)", "MR Rate", "Total Amount", "Claim Amount", "Net Total",
    "Transfer Chain", "Chain Status",
]


def _group_by_mr(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Group raw per-line-item dataframe into one row per jute_mr_id.

    Returns:
        (grouped_df, line_items_map) where line_items_map is
        {jute_mr_id: [{weight, original_rate, original_claim}, ...]}
    """
    if df is None or df.empty:
        return df, {}

    line_items_map = {}
    grouped_rows = []

    def _opt_num(v):
        """Coerce to float, returning None for missing/invalid values.

        Distinguishes "missing data" (None) from "real zero" (0.0). Calculations
        can treat None as 0; the display layer can show '—' for missing values.
        """
        if v is None or not pd.notna(v):
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    for mr_id, group in df.groupby("jute_mr_id"):
        # Extract per-line-item details
        items = []
        for _, row in group.iterrows():
            w = _opt_num(row.get("Weight (KG)"))
            items.append({
                "weight": round(w, 0) if w is not None else None,
                "original_rate": _opt_num(row.get("MR Rate")),
                "original_claim": _opt_num(row.get("Claim Amount")),
                "item_quality": row.get("Item Quality") or "Item",
            })
        line_items_map[int(mr_id)] = items

        # Aggregated row
        first = group.iloc[0].to_dict()
        total_weight = sum(li["weight"] for li in items)
        total_amount = group["Total Amount"].fillna(0).astype(float).sum()
        claim_amount = group["Claim Amount"].fillna(0).astype(float).sum()

        first["Weight (KG)"] = total_weight
        first["Total Amount"] = total_amount
        first["Claim Amount"] = claim_amount
        first["Net Total"] = total_amount - claim_amount
        first["MR Rate"] = (total_amount / total_weight * 100) if total_weight > 0 else 0.0

        # Combine qualities
        qualities = group["Item Quality"].dropna().unique()
        if len(qualities) > 1:
            first["Item Quality"] = " / ".join(str(q) for q in qualities)

        grouped_rows.append(first)

    grouped_df = pd.DataFrame(grouped_rows)
    return grouped_df, line_items_map


def _reconstruct_chain(chain_mrs: list[dict], root_co_id: int) -> list[dict]:
    """Reconstruct transfer chain order from MR records.

    Args:
        chain_mrs: List of MR dicts with jute_mr_id, src_com_id, owner_co_id, branch_id.
                   Must be sorted by jute_mr_id ASC.
        root_co_id: The root/source company's co_id.

    Returns:
        Ordered list of MR dicts representing the chain.
    """
    if not chain_mrs:
        return []

    # Ensure sorted by jute_mr_id for tie-breaking
    sorted_mrs = sorted(chain_mrs, key=lambda m: m["jute_mr_id"])

    visited = set()
    ordered = []
    current_co = root_co_id

    while True:
        # Find unvisited MR where src_com_id == current_co, lowest jute_mr_id first
        candidate = None
        for mr in sorted_mrs:
            if mr["jute_mr_id"] not in visited and mr.get("src_com_id") == current_co:
                candidate = mr
                break

        if candidate is None:
            break

        visited.add(candidate["jute_mr_id"])
        ordered.append(candidate)
        # Derive next current_co from this MR's owner
        current_co = candidate.get("owner_co_id") or candidate.get("co_id")

    return ordered


def _empty_transfer_step() -> dict:
    """Return a blank transfer step dict."""
    return {
        "company": "",
        "mr_no": None,
        "mr_date": None,
        "pct_rate_increase": 0.0,
        "mr_rate": 0.0,
        "total_amount": 0.0,
        "claim_amount": 0.0,
        "net_amount": 0.0,
        "warehouse_id": None,
        "challan_date": None,
        "lc_reference_no": "",
        "lc_date": None,
        "po_no_for_lc": "",
        "order_date_for_lc": None,
        "transfer_transport": True,
    }


def _build_chain_summary(steps: list) -> str:
    """Build a human-readable transfer chain summary string."""
    companies = [s["company"] for s in steps if s["company"]]
    if not companies:
        return "No transfers"
    return " -> ".join(companies)


def _get_chain_status(steps: list, source_co_branch: str) -> str:
    """Determine chain status based on whether it returns to source."""
    companies = [s["company"] for s in steps if s["company"]]
    if not companies:
        return "-"
    if companies and companies[-1] == source_co_branch:
        return "Complete"
    return f"{len(companies)} step(s)"


def _calculate_step_total_amount(step_index: int, line_items: list, step_dict: dict,
                                 steps: list, original_total_amount: float,
                                 use_new_rounding: bool = False) -> float:
    """Calculate total amount for a step using per-item rate chaining.

    Args:
        step_index: Current step number (0-indexed)
        line_items: List of dicts with 'weight', 'original_rate', 'original_claim' keys
        step_dict: Current step's data dict
        steps: List of all steps (for calculating cumulative rates)
        original_total_amount: Original total amount from source MR
        use_new_rounding: When True, use round-then-cascade with 2-decimal amounts

    Returns:
        Calculated total amount for this step (raw sum, not rounded)
    """
    if use_new_rounding:
        # Use shared calculation functions for consistency with posting
        raw_sum, _, _ = _calculate_step_totals(line_items, steps, step_index)
        return raw_sum

    if step_index == 0:
        # Step 1: Use original rates per item
        total = 0.0
        for line_item in line_items:
            qty = float(line_item.get("weight", 0) or 0)
            rate = float(line_item.get("original_rate", 0) or 0)
            total += qty * rate / 100.0
        return total
    else:
        # Step 2+: Apply cumulative % increases to each item's original rate
        cumulative_multiplier = 1.0
        for step_i in range(1, step_index):
            if steps and step_i < len(steps):
                step_pct = float(steps[step_i].get("pct_rate_increase", 0) or 0)
                cumulative_multiplier *= (1.0 + step_pct / 100.0)

        # Apply current step's % increase
        current_pct = float(step_dict.get("pct_rate_increase", 0) or 0)
        current_multiplier = cumulative_multiplier * (1.0 + current_pct / 100.0)

        # Calculate per-item amounts with current multiplier
        total = 0.0
        for line_item in line_items:
            qty = float(line_item.get("weight", 0) or 0)
            original_rate = float(line_item.get("original_rate", 0) or 0)
            effective_rate = original_rate * current_multiplier
            total += qty * effective_rate / 100.0

        return total


def _recalculate_chain(steps: list, line_items: list, original_total_amount: float = 0.0,
                       use_new_rounding: bool = False) -> list:
    """Recalculate all derived values in a transfer chain.

    Uses total-based chaining: each step's raw total is prev_raw_total * (1 + pct/100).
    This is mathematically equivalent to per-item rate chaining (since all weights are
    fixed), but avoids errors caused by reconstructing per-item rates from rounded totals.

    For saved steps (step has 'saved_mr_id' set), the stored total_amount is used directly
    as both the display value and the prev_raw_total anchor for the next step. For unsaved
    editing steps, raw_total is computed from prev_raw_total and rounded only for display.

    When use_new_rounding=True, uses per-item round-then-cascade calculation via
    _calculate_step_totals() for consistent rounding with posting functions.

    Args:
        steps: List of transfer step dicts (mutated in place and returned).
        line_items: List of {weight, original_rate, original_claim} per line item.
        original_total_amount: The original source MR total amount (used for Step 1 base
                               when no saved total is available).
        use_new_rounding: When True, use round-then-cascade with 2-decimal amounts.

    Returns:
        The steps list with computed aggregates filled in.
    """
    claims = [float(li.get("original_claim") or 0) for li in line_items]
    total_weight = sum(round(float(li.get("weight") or 0), 0) for li in line_items)
    total_claim = round(sum(claims), 0)

    # Track the running total so unsaved steps can anchor on the previous
    # step's actual total (saved or calculated) rather than recalculating
    # from original rates with a cumulative multiplier.  This avoids the bug
    # where a saved step's rates diverge from what pct_rate_increase alone
    # would produce and subsequent unsaved steps show stale original values.
    prev_total = original_total_amount

    for i, step in enumerate(steps):
        if not step.get("company"):
            step["total_amount"] = 0
            step["claim_amount"] = 0
            step["net_amount"] = 0
            step["roundoff"] = 0.0
            step["weighted_avg_rate"] = 0.0
            continue

        if step.get("saved_mr_id"):
            # Saved step: use DB-stored total as the authoritative value
            stored_total = float(step.get("total_amount") or 0)
            step["total_amount"] = round(stored_total, 0)
            step["roundoff"] = 0.0
            step["claim_amount"] = total_claim
            step["net_amount"] = round(step["total_amount"] - total_claim, 0)
            step["weighted_avg_rate"] = (
                step["total_amount"] / total_weight * 100
            ) if total_weight > 0 else 0.0
            prev_total = stored_total
        else:
            if use_new_rounding:
                # Per-item round-then-cascade calculation (consistent with posting)
                raw_sum, rounded_total, roundoff = _calculate_step_totals(
                    line_items, steps, i
                )
                step["total_amount"] = rounded_total
                step["roundoff"] = roundoff
                step["claim_amount"] = total_claim
                step["net_amount"] = round(rounded_total - total_claim, 0)
                step["weighted_avg_rate"] = (
                    rounded_total / total_weight * 100
                ) if total_weight > 0 else 0.0
                prev_total = raw_sum
            else:
                # Unsaved editing step: anchor on previous step's total
                if i == 0:
                    # Step 1 (source): use per-item calculation
                    calculated_total = _calculate_step_total_amount(
                        i, line_items, step, steps, original_total_amount
                    )
                else:
                    # Step 2+: apply this step's % increase to previous total
                    current_pct = float(step.get("pct_rate_increase", 0) or 0)
                    calculated_total = prev_total * (1.0 + current_pct / 100.0)

                rounded_total = round(calculated_total, 0)

                step["total_amount"] = rounded_total
                step["roundoff"] = round(rounded_total - calculated_total, 2)
                step["claim_amount"] = total_claim
                step["net_amount"] = round(rounded_total - total_claim, 0)
                step["weighted_avg_rate"] = (
                    rounded_total / total_weight * 100
                ) if total_weight > 0 else 0.0
                prev_total = calculated_total

    return steps


def _find_source_co_branch(selected_company_id, selected_branch_id,
                           co_branch_mapping: dict) -> str | None:
    """Find the co_branch label for the selected source company/branch by IDs."""
    for label, (co_id, branch_id) in co_branch_mapping.items():
        if co_id == selected_company_id and branch_id == selected_branch_id:
            return label
    return None
