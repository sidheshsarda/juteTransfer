"""In-memory stand-in for the sls MySQL database, so tests can run the app's
REAL SQL (transfer.py, po_ops.py, queries.py) without a MySQL server.

How it works
    * The tables are created in an in-memory SQLite database from
      tests/fixtures/sls_schema_snapshot.json (the structure of the live
      tables), with MySQL's STRICT-mode refusals turned into constraints:
      NOT NULL, varchar length, decimal / integer range, non-numeric text in a
      numeric column, a non-date in a date column, unique keys.
    * The app keeps using SQLAlchemy exactly as in production: the fixture
      points DatabaseConnection at a SQLAlchemy engine on that SQLite database,
      so get_transaction() / get_connection() / execute_query() /
      execute_insert_returning_id() are the app's own code. Every statement is
      rewritten from the MySQL dialect just before SQLite sees it
      (see translate()); bind parameters are converted the way mysql-connector
      would send them.
    * Values come back as mysql-connector returns them: DATE -> date,
      DATETIME -> datetime, TIME -> timedelta, DECIMAL(p,s) -> Decimal with
      scale s, FLOAT -> the single-precision value MySQL would print.
    * NOW() is a frozen clock (FakeMySQL.now), so audit columns are testable.

Honesty rules
    * A statement that cannot be translated, uses something with different
      semantics in SQLite, or names an unknown table / column / function
      raises HarnessError showing the ORIGINAL statement. It is also recorded
      on FakeMySQL.harness_errors and the fixture fails the test at teardown,
      so app code that swallows exceptions cannot hide it.
    * What MySQL itself would refuse raises FakeMySQLError (with MySQL's errno).

What this does NOT prove (single SQLite connection)
    * Locking and concurrency: FOR UPDATE is dropped, there is no second
      session, no isolation level, no deadlock, no PO-number race. User locks
      (GET_LOCK / RELEASE_LOCK) are granted at once and only recorded
      (FakeMySQL.locks / lock_log); a test names the ones "another
      connection" holds in FakeMySQL.busy_locks. What IS checked: the lock
      connection must end its own transaction before the app's begins (the
      second-connection rule below), and no lock may be left held.
    * A second connection opened while a transaction is open (MySQL: a
      separate session that cannot see the uncommitted rows) is refused
      with HarnessError rather than emulated.
    * Expressions over DATE / DECIMAL columns (MAX(date_col), SUM(dec_col),
      COALESCE(...)) come back as str / float, not date / Decimal: only plain
      column references are converted.
    * Arithmetic over DECIMAL columns is float arithmetic. ROUND() drops the
      float noise first and then rounds half away from zero as MySQL does
      for exact values (so ROUND(SUM(total_price), 0) is faithful), but an
      unrounded difference such as ROUND(SUM(x), 0) - SUM(x) keeps the noise
      (-0.010000000242 where MySQL stores -0.01): compare it with a
      tolerance. ROUND of a FLOAT / DOUBLE value at an exact tie (half to
      even in MySQL) is not emulated.
    * String comparison is ASCII case-insensitive (COLLATE NOCASE) -- an
      approximation of the MySQL collation; no accent or trailing-space rules.
    * A DATE column compared with a bound *datetime* compares as text.
    * Ids used by a rolled-back transaction are not reused (as in InnoDB), but
      no other InnoDB internals are modelled; triggers and foreign keys of the
      live database do not exist here. Of its views only the ERP stock ledger
      vw_jute_stock_outstanding exists (VIEWS below, copied from sls).
"""

import json
import math
import re
import sqlite3
import struct
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "sls_schema_snapshot.json"

# The frozen database clock. The live server clock is IST.
DEFAULT_NOW = datetime(2026, 10, 2, 11, 30, 0)


class HarnessError(AssertionError):
    """The fake cannot run a statement the way MySQL would. Never a pass."""


class FakeMySQLError(SQLAlchemyError):
    """A statement MySQL (STRICT mode) would itself refuse. `errno` is the
    MySQL error number, `column` the offending column when there is one."""

    def __init__(self, errno: int, message: str, statement=None, column=None):
        self.errno = errno
        self.column = column
        self.statement = statement
        shown = f"\n[statement] {_squash(statement)}" if statement else ""
        super().__init__(f"({errno}) {message}{shown}")


def _squash(sql) -> str:
    return " ".join(str(sql).split())


# ---------------------------------------------------------------------------
# MySQL dialect -> SQLite
# ---------------------------------------------------------------------------

_LITERAL = re.compile(r"""
      (?P<sq>'(?:[^'\\]|\\.|'')*')
    | (?P<dq>"(?:[^"\\]|\\.|"")*")
    | (?P<bt>`(?:[^`]|``)*`)
    | (?P<lc>(?:--(?=\s|$)|\#)[^\n]*)
    | (?P<bc>/\*.*?\*/)
""", re.S | re.X)
_PLACEHOLDER = re.compile("\x01(\\d+)\x02")
_ESCAPES = {"0": "\0", "b": "\b", "n": "\n", "r": "\r", "t": "\t", "Z": "\x1a"}


def _mask(sql: str):
    """Take string literals, quoted identifiers and comments out of the way so
    the rewrites below only ever see code."""
    kept = []

    def repl(m):
        if m.lastgroup in ("lc", "bc"):
            return " "
        kept.append((m.lastgroup, m.group(0)))
        return f"\x01{len(kept) - 1}\x02"

    return _LITERAL.sub(repl, sql), kept


