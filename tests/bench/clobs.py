"""Build a dataset of large CLOBs from real text (Wikipedia via Hugging Face, Project Gutenberg).

Creates schema SCOUT_CLOB:

  WIKI_SIMPLE (id, title, body CLOB)   Simple English Wikipedia (snapshot 20231101), every article
  WIKI_JA     (id, title, body NCLOB)  the first --ja-rows articles of Japanese Wikipedia (3-byte UTF-8)
  BOOKS       (id, title, body CLOB)   four Gutenberg books (2-5.6 MB each) and HUGE: War and Peace
                                       repeated --huge-copies times (about 100 MB by default)

Each planted word is in exactly one row:
  FOURK      spans byte 4,000 (the SQL VARCHAR2 limit) of a WIKI_SIMPLE article
  THIRTYTWOK spans byte 32,767 (the extended VARCHAR2 / PL/SQL limit) of a WIKI_SIMPLE article
  KANA       right after a run of kana, at character KANA_CHAR_OFFSET of a WIKI_JA article
  TAIL       the very last word of BOOKS row HUGE
  ABSENT     appears nowhere

Downloads are cached in $SCOUT_DATA_CACHE (default ~/.cache/scout), never in the repository, and
checked against pinned sha256 sums. Wikipedia text is CC BY-SA 3.0; Gutenberg texts are public
domain. Simple English is a 157 MB download, the Japanese shard 611 MB (skip it with --no-ja).

Usage: python -m tests.bench.clobs [--rows N] [--ja-rows N] [--no-ja] [--huge-copies N]
Needs the admin credentials in .env and `pip install -e ".[bench]"` (pyarrow).
"""
import argparse
import hashlib
import os
import time
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

import oracledb
from dotenv import load_dotenv

from scout import db
from scout.config import ConnectionConfig
from tests.bench.load import ABSENT  # noqa: F401  (re-exported: the word that is nowhere)

SCHEMA = "SCOUT_CLOB"
FOURK = "zqclobfourk"
THIRTYTWOK = "zqclobthirtytwok"
KANA = "zqclobkana"
TAIL = "zqclobtail"
KANA_CHAR_OFFSET = 20_000
KANA_RUN = "かなカナ" * 16
HUGE_TITLE = "HUGE"
BATCH = 500
USER_AGENT = "SCOUT-test-data/0.1 (test fixture loader)"

Row = tuple[int, str, str]  # (id, title, body)


@dataclass(frozen=True)
class Source:
    url: str
    sha256: str
    name: str  # path inside the cache directory


@dataclass(frozen=True)
class Planted:
    word: str
    table: str
    id: int


_HF = "https://huggingface.co/datasets/wikimedia/wikipedia/resolve/b04c8d1ceb2f5cd4588862100d08de323dccfbaa"
WIKI_SIMPLE = Source(
    f"{_HF}/20231101.simple/train-00000-of-00001.parquet",
    "31bded16768a47c286becd292079122f5d7d4397a17b87d4250a00ccd581e6f0",
    "hf/20231101.simple/train-00000-of-00001.parquet",
)
WIKI_JA = Source(
    f"{_HF}/20231101.ja/train-00000-of-00015.parquet",
    "4751c14478e712fd637bd83c2cf3537b0e299ea5115e9a78ddededf42f34c29d",
    "hf/20231101.ja/train-00000-of-00015.parquet",
)


def _gutenberg(ebook: int, sha256: str) -> Source:
    return Source(f"https://www.gutenberg.org/cache/epub/{ebook}/pg{ebook}.txt", sha256,
                  f"gutenberg/pg{ebook}.txt")


BOOKS = {
    "War and Peace": _gutenberg(2600, "2d5bb2ad5f422765e714617e21fa31bbaf8958aa79682c86fca6660fcc5d1b2b"),
    "Les Misérables": _gutenberg(135, "6cf3b9d6fc5e6f733737425267d3af296d7f99f9b9fd1d255a3ab7689a6346ab"),
    "The Complete Works of William Shakespeare":
        _gutenberg(100, "eb61e33cab23dbe712ad57091ded6c7292ba91ac3bb2764f51a3223ec0068fe5"),
    "Don Quijote": _gutenberg(2000, "534f41d59f7142163fa0964076ac6351845c006ea6433778be637fac6d5b04d7"),
}


