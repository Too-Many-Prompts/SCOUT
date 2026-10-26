import pytest
from typer.testing import CliRunner

from scout.cli import app

runner = CliRunner()


def test_help_lists_search_command():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0 and "search" in result.output


def test_search_help_exposes_options():
    result = runner.invoke(app, ["search", "--help"])
    assert result.exit_code == 0
    for opt in ("--whole-word", "--near", "--case-sensitive", "--threads", "--csv",
                "--host", "--port", "--service"):
        assert opt in result.output
    for gone in ("--mode", "--wildcard", "--partial", "--distance"):
        assert gone not in result.output


def test_proximity_needs_two_terms_before_any_prompt():
    result = runner.invoke(app, ["search", "one", "--near", "3"])
    assert result.exit_code == 2 and "Username" not in result.output


@pytest.mark.integration
def test_search_writes_csv(tmp_path, reader_config):
    out = tmp_path / "hits.csv"
    result = runner.invoke(
        app,
        ["search", "report", "-w", "--non-interactive", "--schema", "SCOUT_HR", "--csv", str(out)],
        env={"SCOUT_TEST_DB_USER": reader_config.user, "SCOUT_TEST_DB_PASSWORD": reader_config.password},
    )
    assert result.exit_code == 0, result.output
    lines = out.read_text().splitlines()
    # locate mode labels the file: every column here fit within the limit, so all rows are complete
    assert lines[0] == "owner,table,column,value,complete" and len(lines) == 4
    assert all(line.endswith(",yes") for line in lines[1:])
    assert "SCOUT_HR.EMPLOYEES.NOTES" in result.stdout


@pytest.mark.integration
def test_interactive_prompts_for_user_password_service_and_schema(reader_config):
    # all asked before connecting; the schema typed at login scopes the search
    result = runner.invoke(
        app, ["search", "report"],
        input=f"{reader_config.user}\n{reader_config.password}\n{reader_config.service_name}\nSCOUT_HR\n",
    )
    assert result.exit_code == 0, result.output
    for prompt in ("Username", "Password", "Service name", "Schema to search"):
        assert prompt in result.output
    assert "4 match(es)" in result.output  # default = contains, so 'Reportedly' counts; SCOUT_HR only
    assert "SCOUT_DOCS" not in result.stdout
    assert reader_config.password not in result.output  # never echoed


def test_bad_credentials_exit_cleanly_without_traceback():
    result = runner.invoke(app, ["search", "x"], input="nobody\nwrong\nNOSUCHSVC\n\n")
    assert result.exit_code == 1 and "Database error" in result.output


@pytest.mark.integration
def test_unsearchable_table_is_skipped_and_reported_at_the_end(reader_config):
    result = runner.invoke(
        app, ["search", "report"],
        input=f"{reader_config.user}\n{reader_config.password}\n{reader_config.service_name}\nSCOUT_HR,SCOUT_BROKEN\n",
    )
    assert result.exit_code == 0, result.output  # carried on despite the broken table
    assert "4 match(es)" in result.output
    tail = result.output.split("Skipped 1 column(s)")[1]
    assert "SCOUT_BROKEN.EXT_MISSING.TXT" in tail and "ORA-" in tail


def _fake_session(monkeypatch, fail=None):
    """db.open_session stub + search stub: returns (logins, searches) call logs."""
    from unittest.mock import MagicMock

    from scout import cli, db
    from scout.search import Match

    logins, searches = [], []

    def open_session(cfg, schemas, threads):
        logins.append((cfg, schemas, threads))
        if fail:
            raise fail
        return db.Session(cfg, schemas, MagicMock(name="pool"))

    def fake_search(cfg, opts, progress=None, on_skip=None, pool=None, **kw):
        searches.append((opts, pool))
        yield Match("O", "T", "C", "hit " + opts.terms[0])

    monkeypatch.setattr(db, "open_session", open_session)
    monkeypatch.setattr(cli, "run_search", fake_search)
    return logins, searches


