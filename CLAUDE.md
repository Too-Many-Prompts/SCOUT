# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

SCOUT (Schema-Crawling Oracle Universal Tool): a Python tool for searching Oracle databases
with unknown schema for strings. Ships both a CLI and a Textual TUI against the same core
search logic.

Implemented: column discovery, pattern building, threaded search, CSV export, the CLI `search`
command and the TUI search/export. Not implemented yet: `search.fetch_row` (fetch the whole row
behind a match; `Match.rowid` is already captured for it) and any use of Oracle Text indexes for
matching. Stubs still in the tree should raise `NotImplementedError` and have strict-xfail tests.
Status: 107 tests pass, 1 strict-xfail (`fetch_row`). The Docker compose path (`scripts/db.sh`,
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
- **Known performance limit:** one full scan per string column (a 40-column table is read 40
  times); one query per table OR-ing the columns would be faster.

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

# run the CLI: `scout` logs in once and opens a scout> prompt (search ..., help, exit);
# `scout search ...` is the one-shot, scriptable form
scout
scout search --help

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
- **A broken column never sinks the crawl.** `search(..., on_skip=cb)` reports columns that fail
  (e.g. an external table whose file is missing) via `cb`, from the caller's thread, and carries
  on; the CLI and TUI list them at the end. Without `on_skip`, the error is raised. The seed's
  `SCOUT_BROKEN.EXT_MISSING` exercises this; keep `SCOUT_BROKEN` out of `expected.SCHEMAS`.

## Layout

```
src/scout/
  cli.py     # Typer app, entry point: `scout search ...`
  tui.py     # Textual App
  search.py  # core search/crawl logic (SearchOptions, Match, search), UI-agnostic
  pattern.py # search options -> Oracle REGEXP_LIKE pattern (pure, no DB)
  config.py  # ConnectionConfig, defaults.txt/env defaults, default thread count
  db.py      # thick-mode init, connect, session pool, Session/open_session (login once)
  export.py  # CSV writer
tests/
  conftest.py        # env-driven fixtures: admin_config, reader_config, *_connection (skip if no DB)
  db/seed.sql        # synthetic multi-schema fixture; db/expected.py is its ground truth
  db/load_seed.py    # loads the seed without docker exec
  test_*.py          # unit (mocked) + integration (@pytest.mark.integration) + cli/tui
docker-compose.yml   # test Oracle (gvenzl/oracle-free:latest)
scripts/db.sh        # up|down|reset|logs
```
