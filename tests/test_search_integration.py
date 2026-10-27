"""Behavior of search() against the seeded container (tests/db/seed.sql).

Default searches run as scout_reader through the ALL_* dictionary views.
"""
import pytest

from scout.search import DictionaryTier, SearchOptions, search
from tests.db import expected as ex

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("scan_mode")]


def run(cfg, *terms, **kw):
    kw.setdefault("schemas", ex.SCHEMAS)
    kw.setdefault("threads", 4)
    kw.setdefault("whole_word", True)  # most tests assert whole-word results; contains tests say whole_word=False
    return {m.key for m in search(cfg, SearchOptions(terms=terms, **kw))}


def test_word_ignores_case_and_whole_word_only(reader_config):
    # 'Reportedly' must not match; 'REPORT' must
    assert run(reader_config, "report") == ex.REPORT_NOTES_CI


def test_word_case_sensitive(reader_config):
    assert run(reader_config, "report", case_sensitive=True) == ex.REPORT_NOTES_CS


def test_phrase_case_insensitive(reader_config):
    assert run(reader_config, "quick brown fox") == ex.PHRASE_FOX_CI


def test_phrase_case_sensitive(reader_config):
    got = run(reader_config, "quick brown fox", case_sensitive=True)
    assert got == ex.PHRASE_FOX_CS


def test_proximity_within_distance(reader_config):
    got = run(reader_config, "quick", "lazy", near=5)
    assert got == {ex.QUICK_LAZY_2}


def test_proximity_larger_distance_adds_far_match(reader_config):
    got = run(reader_config, "quick", "lazy", near=6)
    assert got == {ex.QUICK_LAZY_2, ex.QUICK_LAZY_6}


def test_proximity_too_tight_finds_nothing(reader_config):
    got = run(reader_config, "quick", "lazy", near=1)
    assert got == set()


def test_wildcard_single_char(reader_config):
    assert run(reader_config, "colo?r", whole_word=False) == {ex.COLOUR}
    assert run(reader_config, "colo?r") == {ex.COLOUR}  # whole word too


def test_wildcard_star(reader_config):
    # contains (default): the term may run into longer words
    assert run(reader_config, "colo*r", whole_word=False) == {
        ex.COLOUR, ex.COLOR, ex.COLORADO, ex.COLORS_SENTENCE}
    assert run(reader_config, "colou*r", whole_word=False) == {ex.COLOUR}
    assert run(reader_config, "col*", whole_word=False) == {
        ex.COLOUR, ex.COLOR, ex.COLORADO, ex.COLORS_SENTENCE}


def test_wildcards_work_with_whole_word(reader_config):
    assert run(reader_config, "colo*r") == {ex.COLOUR, ex.COLOR}  # Colorado/colors are other words


def test_escaped_wildcards_are_literal(reader_config):
    assert run(reader_config, r"colo\?r", whole_word=False) == set()  # no literal '?' in the data
    assert run(reader_config, r"colo\*r", whole_word=False) == set()


def test_other_metachars_are_always_literal(reader_config):
    assert run(reader_config, "100%") == {ex.PERCENT_ROW}
    assert run(reader_config, "user_name") == {ex.PERCENT_ROW}  # not 'username'


def test_quotes_in_term_and_data(reader_config):
    assert run(reader_config, "it's a 'quoted' value") == {ex.QUOTED_ROW}


def test_sql_injection_attempt_is_inert(reader_config, reader_connection):
    assert run(reader_config, "'; DROP TABLE scout_hr.employees; --") == set()
    cur = reader_connection.cursor()
    cur.execute("SELECT COUNT(*) FROM scout_hr.employees")
    assert cur.fetchone() == (ex.EMPLOYEE_ROWS,)


def test_nvarchar_unicode(reader_config):
    assert run(reader_config, "MÜLLER") == {ex.AUTHOR_MULLER}


def test_char_column_is_searched(reader_config):
    hits = [m for m in search(reader_config, SearchOptions(terms=("active",), whole_word=True, schemas=ex.SCHEMAS))]
    assert {(m.owner, m.table, m.column) for m in hits} == {("SCOUT_HR", "EMPLOYEES", "STATUS")}
    assert len(hits) == ex.ACTIVE_STATUS_ROWS


def test_non_string_columns_and_views_are_skipped(reader_config):
    assert run(reader_config, "42") == set()
    assert not any(k[1] == "DOC_TITLES" for k in run(reader_config, "Fox"))


def test_all_tier_cannot_see_unprivileged_schema(reader_config):
    assert run(reader_config, "confidential") == set()


def test_dba_tier_sees_everything(admin_config):  # the only search test needing elevated access
    got = run(admin_config, "report", tier=DictionaryTier.DBA)
    assert got == ex.REPORT_NOTES_CI | ex.REPORT_SECRET


def test_schema_and_table_filters(reader_config):
    got = run(reader_config, "report", schemas=("SCOUT_DOCS",))
    assert got == set()
    got = run(reader_config, "report", tables=("EMPLOYEES",))
    assert got == ex.REPORT_NOTES_CI


def test_matches_carry_rowid_for_future_row_fetch(reader_config):
    ms = list(search(reader_config, SearchOptions(terms=("report",), schemas=ex.SCHEMAS)))
    assert ms and all(m.rowid for m in ms)


def test_whole_word_does_not_match_partial(reader_config):
    assert run(reader_config, "albert") == ex.ALBERT_WORD_ONLY


