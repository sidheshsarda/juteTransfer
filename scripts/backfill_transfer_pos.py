"""Create the transfer POs of chains that were saved before transfer POs
existed (sls only): a Forwarding PO for every chain hop MR without one, and a
Final PO for every finalized chain root without one.

DRY-RUN BY DEFAULT: prints what would be created and writes nothing (the
planning runs in a READ ONLY transaction). Uses the same po_ops functions as
the Transfer Chain page, driven from the stored MR rows, so a backfilled PO
is what the save would have produced.

    python -m scripts.backfill_transfer_pos                       # dry-run, everything
    python -m scripts.backfill_transfer_pos --root 28052          # dry-run, one lorry
    # writes need the owner's OK, ONE scope, and the count the dry-run showed:
    python -m scripts.backfill_transfer_pos --apply --root 28052 --expect 2   # the pilot
    python -m scripts.backfill_transfer_pos --apply --all --expect 195
    python -m scripts.backfill_transfer_pos --undo <log.json>                 # dry-run of the undo
    python -m scripts.backfill_transfer_pos --undo <log.json> --apply --expect 2

Scopes: --all (every chain), --root N (repeatable), --limit N (the next N
lorries per branch that still need a PO). The pilot lorry was root 28052
(Empire GE 1 of 05-08-2026), the oldest lorry at both Jagrati and Empire, so
the numbers still rise with the dates; a --root / --limit run that would
jump older lorries says so.

Numbers are taken in date order per branch (PO date, gate-entry no, MR id),
Forwarding POs first, then Final POs, one transaction per PO under the
branch's PO numbering lock (the ERP's protocol, po_ops.po_no_lock: an ERP PO
save at that branch waits, or this run does); re-running skips what already
exists. The run STOPS at the first failed PO and at the first PO whose number
turns out to be used twice (a writer that skipped the lock, or an older
duplicate): nothing after it has taken a number, so a re-run continues in
date order. Exit code 0 only when every planned PO was created and the
after-run check is clean.

Every --apply run writes a JSON log (the plan first, then each PO as it is
committed); --undo deletes exactly the PO ids in it, and only while each
still carries that MR's transfer marker and no other MR is linked to it.
Undo keeps the supplier-party map rows the run added (they are valid ERP
masters).

Run it while nobody raises POs at the mill and nobody saves chain steps, and
only after every running copy of this app has the transfer-PO code (the old
code never deletes a transfer PO).
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text

from src.jutetransfer import po_ops
from src.jutetransfer.database import DatabaseConnection, named_locks
from src.jutetransfer.po_helpers import (
    ROLE_FINAL, ROLE_FORWARD, format_po_no, fy_label, parse_marker,
)
from src.jutetransfer.queries import _get_financial_year_bounds

DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / ".logs"
UPDATED_BY = 1  # same demo user id the app writes

_CANDIDATES_SQL = """
    SELECT h.jute_mr_id AS hop_id, h.src_jute_mr_id AS root_id,
           h.branch_id AS hop_branch, h.jute_mr_date AS hop_date,
           h.jute_gate_entry_date AS hop_ge_date, h.po_id AS hop_po_id,
           r.branch_id AS root_branch, r.status_id AS root_status,
           r.branch_mr_no AS root_mr_no, r.jute_mr_date AS root_mr_date,
           r.jute_gate_entry_no AS ge_no, r.jute_gate_entry_date AS ge_date
    FROM jute_mr h
    JOIN jute_mr r ON r.jute_mr_id = h.src_jute_mr_id
    WHERE h.transfer_mode = 0 AND h.src_jute_mr_id IS NOT NULL
"""
# Driven from the hop rows on purpose: jute_mr.src_jute_mr_id has no index.


def _d(value):
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _po_date(*candidates) -> date:
    """The date the PO of an MR will carry, for sorting -- po_ops._plan takes
    the MR date, else the gate-entry date, else today (so: undated last)."""
    for value in candidates:
        if _d(value):
            return _d(value)
    return date.max


def _forward_key(r):
    return (_po_date(r["hop_date"], r["hop_ge_date"]), int(r["ge_no"] or 0), int(r["hop_id"]))


def _final_key(r):
    return (_po_date(r["root_mr_date"], r["ge_date"]), int(r["ge_no"] or 0), int(r["root_id"]))


def _write_json(path: Path, data: dict) -> None:
    """Replace the file in one step: a kill mid-write must not lose the list
    an undo needs."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str))
    os.replace(tmp, path)


