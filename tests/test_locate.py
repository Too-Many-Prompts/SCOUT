"""Locate mode: a few example hits per column, with an honest 'more available' flag."""
import pytest

from scout.config import ConnectionConfig
from scout.search import SearchOptions, search
from tests.db import expected as ex

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("scan_mode")]

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
    from scout.search import discover_columns, prune_columns

    opts = SearchOptions(terms=("report",), schemas=ex.SCHEMAS)
    discovered = sorted(col_key(c) for c in prune_columns(discover_columns(reader_connection, opts), opts))
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


# ---- characters that fold to several letters must neither be pruned nor pre-checked away

EDGE = ("SCOUT_ODD", "Unicode Edge")


@pytest.mark.parametrize("term,narrow_char", [("fi", "ﬁ"), ("ffi", "ﬃ"), ("ss", "ß")])
def test_pruning_keeps_a_one_character_column_holding_a_ligature(reader_config, term, narrow_char):
    matches, _ = run(reader_config, term, schemas=("SCOUT_ODD",))
    assert (*EDGE, "NARROW", narrow_char) in {m.key for m in matches}


EDGE_TERMS = [
    "kelvin", "sign", "safe", "strasse", "stra", "fine", "office", "ffi", "fi", "istanbul", "ss", "st",
    "ice", "Straße", "SAFE", "k", "KELVIN", "harbour", "print", "supplies", "plain", "ascii", "lib",
]


def search_column_matches(conn, col, opts, with_precheck):
    import threading

    from scout.pattern import build_regex, required_literal
    from scout.search import _search_column

    pattern, flags = build_regex(opts)
    literal = required_literal(opts) if with_precheck else None
    gen = _search_column(conn, col, pattern, flags, threading.Event(), None, literal, not opts.case_sensitive)
    return {m.key for m in gen}


@pytest.mark.parametrize("case_sensitive", [False, True])
def test_precheck_never_changes_the_result(reader_connection, case_sensitive):
    from scout.search import discover_columns

    cols = discover_columns(
        reader_connection, SearchOptions(terms=("x",), schemas=("SCOUT_ODD", "SCOUT_HR", "SCOUT_DOCS")))
    for term in EDGE_TERMS + BRUTE_TERMS:
        opts = SearchOptions(terms=(term,), case_sensitive=case_sensitive)
        for col in cols:
            assert search_column_matches(reader_connection, col, opts, True) == \
                search_column_matches(reader_connection, col, opts, False), (term, col)


def test_nls_lower_never_misses_a_regex_match_for_the_safe_characters(reader_connection):
    """Sweep the whole BMP: wherever REGEXP_LIKE(..., 'i') matches a safe literal character,
    INSTR(NLS_LOWER(text)) must find it too. This is the proof behind _SAFE_FOLD."""
    from scout.pattern import _SAFE_FOLD

    sql = """
        SELECT cp FROM (SELECT LEVEL + 31 AS cp FROM dual CONNECT BY LEVEL <= 65504)
         WHERE cp NOT BETWEEN 55296 AND 57343
           AND REGEXP_LIKE(UNISTR('\\' || LPAD(TRIM(TO_CHAR(cp, 'XXXX')), 4, '0')), :c, 'i')
           AND INSTR(NLS_LOWER(UNISTR('\\' || LPAD(TRIM(TO_CHAR(cp, 'XXXX')), 4, '0')), 'NLS_SORT=BINARY'), :c) = 0"""
    missed = {}
    with reader_connection.cursor() as cur:
        for ch in sorted(_SAFE_FOLD):
            cur.execute(sql, c=ch)
            rows = cur.fetchall()
            if rows:
                missed[ch] = [hex(r[0]) for r in rows]
    assert missed == {}


# ---- single scan per table: same answers as column-by-column, reading each table once

