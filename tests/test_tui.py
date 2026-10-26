from unittest.mock import MagicMock

from textual.widgets import DataTable

from scout import db
from scout.tui import LoginScreen, ScoutApp


def fake_login(monkeypatch, fail=None):
    """Replace db.open_session; returns the list of (cfg, schemas, threads) logins attempted."""
    calls = []

    def open_session(cfg, schemas, threads):
        calls.append((cfg, schemas, threads))
        if fail:
            raise fail
        return db.Session(cfg, schemas, MagicMock(name="pool"))

    monkeypatch.setattr(db, "open_session", open_session)
    return calls


async def login(app, pilot, schema=""):
    app.screen.query_one("#login-user").value = "u"
    app.screen.query_one("#login-password").value = "pw"
    app.screen.query_one("#login-schema").value = schema
    await pilot.click("#login-button")
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_login_screen_comes_first_and_masks_password(monkeypatch):
    monkeypatch.setenv("SCOUT_DB_HOST", "db.example")
    app = ScoutApp()
    async with app.run_test() as pilot:
        assert isinstance(app.screen, LoginScreen)
        for wid in ("login-host", "login-port", "login-service", "login-user", "login-password", "login-schema", "login-button"):
            assert app.screen.query_one(f"#{wid}")
        assert app.screen.query_one("#login-password").password is True
        assert app.screen.query_one("#login-host").value == "db.example"  # defaults pre-filled
        assert app.screen.query_one("#login-user").value == ""  # credentials never defaulted
        await pilot.pause()


async def test_main_screen_has_expected_widgets_after_login(monkeypatch):
    fake_login(monkeypatch)
    app = ScoutApp()
    async with app.run_test() as pilot:
        await login(app, pilot)
        assert not isinstance(app.screen, LoginScreen)
        for wid in ("terms", "near", "whole_word", "case", "threads", "search", "export"):
            assert app.query_one(f"#{wid}")
        table = app.query_one("#results", DataTable)
        assert [str(c.label) for c in table.columns.values()] == ["Owner", "Table", "Column", "Value"]


async def test_failed_login_stays_on_login_screen(monkeypatch):
    fake_login(monkeypatch, fail=RuntimeError("ORA-01017: invalid credential"))
    app = ScoutApp()
    async with app.run_test() as pilot:
        await login(app, pilot)
        assert isinstance(app.screen, LoginScreen)
        assert "ORA-01017" in str(app.screen.query_one("#login-error").render())
        assert app.screen.query_one("#login-button").disabled is False  # can retry
        assert app.session is None


async def test_login_requires_user_and_password(monkeypatch):
    calls = fake_login(monkeypatch)
    app = ScoutApp()
    async with app.run_test() as pilot:
        await pilot.click("#login-button")
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen) and calls == []
        assert "required" in str(app.screen.query_one("#login-error").render())


async def test_logout_closes_pool_and_returns_to_prefilled_login(monkeypatch):
    fake_login(monkeypatch)
    app = ScoutApp()
    async with app.run_test() as pilot:
        await login(app, pilot, schema="O")
        pool = app.session.pool
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen) and app.session is None
        pool.close.assert_called_once()
        assert app.screen.query_one("#login-user").value == "u"
        assert app.screen.query_one("#login-schema").value == "O"
        assert app.screen.query_one("#login-password").value == ""  # password is forgotten


async def test_two_searches_share_one_login_and_pool(monkeypatch):
    from scout import search as search_core
    from scout.search import Match

    calls = fake_login(monkeypatch)
    pools = []

    def fake_search(cfg, opts, progress=None, on_skip=None, pool=None, **kw):
        pools.append(pool)
        yield Match("O", "T", "C", "hit")

    monkeypatch.setattr(search_core, "search", fake_search)
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        for term in ("one", "two"):
            app.query_one("#terms").value = term
            await pilot.click("#search")
            await app.workers.wait_for_complete()
            await pilot.pause(0.5)  # a click inside the button's pressed animation is ignored
        assert len(calls) == 1
        assert len(pools) == 2 and pools[0] is pools[1] is app.session.pool