def _string_value(raw: str) -> str:
    """Value of a MySQL string literal ('' and backslash escapes decoded)."""
    quote, body, out, i = raw[0], raw[1:-1], [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            # \% and \_ keep their backslash (they only matter to LIKE)
            out.append("\\" + nxt if nxt in "%_" else _ESCAPES.get(nxt, nxt))
            i += 2
        elif ch == quote and body[i + 1:i + 2] == quote:
            out.append(quote)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _unmask(code: str, kept) -> str:
    def repl(m):
        kind, raw = kept[int(m.group(1))]
        if kind == "bt":        # `identifier` -> "identifier"
            return '"' + raw[1:-1].replace("``", "`").replace('"', '""') + '"'
        # 'string' and MySQL's "string" -> a SQLite string literal
        return "'" + _string_value(raw).replace("'", "''") + "'"

    return _PLACEHOLDER.sub(repl, code)


def _depths(code: str) -> list:
    """Parenthesis depth in front of every character."""
    out, depth = [], 0
    for ch in code:
        if ch == ")":
            depth -= 1
        out.append(depth)
        if ch == "(":
            depth += 1
    return out


def _close_paren(code: str, open_at: int) -> int:
    depth = 0
    for i in range(open_at, len(code)):
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise HarnessError(f"unbalanced parentheses: {_squash(code)}")


def _split_top_level(code: str, pattern) -> list:
    """Split at the matches of `pattern` that are outside every parenthesis."""
    depths, parts, last = _depths(code), [], 0
    for m in pattern.finditer(code):
        if depths[m.start()] == 0:
            parts.append(code[last:m.start()])
            last = m.end()
    parts.append(code[last:])
    return parts


_KEYWORD_AFTER_TABLE = (r"(?:JOIN|INNER|LEFT|RIGHT|CROSS|NATURAL|STRAIGHT_JOIN|ON|USING|"
                        r"WHERE|ORDER|GROUP|LIMIT|SET|HAVING)\b")
_TABLE_REF = re.compile(
    rf"\b(?:FROM|JOIN)\s+(\w+)(?:\s+(?:AS\s+)?(?!{_KEYWORD_AFTER_TABLE})(\w+))?", re.I)
_MULTI_DELETE = re.compile(
    r"^\s*DELETE\s+(?!FROM\b)(?P<targets>[\w\s,.*]+?)\s+FROM\s+(?P<source>.+?)\s*;?\s*$",
    re.I | re.S)


def _rewrite_multi_delete(code: str) -> str:
    """DELETE alias FROM t alias JOIN ... WHERE ...   (MySQL only)
    -> DELETE FROM t WHERE rowid IN (SELECT alias.rowid FROM t alias JOIN ...)"""
    m = _MULTI_DELETE.match(code)
    if not m:
        return code
    targets = [t.strip().removesuffix(".*") for t in m.group("targets").split(",")]
    if len(targets) != 1:
        raise HarnessError("DELETE from several tables at once is not emulated")
    source = m.group("source")
    from_clause = "FROM " + source
    depths, tables = _depths(from_clause), {}
    for ref in _TABLE_REF.finditer(from_clause):
        if depths[ref.start()] == 0:
            tables[(ref.group(2) or ref.group(1)).lower()] = ref.group(1)
    table = tables.get(targets[0].lower())
    if not table:
        raise HarnessError(f"DELETE target {targets[0]!r} is not a table of its FROM clause")
    return (f"DELETE FROM {table} WHERE rowid IN "
            f"(SELECT {targets[0]}.rowid FROM {source})")


_POSITION = re.compile(r"\bPOSITION\s*\(", re.I)
_IN = re.compile(r"\s+IN\s+", re.I)


def _rewrite_position(code: str) -> str:
    """POSITION(needle IN haystack) -> LOCATE(needle, haystack)."""
    while True:
        m = _POSITION.search(code)
        if not m:
            return code
        close = _close_paren(code, m.end() - 1)
        parts = _split_top_level(code[m.end():close], _IN)
        if len(parts) != 2:
            raise HarnessError("POSITION() without a single top-level IN")
        code = (code[:m.start()] + f"LOCATE({parts[0].strip()}, {parts[1].strip()})"
                + code[close + 1:])


_CAST = re.compile(r"\bCAST\s*\(", re.I)
_CAST_TYPE = re.compile(r"\s+AS\s+([A-Za-z_]+(?:\s+[A-Za-z_]+)?)\s*(\([\d\s,]*\))?\s*$", re.I)


def _rewrite_casts(code: str) -> str:
    """CAST(x AS UNSIGNED | SIGNED) -> CAST(x AS INTEGER). Any other target
    type than CHAR would silently mean something else in SQLite."""
    start = 0
    while True:
        m = _CAST.search(code, start)
        if not m:
            return code
        close = _close_paren(code, m.end() - 1)
        inner = code[m.end():close]
        t = _CAST_TYPE.search(inner)
        kind = " ".join(t.group(1).upper().split()) if t else ""
        if kind in ("UNSIGNED", "SIGNED", "UNSIGNED INTEGER", "SIGNED INTEGER",
                    "UNSIGNED INT", "SIGNED INT", "INTEGER"):
            inner = inner[:t.start()] + " AS INTEGER"
        elif kind != "CHAR":
            raise HarnessError(f"CAST(... AS {kind or '?'}) is not emulated")
        code = code[:m.end()] + inner + code[close:]
        start = m.end()


_UPDATE = re.compile(r"^\s*UPDATE\s+(\w+)\s+(?:(?:AS\s+)?(?!SET\b)\w+\s+)?SET\s+(.*)$", re.I | re.S)
_DELETE_FROM = re.compile(r"^\s*DELETE\s+FROM\s+(\w+)\b(.*)$", re.I | re.S)
_WHERE = re.compile(r"\bWHERE\b", re.I)
_COMMA = re.compile(r",")
_SUBSELECT = re.compile(r"\(\s*SELECT\b", re.I)
_ASSIGN = re.compile(r"^\s*(?:\w+\s*\.\s*)?(\w+)\s*=(.*)$", re.S)


def _without_subselects(code: str) -> str:
    while True:
        m = _SUBSELECT.search(code)
        if not m:
            return code
        code = code[:m.start()] + " " + code[_close_paren(code, m.start()) + 1:]


def _check_update_order(code: str) -> None:
    """MySQL evaluates the SET list left to right: `SET a = a + 1, b = a`
    gives b the NEW a. SQLite (standard SQL) gives it the old one. Refuse a
    statement whose result would depend on that."""
    m = _UPDATE.match(code)
    if not m:
        return
    set_list = _split_top_level(m.group(2), _WHERE)[0]
    assigned = []
    for part in _split_top_level(set_list, _COMMA):
        a = _ASSIGN.match(part)
        if not a:
            continue
        rhs = _without_subselects(a.group(2))
        for earlier in assigned:
            if re.search(rf"(?<![:\w]){re.escape(earlier)}\b", rhs, re.I):
                raise HarnessError(
                    f"UPDATE reads column {earlier!r} after assigning it in the same "
                    "SET list: MySQL would use the new value, SQLite the old one")
        assigned.append(a.group(1))


def _check_error_1093(code: str) -> None:
    """MySQL refuses UPDATE / DELETE on a table that a subquery of the same
    statement reads (error 1093) unless the subquery is a derived table."""
    m = _UPDATE.match(code)
    rest = m.group(2) if m else None
    if not m:
        m = _DELETE_FROM.match(code)
        rest = m.group(2) if m else None
    if not m:
        return
    table = m.group(1)
    stack = []                      # one flag per open parenthesis: derived table?
    for tok in re.finditer(rf"\(|\)|\b(?:FROM|JOIN)\s+{re.escape(table)}\b", rest, re.I):
        if tok.group(0) == "(":
            before = rest[:tok.start()].rstrip()
            stack.append(bool(re.search(r"\b(?:FROM|JOIN)$", before, re.I)))
        elif tok.group(0) == ")":
            if stack:
                stack.pop()
        elif stack and not any(stack):
            raise FakeMySQLError(
                1093, f"You can't specify target table '{table}' for update in FROM clause")


_LOCK = re.compile(
    r"\s+(?:FOR\s+UPDATE(?:\s+(?:NOWAIT|SKIP\s+LOCKED))?|FOR\s+SHARE|LOCK\s+IN\s+SHARE\s+MODE)\b",
    re.I)
_LAST_INSERT_ID = re.compile(r"\bLAST_INSERT_ID\s*\(\s*\)", re.I)
_START_READ_ONLY = re.compile(r"^\s*START\s+TRANSACTION\s+READ\s+ONLY\s*;?\s*$", re.I)
_CURRENT_TIMESTAMP = re.compile(r"\bCURRENT_TIMESTAMP\b(?:\s*\(\s*\))?", re.I)
_CURRENT_DATE = re.compile(r"\bCURRENT_DATE\b(?:\s*\(\s*\))?", re.I)


def translate(sql: str) -> str:
    """One MySQL statement, as the app wrote it -> the SQLite statement that
    means the same. Raises HarnessError when it cannot be made to."""
    code, kept = _mask(str(sql))
    if "||" in code:
        raise HarnessError("'||' is logical OR in MySQL but string concatenation in SQLite")
    _check_update_order(code)
    _check_error_1093(code)
    code = _rewrite_multi_delete(code)
    code = _rewrite_position(code)
    code = _rewrite_casts(code)
    code = _LAST_INSERT_ID.sub("last_insert_rowid()", code)
    code = _CURRENT_TIMESTAMP.sub("NOW()", code)
    code = _CURRENT_DATE.sub("CURDATE()", code)
    code = _LOCK.sub("", code)                    # no row locks: one connection
    code = code.replace("<=>", " IS ")            # NULL-safe equality
    code = code.replace("/", " * 1.0 / ")         # MySQL '/' never truncates
    return _unmask(code, kept)


# ---------------------------------------------------------------------------
# MySQL functions and value rules, as SQLite user functions
# ---------------------------------------------------------------------------

def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _half_up(value, exponent: Decimal) -> Decimal:
    return Decimal(repr(value)).quantize(exponent, rounding=ROUND_HALF_UP)


def _mysql_float(value):
    """What a FLOAT column keeps: the nearest single-precision number."""
    if not _is_number(value):
        return value
    try:
        return struct.unpack("f", struct.pack("f", float(value)))[0]
    except OverflowError:
        return value


def _mysql_decimal(value, scale):
    """What a DECIMAL(p, scale) column keeps: rounded half-up to its scale."""
    if not _is_number(value):
        return value
    return float(_half_up(value, Decimal(1).scaleb(-int(scale))))


def _mysql_int(value):
    """What an integer column keeps of a fractional number: rounded half-up."""
    return int(_half_up(value, Decimal(1))) if isinstance(value, float) else value


_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _mysql_date(value):
    """What a DATE column keeps of a datetime string: the date."""
    if isinstance(value, str) and len(value) > 10 and _DATE_PREFIX.match(value):
        return value[:10]
    return value


def _mysql_datetime(value):
    """What a DATETIME column keeps: 'YYYY-MM-DD HH:MM:SS', whole seconds."""
    if not isinstance(value, str) or not _DATE_PREFIX.match(value):
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.microsecond:
        parsed = (parsed + timedelta(microseconds=500000)).replace(microsecond=0)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


_TIME_TEXT = re.compile(r"^(-?)(\d{1,3}):(\d{2}):(\d{2})(?:\.\d+)?$")


def _mysql_time(value):
    """What a TIME column keeps of a DATETIME value (NOW(), as the marked-move
    header writes into in_time / out_time): the time part, as MySQL does."""
    if isinstance(value, str) and _DATE_PREFIX.match(value):
        try:
            return datetime.fromisoformat(value).strftime("%H:%M:%S")
        except ValueError:
            return value
    return value


def _mysql_temporal_ok(value, kind) -> int:
    """1 when `value` is acceptable to a DATE / DATETIME / TIME column."""
    if value is None:
        return 1
    if not isinstance(value, str):
        return 0
    if kind == "time" and _TIME_TEXT.match(value):
        return 1
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return 0
    return 1


def _regexp(pattern, value):
    if pattern is None or value is None:
        return None
    return int(re.search(pattern, str(value), re.I) is not None)   # ci collation


def _substring(value, pos, length=None):
    """SUBSTRING(str, pos[, len]): 1-based; pos 0 gives '', negative counts
    from the end."""
    if value is None or pos is None:
        return None
    value, pos = str(value), int(pos)
    if pos == 0:
        return ""
    start = pos - 1 if pos > 0 else max(len(value) + pos, 0)
    if length is None:
        return value[start:]
    return value[start:start + max(int(length), 0)]


def _locate(needle, haystack, pos=1):
    if needle is None or haystack is None:
        return None
    return str(haystack).lower().find(str(needle).lower(), int(pos) - 1) + 1


def _as_text(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _concat(*args):
    return None if any(a is None for a in args) else "".join(_as_text(a) for a in args)


def _mysql_round(value, digits=0):
    """ROUND(x[, d]) as MySQL rounds an exact (DECIMAL) value: half away from
    zero, on the exact decimal. SQLite only has the float: SUM() over a
    DECIMAL column such as total_price gives 1610608.4999999998 for
    991403.33 + 606341.97 + 12863.20, which its own round() takes down to
    1610608 where MySQL's exact 1610608.50 goes up to 1610609 (and
    round(1.005, 2) is 1.0 in SQLite, 1.01 in MySQL). So the float noise
    beyond the 15 significant digits a double holds (DBL_DIG) is dropped
    first. (MySQL rounds an approximate FLOAT / DOUBLE value half to even at
    an exact tie -- not emulated: the app only rounds DECIMAL sums.)"""
    if value is None or digits is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HarnessError(f"ROUND() of a non-number ({value!r}) is not emulated")
    exponent = Decimal(1).scaleb(-int(digits))
    exact = Decimal(value) if isinstance(value, int) else Decimal(f"{value:.15g}")
    with localcontext() as ctx:
        ctx.prec = 60
        rounded = exact.quantize(exponent, rounding=ROUND_HALF_UP)
    return int(rounded) if isinstance(value, int) else float(rounded)


def _date_part(value, index: int):
    """YEAR() / MONTH() / DAY() of a DATE or DATETIME value (NULL -> NULL)."""
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE_PREFIX.match(value):
        raise HarnessError(f"YEAR()/MONTH()/DAY() of a non-date ({value!r}) is not emulated")
    return int(value[:10].split("-")[index])


def _least(*args):
    return None if any(a is None for a in args) else min(args)


def _greatest(*args):
    return None if any(a is None for a in args) else max(args)


def _to_date(raw: bytes) -> date:
    return date.fromisoformat(raw.decode()[:10])


def _to_datetime(raw: bytes) -> datetime:
    return datetime.fromisoformat(_mysql_datetime(raw.decode()))


def _to_timedelta(raw: bytes) -> timedelta:
    m = _TIME_TEXT.match(raw.decode())
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)), seconds=int(m.group(4)))
    return -delta if m.group(1) else delta


