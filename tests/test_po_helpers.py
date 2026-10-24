"""Pure math behind transfer POs: rate rounded to the closest 50, the quantity
of one lorry of the Original PO's lorry type split over the MR's lines by
received weight (percentage mode) -- or, without a lorry type, whole bales /
loose units nearest the received weight -- value, provenance marker, printed
PO number."""
from datetime import date, datetime
from decimal import Decimal

import pytest

from src.jutetransfer.po_helpers import (
    MARKA_MAX_LEN,
    ROLE_FINAL,
    ROLE_FORWARD,
    build_marker,
    build_po_lines,
    equivalent_units,
    final_marker_like,
    format_po_no,
    fy_label,
    lorry_kg,
    normalise_uom,
    parse_marker,
    po_totals,
    round_rate,
    split_percentages,
    unit_kg,
)


# --- rate rounded to the closest 50 (owner rule 2026-10-01) -----------------

@pytest.mark.parametrize("rate, expected", [
    (12914, 12900),        # live finalized root 28137, line 1
    (12713, 12700),        # live finalized root 28137, line 2
    (11861, 11850),        # live LCPL PO rate that is off the 50 grid
    (13351, 13350),
    (12850, 12850),        # already on the grid: untouched
    (12925, 12950),        # tie goes up
    (12975, 13000),
    (12924.99, 12900),
    (12924.999999, 12900),
    (12925.0, 12950),
    (25, 50),
    (24.99, 0),
    (Decimal("12925"), 12950),
    ("12914", 12900),
])
def test_round_rate_to_closest_50(rate, expected):
    assert round_rate(rate) == expected


@pytest.mark.parametrize("rate", [None, 0, 0.0, float("nan"), "", "abc"])
def test_round_rate_missing_is_zero(rate):
    assert round_rate(rate) == 0.0


def test_round_rate_other_step():
    assert round_rate(12914, step=100) == 12900
    assert round_rate(12950, step=100) == 13000


# --- units ------------------------------------------------------------------

def test_uom_normalisation_and_unit_kg():
    assert normalise_uom(" bale ") == "BALE"
    assert normalise_uom(None, "loose") == "LOOSE"
    assert normalise_uom(None, "130", "BALE") == "BALE"   # legacy '130' skipped
    assert normalise_uom(None, float("nan")) == "LOOSE"   # nothing usable
    assert unit_kg("BALE") == 150
    assert unit_kg("LOOSE") == 48
    assert unit_kg(None) == 48


@pytest.mark.parametrize("kg, unit, expected", [
    (9312, 150, 62),     # 62.08
    (1441, 150, 10),     # 9.6
    (4504, 48, 94),      # 93.83
    (225, 150, 2),       # 1.5 -> half up
    (74, 150, 1),        # never 0
    (0, 150, 1),
    (None, 48, 1),
])
def test_equivalent_units(kg, unit, expected):
    assert equivalent_units(kg, unit) == expected


# --- PO lines from an MR ----------------------------------------------------

def _hop_28171_lines():
    """Live hop MR 28171 (Jagrati): two soft-deleted lines + two real ones."""
    return [
        {"jute_mr_li_id": 45567, "actual_item_id": 610, "challan_item_id": 610,
         "accepted_weight": 0.0, "rate": 0.0, "active": 0,
         "marka": None, "crop_year": None, "allowable_moisture": 20.0},
        {"jute_mr_li_id": 45568, "actual_item_id": 612, "challan_item_id": 612,
         "accepted_weight": 0.0, "rate": 0.0, "active": 0,
         "marka": None, "crop_year": None, "allowable_moisture": 20.0},
        {"jute_mr_li_id": 45570, "actual_item_id": 612, "challan_item_id": 612,
         "accepted_weight": 1441.0, "rate": 12650.0, "active": 1,
         "marka": None, "crop_year": None, "allowable_moisture": 20.0},
        {"jute_mr_li_id": 45569, "actual_item_id": 610, "challan_item_id": 610,
         "accepted_weight": 9312.0, "rate": 12850.0, "active": 1,
         "marka": None, "crop_year": None, "allowable_moisture": 20.0},
    ]


