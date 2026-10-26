"""Cancel, time limit and progress: unit tests over a fake database (no Oracle needed)."""
import re
import threading
import time
from unittest.mock import MagicMock

import oracledb
import pytest

from scout import db, search as search_mod
from scout.config import ConnectionConfig
from scout.control import CancelToken, Progress
from scout.search import ColumnTimeout, SearchOptions, search


class FakeDb:
    """A pool of fake connections. `columns`: (owner, table, column, max_chars, blocks) tuples.
    `behaviour[column]` is called with (cursor) when that column's query runs and returns rows
    (or raises / blocks). Every connection records call_timeout values and cancel() calls."""

    def __init__(self, columns, behaviour=None):
        self.columns = columns
        self.behaviour = behaviour or {}
        self.conns: list[MagicMock] = []
        self.timeouts: list[int] = []
        self.pool = MagicMock(name="pool")
        self.pool.max = 8
        self.pool.acquire.side_effect = self._acquire

    def _acquire(self):
        fake = self
        conn = MagicMock(name="conn")
        conn.cancel_event = threading.Event()
        conn.cancel.side_effect = conn.cancel_event.set
        timeouts = self.timeouts

        class Cursor:
            rows: list = []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, *a, **kw):
                self.rows = []
                if "TAB_COLUMNS" in sql:
                    self.rows = [(o, t, c, "VARCHAR2", 0, mc, bl) for o, t, c, mc, bl in fake.columns]
                    self.discovery = True
                    return
                self.discovery = False
                m = re.search(r'SELECT ROWID, "([^"]+)"', sql)
                if m:
                    timeouts.append(conn.call_timeout)
                    self.rows = list(fake.behaviour.get(m.group(1), lambda cur, conn: [])(self, conn))

            def fetchall(self):
                return self.rows

            def fetchmany(self):
                rows, self.rows = self.rows, []
                return rows

        conn.cursor.side_effect = Cursor
        self.conns.append(conn)
        return conn

    def patch(self, monkeypatch):
        monkeypatch.setattr(db, "create_pool", lambda cfg, size: self.pool)


CFG = ConnectionConfig(user="u", password="p")
TWO_TABLES = [("S", "A", "C1", 100, 10), ("S", "A", "C2", 100, 10), ("S", "B", "C3", 100, 30)]


def opts(**kw):
    kw.setdefault("threads", 2)
    return SearchOptions(terms=("needle",), **kw)


def hit(cur, conn):
    return [("AAAB", "needle here")]


# ---- CancelToken

def test_cancel_calls_registered_connections_once_and_is_idempotent():
    token, conn = CancelToken(), MagicMock()
    token.register(conn)
    token.cancel()
    token.cancel()
    conn.cancel.assert_called_once()
    assert token.cancelled and token.is_set()


def test_a_connection_registered_after_cancel_is_cancelled_immediately():
    token, conn = CancelToken(), MagicMock()
    token.cancel()
    token.register(conn)
    conn.cancel.assert_called_once()


def test_unregistered_connections_are_left_alone_and_errors_are_ignored():
    token, quiet, broken = CancelToken(), MagicMock(), MagicMock()
    broken.cancel.side_effect = oracledb.Error("gone")
    token.register(quiet)
    token.register(broken)
    token.unregister(quiet)
    token.cancel()  # does not raise
    quiet.cancel.assert_not_called()


def test_cancelling_a_parent_cancels_linked_children_only_downwards():
    parent, child = CancelToken(), CancelToken()
    parent.link(child)
    child.cancel()
    assert not parent.cancelled
    parent2, child2 = CancelToken(), CancelToken()
    parent2.link(child2)
    parent2.cancel()
    assert child2.cancelled


# ---- search + cancel

def test_cancel_before_start_ends_cleanly_without_touching_the_database(monkeypatch):
    monkeypatch.setattr(db, "create_pool", lambda *a: pytest.fail("must not connect"))
    token = CancelToken()
    token.cancel()
    assert list(search(CFG, opts(), cancel=token)) == []


