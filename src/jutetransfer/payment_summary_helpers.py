"""Jute Supplier Dues (Payment Summary): pure shaping, no Streamlit / DB imports.

Works on the Payment Tracker's rows (payment_tracker_helpers.tracker_rows):
groups bills by the jute supplier master (the broker), matches the sheet's
short names to the master, and works a NOTIONAL oldest-first (FIFO) position
for dates / days pending. Nothing here re-applies a payment or saves anything.
Spec: docs/superpowers/specs/2026-11-24-payment-summary-design.md.
"""

import re
from datetime import date
from typing import Optional

import pandas as pd

from . import payment_tracker_helpers as H

EPS = 0.005
OTHERS = "others"
_OTHER_NAMES = {"", "others", "other", "na", "n a"}

ON_ACCOUNT_COLS = ["co_id", "co_prefix", "party_id", "supplier", "amount", "bill_date"]


# ---- small helpers --------------------------------------------------------
def _isnull(v) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _int(v) -> Optional[int]:
    if _isnull(v):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _s(v) -> str:
    return "" if _isnull(v) else str(v).strip()


def _f(v) -> float:
    return 0.0 if _isnull(v) else float(v)


def _d(v) -> Optional[date]:
    return H._as_date(v)


def _records(df) -> list:
    return [] if df is None or len(df) == 0 else df.to_dict("records")


def _clean_master(master) -> dict:
    out = {}
    for k, v in (master or {}).items():
        i = _int(k)
        if i is not None:
            out[i] = _s(v)
    return out


# ---- names ----------------------------------------------------------------
def norm_name(name) -> str:
    text = _s(name).lower()
    text = re.sub(r"[.,&/\-']", " ", text)
    return " ".join(text.split())


def others_ids(master: dict) -> set:
    return {i for i, n in _clean_master(master).items() if norm_name(n) in _OTHER_NAMES}


def _real(master: dict) -> dict:
    m = _clean_master(master)
    bad = others_ids(m)
    return {i: n for i, n in m.items() if i not in bad}


def _tokens_match(name_tokens: list, mtokens: list, initials: bool) -> bool:
    """Every name token is a prefix of a distinct master token, in order; with
    `initials` a token (2+ letters) may instead be the initials of consecutive
    master tokens."""
    def go(ni: int, mi: int) -> bool:
        if ni == len(name_tokens):
            return True
        t = name_tokens[ni]
        for start in range(mi, len(mtokens)):
            if mtokens[start].startswith(t) and go(ni + 1, start + 1):
                return True
            if initials and len(t) >= 2:
                for k in range(2, len(mtokens) - start + 1):
                    if "".join(x[0] for x in mtokens[start:start + k]) == t and go(ni + 1, start + k):
                        return True
        return False
    return go(0, 0)


def match_master(name, master: dict) -> Optional[int]:
    """A sheet short name -> ONE real master id, or None (no match / ambiguous)."""
    n = norm_name(name)
    if n in _OTHER_NAMES:
        return None
    real = _real(master)
    exact = [i for i, nm in real.items() if norm_name(nm) == n]
    if exact:
        return exact[0] if len(exact) == 1 else None
    toks = n.split()
    for initials in (False, True):
        hits = [i for i, nm in real.items()
                if _tokens_match(toks, norm_name(nm).split(), initials)]
        if hits:
            return hits[0] if len(hits) == 1 else None
    return None


def resolve_supplier(row: dict, master: dict, party_map: dict) -> tuple:
    real = _real(master)
    jsid = _int(row.get("jute_supplier_id"))
    if jsid is not None and jsid in real:
        return jsid, "mr"
    if jsid is None:
        sheet = norm_name(row.get("sheet_party"))
        if sheet:
            hits = [i for i, nm in real.items() if norm_name(nm) == sheet]
            if len(hits) == 1:
                return hits[0], "sheet"
    pid = _int(row.get("party_id"))
    if pid is not None and party_map:
        mapped = _int(party_map.get(pid))
        if mapped is not None and mapped in real:
            return mapped, "confirmed"
    return None, "others"


def _key_for(row: dict, master: dict, party_map: dict, split_others: bool) -> tuple:
    sid, source = resolve_supplier(row, master, party_map)
    if sid is not None:
        return f"S{sid}", _clean_master(master)[sid], source
    if split_others:
        pid = _int(row.get("party_id"))
        if pid is not None:
            return f"O{pid}", f"{_s(row.get('Supplier'))} (others)", source
    return "O", OTHERS, source