# --- one lorry of the Original PO's lorry type (owner rule 2026-10-24) ------

@pytest.mark.parametrize("capacity, expected", [
    (100, 10000.0), (160.0, 16000.0), (Decimal("120"), 12000.0), ("140", 14000.0),
    (100.5, 10050.0),
])
def test_lorry_kg_is_the_capacity_in_quintals_times_100(capacity, expected):
    assert lorry_kg(capacity) == expected


@pytest.mark.parametrize("capacity", [None, 0, 0.0, -5, float("nan"), "", "abc"])
def test_lorry_kg_missing_capacity_is_none(capacity):
    assert lorry_kg(capacity) is None


@pytest.mark.parametrize("weights, expected", [
    ([9312, 1441], ["86.60", "13.40"]),                 # live GE 21: 86.599 / 13.401
    ([9312, 1441, 700], ["81.31", "12.58", "6.11"]),    # 81.306 / 12.582 / 6.112
    ([1, 1, 1], ["33.34", "33.33", "33.33"]),           # leftover to the first of equals
    ([2, 1, 1], ["50.00", "25.00", "25.00"]),
    ([6250, 6250], ["50.00", "50.00"]),
    ([100, 0, 50], ["66.67", "0.00", "33.33"]),         # a zero line keeps its place
    ([5000], ["100.00"]),
])
def test_split_percentages_two_decimals_summing_to_100(weights, expected):
    pcts = split_percentages(weights)
    assert [str(p) for p in pcts] == expected
    assert sum(pcts) == Decimal("100.00")


def test_split_percentages_nothing_to_split():
    assert split_percentages([]) == []
    assert split_percentages([0, None, float("nan")]) == []


def test_lorry_shares_percentage_mode_from_live_hop():
    """GE 21 (10,753 kg received) on a 100-quintal lorry type: the PO orders
    10,000 kg in the lines' shares; units are the ERP's informational count."""
    lines = build_po_lines(_hop_28171_lines(), "BALE", default_crop_year=26, lorry_kg=10000)
    assert [l["jute_mr_li_id"] for l in lines] == [45569, 45570]
    assert [l["percentage"] for l in lines] == [86.6, 13.4]
    assert [l["line_kg"] for l in lines] == [8660.0, 1340.0]
    assert [l["quantity"] for l in lines] == [58.0, 9.0]          # 57.73 / 8.93 bales
    assert [l["accepted_weight"] for l in lines] == [9312.0, 1441.0]   # the MR untouched
    assert [l["rate"] for l in lines] == [12850.0, 12650.0]
    assert [l["value"] for l in lines] == [1112810.0, 169510.0]  # 86.6 q / 13.4 q
    assert po_totals(lines) == (10000.0, 1282320.0)


def test_lorry_shares_loose_units_and_odd_capacities():
    lines = build_po_lines(_hop_28171_lines(), "LOOSE", lorry_kg=12000)
    assert [l["line_kg"] for l in lines] == [10392.0, 1608.0]
    assert [l["quantity"] for l in lines] == [217.0, 34.0]       # 216.5 up / 33.5 up
    assert po_totals(lines)[0] == 12000.0
    lines = build_po_lines(_hop_28171_lines(), "BALE", lorry_kg=10050)   # 100.5 quintals
    assert [l["line_kg"] for l in lines] == [8703.3, 1346.7]
    assert po_totals(lines)[0] == 10050.0


