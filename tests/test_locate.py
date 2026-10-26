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


# ---- drill-down: every hit for one chosen column

@pytest.fixture
def pool(reader_config):
    from scout import db

    p = db.create_pool(reader_config, 2)
    yield p
    p.close(force=True)


def test_drill_down_returns_every_hit_even_when_opts_has_a_limit(reader_config, reader_connection, pool):
    from scout.search import drill_down, resolve_column

    opts = SearchOptions(terms=("report",), schemas=ex.SCHEMAS, limit_per_column=1)
    col = resolve_column(reader_connection, "SCOUT_HR.EMPLOYEES.NOTES", opts)
    hits = list(drill_down(pool, col, opts))
    complete, _ = run(reader_config, "report")
    assert {m.key for m in hits} == {m.key for m in complete if (m.owner, m.table, m.column) == col_key(col)}
    assert len(hits) >= 3 and all(m.rowid for m in hits)


def test_resolve_column_handles_dots_spaces_and_case(reader_connection):
    from scout.search import resolve_column

    opts = SearchOptions(terms=("x",))
    odd = resolve_column(reader_connection, "SCOUT_ODD.MixedCase Table.Weird Col", opts)
    assert col_key(odd) == ex.WEIRD
    lower = resolve_column(reader_connection, "scout_hr.employees.notes", opts)
    assert col_key(lower) == ex.EMP_NOTES


@pytest.mark.parametrize("ref", [
    "SCOUT_HR.EMPLOYEES.NOPE", "SCOUT_HR.NOPE.NOTES", "NOPE.EMPLOYEES.NOTES",
    "SCOUT_HR.EMPLOYEES.SALARY",  # a NUMBER column is never searchable
    "EMPLOYEES.NOTES", "SCOUT_HR", "", "..",
])
def test_resolve_unknown_column_returns_none(reader_connection, ref):
    from scout.search import resolve_column

    assert resolve_column(reader_connection, ref, SearchOptions(terms=("x",))) is None


# ---- pruning: columns too short for the term are skipped; nothing with a hit may be lost

BRUTE_TERMS = [
    "a", "at", "zebra", "quick", "report", "albert", "colo?r", "colo*r", "a*", "qu*k", "fox", "the",
    "lazy dog", "quick brown fox", "Ana Silva", "user_name", "100%", "it's a 'quoted' value",
    "the quick brown fox jumps over the lazy dog", "Said 100% done; check user_name field",
    "reportedly late on deliverables", "Prefers remote work; the report is due Friday",
    "lateneedle", "zxqneedle", "Zoë Müller",
]


def brute_force_columns(conn, opts):
    """Columns that really contain a hit, found with no pruning at all (direct SQL per column)."""
    from scout.pattern import build_regex
    from scout.search import _quote, discover_columns

    pattern, flags = build_regex(opts)
    found = set()
    for c in discover_columns(conn, opts):
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM (SELECT 1 FROM {_quote(c.owner)}.{_quote(c.table)} "
                f"WHERE REGEXP_LIKE({_quote(c.column)}, :p, :f) FETCH FIRST 1 ROWS ONLY)",
                p=pattern, f=flags,
            )
            if cur.fetchone()[0]:
                found.add(col_key(c))
    return found


@pytest.mark.parametrize("term", BRUTE_TERMS)
def test_pruning_never_removes_a_column_with_a_hit(reader_config, reader_connection, term):
    opts = SearchOptions(terms=(term,), schemas=ex.SCHEMAS, threads=4)
    matches, outcomes = run(reader_config, term)
    assert {(m.owner, m.table, m.column) for m in matches} == brute_force_columns(reader_connection, opts)


def test_near_search_is_not_pruned_wrongly(reader_config, reader_connection):
    opts = SearchOptions(terms=("quick", "lazy"), near=6, schemas=ex.SCHEMAS, threads=4)
    matches = list(search(reader_config, opts))
    assert {(m.owner, m.table, m.column) for m in matches} == brute_force_columns(reader_connection, opts)
    assert matches


def test_columns_shorter_than_the_term_are_not_searched(reader_config):
    term = "x" * 60  # longer than FIRST_NAME VARCHAR2(50), shorter than NOTES VARCHAR2(400)
    _, outcomes = run(reader_config, term)
    searched = {col_key(o.column) for o in outcomes}
    assert ("SCOUT_HR", "EMPLOYEES", "FIRST_NAME") not in searched
    assert ex.EMP_NOTES in searched


def test_clob_columns_are_never_pruned(reader_config):
    term = "quick brown fox jumps over the lazy dog"  # 39 characters
    matches, outcomes = run(reader_config, term)
    assert ex.DOC_BODY in {col_key(o.column) for o in outcomes}
    assert any((m.owner, m.table, m.column) == ex.DOC_BODY for m in matches)
