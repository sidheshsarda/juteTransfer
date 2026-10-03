"""Self-tests of tests/fake_mysql.py. The in-memory stand-in for the sls MySQL
database must behave like MySQL wherever the app relies on it -- and fail
loudly wherever it cannot."""
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from src.jutetransfer import transfer
from src.jutetransfer.config import DatabaseConfig
from src.jutetransfer.database import DatabaseConnection
from src.jutetransfer.queries import get_source_mr_full

from .fake_mysql import (  # noqa: F401  (fake_db / scenario are fixtures)
    DEFAULT_NOW,
    SNAPSHOT_PATH,
    FakeMySQLError,
    HarnessError,
    build_scenario,
    fake_db,
    scenario,
    translate,
)

pytestmark = pytest.mark.usefixtures("fake_db")


@contextmanager
def harness_error(db, match):
    """The statement must be refused loudly AND be on record for the fixture's
    teardown check (which this then clears: the refusal was the point)."""
    with pytest.raises(HarnessError, match=match):
        yield
    assert db.harness_errors, "a harness error must also be recorded"
    db.harness_errors.clear()


def one(db, sql, **params):
    rows = db.execute(sql, **params)
    assert len(rows) == 1
    return rows[0]


# --- schema ------------------------------------------------------------------

def test_every_snapshot_table_and_column_exists(fake_db):
    import json
    tables = json.loads(SNAPSHOT_PATH.read_text())["tables"]
    assert len(tables) >= 24
    for table, cols in tables.items():
        info = fake_db.raw.execute(f'PRAGMA table_info("{table}")').fetchall()
        assert [c[1] for c in info] == [c["column"] for c in cols], table


def test_auto_increment_primary_keys(fake_db):
    first = fake_db.insert("jute_mukam_mst", mukam_name="A")
    assert fake_db.insert("jute_mukam_mst", mukam_name="B") == first + 1
    assert fake_db.insert("jute_mukam_mst", mukam_id=341, mukam_name="C") == 341
    assert fake_db.insert("jute_mukam_mst", mukam_name="D") == 342
    fake_db.delete("jute_mukam_mst", mukam_id=342)
    assert fake_db.insert("jute_mukam_mst", mukam_name="E") == 343   # never reused


def test_column_defaults_and_current_timestamp(fake_db):
    mr = fake_db.insert("jute_mr", branch_id=29)
    assert fake_db.row("jute_mr", jute_mr_id=mr)["transfer_mode"] == 0
    li = fake_db.insert("jute_po_li", jute_po_id=1)
    row = fake_db.row("jute_po_li", jute_po_li_id=li)
    assert row["active"] == 1
    assert row["updated_date_time"] == DEFAULT_NOW == fake_db.now


def test_required_column_without_default_is_refused(fake_db):
    with pytest.raises(FakeMySQLError, match="'source' cannot be null") as exc:
        fake_db.insert("jute_po_status_log", jute_po_id=1, new_status_id=5)
    assert exc.value.errno == 1048 and exc.value.column == "source"
    with pytest.raises(FakeMySQLError, match="claim_rate"):
        fake_db.execute("INSERT INTO jute_mr_li (jute_mr_id, claim_rate) VALUES (1, NULL)")
    assert fake_db.count("jute_mr_li") == 0


def test_unique_keys(fake_db):
    cols = dict(co_address1="x", co_address2="y", co_zipcode=1, country_id=1, state_id=1)
    fake_db.insert("co_mst", co_name="ONE", co_prefix="EJM", **cols)
    with pytest.raises(FakeMySQLError, match="Duplicate entry") as exc:
        fake_db.insert("co_mst", co_name="TWO", co_prefix="ejm", **cols)   # ci collation
    assert exc.value.errno == 1062


# --- STRICT mode ---------------------------------------------------------------

def test_strict_over_long_string(fake_db):
    assert fake_db.insert("jute_po", close_type="TRANSFER")          # varchar(10)
    with pytest.raises(FakeMySQLError, match="Data too long for column 'close_type'") as exc:
        fake_db.insert("jute_po", close_type="TRANSFERRED")
    assert exc.value.errno == 1406
    fake_db.insert("jute_po_li", marka="M" * 50)
    with pytest.raises(FakeMySQLError, match="'marka'"):
        fake_db.execute("INSERT INTO jute_po_li (jute_po_id, marka) VALUES (1, :m)", m="M" * 51)
    assert fake_db.count("jute_po_li") == 1


def test_strict_decimal_overflow_and_rounding(fake_db):
    ok = fake_db.insert("jute_po", jute_po_value=Decimal("99999999.99"))
    assert fake_db.row("jute_po", jute_po_id=ok)["jute_po_value"] == Decimal("99999999.99")
    for too_big in (100000000, 99999999.995, -100000000.0):
        with pytest.raises(FakeMySQLError, match="Out of range.*'jute_po_value'") as exc:
            fake_db.insert("jute_po", jute_po_value=too_big)
        assert exc.value.errno == 1264
    # stored at the column's scale, half-up -- 1.005 is 1.01 in MySQL
    rounded = fake_db.insert("jute_po", jute_po_value=1.005)
    assert fake_db.row("jute_po", jute_po_id=rounded)["jute_po_value"] == Decimal("1.01")
    assert one(fake_db, "SELECT jute_po_value + 0 AS v FROM jute_po WHERE jute_po_id = :i",
               i=rounded)["v"] == 1.01
    assert fake_db.count("jute_po") == 2


