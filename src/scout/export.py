"""CSV export of search results."""
import csv
from collections.abc import Collection, Iterable
from typing import TextIO

from scout.display import text
from scout.search import Match

CSV_HEADER = ("owner", "table", "column", "value")


def write_csv(
    matches: Iterable[Match], fp: TextIO, incomplete: Collection[tuple[str, str, str]] | None = None
) -> int:
    """Write matches (header + rows, sorted deterministically); return the row count.

    With `incomplete` (the (owner, table, column) keys that have more hits than were returned,
    or timed out) a fifth `complete` column says yes/no per row, so a file of examples is never
    mistaken for a full export. Without it the format is the plain four columns.

    Snippet matches write the snippet (with … where it was cut, whitespace kept) and a
    `more_in_value` yes/no column after `value`."""
    rows = sorted(matches, key=lambda m: (m.owner, m.table, m.column, m.value, m.rowid or ""))
    snippets = any(m.snippet is not None for m in rows)
    header = CSV_HEADER + (("more_in_value",) if snippets else ())
    writer = csv.writer(fp, lineterminator="\n")
    writer.writerow(header if incomplete is None else (*header, "complete"))
    for m in rows:
        row = (m.owner, m.table, m.column, text(m, fold=False))
        if snippets:
            row += ("yes" if m.snippet is not None and m.snippet.more_in_value else "no",)
        if incomplete is not None:
            row += ("no" if m.address in incomplete else "yes",)
        writer.writerow(row)
    return len(rows)