def test_lorry_shares_add_up_to_an_odd_lorry_to_the_gram():
    """100.5 quintals over three equal lines: 33.34 / 33.33 / 33.33 % give
    3,350.67 / 3,349.665 / 3,349.665 kg -- exact, so the header is the lorry."""
    mr = [{"jute_mr_li_id": i, "actual_item_id": 1, "accepted_weight": 700, "rate": 13000,
           "active": 1} for i in (1, 2, 3)]
    lines = build_po_lines(mr, "BALE", lorry_kg=10050)
    assert [l["line_kg"] for l in lines] == [3350.67, 3349.665, 3349.665]
    assert po_totals(lines)[0] == 10050.0
    assert [l["value"] for l in lines] == [435587.1, 435456.45, 435456.45]


def test_lorry_shares_a_share_of_0_percent_has_no_units():
    """As the ERP derives it (round(kg / unit)): a line too light to reach
    0.01 % keeps its place with 0.00 %, 0 kg, 0 units, 0 value -- never the
    one-unit floor of quantity mode."""
    mr = [{"jute_mr_li_id": 1, "actual_item_id": 1, "accepted_weight": 100, "rate": 13000, "active": 1},
          {"jute_mr_li_id": 2, "actual_item_id": 2, "accepted_weight": 0.01, "rate": 13000, "active": 1},
          {"jute_mr_li_id": 3, "actual_item_id": 3, "accepted_weight": 5000, "rate": 13000, "active": 1}]
    lines = build_po_lines(mr, "BALE", lorry_kg=10000)
    assert [(l["percentage"], l["line_kg"], l["quantity"], l["value"]) for l in lines] == [
        (1.96, 196.0, 1, 25480.0), (0.0, 0.0, 0, 0.0), (98.04, 9804.0, 65, 1274520.0)]
    assert sum(l["percentage"] for l in lines) == 100.0
    # a 60 kg share is 0 bales in the ERP's count too (0.4 rounds down)
    light = build_po_lines([mr[0], dict(mr[2], accepted_weight=100 * 9940 / 60)], "BALE",
                           lorry_kg=10000)
    assert light[0]["line_kg"] == 60.0 and light[0]["quantity"] == 0


def test_lorry_shares_value_follows_the_line_kg_not_the_units():
    """As the ERP's percentage mode: value = percentage weight / 100 x rate;
    quantity x 150 would give another figure (8,700 / 1,350 kg)."""
    for l in build_po_lines(_hop_28171_lines(), "BALE", lorry_kg=10000):
        assert l["value"] == float(
            (Decimal(str(l["line_kg"])) / 100 * Decimal(str(l["rate"]))).quantize(Decimal("0.01")))
        assert l["line_kg"] != l["quantity"] * 150


def test_lorry_shares_skip_the_same_lines_as_quantity_mode():
    mr_lines = _hop_28171_lines() + [
        {"jute_mr_li_id": 45571, "actual_item_id": 5, "accepted_weight": 0, "rate": 100, "active": 1},
        {"jute_mr_li_id": 45572, "actual_item_id": 5, "accepted_weight": 500, "rate": 100, "active": 0},
    ]
    lines = build_po_lines(mr_lines, "BALE", lorry_kg=10000)
    assert [l["jute_mr_li_id"] for l in lines] == [45569, 45570]
    assert sum(l["percentage"] for l in lines) == 100.0


@pytest.mark.parametrize("no_lorry", [None, 0, 0.0, float("nan")])
def test_without_a_lorry_the_quantity_mode_rule_applies(no_lorry):
    lines = build_po_lines(_hop_28171_lines(), "BALE", lorry_kg=no_lorry)
    assert [l["percentage"] for l in lines] == [None, None]
    assert [l["quantity"] for l in lines] == [62.0, 10.0]
    assert po_totals(lines) == (10800.0, 1384800.0)


# --- quantity mode (no Original PO lorry type) ------------------------------