def test_strict_integer_columns(fake_db):
    assert fake_db.row("jute_po", jute_po_id=fake_db.insert("jute_po", po_no="12"))["po_no"] == 12
    assert fake_db.row("jute_po", jute_po_id=fake_db.insert("jute_po", po_no=12.5))["po_no"] == 13
    for bad in ("PO-12", "", 2 ** 31):
        with pytest.raises(FakeMySQLError, match="'po_no'"):
            fake_db.insert("jute_po", po_no=bad)
    with pytest.raises(FakeMySQLError, match="'contract_no'"):      # sales_invoice: BIGINT
        fake_db.insert("sales_invoice", round_off=0, contract_no="LC/7")


def test_strict_dates(fake_db):
    with pytest.raises(FakeMySQLError, match="Incorrect date value for column 'po_date'") as exc:
        fake_db.insert("jute_po", po_date="31-08-2026")
    assert exc.value.errno == 1292
    with pytest.raises(FakeMySQLError, match="'po_date'"):
        fake_db.insert("jute_po", po_date=20260831)
    assert fake_db.count("jute_po") == 0


# --- values in, values out -----------------------------------------------------

def test_values_come_back_as_mysql_connector_returns_them(fake_db):
    mr = fake_db.insert(
        "jute_mr", jute_mr_date=date(2026, 9, 1), updated_date_time=datetime(2026, 9, 1, 8, 5, 9),
        in_time=time(9, 15), party_id=8646, total_amount=Decimal("1378878.50"), qc_check=True,
        branch_id=np.int64(29), net_weight=np.float64(11250.0))
    row = fake_db.row("jute_mr", jute_mr_id=mr)
    assert row["jute_mr_date"] == date(2026, 9, 1) and type(row["jute_mr_date"]) is date
    assert row["updated_date_time"] == datetime(2026, 9, 1, 8, 5, 9)
    assert row["in_time"] == timedelta(hours=9, minutes=15)        # TIME -> timedelta
    assert row["party_id"] == "8646"                               # VARCHAR column
    assert row["total_amount"] == 1378878.5 and type(row["total_amount"]) is float
    assert (row["qc_check"], row["branch_id"], row["net_weight"]) == (1, 29, 11250.0)
    assert row["jute_mr_date"] is not None and row["challan_date"] is None

    li = fake_db.insert("jute_mr_li", jute_mr_id=mr, total_price=182286.5, claim_rate=50)
    price = fake_db.row("jute_mr_li", jute_mr_li_id=li)["total_price"]
    assert price == Decimal("182286.50") and str(price) == "182286.50"


def test_the_app_reads_the_same_types_through_sqlalchemy(scenario):
    with DatabaseConnection.get_transaction() as conn:
        mr = get_source_mr_full(scenario.root, conn=conn)
        po = conn.execute(text("SELECT p.po_date, p.jute_po_value FROM jute_po p "
                               "LEFT JOIN branch_mst bm ON bm.branch_id = p.branch_id "
                               "WHERE p.jute_po_id = :id"), {"id": scenario.po}).fetchone()
    assert mr["jute_gate_entry_date"] == date(2026, 8, 31)
    assert type(mr["jute_gate_entry_date"]) is date and mr["jute_mr_date"] is None
    assert mr["in_time"] == timedelta(hours=9, minutes=15)
    assert [li["total_price"] for li in mr["line_items"]] == [Decimal("1196592.00"),
                                                             Decimal("182286.50")]
    assert po[0] == date(2026, 8, 26) and po._mapping["jute_po_value"] == Decimal("2562000.00")


def test_float_columns_are_single_precision(fake_db):
    """jute_mr_li.rate is FLOAT: 129.14 * 100 = 12913.999999999998 in Python
    is stored and read back as 12914, as in MySQL."""
    li = fake_db.insert("jute_mr_li", rate=129.14 * 100, accepted_weight=1234567.89,
                        claim_rate=0.1)
    row = fake_db.row("jute_mr_li", jute_mr_li_id=li)
    assert row["rate"] == 12914.0
    assert row["accepted_weight"] == 1234570.0           # 6 significant digits
    assert row["claim_rate"] == 0.1
    assert one(fake_db, "SELECT COUNT(*) AS n FROM jute_mr_li WHERE rate = 12914")["n"] == 1
    # DOUBLE keeps every digit
    mr = fake_db.insert("jute_mr", total_amount=1385746.01)
    assert fake_db.row("jute_mr", jute_mr_id=mr)["total_amount"] == 1385746.01


def test_bind_parameters_python_types(fake_db):
    row = one(fake_db, "SELECT :d AS d, :dt AS dt, :dec AS dec, :f AS f, :b AS b, :t AS t, "
                       ":td AS td, :n AS n, :np AS np",
              d=date(2026, 4, 1), dt=datetime(2026, 4, 1, 9, 30, 15, 600000),
              dec=Decimal("12.50"), f=1.25, b=True, t=time(13, 40), td=timedelta(hours=13, minutes=40),
              n=None, np=np.int64(7))
    assert row == {"d": "2026-04-01", "dt": "2026-04-01 09:30:16", "dec": 12.5, "f": 1.25,
                   "b": 1, "t": "13:40:00", "td": "13:40:00", "n": None, "np": 7}


