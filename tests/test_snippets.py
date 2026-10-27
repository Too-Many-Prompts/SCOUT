"""Snippets: Oracle cuts the first match with SNIPPET_CONTEXT characters each side, says whether the
value has more matches, and returns the pieces as strings (CLOB positions count UTF-16 units)."""
import time
from dataclasses import replace

import oracledb
import pytest

from scout import db
from scout.search import DETAIL_CONTEXT, SNIPPET_CONTEXT, SearchOptions, Snippet, fetch_detail, search
from tests.db.expected import PHRASE_FOX_CI, REPORT_NOTES_CI, SCHEMAS

SCHEMA = "SCOUT_SNIP_TINY"

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("scan_mode")]

ROWS = [
    (1, "x albert albert y", None, None),
    (2, None, "a" * 50 + " zqsnip " + "b" * 50, None),
    (3, None, "zqstart " + "c" * 100, "か" * 100 + "zqend"),
    (4, None, "😀" * 30 + "zqemoji" + "😀" * 30, None),
    (5, None, "short zqtiny", None),
    (6, None, "x" * 5_000_000 + " zqhuge", None),
    (7, None, "zqtwo " + "y" * 100 + " zqtwo end", None),
    # the window's left edge (hit start - 20 UTF-16 units) lands inside the emoji: ORA-22831 unless handled
    (8, None, "😀" + "a" * 19 + "zqodd", "😀" + "a" * 19 + "zqodd"),
    # the same at the detail window's edge (hit start - 100,000 units)
    (9, None, "😀" + "b" * 99_999 + "zqdet", None),
    (10, "say albert einstein albert einstein ok", None, None),
]


@pytest.fixture(scope="module")
def snip_tiny(admin_writer_connection):
    cur = admin_writer_connection.cursor()
    try:
        cur.execute(f"DROP USER {SCHEMA} CASCADE")
    except oracledb.DatabaseError as exc:
        if "ORA-01918" not in str(exc):
            raise
    cur.execute(f"CREATE USER {SCHEMA} NO AUTHENTICATION QUOTA UNLIMITED ON users")
    cur.execute(f"CREATE TABLE {SCHEMA}.T (id NUMBER, v VARCHAR2(4000 CHAR), c CLOB, n NCLOB)")
    cur.setinputsizes(None, None, oracledb.DB_TYPE_CLOB, oracledb.DB_TYPE_NCLOB)
    cur.executemany(f"INSERT INTO {SCHEMA}.T VALUES (:1, :2, :3, :4)", ROWS)
    admin_writer_connection.commit()
    cur.execute(f"GRANT SELECT ON {SCHEMA}.T TO scout_reader")
    time.sleep(2)  # ORA-01466: let the DDL second pass before READ ONLY sessions read the table


@pytest.fixture(scope="module")
def fresh_reader(snip_tiny, reader_config):
    """Opened after the load: the shared reader session's snapshot predates the table."""
    conn = db.connect(reader_config)
    yield conn
    conn.close()


def opts_for(term, **kw):
    return SearchOptions(terms=(term,), schemas=(SCHEMA,), threads=2, snippet_context=SNIPPET_CONTEXT, **kw)


@pytest.fixture
def run(snip_tiny, reader_config):
    def go(term, **kw):
        return list(search(reader_config, opts_for(term, **kw)))
    return go


def test_snippet_is_twenty_characters_each_side(run):
    (m,) = run("zqsnip")
    assert m.snippet == Snippet("a" * 19 + " ", "zqsnip", " " + "b" * 19, True, True, False)
    assert m.value == "a" * 19 + " zqsnip " + "b" * 19


def test_hit_at_start_and_end_have_no_marker_on_that_side(run):
    (s,) = run("zqstart")
    (e,) = run("zqend")
    assert s.snippet.before == "" and not s.snippet.cut_before and s.snippet.cut_after
    assert e.snippet.before == "か" * 20 and e.snippet.after == "" and not e.snippet.cut_after


def test_short_value_comes_back_whole(run):
    (m,) = run("zqtiny")
    assert m.value == "short zqtiny" and not (m.snippet.cut_before or m.snippet.cut_after)


def test_whole_word_hit_excludes_boundaries_and_sees_adjacent_second_hit(run):
    (m,) = [m for m in run("albert", whole_word=True) if m.value.startswith("x albert")]
    assert m.snippet.hit == "albert" and m.snippet.more_in_value and m.value == "x albert albert y"


def test_single_hit_has_no_more_in_value(run):
    (m,) = run("zqtiny")
    assert not m.snippet.more_in_value


def test_emoji_clob_snippet_is_clean(run):
    (m,) = run("zqemoji")
    assert m.snippet.hit == "zqemoji" and "�" not in m.value
    m.value.encode("utf-8")  # a lone surrogate would raise here


def test_hit_at_the_end_of_a_5_mb_value(run):
    (m,) = run("zqhuge")
    assert m.snippet == Snippet("x" * 19 + " ", "zqhuge", "", True, False, False)


def test_without_snippet_context_the_full_value_comes_back(snip_tiny, reader_config):
    (m,) = search(reader_config, SearchOptions(terms=("zqsnip",), schemas=(SCHEMA,)))
    assert m.snippet is None and m.value == ROWS[1][2]


