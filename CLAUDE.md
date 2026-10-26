# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

SCOUT (Schema-Crawling Oracle Universal Tool): a Python tool for searching Oracle databases
with unknown schema for strings. Ships both a CLI and a Textual TUI against the same core
search logic.

Implemented: column discovery, pattern building, threaded search, CSV export, the CLI `search`
command and the TUI search/export (styled, themed front ends), locate mode with drill-down, cancel, time limits and live progress
(design: `docs/superpowers/specs/2026-10-25-locate-first-search-design.md`, plan beside it, benchmark
results in `docs/superpowers/plans/2026-10-25-locate-first-search-bench.md`). Not implemented yet: `search.fetch_row` (fetch the whole row
behind a match; `Match.rowid` is already captured for it) and any use of Oracle Text indexes for
matching. Stubs still in the tree should raise `NotImplementedError` and have strict-xfail tests.
Status: 316 tests pass, 1 strict-xfail (`fetch_row`), plus 1 `bench` test run separately (`pytest -m bench`, needs the generated dataset). The Docker compose path (`scripts/db.sh`,
`docker-compose.yml`, `tests/db/init/01_seed.sh`) has been run end to end and works.

## Search semantics (the contract the tests in `tests/db/expected.py` encode)

- **Default match:** a term may match inside longer words (`albert` finds `Talbert` and
  `Albertson`), with no wildcards needed. `--whole-word`/`-w` restricts to whole words and combines
  with wildcards (`albert*` -w = whole words starting with albert). Prefix-only matching is gone.
- **Phrases:** several terms are a phrase: words adjacent, in order, across any
  punctuation/whitespace; only the first and last word may match partially (default mode).
- **Proximity:** `--near N`/`-n N` with exactly two single-word terms; matches in either order and
  N is the max difference in word position (1 = adjacent).
- **Case:** insensitive by default; `case_sensitive` switches the REGEXP_LIKE flag to `c`.
- **Wildcards are always on:** `*` = zero or more word characters, `?` = exactly one; `\*`, `\?`
  and `\\` are literals. All other regex metacharacters (and `% _`) are always literal. Quote
  wildcard words on the command line (shell globbing); in the `scout>` shell use single quotes so
  `shlex` keeps the backslash (`search 'a\*'`).
- **Scope:** only string columns (CHAR/NCHAR/VARCHAR2/NVARCHAR2/CLOB/NCLOB) of real tables.
  Views, Oracle Text `DR$` tables, nested-table storage, IOT overflow segments and
  Oracle-maintained schemas are excluded. Patterns are limited to Oracle's 512-byte regex limit.
- **A search must contain a real character.** A term (or phrase, or either proximity term) made only
  of `* ? % _` is refused with a plain message by `pattern.build_regex`, so the core, CLI, shell and
  TUI all refuse it. An escaped `\*` counts as a real character.
- **Locate mode (the default in the CLI, shell and TUI):** each column stops after 5 example hits
  (`DEFAULT_LOCATE_LIMIT`) and is marked *more available*; `--all` (TUI: "every hit") lifts the limit.
  `SearchOptions.limit_per_column` itself defaults to `None` (unlimited), so library callers and the
  contract tests see complete results. Every searched column reports one `ColumnOutcome`
  (`on_column`); `drill_down` / `resolve_column` / `--column REF` / shell `more REF` / TUI "Show all
  hits" fetch every hit for one column. Locate-mode CSV gains a `complete` column (`no` = examples only).
- **Time limit:** locate-mode queries get 300 s (`DEFAULT_TIME_LIMIT`; `--time-limit`). It is a deadline for
  the whole query: the driver's `call_timeout` covers only one round trip, so `_search_column` re-arms it
  with the time left before every fetch. A column that runs out is `ColumnOutcome.timed_out`; with no hits
  it also goes to `on_skip` as `ColumnTimeout` (raised if there is no `on_skip`), so a timeout is never
  presented as "no hits". `--all` and drill-down have no limit unless asked; a drill-down that times out
  yields its hits and then raises `ColumnTimeout`, and the CLI/TUI label the result partial.
