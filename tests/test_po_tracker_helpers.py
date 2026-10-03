"""PO Tracker shaping (po_tracker_helpers): the status model, PO numbers,
reconciliation of a transfer PO against the MR it was built from, search,
period / mill options, "PO Lorries n of m", banners, table and CSV frames.

The fixtures mirror the live GE 21 lorry (Empire -> Jagrati -> Empire,
31-Aug-2026, original PO EJM/F/JPO/26-27/00011), the worked example of
design-review-3 section 2.2; PO lines are built with po_helpers.build_po_lines,
i.e. exactly as po_ops writes them.
"""
import re
from datetime import date

import pandas as pd
import pytest

from src.jutetransfer import po_tracker_helpers as H
from src.jutetransfer.po_helpers import build_po_lines, po_totals

ROOT, HOP = 28137, 28290
ORIG_PO, FWD_PO, FINAL_PO = 13314, 20001, 20002
TODAY = date(2026, 10, 2)

# the marker po_ops writes today (po_helpers.build_marker); older tails are
# covered by test_final_marker_with_trailing_fields_is_found
FINAL_NOTE = "JT|FINAL|root=28137|mr=28137|srcpo=13314|orig=207/38544/2026-08-31|"


def forward_note(root=ROOT, hop=HOP, src_po=ORIG_PO):
    return f"JT|FORWARD|root={root}|mr={hop}|srcpo={src_po}|"


# --- fixtures: rows shaped like the tracker queries (po_queries) --------------

def mr_line(li_id, mr_id, item, kg, rate, name, active=1):
    return {"jute_mr_li_id": li_id, "jute_mr_id": mr_id, "jute_po_li_id": None,
            "active": active, "actual_item_id": item, "challan_item_id": item,
            "item_name": name, "accepted_weight": kg, "actual_weight": kg, "rate": rate}


def hop_lines(hop=HOP):
    return [mr_line(hop * 10 + 1, hop, 9105, 9312.0, 12850.0, "TD-5"),
            mr_line(hop * 10 + 2, hop, 9106, 1441.0, 12650.0, "TD-6")]


def root_lines(root=ROOT, returned=False):
    """After finalize the mill MR carries the marked-up rates."""
    r5, r6 = (12914.0, 12713.0) if returned else (12850.0, 12650.0)
    return [mr_line(root * 10 + 1, root, 105, 9312.0, r5, "TD-5"),
            mr_line(root * 10 + 2, root, 106, 1441.0, r6, "TD-6")]


def po_lines_for(po_id, mr_lines, uom="BALE"):
    """PO lines as po_ops writes them; hop lines get linked like po_ops does."""
    names = {l["jute_mr_li_id"]: l["item_name"] for l in mr_lines}
    out = []
    for n, pl in enumerate(build_po_lines(mr_lines, uom)):
        out.append({"jute_po_li_id": po_id * 10 + n, "jute_po_id": po_id,
                    "item_id": pl["item_id"], "item_name": names[pl["jute_mr_li_id"]],
                    "quantity": pl["quantity"], "rate": pl["rate"], "value": pl["value"],
                    "percentage": None, "jute_uom": uom, "active": 1,
                    "_mr_li": pl["jute_mr_li_id"]})
    return out


def hop_row(root=ROOT, hop=HOP, ge_no=21, ge_date=date(2026, 8, 31), returned=False,
            orig_po=ORIG_PO, orig_no=11):
    row = {
        "hop_mr_id": hop, "root_mr_id": root, "lorry_date": ge_date,
        "hop_mr_no": 501, "hop_mr_date": ge_date, "fwd_po_id": None,
        "hop_co_id": 74, "hop_co_prefix": "JTSPL",
        "hop_co_name": "JAGRATI TRADE SERVICES PVT. LTD.",
        "hop_party_name": "HONEYWELL COMMERCIAL PVT. LTD.",
        "root_status_id": 3 if returned else 13,
        "root_status_name": "APPROVED" if returned else "PENDING",
        "orig_po_id": orig_po, "ge_no": ge_no, "ge_date": ge_date,
        "root_mr_date": date(2026, 9, 2) if returned else ge_date,
        "root_mr_no": 812 if returned else None, "vehicle_no": "WB-57C-6522",
        "invoice_no": "JTSPL/INV/26-27/0045" if returned else None,
        "invoice_amount": 1385746.0 if returned else None,
        "mill_co_id": 2, "mill_prefix": "EJM", "mill_name": "THE EMPIRE JUTE COMPANY LTD.",
        "broker_name": "shyamji", "root_party_name": "JAGRATI TRADE SERVICES PVT. LTD.",
        "orig_po_no": orig_no, "orig_po_date": date(2026, 8, 25), "orig_po_status_id": 5,
        "orig_po_close_type": "AUTO", "orig_po_weight": 20000.0, "orig_po_value": 2570000.0,
        "orig_po_lorries": 2, "orig_po_supplier_name": "shyamji",
        "orig_po_party_name": "HONEYWELL COMMERCIAL PVT. LTD.",
        "orig_po_co_prefix": "EJM", "orig_po_branch_prefix": "F",
    }
    for key in ("fwd_po_row_id", "fwd_po_no", "fwd_po_date", "fwd_po_status_id",
                "fwd_po_close_type", "fwd_po_weight", "fwd_po_value", "fwd_po_uom",
                "fwd_po_note", "fwd_po_co_prefix", "fwd_po_branch_prefix"):
        row[key] = None
    return row


def transfer_po_row(po_id, po_no, note, *, co_prefix, branch_prefix, lines,
                    status_id=5, po_date=date(2026, 9, 2), party="JAGRATI TRADE SERVICES PVT. LTD."):
    kg, value = po_totals([{"line_kg": l["quantity"] * 150, "value": l["value"]} for l in lines])
    return {"jute_po_id": po_id, "po_no": po_no, "po_date": po_date, "branch_id": 29,
            "status_id": status_id, "close_type": "TRANSFER" if status_id == 5 else None,
            "weight": kg, "jute_po_value": value, "jute_uom": "BALE", "internal_note": note,
            "co_id": 2, "co_prefix": co_prefix, "co_name": "x", "branch_prefix": branch_prefix,
            "party_name": party}


def scenario(*, returned=False, fwd=True, final=None, root=ROOT, hop=HOP, ge_no=21,
             ge_date=date(2026, 8, 31), fwd_po=FWD_PO, fwd_no=21, orig_po=ORIG_PO, orig_no=11):
    """Inputs of build_lorries for one lorry; `final` defaults to `returned`."""
    final = returned if final is None else final
    row = hop_row(root, hop, ge_no, ge_date, returned, orig_po, orig_no)
    hl, rl = hop_lines(hop), root_lines(root, returned)
    po_lines, tpos = [], []
    if fwd:
        fl = po_lines_for(fwd_po, hl)
        by_li = {l["_mr_li"]: l["jute_po_li_id"] for l in fl}
        for l in hl:
            l["jute_po_li_id"] = by_li.get(l["jute_mr_li_id"])
        tp = transfer_po_row(fwd_po, fwd_no, forward_note(root, hop, orig_po),
                             co_prefix="JTSPL", branch_prefix=None, lines=fl,
                             po_date=ge_date, party="HONEYWELL COMMERCIAL PVT. LTD.")
        row.update(fwd_po_id=fwd_po, fwd_po_row_id=fwd_po, fwd_po_no=fwd_no,
                   fwd_po_date=ge_date, fwd_po_status_id=5, fwd_po_close_type="TRANSFER",
                   fwd_po_weight=tp["weight"], fwd_po_value=tp["jute_po_value"],
                   fwd_po_uom="BALE", fwd_po_note=tp["internal_note"],
                   fwd_po_co_prefix="JTSPL", fwd_po_branch_prefix=None)
        po_lines += fl
        tpos.append(tp)
    if final:
        nl = po_lines_for(FINAL_PO, rl)
        note = FINAL_NOTE.replace(str(ROOT), str(root))
        tpos.append(transfer_po_row(FINAL_PO, 63, note, co_prefix="EJM",
                                    branch_prefix="F", lines=nl))
        po_lines += nl
    siblings = [
        {"jute_mr_id": 27001, "po_id": orig_po, "jute_gate_entry_no": 19,
         "jute_gate_entry_date": date(2026, 8, 29), "status_id": 3,
         "status_name": "APPROVED", "mr_weight": 9800.0},
        {"jute_mr_id": root, "po_id": orig_po, "jute_gate_entry_no": ge_no,
         "jute_gate_entry_date": ge_date, "status_id": row["root_status_id"],
         "status_name": row["root_status_name"], "mr_weight": 10753.0},
    ]
    return {"hops": [row], "transfer_pos": tpos, "mr_lines": hl + rl,
            "po_lines": po_lines, "siblings": siblings, "today": TODAY}