def test_shell_logs_in_once_for_many_searches(monkeypatch):
    logins, searches = _fake_session(monkeypatch)
    result = runner.invoke(
        app, [],
        input="localhost\n1521\nme\nsecret\nSVC\nHR\nsearch alpha\nsearch beta --whole-word\nexit\n",
    )
    assert result.exit_code == 0, result.output
    assert len(logins) == 1
    cfg, schemas, _ = logins[0]
    assert (cfg.user, cfg.password, cfg.service_name, schemas) == ("me", "secret", "SVC", ("HR",))
    assert [o.terms for o, _ in searches] == [("alpha",), ("beta",)]
    assert searches[0][1] is searches[1][1]  # same session pool
    assert all(o.schemas == ("HR",) for o, _ in searches)  # login schema scopes each search
    assert "hit alpha" in result.stdout and "hit beta" in result.stdout
    assert "secret" not in result.output  # never echoed


def test_shell_schema_flag_overrides_login_schema_per_search(monkeypatch):
    _, searches = _fake_session(monkeypatch)
    result = runner.invoke(app, [], input="h\n1521\nme\npw\nSVC\nHR\nsearch x -s DOCS\nexit\n")
    assert result.exit_code == 0, result.output
    assert searches[0][0].schemas == ("DOCS",)


def test_shell_survives_bad_input_and_exits_on_eof(monkeypatch):
    logins, searches = _fake_session(monkeypatch)
    result = runner.invoke(
        app, ["shell"],
        input="h\n1521\nme\npw\nSVC\n\nbogus\nsearch\nsearch a --bogus-flag\nsearch \"unclosed\nsearch ok\n",
    )  # EOF at the end acts like exit
    assert result.exit_code == 0, result.output
    assert "Unknown command" in result.output
    assert [o.terms for o, _ in searches] == [("ok",)]  # the bad lines did not end the session


def test_shell_bad_credentials_exit_cleanly(monkeypatch):
    import oracledb

    _fake_session(monkeypatch, fail=oracledb.DatabaseError("ORA-01017: invalid credential"))
    result = runner.invoke(app, [], input="h\n1521\nme\nwrong\nSVC\n\n")
    assert result.exit_code == 1 and "Database error" in result.output and "scout>" not in result.output


def test_shell_closes_session_on_exit(monkeypatch):
    from scout import db

    sessions = []
    real = db.Session.close
    monkeypatch.setattr(db.Session, "close", lambda self: (sessions.append(self), real(self))[1])
    _fake_session(monkeypatch)
    runner.invoke(app, [], input="h\n1521\nme\npw\nSVC\n\nexit\n")
    assert len(sessions) == 1


@pytest.mark.integration
def test_shell_runs_two_real_searches_on_one_login(reader_config, monkeypatch):
    from scout import db

    logins = []
    real = db.open_session
    monkeypatch.setattr(db, "open_session", lambda *a: (logins.append(a), real(*a))[1])
    result = runner.invoke(
        app, [],
        input=(f"{reader_config.host}\n{reader_config.port}\n{reader_config.user}\n{reader_config.password}\n"
               f"{reader_config.service_name}\nSCOUT_HR\nsearch report\nsearch report -w --csv {{csv}}\nexit\n"
               ).replace("{csv}", "/dev/null"),
    )
    assert result.exit_code == 0, result.output
    assert len(logins) == 1
    assert "4 match(es)" in result.output and "3 match(es)" in result.output  # contains, then -w
    assert reader_config.password not in result.output


def test_shell_single_quotes_keep_an_escaped_wildcard_intact(monkeypatch):
    _, searches = _fake_session(monkeypatch)
    result = runner.invoke(app, [], input="h\n1521\nme\npw\nSVC\n\nsearch 'a\\*b' 'c*'\nexit\n")
    assert result.exit_code == 0, result.output
    assert searches[0][0].terms == ("a\\*b", "c*")  # backslash survives shlex inside single quotes


def test_bare_wildcard_is_refused_before_any_prompt():
    for term in ("*", "?", "%"):
        result = runner.invoke(app, ["search", term])
        assert result.exit_code == 2, term
        assert "wildcard" in result.output and "Username" not in result.output


# ---- locate mode in the CLI and shell

def _located(monkeypatch, outcomes=(), skips=(), hits=(), raise_interrupt=False, sink=None):
    """Replace cli.run_search with a fake that reports the given matches/outcomes/skips."""
    from scout import cli

    calls = sink if sink is not None else []

    def fake_search(cfg, opts, progress=None, on_skip=None, pool=None, on_column=None, cancel=None, **kw):
        calls.append({"opts": opts, "pool": pool, "cancel": cancel})
        if raise_interrupt:
            raise KeyboardInterrupt
        yield from hits
        for o in outcomes:
            on_column(o)
        for col, exc in skips:
            on_skip(col, exc)

    monkeypatch.setattr(cli, "run_search", fake_search)
    return calls