def test_default_matches_inside_longer_words_without_wildcards(reader_config):
    assert run(reader_config, "albert", whole_word=False) == ex.ALBERT_SUBSTRING  # includes Talbert


def test_default_report_search_includes_longer_words(reader_config):
    assert run(reader_config, "report", whole_word=False) == ex.REPORT_NOTES_CONTAINS_CI
    assert run(reader_config, "report") == ex.REPORT_NOTES_CI  # --whole-word


def test_whole_word_prefix_via_wildcard(reader_config):
    assert run(reader_config, "albert*") == ex.ALBERT_PREFIX  # whole words starting with albert, not Talbert


def test_contains_applies_to_proximity_terms(reader_config):
    got = run(reader_config, "alb", "mr", near=3, whole_word=False)
    assert got == {ex.ALBERT_WHOLE}
    assert run(reader_config, "alb", "mr", near=3) == set()  # whole-word 'alb' does not exist


def test_text_indexed_columns_are_detected(reader_connection):
    from scout.search import discover_columns

    cols = {(c.table, c.column): c.text_indexed for c in discover_columns(
        reader_connection, SearchOptions(terms=("x",), schemas=("SCOUT_DOCS",)))}
    assert cols[("INDEXED_NOTES", "BODY")] is True
    assert cols[("PLAIN_NOTES", "BODY")] is False
    assert cols[("DOCUMENTS", "BODY")] is False


def test_indexed_and_unindexed_columns_give_identical_results(reader_config):
    def texts(table, **kw):
        got = run(reader_config, "zebra", tables=(table,), **kw)
        return {k[3] for k in got}

    assert texts("INDEXED_NOTES") == texts("PLAIN_NOTES") == ex.ZEBRA_TEXTS
    kw = dict(whole_word=False)
    assert texts("INDEXED_NOTES", **kw) == texts("PLAIN_NOTES", **kw) == ex.ZEBRA_CONTAINS_TEXTS


def test_user_is_genuinely_read_only(reader_connection):
    cur = reader_connection.cursor()
    cur.execute("SELECT privilege FROM session_privs")
    assert {r[0] for r in cur} == {"CREATE SESSION"}
    with pytest.raises(Exception, match="ORA-"):
        cur.execute("INSERT INTO scout_hr.empty_log VALUES ('nope')")


def test_sessions_are_read_only_even_for_a_privileged_user(admin_config):
    # db.connect arms READ ONLY, so even SYSTEM cannot write through SCOUT's connections
    from scout import db

    conn = db.connect(admin_config)
    try:
        with pytest.raises(Exception, match="ORA-01456"):
            conn.cursor().execute("INSERT INTO scout_hr.empty_log VALUES ('nope')")
    finally:
        conn.close()


def test_dba_tier_never_searches_text_index_internals(admin_connection):
    from scout.search import discover_columns

    cols = discover_columns(admin_connection, SearchOptions(
        terms=("x",), tier=DictionaryTier.DBA, schemas=("SCOUT_DOCS",)))
    assert cols and not [c.table for c in cols if c.table.startswith("DR$")]


def test_dba_tier_zebra_search_finds_only_real_tables(admin_config):
    got = run(admin_config, "zebra", tier=DictionaryTier.DBA)
    assert {k[1] for k in got} == {"INDEXED_NOTES", "PLAIN_NOTES"}


def test_fetch_row_returns_the_whole_row(reader_config, reader_connection):
    from scout.search import fetch_row

    match = next(iter(search(reader_config, SearchOptions(terms=("albertina",), schemas=("SCOUT_HR",)))))
    row = {f.name: f for f in fetch_row(reader_connection, match)}
    assert row["ID"].value == "10" and row["FIRST_NAME"].value == "Jon" and row["NOTES"].value == "Albertina"


def test_broken_table_is_skipped_with_on_skip_and_search_continues(reader_config):
    skipped = []
    opts = SearchOptions(terms=("report",), whole_word=True, schemas=("SCOUT_HR", "SCOUT_BROKEN"), threads=4)
    got = {m.key for m in search(reader_config, opts, on_skip=lambda c, e: skipped.append((c, e)))}
    assert got == ex.REPORT_NOTES_CI
    assert [(c.owner, c.table, c.column) for c, _ in skipped] == [ex.BROKEN]
    assert "ORA-" in str(skipped[0][1])


def test_broken_table_raises_without_on_skip(reader_config):
    import oracledb

    opts = SearchOptions(terms=("report",), schemas=("SCOUT_BROKEN",), threads=2)
    with pytest.raises(oracledb.DatabaseError, match="ORA-"):
        list(search(reader_config, opts))


def test_whole_schema_is_searched_when_named(reader_connection):
    from scout.search import discover_columns

    cols = discover_columns(reader_connection, SearchOptions(terms=("x",), schemas=("SCOUT_DOCS",)))
    assert {c.table for c in cols} == {"DOCUMENTS", "INDEXED_NOTES", "PLAIN_NOTES"}


def test_bare_wildcard_is_refused_before_any_database_work():
    from scout.config import ConnectionConfig

    unreachable = ConnectionConfig(host="invalid.invalid", user="u", password="p")
    for term in ("*", "?", "%", "_"):
        with pytest.raises(ValueError, match="wildcard"):
            list(search(unreachable, SearchOptions(terms=(term,))))


def test_reserved_word_table_and_column_names(reader_config):
    # tables ORDER and TABLE, columns GROUP, LEVEL and COMMENT: valid only because identifiers are quoted
    assert run(reader_config, "reservedneedle", schemas=("SCOUT_ODD",)) == ex.RESERVED_HITS