def one(s):
    lorries = H.build_lorries(**s)
    assert len(lorries) == 1
    return lorries[0]


def fwd_po_line(s, n):
    return [l for l in s["po_lines"] if l["jute_po_id"] == FWD_PO][n]


# --- status model (design-review-3 section 3.5) -------------------------------

def test_completed_lorry_with_all_three_pos():
    l = one(scenario(returned=True))
    assert (l["status"], l["status_reason"]) == ("Completed", "")
    assert (l["orig_po"], l["fwd_po"], l["final_po"]) == ("EJM 11", "JTSPL 21", "EJM 63")
    assert l["route"] == "EJM → JTSPL → EJM"
    assert round(l["markup_pct"], 2) == 0.50
    assert not H.needs_attention(l["status"])
    assert l["days"] is None


def test_awaiting_return_is_normal_and_counts_days():
    l = one(scenario())
    assert l["status"] == "Awaiting return"
    assert (l["fwd_po"], l["final_po"]) == ("JTSPL 21", "awaiting")
    assert l["awaiting"] and not H.needs_attention(l["status"])
    assert l["days"] == (TODAY - date(2026, 8, 31)).days
    assert l["markup_pct"] is None            # nothing sold on yet


def test_no_po_yet_forwarding():
    l = one(scenario(fwd=False))
    assert (l["status"], l["status_reason"]) == ("No PO yet", "Forwarding PO not created yet")
    assert l["fwd_po"] == "not yet" and l["final_po"] == "awaiting"
    # expected until the backfill has run (a save always writes its PO): no alarm
    assert not H.needs_attention(l["status"])
    assert l["awaiting"]                      # still listed under Awaiting return


def test_no_po_yet_final_once_back_at_the_mill():
    l = one(scenario(returned=True, final=False))
    assert (l["status"], l["status_reason"]) == ("No PO yet", "Final PO not created yet")
    assert l["final_po"] == "not yet" and l["returned"]
    assert not H.needs_attention(l["status"])


def test_both_missing_named_in_one_reason():
    l = one(scenario(returned=True, fwd=False, final=False))
    assert l["status_reason"] == "Forwarding PO and Final PO not created yet"


def test_no_accepted_weight_is_not_applicable_not_missing():
    s = scenario(fwd=False)
    for line in s["mr_lines"]:
        if line["jute_mr_id"] == HOP:
            line["accepted_weight"] = 0.0
    l = one(s)
    assert l["fwd_po"] == "n/a"
    assert l["status"] == "Awaiting return"


def test_inactive_lines_do_not_need_a_po():
    s = scenario(fwd=False)
    for line in s["mr_lines"]:
        if line["jute_mr_id"] == HOP:
            line["active"] = 0
    assert one(s)["fwd_po"] == "n/a"


def test_other_root_status_needs_attention():
    s = scenario()
    s["hops"][0].update(root_status_id=48, root_status_name="RETURNED")
    l = one(s)
    assert l["status"] == "Other (Returned)"
    assert H.needs_attention(l["status"])
    assert not l["awaiting"]
    assert l["final_po"] == "—"
    # ERP status 48 is called "Returned"; the chip for chains back at the mill is not
    assert not H.matches_show(l, "Back at mill") and "Returned" not in H.SHOW_OPTIONS


def test_odd_mill_mr_status_is_not_hidden_behind_no_po_yet():
    s = scenario(fwd=False)
    s["hops"][0].update(root_status_id=48, root_status_name="RETURNED")
    l = one(s)
    assert l["status"] == "Other (Returned)" and H.needs_attention(l["status"])
    assert l["fwd_po"] == "not yet"
    assert H.backfill_banners([l]) == []        # it needs a look, not the backfill


def test_back_at_mill_needs_status_3_and_the_mr_number():
    """The rule the backfill and po_ops.plan_final_po use for 'finalized'."""
    assert H.is_back_at_mill(3, 812) and H.is_back_at_mill(3.0, "812")
    assert not H.is_back_at_mill(3, None) and not H.is_back_at_mill(13, 812)
    assert not H.is_back_at_mill(48, 812) and not H.is_back_at_mill(None, None)
    s = scenario(returned=True, final=False)
    s["hops"][0]["root_mr_no"] = None          # Approved, but never numbered by a return
    l = one(s)
    assert l["status"] == "Other (Approved)" and "no MR number" in l["status_reason"]
    assert H.needs_attention(l["status"])
    assert not l["returned"] and not H.matches_show(l, "Back at mill")
    assert l["final_po"] == "—" and not l["final"]["missing"]
    assert H.backfill_banners([l]) == []        # the backfill never makes its Final PO
    assert l["route"] == "EJM → JTSPL" and l["invoice_no"] == ""


def _check_reason(s):
    l = one(s)
    assert l["status"] == "Check"
    assert H.needs_attention(l["status"])
    return l["status_reason"]


def test_check_hop_linked_to_an_erp_po_without_marker():
    s = scenario()
    s["hops"][0]["fwd_po_note"] = None
    assert "not a transfer PO" in _check_reason(s)


def test_check_marker_names_another_mr():
    s = scenario()
    s["hops"][0]["fwd_po_note"] = forward_note(hop=HOP + 1)
    assert "names another MR" in _check_reason(s)


def test_check_hop_linked_to_a_po_that_no_longer_exists():
    s = scenario()
    s["hops"][0]["fwd_po_row_id"] = None
    assert "no longer exists" in _check_reason(s)


def test_check_forwarding_po_reopened_in_the_erp():
    s = scenario()
    s["hops"][0]["fwd_po_status_id"] = 3
    assert "no longer Closed (now Approved)" in _check_reason(s)


def test_check_final_po_reopened_in_the_erp():
    s = scenario(returned=True)
    s["transfer_pos"][-1].update(status_id=1, close_type=None)
    assert "Final PO EJM/F/JPO/26-27/00063 is no longer Closed" in _check_reason(s)


def test_check_two_final_pos():
    s = scenario(returned=True)
    extra = dict(s["transfer_pos"][-1], jute_po_id=FINAL_PO + 5, po_no=64)
    s["transfer_pos"].append(extra)
    assert "2 Final POs exist" in _check_reason(s)


def test_check_final_po_while_lorry_not_returned():
    assert ("exists but the lorry is not back at the mill (its MR is Pending)"
            in _check_reason(scenario(final=True)))


def test_check_final_po_on_an_approved_mr_without_its_number():
    s = scenario(returned=True)
    s["hops"][0]["root_mr_no"] = None
    assert "(its MR is Approved without an MR number)" in _check_reason(s)


def test_check_weight_more_than_half_a_unit_off():
    s = scenario()
    line = fwd_po_line(s, 0)
    line["quantity"] = 64.0                         # 9,600 kg against 9,312
    assert "more than half a bale apart" in _check_reason(s)


