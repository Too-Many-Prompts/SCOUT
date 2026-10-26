"""Core search/crawl logic. UI-agnostic: no Typer or Textual imports here.

The CLI calls this synchronously; the TUI must call it from a worker.
Read-only: only SELECTs are issued, over READ ONLY sessions (see scout.db).
"""
import queue
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import Enum

import oracledb

from scout import db
from scout.control import CancelToken, Progress
from scout.config import ConnectionConfig, default_threads
from scout.pattern import build_regex, min_match_length, required_literal


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
    # locate mode: stop each column after this many hits (None = every hit); see ColumnOutcome
    limit_per_column: int | None = None
    # seconds each query may run before its column ends as "timed out" (None = no limit)
    time_limit: float | None = None


@dataclass(frozen=True)
class Column:
    owner: str
    table: str
    column: str
    data_type: str
    text_indexed: bool = False  # has an Oracle Text (CONTEXT) index; informational in v1
    max_chars: int | None = None  # declared maximum length in characters; None for LOBs/unknown
    blocks: int | None = None  # table size in blocks (dictionary statistic; may be missing or stale)


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


@dataclass(frozen=True)
class ColumnOutcome:
    """How one searched column ended. `examples` hits were returned; with a limit,
    `more_available` says the column has hits beyond them."""

    column: Column
    examples: int
    more_available: bool
    timed_out: bool = False


ProgressCallback = Callable[[Progress], None]
SkipCallback = Callable[[Column, Exception], None]  # a column that could not be searched


HEARTBEAT_SECONDS = 1.0  # progress is re-sent at least this often while a search is running
TIMEOUT_CODES = frozenset({"DPY-4024", "ORA-03156"})  # thick driver: call_timeout exceeded
PRECHECK = True  # cheap substring test before the regex; kept only if the benchmark justifies it
SEARCHABLE_TYPES = ("CHAR", "NCHAR", "VARCHAR2", "NVARCHAR2", "CLOB", "NCLOB")
_DONE = object()


class ColumnTimeout(Exception):
    """A column's query ran past SearchOptions.time_limit; reported through on_skip when the
    column had produced no hits (so 'timed out' is never mistaken for 'no hits')."""


def _error_code(exc: BaseException) -> str:
    """'ORA-01013' / 'DPY-4024' for a driver error, '' if there is none."""
    code = getattr(exc.args[0], "full_code", None) if exc.args else None
    if code:
        return str(code)
    m = re.search(r"\b(ORA-\d{5}|DPY-\d{4})", str(exc))
    return m.group(1) if m else ""


@dataclass(frozen=True)
class _Started:
    owner: str
    table: str
    at: float  # time.monotonic() when the first column of this table began


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
                    THEN 1 ELSE 0 END AS text_indexed,
               CASE WHEN c.data_type IN ('CLOB', 'NCLOB') THEN NULL
                    ELSE NULLIF(c.char_length, 0) END AS max_chars,
               tb.blocks
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
        Column(owner, table, column, dtype, bool(indexed), max_chars, blocks)
        for owner, table, column, dtype, indexed, max_chars, blocks in rows
        if dtype in SEARCHABLE_TYPES
    ]


def prune_columns(columns: list[Column], opts: SearchOptions) -> list[Column]:
    """Drop columns whose declared length is shorter than the shortest possible match.

    Uses declared lengths only (never statistics, which can be stale); LOB and unknown-length
    columns are always kept."""
    need = min_match_length(opts)
    return [c for c in columns if c.max_chars is None or c.max_chars >= need]


def _search_column(
    conn: oracledb.Connection,
    col: Column,
    pattern: str,
    flags: str,
    stop: threading.Event,
    limit: int | None = None,
    literal: str | None = None,
    fold: bool = False,
) -> Generator[Match, None, ColumnOutcome]:
    """Yield the column's matches (at most `limit`); returns its ColumnOutcome.

    `literal` (from pattern.required_literal) adds a cheap substring test ahead of the regex;
    `fold` says the search is case-insensitive, so the column is lowercased first."""
    # identifiers come from discover_columns (the dictionary) and are quoted; the term is a bind
    name = _quote(col.column)
    regex = f"REGEXP_LIKE({name}, :pat, :flags)"
    binds: dict[str, object] = {"pat": pattern, "flags": flags}
    if literal is not None:
        # The substring test is a necessary condition of the regex, so it can only skip rows that
        # cannot match. It is nested in CASE because Oracle does not promise to evaluate AND terms
        # in order (measured: the regex ran on every row anyway), but does short-circuit CASE.
        haystack = f"NLS_LOWER({name}, 'NLS_SORT=BINARY')" if fold else name
        condition = f"CASE WHEN INSTR({haystack}, :lit) > 0 THEN CASE WHEN {regex} THEN 1 END END = 1"
        binds["lit"] = literal
    else:
        condition = regex
    sql = f"SELECT ROWID, {name} FROM {_quote(col.owner)}.{_quote(col.table)} WHERE {condition}"
    if limit is not None:
        sql += " FETCH FIRST :lim ROWS ONLY"
        binds["lim"] = limit + 1  # one extra row tells us there are more
    examples, more = 0, False
    try:
        with conn.cursor() as cur:
            cur.arraysize = 500
            cur.execute(sql, **binds)
            while not stop.is_set():
                rows = cur.fetchmany()
                if not rows:
                    break
                for rowid, value in rows:
                    if limit is not None and examples >= limit:
                        more = True
                        break
                    examples += 1
                    yield Match(col.owner, col.table, col.column, value, str(rowid))
    except oracledb.DatabaseError as exc:
        if _error_code(exc) in TIMEOUT_CODES:  # keep what was found; the caller flags it
            return ColumnOutcome(col, examples, more, timed_out=True)
        raise
    return ColumnOutcome(col, examples, more)