ENV = {"SCOUT_TEST_DB_USER": "u", "SCOUT_TEST_DB_PASSWORD": "p"}


def _col(name="A"):
    from scout.search import Column

    return Column("O", "T", name, "VARCHAR2")


def _outcome(name="A", examples=5, more=True, timed_out=False):
    from scout.search import ColumnOutcome

    return ColumnOutcome(_col(name), examples, more, timed_out)


def _hits(*names):
    from scout.search import Match

    return [Match("O", "T", n, f"value in {n}") for n in names]


def test_default_search_is_locate_mode_with_a_where_found_summary(monkeypatch):
    calls = _located(monkeypatch, outcomes=[_outcome("A", 5, True), _outcome("B", 2, False)],
                     hits=_hits("A", "B"))
    result = runner.invoke(app, ["search", "x", "--non-interactive"], env=ENV)
    assert result.exit_code == 0, result.output
    assert calls[0]["opts"].limit_per_column == 5 and calls[0]["opts"].time_limit == 300.0
    summary = result.output.split("Where it was found")[1]
    assert "O.T.A" in summary and "5 example" in summary and "(more available)" in summary
    assert "O.T.B" in summary and summary.count("(more available)") == 1
    assert 'scout search x --column "O.T.A"' in summary  # the exact drill-down command


def test_all_flag_means_every_hit_and_no_time_limit(monkeypatch):
    calls = _located(monkeypatch, outcomes=[_outcome("A", 7, False)], hits=_hits("A"))
    result = runner.invoke(app, ["search", "x", "--all", "--non-interactive"], env=ENV)
    assert result.exit_code == 0, result.output
    assert calls[0]["opts"].limit_per_column is None and calls[0]["opts"].time_limit is None
    assert "(more available)" not in result.output


def test_time_limit_option_applies_in_either_mode(monkeypatch):
    calls = _located(monkeypatch)
    runner.invoke(app, ["search", "x", "--time-limit", "12.5", "--non-interactive"], env=ENV)
    runner.invoke(app, ["search", "x", "--all", "--time-limit", "30", "--non-interactive"], env=ENV)
    assert [c["opts"].time_limit for c in calls] == [12.5, 30.0]
    assert runner.invoke(app, ["search", "x", "--time-limit", "0", "--non-interactive"], env=ENV).exit_code == 2


def test_locate_csv_has_a_complete_column_and_a_notice_all_does_not(monkeypatch, tmp_path):
    _located(monkeypatch, outcomes=[_outcome("A", 5, True), _outcome("B", 1, False)], hits=_hits("A", "B"))
    out = tmp_path / "loc.csv"
    result = runner.invoke(app, ["search", "x", "--non-interactive", "--csv", str(out)], env=ENV)
    assert result.exit_code == 0, result.output
    assert out.read_text().splitlines() == [
        "owner,table,column,value,complete", "O,T,A,value in A,no", "O,T,B,value in B,yes"]
    assert "examples" in result.output and "--all" in result.output  # the file is labelled as examples
    full = tmp_path / "all.csv"
    _located(monkeypatch, outcomes=[_outcome("A", 7, False)], hits=_hits("A"))  # --all: nothing is cut off
    runner.invoke(app, ["search", "x", "--all", "--non-interactive", "--csv", str(full)], env=ENV)
    assert full.read_text().splitlines()[0] == "owner,table,column,value"


def test_timed_out_columns_are_never_reported_as_empty(monkeypatch):
    from scout.search import ColumnTimeout

    _located(monkeypatch,
             outcomes=[_outcome("EMPTY", 0, False, timed_out=True), _outcome("PART", 2, False, timed_out=True)],
             skips=[(_col("EMPTY"), ColumnTimeout("timed out after 300 s"))], hits=_hits("PART", "PART"))
    result = runner.invoke(app, ["search", "x", "--non-interactive"], env=ENV)
    assert result.exit_code == 0, result.output
    assert "Timed out" in result.output and "O.T.EMPTY" in result.output
    assert "Skipped" not in result.output  # a timeout is not a failure to search
    assert "(partial: timed out)" in result.output.split("Where it was found")[1]


