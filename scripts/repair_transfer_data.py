"""Repair rows damaged by transfer-app bugs that are now fixed in code (sls only).

DRY-RUN BY DEFAULT: lists what each repair would change and writes nothing
(the planning runs in a READ ONLY transaction). Writes only with --apply,
--only <repair> and --expect <the count the dry-run showed>, after the owner
has seen the list and said yes. An --apply run prints every row it is about
to write and saves the whole plan (old and new values) in its JSON log
BEFORE the first write; every repaired row is its own transaction, re-checked
against the plan.

    python -m scripts.repair_transfer_data                 # show all repairs
    python -m scripts.repair_transfer_data --only godowns  # one repair
    python -m scripts.repair_transfer_data --apply --only finalized-net --expect 21

Repairs:
  finalized-net   Finalized chain roots whose claim_amount / net_total were
                  left NULL by finalize (the P&L read those purchases as 0).
                  Recomputes the root's money exactly as the ERP's approve
                  would have at the time (transfer._erp_recompute_money: line
                  totals, total, claim, 194Q TDS for the party, roundoff, net)
                  -- the same code finalize now runs. TDS is counted in MR-date
                  order (then MR number): only what the party had been paid
                  BEFORE a lorry is in its Rs 50 lakh count, so the first
                  Rs 50 lakh carry no TDS. (The ERP's Bill Pass save derives
                  TDS again from ALL the party's approved MRs of the year.)
                  The rows keep their updated_by / updated_date_time -- the
                  only record of when each lorry was finalized. Stops at the
                  first row that fails: later lorries count the earlier ones.
  unfinalized-party
                  Pending (13) chain roots that an earlier un-finalize left on
                  the last forwarding company as party (and with the
                  finalize's MR date): only roots whose party is one of the
                  group's own companies. Restores the party the chain derives
                  from its first step and the Pending hand-off MR date (NULL).
  godowns         Godowns whose ERP type ('J') the old "Save godown tags"
                  blanked: the ERP audit log shows J -> NULL as the godown's
                  last real type change. Sets them back to 'J'.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from src.jutetransfer.database import DatabaseConnection
from src.jutetransfer.queries import _get_financial_year_bounds, get_source_mr_full
from src.jutetransfer.transfer import (
    _derive_original_party, _erp_money, _erp_recompute_money,
)

DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / ".logs"
UPDATED_BY = 1  # same demo user id the app writes
REPAIRS = ("finalized-net", "unfinalized-party", "godowns")

# Chain roots, driven from the hop rows: jute_mr.src_jute_mr_id has no index.
_CHAIN_ROOTS_SQL = """
    SELECT DISTINCT h.src_jute_mr_id AS root_id
    FROM jute_mr h
    WHERE h.transfer_mode = 0 AND h.src_jute_mr_id IS NOT NULL
"""


def _rows(conn, sql, **params):
    return [dict(r._mapping) for r in conn.execute(text(sql), params).fetchall()]


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _write_json(path: Path, data: dict) -> None:
    """Replace the file in one step: a kill mid-write must not lose the old
    values."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=_json_default))
    os.replace(tmp, path)


def _norm(name) -> str:
    return " ".join(str(name or "").lower().replace(".", " ").split())


# ---------------------------------------------------------------------------
# Plans (read-only)
# ---------------------------------------------------------------------------

def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10]) if value else date.min


def _approval_order(mr) -> tuple:
    """MR date, then MR number, then id. (The 21 rows of 2026 give the same
    money in MR-number and in finalize-time order too: the Rs 50 lakh is
    crossed at the fifth lorry in each.)"""
    return (_as_date(mr["jute_mr_date"]), int(mr["branch_mr_no"] or 0), int(mr["jute_mr_id"]))