def load_work(conn, root_filter=None):
    """(forward_jobs, final_jobs): hops and finalized roots, each list in the
    order numbers must be taken (PO date, gate-entry no, id)."""
    rows = [dict(r._mapping) for r in conn.execute(text(_CANDIDATES_SQL)).fetchall()]
    if root_filter:
        rows = [r for r in rows if int(r["root_id"]) in root_filter]
    forward = sorted(rows, key=_forward_key)
    roots = {}
    for r in rows:
        # Finalized = the root carries its MR number and is Approved (what the
        # Transfer Chain page treats as "returned").
        if r["root_mr_no"] is not None and int(r["root_status"] or 0) == 3:
            roots[int(r["root_id"])] = r
    final = sorted(roots.values(), key=_final_key)
    return forward, final


def build_plans(conn, forward, final):
    """Plan every job (read-only). Returns a list of entries
    {'job', 'plan', 'mr_lines', 'names'} in processing order."""
    names = {}

    def name(kind, key):
        if key is None:
            return None
        if (kind, key) not in names:
            if kind == "party":
                names[(kind, key)] = po_ops._party_name(conn, key)
            else:
                row = conn.execute(text(
                    "SELECT supplier_name FROM jute_supplier_mst WHERE supplier_id = :id"
                ), {"id": key}).fetchone()
                names[(kind, key)] = row[0] if row else None
        return names[(kind, key)]

    entries = []
    for role, jobs, key in ((ROLE_FORWARD, forward, "hop_id"), (ROLE_FINAL, final, "root_id")):
        for job in jobs:
            mr_id = int(job[key])
            plan = (po_ops.plan_forward_po if role == ROLE_FORWARD else po_ops.plan_final_po)(conn, mr_id)
            entries.append({
                "job": job, "plan": plan, "mr_lines": po_ops._load_mr_lines(conn, mr_id),
                "names": {"party": name("party", plan.get("party_id")),
                          "supplier": name("supplier", plan.get("supplier_id"))},
            })
    return entries


def _limit_planned(entries, limit):
    """--limit N: the next N lorries per branch that still need a PO
    (Forwarding and Final counted apart); entries that need nothing stay."""
    if not limit:
        return entries
    seen, out = defaultdict(int), []
    for e in entries:
        p = e["plan"]
        if p.get("skipped"):
            out.append(e)
            continue
        key = (p["role"], p["branch_id"])
        if seen[key] < limit:
            seen[key] += 1
            out.append(e)
    return out


def older_unserved(conn, entries, forward_all, final_all):
    """For a --root / --limit run: lorries of the same branch that are OLDER
    than one in this run and would still be without a PO after it -- they
    will get higher numbers later. A list of warning lines."""
    in_run = {(e["plan"]["role"], e["plan"]["mr_id"]) for e in entries if not e["plan"].get("skipped")}
    with_final = set()
    for (note,) in conn.execute(text(
            "SELECT internal_note FROM jute_po WHERE internal_note LIKE 'JT|FINAL|%'")).fetchall():
        marker = parse_marker(note)
        if marker:
            with_final.add(marker["root_mr_id"])
    out = []
    for role, jobs, id_key, branch_key, has_po in (
            (ROLE_FORWARD, forward_all, "hop_id", "hop_branch", lambda j: j["hop_po_id"] is not None),
            (ROLE_FINAL, final_all, "root_id", "root_branch", lambda j: int(j["root_id"]) in with_final)):
        waiting = defaultdict(list)             # branch -> older lorries still without a PO
        jumped = defaultdict(set)
        for job in jobs:
            branch, mr_id = int(job[branch_key]), int(job[id_key])
            if (role, mr_id) in in_run:
                jumped[branch].update(waiting[branch])
            elif not has_po(job):
                waiting[branch].append(mr_id)
        for branch, mrs in sorted(jumped.items()):
            if mrs:
                shown = ", ".join(str(m) for m in sorted(mrs)[:8]) + (" ..." if len(mrs) > 8 else "")
                out.append(f"WARNING: branch {branch}: {len(mrs)} older lorry/lorries still need a "
                           f"{'Forwarding' if role == ROLE_FORWARD else 'Final'} PO (MR {shown}); "
                           "they will get HIGHER numbers than this run's.")
    return out


