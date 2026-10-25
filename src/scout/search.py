"""Core search/crawl logic. UI-agnostic: no Typer or Textual imports here.

The CLI calls this synchronously; the TUI must call it from a worker.
Read-only: only SELECTs are issued, over READ ONLY sessions (see scout.db).
"""
import queue
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum

import oracledb

from scout import db
from scout.config import ConnectionConfig, default_threads
from scout.pattern import build_regex


class DictionaryTier(str, Enum):
    ALL = "ALL"  # default: whatever the connected user can see
    DBA = "DBA"  # opt-in: needs elevated privileges


@dataclass(frozen=True)
class SearchOptions:
    terms: tuple[str, ...]  # several terms = a phrase (adjacent, in order); with near, exactly two words
    case_sensitive: bool = False
    whole_word: bool = False  # default: a term may match inside longer words ('albert' finds Talbert)
    near: int | None = None  # proximity: max difference in word position (1 = adjacent), either order
    schemas: tuple[str, ...] = ()  # empty = every visible schema
    tables: tuple[str, ...] = ()
    tier: DictionaryTier = DictionaryTier.ALL
    threads: int = field(default_factory=default_threads)


@dataclass(frozen=True)
class Column:
    owner: str
    table: str
    column: str
    data_type: str
    text_indexed: bool = False  # has an Oracle Text (CONTEXT) index; informational in v1


@dataclass(frozen=True)
class Match:
    owner: str
    table: str
    column: str
    value: str
    rowid: str | None = None  # reserved for the future "fetch whole row" feature

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.owner, self.table, self.column, self.value)


ProgressCallback = Callable[[int, int], None]  # (columns_done, columns_total)
SkipCallback = Callable[[Column, Exception], None]  # a column that could not be searched


SEARCHABLE_TYPES = ("CHAR", "NCHAR", "VARCHAR2", "NVARCHAR2", "CLOB", "NCLOB")
_DONE = object()


@dataclass(frozen=True)
class _Skipped:
    column: Column
    error: Exception



def _quote(identifier: str) -> str:
    """Quote a dictionary-sourced identifier for interpolation into SQL."""
    if '"' in identifier or "\x00" in identifier:
        raise ValueError(f"unsafe identifier from dictionary: {identifier!r}")
    return f'"{identifier}"'


def discover_columns(conn: oracledb.Connection, opts: SearchOptions) -> list[Column]:
    """Searchable (string-typed) columns of tables, from the data dictionary tier in opts.

    Views, domain-index internal tables, nested-table storage and IOT overflow segments are
    excluded, as are Oracle-maintained schemas (SYS, SYSTEM, ...). Schema and
    table filters match case-insensitively.
    """
    t = opts.tier.value  # fixed enum value, never user text: ALL or DBA
    binds: dict[str, str] = {}
    filters = []
    for name, col, values in (("s", "c.owner", opts.schemas), ("t", "c.table_name", opts.tables)):
        if values:
            keys = []
            for i, v in enumerate(values):
                binds[f"{name}{i}"] = v.upper()
                keys.append(f":{name}{i}")
            filters.append(f"AND UPPER({col}) IN ({', '.join(keys)})")
    types = ", ".join(f"'{ty}'" for ty in SEARCHABLE_TYPES)
    sql = f"""
        SELECT c.owner, c.table_name, c.column_name, c.data_type,
               CASE WHEN EXISTS (
                      SELECT 1 FROM {t}_IND_COLUMNS ic
                      JOIN {t}_INDEXES i
                        ON i.owner = ic.index_owner AND i.index_name = ic.index_name
                      WHERE ic.table_owner = c.owner AND ic.table_name = c.table_name
                        AND ic.column_name = c.column_name AND i.ityp_name = 'CONTEXT')
                    THEN 1 ELSE 0 END AS text_indexed
          FROM {t}_TAB_COLUMNS c
          JOIN {t}_TABLES tb ON tb.owner = c.owner AND tb.table_name = c.table_name
         WHERE c.data_type IN ({types})
           AND tb.dropped = 'NO'
           AND tb.secondary = 'N'   -- domain-index internals such as Oracle Text DR$ tables
           AND tb.nested = 'NO'     -- nested-table storage cannot be selected directly
           AND (tb.iot_type IS NULL OR tb.iot_type = 'IOT')  -- not IOT overflow/mapping segments
           AND c.owner NOT IN (SELECT username FROM {t}_USERS WHERE oracle_maintained = 'Y')
           {' '.join(filters)}
         ORDER BY c.owner, c.table_name, c.column_id"""
    with conn.cursor() as cur:
        cur.execute(sql, binds)
        rows = cur.fetchall()
    return [
        Column(owner, table, column, dtype, bool(indexed))
        for owner, table, column, dtype, indexed in rows
        if dtype in SEARCHABLE_TYPES
    ]