def _to_mysql_float(raw: bytes) -> float:
    # MySQL prints a FLOAT with at most 6 significant digits (FLT_DIG).
    return float(f"{float(raw):.6g}")


def _decimal_converter(scale: int):
    exponent = Decimal(1).scaleb(-scale)
    return lambda raw: Decimal(raw.decode()).quantize(exponent, rounding=ROUND_HALF_UP)


# Converters are keyed by the declared column type (sqlite3 PARSE_DECLTYPES);
# the MYSQL_ prefix keeps them away from any other sqlite3 user in the process.
sqlite3.register_converter("MYSQL_DATE", _to_date)
sqlite3.register_converter("MYSQL_DATETIME", _to_datetime)
sqlite3.register_converter("MYSQL_TIME", _to_timedelta)
sqlite3.register_converter("MYSQL_FLOAT", _to_mysql_float)


def adapt_param(value):
    """A bind parameter, the way mysql-connector would send it."""
    if value is None or isinstance(value, (str, bytes)):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            raise FakeMySQLError(
                1054, f"Unknown column '{number}' in 'field list' (a NaN / inf was bound)")
        return number
    if isinstance(value, datetime):
        if value != value:                                   # pandas NaT
            raise FakeMySQLError(1292, "Incorrect datetime value: 'NaT'")
        return _mysql_datetime(value.isoformat(sep=" "))
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.strftime("%H:%M:%S")
    if isinstance(value, timedelta):
        seconds = int(value.total_seconds())
        sign, seconds = ("-" if seconds < 0 else ""), abs(seconds)
        return f"{sign}{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    if type(value).__module__ == "numpy" and hasattr(value, "item"):
        return adapt_param(value.item())
    raise FakeMySQLError(
        1210, f"Python '{type(value).__name__}' cannot be converted to a MySQL type")


def _adapt_params(parameters):
    if isinstance(parameters, (list, tuple)):
        return [_adapt_params(p) for p in parameters]
    if hasattr(parameters, "items"):
        return {k: adapt_param(v) for k, v in parameters.items()}
    return parameters


# ---------------------------------------------------------------------------
# Schema: the snapshot of the live tables -> SQLite DDL
# ---------------------------------------------------------------------------

_INT_RANGES = {
    "tinyint": (-128, 127), "smallint": (-32768, 32767),
    "mediumint": (-8388608, 8388607), "int": (-2147483648, 2147483647),
    "bigint": (-9223372036854775808, 9223372036854775807),
}
_NUMERIC = "typeof({q}) IN ('integer', 'real')"
_TYPE = re.compile(r"^(\w+)(?:\((\d+)(?:,(\d+))?\))?")


class _Schema:
    """DDL for every table of the snapshot, plus what the helpers need."""

    def __init__(self, path: Path):
        self.tables = json.loads(path.read_text(encoding="utf-8"))["tables"]
        self.columns = {t: [c["column"] for c in cols] for t, cols in self.tables.items()}
        self.primary_key = {}
        self.ddl = []
        for table, cols in self.tables.items():
            self._table(table, cols)

    def _table(self, table: str, cols: list) -> None:
        defs, normalise = [], []
        for col in cols:
            name, q = col["column"], f'"{col["column"]}"'
            m = _TYPE.match(col["type"].lower())
            base = m.group(1)
            size = int(m.group(2)) if m.group(2) else None
            scale = int(m.group(3)) if m.group(3) else 0
            if col["key"] == "PRI":
                self.primary_key[table] = name
            if "auto_increment" in (col["extra"] or ""):
                defs.append(f"{q} INTEGER PRIMARY KEY AUTOINCREMENT")
                continue

            kind, check, fix = None, None, None
            if base in _INT_RANGES:
                low, high = _INT_RANGES[base]
                sql_type, kind = base.upper(), "int"
                check = f"{_NUMERIC.format(q=q)} AND {q} BETWEEN {low} AND {high}"
                fix = f"mysql_int({q})"
            elif base == "float":
                sql_type, kind, check = "MYSQL_FLOAT", "num", _NUMERIC.format(q=q)
                fix = f"mysql_float({q})"
            elif base in ("double", "real"):
                sql_type, kind, check = "DOUBLE", "num", _NUMERIC.format(q=q)
            elif base in ("decimal", "numeric"):
                sql_type, kind = f"MYSQL_DECIMAL_{size}_{scale}", "dec"
                sqlite3.register_converter(sql_type, _decimal_converter(scale))
                limit = Decimal(10) ** (size - scale) - Decimal(1).scaleb(-scale)
                check = (f"{_NUMERIC.format(q=q)} AND "
                         f"ABS(mysql_decimal({q}, {scale})) <= {limit}")
                fix = f"mysql_decimal({q}, {scale})"
            elif base in ("varchar", "char"):
                sql_type, kind = f"VARCHAR({size}) COLLATE NOCASE", "len"
                check = f"length({q}) <= {size}"
            elif base in ("tinytext", "text", "mediumtext", "longtext"):
                sql_type = "TEXT COLLATE NOCASE"
            elif base == "date":
                sql_type, kind = "MYSQL_DATE", "date"
                check, fix = f"mysql_temporal_ok({q}, 'date')", f"mysql_date({q})"
            elif base in ("datetime", "timestamp"):
                sql_type, kind = "MYSQL_DATETIME", "datetime"
                check, fix = f"mysql_temporal_ok({q}, 'datetime')", f"mysql_datetime({q})"
            elif base == "time":
                sql_type, kind, check = "MYSQL_TIME", "time", f"mysql_temporal_ok({q}, 'time')"
                fix = f"mysql_time({q})"
            else:
                raise HarnessError(f"{table}.{name}: no SQLite mapping for {col['type']!r}")

            parts = [q, sql_type]
            if col["key"] == "PRI":
                parts.append("PRIMARY KEY")
            if not col["nullable"]:
                parts.append("NOT NULL")
            default = col["default"]
            if default == "CURRENT_TIMESTAMP":
                parts.append("DEFAULT (NOW())")
            elif default is not None:
                numeric = kind in ("int", "num", "dec") and re.fullmatch(r"-?\d+(\.\d+)?", default)
                parts.append(f"DEFAULT {default}" if numeric
                             else "DEFAULT '" + default.replace("'", "''") + "'")
            if col["key"] == "UNI":
                parts.append("UNIQUE")
            if check:
                parts.append(f'CONSTRAINT "{kind}|{table}|{name}" '
                             f"CHECK ({q} IS NULL OR ({check}))")
            defs.append(" ".join(parts))
            if fix:
                normalise.append((q, fix))

        self.ddl.append(f'CREATE TABLE "{table}" (\n  ' + ",\n  ".join(defs) + "\n)")
        if normalise:
            # MySQL stores the value in the column's own type (float32, the
            # decimal scale, the date part...): normalise right after a write.
            changed = " OR ".join(f"NEW.{q} IS NOT {fix.replace(q, 'NEW.' + q)}"
                                  for q, fix in normalise)
            sets = ", ".join(f"{q} = {fix}" for q, fix in normalise)
            for when in ("INSERT", "UPDATE"):
                self.ddl.append(
                    f'CREATE TRIGGER "norm_{when.lower()}_{table}" AFTER {when} ON "{table}" '
                    f'WHEN {changed} BEGIN UPDATE "{table}" SET {sets} '
                    f"WHERE rowid = NEW.rowid; END")


