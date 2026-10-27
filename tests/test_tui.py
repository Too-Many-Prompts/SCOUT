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
    monkeypatch.setattr(search_core, "search", lambda cfg, opts, progress=None, on_skip=None, pool=None, **kw: iter(fake))
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
    # a locate-mode export is labelled; with no cut-off column every row says complete=yes
    assert lines[0] == "owner,table,column,value,complete" and lines[1].startswith("A,T,C")
    assert lines[1].endswith(",yes")


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


# ---- locate mode in the TUI

def _col(name="A"):
    from scout.search import Column

    return Column("O", "T", name, "VARCHAR2")


def _outcome(name="A", examples=5, more=True, timed_out=False):
    from scout.search import ColumnOutcome

    return ColumnOutcome(_col(name), examples, more, timed_out)


def locate_fake(monkeypatch, calls=None, outcomes=(), hits=(), skips=(), hold=None, progress_event=None):
    """Replace search_core.search; `hold` (threading.Event) keeps the fake search running."""
    from scout import search as search_core

    def fake_search(cfg, opts, progress=None, on_skip=None, pool=None, on_column=None, cancel=None, **kw):
        if calls is not None:
            calls.append({"opts": opts, "cancel": cancel})
        if progress_event is not None:
            progress(progress_event)
        yield from hits
        for o in outcomes:
            on_column(o)
        for col, exc in skips:
            on_skip(col, exc)
        if hold is not None:
            while not hold.is_set() and not (cancel and cancel.cancelled):
                hold.wait(0.02)

    monkeypatch.setattr(search_core, "search", fake_search)


async def test_locate_fills_the_found_table_and_flags_more_available(monkeypatch):
    from scout.search import Match

    fake_login(monkeypatch)
    calls = []
    locate_fake(monkeypatch, calls, outcomes=[_outcome("A", 5, True), _outcome("B", 2, False)],
                hits=[Match("O", "T", "A", "a1"), Match("O", "T", "B", "b1")])
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "x"
        await pilot.click("#search")
        await app.workers.wait_for_complete()
        await pilot.pause()
        found = app.query_one("#found", DataTable)
        rows = [tuple(str(c) for c in found.get_row_at(i)) for i in range(found.row_count)]
        assert rows == [("O.T.A", "5", "more available"), ("O.T.B", "2", "")]
    opts = calls[0]["opts"]
    assert opts.limit_per_column == 5 and opts.time_limit == 300.0


async def test_every_hit_checkbox_turns_locate_mode_off(monkeypatch):
    fake_login(monkeypatch)
    calls = []
    locate_fake(monkeypatch, calls)
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "x"
        app.query_one("#all_hits").value = True
        await pilot.click("#search")
        await app.workers.wait_for_complete()
    assert calls[0]["opts"].limit_per_column is None and calls[0]["opts"].time_limit is None


async def test_show_all_hits_replaces_the_results_with_every_hit_for_the_selected_column(monkeypatch):
    from scout import search as search_core
    from scout.search import Match

    fake_login(monkeypatch)
    locate_fake(monkeypatch, outcomes=[_outcome("A", 5, True)], hits=[Match("O", "T", "A", "a1")])
    drilled = []

    def fake_drill(pool, column, opts, cancel=None):
        drilled.append((column, opts))
        for i in range(7):
            yield Match("O", "T", "A", f"hit {i}")

    monkeypatch.setattr(search_core, "drill_down", fake_drill)
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "x"
        await pilot.click("#search")
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.query_one("#found", DataTable).move_cursor(row=0)
        await pilot.click("#show-all")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one("#results", DataTable).row_count == 7
        assert "O.T.A" in str(app.query_one("#status").render())
    column, opts = drilled[0]
    assert column.column == "A" and opts.limit_per_column is None and opts.time_limit is None