- **Outcomes are honest about being cut short:** a column stopped by cancel returns `more_available=True`
  (never "complete"); the TUI labels an export `complete=no` for every row after a cancel or timeout.
- **Cancel:** `CancelToken` (`control.py`) stops a search promptly and interrupts statements already
  running on the server (`Connection.cancel()` -> ORA-01013, swallowed), re-sends the interrupt every
  heartbeat while draining (a statement can start just after `cancel()`), and also covers the dictionary
  query. An ORA-01013 we did not cause is treated as the user's Ctrl+C: the Oracle client breaks running
  statements on SIGINT even when Python never sees it. CLI/shell: Ctrl+C (exit 130); TUI: Cancel or Esc. `Progress` (size-weighted fraction, in-flight tables with elapsed time, hits)
  is sent on every finished column and at least once a second.
- **Oracle's case-insensitive regex folds more than ASCII case** (measured): Kelvin sign = k, long s = s,
  and one character can match several letters (sharp s = ss, ligatures = fi, ffi, ff, fl, st). Two things
  depend on this: `pattern.min_match_length` (pruning of columns too short to hold a match) assumes up to
  3 characters fold into 1 when case-insensitive, and `pattern.required_literal` (the substring
  pre-check) only uses ASCII pieces free of `s` and of possible ligature pairs, tested with
  `NLS_LOWER(col, 'NLS_SORT=BINARY')`. Both are proved by tests (a whole-BMP sweep and equivalence over the
  seed's `Unicode Edge` table, and re-run under Turkish/German NLS settings, which change nothing); do not
  loosen them without extending those tests.
- **Speed (benchmark, 20 tables x 50k rows, 140 columns, in cache):** the regex is the cost, not the scan.
  The pre-check is nested in `CASE WHEN INSTR(..) > 0 THEN CASE WHEN REGEXP_LIKE(..)` because Oracle does
  not evaluate `AND` terms in order (measured): a no-hit search went from 59.5 s to 3.3 s. Single scan per
  table (`SINGLE_SCAN_PER_TABLE`) is built and tested but OFF: no gain in cache, slower in locate mode;
  revisit with a larger-than-memory dataset.
- **READ ONLY snapshots:** a READ ONLY transaction cannot read an object whose definition changed within
  the same second (ORA-01466). Test loaders wait 2 s after DDL; on a live database that is being altered,
  an affected column simply shows up in the skipped list.

## Assumptions that shape the design

- **End users have read-only (SELECT) access.** SCOUT only issues SELECTs, and `db.connect` /
  `db.acquire` put every session in a READ ONLY transaction so a stray write fails in the
  database. Never commit on these connections. Tests default to the unprivileged `scout_reader`
  (only CREATE SESSION + table SELECT grants); `system`/`admin_config` is reserved for the DBA
  tier test and the seed loader (`load_seed` is the one place that connects `read_only=False`).
- **Text indexes are unknown ahead of time.** Some columns may have Oracle Text (CONTEXT)
  indexes; most won't. Matching is always a `REGEXP_LIKE` full scan, so results never depend on
  index presence. `discover_columns` records `Column.text_indexed` for a future `CONTAINS()` fast
  path (risks to resolve first: index staleness, lexer case rules, tokenization differences).
  The seed has `INDEXED_NOTES` (real CONTEXT index) and an identical unindexed `PLAIN_NOTES`.

## Commands

```bash
# install (editable, with dev deps)
pip install -e ".[dev]"

# unit tests (no database required)
pytest

# integration tests against the real Oracle container (see "Testing" below)
pytest -m integration

# run a single test
pytest tests/test_connection.py::test_can_connect_and_query_dual

# test database (needs docker access; image gvenzl/oracle-free:latest)
scripts/db.sh up|down|reset|logs   # data persists in the scout-oradata volume; only `reset` wipes it
                                   # (the seed runs only on a fresh volume; container restarts on boot)
# or load the fixture into an already-running instance, over a normal connection:
python -m tests.db.load_seed

# run the CLI: `scout` logs in once and opens a scout> prompt (search ..., more REF, help, exit);
# `scout search ...` is the one-shot, scriptable form
scout
scout search --help
scout search albert --column "OWNER.TABLE.COLUMN"   # every hit in one column
scout search albert --all --csv hits.csv            # every hit everywhere (default is 5 examples/column)

# benchmark (separate schema SCOUT_BENCH; the load takes about 7 minutes)
python -m tests.bench.load --tables 20 --rows 50000
python -m tests.bench.run --threads 8 --limit 5 [--precheck on|off] [--single-scan on|off]
pytest -m bench

# run the TUI (login screen first; Ctrl+L logs out back to it)
python -m scout.tui
# run the TUI in dev mode (hot-reload, live CSS editing)
textual run --dev src/scout/tui.py

# attach a debug console to a running Textual app (run in a second terminal)
textual console
```

## Testing

There are two tiers, and they are not interchangeable:

- **Unit tests** mock the `oracledb` connection/cursor and run with no database present.
- **Integration tests** (marked `@pytest.mark.integration`) hit a real Oracle instance via
  the `oracle_connection` fixture in `tests/conftest.py`. There is no in-memory/embedded
  Oracle — these tests require a running container (developed against `gvenzl/oracle-free`).

The fixture reads connection info from env vars and **skips** (does not fail) if the database
is unreachable or `SCOUT_TEST_DB_PASSWORD` is unset:

```
SCOUT_TEST_DB_HOST=localhost
SCOUT_TEST_DB_PORT=1521
SCOUT_TEST_DB_SERVICE_NAME=FREEPDB1
SCOUT_TEST_DB_USER=system
SCOUT_TEST_DB_PASSWORD=<set this>
SCOUT_TEST_READER_USER=scout_reader
SCOUT_TEST_READER_PASSWORD=<set this>   # created by tests/db/seed.sql; used by the compose file too
```

See `.env.example`. Do not hardcode credentials into test files.

The fixture data is `tests/db/seed.sql` (it contains non-ASCII text, so the init script forces
`NLS_LANG=...AL32UTF8` for SQL*Plus) (loaded by `docker-compose.yml` on first start, or by
`tests.db.load_seed`); its ground truth is `tests/db/expected.py`. Keep the two in sync. Default
searches run as the low-privilege `scout_reader` (`reader_config` fixture) through `ALL_*`; the
`SCOUT_HIDDEN` schema is invisible to it and only reachable as `system` via the DBA tier. Discovery
joins `ALL_TABLES` to exclude views and skips Oracle-maintained schemas (SYS etc.).

## Architecture constraints

These shape how the modules below are built — read this before changing code.

- **`search.py` must stay UI-agnostic.** The CLI calls it synchronously; the Textual TUI must
  call it from a worker (`self.run_worker`), never directly on the event loop, or the UI will
  freeze during a query. This is why search/query logic lives in its own module rather than
  inside `cli.py` or `tui.py`.
- **Identifiers vs. values in generated SQL.** Table/column names come from the data
  dictionary and cannot be passed as bind variables — they get interpolated into SQL text.
  Any identifier taken from a catalog query must be validated against the dictionary result
  set it came from (and quoted) before being interpolated. The actual search term the user is
  looking for must always be a bind variable, never interpolated.
- **Dictionary view tier determines required privileges.** `USER_TAB_COLUMNS` (own schema
  only), `ALL_TAB_COLUMNS` (anything the connected user has access to), `DBA_TAB_COLUMNS`
  (everything, needs DBA privs) are three different capability levels. Default to `ALL_*`
  and treat `DBA_*` as an opt-in fallback for users with elevated privileges.
- **python-oracledb runs in thick mode.** Requires Oracle Instant Client (found via ldconfig,
  or set `SCOUT_ORACLE_CLIENT_LIB_DIR`). `scout.db.init_client()` calls
  `oracledb.init_oracle_client()` exactly once per process, and every connection goes through
  `db.connect` / `db.create_pool` so it can't be forgotten. Tests use the same path.
- **Threaded search.** `search.search()` fans out per (table, column) over
  `SearchOptions.threads` workers (default 8, `SCOUT_THREADS`, `--threads/-j`). Each worker
  acquires its own connection from a session pool; never share a connection across threads.
  Result order is not guaranteed when threads > 1; worker exceptions must propagate to the caller.
- **Credentials are never defaulted.** Host/port/service/schema/threads have defaults
  (built-ins < optional `defaults.txt` in the cwd < `SCOUT_DB_*`/`SCOUT_THREADS` env < flags; see
  `defaults.example.txt`; `config.load_defaults`); user and password are always prompted (and are
  ignored if put in `defaults.txt`), except with `--non-interactive` (test use only, reads
  `SCOUT_TEST_DB_USER/PASSWORD`).
- **Log in once per app run.** `db.open_session` creates the session pool and borrows one
  connection (so bad credentials fail at login); the resulting `db.Session` (cfg, schemas, pool) is
  passed to `search(..., pool=session.pool)` for every search, which then leaves the pool open and
  caps threads at the pool size. The `scout` shell (`cli._shell_loop`) and the TUI (`LoginScreen`,
  then the search screen; Ctrl+L closes the pool and returns to login) both work this way. Without
  `pool=`, `search()` still makes and closes its own (the one-shot `scout search`).
- **Schema scope is chosen at login.** Host, port, service name, user, password and the schema(s) to
  search (comma-separated, blank = every visible schema) are all asked before connecting; every
  table in the named schema is searched. In the shell `search --schema` overrides it per search.
- **Front-end conventions.**
  - *CLI:* stdout is exactly `OWNER.TABLE.COLUMN<TAB>value`; tests and scripts read it. All styling goes through
    `typer.style`/`secho` (click strips it when not a tty) or to stderr, so never decorate a stdout line. Error
    text is pinned by tests (`Database error`, `Unknown command`, `No searchable column`, `Error:`); `_error()`
    prints the message as given.
  - *TUI:* custom `scout` theme (`SCOUT_THEME`, `SCOUT_CSS` in `tui.py`); amber is reserved for "a hit". Tests
    use the ids `#status #found #results #skipped #terms #search #cancel #show-all #export`; keep them. Do not
    name an App method `_running` (Textual has an attribute of that name; ours is `_set_running`). Enter in
    terms/near/threads runs the search, Ctrl+E exports, Cancel is enabled only while something runs, and hits are
    marked by `_highlight_pattern` (cosmetic: Oracle decides what matched). "Show all hits" stays enabled on
    purpose (a test expects the "Select a column" message).
- **A broken column never sinks the crawl.** `search(..., on_skip=cb)` reports columns that fail
  (e.g. an external table whose file is missing) via `cb`, from the caller's thread, and carries
  on; the CLI and TUI list them at the end. Without `on_skip`, the error is raised. The seed's
  `SCOUT_BROKEN.EXT_MISSING` exercises this; keep `SCOUT_BROKEN` out of `expected.SCHEMAS`.

## Layout

```
src/scout/
  cli.py     # Typer app, entry point: `scout search ...`
  tui.py     # Textual App: theme + CSS, login and search screens
  search.py  # core search/crawl logic (SearchOptions, Match, search), UI-agnostic
  pattern.py # search options -> Oracle REGEXP_LIKE pattern (pure, no DB)
  config.py  # ConnectionConfig, defaults.txt/env defaults, default thread count
  db.py      # thick-mode init, connect, session pool, Session/open_session (login once)
  control.py # CancelToken, Progress (shared by search and the front ends)
  export.py  # CSV writer (optional 'complete' column for locate-mode files)
tests/
  conftest.py        # env-driven fixtures: admin_config, reader_config, *_connection (skip if no DB)
  db/seed.sql        # synthetic multi-schema fixture; db/expected.py is its ground truth
  db/load_seed.py    # loads the seed without docker exec
  bench/             # generated large dataset + timing runner (marker: bench, excluded by default)
  test_*.py          # unit (mocked) + integration (@pytest.mark.integration) + cli/tui
docker-compose.yml   # test Oracle (gvenzl/oracle-free:latest)
scripts/db.sh        # up|down|reset|logs
```
