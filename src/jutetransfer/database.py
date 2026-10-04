"""Database connection and management utilities."""

import streamlit as st
import pandas as pd
import mysql.connector
from mysql.connector import Error as MySQLError
from mysql.connector.abstracts import MySQLConnectionAbstract
from mysql.connector.pooling import PooledMySQLConnection
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from typing import Iterable, Mapping, Optional, Any, Literal, Union
from contextlib import contextmanager

from .config import DatabaseConfig


# Placeholder in a lock name for the current schema (SELECT DATABASE()): user
# locks are server-wide, and the sls MySQL server hosts every tenant.
LOCK_DB_PLACEHOLDER = "{db}"
LOCK_BUSY_MESSAGE = (
    "Another save is numbering documents at this branch right now "
    "(lock {name!r} still held after {timeout} s); please save again in a moment."
)


class DatabaseConnection:
    """Manage MySQL database connections using SQLAlchemy."""
    
    _engine = None
    
    @classmethod
    def get_engine(cls):
        """Get or create SQLAlchemy engine with connection pooling."""
        if cls._engine is None:
            connection_string = DatabaseConfig.get_connection_string()
            cls._engine = create_engine(
                connection_string,
                pool_size=DatabaseConfig.POOL_SIZE,
                max_overflow=DatabaseConfig.MAX_OVERFLOW,
                pool_pre_ping=True,  # Enable connection health checks
                echo=False,  # Set to True for SQL debugging
            )
        return cls._engine
    
    @classmethod
    @contextmanager
    def get_connection(cls):
        """Context manager for database connections."""
        connection = None
        try:
            engine = cls.get_engine()
            connection = engine.connect()
            yield connection
        except Exception:
            if connection:
                connection.rollback()
            raise
        finally:
            if connection:
                connection.close()
    
    @classmethod
    def test_connection(cls) -> tuple[bool, str]:
        """Test database connection.
        
        Returns:
            tuple: (success: bool, message: str)
        """
        try:
            with cls.get_connection() as conn:
                result = conn.execute(text("SELECT 1"))
                result.fetchone()
                return True, "Database connection successful"
        except Exception as e:
            return False, f"Database connection failed: {str(e)}"
    
    @classmethod
    def execute_query(cls, query: str, params: Optional[dict] = None) -> pd.DataFrame:
        """Execute a SELECT query and return results as DataFrame.
        
        Args:
            query: SQL query string
            params: Optional dictionary of query parameters
            
        Returns:
            pd.DataFrame: Query results
        """
        with cls.get_connection() as conn:
            if params:
                df = pd.read_sql_query(text(query), conn, params=params)
            else:
                df = pd.read_sql_query(text(query), conn)
            return df
    
    @classmethod
    @contextmanager
    def get_transaction(cls):
        """Context manager that yields a connection inside an explicit transaction.

        Commits on clean exit, rolls back on exception.
        """
        engine = cls.get_engine()
        connection = engine.connect()
        trans = connection.begin()
        try:
            yield connection
            trans.commit()
        except Exception:
            trans.rollback()
            raise
        finally:
            connection.close()

    @classmethod
    def execute_insert_returning_id(cls, conn, query: str, params: dict) -> int:
        """Execute an INSERT on an existing connection and return the auto-increment ID."""
        conn.execute(text(query), params)
        result = conn.execute(text("SELECT LAST_INSERT_ID()"))
        return result.scalar()

    @classmethod
    def execute_non_query(cls, query: str, params: Optional[dict] = None) -> int:
        """Execute INSERT, UPDATE, DELETE queries.
        
        Args:
            query: SQL query string
            params: Optional dictionary of query parameters
            
        Returns:
            int: Number of affected rows
        """
        with cls.get_connection() as conn:
            result = conn.execute(text(query), params or {})
            conn.commit()
            return result.rowcount
    
    @classmethod
    def insert_dataframe(cls, df: pd.DataFrame, table_name: str, 
                        if_exists: Literal['fail', 'replace', 'append'] = 'append') -> int:
        """Insert DataFrame into database table.
        
        Args:
            df: DataFrame to insert
            table_name: Target table name
            if_exists: How to behave if table exists ('fail', 'replace', 'append')
            
        Returns:
            int: Number of rows inserted
        """
        engine = cls.get_engine()
        rows_inserted = df.to_sql(
            name=table_name,
            con=engine,
            if_exists=if_exists,
            index=False
        )
        return rows_inserted or len(df)


def get_mysql_connector() -> Optional[Union[mysql.connector.MySQLConnection, PooledMySQLConnection, MySQLConnectionAbstract]]:
    """Get a direct MySQL connector (alternative to SQLAlchemy).
    
    Returns:
        MySQL connection object or None if connection fails
    """
    try:
        config = DatabaseConfig.get_mysql_config()
        connection = mysql.connector.connect(**config)
        if connection.is_connected():
            return connection
    except MySQLError:
        return None


def select_ids(conn, sql: str, params: Optional[dict] = None) -> list:
    """Integer ids from a plain (non-locking) read on the caller's connection."""
    return [int(r[0]) for r in conn.execute(text(sql), params or {}).fetchall()]


def delete_by_ids(conn, table: str, pk: str, ids) -> int:
    """DELETE rows by primary key on the caller's connection.

    A DELETE filtered on a column without an index (e.g.
    jute_po_li.jute_po_id, sales_invoice_jute_dtl.invoice_line_item_id)
    scans the table and, under REPEATABLE READ, locks every row of it until
    commit -- ERP saves on that table in every company would wait. Read the
    ids first (select_ids) and delete by key: only those rows are locked.
    `table` / `pk` are code constants, never user input."""
    ids = sorted({int(i) for i in ids})
    if not ids:
        return 0
    result = conn.execute(text(
        f"DELETE FROM {table} WHERE {pk} IN ({','.join(str(i) for i in ids)})"
    ))
    return result.rowcount


