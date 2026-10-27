import os
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import oracledb
import typer
from typer.exceptions import TyperException

from scout import db
from scout.config import (
    DEFAULT_LOCATE_LIMIT,
    DEFAULT_TIME_LIMIT,
    ConnectionConfig,
    default_schemas,
    default_threads,
    load_defaults,
    parse_schemas,
)
from scout.control import CancelToken, Progress
from scout.export import write_csv
from scout.pattern import build_regex
from scout.search import (
    Column,
    ColumnOutcome,
    ColumnTimeout,
    DictionaryTier,
    Match,
    SearchOptions,
    drill_down,
    resolve_column,
)
from scout.search import search as run_search

app = typer.Typer(help="SCOUT: search Oracle databases of unknown schema for strings.")


def _error(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)


def _db_failed(exc: oracledb.Error) -> typer.Exit:
    """Report a database error; returns the Exit to raise (exit status 1)."""
    _error(f"Database error: {exc}")
    return typer.Exit(1)


def _cancelled(hits: int) -> typer.Exit:
    """Report a cancel (Ctrl+C); returns the Exit to raise (exit status 130)."""
    typer.secho(f"Cancelled; {hits} hit(s) found so far.", fg=typer.colors.YELLOW, err=True)
    return typer.Exit(130)


def _wipe_line() -> None:
    """Clear the progress line on stderr, so the next output starts on a clean line."""
    typer.echo("\r\x1b[K", nl=False, err=True)


def _bar(fraction: float, width: int = 12) -> str:
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return "▓" * filled + "░" * (width - filled)


def _split_ref(ref: str) -> tuple[str, str]:
    """('OWNER.TABLE.', 'COLUMN'): the schema path is dimmed, the column name is what you look for."""
    head, _, tail = ref.rpartition(".")
    return (head + "." if head else ""), tail


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """SCOUT: search Oracle databases of unknown schema for strings.

    Run with no command to log in once and search interactively."""
    if ctx.invoked_subcommand is None:
        shell()


@app.command()
def shell(
    host: Annotated[str | None, typer.Option("--host")] = None,
    port: Annotated[int | None, typer.Option("--port")] = None,
    service: Annotated[str | None, typer.Option("--service", help="Service name.")] = None,
    schema: Annotated[list[str] | None, typer.Option("--schema", "-s", help="Pre-fills the schema prompt.")] = None,
    threads: Annotated[int | None, typer.Option("--threads", "-j", help="Connection pool size (default 8).")] = None,
    non_interactive: Annotated[
        bool, typer.Option("--non-interactive", help="Test use only: take user/password from SCOUT_TEST_* env.")
    ] = False,
) -> None:
    """Log in once, then run many searches at a scout> prompt."""
    cfg, schemas = _connection_config(host, port, service, schema, non_interactive, ask_endpoint=True)
    try:
        session = db.open_session(cfg, schemas, threads if threads is not None else default_threads())
    except oracledb.Error as exc:
        raise _db_failed(exc) from exc
    scope = ", ".join(schemas) if schemas else "every visible schema"
    typer.echo(
        typer.style("Connected", fg=typer.colors.GREEN, bold=True)
        + f" as {cfg.user}@{cfg.host}:{cfg.port}/{cfg.service_name}; searching {scope}.",
        err=True,
    )
    typer.echo("Type 'help' for commands, 'exit' to quit.", err=True)
    try:
        _shell_loop(session)
    finally:
        session.close()


def _shell_loop(session: db.Session) -> None:
    search_cmd = typer.main.get_command(app).commands["search"]  # type: ignore[attr-defined]
    while True:
        try:
            line = typer.prompt(typer.style("scout", fg=typer.colors.YELLOW, bold=True), default="", show_default=False, prompt_suffix="> ")
        except typer.Abort:  # Ctrl-D / Ctrl-C
            typer.echo()
            return
        try:
            args = shlex.split(line)
        except ValueError as exc:
            _error(f"Error: {exc}")
            continue
        if not args:
            continue
        name, rest = args[0].lower(), args[1:]
        if name in ("exit", "quit"):
            return
        if name == "help":
            for cmd, text in (
                ("search [OPTIONS] TERMS...", "search the schema(s) chosen at login (try: search --help)"),
                ("", "quote words with * or ? ; for a literal star use single quotes: search 'a\\*'"),
                ("more OWNER.TABLE.COLUMN", "every hit in one column of the last search"),
                ("help", "this list"),
                ("exit | quit", "leave"),
            ):
                typer.echo("  " + typer.style(f"{cmd:<27}", bold=True) + text)
        elif name == "more":
            if session.last_opts is None:
                typer.echo("Run a search first; 'more' shows every hit for a column it found.", err=True)
            elif len(rest) != 1:
                typer.echo("Usage: more OWNER.TABLE.COLUMN", err=True)
            else:
                every = replace(session.last_opts, limit_per_column=None, time_limit=None)
                try:
                    if not _drill(session, every, rest[0], None):
                        _error(f"No searchable column {rest[0]!r}.")
                except typer.Exit:
                    pass
        elif name == "search":
            try:
                search_cmd.main(rest, prog_name="search", standalone_mode=False, obj=session)
            except typer.Exit:
                pass
            except TyperException as exc:  # usage errors: show them, stay at the prompt
                getattr(exc, "show", lambda: typer.echo(f"Error: {exc}", err=True))()
            except SystemExit:  # typer exits after printing some errors itself
                pass
        else:
            _error(f"Unknown command {args[0]!r}; type 'help'.")


