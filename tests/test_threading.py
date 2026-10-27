import pytest

from scout.search import SearchOptions, search
from tests.db import expected as ex

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("scan_mode")]


def keys(cfg, threads, **kw):
    opts = SearchOptions(terms=("col*",), schemas=ex.SCHEMAS, threads=threads, **kw)
    return [m.key for m in search(cfg, opts)]


def test_serial_and_parallel_return_identical_matches(reader_config):
    serial, parallel = keys(reader_config, 1), keys(reader_config, 8)
    assert sorted(serial) == sorted(parallel)
    assert len(parallel) == len(set(parallel))  # no duplicates from split work


def test_progress_callback_reaches_total(reader_config):
    seen = []
    opts = SearchOptions(terms=("report",), schemas=ex.SCHEMAS, threads=4)
    list(search(reader_config, opts, on_progress=seen.append))
    last = seen[-1]
    assert last.columns_done == last.columns_total > 0
    assert last.tables_done == last.tables_total and last.fraction == 1.0


def test_worker_error_propagates(reader_config):
    bad = reader_config.with_overrides(password="wrong-password")
    with pytest.raises(Exception) as exc:
        list(search(bad, SearchOptions(terms=("x",), threads=4)))
    assert not isinstance(exc.value, NotImplementedError)