async def test_search_populates_results_and_exports_csv(monkeypatch, tmp_path):
    fake_login(monkeypatch)
    from scout import search as search_core
    from scout.search import Match

    fake = [Match("O", "T", "C", "hello\nworld"), Match("A", "T", "C", "x")]
    monkeypatch.setattr(search_core, "search", lambda cfg, opts, progress=None, on_skip=None, pool=None: iter(fake))
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "hello"
        app.query_one("#csv_path").value = str(tmp_path / "out.csv")
        await pilot.click("#search")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one("#results", DataTable).row_count == 2
        await pilot.click("#export")
        await pilot.pause()
    lines = (tmp_path / "out.csv").read_text().splitlines()
    assert lines[0] == "owner,table,column,value" and lines[1].startswith("A,T,C")


async def test_bad_term_reports_error_without_searching(monkeypatch):
    fake_login(monkeypatch)
    from scout import search as search_core

    monkeypatch.setattr(search_core, "search", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        await pilot.click("#search")  # empty term
        await pilot.pause()
        assert "Error" in str(app.query_one("#status").render())


async def test_second_search_cancels_first_without_mixing_results(monkeypatch):
    import threading

    fake_login(monkeypatch)

    from scout import search as search_core
    from scout.search import Match

    release = threading.Event()

    def fake_search(cfg, opts, progress=None, on_skip=None, pool=None, **kw):
        if opts.terms == ("first",):
            yield Match("OLD", "T", "C", "a")
            release.wait(5)  # still running when the second search starts
            yield Match("OLD", "T", "C", "b")  # must be dropped: this worker was cancelled
        else:
            yield Match("NEW", "T", "C", "z")

    monkeypatch.setattr(search_core, "search", fake_search)
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "first"
        await pilot.click("#search")
        await pilot.pause(0.2)
        app.query_one("#terms").value = "second"
        await pilot.click("#search")
        await pilot.pause(0.2)
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert [m.owner for m in app.matches] == ["NEW"]


async def test_skipped_columns_are_listed_after_the_search(monkeypatch):
    fake_login(monkeypatch)
    from scout import search as search_core
    from scout.search import Column, Match

    def fake_search(cfg, opts, progress=None, on_skip=None, pool=None, **kw):
        yield Match("O", "T", "C", "hit")
        on_skip(Column("O", "BAD", "TXT", "VARCHAR2"), Exception("ORA-29913: boom\nmore"))

    monkeypatch.setattr(search_core, "search", fake_search)
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot, schema="O, P")
        assert app.session.schemas == ("O", "P")  # schema scope comes from the login
        app.query_one("#terms").value = "x"
        await pilot.click("#search")
        await app.workers.wait_for_complete()
        await pilot.pause()
        skipped = app.query_one("#skipped", DataTable)
        assert skipped.display and skipped.row_count == 1
        assert "skipped 1" in str(app.query_one("#status").render())


async def test_whole_word_and_near_fields_build_the_search_options(monkeypatch):
    from scout import search as search_core

    fake_login(monkeypatch)
    seen = []

    def fake_search(cfg, opts, progress=None, on_skip=None, pool=None, **kw):
        seen.append(opts)
        return iter(())

    monkeypatch.setattr(search_core, "search", fake_search)
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "quick lazy"
        await pilot.click("#search")
        await app.workers.wait_for_complete()
        await pilot.pause(0.5)
        app.query_one("#whole_word").value = True
        app.query_one("#near").value = "4"
        await pilot.click("#search")
        await app.workers.wait_for_complete()
        await pilot.pause()
    first, second = seen
    assert (first.terms, first.whole_word, first.near) == (("quick lazy",), False, None)  # contains by default
    assert (second.terms, second.whole_word, second.near) == (("quick", "lazy"), True, 4)


async def test_bare_wildcard_term_is_refused_in_plain_words(monkeypatch):
    fake_login(monkeypatch)
    from scout import search as search_core

    monkeypatch.setattr(search_core, "search", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "*"
        await pilot.click("#search")
        await pilot.pause()
        assert "wildcard" in str(app.query_one("#status").render())