def add_supplier_key(rows, master, party_map, split_others=False) -> pd.DataFrame:
    base = rows.copy() if rows is not None else pd.DataFrame()
    keys = [_key_for(r, master, party_map, split_others) for r in _records(base)]
    base["skey"] = [k[0] for k in keys]
    base["Jute Supplier"] = [k[1] for k in keys]
    base["ssource"] = [k[2] for k in keys]
    return base


def assign_on_account(on_account, keyed_rows, master, party_map,
                      split_others=False, holds=None) -> pd.DataFrame:
    hold = H.holds_map(holds)
    oa = on_account.copy() if on_account is not None else pd.DataFrame(columns=ON_ACCOUNT_COLS)
    by_party: dict = {}
    for r in _records(keyed_rows):
        pid = _int(r.get("party_id"))
        if pid is not None:
            by_party.setdefault(pid, []).append(r)
    skeys, names, flags = [], [], []
    for r in _records(oa):
        pid = _int(r.get("party_id"))
        cands = by_party.get(pid, []) if pid is not None else []
        key = name = None
        if cands:
            totals: dict = {}
            for c in cands:
                t = totals.setdefault(c["skey"], [0.0, c["Jute Supplier"]])
                t[0] += _f(c.get("Pending"))
            best = max(totals.items(), key=lambda kv: kv[1][0])
            if best[1][0] > EPS:
                key, name = best[0], best[1][1]
            else:
                latest = max(cands, key=lambda c: (_d(c.get("MR Date")) or date.min))
                key, name = latest["skey"], latest["Jute Supplier"]
        if key is None:
            probe = {"party_id": pid, "Supplier": r.get("supplier")}
            key, name, _src = _key_for(probe, master, party_map, split_others)
        skeys.append(key)
        names.append(name)
        co = _int(r.get("co_id"))
        flags.append(co is not None and pid is not None and (co, pid) in hold)
    oa["skey"] = skeys
    oa["Jute Supplier"] = names
    oa["on_hold"] = flags
    return oa


# ---- FIFO -----------------------------------------------------------------
def fifo_position(bills, extra_paid=0.0, today=None) -> dict:
    today = today or date.today()
    items = [(_d(b[0]), _f(b[1]), _f(b[2])) for b in bills]
    billed = sum(a for _, a, _p in items)
    paid = max(sum(a - p for _, a, p in items) + _f(extra_paid), 0.0)
    pending = max(billed - paid, 0.0)
    order = sorted(range(len(items)),
                   key=lambda i: (items[i][0] is not None, items[i][0] or date.min, i))
    remaining = paid
    fifo = [0.0] * len(items)
    for i in order:
        amt = items[i][1]
        take = min(max(amt, 0.0), remaining)
        remaining -= take
        left = round(amt - take, 2)
        fifo[i] = 0.0 if left < EPS else left
    oldest = [d for d, _a, p in items if p > EPS and d is not None]
    pending_from = cleared = None
    if pending > EPS:
        for pos, i in enumerate(order):
            if fifo[i] > EPS:
                pending_from = items[i][0]
                # "cleared till" = the last bill DATE wholly covered: a date that
                # also holds the first uncovered bill is not cleared yet
                before = [items[j][0] for j in order[:pos]
                          if items[j][0] is not None
                          and (pending_from is None or items[j][0] < pending_from)]
                cleared = max(before) if before else None
                break
    elif order:
        cleared = items[order[-1]][0]
    return {
        "billed": round(billed, 2), "paid": round(paid, 2), "pending": round(pending, 2),
        "actual_oldest_unpaid": min(oldest) if oldest else None,
        "pending_from": pending_from, "cleared_till": cleared,
        "days_pending": (today - pending_from).days if pending_from else None,
        "fifo_pending": fifo,
    }


# ---- summaries --------------------------------------------------------------
def _bills_of(recs) -> list:
    return [(r.get("MR Date"), r.get("Bill Amount"), r.get("Pending")) for r in recs]


def _held(r) -> bool:
    v = r.get("on_hold")
    return False if _isnull(v) else bool(v)


def _oa_sum(oa_recs, held: Optional[bool] = None) -> float:
    return sum(_f(o.get("amount")) for o in oa_recs
               if held is None or _held(o) == held)


