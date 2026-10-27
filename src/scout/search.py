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
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import Enum

import oracledb

from scout import db
from scout.control import CancelToken, Progress
from scout.config import ConnectionConfig, default_threads
from scout.pattern import build_regex, min_match_length, required_literal, snippet_regex


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
    # characters of context each side of the first match; Match.value is then that snippet, cut by
    # Oracle (None = the whole value, as library callers and the contract tests expect)
    snippet_context: int | None = None


@dataclass(frozen=True)
class Column:
    owner: str
    table: str
    column: str
    data_type: str
    text_indexed: bool = False  # has an Oracle Text (CONTEXT) index; informational in v1
    max_chars: int | None = None  # declared maximum length in characters; None for LOBs/unknown
    blocks: int | None = None  # table size in blocks (dictionary statistic; may be missing or stale)

    @property
    def address(self) -> tuple[str, str, str]:
        return (self.owner, self.table, self.column)

    @property
    def ref(self) -> str:
        """OWNER.TABLE.COLUMN, as shown to the user and accepted by --column / more."""
        return f"{self.owner}.{self.table}.{self.column}"


@dataclass(frozen=True)
class Snippet:
    """The first match in a value with its context, as Oracle cut it (never sliced in Python:
    Oracle counts CLOB positions in UTF-16 units). `cut_*`: text was left out on that side."""

    before: str
    hit: str
    after: str
    cut_before: bool
    cut_after: bool
    more_in_value: bool  # another match starts after this one
    # where the hit starts, in Oracle's own units (opaque here): lets fetch_detail search from
    # the hit instead of rescanning a huge value from its start
    position: int | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Match:
    owner: str
    table: str
    column: str
    value: str  # the whole value, or with a snippet: before + hit + after
    rowid: str | None = None  # how the TUI re-reads the value (fetch_detail)
    snippet: Snippet | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.owner, self.table, self.column, self.value)

    @property
    def address(self) -> tuple[str, str, str]:
        """The (owner, table, column) the match was found in; compare with Column.address."""
        return (self.owner, self.table, self.column)

    @property
    def ref(self) -> str:
        return f"{self.owner}.{self.table}.{self.column}"


@dataclass(frozen=True)
class ColumnOutcome:
    """How one searched column ended. `examples` hits were returned; with a limit,
    `more_available` says the column has hits beyond them."""

    column: Column
    examples: int
    more_available: bool
    timed_out: bool = False

    @property
    def note(self) -> str:
        """Why the hits shown may not be all of them; '' when the column is complete."""
        if self.timed_out:
            return "partial: timed out"
        if self.more_available:
            return "more available"
        return ""


ProgressCallback = Callable[[Progress], None]
SkipCallback = Callable[[Column, Exception], None]  # a column that could not be searched


# A table of at least this many blocks (about 100 MB at 8 KB) or of unknown size is read once for all its
# string columns (_search_table): on a table too big to cache, separate column scans multiply the I/O.
# Smaller tables keep one scan per column, which stops early in locate mode. None = never use one scan.
# The value is a judgement, not a measured crossover (see the bench notes).
SINGLE_SCAN_MIN_BLOCKS: int | None = 12800
MAX_COLUMNS_PER_SCAN = 20  # one scan covers at most this many columns: short SQL, and a wide table uses several threads
HEARTBEAT_SECONDS = 1.0  # progress is re-sent at least this often while a search is running
TIMEOUT_CODES = frozenset({"DPY-4024", "ORA-03156"})  # thick driver: call_timeout exceeded
CANCEL_CODES = frozenset({"ORA-01013"})  # statement interrupted (our cancel(), or the user's Ctrl+C)
PRECHECK = True  # cheap substring test before the regex; kept only if the benchmark justifies it
SEARCHABLE_TYPES = ("CHAR", "NCHAR", "VARCHAR2", "NVARCHAR2", "CLOB", "NCLOB")
SNIPPET_CONTEXT = 20  # characters each side of the hit in the CLI, the TUI table and the CSV
DETAIL_CONTEXT = 100_000  # characters each side of the hit in the TUI's detail window


