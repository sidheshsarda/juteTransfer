"""Party -> jute supplier confirmations for the Payment Summary page.

Many MRs carry the jute supplier 'others' because names were changed at
import. The Payment Summary suggests which supplier a party (party_mst)
belongs to; an admin confirms, and only a confirmed entry is ever used to
group dues. One row per party in `jt_party_supplier_map` (sls only):

  jute_supplier_id = a real supplier   confirmed: this party belongs to it
  jute_supplier_id = NULL              confirmed: this party stays in 'others'
  no row                               not confirmed

Writes are admin-only (checked here, not only in the page). No Streamlit
import. Without the table (not migrated yet) reads give an empty map.
"""

from typing import Optional

from sqlalchemy import text

from . import access
from .database import DatabaseConnection

DDL = """
CREATE TABLE IF NOT EXISTS jt_party_supplier_map (
    party_id           INT NOT NULL PRIMARY KEY,
    jute_supplier_id   INT NULL,
    note               VARCHAR(200) NULL,
    updated_by         INT NOT NULL,
    updated_date_time  DATETIME NOT NULL
)
"""

# value in save_map's `changes`: delete the row (un-confirm the party)
CLEAR = "__clear__"


def table_exists() -> bool:
    with DatabaseConnection.get_engine().connect() as conn:
        try:
            conn.execute(text("SELECT 1 FROM jt_party_supplier_map LIMIT 1"))
            return True
        except Exception:
            return False


def load_map() -> dict:
    """{party_id: jute_supplier_id or None}; {} when the table is not there."""
    with DatabaseConnection.get_engine().connect() as conn:
        try:
            rows = conn.execute(text(
                "SELECT party_id, jute_supplier_id FROM jt_party_supplier_map")).fetchall()
        except Exception:
            return {}
    return {int(r[0]): (int(r[1]) if r[1] is not None else None) for r in rows}


def save_map(changes: dict, user_id) -> int:
    """Apply {party_id: supplier_id | None | CLEAR}: an id or None upserts the
    row, CLEAR deletes it. All in one transaction. Admins only. Returns the
    number of parties changed."""
    access.require_admin(user_id)
    if not changes:
        return 0
    n = 0
    with DatabaseConnection.get_transaction() as conn:
        for party_id, supplier_id in changes.items():
            if supplier_id == CLEAR:
                conn.execute(text("DELETE FROM jt_party_supplier_map WHERE party_id = :p"),
                             {"p": int(party_id)})
            else:
                sid: Optional[int] = None if supplier_id is None else int(supplier_id)
                conn.execute(text(
                    "INSERT INTO jt_party_supplier_map "
                    "(party_id, jute_supplier_id, updated_by, updated_date_time) "
                    "VALUES (:p, :s, :u, NOW()) "
                    "ON DUPLICATE KEY UPDATE jute_supplier_id = VALUES(jute_supplier_id), "
                    "updated_by = VALUES(updated_by), updated_date_time = VALUES(updated_date_time)"
                ), {"p": int(party_id), "s": sid, "u": int(user_id)})
            n += 1
    return n