def test_check_rate_more_than_25_off():
    s = scenario()
    fwd_po_line(s, 0)["rate"] = 12900.0             # MR rate 12,850
    assert "more than 25 apart" in _check_reason(s)


def test_rounding_within_tolerance_is_not_a_check():
    s = scenario(returned=True)
    # live-like rounding: 12,914 -> 12,900 (14 off), 1,441 kg -> 10 bales (59 off)
    assert one(s)["status"] == "Completed"
    rows = H.reconcile(hop_lines(), [dict(fwd_po_line(s, 0), rate=12875.0)])
    assert H.recon_flags(rows[:1]) == []            # 25 off: still rounding


def test_one_unit_floor_is_not_a_weight_check():
    mr = [mr_line(1, HOP, 9105, 40.0, 12850.0, "TD-5")]
    po = [{"jute_po_li_id": 9, "jute_po_id": FWD_PO, "item_id": 9105, "quantity": 1.0,
           "rate": 12850.0, "value": 19275.0, "jute_uom": "BALE", "active": 1}]
    assert H.recon_flags(H.reconcile(mr, po, "BALE")) == []


def test_check_wins_over_po_missing():
    s = scenario(fwd=False, final=True)            # final PO although not returned
    assert one(s)["status"] == "Check"


@pytest.mark.parametrize("tail", ["party=207|pbr=38544|mrdate=2026-08-31|",
                                  "orig=207/38544/2026-08-31|", ""])
def test_final_marker_with_trailing_fields_is_found(tail):
    s = scenario(returned=True)
    s["transfer_pos"][-1]["internal_note"] = f"JT|FINAL|root={ROOT}|mr={ROOT}|srcpo={ORIG_PO}|{tail}"
    l = one(s)
    assert l["status"] == "Completed" and l["final_po_count"] == 1
    assert l["final"]["pos"][0]["full"] == "EJM/F/JPO/26-27/00063"


def test_show_chips():
    awaiting, no_po, done = (one(scenario()), one(scenario(fwd=False)),
                             one(scenario(returned=True)))
    pick = lambda show: [l["status"] for l in (awaiting, no_po, done) if H.matches_show(l, show)]
    assert H.SHOW_OPTIONS == ["All", "Awaiting return", "Back at mill", "Needs attention"]
    assert pick("All") == ["Awaiting return", "No PO yet", "Completed"]
    assert pick("Awaiting return") == ["Awaiting return", "No PO yet"]
    assert pick("Back at mill") == ["Completed"]
    assert pick("Needs attention") == []
    # a lorry back at the mill whose POs are not made yet is listed there too
    back_no_po = one(scenario(returned=True, fwd=False, final=False))
    assert H.matches_show(back_no_po, "Back at mill")
    assert not H.matches_show(back_no_po, "Needs attention")
    assert not H.matches_show(back_no_po, "Awaiting return")


def test_only_check_and_other_need_attention():
    assert [s for s in ("Check", "Other (Returned)", "No PO yet", "Completed",
                        "Awaiting return") if H.needs_attention(s)] == ["Check", "Other (Returned)"]


def test_worst_status_of_an_original_po():
    assert H.worst_status(["Completed", "Awaiting return"]) == "Awaiting return"
    assert H.worst_status(["Completed", "No PO yet", "Awaiting return"]) == "No PO yet"
    # an odd mill MR needs a look, a PO not made yet does not
    assert H.worst_status(["Completed", "Other (Returned)", "No PO yet"]) == "Other (Returned)"
    assert H.worst_status(["No PO yet", "Check", "Other (Returned)"]) == "Check"
    assert H.worst_status([]) == "—"


# --- PO numbers ---------------------------------------------------------------

def test_short_po_numbers():
    assert H.short_po_no("EJM", 11) == "EJM 11"
    assert H.short_po_no("JTSPL", 21.0) == "JTSPL 21"
    assert H.short_po_no(None, 5) == "5"
    assert H.short_po_no("EJM", None) == ""


def test_full_po_numbers():
    assert H.full_po_no(11, "EJM", "F", date(2026, 8, 25)) == "EJM/F/JPO/26-27/00011"
    assert H.full_po_no(21.0, "JTSPL", None, pd.Timestamp("2026-09-01")) == "JTSPL/JPO/26-27/00021"
    assert H.full_po_no(5, "LCPL", "", "2027-03-31") == "LCPL/JPO/26-27/00005"
    assert H.full_po_no(None, "EJM", "F", None, po_id=777) == "#777"
    assert H.full_po_no(None, "EJM", "F", None) == ""


def test_po_number_ranges_and_rate_ranges():
    assert H.po_number_ranges([("JTSPL", 4), ("JTSPL", 5), ("JTSPL", 6), ("JTSPL", 8)]) == "JTSPL 4–6, 8"
    assert H.po_number_ranges([("JTSPL", 2), ("EJM", 1), ("EJM", None)]) == "EJM 1; JTSPL 2"
    assert H.po_number_ranges([]) == ""
    assert H.rate_range([12850.0, 12850]) == "12,850"
    assert H.rate_range([12500, 11800, None, 12000]) == "11,800–12,500"
    assert H.rate_range([12500, 11800], "-", False) == "11800-12500"
    assert H.rate_range([]) == ""


def test_po_status_labels():
    assert H.po_status_label(5, "AUTO") == "Closed – auto-settled"
    assert H.po_status_label(5, "TRANSFER") == "Closed – transfer PO"
    assert H.po_status_label(3) == "Approved"
    assert H.po_status_label(None) == "—"


def test_original_po_block_and_legacy_quintal_weight():
    l = one(scenario())
    assert H.original_po_headline(l["orig"]) == "2 lorries · 200.00 qtl · 2,570,000"
    s = scenario()
    s["hops"][0].update(orig_po_weight=400.0, orig_po_lorries=4)
    s["po_lines"].append({"jute_po_li_id": 1, "jute_po_id": ORIG_PO, "item_id": 105,
                          "item_name": "TD-5", "quantity": 100.0, "rate": 16300.0,
                          "value": 6520000.0, "percentage": None, "jute_uom": "130",
                          "active": 1})
    orig = one(s)["orig"]
    assert orig["weight_kg"] == 40000.0          # pre-ERP POs store quintals
    table = H.original_po_lines_table(orig)
    assert table.to_dict("records") == [{"Quality": "TD-5", "Units": "100",
                                         "Rate": "16,300", "%": "—"}]


def test_lorry_without_an_original_po():
    s = scenario(fwd=False)
    s["hops"][0].update(orig_po_id=None, orig_po_no=None)
    l = one(s)
    assert l["orig"] is None and l["orig_po"] == "—"
    assert H.original_po_rows([l]) == []


# --- reconciliation (design-review-3 section 2.3) -------------------------------

def test_forwarding_po_reconciles_with_the_hop_mr():
    l = one(scenario())
    rows = l["hops"][0]["recon"]
    assert [(r["quality"], r["mr_kg"], r["po_kg"], r["mr_rate"], r["po_rate"]) for r in rows] == [
        ("TD-5", 9312.0, 9300.0, 12850.0, 12850.0),
        ("TD-6", 1441.0, 1500.0, 12650.0, 12650.0),
    ]
    assert l["fwd_kg"] == 10800.0 and l["fwd_value"] == 1384800.0
    assert l["fwd_diff"] == pytest.approx(5921.5)
    t = H.recon_totals(rows)
    # no rate moved: 'rate unchanged', never the old 'rate to nearest 50 0'
    assert H.difference_sentence(l["fwd_value"], l["mr_amt"], t, "MR", "BALE") == (
        "PO 1,384,800 vs MR 1,378,878: +5,922 (+0.43 %) — whole bales +5,922 · "
        "rate unchanged.")


