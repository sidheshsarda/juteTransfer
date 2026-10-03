"""The two production scripts through main(), as the operator runs them, on
the in-memory stand-in: scripts/backfill_transfer_pos.py (POs for chains
saved before transfer POs existed) and scripts/repair_transfer_data.py --
dry run, apply, the count guard, the log and the undo.

The planning pass of both runs in START TRANSACTION READ ONLY; the stand-in
refuses a write there as MySQL does, and a write after it only works when
that transaction was really ended."""
import json
from datetime import date

import pytest

from scripts import backfill_transfer_pos as backfill
from scripts import repair_transfer_data as repair
from src.jutetransfer import po_ops
from src.jutetransfer.database import DatabaseConnection
from src.jutetransfer.po_helpers import ROLE_FINAL, ROLE_FORWARD, parse_marker

from .fake_mysql import fake_db, scenario  # noqa: F401  (fixtures)
from .test_transfer_po_flow import Chain


@pytest.fixture(autouse=True)
def po_switched_off(monkeypatch):
    """As on production before the roll-out. (Also puts JT_TRANSFER_PO back
    after backfill.apply() has set it for its own run.)"""
    monkeypatch.setenv("JT_TRANSFER_PO", "0")


def legacy_chains(sc) -> dict:
    """Three lorries transferred before transfer POs existed:
      GE 15 -> B on 28 Aug, still out;   GE 16 -> C on 30 Aug, still out;
      GE 21 (the scenario's root) -> B on 1 Sep, back at the mill on 3 Sep."""
    root2 = sc.add_root(ge_no=15, ge_date=date(2026, 8, 27))
    root3 = sc.add_root(ge_no=16, ge_date=date(2026, 8, 28))
    hop2 = sc.add_hop("B", mr_date=date(2026, 8, 28), root=root2)
    hop3 = sc.add_hop("C", mr_date=date(2026, 8, 30), root=root3, mapped=False)
    hop1 = sc.add_hop("B", mr_date=date(2026, 9, 1))
    sc.finalize_root("B", mr_date=date(2026, 9, 3))
    return {"hop1": hop1, "hop2": hop2, "hop3": hop3, "root2": root2, "root3": root3}


def transfer_pos(db) -> list:
    """(role, MR, branch, PO number, PO date) of every transfer PO, in the
    order they were created."""
    out = []
    for po in sorted(db.rows("jute_po"), key=lambda p: p["jute_po_id"]):
        marker = parse_marker(po["internal_note"])
        if marker:
            out.append((marker["role"], marker["mr_id"], po["branch_id"], po["po_no"],
                        po["po_date"]))
    return out


# --- backfill --------------------------------------------------------------------------------

def test_backfill_dry_run_reports_and_writes_nothing(scenario, capsys):
    sc, db = scenario, scenario.db
    legacy_chains(sc)
    since = len(db.statements)
    assert backfill.main([]) == 0
    assert db.writes(since) == []
    out = capsys.readouterr().out
    assert "POs to create: 4" in out and "--expect 4" in out


def test_backfill_apply_creates_what_the_dry_run_showed(scenario, tmp_path, capsys):
    sc, db = scenario, scenario.db
    ids = legacy_chains(sc)
    maps = db.count("jute_supp_party_map")
    args = ["--apply", "--all", "--expect", "4", "--log-dir", str(tmp_path)]

    assert backfill.main(args) == 0

    # numbers in date order per branch; Forwarding POs first, then the Final PO
    assert transfer_pos(db) == [
        (ROLE_FORWARD, ids["hop2"], sc.b_branch, 1, date(2026, 8, 28)),
        (ROLE_FORWARD, ids["hop3"], sc.c_branch, 1, date(2026, 8, 30)),
        (ROLE_FORWARD, ids["hop1"], sc.b_branch, 2, date(2026, 9, 1)),
        (ROLE_FINAL, sc.root, sc.a_branch, sc.po_no + 1, date(2026, 9, 3)),
    ]
    assert all(db.row("jute_mr", jute_mr_id=ids[h])["po_id"] for h in ("hop1", "hop2", "hop3"))
    assert db.row("jute_mr", jute_mr_id=sc.root)["po_id"] == sc.po      # the mill MR keeps its own PO
    assert db.count("jute_supp_party_map") == maps + 1                   # C's missing supplier-party row
    assert ("hops with weight and no PO: 0; finalized roots without a Final PO: 0"
            in capsys.readouterr().out)
    (log,) = tmp_path.glob("jt_backfill_*.json")
    logged = json.loads(log.read_text())
    assert logged["failed"] == []
    assert [(c["role"], c["mr_id"], c["root_mr_id"]) for c in logged["created"]] == [
        (ROLE_FORWARD, ids["hop2"], ids["root2"]), (ROLE_FORWARD, ids["hop3"], ids["root3"]),
        (ROLE_FORWARD, ids["hop1"], sc.root), (ROLE_FINAL, sc.root, sc.root)]

    # run again: nothing is left to create, and it says so before writing
    since = len(db.statements)
    assert backfill.main(args) == 2
    assert db.writes(since) == []