def _metrics(recs: list, oa_recs: list, today: date) -> dict:
    unpaid = [r for r in recs if _f(r.get("Pending")) > EPS]
    pending = sum(_f(r.get("Pending")) for r in recs)
    held_pending = sum(_f(r.get("Pending")) for r in recs if _held(r))
    if held_pending <= EPS:
        hold = ""
    elif held_pending >= pending - EPS:
        hold = "All"
    else:
        hold = "Part"
    parties = sorted({_s(r.get("Supplier")) for r in unpaid if _held(r) and _s(r.get("Supplier"))})
    held_text = ", ".join(parties[:3]) + (f" +{len(parties) - 3}" if len(parties) > 3 else "")
    free = [r for r in recs if not _held(r)]
    fifo_free = fifo_position(_bills_of(free), _oa_sum(oa_recs, False), today)
    fifo_all = fifo_position(_bills_of(recs), _oa_sum(oa_recs), today)
    oldest = [d for d in (_d(r.get("MR Date")) for r in unpaid) if d is not None]
    last = [d for d in (_d(r.get("Last Payment")) for r in recs) if d is not None]
    return {
        "Bills": len(recs),
        "Unpaid Bills": len(unpaid),
        "Bill Amount": round(sum(_f(r.get("Bill Amount")) for r in recs), 2),
        "Paid": round(sum(_f(r.get("Paid")) for r in recs), 2),
        "Pending": round(pending, 2),
        "On Hold": round(held_pending, 2),
        "Free To Pay": round(pending - held_pending, 2),
        "On Account": round(_oa_sum(oa_recs), 2),
        "Hold": hold,
        "Held Parties": held_text,
        "Companies": ", ".join(sorted({_s(r.get("Step 1 Co")) for r in unpaid
                                       if _s(r.get("Step 1 Co"))})),
        "Oldest Unpaid Bill": min(oldest) if oldest else None,
        "Cleared Till": fifo_free["cleared_till"],
        "Pending From": fifo_free["pending_from"],
        "Days Pending": fifo_free["days_pending"],
        "Days Incl. Hold": fifo_all["days_pending"],
        "Last Payment": max(last) if last else None,
    }


def _whole_days(df: pd.DataFrame) -> pd.DataFrame:
    """Day counts as whole numbers; a supplier with nothing free to pay has no
    days (<NA>), which a float column would show as NaN."""
    for col in ("Days Pending", "Days Incl. Hold"):
        df[col] = pd.array([None if _isnull(v) else int(v) for v in df[col]], dtype="Int64")
    return df


SUMMARY_COLUMNS = ["skey", "Jute Supplier", "Unpaid Bills", "Pending", "On Hold", "Free To Pay",
                   "On Account", "Hold", "Held Parties", "Companies", "Oldest Unpaid Bill",
                   "Cleared Till", "Pending From", "Days Pending", "Days Incl. Hold",
                   "Last Payment"]


def _group_by_skey(df) -> dict:
    out: dict = {}
    for r in _records(df):
        out.setdefault(r["skey"], []).append(r)
    return out


def supplier_summary(keyed_rows, on_account_keyed, today) -> pd.DataFrame:
    rows = _group_by_skey(keyed_rows)
    oas = _group_by_skey(on_account_keyed)
    out = []
    for key, recs in rows.items():
        m = _metrics(recs, oas.get(key, []), today)
        m["skey"] = key
        m["Jute Supplier"] = recs[0].get("Jute Supplier")
        out.append(m)
    if not out:
        return pd.DataFrame(columns=SUMMARY_COLUMNS)
    df = _whole_days(pd.DataFrame(out)[SUMMARY_COLUMNS])
    return df.sort_values("Pending", ascending=False, kind="stable").reset_index(drop=True)


# ---- periods ------------------------------------------------------------------
def _fy_start(d: date) -> int:
    return d.year if d.month >= 4 else d.year - 1


def period_label(d: date, months_from: date) -> str:
    if d >= months_from:
        return d.strftime("%b-%y")
    s = _fy_start(d)
    return f"FY {s % 100:02d}-{(s + 1) % 100:02d}"


def period_matrix(keyed_rows, months_from, by="skey") -> pd.DataFrame:
    cells: dict = {}
    order: dict = {}
    for r in _records(keyed_rows):
        d = _d(r.get("MR Date"))
        pend = _f(r.get("Pending"))
        if d is None or abs(pend) <= EPS:
            continue
        label = period_label(d, months_from)
        order[label] = (0, _fy_start(d), 1) if d < months_from else (1, d.year, d.month)
        cells[(r[by], label)] = cells.get((r[by], label), 0.0) + pend
    totals: dict = {}
    for (_k, lab), v in cells.items():
        totals[lab] = totals.get(lab, 0.0) + v
    labels = sorted((lab for lab, v in totals.items() if abs(v) > EPS), key=lambda lab: order[lab])
    keys = sorted({k for k, lab in cells if lab in labels}, key=str)
    data = {lab: [round(cells.get((k, lab), 0.0), 2) for k in keys] for lab in labels}
    return pd.DataFrame(data, index=pd.Index(keys, name=by), columns=labels)


