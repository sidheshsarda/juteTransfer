"""supplier_map: admin guard, what save_map sends, empty map without the table.
A recorder stands in for the database."""
from contextlib import contextmanager

import pytest

from src.jutetransfer import access, supplier_map


class _Conn:
    def __init__(self):
        self.calls = []

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params))


@pytest.fixture
def conn(monkeypatch):
    c = _Conn()

    @contextmanager
    def tx():
        yield c
    monkeypatch.setattr(supplier_map.DatabaseConnection, "get_transaction", staticmethod(tx))
    monkeypatch.setattr(supplier_map.access, "get_role", lambda uid: "admin")
    monkeypatch.delenv("JT_LEGACY_LOGIN", raising=False)
    return c


def test_non_admin_is_refused_before_any_database_work(conn, monkeypatch):
    monkeypatch.setattr(supplier_map.access, "get_role", lambda uid: "user")
    with pytest.raises(supplier_map.access.NotAllowed):
        supplier_map.save_map({1: 5}, 7)
    assert conn.calls == []


def test_upsert_null_and_clear(conn):
    n = supplier_map.save_map({10: 5, 11: None, 12: supplier_map.CLEAR}, 7)
    assert n == 3
    (s1, p1), (s2, p2), (s3, p3) = conn.calls
    assert "INSERT INTO jt_party_supplier_map" in s1 and p1 == {"p": 10, "s": 5, "u": 7}
    assert "INSERT" in s2 and p2 == {"p": 11, "s": None, "u": 7}
    assert s3.startswith("DELETE") and p3 == {"p": 12}


def test_nothing_to_save_does_nothing(conn):
    assert supplier_map.save_map({}, 7) == 0
    assert conn.calls == []


def test_load_map_empty_without_the_table(monkeypatch):
    class Bad:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            raise RuntimeError("no such table")

    class Eng:
        def connect(self):
            return Bad()
    monkeypatch.setattr(supplier_map.DatabaseConnection, "get_engine", staticmethod(lambda: Eng()))
    assert supplier_map.load_map() == {}
    assert supplier_map.table_exists() is False
