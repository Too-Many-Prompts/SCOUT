"""The benchmark loader plants exactly the needle it promises (tiny size; fast)."""
import pytest

from scout.search import SearchOptions, search
from tests.bench.load import ABSENT, NEEDLE, SCHEMA, build

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def tiny_bench(admin_writer_connection):
    build(admin_writer_connection, tables=2, rows=50)


def run(cfg, term):
    return list(search(cfg, SearchOptions(terms=(term,), schemas=(SCHEMA,), threads=2)))


def test_loader_plants_exactly_the_needle(tiny_bench, reader_config):
    hits = run(reader_config, NEEDLE)
    assert len(hits) == 5  # rows 10, 20, 30, 40, 50
    assert {(m.owner, m.table, m.column) for m in hits} == {(SCHEMA, "T01", "C1")}


def test_absent_word_matches_nothing(tiny_bench, reader_config):
    assert run(reader_config, ABSENT) == []
