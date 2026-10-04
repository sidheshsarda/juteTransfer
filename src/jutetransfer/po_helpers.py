"""Pure helpers for transfer POs (no Streamlit/DB imports).

A transfer PO is the purchase order juteTransfer writes for a vertical-chain
transfer: a FORWARD PO with every hop MR at a forwarding company, and a FINAL
PO at the origin company when the chain is finalized. Design:
docs/superpowers/specs/2026-10-01-transfer-po-design.md.
"""

import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

# Owner rule 2026-10-01: PO rates are rounded to the closest 50 (per quintal).
# Only the PO is rounded -- MRs and invoices keep their exact rates.
PO_RATE_STEP = 50

# ERP constants (vowerp3be src/juteProcurement/jutePO.py): a PO line carries a
# count of bales / loose units, never a weight.
BALE_KG = 150
LOOSE_KG = 48
UOM_BALE = "BALE"
UOM_LOOSE = "LOOSE"

PO_STATUS_APPROVED = 3
PO_STATUS_CLOSED = 5
PO_LINE_STATUS = 21  # what ERP-made lines carry; cosmetic, nothing filters on it
# Not one of the ERP's own close types (AUTO/SHORT/BULK/MIGRATED): the
# settlement engine only ever reopens AUTO, so this stays closed, and the ERP
# shows it as plain "Closed".
CLOSE_TYPE_TRANSFER = "TRANSFER"
LOG_SOURCE = "BACKEND"

ROLE_FORWARD = "FORWARD"
ROLE_FINAL = "FINAL"

# jute_po.jute_po_value / jute_po_li.value are DECIMAL(10,2); STRICT mode
# rejects anything larger instead of truncating.
MAX_PO_VALUE = Decimal("99999999.99")
MARKA_MAX_LEN = 50  # jute_po_li.marka is varchar(50); jute_mr_li.marka is 255

_MARKER_RE = re.compile(
    r"^JT\|(FORWARD|FINAL)\|root=(\d+)\|mr=(\d+)\|srcpo=(\d+)\|"
)
# Optional field of a FINAL marker: the mill MR's header as it was before
# finalize overwrote it -- party / party branch / MR date, each empty for NULL.
_ORIGINAL_RE = re.compile(r"\|orig=(\d*)/(\d*)/(\d{4}-\d{2}-\d{2})?\|")


