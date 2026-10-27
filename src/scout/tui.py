import contextlib
import re
from dataclasses import replace

from rich.text import Text
from textual import work
from textual.worker import get_current_worker
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import (
    Button, Checkbox, DataTable, Footer, Header, Input, Label, ProgressBar, TextArea,
)
from textual.widgets.text_area import Selection

from scout import db
from scout import search as search_core
from scout.config import (
    DEFAULT_LOCATE_LIMIT,
    DEFAULT_TIME_LIMIT,
    ConnectionConfig,
    default_schemas,
    default_threads,
    load_defaults,
    parse_schemas,
)
from scout.control import CancelToken, Progress
from scout.display import CUT, parts
from scout.export import write_csv
from scout.pattern import build_regex
from scout.search import (
    SNIPPET_CONTEXT, Column, ColumnOutcome, ColumnTimeout, Detail, Match, RowField, SearchOptions,
)


# One accent. Amber means "a hit"; the rest of the palette stays cool and quiet.
AMBER = "#F2B134"  # signal amber: the hit
AMBER_DIM = "#C98A2B"  # dimmer amber: partial / cut off

SCOUT_THEME = Theme(
    name="scout",
    primary=AMBER,
    secondary="#6FB79B",  # lichen: progress, done
    accent=AMBER,
    warning=AMBER_DIM,
    error="#E5645B",
    success="#6FB79B",
    foreground="#D6DED9",
    background="#0F1B22",  # survey ink
    surface="#172830",  # plate
    panel="#1E3340",
    dark=True,
)

HIT_STYLE = f"bold {AMBER}"

SCOUT_CSS = """
Screen { background: $background; }
Input { border: none; height: 1; padding: 0 1; background: $panel; }
Input:focus { border: none; background: $surface; }
Button { border: none; height: 1; min-width: 12; }
Checkbox { border: none; height: 1; padding: 0 1; background: transparent; }
#login { width: 64; height: auto; margin: 1 4; padding: 0 2; border: round $panel; border-title-color: $primary; }
#login .field { height: 2; }
#login .field Label { width: 12; padding: 0 1 0 0; color: $text-muted; }
#login .field Input { width: 1fr; }
#login-button { margin-top: 0; }
#login-error { padding: 0; color: $error; }
#login-error.busy { color: $text-muted; }

#search-panel { height: auto; padding: 0 1; border: round $panel; border-title-color: $primary; }
#search-form, #options, #export-form { height: auto; }
#search-form Input { width: 1fr; }
#near { width: 22; }
#threads { width: 12; }
#options Checkbox { width: auto; }
#options, #export-form { margin-top: 1; }
#options Button, #export-form Button { width: auto; margin-left: 1; }
#export-form Input { width: 36; }
#search { background: $primary; color: #0F1B22; text-style: bold; }

#progress-row { height: auto; padding: 0 1; }
#progress { width: 28; }
#status { width: 1fr; padding: 0 1; color: $text; }
#status.ok { color: $success; }
#status.warn { color: $warning; }
#status.error { color: $error; }

#body { height: 1fr; }
#found-pane { width: 1fr; height: 100%; border: round $panel; border-title-color: $primary; }
#hits-pane { width: 2fr; height: 100%; border: round $panel; border-title-color: $primary; }
#found { height: 1fr; }
#show-all { width: 100%; }
#results { height: 1fr; }
#empty { padding: 1 2; color: $text-muted; }
#skipped { height: auto; max-height: 6; border: round $warning; border-title-color: $warning; }
DataTable { background: $background; }

DetailScreen { align: center middle; }
#detail { width: 90%; height: 90%; border: round $primary; border-title-color: $primary; background: $surface; }
#detail-title { padding: 0 1; text-style: bold; }
#detail-note { padding: 0 1; color: $text-muted; }
#detail-text { height: 1fr; }
#detail-text .text-area--selection { background: $accent; color: $background; }

RowScreen { align: center middle; }
#row { width: 90%; height: 90%; border: round $primary; border-title-color: $primary; background: $surface; }
#row-title { padding: 0 1; text-style: bold; }
#row-note { padding: 0 1; color: $text-muted; }
#row-fields { height: 1fr; background: $surface; }
#whole-row { width: 100%; }
"""