@app.command()
def search(
    ctx: typer.Context,
    terms: Annotated[
        list[str],
        typer.Argument(help="Search term(s): several words are a phrase. * and ? are wildcards (quote them; \\* is a literal *)."),
    ],
    whole_word: Annotated[
        bool, typer.Option("--whole-word", "-w", help="Match whole words only (default: also inside longer words).")
    ] = False,
    near: Annotated[
        int | None, typer.Option("--near", "-n", help="Two words within N words of each other, either order.")
    ] = None,
    case_sensitive: Annotated[bool, typer.Option("--case-sensitive/--ignore-case")] = False,
    all_hits: Annotated[
        bool, typer.Option("--all", help=f"Every hit in every column (default: up to {DEFAULT_LOCATE_LIMIT} examples per column).")
    ] = False,
    column_ref: Annotated[
        str | None, typer.Option("--column", help='Every hit in one column, e.g. --column "OWNER.TABLE.COLUMN".')
    ] = None,
    time_limit: Annotated[
        float | None,
        typer.Option("--time-limit", help=f"Seconds one query may run (default {DEFAULT_TIME_LIMIT:g} when locating; none with --all)."),
    ] = None,
    threads: Annotated[int | None, typer.Option("--threads", "-j", help="Worker threads (default 8).")] = None,
    schema: Annotated[
        list[str] | None, typer.Option("--schema", "-s", help="Schema(s) to search; asked at login.")
    ] = None,
    table: Annotated[list[str] | None, typer.Option("--table", "-t")] = None,
    dba: Annotated[bool, typer.Option("--dba", help="Use DBA_* views instead of ALL_*.")] = False,
    host: Annotated[str | None, typer.Option("--host")] = None,
    port: Annotated[int | None, typer.Option("--port")] = None,
    service: Annotated[str | None, typer.Option("--service", help="Service name.")] = None,
    csv_path: Annotated[Path | None, typer.Option("--csv", help="Write results to this CSV file.")] = None,
    non_interactive: Annotated[
        bool, typer.Option("--non-interactive", help="Test use only: take user/password from SCOUT_TEST_* env.")
    ] = False,
) -> None:
    """Search all string columns for TERMS: a few examples per column first (use --all for every hit)."""
    if time_limit is not None and time_limit <= 0:
        raise typer.BadParameter("must be more than 0 seconds", param_hint="--time-limit")
    locate = not all_hits and column_ref is None
    if time_limit is None and locate:
        time_limit = DEFAULT_TIME_LIMIT  # --all and --column run to the end unless asked
    base = SearchOptions(
        terms=tuple(terms),
        case_sensitive=case_sensitive,
        whole_word=whole_word,
        near=near,
        schemas=(),
        tables=tuple(table or ()),
        tier=DictionaryTier.DBA if dba else DictionaryTier.ALL,
        threads=threads if threads is not None else default_threads(),
        limit_per_column=DEFAULT_LOCATE_LIMIT if locate else None,
        time_limit=time_limit,
    )
    try:
        build_regex(base)  # reject bad terms before prompting for anything
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    session = ctx.obj if isinstance(ctx.obj, db.Session) else None  # set by the shell: already logged in
    if session is not None:
        cfg, schemas, pool = session.cfg, tuple(schema or session.schemas), session.pool
    else:
        cfg, schemas = _connection_config(host, port, service, schema, non_interactive)
        pool = None
    opts = replace(base, schemas=schemas)
    if session is not None:
        session.last_opts = opts

    if column_ref is not None:
        owned = None
        if session is None:
            try:
                session = owned = db.open_session(cfg, schemas, opts.threads)
            except oracledb.Error as exc:
                raise _db_failed(exc) from exc
        try:
            if not _drill(session, opts, column_ref, csv_path):
                raise typer.BadParameter(
                    f"No searchable column {column_ref!r}; use OWNER.TABLE.COLUMN of a text column.",
                    param_hint="--column",
                )
        finally:
            if owned is not None:
                owned.close()
        return

    found: list[Match] = []
    outcomes: dict[tuple[str, str, str], ColumnOutcome] = {}
    skipped: list[tuple[Column, Exception]] = []
    timed_out: list[Column] = []
    columns_searched = 0
    token = CancelToken()
    show_progress = sys.stderr.isatty()

    def progress(p: Progress) -> None:
        nonlocal columns_searched
        columns_searched = p.columns_total
        if show_progress:
            slow = f" · slowest {p.in_flight[0][0]} {p.in_flight[0][1]:.0f}s" if p.in_flight else ""
            line = f"{_bar(p.fraction)} {p.fraction:4.0%}  {p.columns_done}/{p.columns_total} columns · {p.hits} hits{slow}"
            typer.echo("\r" + typer.style(line, dim=True) + "\x1b[K", nl=False, err=True)

    def on_skip(col: Column, exc: Exception) -> None:
        if isinstance(exc, ColumnTimeout):
            timed_out.append(col)
        else:
            skipped.append((col, exc))

    gen = run_search(cfg, opts, progress, on_skip, pool, on_column=lambda o: outcomes.__setitem__(o.column.address, o), cancel=token)
    try:
        for m in gen:
            found.append(m)
            if show_progress:  # so the hit starts on its own line
                _wipe_line()
            typer.echo(_hit_line(m))
    except KeyboardInterrupt:
        token.cancel()
        gen.close()
        if show_progress:
            _wipe_line()
        raise _cancelled(len(found)) from None
    except oracledb.Error as exc:
        raise _db_failed(exc) from exc
    finally:
        if show_progress:
            _wipe_line()

    if token.cancelled:  # the interrupt reached the database but Python never saw the signal
        raise _cancelled(len(found))

    incomplete = {k for k, o in outcomes.items() if o.more_available or o.timed_out}
    labelled = opts.limit_per_column is not None or bool(incomplete)
    if csv_path:
        with csv_path.open("w", newline="") as fp:
            write_csv(found, fp, incomplete if labelled else None)
    summary = f"{len(found)} match(es) in {columns_searched} column(s)" + (f"; wrote {csv_path}" if csv_path else "")
    typer.secho(summary, fg=typer.colors.GREEN if found else None, bold=bool(found), err=True)
    if not found:
        if whole_word:
            typer.echo("Try again without --whole-word: it also finds the term inside longer words.", err=True)
        elif case_sensitive:
            typer.echo("Try again with --ignore-case.", err=True)
        else:
            typer.echo("Try fewer words, or a * wildcard.", err=True)
    _print_where_found(outcomes, terms, in_shell=session is not None, limited=opts.limit_per_column is not None)
    if csv_path and incomplete:
        typer.echo(
            f"Note: {csv_path} holds examples only for the columns marked complete=no; "
            "re-run with --all, or use --column for one column, to get every hit.",
            err=True,
        )
    if timed_out:
        typer.secho(f"Timed out after {opts.time_limit:g} s (not searched to the end), so a column with "
                    "no hits listed here may still contain the term:", fg=typer.colors.YELLOW, err=True)
        for col in sorted(timed_out, key=lambda c: c.address):
            typer.echo(f"  {col.ref}", err=True)
    if skipped:
        typer.secho(f"Skipped {len(skipped)} column(s) that could not be searched:", fg=typer.colors.YELLOW, err=True)
        for col, exc in sorted(skipped, key=lambda s: s[0].address):
            typer.echo(f"  {col.ref}: {str(exc).splitlines()[0]}", err=True)


