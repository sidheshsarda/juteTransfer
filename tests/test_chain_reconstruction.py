"""Tests for chain reconstruction algorithm."""
from src.jutetransfer.pages.jute_mr import _reconstruct_chain


def _make_chain_mrs(entries):
    """Helper: create list of MR dicts from (jute_mr_id, src_com_id, owner_co_id, branch_id) tuples."""
    return [
        {"jute_mr_id": mid, "src_com_id": src, "owner_co_id": owner, "branch_id": bid}
        for mid, src, owner, bid in entries
    ]


def test_simple_chain_a_b_a():
    """A→B→A: one transferred MR."""
    mrs = _make_chain_mrs([
        (222, 1, 2, 20),  # MR at B, received from A (co_id=1)
    ])
    root_co_id = 1  # Company A
    ordered = _reconstruct_chain(mrs, root_co_id)
    assert [m["jute_mr_id"] for m in ordered] == [222]


def test_chain_a_b_c_a():
    """A→B→C→A: two transferred MRs."""
    mrs = _make_chain_mrs([
        (222, 1, 2, 20),  # MR at B, received from A
        (333, 2, 3, 30),  # MR at C, received from B
    ])
    ordered = _reconstruct_chain(mrs, root_co_id=1)
    assert [m["jute_mr_id"] for m in ordered] == [222, 333]


def test_chain_a_b_c_b_a():
    """A→B→C→B→A: repeated company, disambiguated by jute_mr_id."""
    mrs = _make_chain_mrs([
        (222, 1, 2, 20),  # B, from A
        (333, 2, 3, 30),  # C, from B
        (444, 3, 2, 21),  # B again, from C (different branch)
    ])
    ordered = _reconstruct_chain(mrs, root_co_id=1)
    assert [m["jute_mr_id"] for m in ordered] == [222, 333, 444]


def test_chain_with_ambiguous_sender():
    """A→B→C→B→D→A: B sends twice (to C and to D)."""
    mrs = _make_chain_mrs([
        (222, 1, 2, 20),  # B, from A
        (333, 2, 3, 30),  # C, from B (first send)
        (444, 3, 2, 21),  # B again, from C
        (555, 2, 4, 40),  # D, from B (second send)
    ])
    ordered = _reconstruct_chain(mrs, root_co_id=1)
    assert [m["jute_mr_id"] for m in ordered] == [222, 333, 444, 555]


def test_empty_chain():
    """No transferred MRs."""
    ordered = _reconstruct_chain([], root_co_id=1)
    assert ordered == []
