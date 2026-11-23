"""Dry run of loading the owner's jute payments workbook ("Entry sheet") into
VoW accounts. READ ONLY: SELECTs on sls inside a READ ONLY transaction, and an
Excel report of what an import would do. It writes nothing to the database.

Owner rulings 2026-11-23: accounts start 1 Apr 2025 for every company; the
sheet's amount is what is owed; payments go to a holding account per company;
Stop parties load On hold; bills without an MR load as plain bills.

Run:  uv run --with openpyxl python -m scripts.payment_sheet_dry_run <workbook.xlsx> [out.xlsx]
"""
import collections
import re
import sys
from datetime import date, datetime

import pandas as pd
from sqlalchemy import text

from src.jutetransfer.database import DatabaseConnection

START = date(2025, 4, 1)                      # accounts start (owner ruling)
# Sheet code of the step-1 company -> (co_id, branch_id) on sls
COMPANIES = {"GMPL": (27, 33), "SCPL": (81, 36), "AVPL": (67, 35), "BDPL": (68, 19),
             "JTSPL": (74, 20), "DVPL": (80, 21), "JMPL": (75, 14), "ACPL": (65, 30),
             "SSPL": (82, 11), "DDPL": (26, 32), "SDPL": (45, 34), "ADNOIS": (99, 106),
             "MBPL": (91, 37), "LCPL": (106, 87), "EJM": (2, 29), "NJM": (1, 4)}


def num(v) -> float:
    return float(v) if isinstance(v, (int, float)) else 0.0


def as_date(v):
    return v.date() if isinstance(v, datetime) else None


def fy_start(d: date) -> int:
    return d.year if d.month >= 4 else d.year - 1


def mr_number(s: str):
    """The running number inside the sheet's MR no. (ACPL/JMR/26-27/00024,
    GM57/26-27, GT/33, 56)."""
    s = s.strip()
    if "JMR" in s:
        m = re.search(r"/0*(\d+)$", s)
    elif re.search(r"\d\d-\d\d$", s):
        m = re.search(r"(\d+)\s*/\s*\d\d-\d\d$", s)
    else:
        m = re.search(r"(\d+)$", s)
    return int(m.group(1)) if m else None


def norm(name) -> str:
    s = re.sub(r"\b(M/S\.?|MESSRS|PVT|PRIVATE|LTD|LIMITED|LLP|AND|&|CO)\b", " ", str(name or "").upper())
    return re.sub(r"[^A-Z0-9]", "", s)


def read_sheet(path: str) -> list:
    import openpyxl
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True)["Entry sheet"]
    out = []
    for i, r in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not (r[0] or r[2]):
            continue
        out.append(dict(row=i, co=str(r[0] or "").strip(), ic=str(r[1] or "").strip(),
                        mr_no=str(r[2] or "").strip(), mr_date=as_date(r[3]),
                        party=str(r[6] or "").strip(), supplier=str(r[7] or "").strip(),
                        bill=round(num(r[8]), 2), status=str(r[9] or "").strip().lower(),
                        paid=round(num(r[10]), 2), pending=round(num(r[11]), 2),
                        pay_date=as_date(r[12]), due=as_date(r[13]),
                        terms=r[14] if isinstance(r[14], (int, float)) else None,
                        active=str(r[15] or "").strip().lower(), to_pay_on=as_date(r[16])))
    return out


def classify(r: dict) -> str:
    """What the row becomes with accounts starting on START."""
    d = r["mr_date"]
    if d is None:
        return "no bill date"
    if d >= START:
        return "bill"                                   # posted from its MR
    if r["pending"] > 1 or (r["pay_date"] and r["pay_date"] >= START):
        return "opening"                                # open on 31 Mar 2025
    return "closed before start"


