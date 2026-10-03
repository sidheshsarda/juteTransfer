"""Pure shaping for the PO Tracker page (no Streamlit / DB imports).

The tracker shows one row per transferred lorry -- a vertical-chain root MR --
next to its three purchase orders:

* Original PO   -- the mill's ERP PO on the outside supplier (root.po_id);
* Forwarding PO -- the transfer PO of each hop MR (hop.po_id, verified by
                   its JT|FORWARD marker);
* Final PO      -- the mill's transfer PO on the last forwarding company,
                   found only through its JT|FINAL marker (the root MR keeps
                   its Original PO).

Everything here works on plain dicts with None for a missing value
(records() converts the query frames), so it runs without a database:
status model, PO numbers, reconciliation math and its one-sentence split,
search, period / mill options, "PO Lorries n of m" and the table / CSV
frames. The page (pages/po_tracker.py) only reads state and renders.
"""

import re
import zlib
from datetime import date, datetime
from typing import Optional

import pandas as pd

from .po_helpers import (
    BALE_KG,
    LOOSE_KG,
    PO_RATE_STEP,
    PO_STATUS_CLOSED,
    ROLE_FINAL,
    ROLE_FORWARD,
    UOM_BALE,
    UOM_LOOSE,
    format_po_no,
    fy_label,
    parse_marker,
    unit_kg,
)

# --- vocabulary --------------------------------------------------------------

STATUS_CHECK = "Check"
# With transfer-PO creation switched on a save can never leave a hop without
# its PO (any failure rolls the whole step back), so a lorry without one was
# transferred before the feature (or while it was switched off) and waits for
# the one-time backfill: expected, not something to look into.
STATUS_NO_PO = "No PO yet"
STATUS_COMPLETED = "Completed"
STATUS_AWAITING = "Awaiting return"
STATUS_OTHER = "Other"            # shown as "Other (<MR status name>)"

SHOW_ALL = "All"
SHOW_AWAITING = "Awaiting return"
# Not "Returned": that is the ERP's name of MR status 48 (a lorry sent back to
# the supplier). This chip lists the chains that came back to the mill.
SHOW_BACK = "Back at mill"
SHOW_ATTENTION = "Needs attention"
SHOW_OPTIONS = [SHOW_ALL, SHOW_AWAITING, SHOW_BACK, SHOW_ATTENTION]

VIEW_LORRIES = "Lorries"
VIEW_POS = "Original POs"
VIEW_OPTIONS = [VIEW_LORRIES, VIEW_POS]

CELL_NOT_YET = "not yet"          # a PO the backfill has still to create
CELL_NA = "n/a"                   # nothing to order: no accepted weight
CELL_AWAITING = "awaiting"        # chain not back at the mill yet
CELL_NONE = "—"

MR_STATUS_APPROVED = 3            # root after finalize: the lorry is back at the mill
MR_STATUS_PENDING = 13            # root while the chain is out

# A PO rate is the MR rate rounded to the closest 50, so the two can differ
# by 25 at most; a PO line is the nearest whole bale / loose unit, so its
# weight can differ by half a unit at most. Anything beyond is not rounding.
RATE_TOLERANCE = PO_RATE_STEP / 2.0
_EPS = 0.005

ROUNDING_CAPTION = (
    f"PO weight is in whole bales ({BALE_KG} kg) or loose units ({LOOSE_KG} kg). "
    f"PO rate is rounded to the nearest ₹{PO_RATE_STEP}. The MR and the "
    "invoice keep exact figures."
)

_PO_STATUS_NAMES = {1: "Open", 3: "Approved", 4: "Rejected", 5: "Closed",
                    6: "Cancelled", 21: "Draft"}
_CLOSE_TYPE_NAMES = {"AUTO": "auto-settled", "SHORT": "short-closed",
                     "BULK": "bulk-closed", "MIGRATED": "migrated",
                     "TRANSFER": "transfer PO"}
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# --- value plumbing ----------------------------------------------------------

def _missing(value) -> bool:
    if value is None or value is pd.NaT or value is pd.NA:
        return True
    return isinstance(value, float) and value != value


def records(df) -> list:
    """Query frame -> list of dicts with None for every NULL (NaN / NaT)."""
    if df is None or len(df) == 0:
        return []
    return [
        {k: (None if _missing(v) else v) for k, v in row.items()}
        for row in df.to_dict("records")
    ]


def _f(value) -> Optional[float]:
    if _missing(value):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _i(value) -> Optional[int]:
    """int for ids / numbers that may arrive as float (13314.0), Decimal or
    a VARCHAR ('8646')."""
    f = _f(value)
    return None if f is None else int(f)


def _s(value) -> str:
    return "" if _missing(value) else str(value).strip()


def as_date(value) -> Optional[date]:
    if _missing(value):
        return None
    if isinstance(value, datetime):          # pd.Timestamp included
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _group(rows, key) -> dict:
    out = {}
    for r in rows or []:
        k = _i(r.get(key))
        if k is not None:
            out.setdefault(k, []).append(r)
    return out


# --- formatting --------------------------------------------------------------

def fmt_num(value, blank: str = CELL_NONE) -> str:
    """Whole number with the grouping the other pages use ({:,.0f})."""
    f = _f(value)
    if f is None:
        return blank
    text = f"{f:,.0f}"
    return "0" if text == "-0" else text


def fmt_signed(value, blank: str = CELL_NONE) -> str:
    """'+4,454', '-1,491', '0'."""
    f = _f(value)
    if f is None:
        return blank
    text = f"{abs(f):,.0f}"
    if text == "0":
        return "0"
    return ("+" if f > 0 else "-") + text


def fmt_date(value, blank: str = CELL_NONE) -> str:
    """31-Aug-2026."""
    d = as_date(value)
    return blank if d is None else f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def fmt_day(value, blank: str = "?") -> str:
    """31 Aug (the table's Lorry cell; the year is in the Period filter)."""
    d = as_date(value)
    return blank if d is None else f"{d.day} {_MONTHS[d.month - 1]}"


def short_po_no(co_prefix, po_no) -> str:
    """'EJM 11' -- how a PO is named inside the tables."""
    n = _i(po_no)
    if n is None:
        return ""
    return f"{_s(co_prefix)} {n}".strip()


def full_po_no(po_no, co_prefix, branch_prefix, po_date, po_id=None) -> str:
    """The ERP's printed number (EJM/F/JPO/26-27/00011); '#<id>' when the
    number or date is missing so a PO never shows as blank."""
    text = format_po_no(_f(po_no), co_prefix if isinstance(co_prefix, str) else None,
                        branch_prefix if isinstance(branch_prefix, str) else None,
                        as_date(po_date))
    if text:
        return text
    pid = _i(po_id)
    return f"#{pid}" if pid is not None else ""


def rate_range(rates, sep: str = "–", grouped: bool = True) -> str:
    """'12,850', or '11,800–12,500' when the lines carry several rates."""
    vals = sorted({round(r) for r in (_f(x) for x in rates) if r is not None})
    if not vals:
        return ""
    show = (lambda v: f"{v:,.0f}") if grouped else (lambda v: f"{v:.0f}")
    if vals[0] == vals[-1]:
        return show(vals[0])
    return f"{show(vals[0])}{sep}{show(vals[-1])}"


def po_number_ranges(pos) -> str:
    """Short PO numbers of several POs in one cell: 'JTSPL 4–8' when the
    numbers run on, 'JTSPL 4–6, 9' / 'JTSPL 4, 9' otherwise; companies are
    kept apart with '; '. `pos` is an iterable of (co_prefix, po_no)."""
    by_prefix = {}
    for prefix, no in pos:
        n = _i(no)
        if n is not None:
            by_prefix.setdefault(_s(prefix), set()).add(n)
    parts = []
    for prefix in sorted(by_prefix):
        nums = sorted(by_prefix[prefix])
        runs, start, prev = [], nums[0], nums[0]
        for n in nums[1:]:
            if n == prev + 1:
                prev = n
                continue
            runs.append((start, prev))
            start = prev = n
        runs.append((start, prev))
        text = ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in runs)
        parts.append(f"{prefix} {text}".strip())
    return "; ".join(parts)


def po_status_label(status_id, close_type=None) -> str:
    """'Approved', 'Closed – auto-settled', ... as the ERP words them."""
    s = _i(status_id)
    if s is None:
        return CELL_NONE
    if s == PO_STATUS_CLOSED and _s(close_type):
        kind = _s(close_type).upper()
        return f"Closed – {_CLOSE_TYPE_NAMES.get(kind, kind.lower())}"
    return _PO_STATUS_NAMES.get(s, f"Status {s}")


def mr_status_label(status_id, status_name=None) -> str:
    name = _s(status_name)
    if name:
        return name.title()
    s = _i(status_id)
    return CELL_NONE if s is None else f"Status {s}"


def md_escape(text) -> str:
    """Party and supplier names go into st.markdown; keep '*', '_', '$' ...
    from being read as formatting."""
    return re.sub(r"([\\`*_\[\]$~])", r"\\\1", _s(text))


def grid_key(prefix: str, *parts) -> str:
    """Widget key that changes with the rows shown, so a positional row
    selection cannot survive a filter change and land on another lorry
    (same trick as _lot_grid in pages/warehouse_stock.py)."""
    digest = zlib.crc32(repr(parts).encode("utf-8")) & 0xFFFFFFFF
    return f"{prefix}_{digest:08x}"


# --- financial year / period / mill options ----------------------------------