VANISHED = "This row no longer matches (it changed after the search)."
ROW_GONE = "This row no longer exists (it changed after the search)."
HIT_MARK = "▶"


def format_size(n: int) -> str:
    """Bytes as '812 bytes', '12 KB' or '3.4 MB'."""
    if n < 1024:
        return f"{n:,} bytes"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if n >= 10 else f"{n:.1f} {unit}"
    raise AssertionError("unreachable")


def row_cell(f: RowField, hit: str | None = None) -> Text:
    """One value of the whole-row sheet. NULL and not-fetched content are dim, a cut value says how
    long it really is, and `hit` (the text Oracle matched, for the searched column) is amber."""
    if not f.shown:
        what = f"{f.dtype}, {format_size(f.length)}" if f.length is not None else f.dtype
        return Text(f"‹{what}›", style="dim")
    if f.value is None:
        return Text("NULL", style="dim italic")
    text = Text(" ".join(f.value.split()), overflow="fold")
    if hit:
        at = text.plain.find(" ".join(hit.split()))
        if at >= 0:
            text.stylize(HIT_STYLE, at, at + len(" ".join(hit.split())))
    if f.cut:
        text.append(CUT)
        text.append(f" ({len(f.value):,} of {f.length:,} characters)" if f.length else "", style="dim")
    return text


def detail_text(d: Detail) -> tuple[str, tuple[int, int], tuple[int, int]]:
    """The detail window's text and the hit's (row, column) start and end in it.

    \\r\\n and \\r become \\n first (the Gutenberg texts use \\r\\n); the locations follow the
    TextArea's own line splitting (see _location)."""
    s = d.snippet
    before, hit, after = (re.sub(r"\r\n?", "\n", p) for p in (s.before, s.hit, s.after))
    before = (CUT if s.cut_before else "") + before
    after = after + (CUT if s.cut_after else "")
    return before + hit + after, _location(before), _location(before + hit)


def _location(text: str) -> tuple[int, int]:
    """The (row, column) just after `text`, splitting lines as the TextArea's Document does
    (str.splitlines: form feeds, U+2028 and other separators break lines too)."""
    lines = text.splitlines(keepends=True)
    if not lines:
        return 0, 0
    last = lines[-1]
    if last.splitlines()[0] != last:  # text ends with a line break: the next row starts
        return len(lines), 0
    return len(lines) - 1, len(last)


def detail_note(d: Detail) -> str:
    """"showing N of M characters" when the window is not the whole value, else ""."""
    s = d.snippet
    if not (s.cut_before or s.cut_after):
        return ""
    shown = len(s.before) + len(s.hit) + len(s.after)
    return f"showing {shown:,} of {d.total_length:,} characters"


class DetailScreen(ModalScreen[None]):
    """A large window around one hit (DETAIL_CONTEXT characters each side), read-only and
    scrolled to the hit, which is selected (amber). Esc closes it."""

    BINDINGS = [Binding("escape", "dismiss", "Close"), Binding("r", "whole_row", "Whole row")]

    def __init__(self, match: Match) -> None:
        super().__init__()
        self.match = match
        self.ref = match.ref

    def compose(self) -> ComposeResult:
        with Vertical(id="detail"):
            yield Label(self.ref, id="detail-title")
            yield Label("Loading...", id="detail-note")
            yield TextArea(id="detail-text", read_only=True, soft_wrap=True)

    def on_mount(self) -> None:
        self.query_one("#detail").border_title = "Hit in context (Esc to close, r for the whole row)"

    def action_whole_row(self) -> None:
        self.dismiss()
        self.app.show_row(self.match)

    def show_detail(self, d: Detail | None) -> None:
        if not self.is_attached:  # closed while loading
            return
        note = self.query_one("#detail-note", Label)
        if d is None:
            note.update(VANISHED)
            return
        text, start, end = detail_text(d)
        area = self.query_one("#detail-text", TextArea)
        area.load_text(text)
        area.selection = Selection(start, end)
        area.scroll_cursor_visible(center=True)
        area.focus()
        note.update(detail_note(d))

    def show_error(self, message: str) -> None:
        if self.is_attached:
            self.query_one("#detail-note", Label).update(message)