@dataclass(frozen=True)
class _SnippetQuery:
    """How to cut snippets: see pattern.snippet_regex for `sub` and `back`."""

    sub: int
    back: int
    context: int

    def binds(self) -> dict[str, object]:
        return {"sub": self.sub, "back": self.back, "ctx": self.context}


def _query_pattern(opts: SearchOptions) -> tuple[str, str, "_SnippetQuery | None"]:
    """The pattern and flags every statement binds, plus the snippet settings (None = whole values)."""
    if opts.snippet_context is None:
        pattern, flags = build_regex(opts)
        return pattern, flags, None
    pattern, flags, sub, back = snippet_regex(opts)
    return pattern, flags, _SnippetQuery(sub, back, opts.snippet_context)


class ColumnTimeout(Exception):
    """A column's query ran past SearchOptions.time_limit; reported through on_skip when the
    column had produced no hits (so 'timed out' is never mistaken for 'no hits')."""


def _timeout_error(opts: SearchOptions) -> ColumnTimeout:
    return ColumnTimeout(f"timed out after {opts.time_limit:g} s")


def _deadline(opts: SearchOptions) -> float | None:
    """time.monotonic() value at which a query started now must stop (None = no limit)."""
    return time.monotonic() + opts.time_limit if opts.time_limit else None


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


@dataclass(frozen=True)
class _Done:
    future: Future
    unit: list[Column]


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


def _condition(name: str, literal: str | None, fold: bool) -> str:
    """WHERE-condition for one quoted column name; binds :pat, :flags (and :lit when pre-checking)."""
    regex = f"REGEXP_LIKE({name}, :pat, :flags)"
    if literal is None:
        return regex
    # The substring test is a necessary condition of the regex, so it can only skip rows that
    # cannot match. It is nested in CASE because Oracle does not promise to evaluate AND terms
    # in order (measured: the regex ran on every row anyway), but does short-circuit CASE.
    haystack = f"NLS_LOWER({name}, 'NLS_SORT=BINARY')" if fold else name
    return f"CASE WHEN INSTR({haystack}, :lit) > 0 THEN CASE WHEN {regex} THEN 1 END END = 1"


def _position(name: str, literal: str | None, fold: bool, start: str = "1") -> str:
    """Where the first match's hit (at or after `start`) begins in a quoted column (0 or NULL = no
    match). The substring pre-check stays in front of the regex, nested in CASE as in _condition."""
    regex = f"REGEXP_INSTR({name}, :pat, {start}, 1, 0, :flags, :sub)"
    if literal is None:
        return regex
    haystack = f"NLS_LOWER({name}, 'NLS_SORT=BINARY')" if fold else name
    return f"CASE WHEN INSTR({haystack}, :lit) > 0 THEN {regex} END"


def _window_start(c: str, s: str) -> str:
    """Where the snippet's `before` piece starts: :ctx units before the hit, moved one unit right when
    that lands inside a surrogate pair. SUBSTR on a CLOB raises ORA-22831 for an offset inside a pair
    (positions count UTF-16 units); REGEXP_SUBSTR returns U+FFFD there instead (measured). A genuine
    U+FFFD (one unit) also moves the start one unit right: one character less context, never an error."""
    edge = f"GREATEST({s} - :ctx, 1)"
    return (f"CASE WHEN TO_CHAR(REGEXP_SUBSTR({c}, '.', {edge}, 1, 'n')) = UNISTR('\\FFFD') "
            f"THEN {edge} + 1 ELSE {edge} END")


