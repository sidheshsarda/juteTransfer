"""Database connection and management utilities."""

import streamlit as st
import pandas as pd
import mysql.connector
from mysql.connector import Error as MySQLError
from mysql.connector.abstracts import MySQLConnectionAbstract
from mysql.connector.pooling import PooledMySQLConnection
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from typing import Optional, Any, Literal, Union
from contextlib import contextmanager
import logging

from .config import DatabaseConfig

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class DatabaseConnection:
    """Manage MySQL database connections using SQLAlchemy."""
    
    _engine = None
    
    @classmethod
    def get_engine(cls):
        """Get or create SQLAlchemy engine with connection pooling."""
        if cls._engine is None:
            try:
                connection_string = DatabaseConfig.get_connection_string()
                cls._engine = create_engine(
                    connection_string,
                    pool_size=DatabaseConfig.POOL_SIZE,
                    max_overflow=DatabaseConfig.MAX_OVERFLOW,
                    pool_pre_ping=True,  # Enable connection health checks
                    echo=False,  # Set to True for SQL debugging
                )
                logger.info("Database engine created successfully")
            except Exception as e:
                logger.error(f"Failed to create database engine: {e}")
                raise
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
        except Exception as e:
            logger.error(f"Database connection error: {e}")
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
        try:
            with cls.get_connection() as conn:
                if params:
                    df = pd.read_sql_query(text(query), conn, params=params)
                else:
                    df = pd.read_sql_query(text(query), conn)
                return df
        except Exception as e:
            logger.error(f"Query execution error: {e}")
            raise
    
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
        try:
            with cls.get_connection() as conn:
                result = conn.execute(text(query), params or {})
                conn.commit()
                return result.rowcount
        except Exception as e:
            logger.error(f"Non-query execution error: {e}")
            raise
    
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
        try:
            engine = cls.get_engine()
            rows_inserted = df.to_sql(
                name=table_name,
                con=engine,
                if_exists=if_exists,
                index=False
            )
            logger.info(f"Inserted {rows_inserted} rows into {table_name}")
            return rows_inserted or len(df)
        except Exception as e:
            logger.error(f"DataFrame insertion error: {e}")
            raise


def get_mysql_connector() -> Optional[Union[mysql.connector.MySQLConnection, PooledMySQLConnection, MySQLConnectionAbstract]]:
    """Get a direct MySQL connector (alternative to SQLAlchemy).
    
    Returns:
        MySQL connection object or None if connection fails
    """
    try:
        config = DatabaseConfig.get_mysql_config()
        connection = mysql.connector.connect(**config)
        if connection.is_connected():
            logger.info("MySQL connector connection established")
            return connection
    except MySQLError as e:
        logger.error(f"MySQL connector error: {e}")
        return None


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
