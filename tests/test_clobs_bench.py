"""The downloaded large-CLOB dataset (python -m tests.bench.clobs): each planted word is found in
exactly one row of its table, and a locate-mode search over all of it finishes in time."""
import pytest

from scout.search import SNIPPET_CONTEXT, SearchOptions, search
from tests.bench.clobs import ABSENT, FOURK, KANA, SCHEMA, TAIL, THIRTYTWOK

pytestmark = [pytest.mark.integration, pytest.mark.bench]

TABLE_OF = {FOURK: "WIKI_SIMPLE", THIRTYTWOK: "WIKI_SIMPLE", KANA: "WIKI_JA", TAIL: "BOOKS"}


@pytest.fixture(scope="module")
def tables(reader_connection):
    cur = reader_connection.cursor()
    cur.execute("SELECT table_name FROM all_tables WHERE owner = :o", o=SCHEMA)
    found = {name for (name,) in cur}
    if not found:
        pytest.skip(f"{SCHEMA} is not loaded (python -m tests.bench.clobs)")
    return found


def run(cfg, term, **kw):
    return list(search(cfg, SearchOptions(terms=(term,), schemas=(SCHEMA,), threads=8, **kw)))


@pytest.mark.parametrize("word", [FOURK, THIRTYTWOK, KANA, TAIL])
def test_planted_word_is_found_once(tables, reader_config, word):
    if TABLE_OF[word] not in tables:
        pytest.skip(f"{SCHEMA}.{TABLE_OF[word]} was not loaded")
    hits = run(reader_config, word)
    assert [(m.table, m.column) for m in hits] == [(TABLE_OF[word], "BODY")]
    assert word in hits[0].value


def test_absent_word_matches_nothing(tables, reader_config):
    assert run(reader_config, ABSENT) == []


def test_locate_mode_on_a_common_word(tables, reader_config):
    outcomes = []
    opts = SearchOptions(terms=("the",), schemas=(SCHEMA,), threads=8, limit_per_column=5, time_limit=300)
    assert list(search(reader_config, opts, on_column=outcomes.append))
    bodies = {o.column.table: o for o in outcomes if o.column.column == "BODY"}
    assert bodies and not any(o.timed_out for o in bodies.values())
    assert bodies["WIKI_SIMPLE"].examples == 5 and bodies["BOOKS"].examples == 5


def test_tail_snippet_on_the_huge_value(tables, reader_config):
    opts = SearchOptions(terms=(TAIL,), schemas=(SCHEMA,), tables=("BOOKS",), threads=8,
                         snippet_context=SNIPPET_CONTEXT)
    (m,) = list(search(reader_config, opts))
    assert m.snippet.hit == TAIL and m.snippet.after == "" and m.snippet.cut_before
    assert len(m.value) <= 2 * SNIPPET_CONTEXT + len(TAIL)