def _approved_before(conn, mr, repaired_total: dict, peers: dict) -> float:
    """What the ERP's 194Q count stood at when this MR was finalized: the
    total_amount of the party's approved MRs of the same financial year that
    come before it (an MR repaired earlier in this run counts with its
    repaired total)."""
    if not mr["party_id"] or not mr["jute_mr_date"]:
        return 0.0
    fy_start, fy_end = _get_financial_year_bounds(_as_date(mr["jute_mr_date"]))
    key = (str(mr["party_id"]), fy_start)
    if key not in peers:
        peers[key] = _rows(conn, """
            SELECT jute_mr_id, jute_mr_date, branch_mr_no, total_amount
            FROM jute_mr
            WHERE party_id = :party AND status_id = 3
              AND jute_mr_date IS NOT NULL
              AND jute_mr_date BETWEEN :fy_start AND :fy_end
        """, party=str(mr["party_id"]), fy_start=fy_start.strftime("%Y-%m-%d"),
            fy_end=fy_end.strftime("%Y-%m-%d"))
    mine = _approval_order(mr)
    return sum(float(repaired_total.get(int(p["jute_mr_id"]), p["total_amount"]) or 0)
               for p in peers[key]
               if int(p["jute_mr_id"]) != int(mr["jute_mr_id"]) and _approval_order(p) < mine)


def plan_finalized_net(conn) -> list:
    roots = [r["root_id"] for r in _rows(conn, _CHAIN_ROOTS_SQL)]
    todo = []
    for root_id in sorted(roots):
        mr = _rows(conn, """
            SELECT jute_mr_id, status_id, branch_mr_no, party_id, jute_mr_date,
                   total_amount, claim_amount, tds_amount, roundoff, net_total,
                   updated_by, updated_date_time
            FROM jute_mr WHERE jute_mr_id = :id
        """, id=root_id)[0]
        if int(mr["status_id"] or 0) != 3 or mr["branch_mr_no"] is None:
            continue
        if mr["claim_amount"] is not None and mr["net_total"] is not None:
            continue
        todo.append(mr)
    todo.sort(key=_approval_order)          # MR-date order; also the order they are repaired in
    repaired_total, peers, out = {}, {}, []
    for mr in todo:
        before = _approved_before(conn, mr, repaired_total, peers)
        new = _erp_money(conn, mr["jute_mr_id"], cumulative_previous=before)
        repaired_total[int(mr["jute_mr_id"])] = new["total_amount"]
        out.append({
            "jute_mr_id": mr["jute_mr_id"],
            "mr_no": mr["branch_mr_no"], "mr_date": mr["jute_mr_date"],
            "party_id": mr["party_id"], "approved_before": round(before, 2),
            "old": {k: mr[k] for k in ("total_amount", "claim_amount", "tds_amount",
                                       "roundoff", "net_total")},
            "new": new,
            # left as they are: the only record of when the lorry was finalized
            "stamp": {"updated_by": mr["updated_by"], "updated_date_time": mr["updated_date_time"]},
        })
    return out