def resolve_column(conn: oracledb.Connection, ref: str, opts: SearchOptions) -> Column | None:
    """Find the searchable column named `OWNER.TABLE.COLUMN` (or None).

    Matches against what discovery returns instead of splitting on dots, so quoted names
    containing dots or spaces work. Exact case wins; otherwise a single case-insensitive match.
    """
    parts = ref.split(".")
    for i in range(1, len(parts) - 1):  # try every dot as the owner/table boundary
        owner, rest = ".".join(parts[:i]), ".".join(parts[i:])
        cols = discover_columns(conn, replace(opts, schemas=(owner,), tables=()))
        exact = [c for c in cols if f"{c.table}.{c.column}" == rest]
        if exact:
            return exact[0]
        folded = [c for c in cols if f"{c.table}.{c.column}".upper() == rest.upper()]
        if len(folded) == 1:
            return folded[0]
    return None


def drill_down(
    pool: oracledb.ConnectionPool,
    column: Column,
    opts: SearchOptions,
    cancel: CancelToken | None = None,
) -> Iterator[Match]:
    """Every hit for one column (opts.limit_per_column is ignored), streamed from one
    borrowed read-only connection. Closing the generator or cancelling stops the fetch;
    opts.time_limit (off by default here) ends it early and keeps what was found."""
    pattern, flags = build_regex(opts)
    literal = required_literal(opts) if PRECHECK else None
    token = CancelToken()
    if cancel is not None:
        cancel.link(token)
    try:
        if token.cancelled:
            return
        with db.acquire(pool) as conn:
            token.register(conn)
            try:
                if opts.time_limit:
                    conn.call_timeout = int(opts.time_limit * 1000)
                gen = _search_column(conn, column, pattern, flags, token, None, literal, not opts.case_sensitive)
                try:
                    yield from gen
                except oracledb.DatabaseError:
                    if not token.cancelled:  # an interrupted statement is not an error
                        raise
                finally:
                    token.cancel()
                    gen.close()
            finally:
                token.unregister(conn)
                if opts.time_limit:
                    conn.call_timeout = 0
    finally:
        if cancel is not None:
            cancel.unlink(token)


def _drain(gen: Generator[Match, None, ColumnOutcome], sink: Callable[[Match], None]) -> ColumnOutcome:
    """Feed every yielded match to `sink`; return the generator's final outcome."""
    while True:
        try:
            sink(next(gen))
        except StopIteration as done:
            return done.value