def test_unbindable_parameters_are_refused(fake_db):
    for bad, errno in ((float("nan"), 1054), (pd.NaT, 1292), ([1, 2], 1210), ({"a": 1}, 1210)):
        with pytest.raises(FakeMySQLError) as exc:
            fake_db.execute("SELECT :v AS v", v=bad)
        assert exc.value.errno == errno
    with pytest.raises(FakeMySQLError):
        fake_db.insert("jute_po", weight=float("nan"))


def test_date_comparisons_behave_as_in_mysql(fake_db):
    for no, day in ((7, date(2026, 3, 31)), (3, date(2026, 4, 1)), (9, date(2027, 3, 31)),
                    (1, date(2027, 4, 1))):
        fake_db.insert("jute_po", branch_id=20, po_no=no, po_date=day)
    # a datetime bound into a DATE column keeps its date only
    fake_db.insert("jute_po", branch_id=20, po_no=5, po_date=datetime(2027, 3, 31, 18, 0))
    sql = ("SELECT COALESCE(MAX(po_no), 0) AS max_no FROM jute_po WHERE branch_id = :bid "
           "AND po_date BETWEEN :fy_start AND :fy_end")
    assert one(fake_db, sql, bid=20, fy_start="2026-04-01", fy_end="2027-03-31")["max_no"] == 9
    assert one(fake_db, sql, bid=20, fy_start="2025-04-01", fy_end="2026-03-31")["max_no"] == 7
    assert one(fake_db, sql, bid=20, fy_start="2027-04-01", fy_end="2028-03-31")["max_no"] == 1
    assert one(fake_db, sql, bid=99, fy_start="2026-04-01", fy_end="2027-03-31")["max_no"] == 0
    assert [r["po_no"] for r in fake_db.execute(
        "SELECT po_no FROM jute_po WHERE po_date = :d ORDER BY po_no", d=date(2027, 3, 31))] == [5, 9]


# --- MySQL dialect ----------------------------------------------------------------

def test_now_is_a_frozen_clock(fake_db):
    assert one(fake_db, "SELECT NOW() AS n, CURRENT_TIMESTAMP AS c")["n"] == "2026-10-02 11:30:00"
    fake_db.execute("INSERT INTO jute_po (po_no, closed_date, updated_date_time) "
                    "VALUES (1, NOW(), NOW())")
    assert fake_db.rows("jute_po")[0]["closed_date"] == DEFAULT_NOW
    assert fake_db.tick(minutes=5) == datetime(2026, 10, 2, 11, 35)
    fake_db.execute("UPDATE jute_po SET closed_date = NOW() WHERE po_no = 1")
    row = fake_db.rows("jute_po")[0]
    assert row["closed_date"] == datetime(2026, 10, 2, 11, 35)
    assert row["updated_date_time"] == DEFAULT_NOW
    fake_db.set_now(datetime(2027, 1, 1, 0, 0, 1))
    assert one(fake_db, "SELECT NOW() AS n")["n"] == "2027-01-01 00:00:01"


def test_for_update_is_accepted(scenario):
    sql = "SELECT status_id, branch_mr_no FROM jute_mr WHERE jute_mr_id = :id FOR UPDATE"
    assert "FOR UPDATE" not in translate(sql)
    assert one(scenario.db, sql, id=scenario.root) == {"status_id": 13, "branch_mr_no": None}
    assert translate("SELECT 1 FROM t LOCK IN SHARE MODE").strip() == "SELECT 1 FROM t"


def test_last_insert_id(fake_db):
    with DatabaseConnection.get_transaction() as conn:
        first = DatabaseConnection.execute_insert_returning_id(
            conn, "INSERT INTO jute_po (po_no, jute_po_value) VALUES (:n, :v)", {"n": 1, "v": 2.675})
        second = DatabaseConnection.execute_insert_returning_id(
            conn, "INSERT INTO jute_po (po_no) VALUES (:n)", {"n": 2})
        line = DatabaseConnection.execute_insert_returning_id(
            conn, "INSERT INTO jute_po_li (jute_po_id, rate) VALUES (:p, :r)",
            {"p": second, "r": 129.14 * 100})
    assert second == first + 1
    assert fake_db.row("jute_po", jute_po_id=first)["jute_po_value"] == Decimal("2.68")
    assert fake_db.row("jute_po_li", jute_po_li_id=line)["jute_po_id"] == second


def test_multi_table_delete(fake_db):
    """transfer._delete_invoice: DELETE alias FROM ... JOIN ... WHERE ..."""
    for invoice in (1, 2):
        for _ in range(2):
            dtl = fake_db.insert("sales_invoice_dtl", invoice_id=invoice)
            fake_db.insert("sales_invoice_jute_dtl", invoice_line_item_id=dtl, claim_rate=invoice)
    deleted = fake_db.execute("""
        DELETE sijd FROM sales_invoice_jute_dtl sijd
        JOIN sales_invoice_dtl sid
          ON sid.invoice_line_item_id = sijd.invoice_line_item_id
        WHERE sid.invoice_id = :id
    """, id=1)
    assert deleted == 2
    assert [r["claim_rate"] for r in fake_db.rows("sales_invoice_jute_dtl")] == [2.0, 2.0]
    assert fake_db.count("sales_invoice_dtl") == 4
    # the joined table can be the target too
    assert fake_db.execute("DELETE sid FROM sales_invoice_jute_dtl sijd JOIN sales_invoice_dtl sid "
                           "ON sid.invoice_line_item_id = sijd.invoice_line_item_id") == 2
    assert [r["invoice_id"] for r in fake_db.rows("sales_invoice_dtl")] == [1, 1]