def fy_start_year(d: date) -> int:
    """2026 for any date in the Apr 2026 - Mar 2027 financial year."""
    return d.year if d.month >= 4 else d.year - 1


def fy_text(start_year: int) -> str:
    """'26-27' for the financial year starting in April 2026."""
    return fy_label(date(start_year, 4, 1))


def month_code(d: date) -> str:
    return f"M:{d.year:04d}-{d.month:02d}"


def fy_code(start_year: int) -> str:
    return f"FY:{start_year:04d}"


def period_fy(code) -> Optional[int]:
    """Financial year (start year) a period code belongs to."""
    if not isinstance(code, str):
        return None
    if code.startswith("FY:"):
        return int(code[3:])
    if code.startswith("M:"):
        year, month = int(code[2:6]), int(code[7:9])
        return fy_start_year(date(year, month, 1))
    return None


def in_period(value, code) -> bool:
    d = as_date(value)
    if d is None or not isinstance(code, str):
        return False
    if code.startswith("FY:"):
        return fy_start_year(d) == int(code[3:])
    return month_code(d) == code


def period_file_part(code) -> str:
    """'2026-09' / 'FY26-27' for the CSV file name."""
    if isinstance(code, str) and code.startswith("M:"):
        return code[2:]
    fy = period_fy(code)
    return "all" if fy is None else f"FY{fy_text(fy)}"


def _distinct_lorries(chain_rows, mill_co_id=None) -> dict:
    """{root_mr_id: (gate-entry date, mill co_id)} -- one entry per lorry
    however many hop rows it has."""
    out = {}
    for r in chain_rows or []:
        root = _i(r.get("root_mr_id"))
        d = as_date(r.get("lorry_date"))
        if root is None or d is None:
            continue
        co = _i(r.get("mill_co_id"))
        if mill_co_id and co != mill_co_id:
            continue
        out[root] = (d, co)
    return out


def period_options(chain_rows, mill_co_id=None) -> list:
    """[(code, label)] for the Period control: per financial year (newest
    first) the months that have transferred lorries, newest first, as
    'Sep 2026 (61)', then 'FY 26-27 – all (176)'. Months and years without a
    lorry never appear, so the control cannot land on an empty month."""
    months, years = {}, {}
    for d, _co in _distinct_lorries(chain_rows, mill_co_id).values():
        months[(d.year, d.month)] = months.get((d.year, d.month), 0) + 1
        fy = fy_start_year(d)
        years[fy] = years.get(fy, 0) + 1
    out = []
    for fy in sorted(years, reverse=True):
        for (y, m) in sorted(months, reverse=True):
            if fy_start_year(date(y, m, 1)) == fy:
                out.append((month_code(date(y, m, 1)),
                            f"{_MONTHS[m - 1]} {y} ({months[(y, m)]})"))
        out.append((fy_code(fy), f"FY {fy_text(fy)} – all ({years[fy]})"))
    return out


def default_period(options, today=None) -> Optional[str]:
    """Newest month that has rows (not later than the database's current
    month when that is known); the newest entry when there is no month."""
    codes = [c for c, _label in options]
    month_codes = [c for c in codes if c.startswith("M:")]
    t = as_date(today)
    if t is not None:
        current = month_code(t)
        for c in month_codes:
            if c <= current:
                return c
    if month_codes:
        return month_codes[0]
    return codes[0] if codes else None


def mill_options(chain_rows, fy=None) -> list:
    """[(co_id, label)]: (0, 'All mills (176)') then every origin company
    that has at least one chain, as 'EJM – THE EMPIRE JUTE COMPANY LTD. (66)'.
    Built from the chains, not from the company master; counts are lorries
    in financial year `fy` (all years when None)."""
    names, counts, total = {}, {}, 0
    for r in chain_rows or []:
        co = _i(r.get("mill_co_id"))
        if co is not None and co not in names:
            names[co] = (_s(r.get("mill_prefix")), _s(r.get("mill_name")))
    for d, co in _distinct_lorries(chain_rows).values():
        if fy is not None and fy_start_year(d) != fy:
            continue
        total += 1
        counts[co] = counts.get(co, 0) + 1
    out = [(0, f"All mills ({total})")]
    for co in sorted(names, key=lambda c: (names[c][0], c)):
        prefix, name = names[co]
        label = " – ".join(p for p in (prefix, name) if p) or f"Company {co}"
        out.append((co, f"{label} ({counts.get(co, 0)})"))
    return out


def forwarder_options(lorries) -> list:
    """[(co_id, label)] for the 'Forwarding company' filter: (0, 'All
    forwarding companies') then each forwarder among `lorries` with its
    lorry count."""
    names, counts = {}, {}
    for l in lorries:
        seen = set()
        for hop in l["hops"]:
            co = hop["co_id"]
            if co is None or co in seen:
                continue
            seen.add(co)
            names.setdefault(co, (hop["co_prefix"], hop["co_name"]))
            counts[co] = counts.get(co, 0) + 1
    out = [(0, "All forwarding companies")]
    for co in sorted(names, key=lambda c: (names[c][0], c)):
        label = " – ".join(p for p in names[co] if p) or f"Company {co}"
        out.append((co, f"{label} ({counts[co]})"))
    return out


# --- status model ------------------------------------------------------------

def needs_attention(status) -> bool:
    """Check and Other need a look. Completed and Awaiting return are the two
    normal states, and No PO yet is expected: the lorry waits for the
    one-time backfill (see STATUS_NO_PO)."""
    return status not in (STATUS_COMPLETED, STATUS_AWAITING, STATUS_NO_PO)


def is_back_at_mill(root_status_id, root_mr_no) -> bool:
    """The chain has come back to the mill: the root MR is Approved (3) AND
    carries its MR number (branch_mr_no) -- the rule the backfill and
    po_ops.plan_final_po use for "finalized". A lorry the tracker calls back
    at the mill is therefore always one the backfill gives a Final PO."""
    return _i(root_status_id) == MR_STATUS_APPROVED and _i(root_mr_no) is not None


def matches_show(lorry: dict, show) -> bool:
    """The Show chips. 'Awaiting return' and 'Back at mill' go by where the
    chain is (still out / back at the mill), whatever state its POs are in
    -- so the lorries already back show under 'Back at mill' even before the
    backfill gives them POs. 'Needs attention' goes by the status."""
    if show == SHOW_AWAITING:
        return bool(lorry["awaiting"])
    if show == SHOW_BACK:
        return bool(lorry["returned"])
    if show == SHOW_ATTENTION:
        return needs_attention(lorry["status"])
    return True


def _status_rank(status) -> int:
    """Worst first: Check, Other (...), No PO yet, Awaiting return, Completed."""
    if status == STATUS_CHECK:
        return 0
    if status == STATUS_NO_PO:
        return 2
    if status == STATUS_AWAITING:
        return 3
    if status == STATUS_COMPLETED:
        return 4
    return 1                                   # Other (...)


def worst_status(statuses) -> str:
    """The status an Original PO row shows for its lorries."""
    statuses = list(statuses)
    return min(statuses, key=_status_rank) if statuses else CELL_NONE


def lorry_status(root_status_id, root_status_name, hop_states, final_state) -> tuple:
    """(status, reason) of one lorry. First match wins:

    Check           a PO that exists is wrong (see _hop_state / _final_state)
    Other (name)    the mill MR is neither Pending (chain out) nor back at the
                    mill (Approved with its MR number, is_back_at_mill)
    No PO yet       a hop with accepted weight has no po_id, or the lorry is
                    back at the mill without a Final PO -- the backfill's job
    Completed       back at the mill and every PO present
    Awaiting return root status 13 -- the normal state of a chain that is out

    Other comes before No PO yet: a missing PO is expected until the
    backfill has run, an odd mill MR is not and must not hide behind it.
    """
    checks = [c for h in hop_states for c in h["checks"]] + list(final_state["checks"])
    if checks:
        return STATUS_CHECK, "; ".join(checks)
    status = _i(root_status_id)
    back = bool(final_state["returned"])
    if status != MR_STATUS_PENDING and not back:
        name = mr_status_label(root_status_id, root_status_name)
        if status == MR_STATUS_APPROVED:
            reason = ("The lorry's MR at the mill is Approved but has no MR number, so it "
                      "does not count as back at the mill and gets no Final PO (saving the "
                      "step back to the mill on the Transfer Chain page numbers the MR)")
        else:
            reason = (f"The lorry's MR at the mill is {name}; a transferred lorry is "
                      "Pending while it is out and Approved once it is back at the mill")
        return f"{STATUS_OTHER} ({name})", reason
    fwd_missing = any(h["missing"] for h in hop_states)
    if fwd_missing or final_state["missing"]:
        what = " and ".join(
            name for name, on in (("Forwarding PO", fwd_missing),
                                  ("Final PO", final_state["missing"])) if on
        )
        return STATUS_NO_PO, f"{what} not created yet"
    if back:
        return STATUS_COMPLETED, ""
    return STATUS_AWAITING, ""


# --- reconciliation: PO lines against the MR lines they were built from -------

def is_active(line) -> bool:
    active = _f(line.get("active"))
    return active is None or int(active) == 1


def line_item_id(mr_line) -> Optional[int]:
    """The item a transfer PO line is written with (po_ops: actual item,
    else challan item)."""
    item = _i(mr_line.get("actual_item_id"))
    return item if item is not None else _i(mr_line.get("challan_item_id"))


