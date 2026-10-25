"""Search logic against mocked connections; no database needed."""
from unittest.mock import MagicMock

from scout import db
from scout.config import ConnectionConfig
from scout.search import DictionaryTier, Match, SearchOptions, discover_columns, search

DISCOVERY_ROWS = [  # owner, table, column, type, text_indexed
    ("S", "T", "A", "VARCHAR2", 0), ("S", "T", "B", "NUMBER", 0), ("S", "T", "C", "CLOB", 1),
    ("S", "T", "D", "DATE", 0), ("S", "T", "E", "NVARCHAR2", 0), ("S", "T", "F", "CHAR", 0),
]


def _conn_with_rows(rows):
    """Cursor works as a context manager and via fetchall/fetchmany."""
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.fetchall.return_value = rows
    cur.fetchmany.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def _executed_sql(conn):
    return " ".join(str(c.args[0]) for c in conn.cursor.return_value.execute.call_args_list)


def test_default_tier_reads_all_views_not_dba():
    conn = _conn_with_rows([])
    discover_columns(conn, SearchOptions(terms=("x",)))
    sql = _executed_sql(conn).upper()
    assert "ALL_TAB_COLUMNS" in sql and "DBA_" not in sql


def test_dba_tier_is_opt_in():
    conn = _conn_with_rows([])
    discover_columns(conn, SearchOptions(terms=("x",), tier=DictionaryTier.DBA))
    sql = _executed_sql(conn).upper()
    assert "DBA_TAB_COLUMNS" in sql and "ALL_TAB_COLUMNS" not in sql


def test_only_string_types_are_searchable_and_text_index_is_reported():
    cols = discover_columns(_conn_with_rows(DISCOVERY_ROWS), SearchOptions(terms=("x",)))
    assert {c.column for c in cols} == {"A", "C", "E", "F"}
    assert {c.column for c in cols if c.text_indexed} == {"C"}


def test_discovery_excludes_views_and_oracle_maintained_schemas():
    conn = _conn_with_rows([])
    discover_columns(conn, SearchOptions(terms=("x",)))
    sql = _executed_sql(conn).upper()
    assert "JOIN ALL_TABLES" in sql  # views are not in ALL_TABLES
    assert "ORACLE_MAINTAINED = 'Y'" in sql


def test_discovery_excludes_internal_storage_tables():
    conn = _conn_with_rows([])
    discover_columns(conn, SearchOptions(terms=("x",)))
    sql = " ".join(_executed_sql(conn).upper().split())
    for clause in ("TB.SECONDARY = 'N'", "TB.NESTED = 'NO'", "TB.DROPPED = 'NO'", "TB.IOT_TYPE IS NULL"):
        assert clause in sql


def test_worker_error_propagates_and_pool_is_closed(monkeypatch):
    import oracledb
    import pytest

    statements: list[str] = []
    pool = _fake_pool(statements)
    real_acquire = pool.acquire.side_effect

    def acquire():
        conn = real_acquire()
        make = conn.cursor

        def cursor():
            cur = make()
            orig = cur.execute.side_effect

            def execute(sql, *a, **kw):
                if "REGEXP_LIKE" in sql:
                    raise oracledb.DatabaseError("ORA-00942: table or view does not exist")
                return orig(sql, *a, **kw)

            cur.execute.side_effect = execute
            return cur

        conn.cursor = cursor
        return conn

    pool.acquire.side_effect = acquire
    monkeypatch.setattr(db, "create_pool", lambda cfg, size: pool)
    with pytest.raises(oracledb.DatabaseError, match="ORA-00942"):
        list(search(ConnectionConfig(), SearchOptions(terms=("x",), threads=2)))
    pool.close.assert_called_once()


def test_filters_are_binds_not_interpolated():
    conn = _conn_with_rows([])
    discover_columns(conn, SearchOptions(terms=("x",), schemas=("hr'; drop",), tables=("t",)))
    call = conn.cursor.return_value.execute.call_args
    assert "hr'" not in call.args[0].lower() and "DROP TABLE" not in call.args[0].upper()
    assert set(call.args[1].values()) == {"HR'; DROP", "T"}