def test_final_po_reproduces_the_worked_example():
    l = one(scenario(returned=True))
    rows = l["final"]["recon"]
    assert [(r["po_kg"], r["po_rate"], r["mr_rate"]) for r in rows] == [
        (9300.0, 12900.0, 12914.0), (1500.0, 12700.0, 12713.0)]
    assert (l["final_value"], l["inv_amt"], l["final_diff"]) == (1390200.0, 1385746.0, 4454.0)
    t = H.recon_totals(rows)
    assert round(t["weight_part"], 2) == 5945.0
    assert round(t["rate_part"], 2) == -1491.01
    assert H.difference_sentence(l["final_value"], l["inv_amt"], t, "invoice", "BALE") == (
        "PO 1,390,200 vs invoice 1,385,746: +4,454 (+0.32 %) — whole bales +5,945 · "
        "rate to nearest ₹50: -1,491.")


def test_weight_part_plus_rate_part_is_exactly_the_difference():
    for s in (scenario(), scenario(returned=True)):
        l = one(s)
        for rows in (l["hops"][0]["recon"], l["final"]["recon"]):
            for r in rows:
                assert r["weight_part"] + r["rate_part"] == pytest.approx(r["diff"], abs=1e-6)
            t = H.recon_totals(rows)
            if rows:
                assert t["weight_part"] + t["rate_part"] == pytest.approx(t["diff"], abs=1e-6)


def test_sentence_names_what_rounding_does_not_explain_and_adds_up():
    # the invoice 1,000 under the MR lines: the split shows it separately
    t = {"weight_part": 5945.0, "rate_part": -1491.01, "unmatched": None,
         "po_value": 1390200.0, "mr_amount": 1385746.01}
    text = H.difference_sentence(1390200.0, 1384746.0, t, "invoice", "BALE")
    assert text == ("PO 1,390,200 vs invoice 1,384,746: +5,454 (+0.39 %) — whole bales "
                    "+5,945 · rate to nearest ₹50: -1,491 · MR lines vs invoice +1,000.")
    even = {"weight_part": 0.0, "rate_part": 0.0, "po_value": 100.0, "mr_amount": 100.0}
    assert H.difference_sentence(100.0, 100.0, even, uom="LOOSE") == (
        "PO 100 vs MR 100: 0 (+0.00 %) — whole loose units 0 · rate unchanged.")


def test_rate_part_cannot_read_as_one_number_with_the_rounding_step():
    """'rate to nearest 50 0.' read as '500': the step is '₹50' followed by a
    colon, and a rate that did not move says so."""
    for rate in (0.0, -1491.01, 1371.4, 0.3):
        t = {"weight_part": 5945.0, "rate_part": rate, "po_value": 1390200.0,
             "mr_amount": 1390200.0 - 5945.0 - rate}
        text = H.difference_sentence(1390200.0, t["mr_amount"], t, "MR", "BALE")
        assert not re.search(r"50 [-+0-9]", text), text
        assert ("rate unchanged" in text) == (round(rate) == 0), text
        assert ("rate to nearest ₹50: " in text) == (round(rate) != 0), text


def test_rounding_left_over_never_moves_an_unchanged_rate():
    # a half-rupee MR amount: the whole rupees still add up (+5,923), and what
    # rounding leaves over goes to the bale part, not to a rate that is exactly 0
    t = {"weight_part": 5922.5, "rate_part": 0.0, "po_value": 1000001.0, "mr_amount": 994078.5}
    assert H.difference_sentence(1000001.0, 994078.5, t, "MR", "BALE") == (
        "PO 1,000,001 vs MR 994,078: +5,923 (+0.60 %) — whole bales +5,923 · rate unchanged.")


def test_reconcile_without_a_po_lists_the_mr_side_only():
    rows = H.reconcile(hop_lines(), None)
    assert [r["mr_kg"] for r in rows] == [9312.0, 1441.0]
    assert all(r["po_kg"] is None and r["diff"] is None for r in rows)


def test_reconcile_pairs_unlinked_lines_by_item_and_flags_leftovers():
    mr = root_lines(returned=True)
    po = po_lines_for(FINAL_PO, mr)
    po.append(dict(po[0], jute_po_li_id=999, item_id=4242, item_name="TD-4"))
    rows = H.reconcile(mr, po, "BALE")
    assert [(r["quality"], r["mr_kg"] is None) for r in rows] == [
        ("TD-5", False), ("TD-6", False), ("TD-4", True)]
    assert H.recon_flags(rows) == ["TD-4: PO line of 9,300 kg has no MR line"]


def test_unpaired_lines_are_unmatched_and_listed_after_the_paired_ones():
    """Only a paired row is split into a weight part and a rate part; an MR
    line without a PO line (or the other way round) is 'unmatched' as a
    whole. They used to land in the rate / weight part."""
    mr = root_lines(returned=True)
    po = po_lines_for(FINAL_PO, mr)
    po.append(dict(po[0], jute_po_li_id=999, item_id=4242, item_name="TD-4"))   # no MR line
    no_po_line = mr_line(ROOT * 10, ROOT, 107, 480.0, 12000.0, "TD-7")          # first by id
    rows = H.reconcile([no_po_line] + mr, po, "BALE")
    assert [(r["quality"], r["paired"]) for r in rows] == [
        ("TD-5", True), ("TD-6", True), ("TD-7", False), ("TD-4", False)]
    td7, td4 = rows[2], rows[3]
    assert (td7["weight_part"], td7["rate_part"]) == (None, None)
    assert td7["unmatched"] == td7["diff"] == -480.0 * 120.0
    assert (td4["weight_part"], td4["rate_part"]) == (None, None)
    assert td4["unmatched"] == td4["diff"] == td4["po_value"]
    t = H.recon_totals(rows)
    assert round(t["weight_part"], 2) == 5945.0 and round(t["rate_part"], 2) == -1491.01
    assert t["unmatched"] == td7["diff"] + td4["diff"]
    assert t["weight_part"] + t["rate_part"] + t["unmatched"] == pytest.approx(t["diff"], abs=1e-6)


def test_final_po_without_its_lines_is_told_as_unmatched_not_as_rate():
    """Review case: a Final PO whose lines are gone printed 'whole loose units
    0, rate to nearest 50 -1,226,182, MR lines vs invoice +1,230,768'."""
    s = scenario(returned=True)
    s["po_lines"] = [p for p in s["po_lines"] if p["jute_po_id"] != FINAL_PO]
    l = one(s)
    assert l["status"] == "Check" and "on the MR has no PO line" in l["status_reason"]
    t = H.recon_totals(l["final"]["recon"])
    assert t["weight_part"] is None and t["rate_part"] is None
    assert H.difference_sentence(l["final_value"], l["inv_amt"], t, "invoice", "BALE") == (
        "PO 1,390,200 vs invoice 1,385,746: +4,454 (+0.32 %) — unmatched lines -1,385,746 · "
        "PO total vs its lines +1,390,200.")
    final_rows = H.line_detail_frame([l]).query("`PO Kind` == 'Final'")
    assert final_rows["Whole Unit Diff"].isna().all() and final_rows["Rate Rounding Diff"].isna().all()
    assert final_rows["Unmatched Diff"].sum() == pytest.approx(final_rows["Diff"].sum())


def test_split_adds_up_with_paired_and_unmatched_lines():
    s = scenario()
    s["po_lines"].remove(fwd_po_line(s, 1))   # TD-6's PO line gone; the PO header still counts it
    hop = one(s)["hops"][0]
    t = H.recon_totals(hop["recon"])
    assert H.difference_sentence(hop["po"]["value"], hop["mr_amount"], t, "MR", "BALE") == (
        "PO 1,384,800 vs MR 1,378,878: +5,922 (+0.43 %) — whole bales -1,542 · rate unchanged"
        " · unmatched lines -182,286 · PO total vs its lines +189,750.")
    assert -1542 - 182286 + 189750 == 5922    # the printed parts add up to the difference