def test_backfill_refuses_a_count_that_is_not_what_it_would_create(scenario, tmp_path):
    sc, db = scenario, scenario.db
    legacy_chains(sc)
    since = len(db.statements)
    assert backfill.main(["--apply", "--all", "--expect", "3", "--log-dir", str(tmp_path)]) == 2
    assert db.writes(since) == [] and list(tmp_path.iterdir()) == []


def test_backfill_one_lorry_first_then_the_rest(scenario, tmp_path):
    sc, db = scenario, scenario.db
    ids = legacy_chains(sc)
    assert backfill.main(["--apply", "--root", str(sc.root), "--expect", "2",
                          "--log-dir", str(tmp_path / "pilot")]) == 0
    assert transfer_pos(db) == [
        (ROLE_FORWARD, ids["hop1"], sc.b_branch, 1, date(2026, 9, 1)),
        (ROLE_FINAL, sc.root, sc.a_branch, sc.po_no + 1, date(2026, 9, 3)),
    ]
    assert backfill.main(["--apply", "--all", "--expect", "2",
                          "--log-dir", str(tmp_path / "rest")]) == 0
    assert transfer_pos(db)[2:] == [
        (ROLE_FORWARD, ids["hop2"], sc.b_branch, 2, date(2026, 8, 28)),
        (ROLE_FORWARD, ids["hop3"], sc.c_branch, 1, date(2026, 8, 30)),
    ]


def test_backfill_undo_deletes_exactly_what_the_run_logged(scenario, tmp_path, capsys, monkeypatch):
    sc, db = scenario, scenario.db
    ids = legacy_chains(sc)
    assert backfill.main(["--apply", "--all", "--expect", "4", "--log-dir", str(tmp_path)]) == 0
    (log,) = tmp_path.glob("jt_backfill_*.json")
    maps = db.count("jute_supp_party_map")
    # hop 1's PO is replaced afterwards (the step deleted and saved again from the page)
    monkeypatch.setenv("JT_TRANSFER_PO", "1")
    with DatabaseConnection.get_transaction() as conn:
        po_ops.delete_forward_po(conn, ids["hop1"])
        newer = po_ops.create_forward_po(conn, ids["hop1"], 1)["po_id"]
    capsys.readouterr()

    since = len(db.statements)
    assert backfill.main(["--undo", str(log)]) == 0                              # dry run
    assert "--apply --expect 3" in capsys.readouterr().out
    assert backfill.main(["--undo", str(log), "--apply", "--expect", "4"]) == 2   # 3 are left, not 4
    assert db.writes(since) == []

    assert backfill.main(["--undo", str(log), "--apply", "--expect", "3"]) == 0

    (record,) = tmp_path.glob("*.undo_*.json")
    assert len(json.loads(record.read_text())["deleted"]) == 3
    assert transfer_pos(db) == [(ROLE_FORWARD, ids["hop1"], sc.b_branch, 2, date(2026, 9, 1))]
    assert db.row("jute_mr", jute_mr_id=ids["hop1"])["po_id"] == newer            # not the run's: kept
    for hop in ("hop2", "hop3"):
        assert db.row("jute_mr", jute_mr_id=ids[hop])["po_id"] is None
        assert all(l["jute_po_li_id"] is None for l in db.rows("jute_mr_li", jute_mr_id=ids[hop]))
    assert db.count("jute_po", jute_po_id=sc.po) == 1                             # the ERP's own PO
    assert db.count("jute_supp_party_map") == maps                                # masters stay


