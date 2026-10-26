import os

import oracledb
import pytest
from dotenv import load_dotenv

load_dotenv()  # test credentials live in .env (never committed)

from scout import db  # noqa: E402
from scout.config import ConnectionConfig  # noqa: E402

DB_HOST = os.environ.get("SCOUT_TEST_DB_HOST", "localhost")
DB_PORT = os.environ.get("SCOUT_TEST_DB_PORT", "1521")
DB_SERVICE_NAME = os.environ.get("SCOUT_TEST_DB_SERVICE_NAME", "FREEPDB1")
DB_USER = os.environ.get("SCOUT_TEST_DB_USER", "system")
DB_PASSWORD = os.environ.get("SCOUT_TEST_DB_PASSWORD")
READER_USER = os.environ.get("SCOUT_TEST_READER_USER", "scout_reader")
READER_PASSWORD = os.environ.get("SCOUT_TEST_READER_PASSWORD")


def _config(user, password):
    return ConnectionConfig(
        host=DB_HOST, port=int(DB_PORT), service_name=DB_SERVICE_NAME, user=user, password=password
    )


def _connect_or_skip(cfg, read_only=True):
    if not cfg.password:
        pytest.skip(f"password for {cfg.user} not set; skipping integration test")
    try:
        return db.connect(cfg, read_only=read_only)  # thick mode: db.connect initialises the client
    except oracledb.Error as exc:
        pytest.skip(f"Oracle test container not reachable: {exc}")


@pytest.fixture(scope="session")
def admin_config():
    """SYSTEM; can use DBA_* views. Use only for the DBA tier and seeding; real users are read-only."""
    return _config(DB_USER, DB_PASSWORD)


@pytest.fixture(scope="session")
def reader_config():
    """scout_reader: low privilege, cannot see scout_hidden. Used for default (ALL_*) searches."""
    return _config(READER_USER, READER_PASSWORD)


@pytest.fixture(scope="session")
def reader_connection(reader_config):
    """Read-only session as the unprivileged user: what real SCOUT users have."""
    conn = _connect_or_skip(reader_config)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def oracle_connection(reader_connection):
    return reader_connection


@pytest.fixture(scope="session")
def admin_connection(admin_config):
    """DBA session. Only for tests that need elevated access (DBA tier); default is the reader."""
    conn = _connect_or_skip(admin_config)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def admin_writer_connection(admin_config):
    """Admin session that may write. Only for loaders that build test data (benchmark)."""
    conn = _connect_or_skip(admin_config, read_only=False)
    yield conn
    conn.close()
