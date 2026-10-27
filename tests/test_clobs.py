"""Large CLOBs and NCLOBs: words past 4,000 / 32,767 bytes, after multi-byte text, and at the end
of a multi-MB value are all found. Built from synthetic text (no download, a few seconds)."""
import random

import pytest

from scout import db
from scout.search import SearchOptions, search
from tests.bench.clobs import (
    ABSENT, FOURK, KANA, KANA_CHAR_OFFSET, TAIL, THIRTYTWOK, books_with_huge, build,
)

SCHEMA = "SCOUT_CLOB_TINY"  # never SCOUT_CLOB: that holds the downloaded dataset

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("scan_mode")]


def ascii_text(rng: random.Random, chars: int) -> str:
    """Lowercase words without z or q, so no planted word can occur by chance."""
    letters = "abcdefghijklmnoprstuvwxy"
    words: list[str] = []
    size = 0
    while size < chars:
        word = "".join(rng.choices(letters, k=rng.randint(2, 10)))
        words.append(word)
        size += len(word) + 1
    return " ".join(words)


def kana_text(rng: random.Random, chars: int) -> str:
    return "".join(rng.choices("あいうえおかきくけこさしすせそアイウエオ漢字。", k=chars))


@pytest.fixture(scope="module")
def tiny_clobs(admin_writer_connection):
    rng = random.Random(7)
    simple = [(i, f"article {i}", ascii_text(rng, n))
              for i, n in enumerate([200, 3_000, 9_000, 50_000, 120_000], start=1)]
    ja = [(1, "短い", kana_text(rng, 500)), (2, "長い", kana_text(rng, 3 * KANA_CHAR_OFFSET))]
    book = ascii_text(rng, 1_000_000)
    books = books_with_huge({"Book": book}, book, copies=5)  # HUGE is about 5 MB
    return build(admin_writer_connection, iter(simple), iter(ja), books, schema=SCHEMA)


def run(cfg, term, **kw):
    return list(search(cfg, SearchOptions(terms=(term,), schemas=(SCHEMA,), threads=4, **kw)))


@pytest.fixture(scope="module")
def fresh_reader(tiny_clobs, reader_config):
    """A reader session opened after the load: the shared one's READ ONLY snapshot predates the
    tables (ORA-01466)."""
    conn = db.connect(reader_config)
    yield conn
    conn.close()


def row_id(conn, match) -> int:
    cur = conn.cursor()
    cur.execute(f'SELECT id FROM {SCHEMA}."{match.table}" WHERE ROWID = CHARTOROWID(:r)', r=match.rowid)
    return cur.fetchone()[0]


@pytest.mark.parametrize("word", [FOURK, THIRTYTWOK, KANA, TAIL])
def test_planted_word_is_found_in_its_row_only(tiny_clobs, reader_config, fresh_reader, word):
    (where,) = [p for p in tiny_clobs if p.word == word]
    hits = run(reader_config, word)
    assert [(m.table, m.column) for m in hits] == [(where.table, "BODY")]
    assert row_id(fresh_reader, hits[0]) == where.id
    assert word in hits[0].value


def test_whole_huge_value_comes_back(tiny_clobs, reader_config):
    (hit,) = run(reader_config, TAIL)
    assert len(hit.value) > 5_000_000 and hit.value.endswith(TAIL)


def test_absent_word_matches_nothing(tiny_clobs, reader_config):
    assert run(reader_config, ABSENT) == []


def test_locate_mode_stops_at_the_example_limit(tiny_clobs, reader_config):
    hits = run(reader_config, "a", limit_per_column=2)
    bodies = [m for m in hits if m.column == "BODY" and m.table == "WIKI_SIMPLE"]
    assert len(bodies) == 2