def plan_unfinalized_party(conn) -> list:
    roots = [r["root_id"] for r in _rows(conn, _CHAIN_ROOTS_SQL)]
    group = {_norm(r["co_name"]) for r in _rows(conn, "SELECT co_name FROM co_mst")}
    out = []
    for root_id in sorted(roots):
        mr = _rows(conn, """
            SELECT r.jute_mr_id, r.status_id, r.branch_mr_no, r.party_id,
                   r.party_branch_id, r.jute_mr_date, r.updated_by, r.updated_date_time,
                   p.supp_name
            FROM jute_mr r LEFT JOIN party_mst p ON p.party_id = r.party_id
            WHERE r.jute_mr_id = :id
        """, id=root_id)[0]
        if int(mr["status_id"] or 0) != 13 or mr["branch_mr_no"] is not None:
            continue
        if _norm(mr["supp_name"]) not in group:
            continue  # not on one of the group's own companies: not this bug's damage
        step1_id = _rows(conn, """
            SELECT jute_mr_id FROM jute_mr
            WHERE src_jute_mr_id = :id AND transfer_mode = 0 ORDER BY jute_mr_id LIMIT 1
        """, id=root_id)
        if not step1_id:
            continue
        step1 = get_source_mr_full(step1_id[0]["jute_mr_id"], conn=conn)
        derived = _derive_original_party(conn, root_id, step1)
        if not derived:
            continue
        party, branch = derived
        party_changes = str(mr["party_id"]).strip() != party
        if not party_changes:
            continue  # the party is right; a stray MR date alone is left alone
        name = _rows(conn, "SELECT supp_name FROM party_mst WHERE party_id = :p", p=int(party))
        out.append({
            "jute_mr_id": root_id,
            "old": {"party_id": mr["party_id"], "party_branch_id": mr["party_branch_id"],
                    "jute_mr_date": mr["jute_mr_date"], "updated_by": mr["updated_by"],
                    "updated_date_time": mr["updated_date_time"]},
            "new": {"party_id": party, "party_branch_id": branch, "jute_mr_date": None},
            "old_party_name": mr["supp_name"],
            "new_party_name": name[0]["supp_name"] if name else None,
        })
    return out


def plan_godowns(conn) -> list:
    """Godowns without a type today whose LAST real type change in the ERP's
    own audit log is J -> NULL (a later MARKED or other type would mean
    somebody chose that on purpose)."""
    rows = _rows(conn, """
        SELECT w.warehouse_id, w.branch_id, b.branch_name, c.co_name, w.warehouse_name,
               a.change_time,
               JSON_TYPE(JSON_EXTRACT(a.old_value, '$.warehouse_type')) AS old_kind,
               JSON_UNQUOTE(JSON_EXTRACT(a.old_value, '$.warehouse_type')) AS old_type,
               JSON_TYPE(JSON_EXTRACT(a.new_value, '$.warehouse_type')) AS new_kind,
               JSON_UNQUOTE(JSON_EXTRACT(a.new_value, '$.warehouse_type')) AS new_type
        FROM warehouse_mst w
        JOIN trigger_audit_logs a
          ON a.table_name = 'warehouse_mst' AND a.primary_key_value = w.warehouse_id
        LEFT JOIN branch_mst b ON b.branch_id = w.branch_id
        LEFT JOIN co_mst c ON c.co_id = b.co_id
        WHERE w.warehouse_type IS NULL
        ORDER BY w.branch_id, w.warehouse_id, a.change_time
    """)

    def kind(json_kind, value):
        return None if json_kind in (None, "NULL") else value

    last_change = {}                       # warehouse_id -> its last REAL type change
    for r in rows:
        old, new = kind(r["old_kind"], r["old_type"]), kind(r["new_kind"], r["new_type"])
        if old != new:
            last_change[r["warehouse_id"]] = (old, new, r)
    return [{
        "warehouse_id": r["warehouse_id"], "branch_id": r["branch_id"],
        "branch_name": r["branch_name"], "co_name": r["co_name"],
        "warehouse_name": r["warehouse_name"],
        "blanked_at": r["change_time"],
        "old": {"warehouse_type": None}, "new": {"warehouse_type": "J"},
    } for old, new, r in last_change.values() if old == "J" and new is None]