def _num(value) -> Optional[float]:
    """float(value), or None for None / NaN / unparseable."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def round_rate(rate, step: int = PO_RATE_STEP) -> float:
    """Rate per quintal rounded to the closest `step` rupees, half-up.

    Done in Decimal so a float like 12924.999999 or 12925.0 lands where a
    person would put it (12,925 -> 12,950)."""
    r = _num(rate)
    if not r:
        return 0.0
    units = (Decimal(str(r)) / Decimal(step)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return float(units * step)


def normalise_uom(*candidates) -> str:
    """First candidate that is BALE or LOOSE (case/space-insensitive), else
    LOOSE (the ERP PO form's own default)."""
    for c in candidates:
        if isinstance(c, str) and c.strip().upper() in (UOM_BALE, UOM_LOOSE):
            return c.strip().upper()
    return UOM_LOOSE


def unit_kg(uom: str) -> int:
    """Nominal kg of one PO unit: a bale is 150 kg, a loose unit 48 kg."""
    return BALE_KG if normalise_uom(uom) == UOM_BALE else LOOSE_KG


def equivalent_units(weight_kg, unit: int) -> int:
    """Whole bales / loose units nearest to `weight_kg`, never less than 1.

    Same convention the ERP uses for a PO line's quantity
    (round(line_kg / unit))."""
    w = _num(weight_kg) or 0.0
    units = (Decimal(str(w)) / Decimal(unit)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return max(1, int(units))


def _is_active(line) -> bool:
    active = line.get("active")
    a = _num(active)
    return active is None or a is None or int(a) == 1


def build_po_lines(mr_lines, uom: str, default_crop_year=None) -> list:
    """PO lines for one MR: one per active MR line with accepted weight > 0,
    in jute_mr_li_id order.

    The PO is written in the ERP's quantity mode (no percentages): quantity
    is the whole number of bales / loose units nearest the accepted weight,
    line weight is quantity x unit kg, value is line kg / 100 x the rate
    rounded to the closest 50. That keeps the stored figures identical to
    what the ERP PO page re-derives from quantity."""
    unit = unit_kg(uom)
    out = []
    for li in sorted(mr_lines, key=lambda r: int(r.get("jute_mr_li_id") or 0)):
        if not _is_active(li):
            continue
        accepted = _num(li.get("accepted_weight")) or 0.0
        if accepted <= 0:
            continue
        quantity = equivalent_units(accepted, unit)
        line_kg = quantity * unit
        mr_rate = _num(li.get("rate")) or 0.0
        rate = round_rate(mr_rate)
        value = (Decimal(line_kg) * Decimal(str(rate)) / Decimal(100)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        item_id = _num(li.get("actual_item_id"))
        if item_id is None:
            item_id = _num(li.get("challan_item_id"))
        marka = li.get("marka")
        if marka is not None and not isinstance(marka, str):
            marka = None if _num(marka) is None else str(marka)
        crop_year = _num(li.get("crop_year"))
        if crop_year is None:
            crop_year = _num(default_crop_year)
        if crop_year is not None and crop_year >= 100:
            # The ERP stores a PO line's crop year as the two-digit start
            # year (25 = 25-26); some MR lines carry 2025 / 2026.
            crop_year = crop_year % 100
        out.append({
            "jute_mr_li_id": int(li["jute_mr_li_id"]),
            "item_id": None if item_id is None else int(item_id),
            "accepted_weight": accepted,
            "quantity": float(quantity),
            "line_kg": float(line_kg),
            "mr_rate": mr_rate,
            "rate": rate,
            "value": float(value),
            "marka": marka[:MARKA_MAX_LEN] if marka else None,
            "crop_year": None if crop_year is None else int(crop_year),
            "allowable_moisture": _num(li.get("allowable_moisture")),
        })
    return out


def po_totals(lines) -> tuple:
    """(weight_kg, value) of a PO: plain sums of its lines."""
    weight = sum(Decimal(str(l["line_kg"])) for l in lines)
    value = sum(Decimal(str(l["value"])) for l in lines)
    return float(weight), float(value)


def _original_field(original) -> str:
    """'orig=<party>/<party branch>/<yyyy-mm-dd>|' for build_marker, or ''
    when the original party is not a plain number (then nothing is
    remembered and un-finalize falls back to deriving it)."""
    if not original:
        return ""
    party = original.get("party_id")
    party_s = str(party).strip() if party is not None else ""
    if party_s.endswith(".0"):
        party_s = party_s[:-2]
    if party_s and not party_s.isdigit():
        return ""
    pbr = _num(original.get("party_branch_id"))
    mr_date = original.get("jute_mr_date")
    if hasattr(mr_date, "date") and callable(mr_date.date):
        mr_date = mr_date.date()
    date_s = mr_date.isoformat() if isinstance(mr_date, date) else ""
    return f"orig={party_s}/{'' if pbr is None else int(pbr)}/{date_s}|"


def build_marker(role: str, root_mr_id: int, mr_id: int, src_po_id=None,
                 original: Optional[dict] = None) -> str:
    """Provenance marker stored in jute_po.internal_note.

    Every key=value is closed by '|', so 'root=281|' can never match
    'root=28137|'. A FINAL marker may also remember the mill MR's header as
    it was before finalize (`original`: party_id, party_branch_id,
    jute_mr_date) so un-finalize can put it back exactly."""
    if role not in (ROLE_FORWARD, ROLE_FINAL):
        raise ValueError(f"unknown transfer PO role {role!r}")
    return (
        f"JT|{role}|root={int(root_mr_id)}|mr={int(mr_id)}"
        f"|srcpo={int(src_po_id or 0)}|"
        + (_original_field(original) if role == ROLE_FINAL else "")
    )


def parse_marker(text) -> Optional[dict]:
    """Decode a marker; None when `text` is not a transfer PO marker."""
    if not isinstance(text, str):
        return None
    m = _MARKER_RE.match(text)
    if not m:
        return None
    src_po = int(m.group(4))
    out = {
        "role": m.group(1),
        "root_mr_id": int(m.group(2)),
        "mr_id": int(m.group(3)),
        "src_po_id": src_po or None,
        "original": None,
    }
    o = _ORIGINAL_RE.search(text, m.end() - 1)
    if o:
        out["original"] = {
            "party_id": o.group(1) or None,          # jute_mr.party_id is VARCHAR
            "party_branch_id": int(o.group(2)) if o.group(2) else None,
            "jute_mr_date": date.fromisoformat(o.group(3)) if o.group(3) else None,
        }
    return out


def final_marker_like(root_mr_id: int) -> str:
    """SQL LIKE pattern matching the FINAL PO marker of one root MR."""
    return f"JT|{ROLE_FINAL}|root={int(root_mr_id)}|%"


CLOSE_REMARK_MAX_LEN = 500  # jute_po.close_remark / jute_po_status_log.remark


def _dmy(d) -> str:
    """dd-mm-yyyy, or '?' when the date is missing."""
    if d is None or d != d:
        return "?"
    if hasattr(d, "date") and callable(d.date):
        d = d.date()
    return d.strftime("%d-%m-%Y") if hasattr(d, "strftime") else str(d)


def _lorry_label(ge_no, ge_date) -> str:
    n = _num(ge_no)
    return f"lorry GE {int(n) if n is not None else '?'} dt {_dmy(ge_date)}"


def _name(value, fallback: str) -> str:
    """A company / party name ready to end a sentence ('... PVT. LTD.' keeps
    one full stop, not two)."""
    name = value.strip() if isinstance(value, str) else ""
    return name.rstrip(". ") if name else fallback


def po_remarks(role: str) -> str:
    """jute_po.remarks: shown on the ERP PO form and print, so a printed
    transfer PO says what it is."""
    kind = "Forwarding" if role == ROLE_FORWARD else "Final"
    return f"Transfer PO ({kind}) created by Jute Transfer - inter-company transfer of one lorry."


def forward_close_remark(ge_no, ge_date, mill_name, original_po_no) -> str:
    """What the ERP shows as the Remark of a Forwarding PO's Closed panel --
    the one place a transfer PO can explain itself to an ERP user."""
    text = (
        f"Transfer PO (Forwarding). Created by Jute Transfer for "
        f"{_lorry_label(ge_no, ge_date)} received at "
        f"{_name(mill_name, 'the mill')}. "
        f"Original PO {original_po_no or 'none'}. Do not reopen."
    )
    return text[:CLOSE_REMARK_MAX_LEN]


def final_close_remark(ge_no, ge_date, forwarder_name, original_po_no) -> str:
    """Remark of a Final PO: says why it never shows a receipt."""
    text = (
        f"Transfer PO (Final). Created by Jute Transfer when "
        f"{_lorry_label(ge_no, ge_date)} came back from "
        f"{_name(forwarder_name, 'the forwarding company')}. The lorry was received "
        f"on original PO {original_po_no or 'none'}, so this PO shows no "
        f"receipt. Do not reopen."
    )
    return text[:CLOSE_REMARK_MAX_LEN]


def fy_label(ref_date: date) -> str:
    """'26-27' for the Apr-Mar financial year containing ref_date."""
    start = ref_date.year if ref_date.month >= 4 else ref_date.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


def format_po_no(po_no, co_prefix, branch_prefix, po_date) -> str:
    """Printed PO number, as the ERP builds it at read time:
    <co>/<branch>/JPO/<FY>/<00000>, dropping an empty prefix part.
    '' when the number or date is missing."""
    n = _num(po_no)
    if not n or po_date is None or po_date != po_date:
        return ""
    if hasattr(po_date, "date") and callable(po_date.date):
        po_date = po_date.date()
    parts = [p.strip() for p in (co_prefix, branch_prefix)
             if isinstance(p, str) and p.strip()]
    parts.extend(["JPO", fy_label(po_date), f"{int(n):05d}"])
    return "/".join(parts)
