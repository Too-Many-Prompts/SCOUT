# SCOUT

Schema-Crawling Oracle Universal Tool: find a string in an Oracle database when you don't know which table or column holds it.

SCOUT looks through every string column you can see, shows which columns contain your term, and lets you drill into one. It only reads: every session is a READ ONLY transaction, and it needs nothing beyond `SELECT` access. A command-line tool and a terminal UI share the same search code.

## What it does

- Searches every string column (CHAR, NCHAR, VARCHAR2, NVARCHAR2, CLOB, NCLOB) of the real tables in the schemas you choose. Views, Oracle-maintained schemas and Oracle Text internals are skipped.
- **Locate mode** is the default: each column stops after 5 example hits and is marked *more available*. Drill into one column to get every hit, or use `--all` for everything.
- Matches phrases, words near each other, wildcards, whole words and case-sensitive text.
- Searches columns in parallel, shows live progress, and can be cancelled with Ctrl+C (CLI) or Esc (TUI). Locate-mode queries stop after 300 seconds by default.
- Exports results to CSV. A locate-mode file has a `complete` column so examples are never mistaken for every hit.
- Lists columns that could not be searched instead of stopping.
- Shows a snippet around each hit. In the terminal UI, Enter on a hit opens the full value and `r` opens the whole row behind it.
- Is gentle on a production database: sessions are tagged `SCOUT` in `V$SESSION`, parallel query is switched off, and external tables are skipped unless you pass `--include-external`.

## Requirements

- Python 3.10 or newer
- Oracle Database 19 or later to search
- Oracle Instant Client (Basic or Basic Light, 19 or later). SCOUT uses python-oracledb in thick mode and finds the client through `ldconfig`, or set `SCOUT_ORACLE_CLIENT_LIB_DIR` to the Instant Client folder.
- An Oracle account with `SELECT` on the tables you want to search
- Docker with the compose plugin, only if you want the local test database

## Install

```bash
git clone <this repository's URL> scout && cd scout
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

This installs the `scout` command. Leave off `[dev]` if you don't need the test tools.

If Instant Client is not on the library path, point SCOUT at it:

```bash
export SCOUT_ORACLE_CLIENT_LIB_DIR=/opt/oracle/instantclient_23_5   # example path
```

## Quick start

You need a reachable Oracle database and an account that can `SELECT`. To try SCOUT without one, set up the [local test database](#local-test-database) first; the examples below use it.

Run `scout` to log in once and search interactively:

```
$ scout
Host [localhost]:
Port [1521]:
Username: scout_reader
Password:
Service name [FREEPDB1]:
Schema to search (comma-separated, blank = all visible): SCOUT_HR
Connected as scout_reader@localhost:1521/FREEPDB1; searching SCOUT_HR.
Type 'help' for commands, 'exit' to quit.
scout> search albert
SCOUT_HR.EMPLOYEES.NOTES    Albert met Mr Albertson (+more)
SCOUT_HR.EMPLOYEES.NOTES    Gilbert and Talbert
SCOUT_HR.EMPLOYEES.NOTES    Albertina
Where it was found:
  SCOUT_HR.EMPLOYEES.NOTES     3 example(s)