async def test_show_all_without_a_selected_column_says_what_to_do(monkeypatch):
    fake_login(monkeypatch)
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        await pilot.click("#show-all")
        await pilot.pause()
        assert "Select a column" in str(app.query_one("#status").render())


async def test_cancel_stops_the_search_and_says_so(monkeypatch):
    import threading

    fake_login(monkeypatch)
    calls = []
    locate_fake(monkeypatch, calls, hold=threading.Event())  # runs until cancelled
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "x"
        await pilot.click("#search")
        await pilot.pause(0.2)
        await pilot.click("#cancel")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert calls[0]["cancel"].cancelled
        assert "Cancelled" in str(app.query_one("#status").render())


async def test_escape_also_cancels(monkeypatch):
    import threading

    fake_login(monkeypatch)
    calls = []
    locate_fake(monkeypatch, calls, hold=threading.Event())
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "x"
        await pilot.click("#search")
        await pilot.pause(0.2)
        await pilot.press("escape")
        await app.workers.wait_for_complete()
        assert calls[0]["cancel"].cancelled


async def test_status_shows_progress_and_the_slowest_table(monkeypatch):
    import threading

    from scout.control import Progress

    fake_login(monkeypatch)
    release = threading.Event()
    locate_fake(monkeypatch, hold=release,
                progress_event=Progress(3, 20, 0.35, (("S.BIG", 42.0), ("S.SMALL", 3.0)), 5, 21, 140))
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "x"
        await pilot.click("#search")
        await pilot.pause(0.3)
        text = str(app.query_one("#status").render())
        release.set()
        await app.workers.wait_for_complete()
    assert "35%" in text and "S.BIG" in text and "42s" in text and "5 hits" in text


async def test_locate_export_marks_cut_off_columns_and_says_examples_only(monkeypatch, tmp_path):
    from scout.search import Match

    fake_login(monkeypatch)
    locate_fake(monkeypatch, outcomes=[_outcome("A", 5, True), _outcome("B", 1, False)],
                hits=[Match("O", "T", "A", "a1"), Match("O", "T", "B", "b1")])
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "x"
        app.query_one("#csv_path").value = str(tmp_path / "loc.csv")
        await pilot.click("#search")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.click("#export")
        await pilot.pause()
        status = str(app.query_one("#status").render())
    assert (tmp_path / "loc.csv").read_text().splitlines() == [
        "owner,table,column,value,complete", "O,T,A,a1,no", "O,T,B,b1,yes"]
    assert "examples" in status


async def test_timed_out_columns_are_listed_as_timed_out(monkeypatch):
    from scout.search import ColumnTimeout

    fake_login(monkeypatch)
    locate_fake(monkeypatch, outcomes=[_outcome("A", 0, False, timed_out=True)],
                skips=[(_col("A"), ColumnTimeout("timed out after 300 s"))])
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "x"
        await pilot.click("#search")
        await app.workers.wait_for_complete()
        await pilot.pause()
        skipped = app.query_one("#skipped", DataTable)
        assert skipped.display and "timed out" in str(skipped.get_row_at(0)[1])


async def test_export_after_show_all_hits_is_a_complete_unlabelled_file(monkeypatch, tmp_path):
    from scout import search as search_core
    from scout.search import Match

    fake_login(monkeypatch)
    locate_fake(monkeypatch, outcomes=[_outcome("A", 5, True)], hits=[Match("O", "T", "A", "a1")])
    monkeypatch.setattr(search_core, "drill_down",
                        lambda pool, column, opts, cancel=None: iter([Match("O", "T", "A", f"h{i}") for i in range(3)]))
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "x"
        app.query_one("#csv_path").value = str(tmp_path / "all.csv")
        await pilot.click("#search")
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.query_one("#found", DataTable).move_cursor(row=0)
        await pilot.click("#show-all")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.click("#export")
        await pilot.pause()
        status = str(app.query_one("#status").render())
    lines = (tmp_path / "all.csv").read_text().splitlines()
    assert lines[0] == "owner,table,column,value" and len(lines) == 4  # complete: no 'complete' column
    assert "examples" not in status