def _more_check(c: str, h: str, literal: str | None, fold: bool) -> str:
    """1 when another match starts at or after `h` (the end of the hit), else 0. The substring
    pre-check goes first: without it a value with one early hit is regex-scanned to its end
    (measured: 0.9 s against 0.01 s on 4.9 MB). Nested CASE: Oracle does not evaluate AND in order."""
    regex = f"CASE WHEN REGEXP_INSTR({c}, :pat, {h}, 1, 0, :flags) > 0 THEN 1 ELSE 0 END"
    if literal is None:
        return regex
    haystack = f"NLS_LOWER({c}, 'NLS_SORT=BINARY')" if fold else c
    return f"CASE WHEN INSTR({haystack}, :lit, {h}) > 0 THEN {regex} ELSE 0 END"


def _snippet_sql(cols: list[Column], literal: str | None, fold: bool, where_extra: str = "",
                 extra_select: str = "", start: str = "1", more: bool = True) -> str:
    """One statement that finds and cuts the first match in each column of one table.

    Selects the ROWID, then per column: before, hit, after, cut_before, cut_after, more (0/1),
    position, then `extra_select` (written against the aliases "SCOUT$C<i>"). Binds :pat :flags :sub
    :back :ctx (and :lit). The innermost view computes each match position once: ROWNUM keeps
    Oracle from merging it, which would evaluate the regex again wherever the position is used
    (measured: 111 s instead of 37-48 s on a 97 MB CLOB). `start`: where the search begins (an SQL
    expression); `more=False` skips the more-in-value check (always 0)."""
    owner, table = _quote(cols[0].owner), _quote(cols[0].table)
    n = range(1, len(cols) + 1)
    c, s_, h, b = ({i: f'"SCOUT${k}{i}"' for i in n} for k in "CSHB")
    inner = ", ".join(
        f"{_quote(col.column)} {c[i]}, {_position(_quote(col.column), literal, fold, start)} {s_[i]}"
        for i, col in zip(n, cols)
    )
    where = f" WHERE {where_extra}" if where_extra else ""
    v1 = f'SELECT ROWID "SCOUT$RID", {inner}, ROWNUM "SCOUT$RN1" FROM {owner}.{table}{where}'
    ends = ", ".join(
        f"CASE WHEN {s_[i]} > 0 THEN REGEXP_INSTR({c[i]}, :pat, GREATEST({s_[i]} - :back, 1), 1, 1, :flags, :sub) END {h[i]}, "
        f"CASE WHEN {s_[i]} > 0 THEN {_window_start(c[i], s_[i])} END {b[i]}"
        for i in n
    )
    found = " OR ".join(f"{s_[i]} > 0" for i in n)
    v2 = f'SELECT v1.*, {ends}, ROWNUM "SCOUT$RN2" FROM ({v1}) v1 WHERE {found}'
    pieces = []
    for i in n:
        si, hi, ci, bi = s_[i], h[i], c[i], b[i]
        pieces += [
            f"CASE WHEN {si} > 0 THEN SUBSTR({ci}, {bi}, {si} - {bi}) END",
            f"CASE WHEN {si} > 0 THEN SUBSTR({ci}, {si}, {hi} - {si}) END",
            f"CASE WHEN {si} > 0 THEN SUBSTR({ci}, {hi}, :ctx) END",
            f"CASE WHEN {si} > 0 AND {bi} > 1 THEN 1 ELSE 0 END",
            f"CASE WHEN {si} > 0 AND {hi} + :ctx <= LENGTH({ci}) THEN 1 ELSE 0 END",
            f"CASE WHEN {si} > 0 THEN {_more_check(ci, hi, literal, fold)} ELSE 0 END" if more else "0",
            si,
        ]
    if extra_select:
        pieces.append(extra_select)
    return f'SELECT "SCOUT$RID", {", ".join(pieces)} FROM ({v2}) v2'


def _snippet_match(col: Column, rowid, pieces) -> Match | None:
    """The Match for one column of a snippet row (None when that column did not match)."""
    before, hit, after, cut_before, cut_after, more, position = pieces
    if hit is None:
        return None
    snip = Snippet(before or "", hit, after or "", bool(cut_before), bool(cut_after), bool(more), int(position))
    return Match(col.owner, col.table, col.column, snip.before + snip.hit + snip.after, str(rowid), snip)