def test_backfill_stops_at_the_first_failure_so_numbers_stay_in_date_order(
        scenario, tmp_path, monkeypatch, capsys):
    sc, db = scenario, scenario.db
    ids = legacy_chains(sc)
    real = po_ops.create_forward_po

    def failing(conn, hop_mr_id, updated_by):
        if hop_mr_id == ids["hop3"]:
            raise RuntimeError("Lock wait timeout exceeded")
        return real(conn, hop_mr_id, updated_by)

    monkeypatch.setattr(po_ops, "create_forward_po", failing)
    assert backfill.main(["--apply", "--all", "--expect", "4", "--log-dir", str(tmp_path / "1")]) == 1
    # 28 Aug created, 30 Aug failed; 1 Sep and the Final PO were not attempted
    assert transfer_pos(db) == [(ROLE_FORWARD, ids["hop2"], sc.b_branch, 1, date(2026, 8, 28))]
    out = capsys.readouterr().out
    assert "STOPPED" in out and "Created 1 of 4 planned" in out

    monkeypatch.setattr(po_ops, "create_forward_po", real)
    assert backfill.main(["--apply", "--all", "--expect", "3", "--log-dir", str(tmp_path / "2")]) == 0
    assert transfer_pos(db)[1:] == [
        (ROLE_FORWARD, ids["hop3"], sc.c_branch, 1, date(2026, 8, 30)),
        (ROLE_FORWARD, ids["hop1"], sc.b_branch, 2, date(2026, 9, 1)),
        (ROLE_FINAL, sc.root, sc.a_branch, sc.po_no + 1, date(2026, 9, 3)),
    ]


def test_backfill_reports_a_po_the_fresh_plan_no_longer_wants(scenario, tmp_path, monkeypatch, capsys):
    """A chain changed between the dry run and the write (here: un-finalized)."""
    sc = scenario
    legacy_chains(sc)
    monkeypatch.setattr(po_ops, "create_final_po", lambda conn, root, by: {
        "role": ROLE_FINAL, "po_id": None, "po_no": None, "po_no_formatted": "",
        "skipped": f"MR {root} is not finalized"})
    assert backfill.main(["--apply", "--all", "--expect", "4", "--log-dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert f"MR {sc.root} is not finalized" in out and "Created 3 of 4 planned" in out
    (log,) = tmp_path.glob("jt_backfill_*.json")
    assert json.loads(log.read_text())["skipped"] == [
        {"role": ROLE_FINAL, "mr_id": sc.root, "reason": f"MR {sc.root} is not finalized"}]


def test_backfill_stops_when_a_number_is_used_twice(scenario, tmp_path, monkeypatch, capsys):
    """The ERP numbers MAX+1 without a lock, as this app does: a PO raised at
    the mill in the same seconds ends up with the same number."""
    sc = scenario
    legacy_chains(sc)
    real = po_ops._next_po_no
    monkeypatch.setattr(po_ops, "_next_po_no", lambda conn, branch, po_date: (
        sc.po_no if branch == sc.a_branch else real(conn, branch, po_date)))
    assert backfill.main(["--apply", "--all", "--expect", "4", "--log-dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "DUPLICATE NUMBER" in out and "PO numbers used twice: 1" in out
    (log,) = tmp_path.glob("jt_backfill_*.json")
    (twice,) = json.loads(log.read_text())["duplicates"]
    assert twice["same_number_as"] == [sc.po]


def test_backfill_limit_takes_the_next_lorries_that_need_a_po(scenario, tmp_path):
    sc, db = scenario, scenario.db
    ids = legacy_chains(sc)
    assert backfill.main(["--apply", "--limit", "1", "--expect", "3",
                          "--log-dir", str(tmp_path / "1")]) == 0
    assert [po[:2] for po in transfer_pos(db)] == [
        (ROLE_FORWARD, ids["hop2"]), (ROLE_FORWARD, ids["hop3"]), (ROLE_FINAL, sc.root)]
    assert backfill.main(["--apply", "--limit", "1", "--expect", "1",
                          "--log-dir", str(tmp_path / "2")]) == 0
    assert transfer_pos(db)[-1] == (ROLE_FORWARD, ids["hop1"], sc.b_branch, 2, date(2026, 9, 1))


def test_backfill_warns_when_a_narrow_run_jumps_older_lorries(scenario, capsys):
    sc = scenario
    ids = legacy_chains(sc)
    assert backfill.main(["--root", str(sc.root)]) == 0
    assert (f"WARNING: branch {sc.b_branch}: 1 older lorry/lorries still need a Forwarding PO "
            f"(MR {ids['hop2']})") in capsys.readouterr().out
    assert backfill.main(["--root", str(ids["root2"])]) == 0     # the oldest lorry at B
    assert "WARNING" not in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["--apply"], ["--apply", "--all"], ["--apply", "--root", "5"], ["--apply", "--expect", "2"],
    ["--undo", "log.json", "--apply"],
    ["--all", "--root", "5"], ["--all", "--limit", "2"], ["--root", "5", "--limit", "1"],
    ["--undo", "log.json", "--all"],
])
def test_backfill_will_not_run_without_one_scope_and_the_expected_count(argv, fake_db):
    since = len(fake_db.statements)
    with pytest.raises(SystemExit):
        backfill.main(argv)
    assert fake_db.statements[since:] == []


# --- repairs ---------------------------------------------------------------------------------

def finalized_the_old_way(sc) -> None:
    """A chain finalized by the old code: claim / TDS / roundoff / net blank."""
    chain = Chain(sc)
    chain.save("B", mr_date=date(2026, 9, 1))
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 3))
    sc.db.update("jute_mr", {"jute_mr_id": sc.root}, claim_amount=None, tds_amount=None,
                 roundoff=None, net_total=None)