def test_cancel_after_finish_and_twice_is_harmless(monkeypatch):
    fake = FakeDb(TWO_TABLES, {"C1": hit})
    fake.patch(monkeypatch)
    token = CancelToken()
    assert len(list(search(CFG, opts(), cancel=token))) == 1
    token.cancel()
    token.cancel()


def test_cancel_interrupts_a_running_statement_without_reporting_a_skip(monkeypatch):
    def block_until_cancelled(cur, conn):
        assert conn.cancel_event.wait(5), "cancel() never reached the connection"
        raise oracledb.OperationalError("ORA-01013: user requested cancel of current operation")

    fake = FakeDb(TWO_TABLES[:1], {"C1": block_until_cancelled})
    fake.patch(monkeypatch)
    token, skipped = CancelToken(), []
    threading.Timer(0.2, token.cancel).start()
    start = time.monotonic()
    out = list(search(CFG, opts(), on_skip=lambda c, e: skipped.append(c), cancel=token))
    assert out == [] and skipped == [] and time.monotonic() - start < 3


def test_an_early_close_stops_workers_and_leaves_the_callers_token_unset(monkeypatch):
    fake = FakeDb(TWO_TABLES, {"C1": hit, "C2": hit, "C3": hit})
    fake.patch(monkeypatch)
    token = CancelToken()
    gen = search(CFG, opts(), cancel=token)
    next(gen)
    gen.close()
    assert not token.cancelled


# ---- progress

def test_progress_reaches_one_and_is_weighted_by_table_size(monkeypatch):
    FakeDb(TWO_TABLES).patch(monkeypatch)
    seen: list[Progress] = []
    list(search(CFG, opts(), on_progress=seen.append))
    last = seen[-1]
    assert (last.tables_done, last.tables_total, last.fraction) == (2, 2, 1.0)
    assert (last.columns_done, last.columns_total) == (3, 3)
    assert [p.fraction for p in seen] == sorted(p.fraction for p in seen)  # never goes backwards
    assert seen[0].fraction == 0.0


def test_progress_with_no_searchable_columns_finishes_at_one(monkeypatch):
    FakeDb([]).patch(monkeypatch)
    seen: list[Progress] = []
    assert list(search(CFG, opts(), on_progress=seen.append)) == []
    assert seen[-1] == Progress(0, 0, 1.0, (), 0, 0, 0)


def test_progress_counts_hits(monkeypatch):
    FakeDb(TWO_TABLES, {"C1": hit, "C3": hit}).patch(monkeypatch)
    seen: list[Progress] = []
    list(search(CFG, opts(), on_progress=seen.append))
    assert seen[-1].hits == 2


def test_heartbeat_names_the_slow_table_with_growing_elapsed_time(monkeypatch):
    monkeypatch.setattr(search_mod, "HEARTBEAT_SECONDS", 0.1)

    def slow(cur, conn):
        time.sleep(0.7)
        return []

    FakeDb(TWO_TABLES[:1], {"C1": slow}).patch(monkeypatch)
    seen: list[Progress] = []
    list(search(CFG, opts(), on_progress=seen.append))
    flying = [p.in_flight for p in seen if p.in_flight]
    assert len(flying) >= 2 and all(f[0][0] == "S.A" for f in flying)
    assert flying[-1][0][1] > flying[0][0][1]


# ---- time limit

TIMEOUT = oracledb.DatabaseError("DPY-4024: call timeout of 1000 ms exceeded\nORA-03156: OCI call timed out")


def test_time_limit_sets_call_timeout_and_resets_it_for_pool_reuse(monkeypatch):
    fake = FakeDb(TWO_TABLES[:1], {"C1": hit})
    fake.patch(monkeypatch)
    list(search(CFG, opts(time_limit=1.5)))
    assert fake.timeouts == [1500]
    assert fake.conns[-1].call_timeout == 0


