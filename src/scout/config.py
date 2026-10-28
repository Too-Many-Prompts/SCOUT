"""Connection and runtime defaults. Credentials are never defaulted: callers must prompt."""
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 1521
DEFAULT_SERVICE_NAME = "FREEPDB1"
DEFAULT_THREADS = 8
DEFAULT_LOCATE_LIMIT = 5  # example hits per column in the CLI, shell and TUI (--all lifts it)
DEFAULT_TIME_LIMIT = 300.0  # seconds a locate-mode query may run before its column "times out"
DEFAULTS_FILE = "defaults.txt"
_FILE_KEYS = {"host", "port", "service_name", "schema", "threads"}  # never user/password


@dataclass(frozen=True)
class ConnectionConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    service_name: str = DEFAULT_SERVICE_NAME
    user: str | None = None
    password: str | None = field(default=None, repr=False)  # never in repr, logs or tracebacks

    def with_overrides(self, **overrides) -> "ConnectionConfig":
        """Return a copy with every override that is not None applied."""
        return replace(self, **{k: v for k, v in overrides.items() if v is not None})


def _read_defaults_file(path: Path | None = None) -> dict[str, str]:
    """Optional `key = value` file (# comments); absent file or unknown keys are ignored."""
    path = path or Path(DEFAULTS_FILE)
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        line = line.split("#", 1)[0].strip()
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() in _FILE_KEYS:
            values[key.strip().lower()] = value.strip()
    return values


def _int_setting(name: str, env_var: str, file_values: dict[str, str], builtin: int) -> int:
    raw = os.environ.get(env_var) or file_values.get(name)
    if not raw:
        return builtin
    try:
        return int(raw)
    except ValueError:
        source = env_var if os.environ.get(env_var) else f"{DEFAULTS_FILE} ({name})"
        raise ValueError(f"{source}: expected a whole number, got {raw!r}") from None


def load_defaults() -> ConnectionConfig:
    """Host/port/service: built-ins < defaults.txt < SCOUT_DB_* env vars."""
    f = _read_defaults_file()
    return ConnectionConfig(
        host=os.environ.get("SCOUT_DB_HOST") or f.get("host") or DEFAULT_HOST,
        port=_int_setting("port", "SCOUT_DB_PORT", f, DEFAULT_PORT),
        service_name=os.environ.get("SCOUT_DB_SERVICE_NAME") or f.get("service_name") or DEFAULT_SERVICE_NAME,
    )


def parse_schemas(text: str) -> tuple[str, ...]:
    """'a, b,,c' -> ('a', 'b', 'c'); blank means every visible schema."""
    return tuple(s.strip() for s in text.split(",") if s.strip())


def default_schemas() -> tuple[str, ...]:
    """Schema(s) pre-filled at login (blank = every visible schema)."""
    return parse_schemas(os.environ.get("SCOUT_DB_SCHEMA") or _read_defaults_file().get("schema", ""))


def default_threads() -> int:
    return _int_setting("threads", "SCOUT_THREADS", _read_defaults_file(), DEFAULT_THREADS)
