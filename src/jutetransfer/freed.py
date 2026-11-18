"""Freed-number slots (owner ruling 2026-11-17).

Every number the app hands out is MAX+1 of its series, so a number freed in
the MIDDLE of a series (a hop deleted because the lorry belonged at another
company, a chain un-finalized) used to stay a gap for ever, and the next
save took a new number at the top. Here a delete remembers the number it
frees (table jt_freed_no), and the next save in that series whose DATE FITS
takes it back:

  fits = the number is still unused, it is below the series' current top,
         and the new document's date lies between the latest date among the
         lower numbers and the earliest date among the higher ones

so a re-used number never breaks the number-follows-date order. A number
freed at the TOP of a series needs no slot: MAX+1 hands it out again.

Series (branch + April-March financial year of the document's date):
  mr        jute_mr.branch_mr_no        by jute_mr_date
  gate      jute_mr.jute_gate_entry_no  by jute_gate_entry_date
  billpass  jute_mr.bill_pass_no        by bill_pass_date
  invoice   sales_invoice.invoice_no    by invoice_date
PO and challan numbers are not covered.

Everything runs on the caller's connection, inside its transaction: a slot
taken by a save that rolls back is free again. Without the table (not
migrated yet) the app behaves exactly as before. No Streamlit import.
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import text

from .queries import _get_financial_year_bounds

SERIES = {
    "mr": ("jute_mr", "branch_mr_no", "jute_mr_date"),
    "gate": ("jute_mr", "jute_gate_entry_no", "jute_gate_entry_date"),
    "billpass": ("jute_mr", "bill_pass_no", "bill_pass_date"),
    "invoice": ("sales_invoice", "invoice_no", "invoice_date"),
}

DDL = """
CREATE TABLE IF NOT EXISTS jt_freed_no (
    freed_id           BIGINT PRIMARY KEY AUTO_INCREMENT,
    series             VARCHAR(10) NOT NULL,
    branch_id          INT NOT NULL,
    fy_start_year      INT NOT NULL,
    doc_no             BIGINT NOT NULL,
    doc_date           DATE NOT NULL,
    freed_from_id      BIGINT NULL,
    freed_by           INT NULL,
    freed_date_time    DATETIME NULL,
    closed_date_time   DATETIME NULL,
    closed_note        VARCHAR(40) NULL,
    KEY idx_jt_freed_no_open (series, branch_id, fy_start_year, closed_date_time)
)
"""

_ready = False


def _as_date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return value


def _table_ready(conn) -> bool:
    """True once jt_freed_no exists (remembered; a missing table is asked
    again, so creating it needs no restart)."""
    global _ready
    if not _ready:
        try:
            conn.execute(text("SELECT 1 FROM jt_freed_no LIMIT 1"))
            _ready = True
        except Exception:
            return False
    return True


def record(conn, series: str, branch_id, doc_no, doc_date, freed_from_id=None,
           freed_by=None) -> None:
    """Remember a number that the caller is about to free -- unless it is
    the top of its series, which MAX+1 hands out again by itself."""
    doc_date = _as_date(doc_date)
    if branch_id is None or doc_no is None or doc_date is None or not _table_ready(conn):
        return
    try:
        doc_no = int(doc_no)
    except (TypeError, ValueError):
        return
    if doc_no <= 0:
        return
    table, no_col, date_col = SERIES[series]
    fy_start, fy_end = _get_financial_year_bounds(doc_date)
    higher = conn.execute(text(
        f"SELECT 1 FROM {table} WHERE branch_id = :b AND {no_col} > :n "
        f"AND {date_col} BETWEEN :fs AND :fe LIMIT 1"),
        {"b": int(branch_id), "n": doc_no, "fs": fy_start.strftime("%Y-%m-%d"),
         "fe": fy_end.strftime("%Y-%m-%d")}).fetchone()
    if not higher:
        return
    conn.execute(text(
        "INSERT INTO jt_freed_no (series, branch_id, fy_start_year, doc_no, doc_date, "
        "freed_from_id, freed_by, freed_date_time) VALUES (:s, :b, :fy, :n, :d, :f, :u, NOW())"),
        {"s": series, "b": int(branch_id), "fy": _get_financial_year_bounds(doc_date)[0].year,
         "n": doc_no, "d": doc_date, "f": freed_from_id, "u": freed_by})


def _close(conn, freed_id: int, note: str) -> None:
    conn.execute(text(
        "UPDATE jt_freed_no SET closed_date_time = NOW(), closed_note = :n WHERE freed_id = :id"),
        {"n": note, "id": freed_id})


def take(conn, series: str, branch_id: int, doc_date, peek: bool = False) -> Optional[int]:
    """The lowest freed number of the series that a document dated `doc_date`
    may take, or None. Closes the slot unless `peek`. Slots whose number has
    been used again meanwhile are closed on the way."""
    doc_date = _as_date(doc_date)
    if doc_date is None or not _table_ready(conn):
        return None
    table, no_col, date_col = SERIES[series]
    fy_start, fy_end = _get_financial_year_bounds(doc_date)
    scope = {"b": int(branch_id), "fs": fy_start.strftime("%Y-%m-%d"),
             "fe": fy_end.strftime("%Y-%m-%d")}
    in_series = (f"FROM {table} WHERE branch_id = :b AND {no_col} IS NOT NULL "
                 f"AND {date_col} BETWEEN :fs AND :fe")
    slots = conn.execute(text(
        "SELECT freed_id, doc_no FROM jt_freed_no WHERE series = :s AND branch_id = :b "
        "AND fy_start_year = :fy AND closed_date_time IS NULL ORDER BY doc_no, freed_id"),
        {"s": series, "b": int(branch_id), "fy": fy_start.year}).fetchall()
    if not slots:
        return None
    top = int(conn.execute(text(f"SELECT COALESCE(MAX({no_col}), 0) {in_series}"),
                           scope).scalar() or 0)
    seen = set()
    for freed_id, doc_no in slots:
        doc_no = int(doc_no)
        if doc_no in seen:                      # the same number freed twice: one slot is enough
            if not peek:
                _close(conn, freed_id, "duplicate")
            continue
        seen.add(doc_no)
        if doc_no > top:                        # the top of the series: MAX+1 hands it out
            continue
        params = {**scope, "n": doc_no}
        if conn.execute(text(f"SELECT 1 {in_series} AND {no_col} = :n LIMIT 1"), params).fetchone():
            if not peek:
                _close(conn, freed_id, "number in use")
            continue
        lo = conn.execute(text(
            f"SELECT {date_col} {in_series} AND {no_col} < :n ORDER BY {date_col} DESC LIMIT 1"),
            params).fetchone()
        hi = conn.execute(text(
            f"SELECT {date_col} {in_series} AND {no_col} > :n ORDER BY {date_col} ASC LIMIT 1"),
            params).fetchone()
        if lo and _as_date(lo[0]) > doc_date:
            continue
        if hi and _as_date(hi[0]) < doc_date:
            continue
        if not peek:
            _close(conn, freed_id, "reused")
        return doc_no
    return None
