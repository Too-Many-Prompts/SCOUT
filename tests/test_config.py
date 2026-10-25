from scout.config import DEFAULT_THREADS, ConnectionConfig, default_threads, load_defaults


def test_builtin_defaults(monkeypatch):
    for var in ("SCOUT_DB_HOST", "SCOUT_DB_PORT", "SCOUT_DB_SERVICE_NAME", "SCOUT_THREADS"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_defaults()
    assert (cfg.host, cfg.port, cfg.service_name) == ("localhost", 1521, "FREEPDB1")
    assert cfg.user is None and cfg.password is None  # credentials are never defaulted
    assert default_threads() == DEFAULT_THREADS == 8


def test_env_overrides_defaults(monkeypatch):
    monkeypatch.setenv("SCOUT_DB_HOST", "db.example")
    monkeypatch.setenv("SCOUT_DB_PORT", "1522")
    monkeypatch.setenv("SCOUT_THREADS", "3")
    cfg = load_defaults()
    assert (cfg.host, cfg.port) == ("db.example", 1522)
    assert default_threads() == 3


def test_runtime_overrides_beat_defaults_and_ignore_none():
    cfg = ConnectionConfig().with_overrides(host="h", port=None, service_name="svc")
    assert (cfg.host, cfg.port, cfg.service_name) == ("h", 1521, "svc")


import pytest

from scout.config import default_schemas


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    for var in ("SCOUT_DB_HOST", "SCOUT_DB_PORT", "SCOUT_DB_SERVICE_NAME", "SCOUT_DB_SCHEMA", "SCOUT_THREADS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_defaults_file_is_read_when_present(in_tmp):
    (in_tmp / "defaults.txt").write_text(
        "# comment\nhost = db.file\nport=1530  # trailing\nservice_name = SVC\nschema = HR, DOCS\nthreads = 4\n\nbogus line\n"
    )
    cfg = load_defaults()
    assert (cfg.host, cfg.port, cfg.service_name) == ("db.file", 1530, "SVC")
    assert default_schemas() == ("HR", "DOCS") and default_threads() == 4


def test_missing_defaults_file_is_fine(in_tmp):
    assert load_defaults().host == "localhost" and default_schemas() == ()


def test_env_beats_defaults_file(in_tmp, monkeypatch):
    (in_tmp / "defaults.txt").write_text("host = from.file\nport = 1530\n")
    monkeypatch.setenv("SCOUT_DB_HOST", "from.env")
    cfg = load_defaults()
    assert (cfg.host, cfg.port) == ("from.env", 1530)


def test_credentials_in_defaults_file_are_ignored(in_tmp):
    (in_tmp / "defaults.txt").write_text("user = scott\npassword = tiger\n")
    cfg = load_defaults()
    assert cfg.user is None and cfg.password is None


def test_bad_number_in_defaults_file_names_the_setting(in_tmp):
    (in_tmp / "defaults.txt").write_text("port = abc\n")
    with pytest.raises(ValueError, match="defaults.txt .port."):
        load_defaults()
