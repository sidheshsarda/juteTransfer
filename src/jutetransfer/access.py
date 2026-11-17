"""Who may use the transfer app, and who may change what is already saved.

Login is the user's own VoWERP login (sls.user_mst: e-mail + bcrypt
password). Being a VoWERP user is not enough: the app has its own access
list, `jt_user_access`, with two roles:

  user   create chains, marked moves and lots
  admin  everything a user can, plus anything that changes or removes what
         is already saved (delete a step, un-finalize, undo a marked move),
         and managing the access list

Bootstrap: the e-mails in JT_ADMIN_EMAILS (.env, comma-separated) are always
admins, table or no table, so the first admin can log in and add the others.

Transition switch: JT_LEGACY_LOGIN=1 keeps the two old shared logins working
(admin -> admin, user -> user) and makes the guard below permissive, because
a shared login carries no identity to check. Off by default.

No Streamlit import here: the ops modules call `require_admin` themselves, so
hiding a button in a page is never the only thing standing between a user and
a delete.
"""

import os
from typing import Optional

from sqlalchemy import text

from .database import DatabaseConnection

ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLES = (ROLE_USER, ROLE_ADMIN)

# The old shared logins, honoured only while JT_LEGACY_LOGIN is on.
LEGACY_USERS = {"admin": ("admin123", ROLE_ADMIN), "user": ("user123", ROLE_USER)}

DDL = """
CREATE TABLE IF NOT EXISTS jt_user_access (
    user_id            INT NOT NULL PRIMARY KEY,
    role               VARCHAR(10) NOT NULL,
    active             TINYINT(1) NOT NULL DEFAULT 1,
    updated_by         INT NULL,
    updated_date_time  DATETIME NULL
)
"""


class NotAllowed(PermissionError):
    """The logged-in user may not do this."""


def legacy_login_enabled() -> bool:
    return os.getenv("JT_LEGACY_LOGIN", "").strip().lower() in ("1", "true", "yes", "on")


def admin_emails() -> set:
    return {e.strip().lower() for e in os.getenv("JT_ADMIN_EMAILS", "").split(",") if e.strip()}


def check_password(password: str, hashed: Optional[str]) -> bool:
    """bcrypt check; a malformed or empty hash is simply a wrong password."""
    if not password or not hashed:
        return False
    import bcrypt
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def resolve_role(email: Optional[str], table_role: Optional[str]) -> Optional[str]:
    """Role from the bootstrap list first, then the access list. None = no access."""
    if email and email.strip().lower() in admin_emails():
        return ROLE_ADMIN
    return table_role if table_role in ROLES else None


def _table_role(conn, user_id: int) -> Optional[str]:
    """The active access-list role, or None (also when the table is not there yet)."""
    try:
        row = conn.execute(
            text("SELECT role FROM jt_user_access WHERE user_id = :u AND active = 1"),
            {"u": user_id},
        ).fetchone()
    except Exception:
        return None
    return row[0] if row else None


def get_role(user_id) -> Optional[str]:
    """Role of an active VoWERP user id, read fresh (never from the session)."""
    if user_id is None:
        return None
    with DatabaseConnection.get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT email_id FROM user_mst WHERE user_id = :u AND active = 1"),
            {"u": user_id},
        ).fetchone()
        if not row:
            return None
        return resolve_role(row[0], _table_role(conn, user_id))


def authenticate(email: str, password: str) -> Optional[dict]:
    """{'user_id', 'name', 'email', 'role'} for a correct login; role is None
    when the person is a VoWERP user without access to this app. None when
    the e-mail or password is wrong."""
    email = (email or "").strip()
    if not email or not password:
        return None
    with DatabaseConnection.get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT user_id, name, email_id, password FROM user_mst "
                 "WHERE LOWER(email_id) = :e AND active = 1"),
            {"e": email.lower()},
        ).fetchone()
        if not row or not check_password(password, row[3]):
            return None
        return {"user_id": int(row[0]), "name": row[1] or row[2], "email": row[2],
                "role": resolve_role(row[2], _table_role(conn, int(row[0])))}


def require_admin(user_id) -> None:
    """Raise NotAllowed unless `user_id` is an admin. Called inside the ops
    functions that change or remove saved transfers."""
    if legacy_login_enabled():
        return
    if get_role(user_id) != ROLE_ADMIN:
        raise NotAllowed("Only a transfer admin can change or delete what is already saved.")


# ── access list (admin page) ────────────────────────────────────────────────

def table_exists() -> bool:
    with DatabaseConnection.get_engine().connect() as conn:
        try:
            conn.execute(text("SELECT 1 FROM jt_user_access LIMIT 1"))
            return True
        except Exception:
            return False


def list_access() -> list:
    """Access-list rows with the person's name and e-mail, admins first."""
    with DatabaseConnection.get_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT a.user_id, u.name, u.email_id, a.role, a.active, u.active AS erp_active "
            "FROM jt_user_access a JOIN user_mst u ON u.user_id = a.user_id "
            "ORDER BY a.active DESC, a.role, u.name"
        )).mappings().all()
    return [dict(r) for r in rows]


def find_users(query: str, limit: int = 20) -> list:
    """Active VoWERP users whose name or e-mail contains `query`."""
    q = (query or "").strip()
    if len(q) < 2:
        return []
    with DatabaseConnection.get_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT user_id, name, email_id FROM user_mst "
            "WHERE active = 1 AND (LOWER(name) LIKE :q OR LOWER(email_id) LIKE :q) "
            "ORDER BY name LIMIT :n"
        ), {"q": f"%{q.lower()}%", "n": limit}).mappings().all()
    return [dict(r) for r in rows]


def set_access(user_id: int, role: str, active: bool, updated_by: int) -> None:
    """Grant, change or withdraw one person's access. Admins only."""
    require_admin(updated_by)
    if role not in ROLES:
        raise ValueError(f"Unknown role {role!r}")
    with DatabaseConnection.get_transaction() as conn:
        done = conn.execute(text(
            "UPDATE jt_user_access SET role = :r, active = :a, updated_by = :b, "
            "updated_date_time = NOW() WHERE user_id = :u"
        ), {"r": role, "a": 1 if active else 0, "b": updated_by, "u": user_id})
        if not done.rowcount:
            conn.execute(text(
                "INSERT INTO jt_user_access (user_id, role, active, updated_by, updated_date_time) "
                "VALUES (:u, :r, :a, :b, NOW())"
            ), {"r": role, "a": 1 if active else 0, "b": updated_by, "u": user_id})