def search(
    cfg: ConnectionConfig,
    opts: SearchOptions,
    on_progress: ProgressCallback | None = None,
    on_skip: SkipCallback | None = None,
    pool: oracledb.ConnectionPool | None = None,
    on_column: Callable[[ColumnOutcome], None] | None = None,
    cancel: CancelToken | None = None,
) -> Iterator[Match]:
    """Search every discovered column on opts.threads workers, yielding matches as found.

    Order is not guaranteed when threads > 1. Closing the generator early cancels outstanding
    work. If a column cannot be queried (missing privilege, broken external table, ...):
    with on_skip, the column is reported through it, from the calling thread, and the search
    carries on; without on_skip the error stops the search and is raised to the caller.

    With `pool` (a Session's pool) the search borrows from it and leaves it open, and threads
    are capped at the pool size; without it, a pool is created and closed for this call.

    With opts.limit_per_column (locate mode) each column stops after that many hits. `on_column`
    receives one ColumnOutcome per column searched, from the calling thread (skipped columns
    go to on_skip instead). A column that outruns opts.time_limit ends with
    ColumnOutcome.timed_out; if it had no hits it is also reported to on_skip as a ColumnTimeout.

    `cancel` ends the search promptly: statements already running are interrupted on the
    server, nothing new starts, and the generator finishes without raising. `on_progress`
    gets a Progress on every finished column and at least every HEARTBEAT_SECONDS.
    """
    if opts.limit_per_column is not None and opts.limit_per_column < 1:
        raise ValueError("limit_per_column must be at least 1")
    pattern, flags = build_regex(opts)  # validates the options before touching the database
    literal = required_literal(opts) if PRECHECK else None
    if cancel is not None and cancel.cancelled:
        return
    threads = max(1, opts.threads)
    own_pool = pool is None
    if own_pool:
        pool = db.create_pool(cfg, threads)
    else:
        threads = min(threads, pool.max)
    token = CancelToken()  # ours, so closing this generator never marks the caller's token
    if cancel is not None:
        cancel.link(token)
    results: queue.Queue = queue.Queue()
    executor = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="scout")
    try:
        with db.acquire(pool) as conn:
            columns = prune_columns(discover_columns(conn, opts), opts)

        tables: dict[tuple[str, str], list[Column]] = {}
        for col in columns:
            tables.setdefault((col.owner, col.table), []).append(col)
        sized = all(cols[0].blocks for cols in tables.values())  # else every table weighs the same
        weight = {k: (cols[0].blocks if sized else 1) for k, cols in tables.items()}
        total_weight = sum(weight.values())
        finished: set[tuple[str, str]] = set()
        columns_done: Counter = Counter()
        started: dict[tuple[str, str], float] = {}
        hits = 0
        last_emit = time.monotonic()

        def snapshot() -> Progress:
            now = time.monotonic()
            if not tables:
                fraction = 1.0
            else:
                done_w = sum(weight[k] for k in finished)
                partial = sum(weight[k] * columns_done[k] / len(tables[k]) for k in tables if k not in finished)
                fraction = done_w / total_weight if not partial else (done_w + partial) / total_weight
            flying = sorted(
                ((f"{o}.{t}", now - at) for (o, t), at in started.items() if (o, t) not in finished),
                key=lambda f: -f[1],
            )
            return Progress(len(finished), len(tables), fraction, tuple(flying), hits,
                            sum(columns_done.values()), len(columns))

        def emit() -> None:
            nonlocal last_emit
            last_emit = time.monotonic()
            if on_progress:
                on_progress(snapshot())

        emit()

        def work(col: Column) -> None:
            if token.cancelled:
                return
            results.put(_Started(col.owner, col.table, time.monotonic()))
            try:
                with db.acquire(pool) as conn:
                    token.register(conn)
                    try:
                        if opts.time_limit:
                            conn.call_timeout = int(opts.time_limit * 1000)
                        gen = _search_column(
                            conn, col, pattern, flags, token, opts.limit_per_column, literal, not opts.case_sensitive
                        )
                        results.put(_drain(gen, results.put))
                    finally:
                        token.unregister(conn)
                        if opts.time_limit:
                            conn.call_timeout = 0  # the connection goes back to the pool
            except oracledb.DatabaseError as exc:
                if token.cancelled:  # interrupted by cancel(): not an error
                    return
                if on_skip is None:
                    raise
                results.put(_Skipped(col, exc))

        total = len(columns)
        for col in columns:
            executor.submit(work, col).add_done_callback(lambda f, col=col: results.put((_DONE, f, col)))

        done = 0
        while done < total:
            try:
                item = results.get(timeout=HEARTBEAT_SECONDS)
            except queue.Empty:
                emit()
                continue
            if isinstance(item, Match):
                hits += 1
                yield item
            elif isinstance(item, _Started):
                started.setdefault((item.owner, item.table), item.at)
            elif isinstance(item, ColumnOutcome):
                if on_column:
                    on_column(item)
                if item.timed_out and not item.examples and on_skip:
                    on_skip(item.column, ColumnTimeout(f"timed out after {opts.time_limit:g} s"))
            elif isinstance(item, _Skipped):
                on_skip(item.column, item.error)
            else:
                _, future, col = item
                done += 1
                error = future.exception()
                if error is not None:
                    raise error
                key = (col.owner, col.table)
                columns_done[key] += 1
                if columns_done[key] == len(tables[key]):
                    finished.add(key)
                emit()
                continue
            if time.monotonic() - last_emit >= HEARTBEAT_SECONDS:
                emit()
        if not columns:
            emit()
    finally:
        token.cancel()  # interrupt anything still running on the server, stop new work
        executor.shutdown(wait=True, cancel_futures=True)
        if cancel is not None:
            cancel.unlink(token)
        if own_pool:
            pool.close(force=True)


def fetch_row(conn: oracledb.Connection, match: Match) -> dict[str, object]:
    """Future feature: the entire row behind a match."""
    raise NotImplementedError