def used_mr_lines(mr_lines) -> list:
    """Active MR lines that carry accepted weight -- the ones a transfer PO
    is built from -- in line-id order."""
    out = [l for l in mr_lines or []
           if is_active(l) and (_f(l.get("accepted_weight")) or 0.0) > 0]
    return sorted(out, key=lambda l: _i(l.get("jute_mr_li_id")) or 0)


def _is_unit_uom(uom) -> bool:
    return isinstance(uom, str) and uom.strip().upper() in (UOM_BALE, UOM_LOOSE)


def _recon_row(mr_line, po_line, po_uom, has_po: bool) -> dict:
    mr_kg = mr_rate = mr_amount = None
    if mr_line is not None:
        mr_kg = _f(mr_line.get("accepted_weight")) or 0.0
        mr_rate = _f(mr_line.get("rate")) or 0.0
        mr_amount = mr_kg * mr_rate / 100.0
    po_kg = po_rate = po_value = po_units = unit = uom = None
    if po_line is not None:
        uom = po_line.get("jute_uom") if _is_unit_uom(po_line.get("jute_uom")) else po_uom
        unit = unit_kg(uom)
        po_units = _f(po_line.get("quantity")) or 0.0
        po_kg = po_units * unit
        po_rate = _f(po_line.get("rate")) or 0.0
        stored = _f(po_line.get("value"))
        po_value = stored if stored is not None else po_kg * po_rate / 100.0
    paired = mr_line is not None and po_line is not None
    diff = weight_part = rate_part = unmatched = None
    if has_po:
        # An absent side counts as nothing ordered / nothing received.
        diff = (po_value or 0.0) - (mr_amount or 0.0)
        if paired:
            # weight part + rate part = the line's difference
            weight_part = (po_kg - mr_kg) / 100.0 * po_rate
            rate_part = mr_kg / 100.0 * (po_rate - mr_rate)
        else:
            # A line without its counterpart is neither whole-unit nor rate
            # rounding: its whole difference is 'unmatched'.
            unmatched = diff
    src = mr_line if mr_line is not None else po_line
    return {
        "quality": _s(src.get("item_name")) or "Item",
        "mr_kg": mr_kg, "po_kg": po_kg, "po_units": po_units,
        "unit_kg": unit, "uom": uom.strip().upper() if _is_unit_uom(uom) else None,
        "mr_rate": mr_rate, "po_rate": po_rate,
        "mr_amount": mr_amount, "po_value": po_value, "diff": diff,
        "paired": paired, "weight_part": weight_part, "rate_part": rate_part,
        "unmatched": unmatched,
        "jute_mr_li_id": None if mr_line is None else _i(mr_line.get("jute_mr_li_id")),
        "jute_po_li_id": None if po_line is None else _i(po_line.get("jute_po_li_id")),
    }


def reconcile(mr_lines, po_lines, po_uom=None) -> list:
    """Reconciliation rows of one PO against the MR it mirrors: one row per
    MR line with accepted weight, paired with its PO line.

    Pairing: the stored link (jute_mr_li.jute_po_li_id, set on hop lines)
    first; then same item in line-id order (a Final PO is not linked from
    the root's lines, but is written one PO line per MR line in that order).
    What stays unpaired gets a row of its own with the other side empty,
    listed after the paired rows: MR lines without a PO line, then PO lines
    without an MR line. Only paired rows are split into a weight part and a
    rate part; an unpaired row's difference is 'unmatched'.

    `po_lines` None = the PO does not exist: the rows carry the MR side only.
    Compares kg, never bale / unit counts: an MR counts its bales as
    weighed, a PO counts nominal 150 / 48 kg units.
    """
    has_po = po_lines is not None
    po = sorted([l for l in (po_lines or []) if is_active(l)],
                key=lambda l: _i(l.get("jute_po_li_id")) or 0)
    mr_active = sorted([l for l in (mr_lines or []) if is_active(l)],
                       key=lambda l: _i(l.get("jute_mr_li_id")) or 0)
    po_by_id = {_i(p.get("jute_po_li_id")): p for p in po}
    taken, pairs, unlinked = set(), [], []
    for m in mr_active:
        link = _i(m.get("jute_po_li_id"))
        if link is not None and link in po_by_id and link not in taken:
            taken.add(link)
            pairs.append((m, po_by_id[link]))
        else:
            unlinked.append(m)
    rest = [p for p in po if _i(p.get("jute_po_li_id")) not in taken]
    for m in unlinked:
        if (_f(m.get("accepted_weight")) or 0.0) <= 0:
            continue                      # nothing was ordered for an empty line
        item = line_item_id(m)
        hit = next((p for p in rest if _i(p.get("item_id")) == item), None)
        if hit is not None:
            rest.remove(hit)
        pairs.append((m, hit))
    pairs.sort(key=lambda mp: _i(mp[0].get("jute_mr_li_id")) or 0)
    ordered = ([mp for mp in pairs if mp[1] is not None]
               + [mp for mp in pairs if mp[1] is None]
               + [(None, p) for p in rest])
    return [_recon_row(m, p, po_uom, has_po) for m, p in ordered]


def recon_flags(rows) -> list:
    """What rounding cannot explain, in words (the 'Check' reasons): a line
    without a counterpart, a weight more than half a unit off, a rate more
    than 25 off."""
    out = []
    for r in rows:
        q = r["quality"]
        if r["po_kg"] is None:
            out.append(f"{q}: {fmt_num(r['mr_kg'])} kg on the MR has no PO line")
            continue
        if r["mr_kg"] is None:
            out.append(f"{q}: PO line of {fmt_num(r['po_kg'])} kg has no MR line")
            continue
        unit = r["unit_kg"] or 0
        off = abs(r["po_kg"] - r["mr_kg"])
        # A PO line is never less than one unit, so a lot lighter than half
        # a unit legitimately sits further away.
        one_unit_floor = r["po_units"] == 1 and r["mr_kg"] < unit / 2.0
        if off > unit / 2.0 + _EPS and not one_unit_floor:
            out.append(
                f"{q}: PO {fmt_num(r['po_kg'])} kg against MR "
                f"{fmt_num(r['mr_kg'])} kg is more than half a "
                f"{'bale' if unit == BALE_KG else 'loose unit'} apart"
            )
        if abs(r["po_rate"] - r["mr_rate"]) > RATE_TOLERANCE + _EPS:
            out.append(
                f"{q}: PO rate {fmt_num(r['po_rate'])} against MR rate "
                f"{fmt_num(r['mr_rate'])} is more than {RATE_TOLERANCE:.0f} apart"
            )
    return out


