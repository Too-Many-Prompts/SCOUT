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

## Requirements

- Python 3.10 or newer
- Oracle Instant Client. SCOUT uses python-oracledb in thick mode and finds the client through `ldconfig`, or set `SCOUT_ORACLE_CLIENT_LIB_DIR`.
- An Oracle account with `SELECT` on the tables you want to search

## Install

```bash
pip install -e ".[dev]"
```

This installs the `scout` command. Leave off `[dev]` if you don't need the test tools.

## Quick start

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
SCOUT_HR.EMPLOYEES.NOTES    Talbert moved to Albertson Rd
SCOUT_HR.EMPLOYEES.BIO      Albert Einstein fan
2 match(es) in 140 column(s)
Where it was found:
  SCOUT_HR.EMPLOYEES.NOTES     5 example(s) (more available)
      every hit: more SCOUT_HR.EMPLOYEES.NOTES
  SCOUT_HR.EMPLOYEES.BIO       2 example(s)
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

## Development

```bash
pytest                      # unit tests, no database needed
scripts/db.sh up            # test Oracle in Docker (gvenzl/oracle-free), seeded on first start
cp .env.example .env        # then set the passwords
pytest -m integration       # tests against that database
pytest -m bench             # benchmark; needs the dataset from python -m tests.bench.load
```

`scripts/db.sh` also takes `down`, `reset` (wipes the data) and `logs`.

`CLAUDE.md` describes the design constraints and the exact search rules. The design notes and benchmark results are in `docs/superpowers/`.

## Limitations

- Matching is always a `REGEXP_LIKE` scan. Oracle Text indexes are not used yet.
- Fetching the whole row behind a hit is not implemented yet.
- Oracle limits a search pattern to 512 bytes.
- A table that is being altered can't be read for a second after the change, so its columns may show up in the skipped list.