class RowScreen(ModalScreen[None]):
    """Every column of the row behind a hit, one column per line: names down the left, values on the
    right, the searched column marked and its hit amber. Enter on that column opens the hit in
    context; Esc closes."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, match: Match) -> None:
        super().__init__()
        self.match = match

    def compose(self) -> ComposeResult:
        with Vertical(id="row"):
            yield Label(Text.assemble(f"{self.match.owner}.{self.match.table}  ", (str(self.match.rowid), "dim")),
                        id="row-title")
            yield Label("Loading...", id="row-note")
            yield DataTable(id="row-fields", cursor_type="row", zebra_stripes=False)

    def on_mount(self) -> None:
        self.query_one("#row").border_title = "Whole row (Esc to close)"

    def show_row(self, fields: list[RowField] | None) -> None:
        if not self.is_attached:  # closed while loading
            return
        note = self.query_one("#row-note", Label)
        if fields is None:
            note.update(ROW_GONE)
            return
        table = self.query_one("#row-fields", DataTable)
        width = max(20, table.size.width - 2 - max(len(f.name) for f in fields) - 20 - 8)
        table.add_column("", width=1)
        table.add_column("Column")
        table.add_column("Type")
        table.add_column("Value", width=width)
        hit = self.match.snippet.hit if self.match.snippet is not None else None
        for f in fields:
            is_hit = f.name == self.match.column
            table.add_row(
                Text(HIT_MARK, style=HIT_STYLE) if is_hit else "",
                Text(f.name, style=HIT_STYLE if is_hit else "bold"),
                Text(f.dtype.lower(), style="dim"),
                row_cell(f, hit if is_hit else None),
                key=f.name, height=None,
            )
            if is_hit:
                table.move_cursor(row=table.row_count - 1, scroll=True)
        note.update(f"{len(fields)} columns")
        table.focus()

    def show_error(self, message: str) -> None:
        if self.is_attached:
            self.query_one("#row-note", Label).update(message)


class LoginScreen(Screen):
    """Asks for the connection once; the resulting db.Session is reused by every search.

    The connection attempt runs in a thread worker so the event loop never blocks.
    """

    def __init__(self, cfg: ConnectionConfig, schemas: tuple[str, ...], error: str = "") -> None:
        super().__init__()
        self.cfg, self.schemas, self.error = cfg, schemas, error

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="login") as box:
            box.border_title = "Log in to Oracle"
            for label, inp in (
                ("Host", Input(self.cfg.host, placeholder="localhost", id="login-host")),
                ("Port", Input(str(self.cfg.port), placeholder="1521", id="login-port")),
                ("Service", Input(self.cfg.service_name, placeholder="service name", id="login-service")),
                ("User", Input(self.cfg.user or "", placeholder="user", id="login-user")),
                ("Password", Input(placeholder="password", password=True, id="login-password")),
                ("Schema(s)", Input(",".join(self.schemas), placeholder="comma-separated; blank = every visible schema",
                                    id="login-schema")),
            ):
                with Horizontal(classes="field"):
                    yield Label(label)
                    yield inp
            yield Button("Log in", id="login-button", variant="primary")
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
        err = self.query_one("#login-error", Label)
        err.remove_class("busy")
        err.update(text)
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
            schemas = parse_schemas(self._text("login-schema"))
            self.query_one("#login-button", Button).disabled = True
            err = self.query_one("#login-error", Label)
            err.add_class("busy")
            err.update("Connecting...")
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
    BINDINGS = [
        Binding("ctrl+l", "logout", "Log out"),
        Binding("escape", "cancel_search", "Cancel search"),
        Binding("ctrl+e", "export", "Export CSV"),
        Binding("r", "whole_row", "Whole row"),
    ]
    CSS = SCOUT_CSS

    def __init__(self) -> None:
        super().__init__()
        self.matches: list[Match] = []
        self.skipped = 0
        self.session: db.Session | None = None
        self.token: CancelToken | None = None  # cancels the running search or drill-down
        self.outcomes: dict[str, ColumnOutcome] = {}  # by "OWNER.TABLE.COLUMN"
        self.opts: SearchOptions | None = None  # the last search, for "Show all hits"
        self.limited = False  # results are examples (locate mode), not every hit
        self.cut_off: set[tuple[str, str, str]] = set()  # columns with more hits than shown / timed out
        self.partial = False  # the run was cancelled or timed out: whatever is listed is not everything
        self._highlight: re.Pattern | None = None  # cosmetic: marks the searched text in the Value column

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="search-panel") as panel:
            panel.border_title = "Search"
            with Horizontal(id="search-form"):
                yield Input(placeholder="search term(s); * and ? are wildcards", id="terms")
                yield Input(placeholder="within N words (two terms)", id="near")
                yield Input(str(default_threads()), placeholder="threads", id="threads")
            with Horizontal(id="options"):
                yield Checkbox("whole word", id="whole_word")
                yield Checkbox("case sensitive", id="case")
                yield Checkbox("every hit (slower)", id="all_hits")
                yield Button("Search", id="search")
                yield Button("Cancel", id="cancel", variant="error", disabled=True)
            with Horizontal(id="export-form"):
                yield Input("scout_results.csv", placeholder="csv path", id="csv_path")
                yield Button("Export CSV", id="export")
        with Horizontal(id="progress-row"):
            yield ProgressBar(total=100, show_eta=False, show_percentage=True, id="progress")
            yield Label("", id="status")
        with Horizontal(id="body"):
            with Vertical(id="found-pane") as pane:
                pane.border_title = "Where it was found"
                yield DataTable(id="found")
                yield Button("Show all hits", id="show-all")
            with Vertical(id="hits-pane") as pane:
                pane.border_title = "Hits"
                yield Label("No search yet. Type a term and press Enter.", id="empty")
                yield DataTable(id="results")
                yield Button("Whole row", id="whole-row")
        yield DataTable(id="skipped")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(SCOUT_THEME)
        self.theme = "scout"
        found = self.query_one("#found", DataTable)
        found.add_columns("Column", "Examples", "More")
        found.cursor_type = "row"
        results = self.query_one("#results", DataTable)
        results.add_columns("Owner", "Table", "Column", "Value")
        results.display = False
        self.query_one("#progress", ProgressBar).display = False
        skipped = self.query_one("#skipped", DataTable)
        skipped.add_columns("Skipped column", "Reason")
        skipped.border_title = "Skipped columns"
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
        self.action_cancel_search()
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
        elif event.button.id == "cancel":
            self.action_cancel_search()
        elif event.button.id == "show-all":
            self.show_all_hits()
        elif event.button.id == "whole-row":
            self.action_whole_row()

    def _value(self, wid: str) -> str:
        return str(self.query_one(f"#{wid}").value).strip()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("terms", "near", "threads"):
            self.run_search()
        elif event.input.id == "csv_path":
            self.export_csv()

    def action_export(self) -> None:
        if self.session is not None and not isinstance(self.screen, LoginScreen):
            self.export_csv()

    def _status(self, text: str, kind: str = "") -> None:
        """Set the status line; kind (ok / warn / error) colours it."""
        label = self.query_one("#status", Label)
        label.set_classes(kind)
        label.update(text)

    def _set_running(self, running: bool) -> None:
        """Cancel only makes sense while a search or drill-down runs; the bar only then too."""
        self.query_one("#cancel", Button).disabled = not running
        bar = self.query_one("#progress", ProgressBar)
        bar.display = running
        if not running:
            bar.update(progress=0)

    def _show_hits(self, empty_text: str | None = None) -> None:
        """Swap between the hits table and a short hint when there is nothing to list."""
        empty = self.query_one("#empty", Label)
        results = self.query_one("#results", DataTable)
        empty.display = empty_text is not None
        results.display = empty_text is None
        if empty_text is not None:
            empty.update(empty_text)

    def run_search(self) -> None:
        session = self.session
        if session is None:
            self._status("Error: not logged in", "error")
            return
        try:
            every_hit = self.query_one("#all_hits", Checkbox).value
            near = int(self._value("near")) if self._value("near") else None
            terms = tuple(self._value("terms").split()) if near is not None else (self._value("terms"),)
            opts = SearchOptions(
                terms=terms,
                case_sensitive=self.query_one("#case", Checkbox).value,
                whole_word=self.query_one("#whole_word", Checkbox).value,
                near=near,
                threads=int(self._value("threads") or default_threads()),
                schemas=session.schemas,
                limit_per_column=None if every_hit else DEFAULT_LOCATE_LIMIT,
                time_limit=None if every_hit else DEFAULT_TIME_LIMIT,
                snippet_context=SNIPPET_CONTEXT,
            )
            build_regex(opts)
        except ValueError as exc:
            self._status(f"Error: {exc}", "error")
            return
        self._highlight = _highlight_pattern(opts)
        self._start_results(limited=opts.limit_per_column is not None)
        self.skipped = 0
        self.outcomes = {}
        self.opts = opts
        self.query_one("#found", DataTable).clear()
        skipped = self.query_one("#skipped", DataTable)
        skipped.clear()
        skipped.display = False
        self._show_hits("Searching...")
        self._status("Searching...")
        self._search_worker(session, opts, self.token)

    def _start_results(self, limited: bool) -> None:
        """Stop whatever runs (on the server too) and empty the hits list for a new run.
        `limited`: the run returns examples (locate mode), not every hit."""
        self.action_cancel_search()
        self.token = CancelToken()
        self.matches = []
        self.limited = limited
        self.cut_off = set()
        self.partial = False
        self.query_one("#results", DataTable).clear()
        self._set_running(True)

    @work(thread=True, exclusive=True, group="search")
    def _search_worker(self, session: db.Session, opts, token: CancelToken) -> None:
        """Runs off the event loop; every UI touch goes through call_from_thread."""
        worker = get_current_worker()

        def live() -> bool:  # false once superseded by a newer search
            return not worker.is_cancelled

        def progress(p: Progress) -> None:
            if live() and not token.cancelled:
                self.call_from_thread(self._progress, p)

        def on_skip(col, exc) -> None:
            if live():
                self.call_from_thread(self._add_skipped, col, exc)

        def on_column(outcome: ColumnOutcome) -> None:
            if live():
                self.call_from_thread(self._add_outcome, outcome)

        try:
            for m in search_core.search(
                session.cfg, opts, progress, on_skip, pool=session.pool, on_column=on_column, cancel=token
            ):
                if not live():  # superseded by a newer search: stop and clean up
                    return
                self.call_from_thread(self._add_match, m)
        except Exception as exc:  # surface DB/connection errors in the UI instead of crashing
            if live():
                self.call_from_thread(self._failed, f"Error: {exc}")
            return
        if live():
            self.call_from_thread(self._finished, token.cancelled)

    def _progress(self, p: Progress) -> None:
        self.query_one("#progress", ProgressBar).update(progress=round(p.fraction * 100))
        self._status(_progress_text(p))

    def _failed(self, text: str) -> None:
        self._set_running(False)
        if not self.matches:
            self._show_hits("The search stopped before any match was found.")
        self._status(text, "error")

    def _finished(self, cancelled: bool) -> None:
        n = len(self.matches)
        self._set_running(False)
        if not n:
            self._show_hits("No matches." + _no_match_hint(self.opts))
        if cancelled:
            self.partial = True
            self._status(f"Cancelled: {n} match(es) so far (partial)", "warn")
            return
        more = sum(1 for o in self.outcomes.values() if o.more_available)
        note = f"; {more} column(s) have more, select one and press Show all hits" if more else ""
        skip = f"; skipped {self.skipped} column(s), see below" if self.skipped else ""
        self._status(f"Done: {n} match(es){note}{skip}", "warn" if more or self.skipped else "ok")

    def action_cancel_search(self) -> None:
        """Stop the running search or drill-down, interrupting the database as well."""
        if self.token is not None and not self.token.cancelled:
            self.token.cancel()

    def _add_outcome(self, outcome: ColumnOutcome) -> None:
        ref = outcome.column.ref
        self.outcomes[ref] = outcome
        if outcome.note:
            self.cut_off.add(outcome.column.address)
        if not outcome.examples:
            return
        found = self.query_one("#found", DataTable)
        found.add_row(
            ref,
            Text(str(outcome.examples), justify="right"),
            Text(outcome.note, style=AMBER_DIM if outcome.timed_out else AMBER),
            key=ref,
        )

    def show_all_hits(self) -> None:
        """Replace the results with every hit for the column selected in the found table."""
        outcome = self._selected_outcome()
        if outcome is None:
            self._status("Select a column in the list of where it was found first.", "warn")
            return
        ref = outcome.column.ref
        self._start_results(limited=False)  # a single column, complete: exports without the complete column
        self._show_hits()
        self._status(f"Fetching every hit in {ref}...")
        every = replace(self.opts, limit_per_column=None, time_limit=None)
        self._drill_worker(self.session, outcome.column, every, self.token, ref)

    def _selected_outcome(self) -> ColumnOutcome | None:
        """The outcome of the row selected in the found table, if there is a usable one."""
        found = self.query_one("#found", DataTable)
        if self.session is None or self.opts is None or not found.row_count:
            return None
        ref = None
        with contextlib.suppress(Exception):  # no row under the cursor
            ref = found.coordinate_to_cell_key(found.cursor_coordinate).row_key.value
        return self.outcomes.get(ref)

    @work(thread=True, exclusive=True, group="search")
    def _drill_worker(self, session: db.Session, column: Column, opts, token: CancelToken, ref: str) -> None:
        worker = get_current_worker()
        try:
            for m in search_core.drill_down(session.pool, column, opts, token):
                if worker.is_cancelled:
                    return
                self.call_from_thread(self._add_match, m)
        except ColumnTimeout as exc:  # hits found are kept, but the list is not every hit
            if not worker.is_cancelled:
                self.call_from_thread(self._drill_end, f"Timed out ({exc}): {len(self.matches)} hit(s) in {ref} "
                                      "so far (partial)", True)
            return
        except Exception as exc:
            if not worker.is_cancelled:
                self.call_from_thread(self._failed, f"Error: {exc}")
            return
        if not worker.is_cancelled:
            if token.cancelled:
                self.call_from_thread(self._drill_end, f"Cancelled: {len(self.matches)} hit(s) in {ref} so far (partial)", True)
            else:
                self.call_from_thread(self._drill_end, f"All {len(self.matches)} hit(s) in {ref}", False)

    def _drill_end(self, text: str, partial: bool) -> None:
        """A drill-down finished; `partial` means it was cancelled or timed out."""
        if partial:
            self.partial = True
        self._set_running(False)
        self._status(text, "warn" if partial else "ok")

    def _add_skipped(self, col, exc) -> None:
        self.skipped += 1
        table = self.query_one("#skipped", DataTable)
        table.display = True
        reason = str(exc).splitlines()[0]
        table.add_row(col.ref, reason)

    def _add_match(self, m: Match) -> None:
        self.matches.append(m)
        if len(self.matches) == 1:
            self._show_hits()
        if m.snippet is None:  # a full value: mark the term with the cosmetic pattern
            value = Text(" ".join(m.value.split()), no_wrap=True, overflow="ellipsis")
            if self._highlight is not None:
                value.highlight_regex(self._highlight, HIT_STYLE)
        else:  # a snippet: Oracle says exactly which text matched
            before, hit, after = parts(m, fold=True)
            value = Text(before, no_wrap=True, overflow="ellipsis")
            value.append(hit, style=HIT_STYLE)
            value.append(after)
            if m.snippet.more_in_value:
                value.append(" +", style="dim")
        self.query_one("#results", DataTable).add_row(
            Text(m.owner, style="dim"), Text(m.table, style="dim"), Text(m.column, style="bold"), value,
            key=str(len(self.matches) - 1),
        )

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        """Enter on a hit opens the detail window (a large window around the hit); Enter on the
        searched column of the whole-row sheet opens the same window."""
        if self.session is None or self.opts is None:
            return
        if event.data_table.id == "results":
            self._open_detail(self.matches[int(event.cell_key.row_key.value)])

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on the searched column of the whole-row sheet opens the hit in context."""
        if event.data_table.id == "row-fields" and isinstance(self.screen, RowScreen):
            if event.row_key.value == self.screen.match.column:
                self._open_detail(self.screen.match)

    def _open_detail(self, m: Match) -> None:
        if m.rowid is None or self.session is None or self.opts is None:
            return
        screen = DetailScreen(m)
        self.push_screen(screen)
        self._detail_worker(self.session, m, self.opts, screen)

    def action_whole_row(self) -> None:
        """r / the Whole row button: every column of the row behind the selected hit."""
        if isinstance(self.screen, ModalScreen):
            return
        table = self.query_one("#results", DataTable)
        if not self.matches or table.row_count == 0:
            self._status("Select a hit first.", "warn")
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        self.show_row(self.matches[int(row_key.value)])

    def show_row(self, m: Match) -> None:
        if m.rowid is None or self.session is None:
            return
        screen = RowScreen(m)
        self.push_screen(screen)
        self._row_worker(self.session, m, screen)

    @work(thread=True, exclusive=True, group="detail")
    def _row_worker(self, session: db.Session, m: Match, screen: RowScreen) -> None:
        try:
            with db.acquire(session.pool) as conn:
                fields = search_core.fetch_row(conn, m)
        except Exception as exc:
            self.call_from_thread(screen.show_error, f"Error: {str(exc).splitlines()[0]}")
            return
        self.call_from_thread(screen.show_row, fields)

    @work(thread=True, exclusive=True, group="detail")
    def _detail_worker(self, session: db.Session, m: Match, opts: SearchOptions, screen: DetailScreen) -> None:
        try:
            with db.acquire(session.pool) as conn:
                detail = search_core.fetch_detail(conn, m, opts)
        except Exception as exc:
            self.call_from_thread(screen.show_error, f"Error: {str(exc).splitlines()[0]}")
            return
        self.call_from_thread(screen.show_detail, detail)

    def export_csv(self) -> None:
        path = self._value("csv_path") or "scout_results.csv"
        if self.partial:  # cancelled or timed out: every row came from an unfinished search
            incomplete = {m.address for m in self.matches}
        else:
            incomplete = self.cut_off
        labelled = self.limited or self.partial or bool(incomplete)
        try:
            with open(path, "w", newline="") as fp:
                n = write_csv(self.matches, fp, incomplete if labelled else None)
        except OSError as exc:
            self._status(f"Export failed: {exc}", "error")
            return
        if self.partial:
            note = " (partial: the search was cancelled or timed out)"
        elif incomplete:
            note = " (examples only for columns marked complete=no; use Show all hits for the rest)"
        else:
            note = ""
        self._status(f"Wrote {n} row(s) to {path}{note}", "warn" if note else "ok")


