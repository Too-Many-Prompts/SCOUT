"""Small shared types for controlling and observing a running search (UI-agnostic)."""
import contextlib
import threading
from dataclasses import dataclass


class CancelToken:
    """Cooperative cancel for a search, plus a hard stop for statements already running.

    cancel() is safe from any thread, at any time, and idempotent. Connections registered while
    a statement runs get conn.cancel() (the statement ends with ORA-01013); a connection
    registered after cancel() is cancelled immediately. A token can be linked to a child token
    (search() links its own, so closing the generator early never poisons a caller's token).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._conns: list = []
        self._children: list["CancelToken"] = []

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def is_set(self) -> bool:  # lets a token stand in for threading.Event in worker loops
        return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            conns, children = list(self._conns), list(self._children)
        for conn in conns:
            _break(conn)
        for child in children:
            child.cancel()

    def register(self, conn) -> None:
        with self._lock:
            self._conns.append(conn)
            already = self._cancelled
        if already:
            _break(conn)

    def unregister(self, conn) -> None:
        with self._lock:
            if conn in self._conns:
                self._conns.remove(conn)

    def link(self, child: "CancelToken") -> None:
        with self._lock:
            self._children.append(child)
            already = self._cancelled
        if already:
            child.cancel()

    def unlink(self, child: "CancelToken") -> None:
        with self._lock:
            if child in self._children:
                self._children.remove(child)


def _break(conn) -> None:
    # the connection may be mid-statement, closing or already gone; there is nothing to do then
    with contextlib.suppress(Exception):
        conn.cancel()


@dataclass(frozen=True)
class Progress:
    """Where a search is. `fraction` is weighted by table size (blocks) when known.
    `in_flight` lists ("OWNER.TABLE", seconds_running) for tables being scanned, longest first."""

    tables_done: int
    tables_total: int
    fraction: float
    in_flight: tuple[tuple[str, float], ...]
    hits: int
    columns_done: int = 0
    columns_total: int = 0
