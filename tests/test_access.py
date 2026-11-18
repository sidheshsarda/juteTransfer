"""Access list: roles, the password check and the admin guard inside the ops."""

import bcrypt
import pytest

from src.jutetransfer import access, transfer, warehouse_stock_ops


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.delenv("JT_LEGACY_LOGIN", raising=False)


def test_check_password():
    h = bcrypt.hashpw(b"secret", bcrypt.gensalt(4)).decode()
    assert access.check_password("secret", h)
    assert not access.check_password("wrong", h)
    assert not access.check_password("", h)
    assert not access.check_password("secret", None)
    assert not access.check_password("secret", "not-a-hash")


def test_resolve_role_bootstrap_beats_the_table(monkeypatch):
    monkeypatch.setenv("JT_ADMIN_EMAILS", "Boss@Mill.in , second@mill.in")
    assert access.resolve_role("boss@mill.in", None) == "admin"
    assert access.resolve_role("boss@mill.in", "user") == "admin"
    assert access.resolve_role("clerk@mill.in", "user") == "user"
    assert access.resolve_role("clerk@mill.in", "admin") == "admin"
    assert access.resolve_role("clerk@mill.in", None) is None
    assert access.resolve_role("clerk@mill.in", "owner") is None


def test_legacy_switch_is_off_by_default(enforced):
    assert not access.legacy_login_enabled()


@pytest.mark.parametrize("role", [None, "user"])
def test_require_admin_refuses_non_admins(enforced, monkeypatch, role):
    monkeypatch.setattr(access, "get_role", lambda uid: role)
    with pytest.raises(access.NotAllowed):
        access.require_admin(7)


def test_require_admin_lets_an_admin_through(enforced, monkeypatch):
    monkeypatch.setattr(access, "get_role", lambda uid: "admin")
    access.require_admin(7)


def test_deletes_are_refused_before_any_database_work(enforced, monkeypatch):
    monkeypatch.setattr(access, "get_role", lambda uid: "user")

    def boom(*a, **k):
        raise AssertionError("the database was touched")
    monkeypatch.setattr(transfer.DatabaseConnection, "get_transaction", boom)
    with pytest.raises(access.NotAllowed):
        transfer.delete_chain_from_step(1, 2, updated_by=7)
    with pytest.raises(access.NotAllowed):
        warehouse_stock_ops.delete_marked_move(3, 7)
    with pytest.raises(access.NotAllowed):
        access.set_access(9, "admin", True, updated_by=7)