def test_forward_po_lines_from_live_hop():
    lines = build_po_lines(_hop_28171_lines(), "BALE", default_crop_year=26)
    assert [l["jute_mr_li_id"] for l in lines] == [45569, 45570]  # id order
    assert [l["item_id"] for l in lines] == [610, 612]
    assert [l["quantity"] for l in lines] == [62.0, 10.0]
    assert [l["line_kg"] for l in lines] == [9300.0, 1500.0]
    assert [l["rate"] for l in lines] == [12850.0, 12650.0]       # on the grid
    assert [l["value"] for l in lines] == [1195050.0, 189750.0]
    assert [l["crop_year"] for l in lines] == [26, 26]            # PO fallback
    assert po_totals(lines) == (10800.0, 1384800.0)


def test_final_po_lines_round_the_marked_up_rate():
    """Finalized root 28137: 12850 / 12650 marked up 0.5 % -> 12914 / 12713."""
    mr_lines = [
        {"jute_mr_li_id": 45537, "actual_item_id": 56, "accepted_weight": 9312.0,
         "rate": 12914.0, "active": 1, "allowable_moisture": 20.0},
        {"jute_mr_li_id": 45538, "actual_item_id": 57, "accepted_weight": 1441.0,
         "rate": 12713.0, "active": 1, "allowable_moisture": 20.0},
    ]
    lines = build_po_lines(mr_lines, "BALE")
    assert [l["rate"] for l in lines] == [12900.0, 12700.0]
    assert [l["mr_rate"] for l in lines] == [12914.0, 12713.0]    # MR untouched
    assert [l["value"] for l in lines] == [1199700.0, 190500.0]
    assert po_totals(lines) == (10800.0, 1390200.0)


def test_loose_lines_use_48_kg_units():
    lines = build_po_lines(
        [{"jute_mr_li_id": 1, "actual_item_id": 616, "accepted_weight": 4504,
          "rate": 11750, "active": None}],
        "LOOSE",
    )
    assert lines[0]["quantity"] == 94.0
    assert lines[0]["line_kg"] == 4512.0
    assert lines[0]["value"] == 530160.0          # 4512 / 100 * 11750


def test_stored_weight_and_value_follow_the_erp_quantity_formula():
    """ERP legacy mode: line kg = quantity x 150|48, value = kg / 100 x rate.
    The stored figures must be exactly that, or the PO page and PO list of
    the same PO would disagree."""
    lines = build_po_lines(_hop_28171_lines(), "BALE")
    for l in lines:
        assert l["line_kg"] == l["quantity"] * 150
        assert l["value"] == round(l["line_kg"] / 100 * l["rate"], 2)


def test_lines_without_weight_or_inactive_are_skipped():
    mr_lines = [
        {"jute_mr_li_id": 1, "actual_item_id": 5, "accepted_weight": 0, "rate": 100, "active": 1},
        {"jute_mr_li_id": 2, "actual_item_id": 5, "accepted_weight": None, "rate": 100, "active": 1},
        {"jute_mr_li_id": 3, "actual_item_id": 5, "accepted_weight": float("nan"), "rate": 100, "active": 1},
        {"jute_mr_li_id": 4, "actual_item_id": 5, "accepted_weight": 500, "rate": 100, "active": 0},
        {"jute_mr_li_id": 5, "actual_item_id": 5, "accepted_weight": -3, "rate": 100, "active": 1},
    ]
    assert build_po_lines(mr_lines, "BALE") == []
    assert po_totals([]) == (0.0, 0.0)


def test_item_falls_back_to_challan_item_and_may_be_missing():
    lines = build_po_lines([
        {"jute_mr_li_id": 1, "actual_item_id": None, "challan_item_id": 77,
         "accepted_weight": 300, "rate": 10000, "active": 1},
        {"jute_mr_li_id": 2, "actual_item_id": float("nan"), "challan_item_id": None,
         "accepted_weight": 300, "rate": 10000, "active": 1},
    ], "BALE")
    assert [l["item_id"] for l in lines] == [77, None]


