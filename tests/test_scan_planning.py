"""Which columns share a scan, and what happens when a shared scan fails. No database needed."""
import re
import threading
from unittest.mock import MagicMock

import oracledb
import pytest

from scout import search as search_mod
from scout.config import ConnectionConfig
from scout.search import Column, SearchOptions, _plan_units, search

CFG = ConnectionConfig(user="u", password="p")


def cols(n, table="T", blocks=None, owner="O"):
    return [Column(owner, table, f"C{i:02d}", "VARCHAR2", blocks=blocks) for i in range(1, n + 1)]


def plan(tables, **kw):
    grouped = {}
    for c in tables:
        grouped.setdefault((c.owner, c.table), []).append(c)
    kw.setdefault("terms", ("x",))
    return _plan_units(grouped, SearchOptions(**kw))


@pytest.fixture(autouse=True)
def default_limits(monkeypatch):
    monkeypatch.setattr(search_mod, "SINGLE_SCAN_MIN_BLOCKS", 1000)
    monkeypatch.setattr(search_mod, "MAX_COLUMNS_PER_SCAN", 20)


def test_a_small_table_is_searched_column_by_column():
    assert [len(u) for u in plan(cols(5, blocks=999))] == [1] * 5


def test_a_large_table_is_read_once():
    assert [len(u) for u in plan(cols(5, blocks=1000))] == [5]


def test_a_table_with_unknown_size_is_read_once():
    assert [len(u) for u in plan(cols(5, blocks=None))] == [5]


def test_proximity_is_always_column_by_column():
    assert [len(u) for u in plan(cols(5, blocks=10**6), terms=("a", "b"), near=3)] == [1] * 5


def test_one_scan_can_be_switched_off():
    search_mod.SINGLE_SCAN_MIN_BLOCKS = None
    assert [len(u) for u in plan(cols(5, blocks=10**6))] == [1] * 5


def test_a_table_with_one_string_column_is_just_that_column():
    assert plan(cols(1, blocks=10**6)) == [cols(1, blocks=10**6)]


@pytest.mark.parametrize("n,sizes", [(2, [2]), (20, [20]), (21, [11, 10]), (40, [20, 20]), (45, [15, 15, 15]), (1000, [20] * 50)])
def test_wide_tables_are_split_evenly_into_groups_of_at_most_twenty(n, sizes):
    units = plan(cols(n, blocks=None))
    assert [len(u) for u in units] == sizes
    assert [c for u in units for c in u] == cols(n)  # every column once, in order


def test_two_tables_never_share_a_unit():
    units = plan(cols(3, "A") + cols(3, "B", blocks=5) + cols(3, "C"))
    assert [{c.table for c in u} for u in units] == [{"A"}, {"B"}, {"B"}, {"B"}, {"C"}]


# ---- a failing shared scan falls back to one column at a time

def bad(msg="ORA-01476: divisor is equal to zero"):
    return oracledb.DatabaseError(msg)


class FakeDb:
    """Table T with columns A and B (size unknown, so they share a scan).

    `combined` / `single[column]` are called as (limit_bound) and return a list of fetch batches, or
    raise: a batch that is an Exception is raised when it is reached, as a failing fetch would."""

    def __init__(self, combined, single):
        self.combined, self.single = combined, single
        self.statements = []
        self.pool = MagicMock(name="pool")
        self.pool.max = 4
        self.pool.acquire.side_effect = self._acquire

    def _acquire(self):
        fake = self
        conn = MagicMock(name="conn")

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, *a, **binds):
                fake.statements.append(sql)
                if "TAB_COLUMNS" in sql:
                    self.batches = [[("O", "T", c, "VARCHAR2", 0, 100, None) for c in ("A", "B")]]
                    return
                single = re.match(r'SELECT ROWID, "(\w+)" FROM', sql)
                source = fake.single[single.group(1)] if single else fake.combined
                self.batches = list(source() if callable(source) else source)
                self.lim = binds.get("lim")

            def fetchall(self):
                return self.batches.pop(0)

            def fetchmany(self):
                if not self.batches:
                    return []
                batch = self.batches.pop(0)
                if isinstance(batch, Exception):
                    raise batch
                return batch

            arraysize = 1

        conn.cursor.side_effect = Cursor
        return conn