def _search_column(
    conn: oracledb.Connection, col: Column, pattern: str, flags: str, stop: threading.Event
) -> Iterator[Match]:
    # identifiers come from discover_columns (the dictionary) and are quoted; the term is a bind
    name = _quote(col.column)
    sql = (
        f"SELECT ROWID, {name} FROM {_quote(col.owner)}.{_quote(col.table)} "
        f"WHERE REGEXP_LIKE({name}, :pat, :flags)"
    )
    with conn.cursor() as cur:
        cur.arraysize = 500
        cur.execute(sql, pat=pattern, flags=flags)
        while not stop.is_set():
            rows = cur.fetchmany()
            if not rows:
                return
            for rowid, value in rows:
                yield Match(col.owner, col.table, col.column, value, str(rowid))


def search(
    cfg: ConnectionConfig,
    opts: SearchOptions,
    on_progress: ProgressCallback | None = None,
    on_skip: SkipCallback | None = None,
    pool: oracledb.ConnectionPool | None = None,
) -> Iterator[Match]:
    """Search every discovered column on opts.threads workers, yielding matches as found.

    Order is not guaranteed when threads > 1. Closing the generator early cancels outstanding
    work. If a column cannot be queried (missing privilege, broken external table, ...):
    with on_skip, the column is reported through it, from the calling thread, and the search
    carries on; without on_skip the error stops the search and is raised to the caller.

    With `pool` (a Session's pool) the search borrows from it and leaves it open, and threads
    are capped at the pool size; without it, a pool is created and closed for this call.
    """
    pattern, flags = build_regex(opts)  # validates the options before touching the database
    threads = max(1, opts.threads)
    own_pool = pool is None
    if own_pool:
        pool = db.create_pool(cfg, threads)
    else:
        threads = min(threads, pool.max)
    stop = threading.Event()
    results: queue.Queue = queue.Queue()
    executor = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="scout")
    try:
        with db.acquire(pool) as conn:
            columns = discover_columns(conn, opts)
        total = len(columns)
        if on_progress:
            on_progress(0, total)

        def work(col: Column) -> None:
            if stop.is_set():
                return
            try:
                with db.acquire(pool) as conn:
                    for match in _search_column(conn, col, pattern, flags, stop):
                        results.put(match)
            except oracledb.DatabaseError as exc:
                if on_skip is None:
                    raise
                results.put(_Skipped(col, exc))

        for col in columns:
            executor.submit(work, col).add_done_callback(lambda f: results.put((_DONE, f)))

        done = 0
        while done < total:
            item = results.get()
            if isinstance(item, Match):
                yield item
                continue
            if isinstance(item, _Skipped):
                on_skip(item.column, item.error)
                continue
            done += 1
            error = item[1].exception()
            if error is not None:
                raise error
            if on_progress:
                on_progress(done, total)
    finally:
        stop.set()
        executor.shutdown(wait=True, cancel_futures=True)
        if own_pool:
            pool.close(force=True)


def fetch_row(conn: oracledb.Connection, match: Match) -> dict[str, object]:
    """Future feature: the entire row behind a match."""
    raise NotImplementedError
