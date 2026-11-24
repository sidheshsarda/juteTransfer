"""Jute Payment Tracker: pure shaping (no Streamlit / DB imports).

One row per SUPPLIER BILL -- the MR of the company that bought the lorry from
the supplier:
  * the step-1 MR of a transfer chain (the first hop; its root is the gate
    entry at the mill), or
  * a mill MR that was never transferred (gate entry and purchase in one
    company).
A chain root itself, later hops and marked-stock children are not bills: they
are inter-company.

Bill amount = jute_mr.net_total (after claim and TDS: what is payable, owner
ruling 2026-11-19). Terms = jute_mr.payment_terms_days (the MR column the ERP
fills; the root's when the step-1 MR has none). Due = payment_due_date when
set, else MR date + terms. Paid / pending come from the accounts module (the
bill's JUTE_BILLPASS voucher and its bill reference); a bill accounts has not
posted yet shows as 'Not in accounts' with the whole amount pending.

Party payment hold (acc_ledger.payment_hold / payment_hold_note, set in VoW):
rows of a held supplier carry an 'On hold' column and cannot be ticked for
payment. Any bill VoW holds a reference for (current or opening) can be paid;
the payment request names bills by acc_bill_ref_id.
"""

from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

# Bills dated before this are not read from the MRs: what was still owed on
# them is the opening bills entered in VoW (otherwise every old MR, paid long
# ago outside the system, would show as pending).
ACCOUNTS_START = date(2026, 4, 1)

STATUS_PAID = "Paid"
STATUS_PART = "Part paid"
STATUS_PENDING = "Pending"
STATUS_PROPOSED = "Payment proposed"      # a payment voucher is waiting for approval in VoW
STATUS_NOT_POSTED = "Not in accounts"
STATUSES = (STATUS_PENDING, STATUS_PART, STATUS_PROPOSED, STATUS_PAID, STATUS_NOT_POSTED)

BILL_CURRENT = "Current"
BILL_OPENING = "Opening"          # owed at the start of accounts, entered bill-wise in VoW
BILL_IMPORTED = "Imported"        # entered the same way but dated on/after the accounts start
OPENING_KINDS = (BILL_OPENING, BILL_IMPORTED)

COLUMNS = [
    "Bill", "Gate Entry Co", "GE No", "GE Date", "Step 1 Co", "MR No", "MR Date", "Party",
    "Supplier",
    "Bill Amount", "Paid", "Pending", "Proposed", "Status", "On hold", "Terms (days)", "Due Date",
    "Overdue (days)", "Last Payment",
]


def _as_date(value) -> Optional[date]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    if pd.isna(value):
        return None
    if isinstance(value, datetime) or hasattr(value, "date"):
        return value.date() if callable(getattr(value, "date", None)) else value
    return value


def _num(value) -> float:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    return float(value)