def test_repair_dry_run_reports_and_writes_nothing(scenario, capsys):
    sc, db = scenario, scenario.db
    finalized_the_old_way(sc)
    since = len(db.statements)
    assert repair.main(["--only", "finalized-net"]) == 0
    assert db.writes(since) == []
    assert "1 finalized root MR(s)" in capsys.readouterr().out


def test_repair_finalized_net_through_main(scenario, tmp_path):
    sc, db = scenario, scenario.db
    finalized_the_old_way(sc)
    args = ["--apply", "--only", "finalized-net", "--log-dir", str(tmp_path)]
    since = len(db.statements)
    assert repair.main(args + ["--expect", "2"]) == 2                  # not what the dry run shows
    assert db.writes(since) == [] and list(tmp_path.iterdir()) == []

    assert repair.main(args + ["--expect", "1"]) == 0

    root = db.row("jute_mr", jute_mr_id=sc.root)
    assert (root["total_amount"], root["claim_amount"], root["tds_amount"], root["roundoff"],
            root["net_total"]) == (1385746.01, 13757.3, 0.0, 0.29, 1371989.0)
    (log,) = tmp_path.glob("jt_repair_*.json")
    logged = json.loads(log.read_text())
    assert [r["jute_mr_id"] for r in logged["repaired"]] == [sc.root] and logged["failed"] == []
    assert logged["repaired"][0]["old"]["net_total"] is None           # enough to put it back by hand
    assert repair.main(args + ["--expect", "1"]) == 2                  # nothing left to repair


def test_repair_unfinalized_party_through_main(scenario, tmp_path):
    """The old un-finalize left a Pending root on the forwarding company as
    party, with the finalize's MR date."""
    sc, db = scenario, scenario.db
    Chain(sc).save("B", mr_date=date(2026, 9, 1))
    b_in_a, b_in_a_branch = sc.party_in(sc.a_co, sc.b_name.upper())
    db.update("jute_mr", {"jute_mr_id": sc.root}, party_id=str(b_in_a),
              party_branch_id=b_in_a_branch, jute_mr_date=date(2026, 9, 3))

    assert repair.main(["--apply", "--only", "unfinalized-party", "--expect", "1",
                        "--log-dir", str(tmp_path)]) == 0

    root = db.row("jute_mr", jute_mr_id=sc.root)
    assert (root["party_id"], root["party_branch_id"], root["jute_mr_date"], root["status_id"]) == (
        str(sc.supplier_party), sc.supplier_party_branch, None, 13)
    (log,) = tmp_path.glob("jt_repair_*.json")
    assert json.loads(log.read_text())["repaired"][0]["old"]["party_id"] == str(b_in_a)