def simulate_numbers(conn, entries):
    """Give every planned PO the number it would take if the run started
    now: next free number per branch + financial year, in processing order."""
    next_no = {}
    for e in entries:
        p = e["plan"]
        if p.get("skipped"):
            continue
        key = (p["branch_id"], fy_label(p["po_date"]))
        if key not in next_no:
            next_no[key] = po_ops._next_po_no(conn, p["branch_id"], p["po_date"])
        p["sim_po_no"] = next_no[key]
        p["sim_po_no_formatted"] = format_po_no(
            next_no[key], p.get("co_prefix"), p.get("branch_prefix"), p["po_date"])
        next_no[key] += 1


def _mr_amount(mr_lines):
    return sum(float(l["accepted_weight"] or 0) * float(l["rate"] or 0) / 100.0
               for l in mr_lines if float(l["accepted_weight"] or 0) > 0)


def _mr_kg(mr_lines):
    return sum(float(l["accepted_weight"] or 0) for l in mr_lines
               if float(l["accepted_weight"] or 0) > 0)


def _print_po(e, title):
    p, job, names = e["plan"], e["job"], e.get("names") or {}
    print(f"\n  --- {title} ---")
    print(f"  {p['role']} PO {p.get('sim_po_no_formatted', '?')} dated {p['po_date']} "
          f"at {p.get('co_name')} | lorry GE {job['ge_no']} dt {_d(job['ge_date'])} "
          f"| MR {p['mr_id']} | original PO {p.get('source_po_no') or 'none'}")
    print(f"  supplier {names.get('supplier') or '?'} ({p.get('supplier_id')}) / "
          f"party {names.get('party') or '?'} ({p.get('party_id')}) | {p['uom']} "
          f"| credit {p.get('credit_term')} "
          f"| weight {p['weight']:,.0f} kg | value {p['value']:,.2f}")
    print(f"  remark: {p['close_remark']}")
    print("    item      MR kg   PO units    PO kg    MR rate    PO rate        PO value")
    for l in p["lines"]:
        print(f"    {str(l['item_id']):>6} {l['accepted_weight']:>9,.0f} {l['quantity']:>9,.0f} "
              f"{l['line_kg']:>9,.0f} {l['mr_rate']:>10,.0f} {l['rate']:>10,.0f} {l['value']:>15,.2f}")
    print(f"  MR: {_mr_kg(e['mr_lines']):,.0f} kg, amount {_mr_amount(e['mr_lines']):,.2f} "
          f"-> PO differs by {p['value'] - _mr_amount(e['mr_lines']):+,.2f}")