PLANNERS = {
    "finalized-net": plan_finalized_net,
    "unfinalized-party": plan_unfinalized_party,
    "godowns": plan_godowns,
}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report(plans: dict, applying: bool = False) -> None:
    print("=" * 78)
    print("TRANSFER DATA REPAIRS — " + ("APPLYING: these rows are about to be written" if applying
                                        else "DRY RUN (nothing is written)"))
    print("=" * 78)
    if "finalized-net" in plans:
        p = plans["finalized-net"]
        print(f"\n[finalized-net] {len(p)} finalized root MR(s) with NULL claim / net total"
              " -> the ERP's own money rule (as its approve would have written it)")
        print("  TDS (194Q, 0.1%) is counted in MR-date order: nothing on the first 50,00,000"
              " bought from a party in the year.")
        print("  (An ERP Bill Pass save on one of these MRs derives its TDS again from ALL the"
              " party's approved MRs of the year.)")
        for r in p:
            o, n = r["old"], r["new"]
            print(f"  MR {r['jute_mr_id']} (MR no {r['mr_no']}, {_as_date(r['mr_date']):%d-%m-%Y}):"
                  f" total {o['total_amount']} -> {n['total_amount']:,.2f}"
                  f" | claim {o['claim_amount']} -> {n['claim_amount']:,.2f}"
                  f" | bought before {r['approved_before']:,.2f}"
                  f" | TDS {o['tds_amount']} -> {n['tds_amount']:,.2f}"
                  f" | net {o['net_total']} -> {n['net_total']:,.2f}")
        if p:
            print(f"  Purchases the P&L will now count: "
                  f"{sum(r['new']['net_total'] for r in p):,.2f} "
                  f"(TDS {sum(r['new']['tds_amount'] for r in p):,.2f})")
    if "unfinalized-party" in plans:
        p = plans["unfinalized-party"]
        print(f"\n[unfinalized-party] {len(p)} pending root MR(s) still on the forwarder as party")
        for r in p:
            print(f"  MR {r['jute_mr_id']}: party {r['old']['party_id']} "
                  f"({r['old_party_name']}) / branch {r['old']['party_branch_id']} -> "
                  f"{r['new']['party_id']} ({r['new_party_name']}) / branch "
                  f"{r['new']['party_branch_id']}; MR date {r['old']['jute_mr_date']} -> NULL")
    if "godowns" in plans:
        p = plans["godowns"]
        by_branch = {}
        for r in p:
            by_branch.setdefault(r["branch_id"], []).append(r)
        print(f"\n[godowns] {len(p)} godown(s) blanked by 'Save godown tags', back to 'J'")
        for b, rs in sorted(by_branch.items()):
            last = max(r["blanked_at"] for r in rs)
            names = ", ".join(r["warehouse_name"] for r in rs[:5])
            print(f"  {rs[0].get('co_name') or '?'} / {rs[0].get('branch_name') or '?'} (branch {b}): "
                  f"{len(rs)} (last blanked {last}) e.g. {names}" + (" ..." if len(rs) > 5 else ""))
    if not applying:
        print("\nNothing was written. After the owner's OK, one repair at a time:")
        for name, items in plans.items():
            print(f"    --apply --only {name} --expect {len(items)}")
        print("Each run prints its rows and saves old and new values in "
              ".logs/jt_repair_<time>.json before it writes.")


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _repair_finalized_net(conn, it) -> None:
    cur = conn.execute(text("""
        SELECT status_id, branch_mr_no, claim_amount, net_total
        FROM jute_mr WHERE jute_mr_id = :id FOR UPDATE
    """), {"id": it["jute_mr_id"]}).fetchone()
    if (not cur or int(cur[0] or 0) != 3 or cur[1] is None
            or (cur[2] is not None and cur[3] is not None)):
        raise RuntimeError("no longer a finalized root with a NULL claim / net")
    # The planned TDS is written as planned: derived again here it would
    # count every MR approved since, not only those before this lorry.
    got = _erp_recompute_money(conn, it["jute_mr_id"], tds_amount=it["new"]["tds_amount"])
    if any(abs(float(got[k]) - float(it["new"][k])) > 0.005 for k in got):
        raise RuntimeError(f"money changed since the dry run: {got}")
    # updated_by / updated_date_time stay: they are the only record of when
    # the lorry was finalized. The JSON log is the record of this repair.


