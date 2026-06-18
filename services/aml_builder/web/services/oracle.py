"""
Oracle database connection pool for the AML Builder service.

Uses oracledb (the modern python-oracledb thin client) with a persistent
connection pool — the same proven pattern as the PioTech AI services.
"""

import logging
from contextlib import contextmanager
from typing import Generator, List, Optional, Tuple, Any

import oracledb

from web.services.settings import settings

logger = logging.getLogger(__name__)

# Module-level pool — created once at startup, shared across all requests
_pool: Optional[oracledb.ConnectionPool] = None


def init_pool() -> None:
    """Initialize the Oracle connection pool.

    Called once at FastAPI startup. Uses settings for all connection
    parameters — no hardcoded values.

    Raises:
        oracledb.Error: If the pool cannot be created (bad DSN, wrong credentials).
    """
    global _pool
    if _pool is not None:
        logger.info("[ORACLE] Pool already initialized — skipping.")
        return

    logger.info(
        "[ORACLE] Initializing connection pool: dsn=%s, min=%d, max=%d",
        settings.ORACLE_DSN,
        settings.ORACLE_POOL_MIN,
        settings.ORACLE_POOL_MAX,
    )

    _pool = oracledb.create_pool(
        user=settings.ORACLE_USER,
        password=settings.ORACLE_PASSWORD,
        dsn=settings.ORACLE_DSN,
        min=settings.ORACLE_POOL_MIN,
        max=settings.ORACLE_POOL_MAX,
        increment=1,
        getmode=oracledb.POOL_GETMODE_WAIT,
        timeout=30,
    )

    logger.info("[ORACLE] Connection pool initialized successfully.")


def close_pool() -> None:
    """Gracefully close the Oracle connection pool.

    Called at FastAPI shutdown to release all connections cleanly.
    """
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
        logger.info("[ORACLE] Connection pool closed.")


@contextmanager
def get_connection() -> Generator[oracledb.Connection, None, None]:
    """Acquire a connection from the pool as a context manager.

    Usage:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(...)
            conn.commit()

    Yields:
        oracledb.Connection: A live Oracle connection from the pool.

    Raises:
        RuntimeError: If the pool has not been initialized.
        oracledb.Error: If the connection cannot be acquired.
    """
    if _pool is None:
        raise RuntimeError(
            "Oracle pool is not initialized. Call init_pool() at startup."
        )

    conn = _pool.acquire()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.release(conn)


def run_readonly(sql: str, params: Optional[dict] = None) -> Tuple[List[str], List[tuple]]:
    """Execute a read-only SELECT query and return column names + rows.

    Args:
        sql (str): The SELECT query to execute. Must start with SELECT or WITH.
        params (Optional[dict]): Named bind parameters for the query.

    Returns:
        Tuple[List[str], List[tuple]]:
            - List of column names (lowercased).
            - List of result rows as tuples.

    Raises:
        ValueError: If the query is not a SELECT/WITH statement.
        oracledb.Error: On Oracle execution error.
    """
    import re

    stripped = sql.strip()
    if not re.match(r"^\s*(SELECT|WITH)\s+", stripped, re.IGNORECASE):
        raise ValueError(
            f"run_readonly only accepts SELECT queries. Got: {stripped[:60]}..."
        )

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(stripped, params or {})
        columns = [col[0].lower() for col in cursor.description]
        rows = cursor.fetchall()
        return columns, rows


def run_write(sql: str, params: Optional[dict] = None) -> int:
    """Execute a DML statement (INSERT/UPDATE/DELETE) and commit.

    Args:
        sql (str): The DML statement to execute.
        params (Optional[dict]): Named bind parameters.

    Returns:
        int: Number of rows affected.

    Raises:
        oracledb.Error: On Oracle execution error (triggers rollback).
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params or {})
        affected = cursor.rowcount
        conn.commit()
        logger.debug("[ORACLE] DML committed. Rows affected: %d", affected)
        return affected


def run_write_many(sql: str, params_list: List[dict]) -> int:
    """Execute a batch DML statement (executemany) and commit.

    More efficient than calling run_write() in a loop for bulk inserts.

    Args:
        sql (str): The DML statement template.
        params_list (List[dict]): List of parameter dicts, one per row.

    Returns:
        int: Total number of rows affected across all executions.

    Raises:
        oracledb.Error: On Oracle execution error (triggers rollback).
    """
    if not params_list:
        logger.debug("[ORACLE] run_write_many called with empty params_list — skipping.")
        return 0

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.executemany(sql, params_list)
        affected = cursor.rowcount
        conn.commit()
        logger.debug(
            "[ORACLE] Batch DML committed. Rows affected: %d", affected
        )
        return affected


def call_procedure(procedure_name: str, params: Optional[dict] = None) -> None:
    """Execute an Oracle stored procedure and commit.

    Used specifically to call FILL_PIO_AML_CUSTOMERS after scenario creation.

    Args:
        procedure_name (str): The procedure name (e.g., 'FILL_PIO_AML_CUSTOMERS').
        params (Optional[dict]): Named bind parameters for the procedure.

    Raises:
        oracledb.Error: On execution error (triggers rollback).
    """
    pl_sql = f"BEGIN {procedure_name}; END;"
    if params:
        # Build parameterized block if needed
        param_str = ", ".join(f":{k} => :{k}" for k in params)
        pl_sql = f"BEGIN {procedure_name}({param_str}); END;"

    logger.info("[ORACLE] Calling procedure: %s", procedure_name)

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(pl_sql, params or {})
        conn.commit()
        logger.info("[ORACLE] Procedure '%s' completed successfully.", procedure_name)