def test_hand_linked_erp_po_is_not_compared_line_by_line():
    """Review case (hop 28533 linked to LCPL 88): an ERP PO is written with
    other items, so pairing its lines printed six unpaired rows and a Total
    Diff of +1,716,170. Now only the MR side is listed and the row is a
    Check that says why."""
    s = scenario()
    s["hops"][0]["fwd_po_note"] = None                 # an ERP PO: no Jute Transfer marker
    for line in s["po_lines"]:
        line["item_id"] += 1000                        # another company's item ids
    l = one(s)
    hop = l["hops"][0]
    assert l["status"] == "Check" and "not a transfer PO" in l["status_reason"]
    assert not hop["is_transfer_po"] and l["fwd_value"] is None
    assert [(r["mr_kg"], r["po_kg"], r["diff"]) for r in hop["recon"]] == [
        (9312.0, None, None), (1441.0, None, None)]
    lines = H.line_detail_frame([l])
    assert lines["PO Kg"].isna().all() and lines["Diff"].isna().all()


def test_recon_table_compact_and_full():
    l = one(scenario(returned=True))
    compact = H.recon_table(l["final"]["recon"])
    assert list(compact.columns) == ["Quality", "MR kg", "PO kg", "MR rate", "PO rate", "Diff"]
    assert compact.iloc[-1].to_dict() == {"Quality": "Total", "MR kg": "10,753",
                                          "PO kg": "10,800", "MR rate": "", "PO rate": "",
                                          "Diff": "+4,454"}
    full = H.recon_table(l["final"]["recon"], full=True)
    assert list(full.columns) == ["Quality", "MR kg", "PO kg", "PO units", "MR rate",
                                  "PO rate", "MR amount", "PO value", "Diff"]
    assert full.iloc[0]["PO units"] == "62"


# --- search (design-review-3 section 3.9) ----------------------------------------

def test_search_all_digits_is_an_exact_number_match():
    l = one(scenario(returned=True))
    for hit in ("21", "11", "63", "501", "812", "00063", " 21 "):
        assert H.search_matches(l, hit), hit
    for miss in ("121", "2", "1", "6300"):
        assert not H.search_matches(l, miss), miss


def test_search_text_is_a_case_insensitive_substring():
    l = one(scenario(returned=True))
    for hit in ("jtspl/jpo/26-27/00021", "EJM/F/JPO/26-27/00063", "honeywell", "Shyam",
                "WB-57C", "wb57c6522", "jtspl/inv", "EJM 63", "ejm  11"):
        assert H.search_matches(l, hit), hit
    for miss in ("limelight", "JPO/25-26", "EJM 6"):
        assert not H.search_matches(l, miss), miss
    assert H.search_matches(l, "") and H.search_matches(l, None)


def test_search_never_raises_whatever_is_typed():
    """'²' and '①' are digits to str.isdigit() but not to int(): typing one
    (a long press on 2 on many phone keyboards) replaced the page with a
    traceback. Every character Python calls a digit or numeric, alone or
    after a prefix, and other odd input simply matches or not."""
    l = one(scenario(returned=True))
    odd = [chr(c) for c in range(0x110000) if chr(c).isdigit() or chr(c).isnumeric()]
    assert "²" in odd and "①" in odd and "½" in odd
    for ch in odd:
        for text in (ch, ch * 3, f"GE {ch}", f"#{ch}", f"MR {ch}{ch}", f"{ch}6522"):
            H.search_matches(l, text)
    for text in ("1" * 5000, "GE " + "9" * 5000, "GE" + " " * 20000 + "x",
                 "#" + " " * 20000 + "#", "\x00", "\ud800", "🚚", "%", "_", "'", "\\",
                 None, 63, 21.0):
        H.search_matches(l, text)
    assert not H.search_matches(l, "²") and not H.search_matches(l, "①")
    # decimal digits of other scripts are numbers, as int() reads them
    assert H.search_matches(l, "６３") and H.search_matches(l, "٦٣") and H.search_matches(l, "GE ２１")


def test_search_numbers_named_like_the_table():
    """'GE 21' is what the Lorry column shows; 'MR 812', 'PO 63' and '#21'
    name a number too. A named number matches that kind only."""
    l = one(scenario(returned=True))    # GE 21 · mill MR 812 · forwarder MR 501 · POs 11 / 21 / 63
    for hit in ("GE 21", "ge21", "GE No. 21", "ge no: 21", "GE-21", "GE #21", "GE  21",
                "MR 812", "mr 501", "MR No. 501", "PO 63", "po 11", "PO 21", "#21", "# 63"):
        assert H.search_matches(l, hit), hit
    for miss in ("GE 11", "GE 63", "MR 21", "PO 501", "PO 812", "GE 121", "#121", "MR 8"):
        assert not H.search_matches(l, miss), miss


def test_search_plate_digits_find_the_lorry_number():
    l = one(scenario(returned=True))    # WB-57C-6522
    for hit in ("6522", "#6522", "c6522", "57C 6522", "wb-57c"):
        assert H.search_matches(l, hit), hit
    # under four digits a number is a GE / PO / MR number only, so '57' and
    # '652' do not pick plates; a named number never looks at the plate
    for miss in ("57", "652", "0652", "5765", "GE 6522", "MR 6522"):
        assert not H.search_matches(l, miss), miss


def test_search_finds_a_po_shown_by_its_id():
    s = scenario()
    s["hops"][0]["fwd_po_row_id"] = None          # the PO row is gone: the cell shows '#20001'
    l = one(s)
    assert l["fwd_po"] == f"#{FWD_PO}"
    assert H.search_matches(l, f"#{FWD_PO}") and H.search_matches(l, str(FWD_PO))


def test_nothing_found_text_is_markdown_safe():
    assert H.nothing_found_text("GE 21", 2026) == "Nothing found for 'GE 21' in FY 26-27."
    assert H.nothing_found_text("**x**_[y]$", 2026) == (
        r"Nothing found for '\*\*x\*\*\_\[y\]\$' in FY 26-27.")


# --- periods, mills, PO Lorries -------------------------------------------------

CHAINS = [
    {"hop_mr_id": 1, "root_mr_id": 10, "lorry_date": date(2026, 9, 23), "mill_co_id": 2,
     "mill_prefix": "EJM", "mill_name": "Empire", "fwd_po_id": None},
    {"hop_mr_id": 2, "root_mr_id": 20, "lorry_date": date(2026, 9, 1), "mill_co_id": 106,
     "mill_prefix": "LCPL", "mill_name": "Limelight", "fwd_po_id": None},
    {"hop_mr_id": 3, "root_mr_id": 30, "lorry_date": date(2026, 8, 31), "mill_co_id": 2,
     "mill_prefix": "EJM", "mill_name": "Empire", "fwd_po_id": None},
    {"hop_mr_id": 4, "root_mr_id": 30, "lorry_date": date(2026, 8, 31), "mill_co_id": 2,
     "mill_prefix": "EJM", "mill_name": "Empire", "fwd_po_id": None},   # 2nd hop, same lorry
    {"hop_mr_id": 5, "root_mr_id": 40, "lorry_date": date(2026, 3, 15), "mill_co_id": 106,
     "mill_prefix": "LCPL", "mill_name": "Limelight", "fwd_po_id": None},
]


def test_period_options_newest_first_with_lorry_counts():
    assert H.period_options(CHAINS) == [
        ("M:2026-09", "Sep 2026 (2)"), ("M:2026-08", "Aug 2026 (1)"),
        ("FY:2026", "FY 26-27 – all (3)"),
        ("M:2026-03", "Mar 2026 (1)"), ("FY:2025", "FY 25-26 – all (1)"),
    ]
    assert H.period_options(CHAINS, mill_co_id=2) == [
        ("M:2026-09", "Sep 2026 (1)"), ("M:2026-08", "Aug 2026 (1)"),
        ("FY:2026", "FY 26-27 – all (2)"),
    ]


