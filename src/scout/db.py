"""Oracle connectivity. Thick mode only: requires an Oracle Instant Client install.

SCOUT is a read-only tool: every session is switched to a READ ONLY transaction so an
accidental write fails in the database, not just in our code. Never commit on these
connections; a commit/rollback ends the read-only transaction (acquire() re-arms it).
"""
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import oracledb

from scout.config import ConnectionConfig

_init_lock = threading.Lock()
_client_initialized = False


def init_client() -> None:
    """Enable thick mode once per process. Must run before the first connection."""
    global _client_initialized
    with _init_lock:
        if _client_initialized:
            return
        lib_dir = os.environ.get("SCOUT_ORACLE_CLIENT_LIB_DIR")
        oracledb.init_oracle_client(lib_dir=lib_dir) if lib_dir else oracledb.init_oracle_client()
        oracledb.defaults.fetch_lobs = False  # CLOB/NCLOB come back as str
        _client_initialized = True


def _dsn(cfg: ConnectionConfig) -> str:
    return oracledb.makedsn(cfg.host, cfg.port, service_name=cfg.service_name)


def _set_read_only(conn: oracledb.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")


def connect(cfg: ConnectionConfig, read_only: bool = True) -> oracledb.Connection:
    """read_only=False exists only for the test-seed loader; the tool itself never uses it."""
    init_client()
    conn = oracledb.connect(user=cfg.user, password=cfg.password, dsn=_dsn(cfg))
    if read_only:
        _set_read_only(conn)
    return conn


def create_pool(cfg: ConnectionConfig, size: int) -> oracledb.ConnectionPool:
    """Session pool for the threaded search; use acquire() to borrow a read-only connection."""
    init_client()
    return oracledb.create_pool(
        user=cfg.user, password=cfg.password, dsn=_dsn(cfg), min=1, max=size, increment=1
    )


@contextmanager
def acquire(pool: oracledb.ConnectionPool) -> Iterator[oracledb.Connection]:
    """Borrow a connection in a READ ONLY transaction; ended and returned on exit."""
    conn = pool.acquire()
    try:
        _set_read_only(conn)
        yield conn
    finally:
        try:
            conn.rollback()  # ends the read-only transaction; nothing to lose
        finally:
            pool.release(conn)


@dataclass
class Session:
    """One login, reused by every search in an app run: config, schema scope and session pool."""

    cfg: ConnectionConfig
    schemas: tuple[str, ...]
    pool: oracledb.ConnectionPool

    def close(self) -> None:
        self.pool.close(force=True)


def open_session(cfg: ConnectionConfig, schemas: tuple[str, ...], threads: int) -> Session:
    """Create the pool and borrow one connection so bad credentials fail here, at login."""
    pool = create_pool(cfg, max(1, threads))
    try:
        with acquire(pool):
            pass
    except BaseException:
        pool.close(force=True)
        raise
    return Session(cfg, tuple(schemas), pool)