def report(entries, forward_total, final_total, chains_total, scope="--all", warnings=()):
    planned = [e for e in entries if not e["plan"].get("skipped")]
    skipped = [e for e in entries if e["plan"].get("skipped")]
    print("=" * 78)
    print("TRANSFER PO BACKFILL — DRY RUN (nothing is written)")
    print("=" * 78)
    print(f"Chain hops found: {forward_total} | finalized chains: {final_total} "
          f"| chains awaiting return: {chains_total - final_total}")
    print(f"POs to create: {len(planned)}  (already present / nothing to do: {len(skipped)})")
    for w in warnings:
        print(w)

    by = defaultdict(list)
    for e in planned:
        p = e["plan"]
        by[(p["role"], p["branch_id"], p.get("co_name"), fy_label(p["po_date"]))].append(e)
    print("\nPer company")
    for (role, _b, co, fy), es in sorted(by.items(), key=lambda kv: (kv[0][0] != ROLE_FORWARD, str(kv[0][2]))):
        nos = [e["plan"]["sim_po_no"] for e in es]
        first = min(es, key=lambda e: e["plan"]["sim_po_no"])["plan"]["sim_po_no_formatted"]
        last = max(es, key=lambda e: e["plan"]["sim_po_no"])["plan"]["sim_po_no_formatted"]
        kg = sum(e["plan"]["weight"] for e in es)
        val = sum(e["plan"]["value"] for e in es)
        dates = sorted(e["plan"]["po_date"] for e in es)
        label = "Forwarding" if role == ROLE_FORWARD else "Final"
        print(f"  {co}: {len(es)} {label} PO(s), FY {fy}, numbers {first} .. {last} "
              f"({min(nos)}-{max(nos)}), dated {dates[0]} .. {dates[-1]}, "
              f"{kg:,.0f} kg, value {val:,.2f}")
    print("  (numbers are the next free ones right now; a PO raised in the ERP "
          "before the run moves them)")

    # Exceptions
    zero_lines = sum(len([l for l in e["mr_lines"] if float(l["accepted_weight"] or 0) <= 0])
                     for e in planned)
    off = []
    rounded = []
    for e in planned:
        p = e["plan"]
        amt = _mr_amount(e["mr_lines"])
        if amt and abs(p["value"] - amt) / amt > 0.01:
            off.append((abs(p["value"] - amt) / amt, e))
        if any(l["rate"] != l["mr_rate"] for l in p["lines"]):
            rounded.append(e)
    map_rows = defaultdict(int)
    for e in planned:
        row = e["plan"].get("map_row")
        if row:
            names = e.get("names") or {}
            map_rows[(e["plan"].get("co_name"), names.get("supplier"), row["supplier_id"],
                      names.get("party"), row["party_id"])] += 1
    no_src = [e for e in planned if not e["plan"].get("source_po_id")]
    print("\nExceptions")
    print(f"  zero-weight MR lines left out of the POs: {zero_lines}")
    print(f"  POs more than 1% off their MR amount (part-lorries): {len(off)}"
          + (" -> MR " + ", ".join(str(e['plan']['mr_id']) for _, e in sorted(off, key=lambda t: -t[0])[:8]) if off else ""))
    fwd_rounded = [e for e in rounded if e["plan"]["role"] == ROLE_FORWARD]
    print(f"  Forwarding POs whose rate moves when rounded to 50: {len(fwd_rounded)}"
          + (" -> MR " + ", ".join(str(e['plan']['mr_id']) for e in fwd_rounded[:10]) if fwd_rounded else ""))
    print(f"  Final POs whose rate moves when rounded to 50: "
          f"{len([e for e in rounded if e['plan']['role'] == ROLE_FINAL])}")
    print(f"  supplier-party map rows to insert at forwarding companies: {len(map_rows)}")
    for (co, supplier, supplier_id, party, party_id), n in sorted(map_rows.items(), key=str):
        print(f"    {co}: {supplier or '?'} ({supplier_id}) -> {party or '?'} ({party_id}), {n} lorry/lorries")
    print(f"  lorries with no original ERP PO: {len(no_src)}")
    reasons = defaultdict(int)
    for e in skipped:
        r = e["plan"]["skipped"]
        reasons["already has a PO" if "already has" in r else r] += 1
    for r, n in sorted(reasons.items()):
        print(f"  skipped — {r}: {n}")

    tot_val = sum(e["plan"]["value"] for e in planned)
    tot_amt = sum(_mr_amount(e["mr_lines"]) for e in planned)
    print(f"\nTotals: PO value {tot_val:,.2f} vs MR amount {tot_amt:,.2f} "
          f"({tot_val - tot_amt:+,.2f})")

    # Samples
    print("\nSamples (in full)")
    fwd = [e for e in planned if e["plan"]["role"] == ROLE_FORWARD]
    fin = [e for e in planned if e["plan"]["role"] == ROLE_FINAL]
    shown = set()

    def show(e, title):
        if e and id(e) not in shown:
            shown.add(id(e))
            _print_po(e, title)

    show(next((e for e in fwd if e["plan"]["uom"] == "LOOSE" and len(e["plan"]["lines"]) == 1), None),
         "single-line loose lorry")
    show(next((e for e in fwd if e["plan"]["uom"] == "BALE" and len(e["plan"]["lines"]) > 1), None),
         "multi-line bale lorry")
    if fin:
        pair_root = fin[0]["plan"]["root_mr_id"]
        show(next((e for e in fwd if e["plan"]["root_mr_id"] == pair_root), None),
             "finalized lorry — Forwarding PO")
        show(fin[0], "finalized lorry — Final PO")
    if off:
        show(sorted(off, key=lambda t: -t[0])[0][1], "largest difference from its MR")
    show(next(iter(fwd_rounded), None), "Forwarding PO with a rounded rate")
    print("\nNothing was written. To create exactly these, after the owner's OK:")
    print(f"    --apply {scope} --expect {len(planned)}")
    print("Undo: every --apply run logs the POs it created in .logs/jt_backfill_<time>.json;")
    print("    --undo <that log>               shows what would be deleted")
    print("    --undo <that log> --apply --expect <n>  deletes exactly those POs")
    print("  (the supplier-party map rows a run adds stay: they are valid ERP masters)")