def test_regexp_cast_substring_position(scenario):
    db = scenario.db
    row = one(db, "SELECT 'J207' REGEXP '^J[0-9]+$' AS a, 'S207' REGEXP '^J[0-9]+$' AS b, "
                  "'j207' REGEXP :p AS c, NULL REGEXP 'x' AS d", p="^J[0-9]+$")
    assert row == {"a": 1, "b": 0, "c": 1, "d": None}
    row = one(db, "SELECT CAST('0012' AS UNSIGNED) AS a, CAST('abc' AS UNSIGNED) AS b, "
                  "CAST(NULL AS SIGNED) AS c, CAST(12 AS CHAR) AS d")
    assert row == {"a": 12, "b": 0, "c": None, "d": "12"}
    row = one(db, "SELECT SUBSTRING('J207', 2) AS a, SUBSTRING('ABCDEF', 2, 3) AS b, "
                  "SUBSTRING('ABC', 0) AS c, SUBSTRING('ABCDEF', -2) AS d, SUBSTRING(NULL, 1) AS e")
    assert row == {"a": "207", "b": "BCD", "c": "", "d": "EF", "e": None}
    row = one(db, "SELECT POSITION('/' IN 'SEP/0007') AS a, POSITION('x' IN 'abc') AS b, "
                  "CAST(SUBSTRING('SEP/0007', POSITION('/' IN 'SEP/0007') + 1) AS UNSIGNED) AS c")
    assert row == {"a": 4, "b": 0, "c": 7}
    # the app's own statements: numeric order, not string order ('J99' < 'J207')
    with DatabaseConnection.get_transaction() as conn:
        assert transfer._generate_supp_code(conn, scenario.a_co) == "J208"
        assert transfer._generate_supp_code(conn, scenario.b_co) == "J001"
        db.insert("sales_invoice", branch_id=20, challan_no="SEP/0009", round_off=0,
                  challan_date=date(2026, 9, 30))
        db.insert("sales_invoice", branch_id=20, challan_no="SEP/0010", round_off=0,
                  challan_date=date(2026, 9, 1))
        db.insert("sales_invoice", branch_id=20, challan_no="AUG/0031", round_off=0,
                  challan_date=date(2026, 8, 31))
        assert transfer._get_next_challan_no(conn, 20, date(2026, 9, 3)) == "SEP/0011"
        assert transfer._get_next_challan_no(conn, 20, date(2026, 10, 1)) == "OCT/0001"


def test_concat_null_safe_equal_least_greatest_abs(fake_db):
    row = one(fake_db, "SELECT CONCAT('Status-', 13) AS a, CONCAT('a', NULL, 'b') AS b, "
                       "CONCAT('x', 1.5, :s) AS c", s="y")
    assert row == {"a": "Status-13", "b": None, "c": "x1.5y"}
    row = one(fake_db, "SELECT (NULL <=> NULL) AS a, (1 <=> NULL) AS b, (2 <=> 2) AS c, "
                       "(:p <=> :q) AS d", p=None, q=None)
    assert row == {"a": 1, "b": 0, "c": 1, "d": 1}
    row = one(fake_db, "SELECT LEAST(3, 1, 2) AS a, GREATEST(3, 1, 2) AS b, LEAST(1, NULL) AS c, "
                       "GREATEST(NULL, 5) AS d, ABS(100 - 100.0004) < 0.001 AS e, ABS(-3) AS f")
    assert row == {"a": 1, "b": 3, "c": None, "d": None, "e": 1, "f": 3}


def test_boolean_expressions_in_order_by(fake_db):
    db = fake_db
    db.insert("jute_lorry_mst", jute_lorry_type_id=1, co_id=None, lorry_type="SHARED", weight=100)
    db.insert("jute_lorry_mst", jute_lorry_type_id=2, co_id=74, lorry_type="OWN", weight=100)
    db.insert("jute_lorry_mst", jute_lorry_type_id=3, co_id=74, lorry_type="BIG", weight=200)
    rows = db.execute("SELECT jute_lorry_type_id FROM jute_lorry_mst WHERE (co_id = :co "
                      "OR co_id IS NULL) AND ABS(weight - :w) < 0.001 "
                      "ORDER BY (co_id IS NULL), jute_lorry_type_id", co=74, w=100.0)
    assert [r["jute_lorry_type_id"] for r in rows] == [2, 1]
    for map_id, supplier in ((1, 2397), (2, 2430), (3, 2500)):
        db.insert("jute_supp_party_map", map_id=map_id, co_id=2, jute_supplier_id=supplier,
                  party_id=8646, updated_by=1)
    sql = ("SELECT jute_supplier_id FROM jute_supp_party_map WHERE co_id = :co AND party_id = :pid "
           "ORDER BY (jute_supplier_id = :pref) DESC, map_id LIMIT 1")
    assert one(db, sql, co=2, pid=8646, pref=2430)["jute_supplier_id"] == 2430
    assert one(db, sql, co=2, pid=8646, pref=0)["jute_supplier_id"] == 2397