_SCHEMAS = {}

# The ERP's per-MR-line stock ledger, as it is on sls on 2026-10-03 (SHOW CREATE
# VIEW; the same text as vowerp3be dbqueries/migrations/
# 20260925_vw_jute_stock_outstanding_waste_kg_qty.sql). It is what the app's
# _available_kg, the Lots / Transfer / Marked grids and the P&L read:
#   bal_weight = actual_weight - issued (jute_issue, not status 4)
#                              - sold (approved raw-jute invoice lines that name
#                                      the MR line in sales_invoice_dtl.jute_mr_li_id)
# over active lines of MRs not at status 4 / 6 / 21 / 48. (A pending ERP
# migration, 20261003_vw_jute_stock_outstanding_returned_in_stock.sql, keeps
# status 48 in stock; model that here only once it is on sls.) Written in the
# MySQL dialect and run through translate() like any app statement.
VIEWS = {
    "vw_jute_stock_outstanding": """
CREATE VIEW vw_jute_stock_outstanding AS
SELECT
    jml.jute_mr_li_id                                              AS jute_mr_li_id,
    jm.out_date                                                    AS inward_date,
    jm.branch_id                                                   AS branch_id,
    jm.branch_mr_no                                                AS branch_mr_no,
    jm.jute_gate_entry_no                                          AS jute_gate_entry_no,
    wm.warehouse_name                                              AS warehouse_name,
    jml.actual_quality                                             AS actual_quality,
    jml.actual_item_id                                             AS actual_item_id,
    CASE WHEN ig.item_type_id = 3
         THEN jml.actual_weight
         ELSE jml.actual_qty END                                   AS actual_qty,
    jml.actual_weight                                              AS actual_weight,
    jm.unit_conversion                                             AS unit_conversion,
    CASE WHEN ig.item_type_id = 3
         THEN ROUND((jml.actual_weight
                - IFNULL(iss.isswt, 0)
                - IFNULL(sold.soldwt, 0)), 3)
         ELSE (jml.actual_qty
                - IFNULL(iss.issqty, 0)
                - CASE WHEN jml.actual_weight > 0
                       THEN jml.actual_qty * IFNULL(sold.soldwt, 0) / jml.actual_weight
                       ELSE 0 END) END                             AS bal_qty,
    ROUND((jml.actual_weight
       - IFNULL(iss.isswt, 0)
       - IFNULL(sold.soldwt, 0)), 3)                               AS bal_weight,
    jml.accepted_weight                                            AS accepted_weight,
    (jml.accepted_weight
       - ROUND(((jml.accepted_weight / jml.actual_qty) * IFNULL(iss.issqty, 0)), 3))
                                                                   AS bal_accepted_weight,
    jml.rate                                                       AS rate,
    jml.actual_rate                                                AS actual_rate,
    jm.status_id                                                   AS mr_status_id,
    CASE WHEN ig.item_type_id = 3
         THEN IFNULL(sold.soldwt, 0)
         WHEN jml.actual_weight > 0
         THEN jml.actual_qty * IFNULL(sold.soldwt, 0) / jml.actual_weight
         ELSE 0 END                                                AS sold_qty,
    IFNULL(sold.soldwt, 0)                                         AS sold_weight
FROM jute_mr jm
JOIN jute_mr_li jml
    ON jm.jute_mr_id = jml.jute_mr_id
LEFT JOIN warehouse_mst wm
    ON wm.warehouse_id = jml.warehouse_id
LEFT JOIN item_mst im
    ON im.item_id = jml.actual_item_id
LEFT JOIN item_grp_mst ig
    ON ig.item_grp_id = im.item_grp_id
LEFT JOIN (
    SELECT
        ji.jute_mr_li_id      AS jute_mr_li_id,
        SUM(ji.quantity)      AS issqty,
        SUM(ji.weight)        AS isswt
    FROM jute_issue ji
    WHERE ji.status_id <> 4
    GROUP BY ji.jute_mr_li_id
) iss
    ON iss.jute_mr_li_id = jml.jute_mr_li_id
LEFT JOIN (
    SELECT
        sid.jute_mr_li_id                                         AS jute_mr_li_id,
        SUM(COALESCE(NULLIF(sid.sales_weight, 0), sid.quantity, 0)) AS soldwt
    FROM sales_invoice_dtl sid
    JOIN sales_invoice sinv
        ON sinv.invoice_id = sid.invoice_id
    WHERE sid.jute_mr_li_id IS NOT NULL
      AND sinv.invoice_type = 5
      AND sinv.status_id = 3
      AND COALESCE(sinv.active, 1) = 1
    GROUP BY sid.jute_mr_li_id
) sold
    ON sold.jute_mr_li_id = jml.jute_mr_li_id
WHERE jm.status_id NOT IN (4, 6, 21, 48)
  AND jml.active = 1
  AND (jml.status IS NULL OR jml.status NOT IN ('4', '6'))
""",
}


def _schema(path: Path) -> _Schema:
    if path not in _SCHEMAS:
        _SCHEMAS[path] = _Schema(path)
    return _SCHEMAS[path]


_CHECK_FAILED = re.compile(r"CHECK constraint failed: (\w+)\|(\w+)\|(\w+)")
_NOT_NULL_FAILED = re.compile(r"NOT NULL constraint failed: (\w+)\.(\w+)")
_UNIQUE_FAILED = re.compile(r"UNIQUE constraint failed: (\w+)\.(\w+)")
_STRICT = {
    "len": (1406, "Data too long for column '{c}' at row 1"),
    "int": (1264, "Out of range (or incorrect integer) value for column '{c}' at row 1"),
    "num": (1265, "Data truncated (incorrect number) for column '{c}' at row 1"),
    "dec": (1264, "Out of range (or incorrect decimal) value for column '{c}' at row 1"),
    "date": (1292, "Incorrect date value for column '{c}' at row 1"),
    "datetime": (1292, "Incorrect datetime value for column '{c}' at row 1"),
    "time": (1292, "Incorrect time value for column '{c}' at row 1"),
}


def _as_mysql_error(exc: Exception, statement):
    """The MySQL error a SQLite constraint failure stands for, or None."""
    if not isinstance(exc, sqlite3.IntegrityError):
        return None
    message = str(exc)
    m = _CHECK_FAILED.search(message)
    if m and m.group(1) in _STRICT:
        errno, fmt = _STRICT[m.group(1)]
        return FakeMySQLError(errno, fmt.format(c=m.group(3)) + f" (table {m.group(2)})",
                              statement, column=m.group(3))
    m = _NOT_NULL_FAILED.search(message)
    if m:
        return FakeMySQLError(1048, f"Column '{m.group(2)}' cannot be null / has no default "
                                    f"value (table {m.group(1)})", statement, column=m.group(2))
    m = _UNIQUE_FAILED.search(message)
    if m:
        return FakeMySQLError(1062, f"Duplicate entry for key '{m.group(1)}.{m.group(2)}'",
                              statement, column=m.group(2))
    return None