def _repair_unfinalized_party(conn, it) -> None:
    res = conn.execute(text("""
        UPDATE jute_mr SET party_id = :party, party_branch_id = :pbr,
               jute_mr_date = NULL,
               updated_by = :by, updated_date_time = NOW()
        WHERE jute_mr_id = :id AND status_id = 13
          AND branch_mr_no IS NULL AND party_id = :old_party
    """), {"party": it["new"]["party_id"], "pbr": it["new"]["party_branch_id"],
           "by": UPDATED_BY, "id": it["jute_mr_id"],
           "old_party": str(it["old"]["party_id"])})
    if res.rowcount != 1:
        raise RuntimeError(f"expected 1 row, changed {res.rowcount} (row changed since the dry run?)")


def _repair_godown(conn, it) -> None:
    res = conn.execute(text("""
        UPDATE warehouse_mst SET warehouse_type = 'J'
        WHERE warehouse_id = :id AND warehouse_type IS NULL
    """), {"id": it["warehouse_id"]})
    if res.rowcount != 1:
        raise RuntimeError(f"expected 1 row, changed {res.rowcount} (row changed since the dry run?)")


REPAIR_FNS = {
    "finalized-net": _repair_finalized_net,
    "unfinalized-party": _repair_unfinalized_party,
    "godowns": _repair_godown,
}


def apply(plans: dict, log_path: Path) -> int:
    """One transaction per row; the whole plan is in the log before the first
    write. A row that no longer matches the plan is rolled back and reported.
    finalized-net stops at its first failure (a later lorry's TDS counts the
    earlier ones as repaired); the other repairs go on to the next row."""
    log = {"planned": plans, "repaired": [], "failed": [], "not_attempted": []}
    _write_json(log_path, log)
    for name, items in plans.items():
        for position, it in enumerate(items):
            key = it.get("jute_mr_id") or it.get("warehouse_id")
            try:
                with DatabaseConnection.get_transaction() as conn:
                    REPAIR_FNS[name](conn, it)
            except Exception as exc:
                log["failed"].append({"repair": name, **it, "error": str(exc)})
                print(f"  {name}: FAILED {key}: {exc}")
                if name == "finalized-net":
                    rest = items[position + 1:]
                    log["not_attempted"] += [{"repair": name, **r} for r in rest]
                    print(f"  {name}: STOPPED, {len(rest)} later row(s) not attempted "
                          "(their TDS counts this lorry). Take a new dry run.")
                    _write_json(log_path, log)
                    break
            else:
                log["repaired"].append({"repair": name, **it})
                print(f"  {name}: repaired {key}")
            _write_json(log_path, log)
    print(f"\nRepaired {len(log['repaired'])}, failed {len(log['failed'])}, "
          f"not attempted {len(log['not_attempted'])}. Old and new values: {log_path}")
    return 1 if log["failed"] else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--only", choices=REPAIRS, action="append",
                    help="run only this repair (repeatable; required with --apply)")
    ap.add_argument("--expect", type=int,
                    help="with --apply: the row count the dry-run showed for these repairs")
    ap.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    args = ap.parse_args(argv)
    if args.apply and (not args.only or args.expect is None):
        ap.error("--apply needs --only <repair> and --expect N (the count the dry-run showed)")
    chosen = args.only or list(REPAIRS)

    with DatabaseConnection.get_engine().connect() as conn:
        # Read-only planning: a READ ONLY transaction, not SET SESSION (that
        # would stay on the pooled connection and fail every --apply write).
        conn.execute(text("START TRANSACTION READ ONLY"))
        try:
            plans = {name: PLANNERS[name](conn) for name in chosen}
        finally:
            conn.rollback()

    if not args.apply:
        report(plans)
        return 0
    found = sum(len(v) for v in plans.values())
    if found != args.expect:
        print(f"REFUSED: --expect {args.expect} but {found} row(s) would be repaired now; "
              "re-run the dry run and check")
        return 2
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"jt_repair_{datetime.now():%Y%m%d_%H%M%S}.json"
    report(plans, applying=True)
    print(f"\nOld and new values of every row above: {log_path}\n")
    return apply(plans, log_path)


if __name__ == "__main__":
    sys.exit(main())
