"""
Oracle database connection pool for the AML Builder service.

Uses oracledb (the modern python-oracledb thin client) with a persistent
connection pool — the same proven pattern as the PioTech AI services.
"""

import logging
from contextlib import contextmanager
from typing import Generator, List, Optional, Tuple

import oracledb

from services.aml_builder.web.services.settings import settings

logger = logging.getLogger(__name__)

# Module-level pool — created once at startup, shared across all requests
_pool: Optional[oracledb.ConnectionPool] = None
_shadow_pool: Optional[oracledb.ConnectionPool] = None


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


def init_shadow_pool() -> None:
    """Initialize the secondary/shadow Oracle connection pool used for shadow testing.

    Uses SHADOW_ORACLE_* settings if provided; falls back to primary ORACLE_* settings.

    Raises:
        None: Logs error without failing if secondary pool connection fails.
    """
    global _shadow_pool
    if _shadow_pool is not None:
        logger.info("[ORACLE:SHADOW] Shadow pool already initialized — skipping.")
        return

    dsn = settings.SHADOW_ORACLE_DSN or settings.ORACLE_DSN
    user = settings.SHADOW_ORACLE_USER or settings.ORACLE_USER
    password = settings.SHADOW_ORACLE_PASSWORD or settings.ORACLE_PASSWORD

    logger.info(
        "[ORACLE:SHADOW] Initializing shadow connection pool: dsn=%s, user=%s, min=%d, max=%d",
        dsn,
        user,
        settings.SHADOW_ORACLE_POOL_MIN,
        settings.SHADOW_ORACLE_POOL_MAX,
    )

    try:
        _shadow_pool = oracledb.create_pool(
            user=user,
            password=password,
            dsn=dsn,
            min=settings.SHADOW_ORACLE_POOL_MIN,
            max=settings.SHADOW_ORACLE_POOL_MAX,
            increment=1,
            getmode=oracledb.POOL_GETMODE_WAIT,
            timeout=30,
        )
        logger.info("[ORACLE:SHADOW] Shadow connection pool initialized successfully.")
    except Exception as exc:
        logger.error("[ORACLE:SHADOW] Failed to initialize shadow pool: %s", exc)
        _shadow_pool = None


def close_pool() -> None:
    """Gracefully close the Oracle connection pool.

    Called at FastAPI shutdown to release all connections cleanly.
    """
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
        logger.info("[ORACLE] Connection pool closed.")


def close_shadow_pool() -> None:
    """Gracefully close the shadow Oracle connection pool."""
    global _shadow_pool
    if _shadow_pool is not None:
        try:
            _shadow_pool.close()
            logger.info("[ORACLE:SHADOW] Shadow connection pool closed.")
        except Exception as exc:
            logger.error("[ORACLE:SHADOW] Error closing shadow pool: %s", exc)
        _shadow_pool = None


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


@contextmanager
def atomic_connection() -> Generator[oracledb.Connection, None, None]:
    """Yield a single Oracle connection for a multi-statement atomic transaction.

    All statements executed on the yielded connection share one transaction
    boundary: a clean exit commits everything; any exception triggers a full
    rollback before re-raising. Use this for all QB writer operations so that
    a failure mid-sequence never leaves partial data in Oracle.

    Usage:
        with atomic_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(insert_sql_1, params_1)
            cursor.execute(insert_sql_2, params_2)
        # single commit happens automatically on clean exit

    Yields:
        oracledb.Connection: A live connection from the pool.

    Raises:
        RuntimeError: If the pool has not been initialized.
        oracledb.Error: On Oracle execution error (triggers automatic rollback).
    """
    if _pool is None:
        raise RuntimeError(
            "Oracle pool is not initialized. Call init_pool() at startup."
        )
    conn = _pool.acquire()
    try:
        yield conn
        conn.commit()
        logger.debug("[ORACLE] Atomic transaction committed.")
    except Exception:
        conn.rollback()
        logger.error("[ORACLE] Atomic transaction rolled back due to exception.")
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


@contextmanager
def get_shadow_connection() -> Generator[oracledb.Connection, None, None]:
    """Acquire a connection from the shadow pool as a context manager.

    If shadow pool is not initialized, falls back to the primary pool.

    Yields:
        oracledb.Connection: A live Oracle connection.
    """
    if _shadow_pool is None:
        logger.warning("[ORACLE:SHADOW] Shadow pool not initialized — falling back to primary pool.")
        with get_connection() as conn:
            yield conn
        return

    conn = _shadow_pool.acquire()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        _shadow_pool.release(conn)


def run_shadow_readonly(sql: str, params: Optional[dict] = None) -> Tuple[List[str], List[tuple]]:
    """Execute a read-only SELECT query against the shadow test database and return column names + rows.

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
            f"run_shadow_readonly only accepts SELECT queries. Got: {stripped[:60]}..."
        )

    with get_shadow_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(stripped, params or {})
        columns = [col[0].lower() for col in cursor.description]
        rows = cursor.fetchall()
        return columns, rows