# ---------------------------------------------------------------------------
# The fake database
# ---------------------------------------------------------------------------

class FakeMySQL:
    """The sls tables in memory, behind a SQLAlchemy engine.

    engine            what DatabaseConnection uses once install() ran
    now / set_now()   the frozen clock behind NOW()
    statements        every statement the app executed, as it wrote it
    parameters        the bind parameters of each, same index as statements
    harness_errors    HarnessErrors raised so far (must stay empty)
    read_only         True between START TRANSACTION READ ONLY and the end of
                      that transaction: a write is then refused as MySQL
                      refuses it (error 1792)
    database_name     what SELECT DATABASE() answers (the schema name in the
                      app's lock names)
    locks             user locks held right now (GET_LOCK adds, RELEASE_LOCK
                      removes); the fixture fails a test that leaves one held
    busy_locks        names GET_LOCK answers 0 for (held by "another
                      connection"); lock_log records every GET_LOCK /
                      RELEASE_LOCK as ('get' | 'release', name, result).
                      One connection: who holds a lock is not modelled beyond
                      that
    insert / update / delete / rows / row / count / snapshot / diff
                      direct access for building and inspecting data
    """

    def __init__(self, snapshot_path: Path = SNAPSHOT_PATH, now: datetime = DEFAULT_NOW,
                 database_name: str = "sls"):
        self._now = now
        self.harness_errors = []
        self.statements = []
        self.parameters = []
        self.read_only = False
        self.database_name = database_name
        self.locks = set()
        self.busy_locks = set()
        self.null_locks = set()        # names GET_LOCK answers NULL for (an error)
        self.lock_log = []
        self.schema = _schema(Path(snapshot_path))
        self.raw = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES,
                                   isolation_level=None, check_same_thread=False)
        self.engine = create_engine(
            "sqlite://", creator=lambda: self.raw, poolclass=StaticPool,
            paramstyle="named", pool_reset_on_return=None)
        self.engine.connect().close()       # SQLAlchemy's own on-connect work first
        self._register_functions()
        self.raw.execute("PRAGMA recursive_triggers = OFF")
        for statement in self.schema.ddl:
            self.raw.execute(statement)
        for view in VIEWS.values():
            self.raw.execute(translate(view))
        self._listen()

    # -- clock ---------------------------------------------------------------

    @property
    def now(self) -> datetime:
        return self._now

    def set_now(self, moment: datetime) -> None:
        self._now = moment

    def tick(self, **delta) -> datetime:
        """Move the clock forward (keyword arguments of timedelta)."""
        self._now += timedelta(**delta)
        return self._now

    # -- wiring --------------------------------------------------------------

    def _register_functions(self) -> None:
        create = self.raw.create_function
        create("NOW", 0, lambda: self._now.strftime("%Y-%m-%d %H:%M:%S"))
        create("CURDATE", 0, lambda: self._now.strftime("%Y-%m-%d"))
        create("DATABASE", 0, lambda: self.database_name)
        create("GET_LOCK", 2, self._get_lock)
        create("RELEASE_LOCK", 1, self._release_lock)
        create("regexp", 2, _regexp, deterministic=True)
        for name in ("SUBSTRING", "SUBSTR", "MID"):
            create(name, 2, _substring, deterministic=True)
            create(name, 3, _substring, deterministic=True)
        create("LOCATE", 2, _locate, deterministic=True)
        create("LOCATE", 3, _locate, deterministic=True)
        create("CONCAT", -1, _concat, deterministic=True)
        create("ROUND", 1, _mysql_round, deterministic=True)
        create("ROUND", 2, _mysql_round, deterministic=True)
        create("LEAST", -1, _least, deterministic=True)
        create("GREATEST", -1, _greatest, deterministic=True)
        create("YEAR", 1, lambda v: _date_part(v, 0), deterministic=True)
        create("MONTH", 1, lambda v: _date_part(v, 1), deterministic=True)
        create("DAY", 1, lambda v: _date_part(v, 2), deterministic=True)
        create("mysql_float", 1, _mysql_float, deterministic=True)
        create("mysql_decimal", 2, _mysql_decimal, deterministic=True)
        create("mysql_int", 1, _mysql_int, deterministic=True)
        create("mysql_date", 1, _mysql_date, deterministic=True)
        create("mysql_datetime", 1, _mysql_datetime, deterministic=True)
        create("mysql_time", 1, _mysql_time, deterministic=True)
        create("mysql_temporal_ok", 2, _mysql_temporal_ok, deterministic=True)

    def _harness_error(self, message: str) -> HarnessError:
        error = HarnessError(message)
        self.harness_errors.append(error)
        return error

    # -- user locks (GET_LOCK / RELEASE_LOCK) ----------------------------------

    def _get_lock(self, name, timeout):
        """GET_LOCK(name, timeout): 1 when granted, 0 when another connection
        holds it (busy_locks), NULL for a NULL name -- as MySQL answers. A
        name longer than 64 characters is refused as MySQL refuses it."""
        if name is None:
            self.lock_log.append(("get", None, None))
            return None
        name = str(name)
        if len(name) > 64:
            # recorded here: sqlite3 only reports "user-defined function raised exception"
            raise self._harness_error(f"GET_LOCK name longer than 64 characters: {name!r}")
        if name in self.null_locks:
            result = None
        else:
            result = 0 if name in self.busy_locks else 1
        if result:
            self.locks.add(name)
        self.lock_log.append(("get", name, result))
        return result

    def _release_lock(self, name):
        """RELEASE_LOCK(name): 1 when this connection held it, 0 when another
        does (busy_locks), NULL when nobody holds it."""
        if name is None:
            return None
        name = str(name)
        if name in self.locks:
            self.locks.discard(name)
            result = 1
        else:
            result = 0 if name in self.busy_locks else None
        self.lock_log.append(("release", name, result))
        return result

    def _listen(self) -> None:
        engine, raw = self.engine, self.raw

        @event.listens_for(engine, "begin")
        def begin(conn):
            if raw.in_transaction:
                raise self._harness_error(
                    "a second connection was opened while a transaction is open: on "
                    "MySQL that is another session, which cannot see the uncommitted "
                    "rows -- not emulated")
            conn.exec_driver_sql("BEGIN")

        @event.listens_for(engine, "before_cursor_execute", retval=True)
        def rewrite(conn, cursor, statement, parameters, context, executemany):
            if statement in ("BEGIN", "COMMIT", "ROLLBACK"):
                return statement, parameters
            self.statements.append(_squash(statement))
            self.parameters.append(dict(parameters) if hasattr(parameters, "items")
                                   else parameters)
            if _START_READ_ONLY.match(statement):
                self.read_only = True                # until this transaction ends
                return "SELECT 1", ()
            if self.read_only and statement.split(None, 1)[0].upper() in (
                    "INSERT", "UPDATE", "DELETE", "REPLACE"):
                raise FakeMySQLError(
                    1792, "Cannot execute statement in a READ ONLY transaction.", statement)
            try:
                translated = translate(statement)
            except HarnessError as exc:
                raise self._harness_error(
                    f"{exc}\n[statement] {_squash(statement)}") from None
            if context is not None:
                context.fake_mysql_original = statement
            return translated, _adapt_params(parameters)

        @event.listens_for(engine, "handle_error")
        def explain(ctx):
            exc = ctx.original_exception
            if isinstance(exc, (HarnessError, FakeMySQLError)):
                return None
            original = getattr(ctx.execution_context, "fake_mysql_original", ctx.statement)
            mysql_error = _as_mysql_error(exc, original)
            if mysql_error is not None:
                raise mysql_error from exc
            raise self._harness_error(
                f"the fake database could not run a statement: {exc}\n"
                f"[statement] {_squash(original)}\n"
                f"[as sqlite] {_squash(ctx.statement)}\n"
                f"[parameters] {ctx.parameters!r}") from exc

        rollback = engine.dialect.do_rollback

        def do_rollback(dbapi_connection):
            self.read_only = False
            # InnoDB never hands out again an auto-increment value a rolled
            # back transaction used; SQLite would.
            used = raw.execute("SELECT name, seq FROM sqlite_sequence").fetchall()
            rollback(dbapi_connection)
            for name, seq in used:
                if raw.execute("SELECT 1 FROM sqlite_sequence WHERE name = ?", (name,)).fetchone():
                    raw.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?",
                                (seq, name))
                else:
                    raw.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                                (name, seq))

        engine.dialect.do_rollback = do_rollback

        commit = engine.dialect.do_commit

        def do_commit(dbapi_connection):
            self.read_only = False
            commit(dbapi_connection)

        engine.dialect.do_commit = do_commit

    def install(self, monkeypatch) -> None:
        """Point the app's DatabaseConnection at this database (and make the
        real one unreachable for the duration of the test)."""
        from src.jutetransfer.config import DatabaseConfig
        from src.jutetransfer.database import DatabaseConnection

        engine = self.engine

        def refuse(cls):
            raise self._harness_error("a test tried to reach the real MySQL database")

        monkeypatch.setattr(DatabaseConnection, "_engine", engine)
        monkeypatch.setattr(DatabaseConnection, "get_engine", classmethod(lambda cls: engine))
        monkeypatch.setattr(DatabaseConfig, "get_connection_string", classmethod(refuse))
        monkeypatch.setattr(DatabaseConfig, "get_mysql_config", classmethod(refuse))

    def close(self) -> None:
        self.engine.dispose()
        self.raw.close()

    # -- direct access ---------------------------------------------------------

    def _columns(self, table: str, used=()) -> list:
        if table not in self.schema.columns:
            raise HarnessError(f"no table {table!r} in the schema snapshot")
        unknown = [c for c in used if c not in self.schema.columns[table]]
        if unknown:
            raise HarnessError(f"{table} has no column(s) {unknown}")
        return self.schema.columns[table]

    def _run(self, sql: str, params=()):
        try:
            return self.raw.execute(sql, [adapt_param(p) for p in params])
        except sqlite3.Error as exc:
            raise (_as_mysql_error(exc, sql) or HarnessError(f"{exc}\n[statement] {sql}")) from exc

    @staticmethod
    def _where(where: dict):
        clauses, params = [], []
        for column, value in where.items():
            if value is None:
                clauses.append(f'"{column}" IS NULL')
            else:
                clauses.append(f'"{column}" = ?')
                params.append(value)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params

    def insert(self, table: str, **cols) -> int:
        """Insert one row (same STRICT rules as the app's inserts); its id."""
        self._columns(table, cols)
        names = ", ".join(f'"{c}"' for c in cols)
        marks = ", ".join("?" for _ in cols)
        sql = (f'INSERT INTO "{table}" ({names}) VALUES ({marks})' if cols
               else f'INSERT INTO "{table}" DEFAULT VALUES')
        return self._run(sql, list(cols.values())).lastrowid

    def update(self, table: str, where: dict, **cols) -> int:
        """UPDATE table SET cols WHERE where; the number of rows matched."""
        self._columns(table, list(where) + list(cols))
        clause, params = self._where(where)
        sets = ", ".join(f'"{c}" = ?' for c in cols)
        return self._run(f'UPDATE "{table}" SET {sets}{clause}',
                         list(cols.values()) + params).rowcount

    def delete(self, table: str, **where) -> int:
        self._columns(table, where)
        clause, params = self._where(where)
        return self._run(f'DELETE FROM "{table}"{clause}', params).rowcount

    def rows(self, table: str, **where) -> list:
        """Rows as dicts (values as mysql-connector returns them), in id order."""
        self._columns(table, where)
        clause, params = self._where(where)
        cursor = self._run(f'SELECT * FROM "{table}"{clause} ORDER BY rowid', params)
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, r)) for r in cursor.fetchall()]

    def row(self, table: str, **where) -> dict:
        """The one row matching `where`."""
        found = self.rows(table, **where)
        if len(found) != 1:
            raise AssertionError(f"expected one {table} row for {where}, found {len(found)}")
        return found[0]

    def count(self, table: str, **where) -> int:
        self._columns(table, where)
        clause, params = self._where(where)
        return self._run(f'SELECT COUNT(*) FROM "{table}"{clause}', params).fetchone()[0]

    def snapshot(self, tables=None, ignore=None) -> dict:
        """{table: [row dict, ...]} for before / after comparisons.

        tables: the tables to include (default: all).
        ignore: {table: columns to leave out}; the key '*' applies to every
                table."""
        ignore = ignore or {}
        out = {}
        for table in (tables or list(self.schema.columns)):
            skip = set(ignore.get("*", ())) | set(ignore.get(table, ()))
            out[table] = [{k: v for k, v in r.items() if k not in skip}
                          for r in self.rows(table)]
        return out

    def diff(self, before: dict, after: dict) -> list:
        """Readable differences between two snapshots ([] when equal)."""
        out = []
        for table in sorted(set(before) | set(after)):
            key = self.schema.primary_key.get(table)
            old = {r.get(key, i): r for i, r in enumerate(before.get(table, []))}
            new = {r.get(key, i): r for i, r in enumerate(after.get(table, []))}
            out.extend(f"{table}[{k}] deleted" for k in old if k not in new)
            out.extend(f"{table}[{k}] inserted" for k in new if k not in old)
            for k in old:
                if k in new and old[k] != new[k]:
                    out.extend(
                        f"{table}[{k}].{c}: {old[k].get(c)!r} -> {new[k].get(c)!r}"
                        for c in sorted(set(old[k]) | set(new[k]))
                        if old[k].get(c) != new[k].get(c))
        return out

    def execute(self, sql: str, **params):
        """Run one MySQL-dialect statement through the engine, the way the app
        does, in its own committed transaction. Rows (dicts) for a SELECT,
        the row count otherwise."""
        with self.engine.connect() as conn:
            result = conn.execute(text(sql), params)
            out = ([dict(r._mapping) for r in result.fetchall()] if result.returns_rows
                   else result.rowcount)
            conn.commit()
        return out

    def writes(self, since: int = 0) -> list:
        """The INSERT / UPDATE / DELETE statements the app executed."""
        return [s for s in self.statements[since:]
                if s.split(None, 1)[0].upper() in ("INSERT", "UPDATE", "DELETE", "REPLACE")]