def test_default_period_is_the_newest_month_not_after_the_database_date():
    options = H.period_options(CHAINS)
    assert H.default_period(options, TODAY) == "M:2026-09"
    assert H.default_period(options, date(2026, 8, 31)) == "M:2026-08"
    assert H.default_period(options) == "M:2026-09"
    assert H.default_period([("FY:2026", "FY 26-27 – all (0)")]) == "FY:2026"


def test_period_codes():
    assert H.period_fy("M:2026-03") == 2025 and H.period_fy("FY:2026") == 2026
    assert H.period_fy(None) is None
    assert H.in_period(date(2026, 9, 30), "M:2026-09")
    assert not H.in_period(date(2026, 10, 1), "M:2026-09")
    assert H.in_period(date(2027, 3, 31), "FY:2026") and not H.in_period(date(2027, 4, 1), "FY:2026")
    assert not H.in_period(None, "FY:2026")
    assert H.fy_text(2026) == "26-27" and H.fy_start_year(date(2027, 3, 31)) == 2026


def test_mill_options_built_from_the_chains():
    assert H.mill_options(CHAINS, 2026) == [
        (0, "All mills (3)"), (2, "EJM – Empire (2)"), (106, "LCPL – Limelight (1)")]
    assert H.mill_options(CHAINS) == [
        (0, "All mills (4)"), (2, "EJM – Empire (2)"), (106, "LCPL – Limelight (2)")]


def test_po_lorries_n_of_m_by_gate_entry_date_then_number():
    siblings = [
        {"jute_mr_id": 3, "po_id": 7, "jute_gate_entry_no": 21, "jute_gate_entry_date": date(2026, 8, 31)},
        {"jute_mr_id": 1, "po_id": 7, "jute_gate_entry_no": 19, "jute_gate_entry_date": date(2026, 8, 29)},
        {"jute_mr_id": 2, "po_id": 7, "jute_gate_entry_no": 20, "jute_gate_entry_date": date(2026, 8, 31)},
        {"jute_mr_id": 9, "po_id": 8, "jute_gate_entry_no": 5, "jute_gate_entry_date": date(2026, 9, 1)},
    ]
    assert H.po_lorry_positions(siblings) == {1: (1, 3), 2: (2, 3), 3: (3, 3), 9: (1, 1)}
    assert one(scenario())["po_lorries"] == "2 of 2"


# --- Original POs view, totals, banners -------------------------------------------

def _three_lorries():
    a = scenario(root=101, hop=201, ge_no=21, fwd_po=301, fwd_no=21)
    b = scenario(root=102, hop=202, ge_no=22, fwd_po=302, fwd_no=22)
    c = scenario(root=103, hop=203, ge_no=23, fwd=False, orig_po=500, orig_no=12)
    merged = {k: a[k] + b[k] + c[k] for k in ("hops", "transfer_pos", "mr_lines", "po_lines")}
    sib = {(x["jute_mr_id"], x["po_id"]): x for x in a["siblings"] + b["siblings"] + c["siblings"]}
    return H.build_lorries(siblings=list(sib.values()), today=TODAY, **merged)


def test_original_pos_view_rolls_lorries_up_per_po():
    lorries = _three_lorries()
    rows = H.original_po_rows(lorries)
    # Lorries = transferred / received on the PO (siblings) / ordered; same PO
    # date, so the higher PO number comes first.
    assert [(r["Orig PO"], r["Lorries"], r["Fwd POs"], r["Final POs"], r["Status"],
             r["MR Kg"]) for r in rows] == [
        ("EJM 12", "1/2/2", "1 not yet", "0 of 1 · 1 awaiting", "No PO yet", 10753.0),
        ("EJM 11", "2/3/2", "JTSPL 21–22", "0 of 2 · 2 awaiting", "Awaiting return", 21506.0),
    ]
    frame = H.original_pos_frame(rows, full=False)
    assert list(frame.columns) == H.COMPACT_PO_COLUMNS and len(frame) == 2


def test_original_po_row_of_a_lorry_back_without_pos():
    row = H.original_po_rows([one(scenario(returned=True, fwd=False, final=False))])[0]
    assert (row["Fwd POs"], row["Final POs"], row["Status"]) == (
        "1 not yet", "0 of 1 · 1 not yet", "No PO yet")


def test_totals_and_summary_lines():
    lorries = _three_lorries()
    t = H.lorry_totals(lorries)
    assert (t["lorries"], t["fwd_count"], t["final_count"], t["awaiting"], t["attention"],
            t["no_po"]) == (3, 2, 0, 3, 0, 1)
    assert H.summary_line(t) == ("3 lorries · 2 forwarding POs · 0 final POs · 3 awaiting return · "
                                 "1 without PO yet · 0 need attention")
    assert H.po_summary_line(H.original_po_rows(lorries), t) == (
        "2 original POs · 3 lorries transferred · 2 forwarding POs · 0 final POs · "
        "3 awaiting return · 1 without PO yet · 0 need attention")
    assert H.totals_line(t) == ("Total 3 lorries: MR 32,259 kg, amount 4,136,636 · 2 Fwd POs "
                                "21,600 kg, value 2,769,600 (diff +11,843) · no Final PO")


def test_summary_counts_only_check_and_other_as_attention():
    check = one(scenario(final=True))                  # a Final PO before the lorry is back
    other = scenario(root=104, hop=204, ge_no=24)
    other["hops"][0].update(root_status_id=48, root_status_name="RETURNED")
    no_po = one(scenario(root=105, hop=205, ge_no=25, fwd=False))
    t = H.lorry_totals([check, one(other), no_po])
    assert H.summary_line(t) == ("3 lorries · 2 forwarding POs · 1 final PO · 2 awaiting return · "
                                 "1 without PO yet · 2 need attention (1 to check, 1 other)")


def test_fy_note_says_where_the_pos_are_when_the_month_shows_none():
    """The pilot's two POs are in August; the default screen is September:
    '0 forwarding POs · 0 final POs' needs the line that says where they are."""
    pilot = one(scenario(root=101, hop=201, fwd_po=301, fwd_no=1, ge_no=1,
                         ge_date=date(2026, 8, 5), returned=True))
    waiting = one(scenario(root=102, hop=202, ge_no=2, fwd=False))
    september, year = H.lorry_totals([waiting]), H.lorry_totals([pilot, waiting])
    assert H.fy_po_note(september, year, "M:2026-09", 2026) == (
        "Transfer POs so far in FY 26-27: 1 forwarding PO, 1 final PO — 2 are in other "
        "months; pick 'FY 26-27 – all' under Period to see them.")
    one_more = H.lorry_totals([one(scenario(root=103, hop=203, ge_no=3, fwd_po=303, fwd_no=3)),
                               waiting])
    assert H.fy_po_note(september, one_more, "M:2026-09", 2026).startswith(
        "Transfer POs so far in FY 26-27: 1 forwarding PO, 0 final POs — 1 is in other months")
    # nothing to add when the month holds every PO, for the whole year, or during a search
    assert H.fy_po_note(year, year, "M:2026-08", 2026) == ""
    assert H.fy_po_note(september, year, "FY:2026", 2026) == ""
    assert H.fy_po_note(september, year, "M:2026-09", 2026, searching=True) == ""
    assert H.fy_po_note(september, year, None, 2026) == ""