@pytest.fixture
def single_scan(monkeypatch):
    from scout import search as search_mod

    monkeypatch.setattr(search_mod, "SINGLE_SCAN_MIN_BLOCKS", 0)


def outcome_summary(outcomes):
    return {col_key(o.column): (o.examples, o.more_available, o.timed_out) for o in outcomes}


VARIANTS = [
    {}, {"whole_word": True}, {"case_sensitive": True}, {"whole_word": True, "case_sensitive": True},
]


@pytest.mark.parametrize("variant", VARIANTS, ids=lambda v: "+".join(v) or "default")
@pytest.mark.parametrize("term", TERMS + ["the", "fi"])
@pytest.mark.parametrize("limit", [None, 1, 2])
def test_single_scan_gives_the_same_columns_matches_and_outcomes(reader_config, monkeypatch, term, limit, variant):
    from scout import search as search_mod

    monkeypatch.setattr(search_mod, "SINGLE_SCAN_MIN_BLOCKS", None)
    per_col, out_a = run(reader_config, term, limit_per_column=limit, **variant)
    monkeypatch.setattr(search_mod, "SINGLE_SCAN_MIN_BLOCKS", 0)
    per_tab, out_b = run(reader_config, term, limit_per_column=limit, **variant)
    assert outcome_summary(out_a) == outcome_summary(out_b)
    if limit is None:
        assert {m.key for m in per_col} == {m.key for m in per_tab}
    else:  # which examples come back may differ; they must still be real hits
        complete, _ = run(reader_config, term, **variant)
        assert {m.key for m in per_tab} <= {m.key for m in complete}


@pytest.mark.parametrize("terms", [("quick", "brown"), ("brown", "fox", "jumps"), ("the", "lazy")])
def test_a_phrase_gives_the_same_matches_either_way(reader_config, monkeypatch, terms):
    from scout import search as search_mod

    results = []
    for blocks in (None, 0):
        monkeypatch.setattr(search_mod, "SINGLE_SCAN_MIN_BLOCKS", blocks)
        matches = list(search(reader_config, SearchOptions(terms=terms, schemas=ex.SCHEMAS, threads=4)))
        results.append({m.key for m in matches})
    assert results[0] == results[1] and results[0]


def test_single_scan_reports_a_broken_table_once_per_column(reader_config, single_scan):
    skipped = []
    opts = SearchOptions(terms=("x",), schemas=("SCOUT_BROKEN",), threads=2, include_external=True)
    list(search(reader_config, opts, on_skip=lambda c, e: skipped.append(col_key(c))))
    assert skipped == [ex.BROKEN]


def test_single_scan_stops_early_once_every_column_has_enough_hits():
    import threading
    from unittest.mock import MagicMock

    from scout.search import Column, _search_table

    fetched = []

    class Cur:
        arraysize = 1
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, **binds): self.sql = sql
        def fetchmany(self):
            fetched.append(1)
            return [("R", "hit a", "hit b")]

    conn = MagicMock()
    conn.cursor.return_value = Cur()
    cols = [Column("O", "T", "A", "VARCHAR2"), Column("O", "T", "B", "VARCHAR2")]
    gen = _search_table(conn, cols, "pat", "i", threading.Event(), 1, None, True)
    got = []
    try:
        while True:
            got.append(next(gen))
    except StopIteration as done:
        outcomes = done.value
    assert len(fetched) == 2  # row 1 fills each column, row 2 proves 'more'; the rest is never read
    assert [(o.examples, o.more_available) for o in outcomes] == [(1, True), (1, True)]
    assert len(got) == 2


