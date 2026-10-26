"""Locate mode: a few example hits per column, with an honest 'more available' flag."""
import pytest

from scout.config import ConnectionConfig
from scout.search import SearchOptions, search
from tests.db import expected as ex

pytestmark = pytest.mark.integration

TERMS = ["report", "quick", "albert", "colo*r", "zebra", "lateneedle"]


def run(cfg, term, **kw):
    """Returns (matches, outcomes) for a search over the seed schemas."""
    outcomes = []
    kw.setdefault("schemas", ex.SCHEMAS)
    kw.setdefault("threads", 4)
    matches = list(search(cfg, SearchOptions(terms=(term,), **kw), on_column=outcomes.append))
    return matches, outcomes


def col_key(c):
    return (c.owner, c.table, c.column)


@pytest.mark.parametrize("term", TERMS)
def test_locate_reports_exactly_the_columns_that_have_hits(reader_config, term):
    complete, _ = run(reader_config, term)
    _, outcomes = run(reader_config, term, limit_per_column=1)
    assert {col_key(o.column) for o in outcomes if o.examples} == {(m.owner, m.table, m.column) for m in complete}


def test_more_available_flag(reader_config):
    notes = ("SCOUT_HR", "EMPLOYEES", "NOTES")
    complete, _ = run(reader_config, "report")
    total = len([m for m in complete if (m.owner, m.table, m.column) == notes])
    assert total >= 3  # 'report' also matches inside 'Reportedly' by default
    for limit in (1, 2, total, total + 5):
        matches, outcomes = run(reader_config, "report", limit_per_column=limit)
        outcome = next(o for o in outcomes if col_key(o.column) == notes)
        want = min(limit, total)
        assert (outcome.examples, outcome.more_available, outcome.timed_out) == (want, limit < total, False)
        assert len([m for m in matches if (m.owner, m.table, m.column) == notes]) == want


def test_every_searched_column_gets_exactly_one_outcome(reader_config, reader_connection):
    from scout.search import discover_columns

    opts = SearchOptions(terms=("report",), schemas=ex.SCHEMAS)
    discovered = sorted(col_key(c) for c in discover_columns(reader_connection, opts))
    _, outcomes = run(reader_config, "report", limit_per_column=2)
    assert sorted(col_key(o.column) for o in outcomes) == discovered


def test_columns_without_hits_report_zero_examples(reader_config):
    _, outcomes = run(reader_config, "report", limit_per_column=2)
    quiet = [o for o in outcomes if col_key(o.column) == ("SCOUT_ODD", "NUMS", "D")]
    assert not quiet  # not a string column: never searched
    docs = next(o for o in outcomes if col_key(o.column) == ex.DOC_BODY)
    assert (docs.examples, docs.more_available) == (0, False)


def test_a_late_hit_column_is_still_reported(reader_config):
    matches, outcomes = run(reader_config, "lateneedle", limit_per_column=1)
    assert [m.key[:3] for m in matches] == [ex.LATE_HIT]
    outcome = next(o for o in outcomes if col_key(o.column) == ex.LATE_HIT)
    assert (outcome.examples, outcome.more_available) == (1, False)


@pytest.mark.parametrize("limit", [0, -1])
def test_invalid_limit_raises_before_any_database_work(limit):
    unreachable = ConnectionConfig(host="invalid.invalid", user="u", password="p")
    with pytest.raises(ValueError, match="limit"):
        list(search(unreachable, SearchOptions(terms=("x",), limit_per_column=limit)))