def test_ctrl_c_cancels_and_exits_130(monkeypatch):
    calls = _located(monkeypatch, raise_interrupt=True)
    result = runner.invoke(app, ["search", "x", "--non-interactive"], env=ENV)
    assert result.exit_code == 130 and "Cancelled" in result.output
    assert calls[0]["cancel"].cancelled


def _drill_fakes(monkeypatch, known=("O.T.A",), logins=None):
    from unittest.mock import MagicMock

    from scout import cli, db
    from scout.search import Column, Match

    drilled = []

    def open_session(cfg, schemas, threads):
        (logins if logins is not None else []).append(cfg)
        return db.Session(cfg, schemas, MagicMock(name="pool"))

    def fake_resolve(conn, ref, opts):
        return Column("O", "T", "A", "VARCHAR2") if ref in known else None

    def fake_drill(pool, column, opts, cancel=None):
        drilled.append((column, opts, cancel))
        yield Match("O", "T", "A", "every hit 1")
        yield Match("O", "T", "A", "every hit 2")

    monkeypatch.setattr(db, "open_session", open_session)
    monkeypatch.setattr(db, "acquire", lambda pool: MagicMock(__enter__=lambda s: MagicMock(), __exit__=lambda *a: False))
    monkeypatch.setattr(cli, "resolve_column", fake_resolve)
    monkeypatch.setattr(cli, "drill_down", fake_drill)
    return drilled


def test_column_option_prints_every_hit_for_one_column(monkeypatch):
    drilled = _drill_fakes(monkeypatch)
    result = runner.invoke(app, ["search", "x", "--column", "O.T.A", "--non-interactive"], env=ENV)
    assert result.exit_code == 0, result.output
    assert "every hit 1" in result.stdout and "every hit 2" in result.stdout
    column, opts, _ = drilled[0]
    assert opts.limit_per_column is None and opts.time_limit is None and opts.terms == ("x",)


def test_unknown_column_exits_2_with_a_clear_message(monkeypatch):
    _drill_fakes(monkeypatch)
    result = runner.invoke(app, ["search", "x", "--column", "O.T.NOPE", "--non-interactive"], env=ENV)
    assert result.exit_code == 2 and "No searchable column" in result.output and "Traceback" not in result.output


def test_shell_more_reuses_the_last_search_and_survives_mistakes(monkeypatch):
    calls = _located(monkeypatch, outcomes=[_outcome("A", 5, True)], hits=_hits("A"))
    drilled = _drill_fakes(monkeypatch)  # also fakes db.open_session
    login = "h\n1521\nme\npw\nSVC\n\n"
    result = runner.invoke(app, [], input=login + "more O.T.A\nsearch alpha --whole-word\nmore O.T.NOPE\nmore O.T.A\nexit\n")
    assert result.exit_code == 0, result.output
    assert "Run a search first" in result.output          # before any search
    assert "No searchable column" in result.output         # unknown ref: stays in the shell
    column, opts, _ = drilled[0]
    assert opts.terms == ("alpha",) and opts.whole_word and opts.limit_per_column is None
    assert 'more O.T.A' in result.output                     # the hint names the shell command


def test_shell_survives_ctrl_c_during_a_search(monkeypatch):
    from unittest.mock import MagicMock

    from scout import cli, db
    from scout.search import Match

    monkeypatch.setattr(db, "open_session", lambda cfg, schemas, threads: db.Session(cfg, schemas, MagicMock()))
    seen = []

    def fake_search(cfg, opts, progress=None, on_skip=None, pool=None, on_column=None, cancel=None, **kw):
        seen.append(opts.terms[0])
        if opts.terms[0] == "a":
            raise KeyboardInterrupt
        yield Match("O", "T", "C", "found b")

    monkeypatch.setattr(cli, "run_search", fake_search)
    result = runner.invoke(app, [], input="h\n1521\nme\npw\nSVC\n\nsearch a\nsearch b\nexit\n")
    assert result.exit_code == 0, result.output
    assert seen == ["a", "b"] and "Cancelled" in result.output and "found b" in result.stdout