def test_division_never_truncates(scenario):
    row = one(scenario.db, "SELECT 7 / 2 AS a, 9312 * 12850 / 100 AS b, 1 / 0 AS c")
    assert row == {"a": 3.5, "b": 1196592.0, "c": None}
    row = one(scenario.db, "SELECT shortage_kgs / 100 AS q FROM jute_mr_li WHERE jute_mr_li_id = :id",
              id=scenario.root_lines[0])
    assert row["q"] == 3.88                                        # INT / INT


def test_round_is_mysql_exact_value_rounding(fake_db):
    """ROUND() as MySQL rounds a DECIMAL: half away from zero on the exact
    value, even when SQLite only has a float just below the tie."""
    row = one(fake_db, "SELECT ROUND(2.5) AS a, ROUND(-2.5) AS b, ROUND(1.005, 2) AS c, "
                       "ROUND(1234.5678, -2) AS d, ROUND(NULL) AS e, ROUND(7) AS f, "
                       "ROUND(12.344, 2) AS g, ROUND(NULL, 2) AS h")
    assert row == {"a": 3.0, "b": -3.0, "c": 1.01, "d": 1200.0, "e": None, "f": 7,
                   "g": 12.34, "h": None}
    # DECIMAL arithmetic is exact in MySQL; SQLite adds floats
    row = one(fake_db, "SELECT 991403.33 + 606341.97 + 12863.20 AS s, "
                       "ROUND(991403.33 + 606341.97 + 12863.20) AS r")
    assert row["s"] < 1610608.5                    # the float: 1610608.4999999998
    assert row["r"] == 1610609.0                   # MySQL's exact 1610608.50, rounded up
    for price in (991403.33, 606341.97, 12863.20):
        fake_db.insert("jute_mr_li", jute_mr_id=1, claim_rate=0, total_price=price)
    assert one(fake_db, "SELECT ROUND(COALESCE(SUM(total_price), 0), 0) AS r FROM jute_mr_li "
                        "WHERE jute_mr_id = 1")["r"] == 1610609.0
    with harness_error(fake_db, "could not run a statement"):
        fake_db.execute("SELECT ROUND('12.5') AS x")


def test_literals_identifiers_and_comments(fake_db):
    fake_db.insert("jute_mukam_mst", mukam_id=9, mukam_name="O'BRIEN / CO")
    row = one(fake_db, """
        SELECT `mukam_name` AS `Mukam Name`,      -- trailing comment with a / and a '
               'it''s' AS a, 'it\\'s' AS b, "double" AS c,
               'NOW() FOR UPDATE /' AS d          # another comment
        FROM jute_mukam_mst /* block ' comment */ WHERE mukam_name = "O'BRIEN / CO"
    """)
    assert row == {"Mukam Name": "O'BRIEN / CO", "a": "it's", "b": "it's", "c": "double",
                   "d": "NOW() FOR UPDATE /"}


def test_string_comparison_is_case_insensitive(scenario):
    assert one(scenario.db, "SELECT co_id FROM co_mst WHERE co_prefix = 'ejm'")["co_id"] == 2
    assert one(scenario.db, "SELECT COUNT(*) AS n FROM party_mst WHERE supp_name LIKE 'honeywell%'"
               )["n"] == 1


def test_result_surface_the_app_uses(scenario):
    with DatabaseConnection.get_connection() as conn:
        result = conn.execute(text("SELECT co_id, co_prefix FROM co_mst ORDER BY co_id"))
        first = result.fetchone()
        assert (first[0], first[1]) == (2, "EJM")
        assert dict(first._mapping) == {"co_id": 2, "co_prefix": "EJM"}
        assert first._mapping.get("missing") is None
        assert [r[0] for r in result.fetchall()] == [27, 74]
        assert conn.execute("SELECT co_name FROM co_mst WHERE co_id = 99"
                            if False else text("SELECT co_name FROM co_mst WHERE co_id = 99")
                            ).fetchone() is None
        assert conn.execute(text("SELECT COUNT(*) FROM co_mst")).scalar() == 3
        assert conn.execute(text("SELECT MAX(co_id) FROM co_mst WHERE co_id > 500")).scalar() is None
        changed = conn.execute(text("UPDATE branch_mst SET active = 1 WHERE co_id IN (2, 74)"))
        assert changed.rowcount == 2                 # matched rows, changed or not
        assert conn.execute(text("DELETE FROM branch_mst WHERE co_id = 99")).rowcount == 0
    assert scenario.db.count("branch_mst") == 3


# --- honesty ------------------------------------------------------------------------

def test_unknown_function_table_or_column_fails_loudly(fake_db):
    with harness_error(fake_db, r"no such function: DATE_FORMAT[\s\S]*\[statement\] SELECT DATE_FORMAT"):
        fake_db.execute("SELECT DATE_FORMAT(NOW(), '%Y') AS y")
    with harness_error(fake_db, r"no such column: co_id[\s\S]*FROM jute_po"):
        fake_db.execute("SELECT co_id FROM jute_po")
    with harness_error(fake_db, "no such table: jute_po_transfer"):
        fake_db.execute("SELECT 1 FROM jute_po_transfer")
    with harness_error(fake_db, "syntax error"):
        fake_db.execute("UPDATE jute_mr mr JOIN jute_po p ON p.jute_po_id = mr.po_id SET mr.po_id = 1")
    with pytest.raises(HarnessError, match="no column"):
        fake_db.insert("jute_po", co_id=2)
    with pytest.raises(HarnessError, match="no table"):
        fake_db.rows("jute_po_transfer")