def same_number_pos(conn, po_id: int) -> list:
    """Ids of OTHER POs carrying this PO's number at its branch in its
    financial year. The ERP and this app both take MAX(po_no)+1 without a
    lock and there is no unique key, so two saves in the same seconds can
    end up with one number."""
    row = conn.execute(text(
        "SELECT branch_id, po_no, po_date FROM jute_po WHERE jute_po_id = :id"
    ), {"id": int(po_id)}).fetchone()
    if not row or row[1] is None or not _d(row[2]):
        return []
    fy_start, fy_end = _get_financial_year_bounds(_d(row[2]))
    rows = conn.execute(text("""
        SELECT jute_po_id FROM jute_po
        WHERE branch_id = :b AND po_no = :n AND po_date BETWEEN :s AND :e AND jute_po_id <> :id
        ORDER BY jute_po_id
    """), {"b": row[0], "n": row[1], "s": fy_start.strftime("%Y-%m-%d"),
           "e": fy_end.strftime("%Y-%m-%d"), "id": int(po_id)}).fetchall()
    return [int(r[0]) for r in rows]


def apply(planned, log_path: Path, scope: str = ""):
    """Create the planned POs: one transaction per PO, the branch's PO
    numbering lock (po_ops.po_no_lock, the ERP's protocol) held from before
    the transaction opens until after it ends and the root row locked first
    inside it (as the page does), each PO planned again under those locks.

    Stops at the first FAILED PO and at the first duplicate number, so that
    no later lorry takes a number before an earlier one. A PO the fresh plan
    no longer wants (the chain changed since the dry run) is reported with
    its reason and the run goes on. Returns the log dict."""
    os.environ["JT_TRANSFER_PO"] = "1"  # an explicit backfill run overrides the switch
    log = {
        "started": datetime.now().isoformat(timespec="seconds"), "scope": scope,
        "planned": [{"role": e["plan"]["role"], "mr_id": e["plan"]["mr_id"],
                     "root_mr_id": e["plan"]["root_mr_id"],
                     "po_no": e["plan"].get("sim_po_no_formatted"),
                     "weight": e["plan"]["weight"], "value": e["plan"]["value"],
                     "map_row": e["plan"].get("map_row")} for e in planned],
        "created": [], "skipped": [], "failed": [], "duplicates": [], "stopped": None,
    }
    _write_json(log_path, log)          # before the first write: no log, no run

    for e in planned:
        p = e["plan"]
        role, mr_id, root_id = p["role"], int(p["mr_id"]), int(p["root_mr_id"])
        try:
            lock = {po_ops.po_no_lock(p["branch_id"]): po_ops.PO_NO_LOCK_TIMEOUT}
            with named_locks(lock), DatabaseConnection.get_transaction() as conn:
                conn.execute(text(
                    "SELECT jute_mr_id FROM jute_mr WHERE jute_mr_id = :id FOR UPDATE"
                ), {"id": root_id})
                if role == ROLE_FORWARD:
                    res = po_ops.create_forward_po(conn, mr_id, UPDATED_BY)
                else:
                    res = po_ops.create_final_po(conn, mr_id, UPDATED_BY)
        except Exception as exc:
            log["failed"].append({"role": role, "mr_id": mr_id, "error": str(exc)})
            log["stopped"] = f"failed at {role} MR {mr_id}"
            _write_json(log_path, log)
            print(f"  FAILED  {role:8} MR {mr_id}: {exc}")
            print("  STOPPED: no later lorry was attempted, so the numbers stay in date order."
                  " Fix the cause, take a new dry run and run again.")
            break
        if not res.get("po_id"):
            log["skipped"].append({"role": role, "mr_id": mr_id, "reason": res.get("skipped")})
            _write_json(log_path, log)
            print(f"  SKIPPED {role:8} MR {mr_id}: {res.get('skipped')} (changed since the plan)")
            continue
        log["created"].append({"role": role, "mr_id": mr_id, "root_mr_id": root_id,
                               "po_id": res["po_id"], "po_no": res["po_no_formatted"]})
        _write_json(log_path, log)
        print(f"  created {role:8} {res['po_no_formatted']} (po {res['po_id']}) for MR {mr_id}")
        with DatabaseConnection.get_connection() as conn:
            twins = same_number_pos(conn, res["po_id"])
            value = conn.execute(text(
                "SELECT jute_po_value FROM jute_po WHERE jute_po_id = :id"
            ), {"id": res["po_id"]}).scalar()
            conn.rollback()
        if abs(float(value or 0) - float(p["value"])) > 0.005:
            print(f"    NOTE: its value is {float(value or 0):,.2f}, the plan showed {p['value']:,.2f} "
                  "(the MR's lines changed since the plan)")
        if twins:
            log["duplicates"].append({"po_id": res["po_id"], "po_no": res["po_no_formatted"],
                                      "same_number_as": twins})
            log["stopped"] = f"duplicate number {res['po_no_formatted']}"
            _write_json(log_path, log)
            print(f"  DUPLICATE NUMBER: {res['po_no_formatted']} is also PO id {twins} "
                  "(a PO was raised at this branch in the same seconds).")
            print(f"  STOPPED. Undo this run (--undo {log_path}) or renumber one of the two, "
                  "then run again.")
            break

    print(f"\nCreated {len(log['created'])} of {len(planned)} planned; "
          f"skipped {len(log['skipped'])}, failed {len(log['failed'])}. Log: {log_path}")
    return log


