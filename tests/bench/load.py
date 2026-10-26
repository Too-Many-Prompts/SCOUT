"""Generate a larger dataset for timing searches (separate from the small functional seed).

Creates schema SCOUT_BENCH with tables T01..Tnn, each: ID, six VARCHAR2(200) columns C1..C6
and a CLOB BODY, filled with deterministic pseudo-random lowercase words.

  needle  'zxqneedle'   planted in T01.C1 on rows where MOD(ID, 10) = 0
  absent  'zzznotthere' appears nowhere (measures the full-scan cost of a search with no hits)

Usage: python -m tests.bench.load --tables 20 --rows 50000
Needs the admin credentials in .env; the only writer besides tests/db/load_seed.py.
"""
import argparse
import os
import time

import oracledb
from dotenv import load_dotenv

from scout import db
from scout.config import ConnectionConfig

SCHEMA = "SCOUT_BENCH"
NEEDLE = "zxqneedle"
ABSENT = "zzznotthere"
TEXT_COLUMNS = 6


def _words() -> str:
    """SQL expression: a few random lowercase words (about 60 characters)."""
    parts = ["DBMS_RANDOM.STRING('l', 4 + MOD(ABS(DBMS_RANDOM.RANDOM), 8))" for _ in range(6)]
    return " || ' ' || ".join(parts)


def build(conn: oracledb.Connection, tables: int, rows: int, schema: str = SCHEMA) -> None:
    """Drop and recreate the benchmark schema; grant SELECT on every table to scout_reader."""
    cur = conn.cursor()
    try:
        cur.execute(f"DROP USER {schema} CASCADE")
    except oracledb.DatabaseError as exc:
        if "ORA-01918" not in str(exc):
            raise
    cur.execute(f"CREATE USER {schema} NO AUTHENTICATION QUOTA UNLIMITED ON users")
    cur.execute("BEGIN DBMS_RANDOM.SEED(42); END;")
    cols = ", ".join(f"c{i} VARCHAR2(200)" for i in range(1, TEXT_COLUMNS + 1))
    for t in range(1, tables + 1):
        name = f"T{t:02d}"
        cur.execute(f"CREATE TABLE {schema}.{name} (id NUMBER, {cols}, body CLOB)")
        values = [
            f"CASE WHEN {t} = 1 AND MOD(lv, 10) = 0 THEN '{NEEDLE} ' || {_words()} ELSE {_words()} END"
        ] + [_words() for _ in range(2, TEXT_COLUMNS + 1)]
        select = ", ".join(values)
        cur.execute(
            f"INSERT INTO {schema}.{name} "
            f"SELECT lv, {select}, TO_CLOB({_words()}) "
            f"FROM (SELECT LEVEL AS lv FROM dual CONNECT BY LEVEL <= :n)",
            n=rows,
        )
        conn.commit()
        cur.execute(f"GRANT SELECT ON {schema}.{name} TO scout_reader")
    # A READ ONLY transaction cannot read an object whose DDL timestamp is within its own
    # second (ORA-01466), so let the clock move on before anyone searches.
    time.sleep(2)


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tables", type=int, default=20)
    ap.add_argument("--rows", type=int, default=50_000)
    args = ap.parse_args()
    cfg = ConnectionConfig(
        host=os.environ.get("SCOUT_TEST_DB_HOST", "localhost"),
        port=int(os.environ.get("SCOUT_TEST_DB_PORT", "1521")),
        service_name=os.environ.get("SCOUT_TEST_DB_SERVICE_NAME", "FREEPDB1"),
        user=os.environ.get("SCOUT_TEST_DB_USER", "system"),
        password=os.environ["SCOUT_TEST_DB_PASSWORD"],
    )
    conn = db.connect(cfg, read_only=False)
    build(conn, args.tables, args.rows)
    print(f"built {SCHEMA}: {args.tables} tables x {args.rows} rows")


if __name__ == "__main__":
    main()