def test_constructs_that_mean_something_else_in_sqlite_are_refused(fake_db):
    with harness_error(fake_db, "logical OR in MySQL"):
        fake_db.execute("SELECT 1 || 0 AS x")
    with harness_error(fake_db, r"CAST\(\.\.\. AS DATE\) is not emulated"):
        fake_db.execute("SELECT CAST('2026-08-31' AS DATE) AS d")
    with harness_error(fake_db, "several tables"):
        fake_db.execute("DELETE a, b FROM sales_invoice_dtl a JOIN sales_invoice_jute_dtl b "
                        "ON a.invoice_line_item_id = b.invoice_line_item_id")
    assert "'1 || 0'" in translate("SELECT '1 || 0' AS x")          # literals are left alone


def test_update_that_depends_on_mysql_left_to_right_assignment_is_refused(scenario):
    db = scenario.db
    with harness_error(db, "reads column 'total_amount' after assigning it"):
        db.execute("UPDATE jute_mr SET total_amount = 5, net_total = total_amount - 1 "
                   "WHERE jute_mr_id = :id", id=scenario.root)
    # the app's own shape is fine: subqueries, bind parameters and other columns
    changed = db.execute("""
        UPDATE jute_mr SET status_id = :status_id, bill_pass_date = :mr_date, jute_mr_date = :mr_date,
            total_amount = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li
                            WHERE jute_mr_id = :id AND (active = 1 OR active IS NULL)),
            net_total = (SELECT ROUND(COALESCE(SUM(total_price), 0), 0) FROM jute_mr_li
                         WHERE jute_mr_id = :id AND (active = 1 OR active IS NULL)) - claim_amount,
            updated_date_time = NOW()
        WHERE jute_mr_id = :id""", id=scenario.root, status_id=3, mr_date=date(2026, 9, 3))
    assert changed == 1
    row = db.row("jute_mr", jute_mr_id=scenario.root)
    assert (row["total_amount"], row["net_total"]) == (1378879.0, None)   # claim_amount is NULL


def test_mysql_error_1093_is_reproduced(scenario):
    db = scenario.db
    for sql in ("UPDATE jute_po SET po_no = (SELECT MAX(po_no) + 1 FROM jute_po) WHERE jute_po_id = 1",
                "DELETE FROM jute_po WHERE po_no = (SELECT MAX(p.po_no) FROM jute_po p)"):
        with pytest.raises(FakeMySQLError, match="can't specify target table 'jute_po'") as exc:
            db.execute(sql)
        assert exc.value.errno == 1093
    # legal in MySQL: the subquery is materialised as a derived table
    db.execute("UPDATE jute_po SET po_no = (SELECT n FROM (SELECT MAX(po_no) + 1 AS n FROM jute_po) x) "
               "WHERE jute_po_id = :id", id=scenario.po)
    assert db.row("jute_po", jute_po_id=scenario.po)["po_no"] == 12
    assert db.count("jute_po") == 1


def test_second_connection_inside_a_transaction_is_refused(scenario):
    with DatabaseConnection.get_transaction() as conn:
        conn.execute(text("UPDATE jute_mr SET status_id = 3 WHERE jute_mr_id = :id"),
                     {"id": scenario.root})
        with harness_error(scenario.db, "second connection"):
            DatabaseConnection.execute_query("SELECT status_id FROM jute_mr")
    assert scenario.db.row("jute_mr", jute_mr_id=scenario.root)["status_id"] == 3   # outer commit kept


def test_the_real_database_is_out_of_reach(fake_db):
    assert DatabaseConnection.get_engine() is fake_db.engine
    with harness_error(fake_db, "real MySQL database"):
        DatabaseConfig.get_connection_string()


# --- transactions -------------------------------------------------------------------

def test_transaction_commits_on_clean_exit(fake_db):
    with DatabaseConnection.get_transaction() as conn:
        conn.execute(text("INSERT INTO jute_mukam_mst (mukam_name) VALUES ('KEPT')"))
    assert [r["mukam_name"] for r in fake_db.rows("jute_mukam_mst")] == ["KEPT"]


def test_rollback_really_undoes_every_write(scenario):
    db = scenario.db
    before = db.snapshot()
    with pytest.raises(RuntimeError, match="boom"):
        with DatabaseConnection.get_transaction() as conn:
            po_id = DatabaseConnection.execute_insert_returning_id(
                conn, "INSERT INTO jute_po (branch_id, po_no) VALUES (:b, :n)", {"b": 20, "n": 1})
            conn.execute(text("INSERT INTO jute_po_li (jute_po_id, quantity) VALUES (:p, 62)"),
                         {"p": po_id})
            conn.execute(text("UPDATE jute_mr SET po_id = :p, status_id = 3"), {"p": po_id})
            conn.execute(text("DELETE FROM jute_mr_li WHERE jute_mr_id = :id"), {"id": scenario.root})
            conn.execute(text("DELETE FROM jute_supp_party_map"))
            assert conn.execute(text("SELECT COUNT(*) FROM jute_mr_li")).scalar() == 0
            raise RuntimeError("boom")
    assert db.diff(before, db.snapshot()) == []
    assert db.snapshot() == before
    # InnoDB does not hand a rolled-back auto-increment value out again
    assert db.insert("jute_po", branch_id=20) == po_id + 1