def main(path: str, out_path: str) -> None:
    rows = read_sheet(path)
    with DatabaseConnection.get_engine().connect() as conn:
        conn.execute(text("START TRANSACTION READ ONLY"))
        branches = sorted({b for _, b in COMPANIES.values()})
        mrs = pd.read_sql_query(text(f"""
            SELECT jute_mr_id, branch_id, branch_mr_no, jute_mr_date, net_total, total_amount,
                   claim_amount, tds_amount, status_id, party_id, party_branch_id, jute_supplier_id,
                   src_jute_mr_id, COALESCE(transfer_mode, 0) AS transfer_mode
            FROM jute_mr WHERE branch_id IN ({','.join(map(str, branches))})
              AND branch_mr_no IS NOT NULL AND jute_mr_date IS NOT NULL"""), conn)
        parties = pd.read_sql_query(text("SELECT party_id, co_id, supp_name, active FROM party_mst"), conn)
        pbranch = {int(x[0]) for x in conn.execute(text(
            "SELECT DISTINCT party_id FROM party_branch_mst WHERE COALESCE(active, 1) = 1")).fetchall()}
        suppliers = {int(x[0]): x[1] for x in conn.execute(text(
            "SELECT supplier_id, supplier_name FROM jute_supplier_mst")).fetchall()}
        first_hops = {int(x[0]) for x in conn.execute(text(
            "SELECT MIN(jute_mr_id) FROM jute_mr WHERE src_jute_mr_id IS NOT NULL "
            "AND COALESCE(transfer_mode, 0) = 0 GROUP BY src_jute_mr_id")).fetchall()}
        roots_with_chain = {int(x[0]) for x in conn.execute(text(
            "SELECT DISTINCT src_jute_mr_id FROM jute_mr WHERE src_jute_mr_id IS NOT NULL "
            "AND COALESCE(transfer_mode, 0) = 0")).fetchall()}
        acc = pd.read_sql_query(text(
            "SELECT co_id, posting_start_date FROM acc_company_settings"), conn)
        conn.rollback()

    mrs["jute_mr_date"] = pd.to_datetime(mrs["jute_mr_date"]).dt.date
    mrs["fy"] = mrs["jute_mr_date"].map(fy_start)
    by_key = collections.defaultdict(list)
    for m in mrs.to_dict("records"):
        by_key[(int(m["branch_id"]), int(m["fy"]), int(m["branch_mr_no"]))].append(m)
    party_name = {int(p["party_id"]): p["supp_name"] for p in parties.to_dict("records")}
    party_co = {int(p["party_id"]): p["co_id"] for p in parties.to_dict("records")}
    by_co_name = collections.defaultdict(lambda: collections.defaultdict(list))
    for p in parties.to_dict("records"):
        if p["co_id"] is not None and not pd.isna(p["co_id"]):
            by_co_name[int(p["co_id"])][norm(p["supp_name"])].append(int(p["party_id"]))
    activated = {int(a["co_id"]): a["posting_start_date"] for a in acc.to_dict("records")}

    # pass 1: match every row to its MR
    for r in rows:
        r["kind"] = classify(r)
        co = COMPANIES.get(r["ic"])
        r["co_id"], r["branch_id"] = co if co else (None, None)
        r["mr"], r["match"] = None, ""
        n = mr_number(r["mr_no"])
        if not co:
            r["match"] = "unknown company code"
        elif r["mr_date"] is None or n is None:
            r["match"] = "no MR number"
        else:
            found = by_key.get((co[1], fy_start(r["mr_date"]), n), [])
            if not found:
                r["match"] = "no MR in sls"
            else:
                exact = [m for m in found if abs(float(m["net_total"] or 0) - r["bill"]) <= 2
                         or abs(float(m["total_amount"] or 0) - float(m["claim_amount"] or 0)
                                - r["bill"]) <= 2]
                r["mr"] = (exact or found)[0]
                r["match"] = "matched" if exact else "matched, amount differs"

    # the broker nickname -> jute supplier, learnt from the matched rows
    votes = collections.defaultdict(collections.Counter)
    for r in rows:
        if r["mr"] and r["mr"]["jute_supplier_id"] and not pd.isna(r["mr"]["jute_supplier_id"]):
            votes[r["party"].lower()][int(r["mr"]["jute_supplier_id"])] += 1
    nick = {k: v.most_common(1)[0] for k, v in votes.items()}

    # pass 2: party / party branch / jute supplier, and problems
    for r in rows:
        problems, m = [], r["mr"]
        r["party_id"] = r["jute_supplier_id"] = None
        r["party_name_sls"] = r["jute_supplier_sls"] = ""
        if r["kind"] in ("closed before start", "no bill date"):
            r["problems"] = "" if r["kind"] != "no bill date" else "no bill date in the sheet"
            continue
        if r["match"] in ("unknown company code",):
            problems.append(f"company code '{r['ic']}' not known")
        if m:
            if m["party_id"] is None or pd.isna(m["party_id"]) or int(m["party_id"]) not in party_name:
                problems.append("the MR has no party / party not in party master")
            else:
                r["party_id"] = int(m["party_id"])
                r["party_name_sls"] = party_name[r["party_id"]]
                if party_co.get(r["party_id"]) != r["co_id"]:
                    problems.append("the MR's party belongs to another company")
                a, b = norm(r["party_name_sls"]), norm(r["supplier"])
                if a != b and a[:8] != b[:8] and a not in b and b not in a:
                    problems.append("supplier name in the sheet differs from the MR's party")
                if r["party_id"] not in pbranch:
                    problems.append("party has no party branch")
                elif m["party_branch_id"] is None or pd.isna(m["party_branch_id"]):
                    problems.append("the MR has no party branch set")
            if m["jute_supplier_id"] is None or pd.isna(m["jute_supplier_id"]):
                problems.append("the MR has no jute supplier")
            else:
                r["jute_supplier_id"] = int(m["jute_supplier_id"])
                r["jute_supplier_sls"] = suppliers.get(r["jute_supplier_id"], "?")
                usual = nick.get(r["party"].lower())
                if usual and usual[0] != r["jute_supplier_id"] and usual[1] >= 3:
                    problems.append(f"sheet party '{r['party']}' is usually jute supplier "
                                    f"'{suppliers.get(usual[0], usual[0])}', the MR has "
                                    f"'{r['jute_supplier_sls']}'")
            if int(m["status_id"] or 0) != 3:
                problems.append(f"the MR is not Approved (status {m['status_id']})")
            if r["kind"] == "bill":
                mid = int(m["jute_mr_id"])
                is_bill = (mid in first_hops) if not pd.isna(m["src_jute_mr_id"]) \
                    else (mid not in roots_with_chain)
                if int(m["transfer_mode"]) != 0 or not is_bill:
                    problems.append("the MR is not a step-1 / untransferred MR (inter-company?)")
            if r["match"] == "matched, amount differs":
                problems.append(f"amount differs: sheet {r['bill']:,.2f}, MR net "
                                f"{float(m['net_total'] or 0):,.2f} (sheet amount used)")
        else:
            if r["co_id"]:
                hits = by_co_name[r["co_id"]].get(norm(r["supplier"]), [])
                if len(hits) == 1:
                    r["party_id"], r["party_name_sls"] = hits[0], party_name[hits[0]]
                    if hits[0] not in pbranch:
                        problems.append("party has no party branch")
                elif not hits:
                    problems.append(f"no MR, and supplier '{r['supplier']}' is not a party of {r['ic']}")
                else:
                    problems.append(f"no MR, and {len(hits)} parties of {r['ic']} are named "
                                    f"'{r['supplier']}'")
                usual = nick.get(r["party"].lower())
                if usual:
                    r["jute_supplier_id"] = usual[0]
                    r["jute_supplier_sls"] = suppliers.get(usual[0], "?")
                else:
                    problems.append(f"no MR, and sheet party '{r['party']}' matches no jute supplier")
            if r["kind"] == "bill":
                problems.append("bill dated after the start but no MR in sls: cannot be posted "
                                "from an MR")
        if r["paid"] > 0 and not r["pay_date"]:
            problems.append("paid amount without a payment date")
        if r["pay_date"] and r["pay_date"] > date.today():
            problems.append(f"payment date {r['pay_date']} is in the future")
        if r["paid"] > r["bill"] + 1:
            problems.append(f"paid {r['paid']:,.2f} is more than the bill (excess goes on account)")
        if r["pay_date"] and r["mr_date"] and r["pay_date"] < r["mr_date"]:
            problems.append("payment dated before the bill (advance)")
        if r["kind"] == "opening" and r["pay_date"] and r["pay_date"] < START and r["paid"] > 0:
            problems.append("part paid before the start: opening taken as the pending amount")
        r["problems"] = "; ".join(problems)

    def opening_amount(r):
        paid_before = r["paid"] if (r["pay_date"] and r["pay_date"] < START) else 0.0
        return round(r["bill"] - paid_before, 2)

    base = ["row", "co", "ic", "mr_no", "mr_date", "party", "supplier", "bill", "paid", "pending",
            "pay_date", "due", "terms", "active"]
    def frame(sel, extra):
        data = []
        for r in sel:
            d = {k: r[k] for k in base}
            d.update(jute_mr_id=int(r["mr"]["jute_mr_id"]) if r["mr"] else None, match=r["match"],
                     party_id=r["party_id"], party_in_sls=r["party_name_sls"],
                     jute_supplier_in_sls=r["jute_supplier_sls"],
                     on_hold="Yes" if r["active"] == "stop" else "", problems=r["problems"])
            d.update(extra(r))
            data.append(d)
        return pd.DataFrame(data)

    opening = frame([r for r in rows if r["kind"] == "opening"],
                    lambda r: {"opening_amount": opening_amount(r)})
    bills = frame([r for r in rows if r["kind"] == "bill"],
                  lambda r: {"mr_net": float(r["mr"]["net_total"] or 0) if r["mr"] else None})
    pays = [r for r in rows if r["paid"] > 0 and r["pay_date"] and r["pay_date"] >= START
            and r["kind"] in ("opening", "bill")]
    pay_rows = frame(pays, lambda r: {"allocated": min(r["paid"], r["bill"]),
                                      "on_account": max(round(r["paid"] - r["bill"], 2), 0.0)})
    groups = (pay_rows.groupby(["ic", "supplier", "pay_date"], dropna=False)
              .agg(bills=("row", "size"), amount=("paid", "sum"), on_account=("on_account", "sum"))
              .reset_index().sort_values(["pay_date", "ic", "supplier"]))
    problems = pd.concat([opening.assign(kind="opening"), bills.assign(kind="bill")], ignore_index=True)
    problems = problems[problems["problems"] != ""][["kind"] + base + ["match", "party_in_sls",
                                                                     "jute_supplier_in_sls", "problems"]]
    kinds = collections.Counter()
    for p in problems["problems"]:
        for one in p.split("; "):
            kinds[re.sub(r"'.*?'|[\d,]+\.\d\d|\d{4}-\d\d-\d\d|\(status .*?\)|\d+ parties", "…", one)] += 1
    per_co = []
    for ic, (co_id, _b) in COMPANIES.items():
        o, b, p = opening[opening["ic"] == ic], bills[bills["ic"] == ic], pay_rows[pay_rows["ic"] == ic]
        if o.empty and b.empty:
            continue
        per_co.append(dict(company=ic, co_id=co_id,
                           in_accounts=("from " + str(activated[co_id])) if co_id in activated else "no",
                           opening_bills=len(o), opening_amount=o["opening_amount"].sum(),
                           bills_since_start=len(b), bills_amount=b["bill"].sum(),
                           bills_without_mr=int((b["jute_mr_id"].isna()).sum()),
                           payments_rows=len(p), payments_amount=p["paid"].sum(),
                           pending_now=o["pending"].sum() + b["pending"].sum(),
                           on_hold_bills=int((o["on_hold"] == "Yes").sum() + (b["on_hold"] == "Yes").sum())))
    summary = pd.DataFrame(per_co)
    counts = collections.Counter(r["kind"] for r in rows)
    with pd.ExcelWriter(out_path) as xl:
        summary.to_excel(xl, sheet_name="By company", index=False)
        pd.DataFrame(sorted(kinds.items(), key=lambda x: -x[1]),
                     columns=["problem", "bills"]).to_excel(xl, sheet_name="Problem types", index=False)
        problems.to_excel(xl, sheet_name="Problems", index=False)
        opening.to_excel(xl, sheet_name="Opening bills", index=False)
        bills.to_excel(xl, sheet_name="Bills since start", index=False)
        pay_rows.to_excel(xl, sheet_name="Payments by bill", index=False)
        groups.to_excel(xl, sheet_name="Payment vouchers", index=False)

    print(f"sheet rows {len(rows)}: {dict(counts)}")
    print(f"opening bills {len(opening)}  amount {opening['opening_amount'].sum():,.0f}  "
          f"pending now {opening['pending'].sum():,.0f}")
    print(f"bills since {START} {len(bills)}  amount {bills['bill'].sum():,.0f}  "
          f"pending now {bills['pending'].sum():,.0f}")
    print(f"payments since {START}: {len(pay_rows)} bills, {pay_rows['paid'].sum():,.0f}, "
          f"{len(groups)} vouchers (company+supplier+date), on account {pay_rows['on_account'].sum():,.0f}")
    print("match:", dict(collections.Counter(r["match"] for r in rows if r["kind"] in ("opening", "bill"))))
    print(f"rows with a problem: {len(problems)}")
    for k, v in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"  {v:5d}  {k}")
    print(summary.to_string(index=False))
    print("report:", out_path)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "payment_sheet_dry_run.xlsx")