COMPANY_COLUMNS = ["Company", "Bills", "Unpaid Bills", "Bill Amount", "Paid", "Pending", "On Hold",
                   "On Account", "Oldest Unpaid Bill", "Cleared Till", "Pending From",
                   "Days Pending", "Days Incl. Hold", "Last Payment"]


def company_summary(keyed_rows, on_account_keyed, skey, today) -> pd.DataFrame:
    recs = [r for r in _records(keyed_rows) if r["skey"] == skey]
    oas = [o for o in _records(on_account_keyed) if o["skey"] == skey]
    out = []
    for co in sorted({_s(r.get("Step 1 Co")) for r in recs}):
        m = _metrics([r for r in recs if _s(r.get("Step 1 Co")) == co],
                     [o for o in oas if _s(o.get("co_prefix")) == co], today)
        m["Company"] = co
        out.append(m)
    if recs:
        m = _metrics(recs, oas, today)
        m["Company"] = "All companies"
        out.append(m)
    if not out:
        return pd.DataFrame(columns=COMPANY_COLUMNS)
    return _whole_days(pd.DataFrame(out)[COMPANY_COLUMNS])


BILL_COLUMNS = ["Step 1 Co", "Gate Entry Co", "MR No", "MR Date", "Supplier", "Bill", "Bill Amount",
                "Paid", "Pending", "FIFO Pending", "On hold", "Status", "Due Date", "Last Payment"]


def supplier_bills(keyed_rows, on_account_keyed, skey) -> pd.DataFrame:
    recs = [r for r in _records(keyed_rows) if r["skey"] == skey]
    if not recs:
        return pd.DataFrame(columns=BILL_COLUMNS)
    oas = [o for o in _records(on_account_keyed) if o["skey"] == skey]
    recs.sort(key=lambda r: (_d(r.get("MR Date")) is not None, _d(r.get("MR Date")) or date.min))
    free = [r for r in recs if not _held(r)]
    fifo = fifo_position(_bills_of(free), _oa_sum(oas, False))["fifo_pending"]
    by_id = {id(r): fifo[i] for i, r in enumerate(free)}
    out = []
    for r in recs:
        row = {c: r.get(c) for c in BILL_COLUMNS}
        row["FIFO Pending"] = None if _held(r) else by_id[id(r)]
        out.append(row)
    return pd.DataFrame(out, columns=BILL_COLUMNS)


# ---- others -> supplier -------------------------------------------------------
SUGGEST_COLUMNS = ["party_id", "Party", "Companies", "Bills", "Unpaid Bills", "Pending",
                   "Sheet Names", "Suggested", "suggested_id", "Basis", "Confirmed"]


