"""CSV export of search results."""
import csv
from collections.abc import Iterable
from typing import TextIO

from scout.search import Match

CSV_HEADER = ("owner", "table", "column", "value")


def write_csv(matches: Iterable[Match], fp: TextIO) -> int:
    """Write matches (header + rows, sorted deterministically); return the row count."""
    rows = sorted(matches, key=lambda m: (m.owner, m.table, m.column, m.value, m.rowid or ""))
    writer = csv.writer(fp, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for m in rows:
        writer.writerow((m.owner, m.table, m.column, m.value))
    return len(rows)