def test_one_info_banner_for_lorries_without_po():
    lorries = [one(scenario(fwd=False)), one(scenario(returned=True, fwd=False, final=False))]
    assert H.backfill_banners(lorries) == [(
        "info", "2 lorries have no transfer PO yet: transferred before transfer POs "
                "existed, or while they were switched off. The one-time backfill creates "
                "them after you approve its dry-run list — nothing to do on this screen.")]
    assert H.backfill_banners([one(scenario())]) == []
    assert H.backfill_banners([]) == []


def test_a_pilot_on_the_oldest_lorry_raises_no_alarm_on_the_others():
    """The pilot backfills the OLDEST lorry first (Empire GE 1 of 05-08-2026);
    every newer lorry still waiting for the backfill must stay a plain info
    line, never an error, and 'Needs attention' stays 0 unless the pilot's
    own POs are wrong -- the screen used to say 'need attention (... PO
    missing)' and MISSING beside a banner saying 'nothing to do'."""
    pilot = scenario(root=101, hop=201, fwd_po=301, fwd_no=1, ge_no=1,
                     ge_date=date(2026, 8, 5), returned=True)
    waiting_1 = scenario(root=102, hop=202, ge_no=2, fwd=False)
    waiting_2 = scenario(root=103, hop=203, ge_no=3, fwd=False)
    merged = {k: pilot[k] + waiting_1[k] + waiting_2[k]
              for k in ("hops", "transfer_pos", "mr_lines", "po_lines", "siblings")}
    lorries = H.build_lorries(today=TODAY, **merged)
    assert [(l["lorry"], l["status"], l["fwd_po"], l["final_po"]) for l in lorries] == [
        ("GE 3 · 31 Aug", "No PO yet", "not yet", "awaiting"),
        ("GE 2 · 31 Aug", "No PO yet", "not yet", "awaiting"),
        ("GE 1 · 5 Aug", "Completed", "JTSPL 1", "EJM 63"),
    ]
    banners = H.backfill_banners(lorries)
    assert [kind for kind, _ in banners] == ["info"]
    assert banners[0][1].startswith("2 lorries have no transfer PO yet")
    assert H.summary_line(H.lorry_totals(lorries)) == (
        "3 lorries · 1 forwarding PO · 1 final PO · 2 awaiting return · "
        "2 without PO yet · 0 need attention")
    assert [l for l in lorries if H.matches_show(l, "Needs attention")] == []
    assert "MISSING" not in H.lorries_frame(lorries).to_string()


def test_orphan_transfer_pos():
    tpos = scenario(returned=True)["transfer_pos"]
    assert H.orphan_transfer_pos(tpos, [ROOT], [FWD_PO]) == []
    orphans = H.orphan_transfer_pos(tpos, [], [])
    assert [(o["Kind"], o["PO No"], o["Made for MR id"]) for o in orphans] == [
        ("Forwarding", "JTSPL/JPO/26-27/00021", HOP), ("Final", "EJM/F/JPO/26-27/00063", ROOT)]


# --- tables and CSV ---------------------------------------------------------------

def test_lorries_frame_compact_and_full():
    lorries = [one(scenario(returned=True))]
    full = H.lorries_frame(lorries)
    assert len(full.columns) == 31 and full.columns[0] == "Lorry"
    assert full.iloc[0]["Lorry"] == "GE 21 · 31 Aug"
    assert full.iloc[0]["MR Kg"] == 10753 and str(full["MR Kg"].dtype) == "Int64"
    assert full.iloc[0]["Final Diff"] == 4454 and full.iloc[0]["Fwd Rate"] == "12,650–12,850"
    compact = H.lorries_frame(lorries, full=False)
    assert list(compact.columns) == ["Lorry", "Orig PO", "Fwd PO", "Final PO", "Status",
                                     "Supplier", "MR Kg"]
    # no PO yet: empty text cells become None so the table shows its placeholder
    missing = H.lorries_frame([one(scenario(fwd=False))]).iloc[0]
    assert missing["Fwd Rate"] is None and missing["Invoice"] is None
    assert pd.isna(missing["Fwd Kg"]) and missing["Fwd PO"] == "not yet"


def test_lorry_csv_is_unformatted_with_full_numbers_ids_and_reason():
    frame = H.lorries_csv_frame([one(scenario(returned=True)), one(scenario(fwd=False))])
    assert len(frame.columns) == 40
    assert list(frame.columns[-9:]) == ["Orig PO No", "Fwd PO No", "Final PO No", "Root MR Id",
                                        "Hop MR Id", "Orig PO Id", "Fwd PO Id", "Final PO Id",
                                        "Status Reason"]
    done, missing = frame.to_dict("records")
    assert (done["Orig PO No"], done["Fwd PO No"], done["Final PO No"]) == (
        "EJM/F/JPO/26-27/00011", "JTSPL/JPO/26-27/00021", "EJM/F/JPO/26-27/00063")
    assert (done["Root MR Id"], done["Hop MR Id"], done["Fwd PO Id"]) == (ROOT, str(HOP), str(FWD_PO))
    assert done["Fwd Rate"] == "12650-12850" and done["MR Amt"] == 1378878.5
    assert done["GE Date"] == date(2026, 8, 31)
    assert (missing["Fwd PO"], missing["Fwd PO No"], missing["Status Reason"]) == (
        "not yet", "", "Forwarding PO not created yet")


def test_line_detail_csv_one_row_per_po_line():
    frame = H.line_detail_frame([one(scenario(returned=True)), one(scenario(fwd=False))])
    assert list(frame.columns) == H.LINE_DETAIL_COLUMNS
    kinds = frame[["GE No", "PO Kind", "PO No", "Quality"]].values.tolist()
    assert kinds == [
        [21, "Forwarding", "JTSPL/JPO/26-27/00021", "TD-5"],
        [21, "Forwarding", "JTSPL/JPO/26-27/00021", "TD-6"],
        [21, "Final", "EJM/F/JPO/26-27/00063", "TD-5"],
        [21, "Final", "EJM/F/JPO/26-27/00063", "TD-6"],
        [21, "Forwarding", "", "TD-5"],             # PO not created yet: MR side only
        [21, "Forwarding", "", "TD-6"],
    ]
    final_rows = frame[frame["PO Kind"] == "Final"]
    assert round(final_rows["Diff"].sum(), 2) == round(
        final_rows["Whole Unit Diff"].sum() + final_rows["Rate Rounding Diff"].sum(), 2)
    assert frame["Unmatched Diff"].isna().all()       # every line paired


def test_csv_file_names():
    assert H.csv_file_name("EJM", "M:2026-09") == "po_tracker_EJM_2026-09.csv"
    assert H.csv_file_name("", "FY:2026", "lines") == "po_tracker_all_FY26-27_lines.csv"
    assert H.csv_file_name(None, None) == "po_tracker_all_all.csv"
    assert H.csv_file_name("", "M:2026-08", show="All") == "po_tracker_all_2026-08.csv"


def test_csv_file_names_follow_search_forwarder_and_show():
    # a search covers the whole financial year and ignores Show
    assert H.csv_file_name("EJM", "M:2026-09", show="Awaiting return", search="GE 21") == (
        "po_tracker_EJM_FY26-27_search-GE-21.csv")
    assert H.csv_file_name("", "M:2026-09", "lines", fwd_prefix="JTSPL",
                           search="jtspl/jpo/26-27/00021") == (
        "po_tracker_all_FY26-27_fwd-JTSPL_search-jtspl-jpo-26-27-00021_lines.csv")
    assert H.csv_file_name("EJM", "M:2026-08", show="Back at mill") == (
        "po_tracker_EJM_2026-08_back-at-mill.csv")
    assert H.csv_file_name("EJM", "M:2026-08", show="Needs attention", fwd_prefix="GTPL") == (
        "po_tracker_EJM_2026-08_needs-attention_fwd-GTPL.csv")
    # whatever was typed, the name stays plain ASCII and short
    assert H.csv_file_name("", "M:2026-03", search="² / ① ..") == "po_tracker_all_FY25-26_search.csv"
    assert H.csv_file_name("", "FY:2026", search="x" * 500) == (
        "po_tracker_all_FY26-27_search-" + "x" * 40 + ".csv")