def suggest_party_suppliers(rows, master, party_history, party_map) -> pd.DataFrame:
    m = _clean_master(master)
    real = _real(m)
    parties: dict = {}
    for r in _records(rows):
        pid = _int(r.get("party_id"))
        if pid is None or resolve_supplier(r, m, {})[0] is not None:
            continue
        parties.setdefault(pid, []).append(r)
    hist: dict = {}
    for h in _records(party_history):
        pid, sid = _int(h.get("party_id")), _int(h.get("jute_supplier_id"))
        if pid is None or sid is None:
            continue
        d = hist.setdefault(pid, {})
        d[sid] = d.get(sid, 0) + int(_f(h.get("n")))
    out = []
    for pid, recs in parties.items():
        # the sheet wrote one broker several ways ("Binod P" / "binod p"): count
        # them as one name, shown in its most used spelling
        spellings: dict = {}
        for r in recs:
            sp = _s(r.get("sheet_party"))
            if sp:
                by = spellings.setdefault(norm_name(sp), {})
                by[sp] = by.get(sp, 0) + 1
        counts = {max(by, key=lambda k: (by[k], k)): sum(by.values())
                  for by in spellings.values()}
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        usable = [kv for kv in ranked if norm_name(kv[0]) not in _OTHER_NAMES]
        sid, basis = None, ""
        if usable:
            top, n = usable[0]
            sid = match_master(top, m)
            if sid is not None:
                basis = f"Sheet name '{top}' on {n} of {len(recs)} bills"
        party_name = next((_s(r.get("Supplier")) for r in recs if _s(r.get("Supplier"))), "")
        if sid is None and party_name:
            # the party itself is in the jute supplier master under its own name
            same = [i for i, nm in real.items() if norm_name(nm) == norm_name(party_name)]
            if len(same) == 1:
                sid = same[0]
                basis = "The party's own name is in the jute supplier master"
                if usable:
                    basis += f" (sheet name '{usable[0][0]}' is not)"
        if sid is None:
            h = {k: v for k, v in hist.get(pid, {}).items() if k in real}
            if h:
                sid = max(h, key=lambda k: (h[k], -k))
                basis = f"Jute supplier on {h[sid]} of {sum(hist[pid].values())} MRs of this party"
            elif usable:
                basis = f"Sheet name '{usable[0][0]}' has no match in the master"
        confirmed = ""
        if party_map and pid in party_map:
            cid = _int(party_map[pid])
            confirmed = OTHERS if cid is None else m.get(cid, "")
        out.append({
            "party_id": pid,
            "Party": party_name,
            "Companies": ", ".join(sorted({_s(r.get("Step 1 Co")) for r in recs
                                           if _s(r.get("Step 1 Co"))})),
            "Bills": len(recs),
            "Unpaid Bills": sum(1 for r in recs if _f(r.get("Pending")) > EPS),
            "Pending": round(sum(_f(r.get("Pending")) for r in recs), 2),
            "Sheet Names": ", ".join(f"{k} ({v})" for k, v in ranked),
            "Suggested": real[sid] if sid is not None else "",
            "suggested_id": sid,
            "Basis": basis,
            "Confirmed": confirmed,
        })
    if not out:
        return pd.DataFrame(columns=SUGGEST_COLUMNS)
    df = pd.DataFrame(out)[SUGGEST_COLUMNS]
    return df.sort_values("Pending", ascending=False, kind="stable").reset_index(drop=True)


GROUP_COLUMNS = ["party_ids", "Party", "Companies", "Bills", "Unpaid Bills", "Pending",
                 "Sheet Names", "Suggested", "suggested_id", "Basis", "Confirmed"]


def group_suggestions(suggestions) -> pd.DataFrame:
    """The confirmation list as the owner reads it: party_mst holds one record
    per company for the same party, so the per-party_id rows of
    suggest_party_suppliers are put together when the party name, the sheet
    name(s), the suggestion and what is confirmed are all the same. One answer
    then covers every party_id in `party_ids`. A party that the sheet books
    under two names (S.S. Rungta: 'rungta' in some companies, 'ss rungta adpl'
    in another) stays on separate rows. Biggest pending first."""
    groups: dict = {}
    for r in _records(suggestions):
        sheet = tuple(sorted(norm_name(part.rsplit("(", 1)[0])
                             for part in _s(r.get("Sheet Names")).split(",") if part.strip()))
        key = (norm_name(r.get("Party")), sheet, _int(r.get("suggested_id")), _s(r.get("Confirmed")))
        groups.setdefault(key, []).append(r)
    out = []
    for recs in groups.values():
        names: dict = {}
        for r in recs:
            for part in _s(r.get("Sheet Names")).split(","):
                if not part.strip():
                    continue
                name, _, n = part.strip().rpartition(" (")
                same = next((k for k in names if norm_name(k) == norm_name(name)), name)
                names[same] = names.get(same, 0) + int(n.rstrip(")") or 0)
        companies = sorted({c.strip() for r in recs for c in _s(r.get("Companies")).split(",")
                            if c.strip()})
        first = recs[0]
        out.append({
            "party_ids": sorted(int(r["party_id"]) for r in recs),
            "Party": _s(first.get("Party")),
            "Companies": ", ".join(companies),
            "Bills": sum(int(_f(r.get("Bills"))) for r in recs),
            "Unpaid Bills": sum(int(_f(r.get("Unpaid Bills"))) for r in recs),
            "Pending": round(sum(_f(r.get("Pending")) for r in recs), 2),
            "Sheet Names": ", ".join(f"{k} ({v})" for k, v in
                                     sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))),
            "Suggested": _s(first.get("Suggested")),
            "suggested_id": _int(first.get("suggested_id")),
            "Basis": (_s(first.get("Basis")) if len(recs) == 1
                      else re.sub(r" on \d+ of \d+ (bills|MRs of this party)", "",
                                  _s(first.get("Basis")))),
            "Confirmed": _s(first.get("Confirmed")),
        })
    if not out:
        return pd.DataFrame(columns=GROUP_COLUMNS)
    df = pd.DataFrame(out)[GROUP_COLUMNS]
    return df.sort_values("Pending", ascending=False, kind="stable").reset_index(drop=True)