def test_a_failed_statement_does_not_end_the_transaction(fake_db):
    with DatabaseConnection.get_transaction() as conn:
        conn.execute(text("INSERT INTO jute_mukam_mst (mukam_name) VALUES ('before')"))
        with pytest.raises(FakeMySQLError):
            conn.execute(text("INSERT INTO jute_po (close_type) VALUES ('MUCH TOO LONG')"))
        conn.execute(text("INSERT INTO jute_mukam_mst (mukam_name) VALUES ('after')"))
    assert [r["mukam_name"] for r in fake_db.rows("jute_mukam_mst")] == ["before", "after"]
    assert fake_db.count("jute_po") == 0


def test_uncommitted_plain_connection_is_rolled_back(fake_db):
    with DatabaseConnection.get_connection() as conn:
        conn.execute(text("INSERT INTO jute_mukam_mst (mukam_name) VALUES ('lost')"))
    assert fake_db.count("jute_mukam_mst") == 0
    assert DatabaseConnection.execute_non_query(
        "INSERT INTO jute_mukam_mst (mukam_name) VALUES (:n)", {"n": "kept"}) == 1
    assert fake_db.count("jute_mukam_mst") == 1


def test_a_read_only_transaction_refuses_writes_until_it_ends(scenario):
    """START TRANSACTION READ ONLY (the scripts' planning pass): MySQL refuses
    a write with error 1792 until that transaction is over."""
    sc = scenario
    write = text("UPDATE jute_po SET remarks = 'x' WHERE jute_po_id = :id")
    with DatabaseConnection.get_engine().connect() as conn:
        conn.execute(text("START TRANSACTION READ ONLY"))
        assert conn.execute(text("SELECT COUNT(*) FROM jute_po")).scalar() == 1
        with pytest.raises(FakeMySQLError) as refused:
            conn.execute(write, {"id": sc.po})
        assert refused.value.errno == 1792
        conn.rollback()
    with DatabaseConnection.get_transaction() as conn:
        conn.execute(write, {"id": sc.po})                # a new transaction may write again
    assert sc.db.row("jute_po", jute_po_id=sc.po)["remarks"] == "x"


def test_execute_query_returns_a_dataframe(scenario):
    df = DatabaseConnection.execute_query(
        "SELECT jute_mr_li_id, accepted_weight, total_price, updated_date_time FROM jute_mr_li "
        "WHERE jute_mr_id = :id AND (active = 1 OR active IS NULL) ORDER BY jute_mr_li_id",
        {"id": scenario.root})
    assert isinstance(df, pd.DataFrame)
    assert df["jute_mr_li_id"].tolist() == list(scenario.root_lines)
    assert df["accepted_weight"].tolist() == [9312.0, 1441.0]
    assert df["total_price"].tolist() == [Decimal("1196592.00"), Decimal("182286.50")]
    assert DatabaseConnection.execute_query("SELECT * FROM jute_issue").empty


# --- helpers ---------------------------------------------------------------------------

def test_rows_count_update_delete_helpers(scenario):
    db = scenario.db
    assert db.count("jute_mr_li", jute_mr_id=scenario.root) == 3
    assert [r["jute_mr_li_id"] for r in db.rows("jute_mr_li", active=1)] == list(scenario.root_lines)
    assert db.rows("jute_mr", src_jute_mr_id=None)[0]["jute_mr_id"] == scenario.root   # IS NULL
    assert db.update("jute_mr_li", {"jute_mr_id": scenario.root, "active": 0}, marka="X") == 1
    assert db.row("jute_mr_li", jute_mr_li_id=scenario.root_dead_line)["marka"] == "X"
    assert db.delete("jute_mr_li", active=0) == 1
    with pytest.raises(AssertionError, match="expected one"):
        db.row("jute_mr_li", jute_mr_id=scenario.root)


def test_snapshot_and_diff(scenario):
    db = scenario.db
    before = db.snapshot()
    assert set(before) == set(db.schema.columns) and db.diff(before, db.snapshot()) == []
    db.update("jute_mr", {"jute_mr_id": scenario.root}, status_id=3, updated_by=1)
    db.insert("jute_po", jute_po_id=13315, branch_id=20)
    db.delete("jute_po_li", jute_po_li_id=scenario.po_lines[1])
    assert db.diff(before, db.snapshot()) == [
        "jute_mr[28137].status_id: 13 -> 3",
        "jute_mr[28137].updated_by: 24 -> 1",
        "jute_po[13315] inserted",
        "jute_po_li[31267] deleted",
    ]
    quiet = {"jute_mr": ["updated_by"], "*": ["status_id"]}
    only = ["jute_mr", "co_mst"]
    assert db.diff(db.snapshot(only, ignore=quiet),
                   {t: [{k: v for k, v in r.items() if k not in ("updated_by", "status_id")}
                        if t == "jute_mr" else r for r in rows]
                    for t, rows in before.items() if t in only}) == []