@pytest.mark.parametrize("argv", [
    ["--apply"], ["--apply", "--only", "godowns"], ["--apply", "--expect", "3"],
])
def test_repair_will_not_write_without_one_repair_and_the_expected_count(argv, fake_db):
    since = len(fake_db.statements)
    with pytest.raises(SystemExit):
        repair.main(argv)
    assert fake_db.statements[since:] == []


def test_repair_shows_every_row_and_saves_the_plan_before_it_writes(
        scenario, tmp_path, capsys, monkeypatch):
    sc, db = scenario, scenario.db
    finalized_the_old_way(sc)
    finalized_at = db.row("jute_mr", jute_mr_id=sc.root)["updated_date_time"]
    seen = {}
    real = repair.REPAIR_FNS["finalized-net"]

    def spy(conn, it):
        (log,) = tmp_path.glob("jt_repair_*.json")
        seen["log"] = json.loads(log.read_text())
        return real(conn, it)

    monkeypatch.setitem(repair.REPAIR_FNS, "finalized-net", spy)
    db.tick(hours=3)
    assert repair.main(["--apply", "--only", "finalized-net", "--expect", "1",
                        "--log-dir", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert out.index("about to be written") < out.index(f"MR {sc.root} ") < out.index("repaired")
    (planned,) = seen["log"]["planned"]["finalized-net"]
    assert planned["jute_mr_id"] == sc.root and planned["new"]["net_total"] == 1371989.0
    assert seen["log"]["repaired"] == []
    # the row keeps the time the lorry was finalized
    assert db.row("jute_mr", jute_mr_id=sc.root)["updated_date_time"] == finalized_at


def test_repair_finalized_net_stops_at_the_first_row_that_fails(
        scenario, tmp_path, monkeypatch, capsys):
    """A later lorry's TDS counts the earlier ones as repaired."""
    sc, db = scenario, scenario.db
    later = sc.add_root(ge_no=30, ge_date=date(2026, 9, 5))
    finalized_the_old_way(sc)                                    # MR date 3 Sep
    chain = Chain(sc, root=later)
    chain.save("B", mr_date=date(2026, 9, 6))
    chain.save("A", pct=0.5, mr_date=date(2026, 9, 8))
    db.update("jute_mr", {"jute_mr_id": later}, claim_amount=None, tds_amount=None,
              roundoff=None, net_total=None)
    real = repair.REPAIR_FNS["finalized-net"]

    def first_fails(conn, it):
        if it["jute_mr_id"] == sc.root:
            raise RuntimeError("no longer a finalized root with a NULL claim / net")
        return real(conn, it)

    monkeypatch.setitem(repair.REPAIR_FNS, "finalized-net", first_fails)
    assert repair.main(["--apply", "--only", "finalized-net", "--expect", "2",
                        "--log-dir", str(tmp_path)]) == 1

    assert db.row("jute_mr", jute_mr_id=later)["net_total"] is None      # not attempted
    (log,) = tmp_path.glob("jt_repair_*.json")
    logged = json.loads(log.read_text())
    assert [r["jute_mr_id"] for r in logged["failed"]] == [sc.root]
    assert [r["jute_mr_id"] for r in logged["not_attempted"]] == [later]
    assert logged["repaired"] == [] and "STOPPED" in capsys.readouterr().out


def test_repair_leaves_a_pending_root_alone_whose_party_is_not_a_group_company(scenario):
    """The mill has two parties with the supplier's name; a Pending chain
    root on the other one was never touched by the old un-finalize."""
    sc, db = scenario, scenario.db
    Chain(sc).save("B", mr_date=date(2026, 9, 1))
    twin = sc.add_party(sc.a_co, sc.supplier_party_name)
    db.update("jute_mr", {"jute_mr_id": sc.root}, party_id=str(twin))
    with DatabaseConnection.get_transaction() as conn:
        assert repair.plan_unfinalized_party(conn) == []