def _hit_line(m: Match) -> str:
    """OWNER.TABLE.COLUMN<TAB>value. The colours vanish when stdout is not a terminal (click strips them),
    so a pipe or a file still gets exactly the plain tab-separated line."""
    head, tail = _split_ref(m.ref)
    return typer.style(head, dim=True) + typer.style(tail, bold=True) + "\t" + " ".join(m.value.split())


def _print_where_found(outcomes, terms, in_shell: bool, limited: bool) -> None:
    """The locate-mode summary: which columns have hits, and how to get all of them."""
    rows = sorted((o for o in outcomes.values() if o.examples), key=lambda o: o.column.address)
    if not rows:
        return
    typer.secho("Where it was found:", bold=True, err=True)
    width = max(len(o.column.ref) for o in rows)
    for o in rows:
        what = f"{o.examples} example(s)" if limited else f"{o.examples} hit(s)"
        head, tail = _split_ref(o.column.ref)
        pad = " " * (width - len(o.column.ref))
        flag = typer.style(f" ({o.note})", fg=typer.colors.YELLOW) if o.note else ""
        typer.echo(f"  {typer.style(head, dim=True)}{typer.style(tail, bold=True)}{pad}  {what:>15}{flag}", err=True)
        if o.more_available:
            if in_shell:
                hint = f"more {shlex.quote(o.column.ref)}"
            else:
                hint = f"scout search {shlex.join(terms)} --column {_quote_ref(o.column.ref)}"
            typer.echo(typer.style(f"      every hit: {hint}", dim=True), err=True)