def _search_column(
    conn: oracledb.Connection,
    col: Column,
    pattern: str,
    flags: str,
    stop: threading.Event,
    limit: int | None = None,
    literal: str | None = None,
    fold: bool = False,
    deadline: float | None = None,
    snippet: _SnippetQuery | None = None,
) -> Generator[Match, None, ColumnOutcome]:
    """Yield the column's matches (at most `limit`); returns its ColumnOutcome.

    `literal` (from pattern.required_literal) adds a cheap substring test ahead of the regex;
    `fold` says the search is case-insensitive, so the column is lowercased first. `deadline`
    (time.monotonic() value) caps the whole query: call_timeout limits one round trip, so it is
    re-armed with the time left before every fetch. A column cut short by `stop` is returned as
    `more_available`, never as complete."""
    # identifiers come from discover_columns (the dictionary) and are quoted; the term is a bind
    name = _quote(col.column)
    binds: dict[str, object] = {"pat": pattern, "flags": flags}
    if literal is not None:
        binds["lit"] = literal
    if snippet is None:
        condition = _condition(name, literal, fold)
        sql = f"SELECT ROWID, {name} FROM {_quote(col.owner)}.{_quote(col.table)} WHERE {condition}"
    else:
        sql = _snippet_sql([col], literal, fold)
        binds.update(snippet.binds())
    if limit is not None:
        sql += " FETCH FIRST :lim ROWS ONLY"
        binds["lim"] = limit + 1  # one extra row tells us there are more
    examples, more = 0, False
    if stop.is_set():  # cancelled before the statement could start: do not start it
        return ColumnOutcome(col, 0, True)
    try:
        with conn.cursor() as cur:
            cur.arraysize = 500
            if not _arm(conn, deadline):
                return ColumnOutcome(col, 0, False, timed_out=True)
            cur.execute(sql, **binds)
            while True:
                if stop.is_set():
                    more = True  # cut short: there may be hits we never read
                    break
                if not _arm(conn, deadline):
                    return ColumnOutcome(col, examples, more, timed_out=True)
                rows = cur.fetchmany()
                if not rows:
                    break
                for rowid, *values in rows:
                    if limit is not None and examples >= limit:
                        more = True
                        break
                    examples += 1
                    if snippet is None:
                        yield Match(col.owner, col.table, col.column, values[0], str(rowid))
                    else:
                        yield _snippet_match(col, rowid, values)
    except oracledb.DatabaseError as exc:
        if _error_code(exc) in TIMEOUT_CODES:  # keep what was found; the caller flags it
            return ColumnOutcome(col, examples, more, timed_out=True)
        raise
    return ColumnOutcome(col, examples, more)


def _arm(conn: oracledb.Connection, deadline: float | None) -> bool:
    """Set the driver's per-call timeout to the time left before `deadline`; False if none is left."""
    if deadline is None:
        return True
    left = deadline - time.monotonic()
    if left <= 0:
        return False
    conn.call_timeout = max(1, int(left * 1000))
    return True


