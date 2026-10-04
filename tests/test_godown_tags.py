"""'Save godown tags' must only touch the godowns whose tag changes, and an
untag must give the godown back its jute type -- it used to write NULL into
warehouse_mst.warehouse_type (ERP master data) for every godown of the
branch (104 godowns blanked in 7 branches by 2026-10-01)."""
from unittest.mock import patch

import pandas as pd

from src.jutetransfer import queries


def _capture(fn, *args):
    seen = []

    def fake(sql, params=None):
        seen.append((" ".join(str(sql).split()), dict(params or {})))
        return 1

    with patch.object(queries.DatabaseConnection, "execute_non_query", side_effect=fake):
        fn(*args)
    return seen


def test_tagging_never_blanks_a_godown_and_skips_already_marked_ones():
    (sql, params), = _capture(queries.set_warehouse_marked, 206, True)
    assert "SET warehouse_type = :t" in sql and "NOT (warehouse_type <=> :t)" in sql
    assert params == {"t": "MARKED", "id": 206}


def test_untagging_only_touches_a_marked_godown_and_restores_jute():
    (sql, params), = _capture(queries.set_warehouse_marked, 206, False)
    assert "SET warehouse_type = :j" in sql and "warehouse_type = :m" in sql
    assert params == {"j": "J", "m": "MARKED", "id": 206}
    assert None not in params.values()


def test_only_jute_marked_or_untyped_godowns_are_offered_for_tagging():
    """Untagging gives a godown the jute type 'J' back, so a store ('S') or
    any other typed godown ('D') is never offered. The options are keyed by
    godown id (the pages show the label), not by name."""
    frame = pd.DataFrame({
        "warehouse_id": [1, 2, 3, 4, 5, 6, 7],
        "warehouse_name": ["JUTE-1", "STORE-1", "MARKED-1", "WIPED-1", "BLANK-1", "D-1",
                           "OTHER-BRANCH"],
        "warehouse_type": ["J", "S", "MARKED", None, " ", "D", "J"],
        "branch_id": [33, 33, 33, 33, 33, 33, 20],
    })
    with patch.object(queries, "load_warehouses", return_value=frame):
        options = queries.get_markable_warehouses_by_branch(33)
    assert options == {1: "JUTE-1", 3: "MARKED-1", 4: "WIPED-1", 5: "BLANK-1"}


def test_godown_types_compare_like_mysql_and_same_names_stay_apart():
    """sls branch 87: two godowns called LCPL_JUTE (244, 360) -- a name-keyed
    map kept one; two typed 'j' (299, 359) -- MySQL reads 'j' as 'J', the
    case-sensitive filter did not. Both are offered now, told apart by id."""
    frame = pd.DataFrame({
        "warehouse_id": [244, 360, 299, 359, 400, 401],
        "warehouse_name": ["LCPL_JUTE", "LCPL_JUTE", "MBGCPL_JUTE-2", "MBGCPL_JUTE-2",
                           "LCPL_MARKED", "LCPL_STORE"],
        "warehouse_type": ["J", "MARKED", "j", "j ", "marked", "s"],
        "branch_id": [87] * 6,
    })
    with patch.object(queries, "load_warehouses", return_value=frame):
        markable = queries.get_markable_warehouses_by_branch(87)
        marked = queries.get_marked_warehouses_by_branch(87)
        every = queries.get_warehouses_by_branch(87)
    assert markable == {244: "LCPL_JUTE (#244)", 360: "LCPL_JUTE (#360)",
                        299: "MBGCPL_JUTE-2 (#299)", 359: "MBGCPL_JUTE-2 (#359)",
                        400: "LCPL_MARKED"}
    assert marked == {360: "LCPL_JUTE (#360)", 400: "LCPL_MARKED"}
    assert set(every) == {244, 360, 299, 359, 400, 401} and every[401] == "LCPL_STORE"
