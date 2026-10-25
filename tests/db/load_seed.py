"""Load tests/db/seed.sql into a running Oracle over a normal connection (no docker exec).

Idempotent: drops and recreates the SCOUT_* users. Usage: python -m tests.db.load_seed
Needs SCOUT_TEST_DB_PASSWORD and SCOUT_TEST_READER_PASSWORD in the environment/.env.
"""
import os
import re
from pathlib import Path

import oracledb
from dotenv import load_dotenv

from scout import db
from scout.config import ConnectionConfig

SEED = Path(__file__).with_name("seed.sql")
USERS = ("scout_hr", "scout_docs", "scout_odd", "scout_hidden", "scout_broken", "scout_reader")


def statements(sql: str, reader_password: str):
    sql = sql.replace("&1", reader_password)
    sql = "\n".join(l for l in sql.splitlines() if not l.strip().startswith(("--", "SET ")))
    return [s.strip() for s in re.split(r";\s*(?:\n|$)", sql) if s.strip()]


def main() -> None:
    load_dotenv()
    cfg = ConnectionConfig(
        host=os.environ.get("SCOUT_TEST_DB_HOST", "localhost"),
        port=int(os.environ.get("SCOUT_TEST_DB_PORT", "1521")),
        service_name=os.environ.get("SCOUT_TEST_DB_SERVICE_NAME", "FREEPDB1"),
        user=os.environ.get("SCOUT_TEST_DB_USER", "system"),
        password=os.environ["SCOUT_TEST_DB_PASSWORD"],
    )
    conn = db.connect(cfg, read_only=False)  # the only writer in the project
    cur = conn.cursor()
    for user in USERS:
        try:
            cur.execute(f"DROP USER {user} CASCADE")
        except oracledb.DatabaseError as exc:
            if "ORA-01918" not in str(exc):  # user does not exist
                raise
    for stmt in statements(SEED.read_text(), os.environ["SCOUT_TEST_READER_PASSWORD"]):
        cur.execute(stmt)
    conn.commit()
    print("seed loaded")


if __name__ == "__main__":
    main()
