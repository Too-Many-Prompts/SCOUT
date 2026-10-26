import os
import shlex
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import oracledb
import typer
from typer.exceptions import TyperException

from scout import db
from scout.control import Progress
from scout.config import ConnectionConfig, default_schemas, default_threads, load_defaults
from scout.export import write_csv
from scout.pattern import build_regex
from scout.search import Column, DictionaryTier, Match, SearchOptions
from scout.search import search as run_search

app = typer.Typer(help="SCOUT: search Oracle databases of unknown schema for strings.")


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
        typer.secho(f"Database error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.echo("Connected. Type 'help' for commands, 'exit' to quit.", err=True)
    try:
        _shell_loop(session)
    finally:
        session.close()


def _shell_loop(session: db.Session) -> None:
    search_cmd = typer.main.get_command(app).commands["search"]  # type: ignore[attr-defined]
    while True:
        try:
            line = typer.prompt("scout", default="", show_default=False, prompt_suffix="> ")
        except typer.Abort:  # Ctrl-D / Ctrl-C
            typer.echo()
            return
        try:
            args = shlex.split(line)
        except ValueError as exc:
            typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
            continue
        if not args:
            continue
        name, rest = args[0].lower(), args[1:]
        if name in ("exit", "quit"):
            return
        if name == "help":
            typer.echo("  search [OPTIONS] TERMS...   search the schema(s) chosen at login (try: search --help)")
            typer.echo("      quote words with * or ? ; for a literal star use single quotes: search 'a\\*'")
            typer.echo("  help                        this list")
            typer.echo("  exit | quit                 leave")
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
            typer.secho(f"Unknown command {args[0]!r}; type 'help'.", fg=typer.colors.RED, err=True)


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
    """Search all string columns for TERMS."""
    base = SearchOptions(
        terms=tuple(terms),
        case_sensitive=case_sensitive,
        whole_word=whole_word,
        near=near,
        schemas=(),
        tables=tuple(table or ()),
        tier=DictionaryTier.DBA if dba else DictionaryTier.ALL,
        threads=threads if threads is not None else default_threads(),
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
    found: list[Match] = []
    skipped: list[tuple[Column, Exception]] = []
    columns_searched = 0

    def progress(p: Progress) -> None:
        nonlocal columns_searched
        columns_searched = p.columns_total

    try:
        for m in run_search(cfg, opts, progress, lambda col, exc: skipped.append((col, exc)), pool):
            found.append(m)
            typer.echo(f"{m.owner}.{m.table}.{m.column}\t{' '.join(m.value.split())}")
    except oracledb.Error as exc:
        typer.secho(f"Database error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    if csv_path:
        with csv_path.open("w", newline="") as fp:
            write_csv(found, fp)
    typer.echo(
        f"{len(found)} match(es) in {columns_searched} column(s)" + (f"; wrote {csv_path}" if csv_path else ""),
        err=True,
    )
    if skipped:
        typer.secho(f"Skipped {len(skipped)} column(s) that could not be searched:", fg=typer.colors.YELLOW, err=True)
        for col, exc in sorted(skipped, key=lambda s: (s[0].owner, s[0].table, s[0].column)):
            typer.echo(f"  {col.owner}.{col.table}.{col.column}: {str(exc).splitlines()[0]}", err=True)


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
    schema_text = typer.prompt(
        "Schema to search (comma-separated, blank = all visible)",
        default=",".join(schema or default_schemas()),
        show_default=bool(schema or default_schemas()),
    )
    schemas = tuple(s.strip() for s in schema_text.split(",") if s.strip())
    return cfg.with_overrides(user=user, password=password, service_name=service_name), schemas