def _search_table(
    conn: oracledb.Connection,
    cols: list[Column],
    pattern: str,
    flags: str,
    stop: threading.Event,
    limit: int | None = None,
    literal: str | None = None,
    fold: bool = False,
    deadline: float | None = None,
    snippet: _SnippetQuery | None = None,
) -> Generator[Match, None, list[ColumnOutcome]]:
    """Search every column of one table in a single scan; returns one ColumnOutcome per column.

    Each row is returned once with, per column, the value if that column matched (else NULL).
    With a limit the scan stops as soon as every column has more hits than the limit; a column
    with no hits keeps the scan going to the end of the table."""
    owner, table = _quote(cols[0].owner), _quote(cols[0].table)
    names = [_quote(c.column) for c in cols]
    binds: dict[str, object] = {"pat": pattern, "flags": flags}
    if literal is not None:
        binds["lit"] = literal
    if snippet is None:
        conds = [_condition(n, literal, fold) for n in names]
        select = ", ".join(f"CASE WHEN {c} THEN {n} END" for c, n in zip(conds, names))
        sql = f"SELECT ROWID, {select} FROM {owner}.{table} WHERE {' OR '.join(conds)}"
    else:
        sql = _snippet_sql(cols, literal, fold)
        binds.update(snippet.binds())
    width = 1 if snippet is None else 7  # values per column in each row
    counts, more, timed_out = [0] * len(cols), [False] * len(cols), False
    if stop.is_set():
        return [ColumnOutcome(c, 0, True) for c in cols]
    try:
        with conn.cursor() as cur:
            cur.arraysize = 500
            if not _arm(conn, deadline):
                return [ColumnOutcome(c, 0, False, timed_out=True) for c in cols]
            cur.execute(sql, **binds)
            while True:
                if stop.is_set():
                    more = [True] * len(cols)  # cut short: there may be hits we never read
                    break
                if not _arm(conn, deadline):
                    timed_out = True
                    break
                rows = cur.fetchmany()
                if not rows:
                    break
                for rowid, *values in rows:
                    for i, col in enumerate(cols):
                        pieces = values[i * width:(i + 1) * width]
                        if pieces[0 if snippet is None else 1] is None:  # the value, or the hit
                            continue
                        if limit is not None and counts[i] >= limit:
                            more[i] = True
                        else:
                            counts[i] += 1
                            if snippet is None:
                                yield Match(col.owner, col.table, col.column, pieces[0], str(rowid))
                            else:
                                yield _snippet_match(col, rowid, pieces)
                if limit is not None and all(more):
                    break
    except oracledb.DatabaseError as exc:
        if _error_code(exc) not in TIMEOUT_CODES:
            raise
        timed_out = True
    return [ColumnOutcome(c, counts[i], more[i], timed_out) for i, c in enumerate(cols)]


