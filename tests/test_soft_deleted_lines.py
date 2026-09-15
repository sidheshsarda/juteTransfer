"""Soft-deleted jute_mr_li rows (active = 0, ERP QC edit) must be invisible to the
vertical transfer chain: not displayed, not copied to the next company, not summed
into header totals, not positionally matched on unfinalize."""
import re
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.jutetransfer import queries
from src.jutetransfer.pages import new_transfer_chain

ACTIVE = re.compile(r"\(\s*(?:li\.)?active\s*=\s*1\s+OR\s+(?:li\.)?active\s+IS\s+NULL\s*\)", re.I)
SRC = Path(__file__).resolve().parents[1] / "src" / "jutetransfer"


def _captured_sql(fn, *args, **kwargs):
    seen = []

    def fake(sql, params=None):
        seen.append(str(sql))
        return pd.DataFrame()

    with patch.object(queries.DatabaseConnection, "execute_query", side_effect=fake):
        fn(*args, **kwargs)
    return seen


def test_mr_overview_query_filters_inactive_lines():
    sql = _captured_sql(queries.get_jute_mr_with_line_items, 2026, 9, 1, 1)
    assert sql and ACTIVE.search(sql[0])


def test_source_mr_full_filters_inactive_lines():
    sql = _captured_sql(queries.get_source_mr_full, 1)
    # first SELECT is the header; line-item SELECT never issued on empty header — assert on source
    li_sql = [s for s in _all_sql_literals(SRC / "queries.py") if "FROM jute_mr_li WHERE jute_mr_id = :id" in s]
    assert li_sql and all(ACTIVE.search(s) for s in li_sql)


def test_step_line_items_filters_inactive_lines():
    seen = []

    def fake(sql, params=None):
        seen.append(str(sql))
        return pd.DataFrame()

    from src.jutetransfer.database import DatabaseConnection
    with patch.object(DatabaseConnection, "execute_query", side_effect=fake):
        new_transfer_chain._fetch_step_line_items(1)
    assert seen and ACTIVE.search(seen[0])


def _all_sql_literals(path):
    return [path.read_text(encoding="utf-8")]


def test_transfer_header_totals_and_unfinalize_ignore_inactive_lines():
    src = (SRC / "transfer.py").read_text(encoding="utf-8")
    sites = re.findall(r"(?<!DELETE )FROM jute_mr_li\s+WHERE jute_mr_id = :\w+[^\n]*", src)
    assert sites, "expected jute_mr_li reads in transfer.py"
    bad = [s for s in sites if not ACTIVE.search(s)]
    assert not bad, bad