@pytest.mark.parametrize("nls", [
    "NLS_SORT=TURKISH NLS_COMP=LINGUISTIC",
    "NLS_LANGUAGE=TURKISH NLS_TERRITORY=TURKEY",
    "NLS_SORT=GERMAN",
])
def test_case_folding_proof_holds_under_other_nls_settings(reader_config, nls):
    """The pre-check and pruning rely on how REGEXP_LIKE(..., 'i') folds case. A user's session
    may use other NLS settings (client NLS_LANG); re-run the sweep and the ligature pairs under them."""
    from scout import db
    from scout.pattern import _SAFE_FOLD

    conn = db.connect(reader_config, read_only=False)  # only to ALTER SESSION; nothing is written
    try:
        with conn.cursor() as cur:
            cur.execute(f"ALTER SESSION SET {nls}")
            cur.execute("SELECT value FROM nls_session_parameters WHERE parameter IN ('NLS_SORT', 'NLS_LANGUAGE')")
            sql = """
                SELECT cp FROM (SELECT LEVEL + 31 AS cp FROM dual CONNECT BY LEVEL <= 65504)
                 WHERE cp NOT BETWEEN 55296 AND 57343
                   AND REGEXP_LIKE(UNISTR('\\' || LPAD(TRIM(TO_CHAR(cp, 'XXXX')), 4, '0')), :c, 'i')
                   AND INSTR(NLS_LOWER(UNISTR('\\' || LPAD(TRIM(TO_CHAR(cp, 'XXXX')), 4, '0')), 'NLS_SORT=BINARY'), :c) = 0"""
            missed = {}
            for ch in sorted(_SAFE_FOLD):
                cur.execute(sql, c=ch)
                rows = cur.fetchall()
                if rows:
                    missed[ch] = [hex(r[0]) for r in rows]
            assert missed == {}, f"pre-check can miss hits under {nls}"
            # the multi-character folds that pruning allows for must still hold
            for text, pat in [("ß", "ss"), ("ﬁ", "fi"), ("ﬃ", "ffi"), ("K", "k"), ("ſ", "s")]:
                cur.execute("SELECT CASE WHEN REGEXP_LIKE(:t, :p, 'i') THEN 1 ELSE 0 END FROM dual", t=text, p=pat)
                assert cur.fetchone()[0] == 1, (text, pat, nls)
    finally:
        conn.close()


# ---- wide tables (searched in groups of 20 columns) and a column that cannot be read

def test_every_column_of_a_wide_table_is_searched_whichever_group_it_falls_in(reader_config):
    matches, outcomes = run(reader_config, "widehit", schemas=("SCOUT_ODD",))
    per_column = {}
    for m in matches:
        if (m.owner, m.table) == ex.WIDE_TABLE:
            per_column[m.column] = per_column.get(m.column, 0) + 1
    assert per_column == ex.WIDE_HIT_COLUMNS  # C20|C21 and C40|C41 sit on group boundaries
    wide = [o for o in outcomes if (o.column.owner, o.column.table) == ex.WIDE_TABLE]
    assert len(wide) == 60  # one outcome per column, hits or not


def test_locate_mode_on_a_wide_table_counts_each_column_on_its_own(reader_config):
    _, outcomes = run(reader_config, "widehit", schemas=("SCOUT_ODD",), limit_per_column=1)
    got = {o.column.column: (o.examples, o.more_available) for o in outcomes if (o.column.owner, o.column.table) == ex.WIDE_TABLE}
    for column, total in ex.WIDE_HIT_COLUMNS.items():
        assert got[column] == (1, total > 1), column
    assert got["C02"] == (0, False)


def test_an_unreadable_column_is_skipped_alone(reader_config):
    skipped, outcomes = [], []
    opts = SearchOptions(terms=("virtneedle",), schemas=("SCOUT_VIRT",), threads=2)
    matches = list(search(reader_config, opts, on_skip=lambda c, e: skipped.append(col_key(c)), on_column=outcomes.append))
    assert {m.value for m in matches if (m.owner, m.table, m.column) == ex.VIRT_NOTE} == ex.VIRT_NOTE_HITS
    assert skipped == [ex.VIRT_BAD]  # NOTE is not among them
    assert ex.VIRT_NOTE in {col_key(o.column) for o in outcomes}
