"""CSV export of search results."""
import csv
from collections.abc import Collection, Iterable
from typing import TextIO

from scout.search import Match

CSV_HEADER = ("owner", "table", "column", "value")


def write_csv(
    matches: Iterable[Match], fp: TextIO, incomplete: Collection[tuple[str, str, str]] | None = None
) -> int:
    """Write matches (header + rows, sorted deterministically); return the row count.

    With `incomplete` (the (owner, table, column) keys that have more hits than were returned,
    or timed out) a fifth `complete` column says yes/no per row, so a file of examples is never
    mistaken for a full export. Without it the format is the plain four columns."""
    rows = sorted(matches, key=lambda m: (m.owner, m.table, m.column, m.value, m.rowid or ""))
    writer = csv.writer(fp, lineterminator="\n")
    writer.writerow(CSV_HEADER if incomplete is None else (*CSV_HEADER, "complete"))
    for m in rows:
        row = (m.owner, m.table, m.column, m.value)
        if incomplete is not None:
            row += ("no" if (m.owner, m.table, m.column) in incomplete else "yes",)
        writer.writerow(row)
    return len(rows)
