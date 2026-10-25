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
    assert lines[0] == "owner,table,column,value" and len(lines) == 4
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

    def fake_search(cfg, opts, progress=None, on_skip=None, pool=None):
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