@pytest.mark.parametrize("terms, kw, expected", [
    (("report",), {"whole_word": True}, REPORT_NOTES_CI),
    (("quick", "brown", "fox"), {}, PHRASE_FOX_CI),
])
def test_snippets_of_the_seed_contract_are_windows_of_its_values(reader_config, terms, kw, expected):
    opts = SearchOptions(terms=terms, schemas=SCHEMAS, snippet_context=SNIPPET_CONTEXT, **kw)
    columns = {(o, t, c) for o, t, c, _ in expected}
    got = [m for m in search(reader_config, opts) if m.address in columns]
    assert len(got) == len(expected)
    for m in got:
        assert any(m.address == (o, t, c) and m.value in v for o, t, c, v in expected)


def test_detail_of_huge_value_is_a_large_window(run, fresh_reader):
    (m,) = run("zqhuge")
    d = fetch_detail(fresh_reader, m, opts_for("zqhuge"))
    assert d.snippet.cut_before and not d.snippet.cut_after
    assert len(d.snippet.before) == DETAIL_CONTEXT and d.snippet.hit == "zqhuge"
    assert d.total_length == 5_000_007


def test_detail_of_short_value_is_whole(run, fresh_reader):
    (m,) = run("zqtiny")
    d = fetch_detail(fresh_reader, m, opts_for("zqtiny"))
    assert d.snippet.before + d.snippet.hit + d.snippet.after == "short zqtiny" and d.total_length == 12


def test_snippet_carries_oracles_position_of_the_hit(run):
    (m,) = run("zqsnip")
    assert m.snippet.position == 52  # 50 a's and a space before it, counted from 1


def test_detail_starts_at_the_hit_position_not_the_value_start(run, fresh_reader):
    # the detail must not rescan a huge value from its start: it searches from the hit's position,
    # so a position past the first zqtwo finds the second one
    (m,) = run("zqtwo")
    assert m.snippet.position == 1 and m.snippet.more_in_value
    later = replace(m, snippet=replace(m.snippet, position=50))
    d = fetch_detail(fresh_reader, later, opts_for("zqtwo"))
    assert d.snippet.after == " end" and d.snippet.before.endswith("y" * 10 + " ")


def test_detail_for_vanished_row_says_so(run, fresh_reader):
    (m,) = run("zqtiny")
    (other,) = run("zqsnip")
    assert fetch_detail(fresh_reader, replace(m, rowid=other.rowid), opts_for("zqtiny")) is None


def test_cli_prints_the_snippet_line(snip_tiny, reader_config):
    from typer.testing import CliRunner

    from scout.cli import app

    result = CliRunner().invoke(
        app, ["search", "zqsnip", "--non-interactive", "--schema", SCHEMA],
        env={"SCOUT_TEST_DB_USER": reader_config.user, "SCOUT_TEST_DB_PASSWORD": reader_config.password},
    )
    assert result.exit_code == 0, result.output
    assert f"{SCHEMA}.T.C\t…{'a' * 19} zqsnip {'b' * 19}…\n" in result.stdout


@pytest.mark.parametrize("column", ["C", "N"])
def test_left_edge_inside_an_emoji_is_moved_to_the_next_character(run, column):
    hits = [m for m in run("zqodd") if m.column == column]
    (m,) = hits
    assert m.snippet.before == "a" * 19 and m.snippet.hit == "zqodd" and m.snippet.cut_before


def test_detail_left_edge_inside_an_emoji(run, fresh_reader):
    (m,) = run("zqdet")
    d = fetch_detail(fresh_reader, m, opts_for("zqdet"))
    assert d.snippet.before == "b" * 99_999 and d.snippet.cut_before


def test_whole_word_proximity_hit_excludes_boundaries_and_sees_the_second_pair(snip_tiny, reader_config):
    opts = SearchOptions(terms=("albert", "einstein"), near=1, whole_word=True, schemas=(SCHEMA,), threads=2,
                         snippet_context=SNIPPET_CONTEXT)
    (m,) = list(search(reader_config, opts))
    assert m.snippet.hit == "albert einstein" and m.snippet.more_in_value


def test_every_occurrence_lists_each_hit_in_a_value(run):
    got = [m for m in run("albert", every_occurrence=True) if m.value.startswith("x albert")]
    assert [m.snippet.hit for m in got] == ["albert", "albert"]
    assert len({m.snippet.position for m in got}) == 2 and not any(m.snippet.more_in_value for m in got)


def test_every_occurrence_whole_word_sees_adjacent_hits(run):
    got = [m for m in run("albert", whole_word=True, every_occurrence=True) if m.value.startswith("x albert")]
    assert len(got) == 2 and len({m.snippet.position for m in got}) == 2


def test_every_occurrence_finds_a_far_second_hit(run):
    got = run("zqtwo", every_occurrence=True)
    assert len(got) == 2 and got[0].rowid == got[1].rowid


def test_every_occurrence_off_keeps_one_hit_per_value(run):
    assert len(run("zqtwo")) == 1


def test_every_occurrence_proximity_lists_each_pair(snip_tiny, reader_config):
    opts = SearchOptions(terms=("albert", "einstein"), near=1, whole_word=True, schemas=(SCHEMA,), threads=2,
                         snippet_context=SNIPPET_CONTEXT, every_occurrence=True)
    got = list(search(reader_config, opts))
    assert len(got) == 2 and all(m.snippet.hit == "albert einstein" for m in got)
    assert len({m.snippet.position for m in got}) == 2
