"""Change log of the transfer app: one row per exception an admin makes
(a date-rule override today; in-place edits later). Table jt_change_log,
sls only, written inside the caller's transaction so the log row commits or
rolls back with the change it describes. No Streamlit import.
"""

import json
from typing import Optional

from sqlalchemy import text

ACTION_DATE_OVERRIDE = "mr-date-override"

DDL = """
CREATE TABLE IF NOT EXISTS jt_change_log (
    log_id             BIGINT PRIMARY KEY AUTO_INCREMENT,
    action             VARCHAR(40) NOT NULL,
    jute_mr_id         BIGINT NULL,
    branch_id          INT NULL,
    detail             TEXT NULL,
    reason             VARCHAR(500) NULL,
    user_id            INT NULL,
    created_date_time  DATETIME NULL,
    KEY idx_jt_change_log_mr (jute_mr_id)
)
"""


def log(conn, action: str, *, jute_mr_id: Optional[int] = None, branch_id: Optional[int] = None,
        detail: Optional[dict] = None, reason: Optional[str] = None,
        user_id: Optional[int] = None) -> None:
    """Record one change. A change that must be logged is refused when it
    cannot be (table not created yet): the error leaves the transaction."""
    try:
        conn.execute(text(
            "INSERT INTO jt_change_log (action, jute_mr_id, branch_id, detail, reason, "
            "user_id, created_date_time) VALUES (:a, :m, :b, :d, :r, :u, NOW())"
        ), {"a": action, "m": jute_mr_id, "b": branch_id,
            "d": json.dumps(detail, default=str) if detail is not None else None,
            "r": (reason or "")[:500] or None, "u": user_id})
    except Exception as e:
        raise ValueError(
            "This change must be recorded in the change log, and the log could not be "
            f"written ({e}). Nothing was saved."
        ) from e