def test_marka_is_cut_to_the_po_column_width_and_line_values_kept():
    long_marka = "M" * 80
    lines = build_po_lines([
        {"jute_mr_li_id": 9, "actual_item_id": 1, "accepted_weight": 1500,
         "rate": 13366, "active": 1, "marka": long_marka, "crop_year": 25,
         "allowable_moisture": 18},
    ], "BALE", default_crop_year=26)
    l = lines[0]
    assert l["marka"] == "M" * MARKA_MAX_LEN
    assert l["crop_year"] == 25                 # the line's own wins
    assert l["allowable_moisture"] == 18.0
    assert l["rate"] == 13350.0
    assert l["accepted_weight"] == 1500.0


def test_zero_rate_line_is_kept_with_zero_value():
    lines = build_po_lines([
        {"jute_mr_li_id": 1, "actual_item_id": 1, "accepted_weight": 1500,
         "rate": None, "active": 1},
    ], "BALE")
    assert lines[0]["rate"] == 0.0 and lines[0]["value"] == 0.0


# --- provenance marker ------------------------------------------------------

def test_marker_roundtrip():
    m = build_marker(ROLE_FORWARD, 28137, 28171, 13314)
    assert m == "JT|FORWARD|root=28137|mr=28171|srcpo=13314|"
    assert parse_marker(m) == {
        "role": "FORWARD", "root_mr_id": 28137, "mr_id": 28171, "src_po_id": 13314,
        "original": None,
    }
    f = build_marker(ROLE_FINAL, 28137, 28137, None)
    assert f == "JT|FINAL|root=28137|mr=28137|srcpo=0|"
    assert parse_marker(f)["src_po_id"] is None
    assert len(m) <= 500


def test_marker_survives_appended_text_and_rejects_everything_else():
    m = build_marker(ROLE_FORWARD, 1, 2, 3) + " Rejected: wrong supplier"
    assert parse_marker(m)["mr_id"] == 2
    for junk in (None, "", "Rejected: x", "JT|OTHER|root=1|mr=2|srcpo=3|",
                 "xJT|FORWARD|root=1|mr=2|srcpo=3|", 12345, float("nan")):
        assert parse_marker(junk) is None
    with pytest.raises(ValueError):
        build_marker("COPY", 1, 2, 3)


def test_final_marker_pattern_cannot_match_a_longer_root_id():
    pattern = final_marker_like(281)
    assert pattern == "JT|FINAL|root=281|%"
    prefix = pattern[:-1]
    assert build_marker(ROLE_FINAL, 281, 281, 5).startswith(prefix)
    assert not build_marker(ROLE_FINAL, 28137, 28137, 5).startswith(prefix)
    assert not build_marker(ROLE_FORWARD, 281, 300, 5).startswith(prefix)
    assert "_" not in prefix and "%" not in prefix       # no LIKE wildcards


# --- printed PO number ------------------------------------------------------

def test_format_po_no_matches_the_erp_layout():
    assert format_po_no(21, "JTSPL", None, date(2026, 8, 31)) == "JTSPL/JPO/26-27/00021"
    assert format_po_no(11, "EJM", "FAC", date(2026, 8, 26)) == "EJM/FAC/JPO/26-27/00011"
    assert format_po_no(71, "LCPL", "  ", date(2027, 1, 5)) == "LCPL/JPO/26-27/00071"
    assert format_po_no(3.0, "X", "", datetime(2026, 4, 1, 9, 30)) == "X/JPO/26-27/00003"
    assert format_po_no(123456, None, None, date(2026, 4, 1)) == "JPO/26-27/123456"


def test_format_po_no_blank_when_number_or_date_missing():
    assert format_po_no(None, "X", "Y", date(2026, 4, 1)) == ""
    assert format_po_no(0, "X", "Y", date(2026, 4, 1)) == ""
    assert format_po_no(5, "X", "Y", None) == ""
    assert format_po_no(float("nan"), "X", "Y", date(2026, 4, 1)) == ""


def test_fy_label_boundaries():
    assert fy_label(date(2026, 3, 31)) == "25-26"
    assert fy_label(date(2026, 4, 1)) == "26-27"
    assert fy_label(date(2099, 12, 31)) == "99-00"