def _text(value) -> str:
    """A string from a frame cell (None / NaN -> '')."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _int_or_none(value) -> Optional[int]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sheet_party_of(origin_note) -> str:
    """The sheet's party name inside an opening bill's origin note: the middle
    '|' segment ('LCPL | kishan k' -> 'kishan k'); '' when there is no '|'."""
    parts = _text(origin_note).split("|")
    return parts[1].strip() if len(parts) > 1 else ""


HIDDEN = ["jute_mr_id", "step1_co_id", "party_id", "acc_bill_ref_id", "can_pay", "on_hold",
          "jute_supplier_id", "sheet_party"]


def holds_map(holds) -> dict:
    """{(co_id, party_id): note} from payment_queries' holds frame (columns
    co_id, party_id, note) or an already-built dict; None / empty -> {}."""
    if holds is None:
        return {}
    if isinstance(holds, dict):
        return {(int(k[0]), int(k[1])): _text(v) for k, v in holds.items()}
    if holds.empty:
        return {}
    return {(int(r["co_id"]), int(r["party_id"])): _text(r.get("note"))
            for r in holds.to_dict("records")}


def hold_text(holds: dict, co_id, party_id) -> str:
    """The 'On hold' cell: the note, 'Yes' when held without a note, else ''."""
    if party_id is None or (isinstance(party_id, float) and pd.isna(party_id)):
        return ""
    key = (int(co_id), int(party_id))
    if key not in holds:
        return ""
    return holds[key] or "Yes"


def fy_label(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


def fy_bounds(fy_start_year: int) -> tuple:
    return date(fy_start_year, 4, 1), date(fy_start_year + 1, 3, 31)


def format_mr_no(co_prefix, branch_prefix, mr_no, mr_date) -> str:
    """The ERP's jute MR number: CO[/BRANCH]/JMR/26-27/00024
    (vowerp3be juteProcurement/formatters.format_jute_mr_number)."""
    if mr_no is None or (isinstance(mr_no, float) and pd.isna(mr_no)) or not mr_date:
        return ""
    parts = [p for p in (_text(co_prefix), _text(branch_prefix)) if p]
    return "/".join(parts + ["JMR", fy_label(mr_date), f"{int(mr_no):05d}"])


def pick_bills(mrs: pd.DataFrame, first_hop_of: dict) -> pd.DataFrame:
    """The supplier bills among candidate MRs. `first_hop_of` = {root id:
    its first hop's id} for every chain; `mrs` needs jute_mr_id and
    src_jute_mr_id."""
    if mrs.empty:
        return mrs
    first_hops = set(first_hop_of.values())
    roots = set(first_hop_of)

    def is_bill(row) -> bool:
        if pd.notna(row["src_jute_mr_id"]):
            return int(row["jute_mr_id"]) in first_hops
        return int(row["jute_mr_id"]) not in roots       # a mill MR never transferred
    return mrs[mrs.apply(is_bill, axis=1)].copy()


def bill_status(bill_amount: float, pending: float, posted: bool, proposed: float = 0.0) -> str:
    if not posted:
        return STATUS_NOT_POSTED
    if pending <= 0.005:
        return STATUS_PAID
    if proposed > 0.005:
        return STATUS_PROPOSED
    if pending < bill_amount - 0.005:
        return STATUS_PART
    return STATUS_PENDING


def build_rows(bills: pd.DataFrame, today: Optional[date] = None, holds=None) -> pd.DataFrame:
    """The tracker grid from the raw bill rows (payment_queries.load_bills)."""
    today = today or date.today()
    holds = holds_map(holds)
    out = []
    for r in bills.to_dict("records"):
        mr_date = _as_date(r.get("jute_mr_date"))
        amount = round(_num(r.get("net_total")), 2)
        posted = pd.notna(r.get("acc_bill_ref_id")) and r.get("acc_bill_ref_id") is not None
        pending = round(_num(r.get("acc_pending")), 2) if posted else amount
        proposed = round(_num(r.get("acc_proposed")), 2) if posted else 0.0
        terms = r.get("payment_terms_days")
        if terms is None or pd.isna(terms):
            terms = r.get("root_terms_days")
        terms = None if terms is None or pd.isna(terms) else int(terms)
        due = _as_date(r.get("payment_due_date")) or _as_date(r.get("acc_due_date"))
        if due is None and mr_date is not None and terms is not None:
            due = mr_date + timedelta(days=terms)
        overdue = (today - due).days if due and pending > 0.005 and today > due else None
        out.append({
            "jute_mr_id": int(r["jute_mr_id"]),
            "step1_co_id": int(r["step1_co_id"]),
            "party_id": None if pd.isna(r.get("party_id")) else int(r["party_id"]),
            "jute_supplier_id": _int_or_none(r.get("jute_supplier_id")),
            "sheet_party": "",
            "acc_bill_ref_id": int(r["acc_bill_ref_id"]) if posted else None,
            "Bill": BILL_CURRENT,
            "Gate Entry Co": _text(r.get("gate_co")),
            "GE No": None if pd.isna(r.get("ge_no")) else int(r["ge_no"]),
            "GE Date": _as_date(r.get("ge_date")),
            "Step 1 Co": _text(r.get("step1_co")),
            "MR No": format_mr_no(r.get("step1_co"), r.get("step1_branch_prefix"),
                                  r.get("branch_mr_no"), mr_date),
            "MR Date": mr_date,
            "Party": _text(r.get("party")),
            "Supplier": _text(r.get("supplier")),
            "Bill Amount": amount,
            "Paid": round(amount - pending, 2),
            "Pending": pending,
            "Proposed": proposed,
            "can_pay": round(max(pending - proposed, 0.0), 2) if posted else 0.0,
            "Status": bill_status(amount, pending, posted, proposed),
            "On hold": hold_text(holds, r["step1_co_id"], r.get("party_id")),
            "Terms (days)": terms,
            "Due Date": due,
            "Overdue (days)": overdue,
            "Last Payment": _as_date(r.get("last_payment_date")),
        })
    for row in out:
        row["on_hold"] = bool(row["On hold"])
    return pd.DataFrame(out, columns=HIDDEN + COLUMNS)


def build_opening_rows(opening: pd.DataFrame, opening_mrs: pd.DataFrame,
                       today: Optional[date] = None, holds=None,
                       accounts_start: Optional[date] = None) -> pd.DataFrame:
    """Opening bills as tracker rows. With `accounts_start`, a bill dated on or
    after it is labelled 'Imported' instead of 'Opening'. One entered against an old jute MR shows
    that MR's gate-entry mill, number and party; one entered as a plain bill
    shows the bill number it was given and its note in place of the party."""
    today = today or date.today()
    holds = holds_map(holds)
    by_id = ({int(m["jute_mr_id"]): m for m in opening_mrs.to_dict("records")}
             if opening_mrs is not None and not opening_mrs.empty else {})
    out = []
    for r in (opening.to_dict("records") if opening is not None and not opening.empty else []):
        linked = _text(r.get("src_doc_type")) == "JUTE_MR" and not pd.isna(r.get("src_doc_id"))
        mr = by_id.get(int(r["src_doc_id"])) if linked else None
        amount, pending = round(_num(r.get("acc_amount")), 2), round(_num(r.get("acc_pending")), 2)
        proposed = round(_num(r.get("acc_proposed")), 2)
        bill_date = _as_date(mr.get("jute_mr_date")) if mr else _as_date(r.get("bill_date"))
        due = _as_date(r.get("acc_due_date"))
        out.append({
            "jute_mr_id": int(mr["jute_mr_id"]) if mr else None,
            "step1_co_id": int(r["step1_co_id"]),
            "party_id": None if pd.isna(r.get("party_id")) else int(r["party_id"]),
            "jute_supplier_id": _int_or_none(mr.get("jute_supplier_id")) if mr else None,
            "sheet_party": sheet_party_of(r.get("origin_note")),
            "acc_bill_ref_id": int(r["acc_bill_ref_id"]),
            "can_pay": round(max(pending - proposed, 0.0), 2),
            "Bill": (BILL_IMPORTED if accounts_start and bill_date and bill_date >= accounts_start
                     else BILL_OPENING),
            "Gate Entry Co": _text(mr.get("gate_co")) if mr else "",
            "GE No": int(mr["ge_no"]) if mr and not pd.isna(mr.get("ge_no")) else None,
            "GE Date": _as_date(mr.get("ge_date")) if mr else None,
            "Step 1 Co": _text(r.get("step1_co")),
            "MR No": (format_mr_no(mr.get("step1_co"), mr.get("step1_branch_prefix"),
                                   mr.get("branch_mr_no"), bill_date) if mr else "")
                     or _text(r.get("bill_no")),
            "MR Date": bill_date,
            "Party": _text(mr.get("party")) if mr else _text(r.get("origin_note")),
            "Supplier": _text(r.get("supplier")),
            "Bill Amount": amount,
            "Paid": round(amount - pending, 2),
            "Pending": pending,
            "Proposed": proposed,
            "Status": bill_status(amount, pending, True, proposed),
            "On hold": hold_text(holds, r["step1_co_id"], r.get("party_id")),
            "Terms (days)": None,
            "Due Date": due,
            "Overdue (days)": (today - due).days if due and pending > 0.005 and today > due else None,
            "Last Payment": _as_date(r.get("last_payment_date")),
        })
    for row in out:
        row["on_hold"] = bool(row["On hold"])
    return pd.DataFrame(out, columns=HIDDEN + COLUMNS)


def merge_rows(opening_rows: pd.DataFrame, mr_rows: pd.DataFrame) -> pd.DataFrame:
    """Opening rows first, then MR rows. An opening/imported ref entered against
    an MR that accounts has not posted itself (no acc_bill_ref_id) IS that bill:
    keep the accounts-backed opening row, drop the MR row. A posted MR keeps its
    row (the imported ref is then an adjustment)."""
    has_o = opening_rows is not None and not opening_rows.empty
    has_m = mr_rows is not None and not mr_rows.empty
    if not has_o:
        return mr_rows.reset_index(drop=True) if has_m else (
            mr_rows if mr_rows is not None else pd.DataFrame(columns=["jute_mr_id"] + COLUMNS))
    if not has_m:
        return opening_rows.reset_index(drop=True)
    named = set(opening_rows["jute_mr_id"].dropna().astype(int))
    dup = mr_rows["jute_mr_id"].isin(named) & mr_rows["acc_bill_ref_id"].isna()
    return pd.concat([opening_rows, mr_rows[~dup]], ignore_index=True)


def tracker_rows(data: dict, today: Optional[date] = None, accounts_start: Optional[date] = None,
                 drop_paid_opening: bool = False) -> pd.DataFrame:
    """The tracker's rows from payment_queries.load_bills' dict: bills picked,
    accounts merged, opening bills added (a single year passes
    drop_paid_opening=True to show only the opening bills still pending)."""
    today = today or date.today()
    accounts_start = accounts_start or ACCOUNTS_START
    mrs = data["mrs"]
    bills = pick_bills(mrs, data["first_hop_of"])
    if not bills.empty:
        bills = bills.merge(data["acc"], on="jute_mr_id", how="left")
    holds = holds_map(data.get("holds"))
    rows = build_rows(bills, today, holds)
    opening = build_opening_rows(data.get("opening"), data.get("opening_mrs"), today, holds,
                                 accounts_start=accounts_start)
    if drop_paid_opening and not opening.empty:
        opening = opening[opening["Status"] != STATUS_PAID]
    return merge_rows(opening, rows)


def totals(rows: pd.DataFrame) -> dict:
    if rows.empty:
        return {"bills": 0, "bill_amount": 0.0, "paid": 0.0, "pending": 0.0, "overdue": 0.0}
    over = rows[rows["Overdue (days)"].notna()]
    return {"bills": len(rows), "bill_amount": float(rows["Bill Amount"].sum()),
            "paid": float(rows["Paid"].sum()), "pending": float(rows["Pending"].sum()),
            "overdue": float(over["Pending"].sum())}


def selection_problem(picked: pd.DataFrame) -> Optional[str]:
    """Why the picked bills cannot be paid together, or None. A payment is
    party-wise: one step-1 company (it pays) and one supplier."""
    if picked.empty:
        return "Tick the bills to pay."
    if picked["step1_co_id"].nunique() > 1:
        return ("The bills picked belong to more than one company ("
                + ", ".join(sorted(picked["Step 1 Co"].unique())) + "). Pick one company's bills.")
    if picked["party_id"].nunique(dropna=False) > 1:
        return "The bills picked belong to more than one supplier. Pick one supplier's bills."
    if picked["on_hold"].any():
        supplier = picked.loc[picked["on_hold"], "Supplier"].iloc[0]
        return (f"{supplier} is on hold for payment. Release the hold in VoW "
                "(Accounting > Pay Bills) first.")
    if (picked["Pending"] <= 0.005).any():
        return "A bill that is already paid is ticked. Untick it."
    if picked["acc_bill_ref_id"].isna().any():
        return ("A ticked bill is not in the accounts module yet. It has to be posted in "
                "accounts before a payment can be made against it.")
    if (picked["can_pay"] <= 0.005).any():
        return ("A ticked bill already has a payment for its full pending amount waiting for "
                "approval in VoW. Untick it.")
    return None


def payment_payload(co_id: int, party_id: int, form: dict, pay_now: dict,
                    acting_user_id: int) -> dict:
    """The request VoW's jute-payments API expects. `pay_now` = {acc_bill_ref_id:
    amount}; bills with nothing to pay are left out. Raises ValueError in
    plain words when the form cannot be sent."""
    bills = [{"acc_bill_ref_id": int(ref_id), "amount": round(float(amount), 2)}
             for ref_id, amount in pay_now.items() if amount and float(amount) > 0.005]
    if not bills:
        raise ValueError("Enter an amount to pay on at least one bill.")
    if not form.get("branch_id"):
        raise ValueError("Pick the branch.")
    if not form.get("bank_ledger_id"):
        raise ValueError("Pick the bank or cash account the payment goes from.")
    if not form.get("voucher_date"):
        raise ValueError("Enter the payment date.")
    return {
        "co_id": int(co_id), "branch_id": int(form["branch_id"]),
        "voucher_date": form["voucher_date"].isoformat(), "party_id": int(party_id),
        "bank_ledger_id": int(form["bank_ledger_id"]), "pay_mode": form.get("pay_mode") or "NEFT",
        "instrument_no": (form.get("instrument_no") or "").strip() or None,
        "instrument_date": (form["instrument_date"].isoformat()
                            if form.get("instrument_date") else None),
        "narration": (form.get("narration") or "").strip() or None,
        "acting_user_id": int(acting_user_id),
        "bills": bills,
    }


def default_narration(supplier: str, mr_nos: list) -> str:
    listed = ", ".join(mr_nos[:6]) + (f" and {len(mr_nos) - 6} more" if len(mr_nos) > 6 else "")
    return f"Payment to {supplier} against {listed}"[:500]