def lock_sort_key(name: str) -> tuple:
    """Sort key giving every process the SAME order of lock names (no two
    savers can then wait for each other): segment by segment, numeric
    segments ascending as numbers ('...:29' before '...:100', as the ERP's
    "ascending branch_id" rule), text segments as text."""
    return tuple((0, int(seg), "") if seg.isdigit() else (1, 0, seg)
                 for seg in str(name).split(":"))


def _drop_connection(conn) -> None:
    """Throw a connection away instead of returning it to the pool: the
    server then frees every user lock it still holds."""
    try:
        conn.invalidate()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def _release_locks(conn, held: list) -> None:
    """RELEASE_LOCK each name on the lock connection, then give it back;
    when that fails the connection is dropped so the server frees them."""
    try:
        for name in reversed(held):
            conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": name})
        conn.rollback()
        conn.close()
    except Exception:
        _drop_connection(conn)


@contextmanager
def named_locks(names, timeout: int = 5):
    """Hold MySQL named (user) locks for the duration of a `with` block, on a
    DEDICATED connection -- the protocol the ERP's jute PO numbering follows
    (vowerp3be src/juteProcurement/po_numbering.py):

        with named_locks([...]):                  # GET_LOCK, in lock_sort_key order
            with DatabaseConnection.get_transaction() as conn:
                ...  MAX+1 reads, inserts ...     # COMMIT / ROLLBACK
                                                  # then RELEASE_LOCK

    Every document number this app allocates (MR, gate entry, bill pass, PO,
    invoice, challan) is MAX+1 on a REPEATABLE READ snapshot, which is the
    transaction's first plain read. Taking the lock BEFORE the transaction
    opens makes that snapshot fresh (it holds everything committed before the
    lock was granted), and releasing only AFTER it ends lets the next holder
    see this save. The lock lives on its own connection because user locks
    belong to the connection, survive ROLLBACK, and a pooled connection
    returned while still holding one would block every later save.

    names    lock names; a {name: timeout} mapping gives each its own wait.
             '{db}' in a name is replaced by SELECT DATABASE() -- user locks
             are server-wide and the server hosts every tenant.
    timeout  seconds to wait for a lock not given its own (GET_LOCK's).
    Raises ValueError when a lock is still held by another connection after
    its timeout ("save again"), or when GET_LOCK fails (NULL). Yields the
    list of names held. No names: nothing is locked.
    """
    waits = dict(names) if isinstance(names, Mapping) else {n: timeout for n in names}
    waits = {str(n): int(t if t is not None else timeout) for n, t in waits.items() if n}
    if not waits:
        yield []
        return
    conn = DatabaseConnection.get_engine().connect()
    held: list = []
    try:
        try:
            if any(LOCK_DB_PLACEHOLDER in n for n in waits):
                db_name = conn.execute(text("SELECT DATABASE()")).scalar()
                if not db_name:
                    raise ValueError("Could not take the save lock: SELECT DATABASE() is empty")
                waits = {n.replace(LOCK_DB_PLACEHOLDER, str(db_name)): t for n, t in waits.items()}
            for name in sorted(waits, key=lock_sort_key):
                got = conn.execute(
                    text("SELECT GET_LOCK(:name, :timeout)"),
                    {"name": name, "timeout": waits[name]},
                ).scalar()
                if got is None:
                    raise ValueError(f"Could not take the save lock {name!r} (database error)")
                if int(got) != 1:
                    raise ValueError(LOCK_BUSY_MESSAGE.format(name=name, timeout=waits[name]))
                held.append(name)
            # End the lock connection's own transaction before the caller's
            # begins: a user lock survives ROLLBACK, the read snapshot does not.
            conn.rollback()
        except Exception:
            _release_locks(conn, held)
            conn = None
            raise
        yield list(held)
    finally:
        if conn is not None:
            _release_locks(conn, held)


@st.cache_resource
def get_cached_database_connection():
    """Cached database connection for Streamlit (use with caution)."""
    return DatabaseConnection.get_engine()


# Database query helpers
def fetch_table_data(table_name: str, 
                     limit: Optional[int] = None,
                     where_clause: Optional[str] = None,
                     order_by: Optional[str] = None) -> pd.DataFrame:
    """Fetch data from a table with optional filters.
    
    Args:
        table_name: Name of the table
        limit: Maximum number of rows to fetch
        where_clause: Optional WHERE clause (without WHERE keyword)
        order_by: Optional ORDER BY clause (without ORDER BY keyword)
        
    Returns:
        pd.DataFrame: Query results
    """
    query = f"SELECT * FROM {table_name}"
    
    if where_clause:
        query += f" WHERE {where_clause}"
    
    if order_by:
        query += f" ORDER BY {order_by}"
    
    if limit:
        query += f" LIMIT {limit}"
    
    return DatabaseConnection.execute_query(query)


def get_table_schema(table_name: str) -> pd.DataFrame:
    """Get schema information for a table.
    
    Args:
        table_name: Name of the table
        
    Returns:
        pd.DataFrame: Table schema information
    """
    query = f"DESCRIBE {table_name}"
    return DatabaseConnection.execute_query(query)


def get_all_tables() -> list[str]:
    """Get list of all tables in the database.
    
    Returns:
        list: List of table names
    """
    query = "SHOW TABLES"
    df = DatabaseConnection.execute_query(query)
    return df.iloc[:, 0].tolist()