scout> more SCOUT_HR.EMPLOYEES.NOTES
```

Hits go to stdout as `OWNER.TABLE.COLUMN<TAB>value`, so they pipe cleanly. The summary and progress go to stderr.

For scripts, use the one-shot form:

```bash
scout search albert --column "SCOUT_HR.EMPLOYEES.NOTES"   # every hit in one column
scout search albert --all --csv hits.csv                  # every hit everywhere
scout search --help                                       # all options
```

Or use the terminal UI:

```bash
python -m scout.tui
```

## Search syntax

| You type | It finds |
| --- | --- |
| `albert` | `albert` anywhere, including inside `Talbert` and `Albertson` |
| `albert -w` | whole words only |
| `new york` | the words next to each other, in order, across any punctuation |
| `-n 3 alpha beta` | the two words within 3 words of each other, in either order (1 = adjacent) |
| `'alb*'` | `*` is zero or more word characters |
| `'alb?rt'` | `?` is exactly one word character |
| `'a\*'` | a literal `*` (`\?` and `\\` work the same way) |
| `--case-sensitive` | exact case (the default ignores case) |

Other regular-expression characters, `%` and `_` are always literal. A term made only of wildcards is refused.

Quote words that contain `*` or `?` so your shell doesn't expand them. In the `scout>` prompt, use single quotes to keep a backslash: `search 'a\*'`.

## Configuration

Host, port, service name, schema and thread count have defaults, and each later source overrides the one before: built-ins, then `defaults.txt` in the folder you run `scout` from, then environment variables, then command-line flags.

```bash
cp defaults.example.txt defaults.txt
```

| Setting | `defaults.txt` key | Environment variable |
| --- | --- | --- |
| Host | `host` | `SCOUT_DB_HOST` |
| Port | `port` | `SCOUT_DB_PORT` |
| Service name | `service_name` | `SCOUT_DB_SERVICE_NAME` |
| Schema(s) | `schema` | `SCOUT_DB_SCHEMA` |
| Worker threads | `threads` | `SCOUT_THREADS` |

Your user name and password are never read from a file or defaulted. SCOUT always asks for them.

## Terminal UI keys

| Key | Action |
| --- | --- |
| Enter | Run the search (from the term, near or threads box) |
| Esc | Cancel the running search |
| Ctrl+E | Export results to CSV |
| Ctrl+L | Log out, back to the login screen |

Select a column in "Where it was found" and choose "Show all hits" to list every hit in it.

## Local test database

The integration tests, and the examples above, run against a throwaway Oracle Free container filled with synthetic data (`tests/db/seed.sql`). Nothing in it is real.

1. Create your `.env` and choose two passwords. `.env` is git-ignored; never commit it.

   ```bash
   cp .env.example .env
   # edit .env: set SCOUT_TEST_DB_PASSWORD (the SYSTEM account's password)
   #            and SCOUT_TEST_READER_PASSWORD (the low-privilege scout_reader account)
   ```

2. Start the container. The first start downloads `gvenzl/oracle-free` (about 1 GB), creates the database and loads the seed; allow a few minutes. `--wait` returns when the database is healthy.

   ```bash
   scripts/db.sh up
   ```

3. Check it, then run the tests:

   ```bash
   scout        # log in as scout_reader, service FREEPDB1, schema SCOUT_HR
   pytest -m integration
   ```

Day to day: `scripts/db.sh down` stops it and keeps the data, `scripts/db.sh logs` follows the log, and `scripts/db.sh reset` wipes the data volume and reseeds. The seed only runs on a fresh volume.

Already have an Oracle 19+ database you may write to? Skip Docker, point the `SCOUT_TEST_DB_*` variables in `.env` at it, and load the seed over a normal connection with `python -m tests.db.load_seed`. This drops and recreates the `SCOUT_*` users, so use a scratch database.

The tests skip, rather than fail, when the database is unreachable or the passwords are unset.

## Development

```bash
pytest                      # unit tests, no database needed
pytest -m integration       # tests against the test database
pytest -m bench             # benchmark; needs the datasets from python -m tests.bench.load / tests.bench.clobs
```

`CLAUDE.md` describes the design constraints and the exact search rules. The design notes and benchmark results are in `docs/superpowers/`.

## Limitations

- Matching is always a `REGEXP_LIKE` scan. Oracle Text indexes are not used yet.
- The whole-row view is in the terminal UI only. In the CLI, the CSV `sql_query` column gives a `SELECT` by rowid to paste into your SQL client.
- A search may contain at most 3 `*` wildcards.
- Oracle limits a search pattern to 512 bytes.
- A table that is being altered can't be read for a second after the change, so its columns may show up in the skipped list.
