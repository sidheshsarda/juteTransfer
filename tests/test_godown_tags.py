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
    any other typed godown ('D') is never offered."""
    frame = pd.DataFrame({
        "warehouse_id": [1, 2, 3, 4, 5, 6, 7],
        "warehouse_name": ["JUTE-1", "STORE-1", "MARKED-1", "WIPED-1", "BLANK-1", "D-1",
                           "OTHER-BRANCH"],
        "warehouse_type": ["J", "S", "MARKED", None, " ", "D", "J"],
        "branch_id": [33, 33, 33, 33, 33, 33, 20],
    })
    with patch.object(queries, "load_warehouses", return_value=frame):
        options = queries.get_markable_warehouses_by_branch(33)
    assert options == {"JUTE-1": 1, "MARKED-1": 3, "WIPED-1": 4, "BLANK-1": 5}