# --- remark shown in the ERP's Closed panel ---------------------------------

def test_close_remarks_explain_the_po_and_fit_the_column():
    from src.jutetransfer.po_helpers import (
        CLOSE_REMARK_MAX_LEN, final_close_remark, forward_close_remark,
    )
    fwd = forward_close_remark(21, date(2026, 8, 31), "THE EMPIRE JUTE COMPANY LTD.",
                               "EJM/F/JPO/26-27/00011")
    assert fwd == ("Transfer PO (Forwarding). Created by Jute Transfer for lorry "
                   "GE 21 dt 31-08-2026 received at THE EMPIRE JUTE COMPANY LTD. "
                   "Original PO EJM/F/JPO/26-27/00011. Do not reopen.")
    fin = final_close_remark(21.0, datetime(2026, 8, 31, 10, 0),
                             "JAGRATI TRADE SERVICES PVT. LTD.", "EJM/F/JPO/26-27/00011")
    assert fin == ("Transfer PO (Final). Created by Jute Transfer when lorry GE 21 "
                   "dt 31-08-2026 came back from JAGRATI TRADE SERVICES PVT. LTD. "
                   "The lorry was received on original PO EJM/F/JPO/26-27/00011, "
                   "so this PO shows no receipt. Do not reopen.")
    # missing pieces never break the sentence; over-long names are cut to fit
    assert "lorry GE ? dt ?" in forward_close_remark(None, None, None, None)
    assert "Original PO none" in forward_close_remark(None, None, "", "")
    assert len(final_close_remark(1, date(2026, 4, 1), "X" * 900, "P")) == CLOSE_REMARK_MAX_LEN


# --- the Final PO remembers the mill MR's header from before finalize -------

def test_final_marker_remembers_the_original_header():
    original = {"party_id": "207", "party_branch_id": 38544, "jute_mr_date": None}
    f = build_marker(ROLE_FINAL, 28137, 28137, 13314, original=original)
    assert f == "JT|FINAL|root=28137|mr=28137|srcpo=13314|orig=207/38544/|"
    assert parse_marker(f)["original"] == {
        "party_id": "207", "party_branch_id": 38544, "jute_mr_date": None}
    dated = build_marker(ROLE_FINAL, 5, 5, 0, original={
        "party_id": 8646.0, "party_branch_id": None,
        "jute_mr_date": datetime(2026, 9, 3, 0, 0)})
    assert dated == "JT|FINAL|root=5|mr=5|srcpo=0|orig=8646//2026-09-03|"
    assert parse_marker(dated)["original"] == {
        "party_id": "8646", "party_branch_id": None, "jute_mr_date": date(2026, 9, 3)}
    # still found by the FINAL lookup pattern, still short enough
    assert f.startswith(final_marker_like(28137)[:-1]) and len(f) < 100


def test_original_is_only_kept_when_it_can_be_restored_exactly():
    # a non-numeric party cannot be written back from the marker: nothing kept
    assert build_marker(ROLE_FINAL, 1, 1, 2, original={"party_id": "ABC"}) \
        == "JT|FINAL|root=1|mr=1|srcpo=2|"
    assert parse_marker("JT|FINAL|root=1|mr=1|srcpo=2|")["original"] is None
    # Forwarding POs never carry it
    assert build_marker(ROLE_FORWARD, 1, 2, 3, original={"party_id": "9"}) \
        == "JT|FORWARD|root=1|mr=2|srcpo=3|"
    # an empty original keeps all three NULL
    assert parse_marker(build_marker(ROLE_FINAL, 1, 1, 2, original={"party_id": None}))[
        "original"] == {"party_id": None, "party_branch_id": None, "jute_mr_date": None}
    # text appended after the marker does not break it
    txt = build_marker(ROLE_FINAL, 1, 1, 2, original={"party_id": 7}) + " Rejected: x"
    assert parse_marker(txt)["original"]["party_id"] == "7"