@pytest.fixture
def fake_db(monkeypatch):
    """A fresh fake database with the app's DatabaseConnection pointed at it."""
    db = FakeMySQL()
    db.install(monkeypatch)
    yield db
    errors = list(db.harness_errors)
    held = sorted(db.locks)
    db.close()
    assert not errors, "statements the fake database could not run faithfully:\n" + \
        "\n".join(str(e) for e in errors)
    assert not held, f"user locks still held at the end of the test: {held}"


# ---------------------------------------------------------------------------
# A chain scenario shaped like the live data (Empire -> Jagrati -> Empire,
# gate entry 21 of 31-08-2026: root MR 28137, ERP PO 13314)
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    """Ids of the rows build_scenario() created. A = the origin mill,
    B and C = forwarding companies. Every id is different from every other,
    so a company id used where a branch id belongs cannot pass by accident."""

    db: FakeMySQL
    uom: str = "BALE"
    user: int = 1                          # demo auth: updated_by is always 1

    a_co: int = 2
    a_branch: int = 29
    a_godown: int = 37
    a_lorry_type: int = 85                 # 100 quintals, belongs to A
    a_name: str = "THE EMPIRE JUTE COMPANY LTD."
    b_co: int = 74
    b_branch: int = 20
    b_godown: int = 202
    b_name: str = "Jagrati Trade Services Pvt. Ltd."
    c_co: int = 27
    c_branch: int = 33
    c_godown: int = 206
    c_name: str = "Greeting Marketing Pvt. Ltd."

    supplier: int = 2430                   # jute_supplier_mst (the broker)
    others_supplier: int = 2397            # catch-all supplier "others"
    supplier_party: int = 207              # the outside supplier as a party of A
    supplier_party_branch: int = 3811
    supplier_party_name: str = "HONEYWELL COMMERCIAL PVT. LTD."
    item_group: int = 12
    items: tuple = (56, 57)                # A's items: D TD-5, D TD-6
    mukam: int = 9                         # the MR's mukam
    po_mukam: int = 341                    # the ERP PO's own mukam id

    po: int = 13314                        # ERP PO X at A
    po_no: int = 11
    po_date: date = date(2026, 8, 26)
    po_lines: tuple = (31266, 31267)
    po_credit_term: int = 45
    po_delivery_days: int = 7

    root: int = 28137                      # root MR at A, Pending (13)
    root_lines: tuple = (45537, 45538)     # active: 9312 kg @ 12850, 1441 kg @ 12650
    root_dead_line: int = 45524            # soft-deleted (active = 0)
    ge_no: int = 21
    ge_date: date = date(2026, 8, 31)

    def co(self, which: str) -> int:
        return {"A": self.a_co, "B": self.b_co, "C": self.c_co}[which]

    def branch(self, which: str) -> int:
        return {"A": self.a_branch, "B": self.b_branch, "C": self.c_branch}[which]

    def godown(self, which: str) -> int:
        return {"A": self.a_godown, "B": self.b_godown, "C": self.c_godown}[which]

    def name(self, which: str) -> str:
        return {"A": self.a_name, "B": self.b_name, "C": self.c_name}[which]

    # -- hand-built states (po_ops tests do not go through transfer.py) --------

    def party_in(self, co_id: int, name: str) -> tuple:
        """(party_id, party_branch_id) of the party called `name` in a
        company, created with one branch when it is not there yet."""
        db = self.db
        for party in db.rows("party_mst", co_id=co_id):
            if party["supp_name"].lower() == name.lower():
                branches = db.rows("party_branch_mst", party_id=party["party_id"])
                return party["party_id"], branches[0]["party_mst_branch_id"]
        party_id = db.insert("party_mst", supp_name=name, co_id=co_id, active=1,
                             party_type_id="2,3", country_id=101, updated_by=self.user)
        branch_id = db.insert(
            "party_branch_mst", party_id=party_id, active=1, gst_no="19AABCX0000X1Z0",
            address="1 STRAND ROAD", zip_code=700001, city_id=5583, state_id=19,
            created_by=self.user, updated_by=self.user)
        return party_id, branch_id

    def add_party(self, co_id: int, name: str, party_id: int = None,
                  branch_ids: tuple = (None,)) -> int:
        """A party in a company even when one of that name is already there
        (the live party master has same-name duplicates), with one party
        branch per entry of branch_ids (None: the next id). Its id."""
        db = self.db
        cols = dict(supp_name=name, co_id=co_id, active=1, party_type_id="2,3",
                    country_id=101, updated_by=self.user)
        if party_id is not None:
            cols["party_id"] = party_id
        party_id = db.insert("party_mst", **cols)
        for branch_id in branch_ids:
            fixed = {} if branch_id is None else {"party_mst_branch_id": branch_id}
            db.insert("party_branch_mst", party_id=party_id, active=1,
                      gst_no=f"19AABCD{party_id:04d}X1Z0", address="2 LYONS RANGE",
                      zip_code=700001, city_id=5583, state_id=19,
                      created_by=self.user, updated_by=self.user, **fixed)
        return party_id

    def items_in(self, co_id: int) -> dict:
        """{A's item id: the same-named item of another company}, created
        (with their group) when missing -- what _ensure_item leaves behind."""
        db = self.db
        groups = db.rows("item_grp_mst", co_id=co_id, item_grp_name="JUTE")
        group = groups[0]["item_grp_id"] if groups else db.insert(
            "item_grp_mst", item_grp_name="JUTE", item_grp_code="JUTE", co_id=co_id,
            item_type_id=2, active="1", updated_by=self.user)
        own = {i["item_name"]: i["item_id"] for i in db.rows("item_mst", item_grp_id=group)}
        mapping = {}
        for source in db.rows("item_mst", item_grp_id=self.item_group):
            if source["item_name"] not in own:
                copy = {k: v for k, v in source.items()
                        if k not in ("item_id", "item_grp_id", "updated_date_time")}
                own[source["item_name"]] = db.insert("item_mst", item_grp_id=group, **copy)
            mapping[source["item_id"]] = own[source["item_name"]]
        return mapping

    def active_lines(self, mr_id: int) -> list:
        """Ids of an MR's active lines, in id order."""
        return [r["jute_mr_li_id"] for r in self.db.rows("jute_mr_li", jute_mr_id=mr_id)
                if r["active"] in (1, None)]

    def add_root(self, ge_no: int = 15, ge_date: date = date(2026, 8, 27)) -> int:
        """Another lorry received on PO X: a second Pending root MR with the
        same three lines."""
        db = self.db
        header = db.row("jute_mr", jute_mr_id=self.root)
        header.pop("jute_mr_id")
        header.update(jute_gate_entry_no=ge_no, jute_gate_entry_date=ge_date, out_date=ge_date)
        root = db.insert("jute_mr", **header)
        for line in db.rows("jute_mr_li", jute_mr_id=self.root):
            line.pop("jute_mr_li_id")
            db.insert("jute_mr_li", **{**line, "jute_mr_id": root})
        return root

    def add_hop(self, at: str = "B", mr_date: date = date(2026, 9, 1), root: int = None,
                party_id="supplier", src_co: int = None, mapped: bool = True,
                **header) -> int:
        """A chain hop MR as save_transfer_step leaves it BEFORE its PO: the
        root's lorry at a forwarding company -- original rates, the company's
        own items and godown, po_id NULL.

        party_id: 'supplier' = the outside supplier re-created as a party of
        that company (what a first hop carries; `mapped` adds the
        supplier / party map row); or any value to store as given.
        src_co:   the company the hop received from (default: the mill)."""
        db, co_id, root = self.db, self.co(at), root or self.root
        party_branch = None
        if party_id == "supplier":
            party_id, party_branch = self.party_in(co_id, self.supplier_party_name)
            if mapped and not db.count("jute_supp_party_map", co_id=co_id,
                                       jute_supplier_id=self.supplier, party_id=party_id):
                db.insert("jute_supp_party_map", co_id=co_id, jute_supplier_id=self.supplier,
                          party_id=party_id, updated_by=self.user)
        items = self.items_in(co_id)
        source = db.row("jute_mr", jute_mr_id=root)
        copied = ("challan_date", "challan_no", "challan_weight", "gross_weight",
                  "tare_weight", "net_weight", "variable_shortage", "actual_weight",
                  "in_time", "out_date", "out_time", "qc_check", "mukam_id",
                  "unit_conversion", "mr_weight", "vehicle_no", "marketing_slip",
                  "transporter", "driver_name", "jute_supplier_id")
        cols = {c: source[c] for c in copied}
        number = db.count("jute_mr", branch_id=self.branch(at)) + 1
        cols.update(
            jute_gate_entry_no=number, branch_mr_no=number, jute_gate_entry_date=mr_date,
            jute_mr_date=mr_date, status_id=3, updated_by=self.user,
            updated_date_time=db.now, po_id=None, branch_id=self.branch(at),
            party_id=party_id, party_branch_id=party_branch,
            src_com_id=self.a_co if src_co is None else src_co,
            total_amount=1378878.0, claim_amount=13757.0, roundoff=0.0,
            net_total=1365121.0, src_jute_mr_id=root, transfer_mode=0,
            bill_pass_no=number, bill_pass_date=mr_date)
        cols.update(header)
        hop = db.insert("jute_mr", **cols)
        for line_id in self.active_lines(root):
            line = db.row("jute_mr_li", jute_mr_li_id=line_id)
            line.pop("jute_mr_li_id")
            line.update(jute_mr_id=hop, jute_po_li_id=None, warehouse_id=self.godown(at),
                        actual_item_id=items[line["actual_item_id"]],
                        challan_item_id=items[line["challan_item_id"]])
            db.insert("jute_mr_li", **line)
        return hop

    def finalize_root(self, forwarder: str = "B", mr_date: date = date(2026, 9, 3),
                      rates: tuple = (12914.0, 12713.0), root: int = None) -> int:
        """Put a root in the state finalize leaves it in: party = the last
        forwarding company as a party of A, final rates, MR / bill-pass /
        invoice fields, status 3. Returns that party's id."""
        db, root = self.db, root or self.root
        party_id, party_branch = self.party_in(self.a_co, self.name(forwarder).upper())
        total = 0.0
        for line_id, rate in zip(self.active_lines(root), rates):
            weight = db.row("jute_mr_li", jute_mr_li_id=line_id)["accepted_weight"]
            price = round(weight * rate / 100, 2)
            db.update("jute_mr_li", {"jute_mr_li_id": line_id}, rate=rate, total_price=price)
            total += price
        number = db.count("jute_mr", branch_id=self.a_branch, status_id=3) + 1
        db.update("jute_mr", {"jute_mr_id": root}, party_id=str(party_id),
                  party_branch_id=party_branch, branch_mr_no=number, jute_mr_date=mr_date,
                  bill_pass_no=number, bill_pass_date=mr_date, status_id=3,
                  total_amount=round(total), roundoff=round(round(total) - total, 2),
                  challan_no="SEP/0001", challan_date=mr_date, invoice_no="JTSPL/SI/26-27/1",
                  invoice_date=mr_date, invoice_amount=round(total), updated_by=self.user)
        return party_id