def test_grid_key_follows_filters_and_rows():
    a = H.grid_key("pot", ("Lorries", 0, "M:2026-09"), [1, 2])
    assert a == H.grid_key("pot", ("Lorries", 0, "M:2026-09"), [1, 2])
    assert a != H.grid_key("pot", ("Lorries", 0, "M:2026-08"), [1, 2])
    assert a != H.grid_key("pot", ("Lorries", 0, "M:2026-09"), [2, 1])


# --- empty inputs -----------------------------------------------------------------

def test_empty_inputs():
    assert H.records(None) == [] and H.records(pd.DataFrame()) == []
    assert H.build_lorries([], [], [], [], [], TODAY) == []
    assert H.build_lorries(None, None, None, None, None) == []
    assert H.period_options([]) == [] and H.default_period([], TODAY) is None
    assert H.mill_options([]) == [(0, "All mills (0)")]
    assert H.forwarder_options([]) == [(0, "All forwarding companies")]
    assert H.original_po_rows([]) == []
    assert H.orphan_transfer_pos([], [], []) == []
    t = H.lorry_totals([])
    assert H.summary_line(t) == ("0 lorries · 0 forwarding POs · 0 final POs · "
                                 "0 awaiting return · 0 need attention")
    assert H.totals_line(t) == "Total 0 lorries: MR 0 kg, amount 0 · no Fwd PO · no Final PO"
    assert H.lorries_frame([]).shape == (0, 31)
    assert H.lorries_frame([], full=False).shape == (0, 7)
    assert H.lorries_csv_frame([]).shape == (0, 40)
    assert H.line_detail_frame([]).shape == (0, len(H.LINE_DETAIL_COLUMNS))
    assert H.original_pos_frame([]).shape == (0, len(H.PO_COLUMNS))
    assert H.recon_table([]).shape == (0, 6)
    assert H.sibling_table([], {}).shape == (0, 7)


def test_records_turns_nan_and_nat_into_none():
    df = pd.DataFrame({"a": [1.0, float("nan")], "d": [pd.Timestamp("2026-09-01"), pd.NaT]})
    assert H.records(df) == [{"a": 1.0, "d": pd.Timestamp("2026-09-01")}, {"a": None, "d": None}]


# --- the page itself, headless on these fixtures (no database) ----------------------

def _page_script():
    from src.jutetransfer.pages import po_tracker
    po_tracker.po_tracker_page()


def _page_frames():
    """(index, data) as po_queries returns them, for three lorries: the pilot
    (GE 1, 5 Aug, all three POs), GE 21 without PO yet and GE 22 awaiting
    return with its Forwarding PO."""
    lorries = [
        scenario(root=101, hop=201, fwd_po=301, fwd_no=1, ge_no=1, ge_date=date(2026, 8, 5),
                 returned=True),
        scenario(root=102, hop=202, ge_no=21, ge_date=date(2026, 9, 23), fwd=False),
        scenario(root=103, hop=203, ge_no=22, ge_date=date(2026, 9, 24), fwd_po=303, fwd_no=3),
    ]
    merged = {k: [row for s in lorries for row in s[k]]
              for k in ("hops", "transfer_pos", "mr_lines", "po_lines", "siblings")}
    chains = pd.DataFrame([{k: h[k] for k in ("hop_mr_id", "root_mr_id", "fwd_po_id", "lorry_date",
                                               "mill_co_id", "mill_prefix", "mill_name")}
                           for h in merged["hops"]])
    return {"chains": chains, "today": TODAY}, {k: pd.DataFrame(v) for k, v in merged.items()}


def test_page_renders_every_chip_view_and_search(monkeypatch):
    """Every Show chip, both views, a lorry's drill-down and the searches of
    the review (incl. '²', which crashed the page) -- none may raise; the
    'Nothing found' text is escaped and the CSV names follow the filters."""
    from streamlit.testing.v1 import AppTest
    from src.jutetransfer.pages import po_tracker as page

    index, data = _page_frames()
    monkeypatch.setattr(page, "get_tracker_index", lambda: index)
    monkeypatch.setattr(page, "load_tracker_data", lambda fy: data)
    names, real_name = [], H.csv_file_name

    def csv_name(*args, **kwargs):
        names.append(real_name(*args, **kwargs))
        return names[-1]

    monkeypatch.setattr(H, "csv_file_name", csv_name)
    at = AppTest.from_function(_page_script, default_timeout=60)

    def run():
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        return ([str(e.value) for e in at.markdown], [str(e.value) for e in at.info],
                [len(df.value) for df in at.dataframe])

    markdown, info, _rows = run()                   # default period: Sep 2026
    assert ("2 lorries · 1 forwarding PO · 0 final POs · 2 awaiting return · "
            "1 without PO yet · 0 need attention") in markdown
    assert [t for t in info if t.startswith("1 lorry has no transfer PO yet")]
    assert names[-2:] == ["po_tracker_all_2026-09.csv", "po_tracker_all_2026-09_lines.csv"]
    # the pilot's POs are in August: the screen says so instead of '0 final POs' alone
    assert ("Transfer POs so far in FY 26-27: 2 forwarding POs, 1 final PO — 2 are in other "
            "months; pick 'FY 26-27 – all' under Period to see them.") in [
                str(c.value) for c in at.caption]
    # the four pinned / fixed columns fit a 390 px phone beside the selection box
    widths = {c: cfg.get("width") for c, cfg in
              page._lorry_column_config().items() if c in ("Lorry", "Orig PO", "Fwd PO", "Final PO")}
    assert widths == {"Lorry": 100, "Orig PO": 70, "Fwd PO": 78, "Final PO": 78}
    assert sum(widths.values()) + 32 <= 358

    for chip in H.SHOW_OPTIONS:
        at.session_state["pot_show"] = chip
        run()
    assert names[-1] == "po_tracker_all_2026-09_needs-attention_lines.csv"
    at.session_state["pot_show"] = "All"

    for text, found in (("GE 21", 1), ("GE 1", 1), ("6522", 3), ("²", 0), ("①", 0),
                        ("**x**", 0)):
        at.text_input(key="pot_search").input(text)
        markdown, info, rows = run()
        if found:
            assert rows[0] == found, text
        else:
            assert H.nothing_found_text(text, 2026) in info, (text, info)
    assert r"Nothing found for '\*\*x\*\*' in FY 26-27." in info
    at.text_input(key="pot_search").input("GE 21")
    run()
    assert names[-2:] == ["po_tracker_all_FY26-27_search-GE-21.csv",
                          "po_tracker_all_FY26-27_search-GE-21_lines.csv"]

    at.text_input(key="pot_search").input("")
    at.session_state["pot_fwd"] = 74                  # JTSPL
    run()
    assert names[-1] == "po_tracker_all_2026-09_fwd-JTSPL_lines.csv"

    # the pilot's drill-down: both difference sentences, worded as fixed
    at.session_state["pot_fwd"] = 0
    at.session_state["pot_period"] = "M:2026-08"
    at.session_state["pot_sel_root"] = 101
    markdown, _info, _rows = run()
    assert ("PO 1,384,800 vs MR 1,378,878: +5,922 (+0.43 %) — whole bales +5,922 · "
            "rate unchanged.") in markdown
    assert ("PO 1,390,200 vs invoice 1,385,746: +4,454 (+0.32 %) — whole bales +5,945 · "
            "rate to nearest ₹50: -1,491.") in markdown

    at.session_state["pot_view"] = "Original POs"
    for chip in H.SHOW_OPTIONS:
        at.session_state["pot_show"] = chip
        run()