def verify(conn):
    """After-run check (read-only): what is still missing, numbers used
    twice, and transfer POs whose MR is gone or no longer theirs."""
    hops = conn.execute(text("""
        SELECT COUNT(*) FROM jute_mr h
        WHERE h.transfer_mode = 0 AND h.src_jute_mr_id IS NOT NULL AND h.po_id IS NULL
          AND EXISTS (SELECT 1 FROM jute_mr_li li WHERE li.jute_mr_id = h.jute_mr_id
                      AND (li.active = 1 OR li.active IS NULL) AND li.accepted_weight > 0)
    """)).scalar()
    _forward, final = load_work(conn)
    missing_final = [int(j["root_id"]) for j in final
                     if not po_ops.find_final_po_ids(conn, int(j["root_id"]))]
    print(f"After-run check: hops with weight and no PO: {hops}; "
          f"finalized roots without a Final PO: {len(missing_final)}"
          + (f" -> {missing_final[:10]}" if missing_final else ""))

    ours = [dict(r._mapping) for r in conn.execute(text("""
        SELECT jute_po_id, branch_id, po_no, po_date, internal_note FROM jute_po
        WHERE internal_note LIKE 'JT|%'
    """)).fetchall()]
    duplicates, orphans = [], []
    branches = sorted({int(p["branch_id"]) for p in ours})
    if branches:
        groups = defaultdict(list)
        for r in conn.execute(text(
                "SELECT jute_po_id, branch_id, po_no, po_date, internal_note FROM jute_po "
                f"WHERE po_no IS NOT NULL AND branch_id IN ({','.join(str(b) for b in branches)})"
        )).fetchall():
            if _d(r[3]):
                groups[(int(r[1]), fy_label(_d(r[3])), int(r[2]))].append((int(r[0]), r[4]))
        for (branch, fy, number), members in sorted(groups.items()):
            if len(members) > 1 and any(parse_marker(note) for _id, note in members):
                duplicates.append({"branch_id": branch, "fy": fy, "po_no": number,
                                   "po_ids": [i for i, _n in members]})
    markers = {int(p["jute_po_id"]): parse_marker(p["internal_note"]) for p in ours}
    mr_ids = sorted({m["mr_id"] for m in markers.values() if m})
    mrs = {}
    for start in range(0, len(mr_ids), 500):
        chunk = mr_ids[start:start + 500]
        for r in conn.execute(text(
                "SELECT jute_mr_id, po_id, status_id, branch_mr_no FROM jute_mr "
                f"WHERE jute_mr_id IN ({','.join(str(i) for i in chunk)})")).fetchall():
            mrs[int(r[0])] = r
    for po_id, m in sorted(markers.items()):
        if not m:
            continue
        mr = mrs.get(m["mr_id"])
        if mr is None:
            orphans.append({"po_id": po_id, "why": f"{m['role']} PO of MR {m['mr_id']}, which no longer exists"})
        elif m["role"] == ROLE_FORWARD and (mr[1] is None or int(mr[1]) != po_id):
            orphans.append({"po_id": po_id, "why": f"hop MR {m['mr_id']} is not linked to it"})
        elif m["role"] == ROLE_FINAL and (int(mr[2] or 0) != 3 or mr[3] is None):
            orphans.append({"po_id": po_id, "why": f"mill MR {m['mr_id']} is no longer finalized"})
    print(f"                 PO numbers used twice: {len(duplicates)}"
          + (f" -> {duplicates[:5]}" if duplicates else "")
          + f"; transfer POs without their MR: {len(orphans)}"
          + (f" -> {orphans[:5]}" if orphans else ""))
    return {"hops_missing": int(hops or 0), "finals_missing": missing_final,
            "duplicates": duplicates, "orphans": orphans}