async def test_export_after_a_cancelled_search_is_labelled_partial(monkeypatch, tmp_path):
    import threading

    from scout.search import Match

    fake_login(monkeypatch)
    locate_fake(monkeypatch, hits=[Match("O", "T", "A", "a1")], hold=threading.Event())
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "x"
        app.query_one("#all_hits").value = True  # even in every-hit mode a cancelled run is partial
        app.query_one("#csv_path").value = str(tmp_path / "part.csv")
        await pilot.click("#search")
        await pilot.pause(0.3)
        await pilot.click("#cancel")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.click("#export")
        await pilot.pause()
        status = str(app.query_one("#status").render())
    assert (tmp_path / "part.csv").read_text().splitlines() == [
        "owner,table,column,value,complete", "O,T,A,a1,no"]
    assert "partial" in status


async def test_a_timed_out_show_all_is_reported_as_partial(monkeypatch):
    from scout import search as search_core
    from scout.search import ColumnTimeout, Match

    fake_login(monkeypatch)
    locate_fake(monkeypatch, outcomes=[_outcome("A", 5, True)], hits=[Match("O", "T", "A", "a1")])

    def timing_out(pool, column, opts, cancel=None):
        yield Match("O", "T", "A", "h1")
        raise ColumnTimeout("timed out after 9 s")

    monkeypatch.setattr(search_core, "drill_down", timing_out)
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "x"
        await pilot.click("#search")
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.query_one("#found", DataTable).move_cursor(row=0)
        await pilot.click("#show-all")
        await app.workers.wait_for_complete()
        await pilot.pause()
        status = str(app.query_one("#status").render())
    assert "timed out" in status.lower() and "partial" in status.lower()


def test_progress_text_before_any_table_is_known():
    from scout.control import Progress
    from scout.tui import _progress_text

    assert "Looking for tables" in _progress_text(Progress(0, 0, 0.0, (), 0, 0, 0))


async def test_enter_in_the_term_box_runs_the_search_and_cancel_is_only_live_while_running(monkeypatch):
    from scout.search import Match
    from textual.widgets import Button

    fake_login(monkeypatch)
    calls = []
    locate_fake(monkeypatch, calls, hits=[Match("O", "T", "C", "Albert Talbert")])
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        assert app.query_one("#cancel", Button).disabled  # nothing to cancel yet
        assert "No search yet" in str(app.query_one("#empty").render())
        app.query_one("#terms").focus()
        app.query_one("#terms").value = "albert"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(calls) == 1 and calls[0]["opts"].terms == ("albert",)
        assert app.query_one("#cancel", Button).disabled  # finished again
        cell = app.query_one("#results", DataTable).get_row_at(0)[3]
        assert {span.start for span in cell.spans} == {0, 8}  # both Albert and Talbert's albert are marked


async def test_no_match_says_what_to_try_instead(monkeypatch):
    fake_login(monkeypatch)
    locate_fake(monkeypatch)
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "zzz"
        app.query_one("#whole_word").value = True
        await pilot.click("#search")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "turning off whole word" in str(app.query_one("#empty").render())


def test_highlight_pattern_follows_wildcards_and_escapes():
    from scout.search import SearchOptions
    from scout.tui import _highlight_pattern

    pat = _highlight_pattern(SearchOptions(terms=("al*t?", r"a\*"), schemas=()))
    assert pat.search("ALBERTS").group() == "ALBERTS"
    assert pat.search("a*b").group() == "a*" and not pat.search("ab")


# ---- snippets and the detail window

def _snippet_match(more=False):
    from scout.search import Match, Snippet

    return Match("O", "T", "C", "ab zq cd", "AAAR1", Snippet("ab ", "zq", " cd", True, True, more))