def _plan_units(tables: dict[tuple[str, str], list[Column]], opts: SearchOptions) -> list[list[Column]]:
    """Units of work: a list of columns that one statement searches.

    Proximity searches and small tables get a unit per column. A large table (or one with no
    statistics) is read once per group of at most MAX_COLUMNS_PER_SCAN columns, split evenly."""
    units: list[list[Column]] = []
    for cols in tables.values():
        blocks = cols[0].blocks
        if (opts.near is not None or SINGLE_SCAN_MIN_BLOCKS is None or len(cols) == 1
                or (blocks is not None and blocks < SINGLE_SCAN_MIN_BLOCKS)):
            units += [[c] for c in cols]
        else:
            groups = -(-len(cols) // MAX_COLUMNS_PER_SCAN)
            size = -(-len(cols) // groups)
            units += [cols[i:i + size] for i in range(0, len(cols), size)]
    return units


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
    borrowed read-only connection. Closing the generator or cancelling stops the fetch.

    opts.time_limit (off by default here) caps the whole fetch; when it runs out, the hits found
    are yielded first and then ColumnTimeout is raised, so a partial list is never mistaken
    for every hit."""
    pattern, flags, snippet = _query_pattern(opts)
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
                gen = _search_column(
                    conn, column, pattern, flags, token,
                    literal=literal, fold=not opts.case_sensitive, deadline=_deadline(opts),
                    snippet=snippet,
                )
                outcome = None
                try:
                    outcome = yield from gen
                except oracledb.DatabaseError as exc:
                    if not token.cancelled and _error_code(exc) not in CANCEL_CODES:
                        raise  # an interrupted statement (cancel / Ctrl+C) just ends the fetch
                finally:
                    gen.close()
            finally:
                token.unregister(conn)
                if opts.time_limit:
                    conn.call_timeout = 0
            if outcome is not None and outcome.timed_out:
                raise _timeout_error(opts)
    finally:
        if cancel is not None:
            cancel.unlink(token)


def _column_of(unit: list[Column], m: Match) -> Column:
    return next(c for c in unit if c.column == m.column)


def _retry_column(conn, col: Column, query: dict, seen: set, results: "queue.Queue", on_skip) -> None:
    """Search one column on its own after a combined scan failed, without repeating hits already
    delivered. With a limit, the column still ends with at most `limit` examples in total.
    `query` holds the keyword arguments of _search_column."""
    limit = query["limit"]
    delivered, capped = len(seen), False

    def deliver(m: Match) -> None:
        nonlocal delivered, capped
        if m.rowid in seen:
            return
        if limit is not None and delivered >= limit:
            capped = True
            return
        seen.add(m.rowid)
        delivered += 1
        results.put(m)

    try:
        outcome = _drain(_search_column(conn, col, **query), deliver)
    except oracledb.DatabaseError as exc:
        if _error_code(exc) in CANCEL_CODES:
            raise
        if on_skip is None:
            raise
        results.put(_Skipped(col, exc))
        return
    results.put(replace(outcome, examples=delivered, more_available=outcome.more_available or capped))


def _drain(gen: Generator[Match, None, object], sink: Callable[[Match], None]):
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
    pattern, flags, snippet = _query_pattern(opts)  # validates the options before touching the database
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
        if on_progress:  # "looking for tables": the dictionary query can take a while
            on_progress(Progress(0, 0, 0.0, (), 0, 0, 0))
        with db.acquire(pool) as conn:
            token.register(conn)  # so cancel() can interrupt a slow dictionary query too
            try:
                columns = prune_columns(discover_columns(conn, opts), opts)
            finally:
                token.unregister(conn)

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
                fraction = (done_w + partial) / total_weight
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

        def work(unit: list[Column]) -> None:
            if token.cancelled:
                return
            results.put(_Started(unit[0].owner, unit[0].table, time.monotonic()))
            try:
                with db.acquire(pool) as conn:
                    token.register(conn)
                    try:
                        query = dict(
                            pattern=pattern, flags=flags, stop=token, limit=opts.limit_per_column,
                            literal=literal, fold=not opts.case_sensitive, deadline=_deadline(opts),
                            snippet=snippet,
                        )
                        if len(unit) == 1:
                            results.put(_drain(_search_column(conn, unit[0], **query), results.put))
                        else:
                            seen: dict[Column, set] = {}  # rowids already delivered, per column

                            def deliver(m: Match) -> None:
                                seen.setdefault(_column_of(unit, m), set()).add(m.rowid)
                                results.put(m)

                            try:
                                for outcome in _drain(_search_table(conn, unit, **query), deliver):
                                    results.put(outcome)
                            except oracledb.DatabaseError as exc:
                                if token.cancelled or _error_code(exc) in CANCEL_CODES:
                                    raise
                                # One bad column must not sink the others: search them one by one,
                                # skipping hits the failed scan already delivered.
                                for col in unit:
                                    _retry_column(conn, col, query, seen.get(col, set()), results, on_skip)
                    finally:
                        token.unregister(conn)
                        if opts.time_limit:
                            conn.call_timeout = 0  # the connection goes back to the pool
            except oracledb.DatabaseError as exc:
                if token.cancelled:  # interrupted by cancel(): not an error
                    return
                if _error_code(exc) in CANCEL_CODES:
                    # Not our doing: the Oracle client breaks running statements on Ctrl+C even when
                    # Python never sees the signal. The user wants out, so stop everything.
                    token.cancel()
                    if cancel is not None:
                        cancel.cancel()  # lets the caller report "cancelled"
                    return
                if on_skip is None:
                    raise
                for col in unit:
                    results.put(_Skipped(col, exc))

        units = _plan_units(tables, opts)
        total = len(units)
        for unit in units:
            executor.submit(work, unit).add_done_callback(lambda f, unit=unit: results.put(_Done(f, unit)))

        done = 0
        yielded: Counter = Counter()  # matches handed to the caller, per column address
        while done < total:
            try:
                item = results.get(timeout=HEARTBEAT_SECONDS)
            except queue.Empty:
                if token.cancelled:
                    token.break_running()  # a statement may have started just after cancel()
                emit()
                continue
            if isinstance(item, Match):
                # Workers outrun a slow caller (a UI adds one row at a time), so after a cancel the
                # queue can still hold thousands of matches: drop them, the caller asked to stop.
                if token.cancelled:
                    continue
                hits += 1
                yielded[item.address] += 1
                yield item
            elif isinstance(item, _Started):
                started.setdefault((item.owner, item.table), item.at)
            elif isinstance(item, ColumnOutcome):
                if token.cancelled:  # count only what the caller got; dropped hits mean "more"
                    given = yielded[item.column.address]
                    item = replace(item, examples=given,
                                   more_available=item.more_available or given < item.examples)
                if on_column:
                    on_column(item)
                if item.timed_out and not item.examples:
                    timeout = _timeout_error(opts)
                    if on_skip is None:
                        raise timeout  # no way to report it, and a timeout must never look like "no hits"
                    on_skip(item.column, timeout)
            elif isinstance(item, _Skipped):
                on_skip(item.column, item.error)
            elif isinstance(item, _Done):
                done += 1
                error = item.future.exception()
                if error is not None:
                    raise error
                key = (item.unit[0].owner, item.unit[0].table)
                columns_done[key] += len(item.unit)
                if columns_done[key] == len(tables[key]):
                    finished.add(key)
                emit()
                continue
            if time.monotonic() - last_emit >= HEARTBEAT_SECONDS:
                emit()
    finally:
        token.cancel()  # interrupt anything still running on the server, stop new work
        executor.shutdown(wait=True, cancel_futures=True)
        if cancel is not None:
            cancel.unlink(token)
        if own_pool:
            pool.close(force=True)


@dataclass(frozen=True)
class Detail:
    """A large window around a hit (for the TUI's detail view) and the value's full length."""

    snippet: Snippet
    total_length: int  # Oracle's LENGTH of the value (UTF-16 units for a CLOB): for the "showing" note


def fetch_detail(
    conn: oracledb.Connection, match: Match, opts: SearchOptions, context: int = DETAIL_CONTEXT
) -> Detail | None:
    """Re-read one hit's value by rowid and cut `context` characters each side of its first match.

    Oracle does the cutting, so a 100 MB value never travels whole, and the regex starts at the
    hit's position from the search (Snippet.position), so it does not rescan the value from its
    start. No substring pre-check: for one known row it would lowercase the whole value for
    nothing (measured: 3.6 s against 0.0 s on 97 MB). None when the row is gone or no longer
    matches (the data changed after the search). The owner/table/column come from the match,
    i.e. from discovery, and are quoted; the rowid is a bind."""
    pattern, flags, sub, back = snippet_regex(opts)
    column = Column(match.owner, match.table, match.column, "CLOB")  # the type is not used here
    sql = _snippet_sql([column], None, not opts.case_sensitive, where_extra="ROWID = CHARTOROWID(:rid)",
                       extra_select='LENGTH("SCOUT$C1")', start="GREATEST(:hit_at - :back, 1)",
                       more=False)  # the window never shows "more", and the check would scan the rest
    position = match.snippet.position if match.snippet is not None else None
    binds: dict[str, object] = {"pat": pattern, "flags": flags, "rid": match.rowid, "hit_at": position or 1,
                                **_SnippetQuery(sub, back, context).binds()}
    with conn.cursor() as cur:
        cur.execute(sql, **binds)
        row = cur.fetchone()
    if row is None:
        return None
    rowid, *pieces, total = row
    found = _snippet_match(column, rowid, pieces)
    return None if found is None else Detail(found.snippet, int(total or 0))


def fetch_row(conn: oracledb.Connection, match: Match) -> dict[str, object]:
    """Future feature: the entire row behind a match."""
    raise NotImplementedError