def test_statement_log(scenario):
    db = scenario.db
    start = len(db.statements)
    db.execute("SELECT 1 AS x FROM co_mst WHERE co_id = :id FOR UPDATE", id=2)
    db.execute("UPDATE co_mst SET updated_by = 1 WHERE co_id = :id", id=2)
    assert db.statements[start:] == ["SELECT 1 AS x FROM co_mst WHERE co_id = :id FOR UPDATE",
                                     "UPDATE co_mst SET updated_by = 1 WHERE co_id = :id"]
    assert db.parameters[start:] == [{"id": 2}, {"id": 2}]
    assert db.writes(start) == ["UPDATE co_mst SET updated_by = 1 WHERE co_id = :id"]


# --- the scenario ------------------------------------------------------------------------

@pytest.mark.parametrize("uom", ["BALE", "LOOSE"])
def test_scenario_is_shaped_like_the_live_data(fake_db, uom):
    sc = build_scenario(fake_db, uom=uom)
    db = sc.db
    root = db.row("jute_mr", jute_mr_id=sc.root)
    assert (root["status_id"], root["po_id"], root["transfer_mode"]) == (13, sc.po, 0)
    assert root["src_jute_mr_id"] is None and root["branch_mr_no"] is None
    assert root["jute_mr_date"] is None                  # Pending roots carry no MR date yet
    assert (root["unit_conversion"], root["qc_check"], root["net_weight"]) == (uom, 1, 11250.0)
    assert (root["jute_gate_entry_no"], root["jute_gate_entry_date"]) == (21, date(2026, 8, 31))
    assert root["party_id"] == "207" and root["branch_id"] == sc.a_branch

    lines = db.rows("jute_mr_li", jute_mr_id=sc.root)
    assert [(l["jute_mr_li_id"], l["active"], l["accepted_weight"], l["rate"], l["claim_rate"])
            for l in lines] == [(45524, 0, 0.0, 0.0, 0.0), (45537, 1, 9312.0, 12850.0, 140.0),
                                (45538, 1, 1441.0, 12650.0, 50.0)]
    assert all(l["crop_year"] is None and l["allowable_moisture"] == 20.0 for l in lines)
    assert sc.active_lines(sc.root) == list(sc.root_lines)

    po = db.row("jute_po", jute_po_id=sc.po)
    assert (po["status_id"], po["branch_id"], po["po_no"], po["jute_uom"]) == (3, sc.a_branch, 11, uom)
    assert (po["vehicle_type_id"], po["credit_term"], po["delivery_days"], po["channel_code"]) == (
        sc.a_lorry_type, 45, 7, "DOMESTIC")
    assert [(l["percentage"], l["crop_year"]) for l in db.rows("jute_po_li", jute_po_id=sc.po)] == [
        (Decimal("80.00"), 26), (Decimal("20.00"), 26)]
    lorry = db.row("jute_lorry_mst")
    assert (lorry["co_id"], lorry["weight"]) == (sc.a_co, 100.0)

    # the forwarding companies start with a branch and a godown, nothing else
    for which in "BC":
        assert db.count("party_mst", co_id=sc.co(which)) == 0
        assert db.count("item_grp_mst", co_id=sc.co(which)) == 0
        assert db.count("jute_lorry_mst", co_id=sc.co(which)) == 0
        assert db.count("jute_supp_party_map", co_id=sc.co(which)) == 0
        assert db.row("warehouse_mst", branch_id=sc.branch(which))["warehouse_id"] == sc.godown(which)
    assert db.row("jute_supp_party_map")["party_id"] == sc.supplier_party
    ids = [sc.a_co, sc.a_branch, sc.b_co, sc.b_branch, sc.c_co, sc.c_branch, sc.supplier,
           sc.supplier_party, sc.po, sc.root, sc.mukam, sc.po_mukam, sc.a_lorry_type, sc.user]
    assert len(set(ids)) == len(ids)


def test_hand_built_hop_and_finalized_root(scenario):
    db, sc = scenario.db, scenario
    hop = sc.add_hop()
    row = db.row("jute_mr", jute_mr_id=hop)
    assert (row["branch_id"], row["src_jute_mr_id"], row["src_com_id"], row["po_id"]) == (
        sc.b_branch, sc.root, sc.a_co, None)
    party = db.row("party_mst", party_id=int(row["party_id"]))
    assert (party["co_id"], party["supp_name"]) == (sc.b_co, sc.supplier_party_name)
    lines = db.rows("jute_mr_li", jute_mr_id=hop)
    assert [(l["accepted_weight"], l["rate"], l["jute_po_li_id"]) for l in lines] == [
        (9312.0, 12850.0, None), (1441.0, 12650.0, None)]
    b_items = {i["item_id"] for i in db.rows("item_mst")} - set(sc.items)
    assert {l["actual_item_id"] for l in lines} == b_items and len(b_items) == 2
    assert sc.add_hop(root=sc.add_root()) == hop + 2 and db.count("party_mst", co_id=sc.b_co) == 1

    forwarder = sc.finalize_root()
    root = db.row("jute_mr", jute_mr_id=sc.root)
    assert (root["status_id"], root["party_id"], root["po_id"]) == (3, str(forwarder), sc.po)
    assert root["jute_mr_date"] == date(2026, 9, 3) and root["total_amount"] == 1385746.0
    assert [l["rate"] for l in db.rows("jute_mr_li", jute_mr_id=sc.root, active=1)] == [12914.0, 12713.0]