def _quote_ref(ref: str) -> str:
    return '"' + ref.replace('"', '\\"') + '"'


def _drill(session: db.Session, opts: SearchOptions, ref: str, csv_path: Path | None) -> bool:
    """Print every hit for one column. Returns False if `ref` names no searchable column."""
    with db.acquire(session.pool) as conn:
        col = resolve_column(conn, ref, opts)
    if col is None:
        return False
    token, found, timed_out = CancelToken(), [], None
    gen = drill_down(session.pool, col, opts, token)
    try:
        for m in gen:
            found.append(m)
            typer.echo(_hit_line(m))
    except ColumnTimeout as exc:
        timed_out = exc  # what was found is kept, but the list is not every hit
    except KeyboardInterrupt:
        token.cancel()
        gen.close()
        raise _cancelled(len(found)) from None
    except oracledb.Error as exc:
        raise _db_failed(exc) from exc
    if token.cancelled:  # the interrupt reached the database but Python never saw the signal
        raise _cancelled(len(found))
    if csv_path:
        with csv_path.open("w", newline="") as fp:
            write_csv(found, fp, {col.address} if timed_out else None)
    typer.echo(f"{len(found)} hit(s) in {col.ref}" + (f"; wrote {csv_path}" if csv_path else ""), err=True)
    if timed_out:
        typer.secho(f"Timed out ({timed_out}): the {len(found)} hit(s) above are partial, not every hit in "
                    f"{col.ref}. Run again with a longer --time-limit or none.", fg=typer.colors.YELLOW, err=True)
    return True


def _connection_config(
    host: str | None,
    port: int | None,
    service: str | None,
    schema: list[str] | None,
    non_interactive: bool,
    ask_endpoint: bool = False,
) -> tuple[ConnectionConfig, tuple[str, ...]]:
    """Built-ins < defaults.txt/env < flags. User, password, service name and the schema to search
    are always asked, all before connecting (host and port too with ask_endpoint, for the shell).
    Returns (config, schemas); no schemas = all visible."""
    cfg = load_defaults().with_overrides(host=host, port=port, service_name=service)
    if non_interactive:  # test use only: no prompts, credentials from SCOUT_TEST_* env
        env = os.environ
        user, password = env.get("SCOUT_TEST_DB_USER"), env.get("SCOUT_TEST_DB_PASSWORD")
        if not user or not password:
            raise typer.BadParameter("--non-interactive needs SCOUT_TEST_DB_USER and SCOUT_TEST_DB_PASSWORD")
        cfg = cfg.with_overrides(
            host=host or env.get("SCOUT_TEST_DB_HOST"),
            port=port or (int(env["SCOUT_TEST_DB_PORT"]) if "SCOUT_TEST_DB_PORT" in env else None),
            service_name=service or env.get("SCOUT_TEST_DB_SERVICE_NAME"),
            user=user,
            password=password,
        )
        return cfg, tuple(schema or default_schemas())
    if ask_endpoint:
        cfg = cfg.with_overrides(
            host=typer.prompt("Host", default=cfg.host), port=typer.prompt("Port", default=cfg.port, type=int)
        )
    user = typer.prompt("Username")
    password = typer.prompt("Password", hide_input=True)
    service_name = typer.prompt("Service name", default=cfg.service_name)
    prefill = schema or default_schemas()
    schema_text = typer.prompt(
        "Schema to search (comma-separated, blank = all visible)",
        default=",".join(prefill),
        show_default=bool(prefill),
    )
    schemas = parse_schemas(schema_text)
    return cfg.with_overrides(user=user, password=password, service_name=service_name), schemas