def test_detail_location_counts_crlf_as_one_line_break():
    from scout.search import Detail, Snippet
    from scout.tui import detail_text

    d = Detail(Snippet("l1\r\nl2 ", "zq", " end", False, False, False), 17)
    text, start, end = detail_text(d)
    assert text == "l1\nl2 zq end" and start == (1, 3) and end == (1, 5)


def test_detail_text_marks_cut_sides():
    from scout.search import Detail, Snippet
    from scout.tui import detail_text

    text, start, end = detail_text(Detail(Snippet("ab ", "zq", " cd", True, True, False), 999))
    assert text == "…ab zq cd…" and start == (0, 4) and end == (0, 6)


def test_detail_note_only_when_cut():
    from scout.search import Detail, Snippet
    from scout.tui import detail_note

    assert detail_note(Detail(Snippet("a", "b", "c", False, False, False), 3)) == ""
    assert detail_note(Detail(Snippet("a", "b", "c", True, False, False), 96_828_460)) == \
        "showing 3 of 96,828,460 characters"


async def _search_with(monkeypatch, app, pilot, matches):
    from scout import search as search_core

    monkeypatch.setattr(search_core, "search",
                        lambda cfg, opts, progress=None, on_skip=None, pool=None, **kw: iter(matches))
    await login(app, pilot)
    app.query_one("#terms").value = "zq"
    await pilot.click("#search")
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_results_cell_shows_snippet_with_badge(monkeypatch):
    from scout.tui import HIT_STYLE

    fake_login(monkeypatch)
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await _search_with(monkeypatch, app, pilot, [_snippet_match(more=True)])
        cell = app.query_one("#results", DataTable).get_row_at(0)[3]
        assert cell.plain == "…ab zq cd… +"
        start = cell.plain.index("zq")
        assert any(s.start == start and s.end == start + 2 and str(s.style) == HIT_STYLE for s in cell.spans)


async def test_search_asks_for_snippets(monkeypatch):
    from scout import search as search_core
    from scout.search import SNIPPET_CONTEXT

    fake_login(monkeypatch)
    seen = []

    def fake(cfg, opts, progress=None, on_skip=None, pool=None, **kw):
        seen.append(opts)
        return iter(())

    monkeypatch.setattr(search_core, "search", fake)
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        app.query_one("#terms").value = "zq"
        await pilot.click("#search")
        await app.workers.wait_for_complete()
    assert seen[0].snippet_context == SNIPPET_CONTEXT


def _fake_detail(monkeypatch, result):
    import contextlib

    from scout import search as search_core

    calls = []
    monkeypatch.setattr(db, "acquire", lambda pool: contextlib.nullcontext("conn"))

    def fake(conn, match, opts, **kw):
        calls.append((conn, match, opts))
        return result

    monkeypatch.setattr(search_core, "fetch_detail", fake)
    return calls


async def test_enter_on_a_hit_opens_the_detail_window(monkeypatch):
    from textual.widgets import TextArea

    from scout.search import Detail, Snippet
    from scout.tui import DetailScreen

    fake_login(monkeypatch)
    calls = _fake_detail(monkeypatch, Detail(Snippet("line one\r\nab ", "zq", " cd", True, False, False), 500))
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await _search_with(monkeypatch, app, pilot, [_snippet_match()])
        app.query_one("#results", DataTable).focus()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        ta = app.screen.query_one("#detail-text", TextArea)
        assert ta.read_only and ta.selected_text == "zq"
        assert "showing" in str(app.screen.query_one("#detail-note").render())
        assert calls[0][0] == "conn" and calls[0][1].rowid == "AAAR1"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DetailScreen)


async def test_detail_for_vanished_row_says_so(monkeypatch):
    from scout.tui import DetailScreen

    fake_login(monkeypatch)
    _fake_detail(monkeypatch, None)
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await _search_with(monkeypatch, app, pilot, [_snippet_match()])
        app.query_one("#results", DataTable).focus()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        assert "no longer matches" in str(app.screen.query_one("#detail-note").render())