# --- planting (pure) -------------------------------------------------------------------------


def char_index_at_byte(text: str, byte_offset: int) -> int:
    """Index of the first character that starts at or after byte_offset of text's UTF-8 form."""
    position = 0
    for index, char in enumerate(text):
        if position >= byte_offset:
            return index
        position += len(char.encode())
    return len(text)


def insert_spanning_byte(text: str, word: str, byte_offset: int) -> str:
    """text with ' word ' inserted so that the word's UTF-8 bytes cover byte_offset."""
    index = char_index_at_byte(text, byte_offset - len(word) // 2)
    return f"{text[:index]} {word} {text[index:]}"


def insert_after_kana(text: str, word: str, char_offset: int) -> str:
    """text with a run of kana and then word inserted at character char_offset."""
    return f"{text[:char_offset]}{KANA_RUN}{word} {text[char_offset:]}"


def _plant_first(rows: Iterable[Row], table: str, word: str, min_bytes: int,
                 change: Callable[[str], str], planted: list[Planted]) -> Iterator[Row]:
    """Yield rows, changing the first whose body has at least min_bytes; record it in planted."""
    done = False
    for row_id, title, body in rows:
        if not done and len(body.encode()) >= min_bytes:
            body, done = change(body), True
            planted.append(Planted(word, table, row_id))
        yield row_id, title, body
    if not done:
        raise ValueError(f"no {table} row has {min_bytes} bytes to plant {word!r}; load more rows")


def plant_simple(rows: Iterable[Row], planted: list[Planted]) -> Iterator[Row]:
    """WIKI_SIMPLE rows with FOURK and THIRTYTWOK planted (a long first article may get both)."""
    rows = _plant_first(rows, "WIKI_SIMPLE", FOURK, 8_000,
                        lambda body: insert_spanning_byte(body, FOURK, 4_000), planted)
    return _plant_first(rows, "WIKI_SIMPLE", THIRTYTWOK, 40_000,
                        lambda body: insert_spanning_byte(body, THIRTYTWOK, 32_767), planted)


def plant_ja(rows: Iterable[Row], planted: list[Planted]) -> Iterator[Row]:
    """WIKI_JA rows with KANA planted at a character (not byte) offset."""
    # at most 3 bytes per character here, so this many bytes guarantees the offset exists
    return _plant_first(rows, "WIKI_JA", KANA, 3 * KANA_CHAR_OFFSET,
                        lambda body: insert_after_kana(body, KANA, KANA_CHAR_OFFSET), planted)


def books_with_huge(books: dict[str, str], huge_source: str, copies: int) -> list[Row]:
    """Rows for BOOKS: each book, then HUGE (huge_source repeated, ending with TAIL)."""
    rows = [(i, title, text) for i, (title, text) in enumerate(books.items(), start=1)]
    huge = "\n".join([huge_source] * copies) + f"\n{TAIL}"
    rows.append((len(rows) + 1, HUGE_TITLE, huge))
    return rows


# --- downloads -------------------------------------------------------------------------------


def cache_dir() -> Path:
    return Path(os.environ.get("SCOUT_DATA_CACHE", Path.home() / ".cache" / "scout"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(source: Source) -> Path:
    """The cached file for source, downloading it first if missing or changed."""
    path = cache_dir() / source.name
    if path.exists() and _sha256(path) == source.sha256:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    print(f"downloading {source.url}")
    request = urllib.request.Request(source.url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request) as response, partial.open("wb") as out:
        for chunk in iter(lambda: response.read(1 << 20), b""):
            out.write(chunk)
    actual = _sha256(partial)
    if actual != source.sha256:
        raise RuntimeError(f"{source.url}: sha256 is {actual}, expected {source.sha256}. The file "
                           "changed upstream; check it and update the pin in tests/bench/clobs.py.")
    partial.replace(path)
    return path


def read_wikipedia(path: Path, limit: int | None) -> Iterator[Row]:
    """(id, title, text) rows from a Hugging Face wikimedia/wikipedia parquet file."""
    import pyarrow.parquet as pq  # bench-only dependency

    def rows() -> Iterator[Row]:
        batches = pq.ParquetFile(path).iter_batches(batch_size=1_000, columns=["id", "title", "text"])
        for batch in batches:
            columns = (batch.column(name).to_pylist() for name in ("id", "title", "text"))
            for row_id, title, text in zip(*columns):
                yield int(row_id), title, text

    return islice(rows(), limit)


def read_book(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


# --- database --------------------------------------------------------------------------------


def _insert(conn: oracledb.Connection, table: str, rows: Iterable[Row],
            lob_type: oracledb.DbType) -> None:
    cur = conn.cursor()
    sql = f"INSERT INTO {table} (id, title, body) VALUES (:1, :2, :3)"
    rows = iter(rows)
    while batch := list(islice(rows, BATCH)):
        cur.setinputsizes(None, None, lob_type)  # values over 32 KB must bind as LOBs
        cur.executemany(sql, batch)
        conn.commit()


def build(conn: oracledb.Connection, simple_rows: Iterable[Row], ja_rows: Iterable[Row] | None,
          books: list[Row], schema: str = SCHEMA) -> list[Planted]:
    """Drop and recreate schema, load the rows with the words planted; return where they went.

    ja_rows=None leaves out WIKI_JA. books must end with the HUGE row (see books_with_huge)."""
    cur = conn.cursor()
    try:
        cur.execute(f"DROP USER {schema} CASCADE")
    except oracledb.DatabaseError as exc:
        if "ORA-01918" not in str(exc):
            raise
    cur.execute(f"CREATE USER {schema} NO AUTHENTICATION QUOTA UNLIMITED ON users")
    tables = {
        "WIKI_SIMPLE": "id NUMBER, title VARCHAR2(1000 CHAR), body CLOB",
        "BOOKS": "id NUMBER, title VARCHAR2(200 CHAR), body CLOB",
    }
    if ja_rows is not None:
        tables["WIKI_JA"] = "id NUMBER, title NVARCHAR2(1000), body NCLOB"
    for name, columns in tables.items():
        cur.execute(f"CREATE TABLE {schema}.{name} ({columns})")
        cur.execute(f"GRANT SELECT ON {schema}.{name} TO scout_reader")

    planted: list[Planted] = []
    _insert(conn, f"{schema}.WIKI_SIMPLE", plant_simple(simple_rows, planted), oracledb.DB_TYPE_CLOB)
    if ja_rows is not None:
        _insert(conn, f"{schema}.WIKI_JA", plant_ja(ja_rows, planted), oracledb.DB_TYPE_NCLOB)
    _insert(conn, f"{schema}.BOOKS", books, oracledb.DB_TYPE_CLOB)
    planted.append(Planted(TAIL, "BOOKS", books[-1][0]))
    # A READ ONLY transaction cannot read an object whose DDL timestamp is within its own
    # second (ORA-01466), so let the clock move on before anyone searches.
    time.sleep(2)
    return planted


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rows", type=int, default=None, help="cap on Simple English articles (default: all)")
    ap.add_argument("--ja-rows", type=int, default=20_000)
    ap.add_argument("--no-ja", action="store_true", help="leave out WIKI_JA (saves a 611 MB download)")
    ap.add_argument("--huge-copies", type=int, default=30)
    args = ap.parse_args()
    cfg = ConnectionConfig(
        host=os.environ.get("SCOUT_TEST_DB_HOST", "localhost"),
        port=int(os.environ.get("SCOUT_TEST_DB_PORT", "1521")),
        service_name=os.environ.get("SCOUT_TEST_DB_SERVICE_NAME", "FREEPDB1"),
        user=os.environ.get("SCOUT_TEST_DB_USER", "system"),
        password=os.environ["SCOUT_TEST_DB_PASSWORD"],
    )
    simple = read_wikipedia(fetch(WIKI_SIMPLE), args.rows)
    ja = None if args.no_ja else read_wikipedia(fetch(WIKI_JA), args.ja_rows)
    texts = {title: read_book(fetch(source)) for title, source in BOOKS.items()}
    books = books_with_huge(texts, texts["War and Peace"], args.huge_copies)
    start = time.perf_counter()
    planted = build(db.connect(cfg, read_only=False), simple, ja, books)
    print(f"built {SCHEMA} in {time.perf_counter() - start:.0f} s")
    for p in planted:
        print(f"  {p.word:<18} {p.table}.id = {p.id}")


if __name__ == "__main__":
    main()