def recon_totals(rows) -> dict:
    """Column sums (None when no row has the figure). weight_part and
    rate_part cover the paired rows only, unmatched the unpaired ones, so
    weight_part + rate_part + unmatched = diff."""
    def total(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return sum(vals) if vals else None
    return {k: total(k) for k in ("mr_kg", "po_kg", "po_units", "mr_amount", "po_value",
                                  "diff", "weight_part", "rate_part", "unmatched")}


RATE_PART_LABEL = f"rate to nearest ₹{PO_RATE_STEP}"


def difference_sentence(po_value, base_amount, totals: dict,
                        base_label: str = "MR", uom=None) -> str:
    """'PO 1,390,200 vs invoice 1,385,746: +4,454 (+0.32 %) — whole bales
    +5,945 · rate to nearest ₹50: -1,491.'

    `totals` = recon_totals of the PO's reconciliation rows. The difference
    is told in parts that are each exact and together make it up:

      whole bales / loose units  (PO kg - MR kg) / 100 x PO rate    } paired
      rate to nearest ₹50        MR kg / 100 x (PO rate - MR rate)  } lines
      unmatched lines            lines without a counterpart, whole difference
      PO total vs its lines      the PO's value minus its lines (0 unless edited)
      MR lines vs <base>         the MR lines minus the base (e.g. the invoice)
      other                      anything left (0 for a PO po_ops wrote)

    The two rounding parts are shown whenever a line is paired, the rate as
    'rate unchanged' when it is 0; the others only when they are not 0.
    Figures are whole rupees and add up to the printed difference: what
    rounding leaves over goes to the largest part, never to a part that is
    exactly 0."""
    po = _f(po_value) or 0.0
    base = _f(base_amount) or 0.0
    total = round(po) - round(base)
    weight, rate = _f(totals.get("weight_part")), _f(totals.get("rate_part"))
    paired = weight is not None or rate is not None
    parts = []                                    # [label, exact]
    if paired:
        units = "whole bales" if _s(uom).upper() == UOM_BALE else "whole loose units"
        parts += [[units, weight or 0.0], [RATE_PART_LABEL, rate or 0.0]]
    parts += [["unmatched lines", _f(totals.get("unmatched")) or 0.0],
              ["PO total vs its lines", po - (_f(totals.get("po_value")) or 0.0)],
              [f"MR lines vs {base_label}", (_f(totals.get("mr_amount")) or 0.0) - base]]
    parts.append(["other", (po - base) - sum(exact for _label, exact in parts)])
    shown = [round(exact) for _label, exact in parts]
    left = total - sum(shown)
    if left:
        biggest = max(range(len(parts)), key=lambda i: abs(parts[i][1]))
        shown[biggest] += left
    pieces = []
    for i, ((label, _exact), value) in enumerate(zip(parts, shown)):
        if paired and i == 0:
            pieces.append(f"{label} {fmt_signed(value)}")
        elif paired and i == 1:
            # a colon, so '₹50' and the figure never read as one number
            pieces.append(f"{label}: {fmt_signed(value)}" if value else "rate unchanged")
        elif value:
            pieces.append(f"{label} {fmt_signed(value)}")
    pct = ""
    if round(base):
        pct = f" ({round(total / round(base) * 100.0, 2) + 0.0:+.2f} %)"
    text = f"PO {fmt_num(po)} vs {base_label} {fmt_num(base)}: {fmt_signed(total)}{pct}"
    if pieces:
        text += " — " + " · ".join(pieces)
    return text + "."


# --- "PO Lorries n of m" -------------------------------------------------------

def po_lorry_positions(siblings) -> dict:
    """{jute_mr_id: (n, m)}: the lorry is the n-th of the m lorries received
    on its Original PO, by gate-entry date (then number, then id).

    `siblings` = every jute_mr with a gate-entry number on those POs -- the
    same rows the ERP's PO list counts as 'Lorries Received'."""
    out = {}
    for rows in _group(siblings, "po_id").values():
        ordered = sorted(rows, key=lambda r: (
            as_date(r.get("jute_gate_entry_date")) or date.max,
            _i(r.get("jute_gate_entry_no")) or 0,
            _i(r.get("jute_mr_id")) or 0,
        ))
        for n, r in enumerate(ordered, start=1):
            mr_id = _i(r.get("jute_mr_id"))
            if mr_id is not None:
                out[mr_id] = (n, len(ordered))
    return out


# --- one lorry -----------------------------------------------------------------

def _po_kind_cells(pos) -> str:
    return ", ".join(p["short"] or p["full"] for p in pos)


def _hop_state(h: dict, root_id: int, lines_by_mr: dict, lines_by_po: dict) -> dict:
    """One hop MR with its Forwarding PO (or the reason it has none)."""
    hop_id = _i(h.get("hop_mr_id"))
    mr_lines = lines_by_mr.get(hop_id, [])
    used = used_mr_lines(mr_lines)
    po_id = _i(h.get("fwd_po_id"))
    found = po_id is not None and _i(h.get("fwd_po_row_id")) is not None
    co_prefix = _s(h.get("hop_co_prefix"))
    mr_no = _i(h.get("hop_mr_no"))
    where = f"MR {mr_no if mr_no is not None else hop_id} at {co_prefix or 'the forwarder'}"

    po, checks, missing, is_transfer_po = None, [], False, False
    if po_id is None:
        missing = bool(used)
        cell = CELL_NOT_YET if missing else CELL_NA
    elif not found:
        cell = f"#{po_id}"
        checks.append(f"{where} is linked to PO id {po_id}, which no longer exists")
    else:
        po = {
            "id": po_id,
            "no": _i(h.get("fwd_po_no")),
            "co_prefix": _s(h.get("fwd_po_co_prefix")),
            "short": short_po_no(h.get("fwd_po_co_prefix"), h.get("fwd_po_no")),
            "full": full_po_no(h.get("fwd_po_no"), h.get("fwd_po_co_prefix"),
                               h.get("fwd_po_branch_prefix"), h.get("fwd_po_date"), po_id),
            "date": as_date(h.get("fwd_po_date")),
            "status_id": _i(h.get("fwd_po_status_id")),
            "status": po_status_label(h.get("fwd_po_status_id"), h.get("fwd_po_close_type")),
            "uom": _s(h.get("fwd_po_uom")).upper() or None,
            "kg": _f(h.get("fwd_po_weight")),
            "value": _f(h.get("fwd_po_value")),
        }
        cell = po["short"] or po["full"]
        marker = parse_marker(h.get("fwd_po_note"))
        if marker is None:
            checks.append(f"{where} is linked to PO {po['full']}, which is not a "
                          "transfer PO (no Jute Transfer marker)")
        elif (marker["role"] != ROLE_FORWARD or marker["mr_id"] != hop_id
              or marker["root_mr_id"] != root_id):
            checks.append(f"{where} is linked to PO {po['full']}, whose marker "
                          f"names another MR ({marker['role'].lower()} of MR id "
                          f"{marker['mr_id']})")
        else:
            is_transfer_po = True
            if po["status_id"] != PO_STATUS_CLOSED:
                checks.append(f"Forwarding PO {po['full']} is no longer Closed "
                              f"(now {po['status']}) — reopened in the ERP")
    # Lines are compared only with this hop's own transfer PO. A hand-linked
    # ERP PO (or another MR's transfer PO) is written with other items, so
    # pairing its lines would only print differences that mean nothing: the
    # rows then carry the MR side alone, as when there is no PO.
    po_lines = lines_by_po.get(po_id, []) if is_transfer_po else None
    recon = reconcile(mr_lines, po_lines, None if po is None else po["uom"])
    if is_transfer_po:
        checks.extend(f"Forwarding PO {po['full']} — {text}" for text in recon_flags(recon))
        totals = recon_totals(recon)
        if po["kg"] is None:
            po["kg"] = totals["po_kg"]
        if po["value"] is None:
            po["value"] = totals["po_value"]
    return {
        "hop_mr_id": hop_id,
        "mr_no": mr_no,
        "mr_date": as_date(h.get("hop_mr_date")),
        "co_id": _i(h.get("hop_co_id")),
        "co_prefix": co_prefix,
        "co_name": _s(h.get("hop_co_name")),
        "party_name": _s(h.get("hop_party_name")),
        "mr_kg": sum(_f(l.get("accepted_weight")) or 0.0 for l in used),
        "mr_amount": sum((_f(l.get("accepted_weight")) or 0.0)
                         * (_f(l.get("rate")) or 0.0) / 100.0 for l in used),
        "has_weight": bool(used),
        "po": po,
        "is_transfer_po": is_transfer_po,
        "cell": cell,
        "missing": missing,
        "checks": checks,
        "recon": recon,
    }


def _final_po(tp: dict) -> dict:
    po_id = _i(tp.get("jute_po_id"))
    return {
        "id": po_id,
        "no": _i(tp.get("po_no")),
        "co_prefix": _s(tp.get("co_prefix")),
        "short": short_po_no(tp.get("co_prefix"), tp.get("po_no")),
        "full": full_po_no(tp.get("po_no"), tp.get("co_prefix"),
                           tp.get("branch_prefix"), tp.get("po_date"), po_id),
        "date": as_date(tp.get("po_date")),
        "status_id": _i(tp.get("status_id")),
        "status": po_status_label(tp.get("status_id"), tp.get("close_type")),
        "uom": _s(tp.get("jute_uom")).upper() or None,
        "kg": _f(tp.get("weight")),
        "value": _f(tp.get("jute_po_value")),
        "co_name": _s(tp.get("co_name")),
        "party_name": _s(tp.get("party_name")),
    }


def _final_state(finals: list, root_status_id, root_status_name, root_mr_no,
                 root_lines: list, lines_by_po: dict) -> dict:
    """The Final PO side of a lorry: the PO(s) carrying its JT|FINAL marker,
    or why there is none."""
    status = _i(root_status_id)
    returned = is_back_at_mill(root_status_id, root_mr_no)
    pos = [_final_po(tp) for tp in sorted(finals, key=lambda t: _i(t.get("jute_po_id")) or 0)]
    checks, missing, recon = [], False, []
    used = used_mr_lines(root_lines)
    if pos:
        cell = _po_kind_cells(pos)
        if len(pos) > 1:
            checks.append(f"{len(pos)} Final POs exist for this lorry "
                          f"({', '.join(p['full'] for p in pos)})")
        if not returned:
            state = mr_status_label(root_status_id, root_status_name)
            if status == MR_STATUS_APPROVED:
                state += " without an MR number"
            checks.append(f"Final PO {pos[0]['full']} exists but the lorry is not back "
                          f"at the mill (its MR is {state})")
        for p in pos:
            if p["status_id"] != PO_STATUS_CLOSED:
                checks.append(f"Final PO {p['full']} is no longer Closed "
                              f"(now {p['status']}) — reopened in the ERP")
        first = pos[0]
        recon = reconcile(root_lines, lines_by_po.get(first["id"], []), first["uom"])
        if returned:
            checks.extend(f"Final PO {first['full']} — {text}" for text in recon_flags(recon))
        totals = recon_totals(recon)
        if first["kg"] is None:
            first["kg"] = totals["po_kg"]
        if first["value"] is None:
            first["value"] = totals["po_value"]
    elif returned:
        missing = bool(used)
        cell = CELL_NOT_YET if missing else CELL_NA
        recon = reconcile(root_lines, None)
    elif status == MR_STATUS_PENDING:
        cell = CELL_AWAITING
    else:
        cell = CELL_NONE
    return {"pos": pos, "cell": cell, "missing": missing, "checks": checks,
            "recon": recon, "returned": returned,
            "mr_amount": sum((_f(l.get("accepted_weight")) or 0.0)
                             * (_f(l.get("rate")) or 0.0) / 100.0 for l in used)}


def _original_po(first: dict, lines_by_po: dict) -> Optional[dict]:
    po_id = _i(first.get("orig_po_id"))
    if po_id is None or _i(first.get("orig_po_no")) is None:
        return None
    lines = sorted([l for l in lines_by_po.get(po_id, []) if is_active(l)],
                   key=lambda l: _i(l.get("jute_po_li_id")) or 0)
    weight = _f(first.get("orig_po_weight"))
    # Two eras share jute_po.weight: ERP-made POs store kg, POs migrated from
    # the old system store quintals and are recognisable by a line unit that
    # is neither BALE nor LOOSE ('130').
    legacy = bool(lines) and any(not _is_unit_uom(l.get("jute_uom")) for l in lines)
    weight_kg = None if weight is None else (weight * 100.0 if legacy else weight)
    return {
        "id": po_id,
        "no": _i(first.get("orig_po_no")),
        "co_prefix": _s(first.get("orig_po_co_prefix")),
        "short": short_po_no(first.get("orig_po_co_prefix"), first.get("orig_po_no")),
        "full": full_po_no(first.get("orig_po_no"), first.get("orig_po_co_prefix"),
                           first.get("orig_po_branch_prefix"), first.get("orig_po_date"), po_id),
        "date": as_date(first.get("orig_po_date")),
        "status_id": _i(first.get("orig_po_status_id")),
        "status": po_status_label(first.get("orig_po_status_id"), first.get("orig_po_close_type")),
        "supplier_name": _s(first.get("orig_po_supplier_name")),
        "party_name": _s(first.get("orig_po_party_name")),
        "lorries_ordered": _i(first.get("orig_po_lorries")),
        "weight_kg": weight_kg,
        "value": _f(first.get("orig_po_value")),
        "lines": [{
            "quality": _s(l.get("item_name")) or "Item",
            "units": _f(l.get("quantity")),
            "uom": _s(l.get("jute_uom")),
            "rate": _f(l.get("rate")),
            "percentage": _f(l.get("percentage")),
        } for l in lines],
        "rates": [_f(l.get("rate")) for l in lines if _f(l.get("rate")) is not None],
    }


def _search_index(lorry: dict) -> dict:
    """What search_matches looks in: the numbers by kind (gate entry, MR, PO),
    the texts, the short PO numbers and the lorry number's letters and
    digits."""
    ge, mr, po_nos, texts, shorts = set(), set(), set(), [], set()

    def add_number(bucket, v):
        n = _i(v)
        if n is not None:
            bucket.add(n)

    def add_po(po):
        if po:
            add_number(po_nos, po.get("no"))
            full = po.get("full") or ""
            texts.append(full)
            if full.startswith("#"):         # shown as '#<id>' (no PO number)
                add_number(po_nos, po.get("id"))
            if po.get("short"):
                shorts.add(_norm_short(po["short"]))

    add_number(ge, lorry["ge_no"])
    add_number(mr, lorry["mill_mr_no"])
    add_po(lorry["orig"])
    for hop in lorry["hops"]:
        add_number(mr, hop["mr_no"])
        add_po(hop["po"])
        if hop["po"] is None and hop["cell"].startswith("#"):
            add_number(po_nos, hop["cell"][1:])   # the PO id of a PO that is gone
        texts.append(hop["party_name"])
    for po in lorry["final"]["pos"]:
        add_po(po)
    texts.extend([lorry["supplier"], lorry["party"], lorry["root_party_name"],
                  lorry["lorry_no"], lorry["invoice_no"]])
    return {
        "ge": ge, "mr": mr, "po": po_nos,
        "numbers": ge | mr | po_nos,
        "texts": [t.lower() for t in texts if t],
        "shorts": shorts,
        "vehicle": _alnum(lorry["lorry_no"]),
    }


def _build_lorry(root_id: int, rows: list, lines_by_mr: dict, lines_by_po: dict,
                 finals: list, positions: dict, siblings_by_po: dict, today) -> dict:
    first = rows[0]                     # the root's columns repeat on every hop row
    root_lines = lines_by_mr.get(root_id, [])
    hops = [_hop_state(h, root_id, lines_by_mr, lines_by_po)
            for h in sorted(rows, key=lambda r: _i(r.get("hop_mr_id")) or 0)]
    final = _final_state(finals, first.get("root_status_id"), first.get("root_status_name"),
                         first.get("root_mr_no"), root_lines, lines_by_po)
    status, reason = lorry_status(first.get("root_status_id"),
                                  first.get("root_status_name"), hops, final)
    orig = _original_po(first, lines_by_po)
    root_status = _i(first.get("root_status_id"))
    returned = final["returned"]
    ge_no = _i(first.get("ge_no"))
    ge_date = as_date(first.get("ge_date"))
    lorry_date = as_date(first.get("lorry_date")) or ge_date
    mill = _s(first.get("mill_prefix"))

    # Row figures describe the FIRST hop -- the purchase from the outside
    # supplier. Live chains have exactly one hop; with more, the later hops
    # are the same lorry sold on and would only double its weight.
    head = hops[0]
    fwd_po = head["po"] if head["is_transfer_po"] else None
    fwd_pos = [h["po"] for h in hops if h["po"] is not None]
    final_po = final["pos"][0] if final["pos"] else None

    last_amount = final["mr_amount"] if returned else hops[-1]["mr_amount"]
    markup = None
    if head["mr_amount"] > 0 and (returned or len(hops) > 1):
        markup = (last_amount / head["mr_amount"] - 1.0) * 100.0

    route = [mill or "?"] + [h["co_prefix"] or "?" for h in hops]
    if returned:
        route.append(mill or "?")

    days = None
    t = as_date(today)
    if root_status == MR_STATUS_PENDING and t is not None and head["mr_date"] is not None:
        days = (t - head["mr_date"]).days

    position = positions.get(root_id)
    inv_amt = _f(first.get("invoice_amount")) if returned else None
    lorry = {
        "root_mr_id": root_id,
        "root_status_id": root_status,
        "root_status": mr_status_label(root_status, first.get("root_status_name")),
        "returned": returned,
        "awaiting": root_status == MR_STATUS_PENDING,   # chain out, not back yet
        "ge_no": ge_no,
        "ge_date": ge_date,
        "lorry_date": lorry_date,
        "lorry": f"GE {ge_no if ge_no is not None else '?'} · {fmt_day(lorry_date)}",
        "mill_co_id": _i(first.get("mill_co_id")),
        "mill": mill,
        "mill_name": _s(first.get("mill_name")),
        "mill_mr_no": _i(first.get("root_mr_no")),
        "returned_date": as_date(first.get("root_mr_date")) if returned else None,
        "supplier": _s(first.get("broker_name")),
        "party": head["party_name"] or _s(first.get("root_party_name")),
        "root_party_name": _s(first.get("root_party_name")),
        "lorry_no": _s(first.get("vehicle_no")),
        "mr_kg": head["mr_kg"],
        "mr_amt": head["mr_amount"],
        "orig": orig,
        "orig_po_id": None if orig is None else orig["id"],
        "orig_po": CELL_NONE if orig is None else (orig["short"] or orig["full"]),
        "po_lorries": "" if position is None else f"{position[0]} of {position[1]}",
        "siblings": siblings_by_po.get(orig["id"], []) if orig else [],
        "hops": hops,
        "fwd_po": ", ".join(h["cell"] for h in hops),
        "fwd_po_count": sum(1 for h in hops if h["is_transfer_po"]),
        "fwd_co": ", ".join(dict.fromkeys(h["co_prefix"] for h in hops if h["co_prefix"])),
        "fwd_co_ids": [h["co_id"] for h in hops if h["co_id"] is not None],
        "fwd_mr": ", ".join(str(h["mr_no"]) for h in hops if h["mr_no"] is not None),
        "fwd_date": None if fwd_po is None else fwd_po["date"],
        "fwd_rates": [r["po_rate"] for r in head["recon"] if r["po_rate"] is not None] if fwd_po else [],
        "fwd_kg": None if fwd_po is None else fwd_po["kg"],
        "fwd_value": None if fwd_po is None else fwd_po["value"],
        "fwd_diff": (None if fwd_po is None or fwd_po["value"] is None
                     else fwd_po["value"] - head["mr_amount"]),
        "fwd_pos": fwd_pos,
        "final": final,
        "final_po": final["cell"],
        "final_po_count": len(final["pos"]),
        "final_date": None if final_po is None else final_po["date"],
        "final_rates": [r["po_rate"] for r in final["recon"] if r["po_rate"] is not None] if final_po else [],
        "final_kg": None if final_po is None else final_po["kg"],
        "final_value": None if final_po is None else final_po["value"],
        "invoice_no": _s(first.get("invoice_no")) if returned else "",
        "inv_amt": inv_amt,
        "final_diff": (None if final_po is None or final_po["value"] is None or inv_amt is None
                       else final_po["value"] - inv_amt),
        "days": days,
        "status": status,
        "status_reason": reason,
        "route": " → ".join(route),
        "markup_pct": markup,
    }
    lorry["search"] = _search_index(lorry)
    return lorry


def build_lorries(hops, transfer_pos, mr_lines, po_lines, siblings, today=None) -> list:
    """One dict per transferred lorry (chain root) of the loaded financial
    year, newest gate entry first -- everything the table, the drill-down
    and the CSVs show, so selecting a row needs no further query.

    Arguments are records() of the tracker queries: `hops` one row per hop
    MR (root, Original PO and Forwarding PO headers joined), `transfer_pos`
    every PO carrying a JT| marker, `mr_lines` / `po_lines` the lines of
    those MRs / POs, `siblings` the MRs received on the Original POs."""
    lines_by_mr = _group(mr_lines, "jute_mr_id")
    lines_by_po = _group(po_lines, "jute_po_id")
    siblings_by_po = _group(siblings, "po_id")
    positions = po_lorry_positions(siblings)
    finals_by_root = {}
    for tp in transfer_pos or []:
        marker = parse_marker(tp.get("internal_note"))
        if marker and marker["role"] == ROLE_FINAL:
            finals_by_root.setdefault(marker["root_mr_id"], []).append(tp)
    by_root = {}
    for h in hops or []:
        root = _i(h.get("root_mr_id"))
        if root is not None:
            by_root.setdefault(root, []).append(h)
    out = [
        _build_lorry(root, rows, lines_by_mr, lines_by_po,
                     finals_by_root.get(root, []), positions, siblings_by_po, today)
        for root, rows in by_root.items()
    ]
    out.sort(key=lambda l: (l["lorry_date"] or date.min, l["ge_no"] or 0, l["root_mr_id"]),
             reverse=True)
    return out


def orphan_transfer_pos(transfer_pos, chain_root_ids, linked_po_ids) -> list:
    """Transfer POs (by marker) whose chain is gone: a Forwarding PO that no
    hop MR points at any more, or a Final PO whose lorry has no chain left.
    `chain_root_ids` / `linked_po_ids` cover every financial year."""
    roots = {int(r) for r in chain_root_ids if r is not None}
    linked = {int(p) for p in linked_po_ids if p is not None}
    out = []
    for tp in transfer_pos or []:
        marker = parse_marker(tp.get("internal_note"))
        po_id = _i(tp.get("jute_po_id"))
        if marker is None or po_id is None:
            continue
        if marker["role"] == ROLE_FORWARD and po_id in linked:
            continue
        if marker["role"] == ROLE_FINAL and marker["root_mr_id"] in roots:
            continue
        out.append({
            "Kind": "Forwarding" if marker["role"] == ROLE_FORWARD else "Final",
            "PO No": full_po_no(tp.get("po_no"), tp.get("co_prefix"),
                                tp.get("branch_prefix"), tp.get("po_date"), po_id),
            "Company": _s(tp.get("co_name")) or _s(tp.get("co_prefix")),
            "Date": fmt_date(tp.get("po_date")),
            "Value": fmt_num(tp.get("jute_po_value")),
            "Made for MR id": marker["mr_id"],
        })
    return out


# --- search --------------------------------------------------------------------

def _alnum(text) -> str:
    return re.sub(r"[^a-z0-9]", "", _s(text).lower())


def _norm_short(text) -> str:
    return " ".join(_s(text).upper().split())


# A number, optionally named the way the table names it: '63', 'GE 21',
# 'ge21', 'GE No. 21', 'MR 66', 'PO 63', '#21'. Matched on text whose spaces
# are collapsed to one, so every optional ' ?' has two choices at most (no
# runaway backtracking on long runs of spaces). \d is any Unicode decimal
# digit -- the set int() accepts -- so '６３' and '٦٣' are 63, while '²' and
# '①' (digits to str.isdigit(), not to int()) are plain text.
_NUMBER_QUERY = re.compile(
    r"(?:(?P<kind>GE|MR|PO) ?(?:NO|NUMBER)?\.? ?[:#-]? ?|# ?)?(?P<digits>\d+)",
    re.IGNORECASE,
)
_MAX_QUERY_DIGITS = 18      # no number here is longer; int() refuses > 4300 digits
PLATE_MIN_DIGITS = 4        # '6522' finds WB-57C-6522; '21' must not find every plate


def _query_number(digits: str) -> Optional[int]:
    if len(digits) > _MAX_QUERY_DIGITS:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def search_matches(lorry: dict, text) -> bool:
    """A number ('63'): exact match on the gate-entry number, any of the
    three PO numbers or an MR number, so '21' never finds '121'. Named
    ('GE 21' as the Lorry column shows it, 'MR 66', 'PO 63'): that kind
    only; '#21' is the same as '21'. Four digits or more ('6522') also
    find the lorry number they are part of.
    Otherwise: case-insensitive part of a full PO number (a pasted
    JTSPL/JPO/26-27/00021 works), supplier, party, lorry number (spaces and
    dashes ignored) or invoice number; or a whole short number ('ejm 11').
    Never raises, whatever the text."""
    needle = _s(text)
    if not needle:
        return True
    index = lorry["search"]
    m = _NUMBER_QUERY.fullmatch(" ".join(needle.split()))
    if m:
        digits = m.group("digits")
        n = _query_number(digits)
        if n is None:
            return False
        kind = m.group("kind")
        if kind:
            return n in index[kind.lower()]
        if n in index["numbers"]:
            return True
        # a lorry is known by its plate's last digits; leading zeros kept
        return (len(digits) >= PLATE_MIN_DIGITS
                and str(n).zfill(len(digits)) in index["vehicle"])
    low = needle.lower()
    if any(low in t for t in index["texts"]):
        return True
    if _norm_short(needle) in index["shorts"]:
        return True
    compact = _alnum(needle)
    return bool(compact) and bool(index["vehicle"]) and compact in index["vehicle"]


# --- tables --------------------------------------------------------------------

# (header, key in the lorry dict). The first seven are the compact table.
LORRY_COLUMNS = [
    ("Lorry", "lorry"), ("Orig PO", "orig_po"), ("Fwd PO", "fwd_po"),
    ("Final PO", "final_po"), ("Status", "status"), ("Supplier", "supplier"),
    ("MR Kg", "mr_kg"),
    ("GE No", "ge_no"), ("GE Date", "ge_date"), ("Mill", "mill"), ("Party", "party"),
    ("Lorry No", "lorry_no"), ("MR Amt", "mr_amt"), ("Orig Date", "orig_date"),
    ("Orig Rate", "orig_rate"), ("PO Lorries", "po_lorries"), ("Fwd Co", "fwd_co"),
    ("Fwd MR", "fwd_mr"), ("Fwd Date", "fwd_date"), ("Fwd Rate", "fwd_rate"),
    ("Fwd Kg", "fwd_kg"), ("Fwd Value", "fwd_value"), ("Fwd Diff", "fwd_diff"),
    ("Final Date", "final_date"), ("Final Rate", "final_rate"), ("Final Kg", "final_kg"),
    ("Final Value", "final_value"), ("Invoice", "invoice_no"), ("Inv Amt", "inv_amt"),
    ("Final Diff", "final_diff"), ("Days", "days"),
]
COMPACT_LORRY_COLUMNS = [h for h, _k in LORRY_COLUMNS[:7]]
LORRY_INT_COLUMNS = ["MR Kg", "GE No", "MR Amt", "Fwd Kg", "Fwd Value", "Fwd Diff",
                     "Final Kg", "Final Value", "Inv Amt", "Final Diff", "Days"]
LORRY_DATE_COLUMNS = ["GE Date", "Orig Date", "Fwd Date", "Final Date"]

PO_COLUMNS = ["Orig PO", "Date", "Lorries", "Fwd POs", "Final POs", "Status",
              "Supplier", "Rate", "Ordered Kg", "Ordered Value", "MR Kg",
              "Fwd Value", "Final Value"]
COMPACT_PO_COLUMNS = PO_COLUMNS[:6]
PO_INT_COLUMNS = ["Ordered Kg", "Ordered Value", "MR Kg", "Fwd Value", "Final Value"]
PO_DATE_COLUMNS = ["Date"]


def _lorry_cell(lorry: dict, key: str, grouped: bool):
    if key == "orig_date":
        return None if lorry["orig"] is None else lorry["orig"]["date"]
    if key == "orig_rate":
        rates = [] if lorry["orig"] is None else lorry["orig"]["rates"]
        return rate_range(rates) if grouped else rate_range(rates, "-", False)
    if key == "fwd_rate":
        return (rate_range(lorry["fwd_rates"]) if grouped
                else rate_range(lorry["fwd_rates"], "-", False))
    if key == "final_rate":
        return (rate_range(lorry["final_rates"]) if grouped
                else rate_range(lorry["final_rates"], "-", False))
    return lorry[key]


def _int_array(values):
    return pd.array([None if _f(v) is None else int(round(_f(v))) for v in values],
                    dtype="Int64")


def _cell(value):
    """'' -> None, so an empty text cell shows the table's '—' placeholder
    like an empty number cell does."""
    return None if isinstance(value, str) and not value else value


def lorries_frame(lorries, full: bool = True) -> pd.DataFrame:
    """The Lorries table: 31 columns (7 when compact), numbers as whole
    integers and dates as dates so header-click sorting works."""
    data = {h: [_cell(_lorry_cell(l, k, True)) for l in lorries] for h, k in LORRY_COLUMNS}
    df = pd.DataFrame(data, columns=[h for h, _k in LORRY_COLUMNS])
    for col in LORRY_INT_COLUMNS:
        df[col] = _int_array(data[col])
    for col in LORRY_DATE_COLUMNS:
        df[col] = pd.Series(data[col], dtype="object")
    return df if full else df[COMPACT_LORRY_COLUMNS]


def lorries_csv_frame(lorries) -> pd.DataFrame:
    """One row per lorry: the 31 columns unformatted, the full PO numbers,
    the five ids and the status reason."""
    rows = []
    for l in lorries:
        row = {}
        for header, key in LORRY_COLUMNS:
            value = _lorry_cell(l, key, False)
            if isinstance(value, float):
                value = round(value, 2)
            row[header] = value
        row["Orig PO No"] = "" if l["orig"] is None else l["orig"]["full"]
        row["Fwd PO No"] = ", ".join(p["full"] for p in l["fwd_pos"])
        row["Final PO No"] = ", ".join(p["full"] for p in l["final"]["pos"])
        row["Root MR Id"] = l["root_mr_id"]
        row["Hop MR Id"] = ", ".join(str(h["hop_mr_id"]) for h in l["hops"])
        row["Orig PO Id"] = l["orig_po_id"]
        row["Fwd PO Id"] = ", ".join(str(p["id"]) for p in l["fwd_pos"])
        row["Final PO Id"] = ", ".join(str(p["id"]) for p in l["final"]["pos"])
        row["Status Reason"] = l["status_reason"]
        rows.append(row)
    columns = ([h for h, _k in LORRY_COLUMNS]
               + ["Orig PO No", "Fwd PO No", "Final PO No", "Root MR Id", "Hop MR Id",
                  "Orig PO Id", "Fwd PO Id", "Final PO Id", "Status Reason"])
    return pd.DataFrame(rows, columns=columns)


LINE_DETAIL_COLUMNS = [
    "GE No", "GE Date", "Mill", "Lorry No", "Supplier", "Party", "PO Kind",
    "PO No", "PO Date", "PO Company", "MR No", "Quality", "MR Kg", "PO Units",
    "PO Kg", "MR Rate", "PO Rate", "MR Amount", "PO Value", "Diff",
    "Whole Unit Diff", "Rate Rounding Diff", "Unmatched Diff", "Root MR Id",
    "MR Id", "MR Line Id", "PO Id", "PO Line Id",
]


def _r2(value):
    f = _f(value)
    return None if f is None else round(f, 2)


def line_detail_frame(lorries) -> pd.DataFrame:
    """The accountant's file: one row per PO line with the MR line it was
    built from. Lines of a lorry whose PO is not created yet are listed with
    the PO columns empty, so the file is usable before the POs exist."""
    rows = []

    def add(l, kind, po, company, mr_no, mr_id, recon):
        for r in recon:
            rows.append({
                "GE No": l["ge_no"], "GE Date": l["ge_date"], "Mill": l["mill"],
                "Lorry No": l["lorry_no"], "Supplier": l["supplier"], "Party": l["party"],
                "PO Kind": kind,
                "PO No": "" if po is None else po["full"],
                "PO Date": None if po is None else po["date"],
                "PO Company": company, "MR No": mr_no, "Quality": r["quality"],
                "MR Kg": _r2(r["mr_kg"]), "PO Units": _r2(r["po_units"]),
                "PO Kg": _r2(r["po_kg"]), "MR Rate": _r2(r["mr_rate"]),
                "PO Rate": _r2(r["po_rate"]), "MR Amount": _r2(r["mr_amount"]),
                "PO Value": _r2(r["po_value"]), "Diff": _r2(r["diff"]),
                "Whole Unit Diff": _r2(r["weight_part"]),
                "Rate Rounding Diff": _r2(r["rate_part"]),
                "Unmatched Diff": _r2(r["unmatched"]),
                "Root MR Id": l["root_mr_id"], "MR Id": mr_id,
                "MR Line Id": r["jute_mr_li_id"],
                "PO Id": None if po is None else po["id"],
                "PO Line Id": r["jute_po_li_id"],
            })

    for l in lorries:
        for h in l["hops"]:
            add(l, "Forwarding", h["po"], h["co_prefix"], h["mr_no"], h["hop_mr_id"], h["recon"])
        final = l["final"]
        if final["recon"]:
            add(l, "Final", final["pos"][0] if final["pos"] else None,
                l["mill"], l["mill_mr_no"], l["root_mr_id"], final["recon"])
    return pd.DataFrame(rows, columns=LINE_DETAIL_COLUMNS)


_SHOW_FILE_PARTS = {SHOW_AWAITING: "awaiting", SHOW_BACK: "back-at-mill",
                    SHOW_ATTENTION: "needs-attention"}
_SEARCH_FILE_CHARS = 40


def _file_part(text, sep: str = "") -> str:
    """ASCII letters and digits only; every run of anything else becomes
    `sep` (dropped when ''), so any text is safe in a file name."""
    return re.sub(r"[^A-Za-z0-9]+", sep, _s(text)).strip(sep)


def csv_file_name(mill_prefix, period_code, kind: str = "", *, show=SHOW_ALL,
                  fwd_prefix="", search="") -> str:
    """po_tracker_<mill>_<period>[_<show>][_fwd-<co>][_search-<text>][_<kind>].csv
    -- named after the rows it holds ('all' for every mill). A search covers
    the whole financial year and ignores Show, so its file is named after
    the year: po_tracker_EJM_FY26-27_search-GE-21.csv."""
    mill = _file_part(mill_prefix) or "all"
    needle = _s(search)
    if needle:
        fy = period_fy(period_code)
        period = "all" if fy is None else period_file_part(fy_code(fy))
    else:
        period = period_file_part(period_code)
    parts = ["po_tracker", mill, period]
    if not needle and show in _SHOW_FILE_PARTS:
        parts.append(_SHOW_FILE_PARTS[show])
    fwd = _file_part(fwd_prefix)
    if fwd:
        parts.append(f"fwd-{fwd}")
    if needle:
        slug = _file_part(needle, "-")[:_SEARCH_FILE_CHARS].strip("-")
        parts.append(f"search-{slug}" if slug else "search")
    if kind:
        parts.append(kind)
    return "_".join(parts) + ".csv"


def nothing_found_text(search, fy_start_year: int) -> str:
    """"Nothing found for 'GE 21' in FY 26-27." -- the search text escaped,
    as st.info renders markdown ('**x**' must not turn bold)."""
    return f"Nothing found for '{md_escape(search)}' in FY {fy_text(fy_start_year)}."


def recon_table(rows, full: bool = False) -> pd.DataFrame:
    """Reconciliation table of one PO block, ready to show (formatted text,
    total row last). Compact: Quality | MR kg | PO kg | MR rate | PO rate |
    Diff; `full` adds PO units, MR amount and PO value."""
    totals = recon_totals(rows)

    def line(label, r, rates=True):
        out = {
            "Quality": label,
            "MR kg": fmt_num(r["mr_kg"]),
            "PO kg": fmt_num(r["po_kg"]),
            "PO units": fmt_num(r["po_units"]),
            "MR rate": fmt_num(r["mr_rate"]) if rates else "",
            "PO rate": fmt_num(r["po_rate"]) if rates else "",
            "MR amount": fmt_num(r["mr_amount"]),
            "PO value": fmt_num(r["po_value"]),
            "Diff": fmt_signed(r["diff"]),
        }
        return out

    body = [line(r["quality"], r) for r in rows]
    if rows:
        body.append(line("Total", {**totals, "mr_rate": None, "po_rate": None}, rates=False))
    columns = (["Quality", "MR kg", "PO kg", "PO units", "MR rate", "PO rate",
                "MR amount", "PO value", "Diff"] if full
               else ["Quality", "MR kg", "PO kg", "MR rate", "PO rate", "Diff"])
    return pd.DataFrame(body, columns=columns)


def original_po_headline(orig: dict) -> str:
    """'2 lorries · 200.00 qtl · 2,570,000' -- what the Original PO ordered.
    Shown only inside one PO's block or row: the same PO feeds one to five
    lorries, so it must never be added up per lorry."""
    parts = []
    if orig["lorries_ordered"] is not None:
        parts.append(_plural(orig["lorries_ordered"], "lorry", "lorries"))
    if orig["weight_kg"] is not None:
        parts.append(f"{orig['weight_kg'] / 100.0:,.2f} qtl")
    if orig["value"] is not None:
        parts.append(fmt_num(orig["value"]))
    return " · ".join(parts)


def original_po_lines_table(orig: dict) -> pd.DataFrame:
    """The Original PO's own lines: Quality | Units | Rate | %."""
    rows = [{
        "Quality": l["quality"],
        "Units": fmt_num(l["units"]) + (f" {l['uom'].lower()}" if _is_unit_uom(l["uom"]) else ""),
        "Rate": fmt_num(l["rate"]),
        "%": CELL_NONE if l["percentage"] is None else f"{l['percentage']:g}",
    } for l in orig["lines"]]
    return pd.DataFrame(rows, columns=["Quality", "Units", "Rate", "%"])


def sibling_table(siblings, lorries_by_root: dict, selected_root_id=None,
                  other_year_root_ids=()) -> pd.DataFrame:
    """'Lorries received on this PO': one line per MR received on the
    Original PO, oldest first, the selected lorry marked, lorries that were
    not transferred shown as such."""
    other = {int(r) for r in other_year_root_ids}
    ordered = sorted(siblings or [], key=lambda r: (
        as_date(r.get("jute_gate_entry_date")) or date.max,
        _i(r.get("jute_gate_entry_no")) or 0,
        _i(r.get("jute_mr_id")) or 0,
    ))
    rows = []
    for s in ordered:
        mr_id = _i(s.get("jute_mr_id"))
        lorry = lorries_by_root.get(mr_id)
        if lorry is not None:
            fwd, fin, status = lorry["fwd_po"], lorry["final_po"], lorry["status"]
        elif mr_id in other:
            fwd, fin = "transferred", ""
            status = "in another financial year"
        else:
            fwd, fin = "not transferred", ""
            status = mr_status_label(s.get("status_id"), s.get("status_name"))
        no = _i(s.get("jute_gate_entry_no"))
        rows.append({
            "": "▶" if selected_root_id is not None and mr_id == selected_root_id else "",
            "GE": "" if no is None else str(no),
            "Date": fmt_date(s.get("jute_gate_entry_date")),
            "MR kg": fmt_num(lorry["mr_kg"] if lorry is not None else s.get("mr_weight")),
            "Fwd PO": fwd,
            "Final PO": fin,
            "Status": status,
        })
    return pd.DataFrame(rows, columns=["", "GE", "Date", "MR kg", "Fwd PO", "Final PO", "Status"])


# --- Original POs view ---------------------------------------------------------

def original_po_rows(lorries) -> list:
    """One row per Original PO over the given lorries (pass every transferred
    lorry of the POs to be listed), newest PO first. Lorries without an
    Original PO have no row here."""
    groups = {}
    for l in lorries:
        if l["orig"] is not None:
            groups.setdefault(l["orig_po_id"], []).append(l)
    out = []
    for po_id, group in groups.items():
        orig = group[0]["orig"]
        transferred = len(group)
        received = len(group[0]["siblings"])
        ordered = orig["lorries_ordered"]

        fwd_numbers = [(h["po"]["co_prefix"], h["po"]["no"])
                       for l in group for h in l["hops"] if h["po"] is not None]
        fwd_missing = sum(1 for l in group for h in l["hops"] if h["missing"])
        fwd_text = po_number_ranges(fwd_numbers)
        if fwd_missing:
            fwd_text = (f"{fwd_text} +{fwd_missing} {CELL_NOT_YET}" if fwd_text
                        else f"{fwd_missing} {CELL_NOT_YET}")
        if not fwd_text:
            fwd_text = CELL_NA

        with_final = [l for l in group if l["final"]["pos"]]
        if len(with_final) == transferred:
            final_text = po_number_ranges(
                (p["co_prefix"], p["no"]) for l in group for p in l["final"]["pos"])
        else:
            awaiting = sum(1 for l in group if l["final_po"] == CELL_AWAITING)
            missing = sum(1 for l in group if l["final"]["missing"])
            final_text = f"{len(with_final)} of {transferred}"
            if awaiting:
                final_text += f" · {awaiting} awaiting"
            if missing:
                final_text += f" · {missing} {CELL_NOT_YET}"

        def total(key):
            vals = [l[key] for l in group if l[key] is not None]
            return sum(vals) if vals else None

        out.append({
            "orig_po_id": po_id,
            "orig": orig,
            "root_mr_ids": [l["root_mr_id"] for l in group],
            "Orig PO": orig["short"] or orig["full"],
            "Date": orig["date"],
            "Lorries": f"{transferred}/{received}/{ordered if ordered is not None else '?'}",
            "Fwd POs": fwd_text,
            "Final POs": final_text,
            "Status": worst_status(l["status"] for l in group),
            "Supplier": orig["supplier_name"] or group[0]["supplier"],
            "Rate": rate_range(orig["rates"]),
            "Ordered Kg": orig["weight_kg"],
            "Ordered Value": orig["value"],
            "MR Kg": total("mr_kg"),
            "Fwd Value": total("fwd_value"),
            "Final Value": total("final_value"),
        })
    out.sort(key=lambda r: (r["Date"] or date.min, r["orig"]["no"] or 0, r["orig_po_id"]),
             reverse=True)
    return out


def original_pos_frame(po_rows, full: bool = True) -> pd.DataFrame:
    """The Original POs table: 13 columns (6 when compact)."""
    data = {c: [_cell(r[c]) for r in po_rows] for c in PO_COLUMNS}
    df = pd.DataFrame(data, columns=PO_COLUMNS)
    for col in PO_INT_COLUMNS:
        df[col] = _int_array(data[col])
    for col in PO_DATE_COLUMNS:
        df[col] = pd.Series(data[col], dtype="object")
    return df if full else df[COMPACT_PO_COLUMNS]


# --- summary and totals ----------------------------------------------------------

def lorry_totals(lorries) -> dict:
    """Totals of a set of lorries. PO figures and differences cover only the
    lorries that have that PO -- a lorry without one would otherwise show up
    as a difference the size of its whole value."""
    t = {"lorries": len(lorries), "mr_kg": 0.0, "mr_amt": 0.0,
         "fwd_count": 0, "fwd_kg": 0.0, "fwd_value": 0.0, "fwd_diff": 0.0,
         "final_count": 0, "final_kg": 0.0, "final_value": 0.0,
         "inv_amt": 0.0, "final_diff": 0.0,
         "awaiting": 0, "completed": 0, "no_po": 0, "check": 0, "other": 0}
    for l in lorries:
        t["mr_kg"] += l["mr_kg"] or 0.0
        t["mr_amt"] += l["mr_amt"] or 0.0
        t["fwd_count"] += l["fwd_po_count"]
        if l["fwd_value"] is not None:
            t["fwd_kg"] += l["fwd_kg"] or 0.0
            t["fwd_value"] += l["fwd_value"]
            t["fwd_diff"] += l["fwd_diff"] or 0.0
        t["final_count"] += l["final_po_count"]
        if l["final_value"] is not None:
            t["final_kg"] += l["final_kg"] or 0.0
            t["final_value"] += l["final_value"]
            if l["inv_amt"] is not None:
                t["inv_amt"] += l["inv_amt"]
                t["final_diff"] += l["final_diff"] or 0.0
        # 'awaiting' counts what the Awaiting return chip lists (chain still
        # out); the other four split the lorries by status.
        if l["awaiting"]:
            t["awaiting"] += 1
        status = l["status"]
        if status == STATUS_COMPLETED:
            t["completed"] += 1
        elif status == STATUS_NO_PO:
            t["no_po"] += 1
        elif status == STATUS_CHECK:
            t["check"] += 1
        elif status != STATUS_AWAITING:
            t["other"] += 1
    # A lorry without PO yet waits for the backfill: not 'attention'.
    t["attention"] = t["check"] + t["other"]
    return t


def _plural(n: int, one: str, many: Optional[str] = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _status_counts(t: dict) -> list:
    """['175 without PO yet', '0 need attention'] -- the first only when
    there is such a lorry."""
    out = [f"{t['no_po']:,} without PO yet"] if t["no_po"] else []
    text = f"{t['attention']:,} need{'s' if t['attention'] == 1 else ''} attention"
    kinds = [f"{t[key]:,} {name}" for key, name in
             (("check", "to check"), ("other", "other")) if t[key]]
    out.append(f"{text} ({', '.join(kinds)})" if kinds else text)
    return out


def summary_line(t: dict) -> str:
    """'61 lorries · 61 forwarding POs · 0 final POs · 61 awaiting return ·
    0 need attention' -- one line instead of four metric tiles; before the
    backfill e.g. '... · 61 without PO yet · 0 need attention'."""
    return " · ".join([
        _plural(t["lorries"], "lorry", "lorries"),
        _plural(t["fwd_count"], "forwarding PO"),
        _plural(t["final_count"], "final PO"),
        f"{t['awaiting']:,} awaiting return",
        *_status_counts(t),
    ])


def totals_line(t: dict) -> str:
    """The line under the Lorries table (never a table row: a total row
    could be selected and would break the row-to-lorry mapping)."""
    parts = [f"Total {_plural(t['lorries'], 'lorry', 'lorries')}: "
             f"MR {fmt_num(t['mr_kg'])} kg, amount {fmt_num(t['mr_amt'])}"]
    if t["fwd_count"]:
        parts.append(f"{_plural(t['fwd_count'], 'Fwd PO')} {fmt_num(t['fwd_kg'])} kg, "
                     f"value {fmt_num(t['fwd_value'])} (diff {fmt_signed(t['fwd_diff'])})")
    else:
        parts.append("no Fwd PO")
    if t["final_count"]:
        parts.append(f"{_plural(t['final_count'], 'Final PO')} {fmt_num(t['final_kg'])} kg, "
                     f"value {fmt_num(t['final_value'])} vs invoices "
                     f"{fmt_num(t['inv_amt'])} (diff {fmt_signed(t['final_diff'])})")
    else:
        parts.append("no Final PO")
    return " · ".join(parts)


def po_summary_line(po_rows, t: dict) -> str:
    """Summary of the Original POs view; `t` = lorry_totals of their lorries."""
    return " · ".join([
        _plural(len(po_rows), "original PO"),
        f"{_plural(t['lorries'], 'lorry', 'lorries')} transferred",
        _plural(t["fwd_count"], "forwarding PO"),
        _plural(t["final_count"], "final PO"),
        f"{t['awaiting']:,} awaiting return",
        *_status_counts(t),
    ])


def po_totals_line(po_rows, t: dict) -> str:
    kg = sum(r["Ordered Kg"] or 0.0 for r in po_rows)
    value = sum(r["Ordered Value"] or 0.0 for r in po_rows)
    return (f"Total {_plural(len(po_rows), 'original PO')}: ordered {fmt_num(kg)} kg, "
            f"value {fmt_num(value)} · {_plural(t['lorries'], 'lorry', 'lorries')} "
            f"transferred, MR {fmt_num(t['mr_kg'])} kg · Fwd PO value "
            f"{fmt_num(t['fwd_value']) if t['fwd_count'] else CELL_NONE} · Final PO value "
            f"{fmt_num(t['final_value']) if t['final_count'] else CELL_NONE}")


def backfill_banners(lorries) -> list:
    """[(kind, text)]: one banner for every lorry missing a transfer PO,
    instead of a warning per lorry.

    With creation switched on a save cannot leave a hop without its PO (any
    failure rolls the whole step back), so a missing PO always means the
    lorry was transferred before transfer POs existed, or while they were
    switched off -- the backfill script creates those. Which lorries the
    backfill has already done says nothing about when the others were
    transferred (a pilot runs on the oldest lorry), so no lorry is called an
    error for missing its PO (status No PO yet, never 'Needs attention')."""
    missing = sum(1 for l in lorries if l["status"] == STATUS_NO_PO)
    if not missing:
        return []
    return [("info",
             f"{_plural(missing, 'lorry has', 'lorries have')} no transfer PO yet: "
             "transferred before transfer POs existed, or while they were switched "
             "off. The one-time backfill creates them after you approve its dry-run "
             "list — nothing to do on this screen.")]