def test_a_timeout_with_no_hits_is_reported_as_timed_out_not_as_empty(monkeypatch):
    def timeout(cur, conn):
        raise TIMEOUT

    FakeDb(TWO_TABLES[:1], {"C1": timeout}).patch(monkeypatch)
    outcomes, skipped = [], []
    list(search(CFG, opts(time_limit=1), on_column=outcomes.append, on_skip=lambda c, e: skipped.append(e)))
    assert [(o.examples, o.timed_out) for o in outcomes] == [(0, True)]
    assert len(skipped) == 1 and isinstance(skipped[0], ColumnTimeout) and "timed out" in str(skipped[0])


def test_a_timeout_after_some_hits_keeps_them_and_is_flagged_partial(monkeypatch):
    def partial(cur, conn):
        def fetchmany(self=cur, calls=[0]):
            calls[0] += 1
            if calls[0] == 1:
                return [("R1", "needle one")]
            raise TIMEOUT

        cur.fetchmany = fetchmany
        return []

    FakeDb(TWO_TABLES[:1], {"C1": partial}).patch(monkeypatch)
    outcomes, skipped = [], []
    got = list(search(CFG, opts(time_limit=1), on_column=outcomes.append, on_skip=lambda c, e: skipped.append(e)))
    assert len(got) == 1 and [(o.examples, o.timed_out) for o in outcomes] == [(1, True)]
    assert skipped == []


def test_other_database_errors_are_still_skips_or_raised(monkeypatch):
    def broken(cur, conn):
        raise oracledb.DatabaseError("ORA-00942: table or view does not exist")

    FakeDb(TWO_TABLES[:1], {"C1": broken}).patch(monkeypatch)
    with pytest.raises(oracledb.DatabaseError, match="ORA-00942"):
        list(search(CFG, opts(time_limit=1)))


# ---- needs the generated dataset: python -m tests.bench.load --tables 20 --rows 50000

@pytest.mark.bench
def test_cancel_stops_server_side_work(reader_config, admin_connection):
    """End to end: cancel() ends a running search quickly and leaves no statement running.

    With the pre-check on, each benchmark query is short, so this proves the quick stop and the
    clean server state; that cancel() really interrupts a *long* statement is shown by the unit
    tests (conn.cancel() is called, ORA-01013 is swallowed) and was measured directly: a
    3e8-row CONNECT BY statement ended 0.32 s after Connection.cancel()."""
    opts = SearchOptions(terms=("zzznotthere",), schemas=("SCOUT_BENCH",), threads=4)
    token = CancelToken()
    done = threading.Event()
    threading.Thread(target=lambda: (list(search(reader_config, opts, cancel=token)), done.set()), daemon=True).start()
    time.sleep(1.0)
    assert not done.is_set(), "search finished before it could be cancelled; use a bigger dataset"
    start = time.monotonic()
    token.cancel()
    assert done.wait(5), "search did not stop after cancel()"
    assert time.monotonic() - start < 3
    with admin_connection.cursor() as cur:
        for _ in range(30):  # sessions may take a moment to wind down
            cur.execute("SELECT COUNT(*) FROM v$session WHERE username = 'SCOUT_READER' AND status = 'ACTIVE'")
            if cur.fetchone()[0] == 0:
                return
            time.sleep(0.1)
    pytest.fail("a SCOUT_READER session is still running a statement after cancel()")


def test_an_interrupt_we_did_not_cause_stops_the_whole_search(monkeypatch):
    """The Oracle client reacts to Ctrl+C by breaking running statements (ORA-01013) even when
    Python never sees the signal. That means 'the user wants out': no skips, nothing more starts,
    and the caller's token reports it so the front end can say 'Cancelled'."""
    started = []

    def interrupted(cur, conn):
        started.append("C1")
        raise oracledb.OperationalError("ORA-01013: user requested cancel of current operation")

    def later(cur, conn):
        started.append("later")
        return []

    cols = [("S", f"T{i}", "C", 100, 10) for i in range(30)]
    FakeDb(cols, {"C": interrupted}).patch(monkeypatch)
    token, skipped = CancelToken(), []
    got = list(search(CFG, opts(threads=1), on_skip=lambda c, e: skipped.append(c), cancel=token))
    assert got == [] and skipped == []
    assert token.cancelled
    assert len(started) < 30  # the remaining tables were never started
