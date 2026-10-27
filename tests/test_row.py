"""The whole-row feature: search.fetch_row (mocked cursor) and the TUI's row sheet."""
import contextlib
from unittest.mock import MagicMock

import pytest
from textual.widgets import DataTable

from scout import db
from scout import search as search_core
from scout.search import Match, RowField, fetch_row
from scout.tui import ScoutApp
from tests.test_tui import _fake_detail, _search_with, _snippet_match, fake_login, login


def _row_conn(columns, row):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.fetchall.return_value = columns
    cur.fetchone.return_value = row
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def _executed(conn):
    return conn.cursor.return_value.execute.call_args_list


# ---- fetch_row

def test_fetch_row_selects_by_rowid_and_shapes_every_column_type():
    columns = [("ID", "NUMBER"), ("NAME", "VARCHAR2"), ("BIO", "CLOB"), ("PIC", "BLOB"), ("XML", "XMLTYPE"),
               ("GONE", "VARCHAR2"), ("PIC2", "BLOB")]
    conn = _row_conn(columns, (1, 10, "Jon", "first 1000", 5_000, 2048, None, None))
    got = fetch_row(conn, Match("S", "T", "NAME", "Jon", "AAAR1"))
    assert got == [
        RowField("ID", "NUMBER", "10", 2),
        RowField("NAME", "VARCHAR2", "Jon", 3),
        RowField("BIO", "CLOB", "first 1000", 5_000, cut=True),
        RowField("PIC", "BLOB", None, 2048, shown=False),
        RowField("XML", "XMLTYPE", None, shown=False),
        RowField("GONE", "VARCHAR2", None),
        RowField("PIC2", "BLOB", None),
    ]
    select = _executed(conn)[1]
    sql = select.args[0]
    assert 'FROM "S"."T"' in sql and "ROWID = CHARTOROWID(:rid)" in sql
    assert 'DBMS_LOB.SUBSTR("BIO", :cell, 1)' in sql and '"XML"' not in sql
    assert select.kwargs == {"rid": "AAAR1", "cell": 1000}


def test_fetch_row_cuts_long_plain_strings_and_omits_the_unused_cell_bind():
    conn = _row_conn([("V", "VARCHAR2")], (1, "x" * 30))
    (f,) = fetch_row(conn, Match("S", "T", "V", "x", "AAAR1"), cell_chars=10)
    assert f.value == "x" * 10 and f.length == 30 and f.cut
    assert _executed(conn)[1].kwargs == {"rid": "AAAR1"}


def test_fetch_row_is_none_when_the_row_or_table_is_gone():
    assert fetch_row(_row_conn([], None), Match("S", "T", "V", "x", "AAAR1")) is None
    assert fetch_row(_row_conn([("V", "VARCHAR2")], None), Match("S", "T", "V", "x", "AAAR1")) is None


def test_fetch_row_needs_a_rowid():
    with pytest.raises(ValueError):
        fetch_row(_row_conn([], None), Match("S", "T", "V", "x"))


def test_fetch_row_refuses_an_unsafe_identifier():
    with pytest.raises(ValueError):
        fetch_row(_row_conn([('A"B', "VARCHAR2")], (1, "x")), Match("S", "T", "V", "x", "AAAR1"))


# ---- the row sheet

def _fields():
    return [RowField("ID", "NUMBER", "10", 2), RowField("C", "VARCHAR2", "ab zq cd", 8),
            RowField("BIO", "CLOB", "long text", 5000, cut=True), RowField("PIC", "BLOB", None, 2048, shown=False),
            RowField("EMPTY", "DATE", None)]


def _fake_row(monkeypatch, result):
    calls = []
    monkeypatch.setattr(db, "acquire", lambda pool: contextlib.nullcontext("conn"))

    def fake(conn, match, **kw):
        calls.append((conn, match))
        return result

    monkeypatch.setattr(search_core, "fetch_row", fake)
    return calls


def test_row_cell_marks_null_cut_binary_and_the_hit():
    from scout.tui import HIT_STYLE, format_size, row_cell

    assert row_cell(RowField("A", "DATE", None)).plain == "NULL"
    assert row_cell(RowField("A", "BLOB", None, 12_288, shown=False)).plain == "‹BLOB, 12 KB›"
    assert row_cell(RowField("A", "XMLTYPE", None, shown=False)).plain == "‹XMLTYPE›"
    assert row_cell(RowField("A", "CLOB", "abc", 5_000, cut=True)).plain == "abc… (3 of 5,000 characters)"
    hit = row_cell(RowField("A", "VARCHAR2", "ab zq\ncd", 8), "zq")
    assert hit.plain == "ab zq cd" and [(s.start, s.end, str(s.style)) for s in hit.spans] == [(3, 5, HIT_STYLE)]
    assert [format_size(n) for n in (812, 1536, 12_288, 3_600_000)] == ["812 bytes", "1.5 KB", "12 KB", "3.4 MB"]


async def test_r_opens_the_whole_row_with_the_hit_column_selected(monkeypatch):
    from scout.tui import RowScreen

    fake_login(monkeypatch)
    calls = _fake_row(monkeypatch, _fields())
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await _search_with(monkeypatch, app, pilot, [_snippet_match()])
        app.query_one("#results", DataTable).focus()
        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, RowScreen)
        table = app.screen.query_one("#row-fields", DataTable)
        assert table.row_count == 5 and table.cursor_row == 1 and calls[0][1].rowid == "AAAR1"
        assert "5 columns" in str(app.screen.query_one("#row-note").render())
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, RowScreen)


async def test_whole_row_button_without_a_hit_asks_to_select_one(monkeypatch):
    from scout.tui import RowScreen

    fake_login(monkeypatch)
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await login(app, pilot)
        await pilot.click("#whole-row")
        await pilot.pause()
        assert not isinstance(app.screen, RowScreen)
        assert "Select a hit" in str(app.query_one("#status").render())


async def test_whole_row_of_a_vanished_row_says_so(monkeypatch):
    from scout.tui import RowScreen

    fake_login(monkeypatch)
    _fake_row(monkeypatch, None)
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await _search_with(monkeypatch, app, pilot, [_snippet_match()])
        await pilot.click("#whole-row")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, RowScreen)
        assert "no longer exists" in str(app.screen.query_one("#row-note").render())


async def test_enter_on_the_hit_column_opens_context_and_r_there_returns_to_the_row(monkeypatch):
    from scout.search import Detail, Snippet
    from scout.tui import DetailScreen, RowScreen

    fake_login(monkeypatch)
    _fake_row(monkeypatch, _fields())
    _fake_detail(monkeypatch, Detail(Snippet("ab ", "zq", " cd", False, False, False), 8))
    app = ScoutApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await _search_with(monkeypatch, app, pilot, [_snippet_match()])
        app.query_one("#results", DataTable).focus()
        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, RowScreen)