def build_scenario(db: FakeMySQL, uom: str = "BALE") -> Scenario:
    """Masters, the ERP PO X and the Pending root MR of one lorry.

    B and C start with a branch and a godown only: no parties, no items, no
    lorry types (the app creates party / item masters itself)."""
    sc = Scenario(db=db, uom=uom)
    erp_user = 24

    for which, co_id, prefix, pan in (("A", sc.a_co, "EJM", "AAACT1111E"),
                                      ("B", sc.b_co, "JTSPL", "AAACJ2222L"),
                                      ("C", sc.c_co, "GMPL", "AAACG3333M")):
        db.insert("co_mst", co_id=co_id, co_name=sc.name(which), co_prefix=prefix,
                  co_address1=f"{co_id} CLIVE ROW", co_address2="KOLKATA", co_zipcode=700001,
                  country_id=101, state_id=19, city_id=5583, co_pan_no=pan,
                  co_cin_no=f"U51909WB1990PTC0{co_id:05d}",
                  co_email_id=f"accounts@{prefix.lower()}.example", updated_by=erp_user)
    for which, name, prefix in (("A", "FACTORY", "F"), ("B", "HEAD OFFICE", None),
                                ("C", "KOLKATA", "KOL")):
        branch = sc.branch(which)
        db.insert("branch_mst", branch_id=branch, branch_name=name, co_id=sc.co(which),
                  branch_address1=f"{branch} MILL ROAD", branch_address2="WEST BENGAL",
                  branch_zipcode=743100 + branch, country_id=101, city_id=5583, state_id=19,
                  gst_no=f"19AAAC{which}{branch:04d}K1Z{branch % 10}",
                  contact_no=f"03322{branch:05d}", contact_person=f"MANAGER {which}",
                  active=1, updated_by=erp_user, branch_prefix=prefix)
        db.insert("warehouse_mst", warehouse_id=sc.godown(which),
                  warehouse_name=f"{which}_JUTE", branch_id=branch, updated_by=erp_user)
    db.insert("jute_lorry_mst", jute_lorry_type_id=sc.a_lorry_type, co_id=sc.a_co,
              lorry_type="LARGE", weight=100, updated_by=erp_user)

    db.insert("jute_supplier_mst", supplier_id=sc.others_supplier, supplier_name="others")
    db.insert("jute_supplier_mst", supplier_id=sc.supplier, supplier_name="shyamji")
    db.insert("jute_mukam_mst", mukam_id=sc.mukam, mukam_name="COSSIMBAZAR")
    db.insert("jute_mukam_mst", mukam_id=sc.po_mukam, mukam_name="COSSIMBAZAR (PO)")

    # parties of A: an older one, and the outside supplier of this lorry
    db.insert("party_mst", party_id=98, supp_name="LOCAL JUTE TRADERS", co_id=sc.a_co,
              active=1, supp_code="J99", party_type_id="3", country_id=101, updated_by=erp_user)
    db.insert("party_mst", party_id=sc.supplier_party, supp_name=sc.supplier_party_name,
              co_id=sc.a_co, active=1, supp_code="J207", party_type_id="2,3",
              party_pan_no="AABCH4444K", supp_email_id="sales@honeywell.example",
              phone_no="03322001100", country_id=101, entity_type_id=2, updated_by=erp_user)
    db.insert("party_branch_mst", party_mst_branch_id=sc.supplier_party_branch,
              party_id=sc.supplier_party, active=1, gst_no="19AABCH4444K1Z2",
              address="14 NETAJI SUBHAS ROAD", address_additional="KOLKATA", zip_code=700001,
              city_id=5583, state_id=19, contact_no="03322001100", contact_person="MR SHAW",
              created_by=erp_user, updated_by=erp_user)
    db.insert("jute_supp_party_map", map_id=5001, co_id=sc.a_co, jute_supplier_id=sc.supplier,
              party_id=sc.supplier_party, updated_by=erp_user)

    db.insert("item_grp_mst", item_grp_id=sc.item_group, item_grp_name="JUTE",
              item_grp_code="JUTE", co_id=sc.a_co, item_type_id=2, active="1",
              updated_by=erp_user)
    for item_id, name in zip(sc.items, ("D TD-5", "D TD-6")):
        db.insert("item_mst", item_id=item_id, item_name=name, item_code=name.replace(" ", ""),
                  item_grp_id=sc.item_group, hsn_code="53031010", uom_id=163, tangible=1,
                  saleable=1, consumable=1, purchaseable=1, manufacturable=0, assembly=0,
                  tax_percentage=0, uom_rounding=2, rate_rounding=2, active=1,
                  updated_by=erp_user)

    # ERP PO X: 2 lorries x 100 quintals, split 80 % / 20 % (percentage mode)
    db.insert("jute_po", jute_po_id=sc.po, branch_id=sc.a_branch, po_no=sc.po_no,
              po_date=sc.po_date, status_id=3, supplier_id=sc.supplier,
              party_id=sc.supplier_party, jute_mukam_id=sc.po_mukam, jute_uom=uom,
              vehicle_type_id=sc.a_lorry_type, vehicle_quantity=2, weight=20000,
              jute_po_value=2562000, channel_code="DOMESTIC", credit_term=sc.po_credit_term,
              delivery_days=sc.po_delivery_days, updated_by=19,
              updated_date_time=datetime(2026, 8, 26, 9, 55, 41))
    for po_li, item_id, pct, qty, rate, value in (
            (sc.po_lines[0], sc.items[0], 80, 107, 12850, 2056000),
            (sc.po_lines[1], sc.items[1], 20, 27, 12650, 506000)):
        db.insert("jute_po_li", jute_po_li_id=po_li, jute_po_id=sc.po, item_id=item_id,
                  quantity=qty, rate=rate, value=value, percentage=pct, jute_uom=uom,
                  crop_year=26, allowable_moisture=20, active=1, status_id=21,
                  updated_date_time=datetime(2026, 8, 26, 9, 55, 39))

    # The root MR as the ERP hands it over (status 13): no MR number or date yet.
    db.insert("jute_mr", jute_mr_id=sc.root, jute_gate_entry_no=sc.ge_no,
              jute_gate_entry_date=sc.ge_date, branch_mr_no=None, jute_mr_date=None,
              challan_date=date(2026, 8, 29), challan_no="26", challan_weight=11400,
              gross_weight=19120, tare_weight=7870, net_weight=11250, variable_shortage=80,
              actual_weight=11170, in_time=time(9, 15), out_date=sc.ge_date,
              out_time=time(13, 40), qc_check=1, mukam_id=sc.mukam, unit_conversion=uom,
              mr_weight=10753, status_id=13, vehicle_no="WB-57C-6522", marketing_slip=0,
              transporter="KALI TRANSPORT", driver_name="RAMESH", updated_by=erp_user,
              updated_date_time=datetime(2026, 8, 31, 18, 5, 0), po_id=sc.po,
              branch_id=sc.a_branch, party_id=str(sc.supplier_party),
              party_branch_id=sc.supplier_party_branch, jute_supplier_id=sc.supplier,
              total_amount=1378878.5, bill_pass_complete=0, src_jute_mr_id=None,
              transfer_mode=0)
    stamp = datetime(2026, 8, 31, 17, 40, 0)
    common = dict(jute_mr_id=sc.root, allowable_moisture=20, warehouse_id=sc.a_godown,
                  crop_year=None, marka=None, water_damage_amount=0, premium_amount=0,
                  updated_date_time=stamp)
    db.insert("jute_mr_li", jute_mr_li_id=sc.root_dead_line, actual_item_id=sc.items[0],
              challan_item_id=sc.items[0], actual_qty=66, actual_weight=0, challan_quantity=66,
              challan_weight=9900, actual_moisture="24.25", shortage_kgs=0, accepted_weight=0,
              rate=0, claim_rate=0, total_price=0, active=0, **common)
    db.insert("jute_mr_li", jute_mr_li_id=sc.root_lines[0], jute_po_li_id=sc.po_lines[0],
              actual_item_id=sc.items[0], challan_item_id=sc.items[0], actual_qty=66,
              actual_weight=9700, challan_quantity=66, challan_weight=9900,
              actual_moisture="24.00", shortage_kgs=388, accepted_weight=9312, rate=12850,
              actual_rate=12850, claim_rate=140, claim_quality="grade down",
              total_price=1196592.00, active=1, **common)
    db.insert("jute_mr_li", jute_mr_li_id=sc.root_lines[1], jute_po_li_id=sc.po_lines[1],
              actual_item_id=sc.items[1], challan_item_id=sc.items[1], actual_qty=10,
              actual_weight=1470, challan_quantity=10, challan_weight=1500,
              actual_moisture="22.00", shortage_kgs=29, accepted_weight=1441, rate=12650,
              actual_rate=12650, claim_rate=50, claim_quality="grade down",
              total_price=182286.50, active=1, **common)
    return sc


@pytest.fixture
def scenario(fake_db) -> Scenario:
    """The BALE scenario on a fresh fake database."""
    return build_scenario(fake_db)
