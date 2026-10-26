from textual import work
from textual.worker import get_current_worker
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, Label

from scout import db
from scout import search as search_core
from scout.config import ConnectionConfig, default_schemas, default_threads, load_defaults
from scout.export import write_csv
from scout.search import Match, SearchOptions


class LoginScreen(Screen):
    """Asks for the connection once; the resulting db.Session is reused by every search.

    The connection attempt runs in a thread worker so the event loop never blocks.
    """

    def __init__(self, cfg: ConnectionConfig, schemas: tuple[str, ...], error: str = "") -> None:
        super().__init__()
        self.cfg, self.schemas, self.error = cfg, schemas, error

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="login"):
            yield Label("Log in to Oracle")
            yield Input(self.cfg.host, placeholder="host", id="login-host")
            yield Input(str(self.cfg.port), placeholder="port", id="login-port")
            yield Input(self.cfg.service_name, placeholder="service name", id="login-service")
            yield Input(self.cfg.user or "", placeholder="user", id="login-user")
            yield Input(placeholder="password", password=True, id="login-password")
            yield Input(",".join(self.schemas), placeholder="schema(s) to search, blank = all visible", id="login-schema")
            yield Button("Log in", id="login-button")
            yield Label(self.error, id="login-error")

    def on_mount(self) -> None:
        self.query_one("#login-user" if not self.cfg.user else "#login-password", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "login-button":
            self.submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.submit()

    def _text(self, wid: str) -> str:
        return self.query_one(f"#{wid}", Input).value.strip()

    def _fail(self, text: str) -> None:
        self.query_one("#login-error", Label).update(text)
        self.query_one("#login-button", Button).disabled = False

    def submit(self) -> None:
        if not self.query_one("#login-button", Button).disabled:
            try:
                cfg = ConnectionConfig(
                    host=self._text("login-host"),
                    port=int(self._text("login-port")),
                    service_name=self._text("login-service"),
                    user=self._text("login-user"),
                    password=self.query_one("#login-password", Input).value,
                )
            except ValueError:
                return self._fail("Error: port must be a number")
            if not cfg.user or not cfg.password:
                return self._fail("Error: user and password are required")
            schemas = tuple(s.strip() for s in self._text("login-schema").split(",") if s.strip())
            self.query_one("#login-button", Button).disabled = True
            self.query_one("#login-error", Label).update("Connecting...")
            self._connect(cfg, schemas)

    @work(thread=True, exclusive=True, group="login")
    def _connect(self, cfg: ConnectionConfig, schemas: tuple[str, ...]) -> None:
        try:
            session = db.open_session(cfg, schemas, default_threads())
        except Exception as exc:  # bad credentials, unreachable host, ...: show it, stay on the screen
            self.app.call_from_thread(self._fail, f"Error: {exc}")
            return
        self.app.call_from_thread(self.app.logged_in, session)


class ScoutApp(App):
    """Login screen, then the search form, results table and CSV export.

    The search runs in a thread worker so the event loop (and the UI) never blocks.
    One login (db.Session) is shared by every search until Ctrl+L.
    """

    TITLE = "SCOUT"
    BINDINGS = [Binding("ctrl+l", "logout", "Log out")]
    CSS = """
    #login { width: 60; height: auto; margin: 2 4; }
    #login Input { width: 1fr; }
    #login-error { padding: 1 0; }
    Horizontal { height: auto; }
    Input { width: 1fr; }
    Checkbox, Button { width: auto; }
    #status { width: 1fr; padding: 1 1; }
    #results { height: 1fr; }
    #skipped { height: auto; max-height: 8; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.matches: list[Match] = []
        self.skipped = 0
        self.session: db.Session | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="search-form"):
            yield Input(placeholder="search term(s); * and ? are wildcards", id="terms")
            yield Input(placeholder="within N words (two terms)", id="near")
            yield Input(str(default_threads()), placeholder="threads", id="threads")
        with Horizontal(id="options"):
            yield Checkbox("whole word", id="whole_word")
            yield Checkbox("case sensitive", id="case")
            yield Button("Search", id="search")
        with Horizontal(id="export-form"):
            yield Input("scout_results.csv", placeholder="csv path", id="csv_path")
            yield Button("Export CSV", id="export")
            yield Label("", id="status")
        yield DataTable(id="results")
        yield DataTable(id="skipped")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#results", DataTable).add_columns("Owner", "Table", "Column", "Value")
        skipped = self.query_one("#skipped", DataTable)
        skipped.add_columns("Skipped column", "Reason")
        skipped.display = False
        self.push_screen(LoginScreen(load_defaults(), default_schemas()))

    def logged_in(self, session: db.Session) -> None:
        self.session = session
        cfg = session.cfg
        self.sub_title = f"{cfg.user}@{cfg.host}:{cfg.port}/{cfg.service_name}"
        self.pop_screen()
        self.query_one("#terms", Input).focus()
        self._status("Connected. Enter a search term.")

    def action_logout(self) -> None:
        """Back to the login screen: stop any search, close the pool, forget the password."""
        if self.session is None or isinstance(self.screen, LoginScreen):
            return
        session, self.session = self.session, None
        self.workers.cancel_group(self, "search")
        session.close()
        self.sub_title = ""
        self.push_screen(LoginScreen(session.cfg.with_overrides(password=None), session.schemas))

    def on_unmount(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "search":
            self.run_search()
        elif event.button.id == "export":
            self.export_csv()

    def _value(self, wid: str) -> str:
        return str(self.query_one(f"#{wid}").value).strip()

    def _status(self, text: str) -> None:
        self.query_one("#status", Label).update(text)

    def run_search(self) -> None:
        session = self.session
        if session is None:
            self._status("Error: not logged in")
            return
        try:
            near = int(self._value("near")) if self._value("near") else None
            terms = tuple(self._value("terms").split()) if near is not None else (self._value("terms"),)
            opts = SearchOptions(
                terms=terms,
                case_sensitive=self.query_one("#case", Checkbox).value,
                whole_word=self.query_one("#whole_word", Checkbox).value,
                near=near,
                threads=int(self._value("threads") or default_threads()),
                schemas=session.schemas,
            )
            from scout.pattern import build_regex

            build_regex(opts)
        except ValueError as exc:
            self._status(f"Error: {exc}")
            return
        self.matches = []
        self.skipped = 0
        self.query_one("#results", DataTable).clear()
        skipped = self.query_one("#skipped", DataTable)
        skipped.clear()
        skipped.display = False
        self._status("Searching...")
        self._search_worker(session, opts)

    @work(thread=True, exclusive=True, group="search")
    def _search_worker(self, session: db.Session, opts) -> None:
        """Runs off the event loop; every UI touch goes through call_from_thread."""
        worker = get_current_worker()

        def progress(p) -> None:
            if not worker.is_cancelled:
                self.call_from_thread(self._status, f"Searching {p.columns_done}/{p.columns_total} columns")

        try:
            def on_skip(col, exc) -> None:
                if not worker.is_cancelled:
                    self.call_from_thread(self._add_skipped, col, exc)

            for m in search_core.search(session.cfg, opts, progress, on_skip, pool=session.pool):
                if worker.is_cancelled:  # superseded by a newer search: stop and clean up
                    return
                self.call_from_thread(self._add_match, m)
        except Exception as exc:  # surface DB/connection errors in the UI instead of crashing
            if not worker.is_cancelled:
                self.call_from_thread(self._status, f"Error: {exc}")
            return
        if not worker.is_cancelled:
            self.call_from_thread(self._status, f"Done: {len(self.matches)} match(es)" + (f"; skipped {self.skipped} column(s), see below" if self.skipped else ""))

    def _add_skipped(self, col, exc) -> None:
        self.skipped += 1
        table = self.query_one("#skipped", DataTable)
        table.display = True
        table.add_row(f"{col.owner}.{col.table}.{col.column}", str(exc).splitlines()[0])

    def _add_match(self, m: Match) -> None:
        self.matches.append(m)
        self.query_one("#results", DataTable).add_row(
            m.owner, m.table, m.column, " ".join(m.value.split())
        )

    def export_csv(self) -> None:
        path = self._value("csv_path") or "scout_results.csv"
        try:
            with open(path, "w", newline="") as fp:
                n = write_csv(self.matches, fp)
        except OSError as exc:
            self._status(f"Export failed: {exc}")
            return
        self._status(f"Wrote {n} row(s) to {path}")


if __name__ == "__main__":
    ScoutApp().run()