def _logged_po_still_ours(conn, it) -> bool:
    """The logged PO still exists and still carries the marker of the MR it
    was created for (read-only)."""
    row = conn.execute(text(
        "SELECT internal_note FROM jute_po WHERE jute_po_id = :id"
    ), {"id": int(it["po_id"])}).fetchone()
    m = parse_marker(row[0]) if row else None
    return bool(m and m["role"] == it["role"] and m["mr_id"] == int(it["mr_id"]))


def undo(log_file, do_apply, expect):
    """Delete exactly the PO ids a previous --apply run logged -- never
    "whatever transfer PO the MR has now" (a later save may have made a new
    one)."""
    log_file = Path(log_file)
    data = json.loads(log_file.read_text())
    items = list(reversed(data.get("created", [])))  # newest first frees the numbers
    with DatabaseConnection.get_connection() as conn:
        present = [it for it in items if _logged_po_still_ours(conn, it)]
        conn.rollback()
    print(f"{'UNDO' if do_apply else 'UNDO DRY RUN'}: {len(items)} PO(s) in {log_file}, "
          f"{len(present)} still there and still the run's own")
    for it in items:
        mark = "to delete" if it in present else "leave (gone, or no longer that MR's PO)"
        print(f"  {mark:40} {it['role']:8} {it.get('po_no')} (po {it['po_id']}, MR {it['mr_id']})")
    if not do_apply:
        print(f"Nothing was deleted. To delete these: --undo {log_file} --apply --expect {len(present)}")
        return 0
    if expect != len(present):
        print(f"REFUSED: --expect {expect} but {len(present)} PO(s) would be deleted; "
              "re-run the undo dry-run and check")
        return 2
    record_path = log_file.with_name(f"{log_file.stem}.undo_{datetime.now():%Y%m%d_%H%M%S}.json")
    record = {"undo_of": str(log_file), "deleted": [], "left": [], "failed": []}
    _write_json(record_path, record)
    for it in present:
        try:
            with DatabaseConnection.get_transaction() as conn:
                conn.execute(text(
                    "SELECT jute_mr_id FROM jute_mr WHERE jute_mr_id = :id FOR UPDATE"
                ), {"id": int(it["root_mr_id"])})
                gone = po_ops.delete_transfer_po(conn, int(it["po_id"]), it["role"],
                                                 int(it["mr_id"]))
            if gone:
                record["deleted"].append(it)
                print(f"  deleted {it['role']:8} {it.get('po_no')} (po {it['po_id']})")
            else:
                record["left"].append(it)
                print(f"  LEFT    {it['role']:8} po {it['po_id']}: changed since the dry run")
        except Exception as exc:
            record["failed"].append({**it, "error": str(exc)})
            print(f"  FAILED  {it['role']:8} po {it['po_id']}: {exc}")
        finally:
            _write_json(record_path, record)
    print(f"Removed {len(record['deleted'])} of {len(present)}. Record: {record_path}")
    return 1 if record["failed"] or record["left"] else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--root", type=int, action="append", help="only this chain root MR id (repeatable)")
    ap.add_argument("--limit", type=int, default=0,
                    help="the next N lorries per branch that still need a PO")
    ap.add_argument("--all", action="store_true", help="every chain")
    ap.add_argument("--expect", type=int, help="with --apply: the PO count the dry-run showed")
    ap.add_argument("--undo", metavar="LOG_JSON", help="delete the POs a previous --apply run created")
    ap.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    args = ap.parse_args(argv)

    scopes = [name for name, given in (("--root", args.root), ("--limit", args.limit),
                                       ("--all", args.all)) if given]
    if args.apply and args.expect is None:
        ap.error("--apply needs --expect N: the count the dry-run showed for the same scope")
    if args.undo:
        if scopes:
            ap.error("--undo takes its scope from the log: no --root / --limit / --all")
        return undo(args.undo, args.apply, args.expect)
    if len(scopes) > 1:
        ap.error(f"{' and '.join(scopes)} are different scopes: give one")
    if args.apply and not scopes:
        ap.error("--apply needs a scope: --root N, --limit N or --all")
    scope = (" ".join(f"--root {r}" for r in args.root) if args.root
             else f"--limit {args.limit}" if args.limit else "--all")

    root_filter = set(args.root) if args.root else None
    print("Planning (read-only) ... every chain takes about four minutes.", flush=True)
    with DatabaseConnection.get_engine().connect() as conn:
        # The planning pass can never write: it runs in a READ ONLY
        # transaction. (Not SET SESSION ...: that would stay on the pooled
        # connection and make the --apply pass fail every write.)
        conn.execute(text("START TRANSACTION READ ONLY"))
        try:
            forward_all, final_all = load_work(conn)
            fwd_jobs = [j for j in forward_all if not root_filter or int(j["root_id"]) in root_filter]
            fin_jobs = [j for j in final_all if not root_filter or int(j["root_id"]) in root_filter]
            chains_total = len({int(j["root_id"]) for j in fwd_jobs})
            entries = _limit_planned(build_plans(conn, fwd_jobs, fin_jobs), args.limit)
            simulate_numbers(conn, entries)
            warnings = (older_unserved(conn, entries, forward_all, final_all)
                        if root_filter or args.limit else [])
            if not args.apply:
                report(entries, len(fwd_jobs), len(fin_jobs), chains_total, scope, warnings)
                return 0
        finally:
            conn.rollback()

    planned = [e for e in entries if not e["plan"].get("skipped")]
    if args.expect != len(planned):
        print(f"REFUSED: --expect {args.expect} but {len(planned)} PO(s) would be created now "
              "(chains changed since the dry run?); re-run the dry run and check")
        return 2
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"jt_backfill_{datetime.now():%Y%m%d_%H%M%S}.json"
    print(f"APPLY {scope}: {len(planned)} PO(s) to create "
          f"({len(entries) - len(planned)} lorry/lorries in scope need nothing); log: {log_path}")
    for w in warnings:
        print(w)
    for e in planned:
        p = e["plan"]
        print(f"  {p['role']:8} {p.get('sim_po_no_formatted', '?')} for MR {p['mr_id']} "
              f"({p['weight']:,.0f} kg, {p['value']:,.2f})")
    log = apply(planned, log_path, scope)
    with DatabaseConnection.get_connection() as conn:
        check = verify(conn)
        conn.rollback()
    incomplete = (len(log["created"]) != len(planned) or log["failed"] or log["duplicates"])
    unclean = check["duplicates"] or check["orphans"] or (
        args.all and (check["hops_missing"] or check["finals_missing"]))
    if incomplete or unclean:
        print("NOT CLEAN: see the lines above"
              + ("" if args.all else " (a --root / --limit run leaves other lorries without POs by design)"))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