def _fake_pool(statements):
    """Pool whose connections record every SQL statement; discovery returns DISCOVERY_ROWS."""
    def make_cursor():
        cur = MagicMock()
        cur.__enter__.return_value = cur

        def execute(sql, *a, **kw):
            statements.append(sql)
            cur.fetchall.return_value = DISCOVERY_ROWS if "TAB_COLUMNS" in sql else []

        cur.execute.side_effect = execute
        cur.fetchmany.return_value = []
        return cur

    pool = MagicMock()
    pool.acquire.side_effect = lambda: MagicMock(cursor=make_cursor)
    return pool


def test_search_only_issues_read_only_sql(monkeypatch):
    statements: list[str] = []
    monkeypatch.setattr(db, "create_pool", lambda cfg, size: _fake_pool(statements))
    assert list(search(ConnectionConfig(), SearchOptions(terms=("x",), threads=3))) == []
    assert statements
    for sql in statements:
        head = sql.strip().upper()
        assert head.startswith(("SELECT", "WITH", "SET TRANSACTION READ ONLY")), sql
    assert any("REGEXP_LIKE" in s for s in statements)


def test_search_term_is_a_bind_variable(monkeypatch):
    statements, binds = [], []

    def spy_pool(cfg, size):
        pool = _fake_pool(statements)
        real = pool.acquire.side_effect

        def acquire():
            conn = real()
            inner = conn.cursor

            def cursor():
                cur = inner()
                orig = cur.execute.side_effect
                cur.execute.side_effect = lambda sql, *a, **kw: (binds.append(kw), orig(sql, *a, **kw))[1]
                return cur

            conn.cursor = cursor
            return conn

        pool.acquire.side_effect = acquire
        return pool

    monkeypatch.setattr(db, "create_pool", spy_pool)
    evil = "x'); DROP TABLE t; --"
    list(search(ConnectionConfig(), SearchOptions(terms=(evil,), threads=1)))
    assert not any("DROP TABLE" in s for s in statements)
    assert any("DROP" in str(b.get("pat", "")) for b in binds)  # it travelled as the bind


def test_supplied_pool_is_used_and_left_open(monkeypatch):
    statements: list[str] = []
    monkeypatch.setattr(db, "create_pool", lambda cfg, size: (_ for _ in ()).throw(AssertionError("own pool")))
    pool = _fake_pool(statements)
    pool.max = 8
    for _ in range(2):  # a session pool serves back-to-back searches
        assert list(search(ConnectionConfig(), SearchOptions(terms=("x",), threads=3), pool=pool)) == []
    assert any("REGEXP_LIKE" in s for s in statements)
    pool.close.assert_not_called()


def test_own_pool_is_closed_when_none_supplied(monkeypatch):
    pool = _fake_pool([])
    monkeypatch.setattr(db, "create_pool", lambda cfg, size: pool)
    list(search(ConnectionConfig(), SearchOptions(terms=("x",), threads=2)))
    pool.close.assert_called_once()


def test_threads_are_capped_at_supplied_pool_size():
    import threading

    pool = _fake_pool([])
    pool.max = 2
    seen = set()
    real = pool.acquire.side_effect

    def acquire():
        seen.add(threading.current_thread().name)
        return real()

    pool.acquire.side_effect = acquire
    list(search(ConnectionConfig(), SearchOptions(terms=("x",), threads=16), pool=pool))
    assert len({n for n in seen if n.startswith("scout")}) <= 2


def test_open_session_validates_login_and_closes_pool_on_failure(monkeypatch):
    import pytest

    pool = MagicMock()
    pool.acquire.side_effect = RuntimeError("ORA-01017")
    monkeypatch.setattr(db, "create_pool", lambda cfg, size: pool)
    with pytest.raises(RuntimeError, match="ORA-01017"):
        db.open_session(ConnectionConfig(), (), 4)
    pool.close.assert_called_once()

    ok = MagicMock()
    monkeypatch.setattr(db, "create_pool", lambda cfg, size: ok)
    session = db.open_session(ConnectionConfig(), ("A",), 4)
    assert session.schemas == ("A",) and session.pool is ok
    session.close()
    ok.close.assert_called_once()
