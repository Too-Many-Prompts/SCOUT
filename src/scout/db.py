"""Oracle connectivity. Thick mode only: requires an Oracle Instant Client install.

SCOUT is a read-only tool: every session is switched to a READ ONLY transaction so an
accidental write fails in the database, not just in our code. Never commit on these
connections; a commit/rollback ends the read-only transaction (acquire() re-arms it).

Every session also (a) tags itself `module = "SCOUT"` (and `action` = what it is doing) so a DBA
can find and kill it in V$SESSION, and (b) disables parallel query, so a table declared
PARALLEL, or automatic DOP, cannot fan one scan out into many PX servers on a shared database.
"""
import contextlib
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import oracledb

from scout.config import ConnectionConfig

if TYPE_CHECKING:
    from scout.search import SearchOptions

MODULE = "SCOUT"  # V$SESSION.MODULE of every session we open
ACTION_IDLE = "idle"
MAX_ACTION_BYTES = 64  # Oracle's V$SESSION.ACTION limit

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


PARALLEL_WARNING = (
    "Could not disable parallel query on this account (ORA-01031: insufficient privileges). "
    "Searches continue, but a table declared PARALLEL may use extra parallel servers on the database."
)
_warnings: list[str] = []
_warned: set[str] = set()
_warn_lock = threading.Lock()


def _warn(message: str) -> None:
    """Remember a non-fatal notice for the front end, once per process (see take_warnings)."""
    with _warn_lock:
        if message not in _warned:
            _warned.add(message)
            _warnings.append(message)


def take_warnings() -> list[str]:
    """Notices raised since the last call (each is raised only once per process); front ends show them."""
    with _warn_lock:
        taken, _warnings[:] = list(_warnings), []
    return taken


def _error_code(exc: BaseException) -> str:
    code = getattr(exc.args[0], "full_code", None) if exc.args else None
    if code:
        return str(code)
    m = re.search(r"\b(ORA-\d{5}|DPY-\d{4})", str(exc))
    return m.group(1) if m else ""


def _prepare(conn: oracledb.Connection) -> None:
    """Make a session safe to point at a shared database: READ ONLY, no parallel query, tagged.
    ALTER SESSION is not DML and normally needs no privilege beyond CREATE SESSION; if an account is
    locked down (ORA-01031) the session still works and a warning is recorded (take_warnings)."""
    conn.module = MODULE
    conn.action = ACTION_IDLE
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        try:
            cur.execute("ALTER SESSION DISABLE PARALLEL QUERY")
        except oracledb.DatabaseError as exc:
            if _error_code(exc) != "ORA-01031":  # anything but "not allowed" is a real problem
                raise
            _warn(PARALLEL_WARNING)  # a locked-down account: carry on, but say so


def set_action(conn: oracledb.Connection, action: str) -> None:
    """Say what this session is doing (shown as V$SESSION.ACTION; sent with the next call)."""
    conn.action = action.encode()[:MAX_ACTION_BYTES].decode(errors="ignore")


def reset_call_timeout(conn: oracledb.Connection) -> None:
    """Clear call_timeout before a connection goes back to the pool. A connection that died on
    that very timeout cannot be reset; that must not replace the timeout the caller is reporting."""
    with contextlib.suppress(oracledb.Error):
        conn.call_timeout = 0


def connect(cfg: ConnectionConfig, read_only: bool = True) -> oracledb.Connection:
    """read_only=False exists only for the test-seed loader; the tool itself never uses it."""
    init_client()
    conn = oracledb.connect(user=cfg.user, password=cfg.password, dsn=_dsn(cfg))
    if read_only:
        _prepare(conn)
    else:
        conn.module = MODULE
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
        _prepare(conn)
        yield conn
    finally:
        try:
            with contextlib.suppress(oracledb.Error):  # a connection that died on a timeout
                conn.action = ACTION_IDLE
                conn.rollback()  # ends the read-only transaction; nothing to lose
        finally:
            pool.release(conn)


@dataclass
class Session:
    """One login, reused by every search in an app run: config, schema scope and session pool."""

    cfg: ConnectionConfig
    schemas: tuple[str, ...]
    pool: oracledb.ConnectionPool
    last_opts: "SearchOptions | None" = None  # the shell's most recent SearchOptions, for `more`

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