def run(fake, **kw):
    skipped, outcomes = [], {}
    kw.setdefault("on_skip", lambda col, exc: skipped.append((col.column, str(exc))))
    opts = SearchOptions(terms=("x",), schemas=("O",), threads=1, limit_per_column=kw.pop("limit", None))
    matches = list(search(CFG, opts, pool=fake.pool, on_column=lambda o: outcomes.update({o.column.column: o}), **kw))
    return matches, outcomes, skipped


def hits(matches):
    return sorted((m.column, m.rowid) for m in matches)


def test_the_shared_scan_is_one_statement_when_it_works():
    fake = FakeDb(combined=[[("R1", "a1", None), ("R2", None, "b2")], []], single={})
    matches, outcomes, skipped = run(fake)
    assert hits(matches) == [("A", "R1"), ("B", "R2")] and not skipped
    assert [(o.examples, o.more_available) for o in outcomes.values()] == [(1, False), (1, False)]
    scans = [s for s in fake.statements if s.startswith("SELECT ROWID")]
    assert len(scans) == 1 and scans[0].count('FROM "O"."T"') == 1


def test_a_failing_shared_scan_skips_only_the_column_that_cannot_be_read():
    fake = FakeDb(combined=lambda: [bad()], single={"A": [[("R1", "a1"), ("R2", "a2")], []], "B": lambda: [bad()]})
    matches, outcomes, skipped = run(fake)
    assert hits(matches) == [("A", "R1"), ("A", "R2")]
    assert [s[0] for s in skipped] == ["B"]  # reported once, and A is not among them
    assert (outcomes["A"].examples, outcomes["A"].more_available) == (2, False) and "B" not in outcomes


def test_hits_delivered_before_the_failure_are_not_repeated():
    def combined():
        return [[("R1", "a1", None)], bad()]  # A's first hit arrives, then the scan dies

    fake = FakeDb(combined, {"A": [[("R1", "a1"), ("R2", "a2")], []], "B": [[("R9", "b9")], []]})
    matches, outcomes, skipped = run(fake)
    assert hits(matches) == [("A", "R1"), ("A", "R2"), ("B", "R9")]  # R1 once
    assert not skipped and outcomes["A"].examples == 2


def test_a_column_never_ends_with_more_examples_than_the_limit_after_a_fallback():
    def combined():
        return [[("R1", "a1", None), ("R2", "a2", None)], bad()]

    # alone, the column's own first rows differ: R1 again, then R3 and R4 (limit + 1 rows are asked for)
    fake = FakeDb(combined, {"A": [[("R1", "a1"), ("R3", "a3"), ("R4", "a4")], []], "B": [[], []]})
    matches, outcomes, skipped = run(fake, limit=2)
    assert hits(matches) == [("A", "R1"), ("A", "R2")]
    assert (outcomes["A"].examples, outcomes["A"].more_available) == (2, True)


def test_without_on_skip_a_column_that_fails_alone_raises():
    fake = FakeDb(combined=lambda: [bad()], single={"A": [[], []], "B": lambda: [bad()]})
    with pytest.raises(oracledb.DatabaseError):
        run(fake, on_skip=None)


def test_a_cancel_is_not_retried_column_by_column():
    fake = FakeDb(combined=lambda: [oracledb.DatabaseError("ORA-01013: user requested cancel")],
                  single={"A": lambda: pytest.fail("retried after a cancel"), "B": lambda: pytest.fail("retried")})
    matches, outcomes, skipped = run(fake)
    assert not matches and not skipped