def _no_match_hint(opts: SearchOptions | None) -> str:
    if opts is None:
        return ""
    if opts.whole_word:
        return " Try turning off whole word."
    if opts.case_sensitive:
        return " Try turning off case sensitive."
    return " Try fewer words, or a * wildcard."


def _highlight_pattern(opts: SearchOptions) -> re.Pattern | None:
    """Cosmetic: a regex that marks the searched text in a hit. Oracle decides what matched;
    this only paints it, so any term it cannot express simply goes unmarked."""
    parts = []
    for term in opts.terms:
        out, i = [], 0
        while i < len(term):
            c = term[i]
            if c == "\\" and i + 1 < len(term):
                out.append(re.escape(term[i + 1]))
                i += 2
                continue
            if c == "*":
                out.append(r"\w*")
            elif c == "?":
                out.append(r"\w")
            else:
                out.append(re.escape(c))
            i += 1
        parts.append("".join(out))
    if not parts:
        return None
    try:
        return re.compile("|".join(parts), 0 if opts.case_sensitive else re.IGNORECASE)
    except re.error:
        return None


def _progress_text(p: Progress) -> str:
    """One status line: how far along, hits so far, and the table taking longest."""
    if p.columns_total == 0 and p.fraction < 1:
        return "Looking for tables..."
    text = f"Searching {p.fraction:.0%} ({p.columns_done}/{p.columns_total} columns) · {p.hits} hits"
    if p.in_flight:
        name, secs = p.in_flight[0]
        text += f" · slowest: {name} {secs:.0f}s"
    return text


if __name__ == "__main__":
    ScoutApp().run()
